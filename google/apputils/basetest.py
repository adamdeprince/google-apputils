#!/usr/bin/env python
# Copyright 2010 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Base functionality for google tests.

This module contains base classes and high-level functions for Google-style
tests.
"""

__author__ = 'dborowitz@google.com (Dave Borowitz)'

import difflib
import getpass
import os
import pprint
import re
import subprocess
import sys
import tempfile
import types
import unittest

import itertools

from google.apputils import app
import gflags as flags
from google.apputils import shellutil

FLAGS = flags.FLAGS

# ----------------------------------------------------------------------
# Internal functions to extract default flag values from environment.
# ----------------------------------------------------------------------
def _GetDefaultTestRandomSeed():
  random_seed = 301
  value = os.environ.get('TEST_RANDOM_SEED', '')
  try:
    random_seed = int(value)
  except ValueError:
    pass
  return random_seed


def _GetDefaultTestTmpdir():
  tmpdir = os.environ.get('TEST_TMPDIR', '')
  if not tmpdir:
    tmpdir = os.path.join(tempfile.gettempdir(), 'google_apputils_basetest')

  return tmpdir


flags.DEFINE_integer('test_random_seed', _GetDefaultTestRandomSeed(),
                     'Random seed for testing. Some test frameworks may '
                     'change the default value of this flag between runs, so '
                     'it is not appropriate for seeding probabilistic tests.',
                     allow_override=1)
flags.DEFINE_string('test_srcdir',
                    os.environ.get('TEST_SRCDIR', ''),
                    'Root of directory tree where source files live',
                    allow_override=1)
flags.DEFINE_string('test_tmpdir', _GetDefaultTestTmpdir(),
                    'Directory for temporary testing files',
                    allow_override=1)


class BeforeAfterTestCaseMeta(type):

  """Adds setUpTestCase() and tearDownTestCase() methods.

  These may be needed for setup and teardown of shared fixtures usually because
  such fixtures are expensive to setup and teardown (eg Perforce clients).  When
  using such fixtures, care should be taken to keep each test as independent as
  possible (eg via the use of sandboxes).

  Example:

    class MyTestCase(basetest.TestCase):

      __metaclass__ = basetest.BeforeAfterTestCaseMeta

      @classmethod
      def setUpTestCase(cls):
        cls._resource = foo.ReallyExpensiveResource()

      @classmethod
      def tearDownTestCase(cls):
        cls._resource.Destroy()

      def testSomething(self):
        self._resource.Something()
        ...
  """

  _test_loader = unittest.defaultTestLoader

  def __init__(cls, name, bases, dict):
    type.__init__(cls, name, bases, dict)

    # Notes from mtklein

    # This code can be tricky to think about.  Here are a few things to remember
    # as you read through it.

    # When inheritance is involved, this __init__ is called once on each class
    # in the inheritance chain when that class is defined.  In a typical
    # scenario where a BaseClass inheriting from TestCase declares the
    # __metaclass__ and SubClass inherits from BaseClass, __init__ will be first
    # called with cls=BaseClass when BaseClass is defined, and then called later
    # with cls=SubClass when SubClass is defined.

    # To know when to call setUpTestCase and tearDownTestCase, this class wraps
    # the setUp, tearDown, and test* methods in a TestClass.  We'd like to only
    # wrap those methods in the leaves of the inheritance tree, but we can't
    # know when we're a leaf at wrapping time.  So instead we wrap all the
    # setUp, tearDown, and test* methods, but code them so that we only do the
    # counting we want at the leaves, which we *can* detect when we've got an
    # actual instance to look at --- i.e. self, when a method is running.

    # Because we're wrapping at every level of inheritance, some methods get
    # wrapped multiple times down the inheritance chain; if SubClass were to
    # inherit, say, setUp or testFoo from BaseClass, that method would be
    # wrapped twice, first by BaseClass then by SubClass.  That's OK, because we
    # ensure that the extra code we inject with these wrappers is idempotent.

    # test_names are the test methods this class can see.
    test_names = set(cls._test_loader.getTestCaseNames(cls))

    # Each class keeps a set of the tests it still has to run.  When it's empty,
    # we know we should call tearDownTestCase.  For now, it holds the sentinel
    # value of None, acting as a indication that we need to call setUpTestCase,
    # which fills in the actual tests to run.
    cls.__tests_to_run = None

    # These calls go through and monkeypatch various methods, in no particular
    # order.
    BeforeAfterTestCaseMeta.SetSetUpAttr(cls, test_names)
    BeforeAfterTestCaseMeta.SetTearDownAttr(cls)
    BeforeAfterTestCaseMeta.SetTestMethodAttrs(cls, test_names)
    BeforeAfterTestCaseMeta.SetBeforeAfterTestCaseAttr()

  # Just a little utility function to help with monkey-patching.
  @staticmethod
  def SetMethod(cls, method_name, replacement):
    """Like setattr, but also preserves name, doc, and module metadata."""
    original = getattr(cls, method_name)
    replacement.__name__ = original.__name__
    replacement.__doc__ = original.__doc__
    replacement.__module__ = original.__module__
    setattr(cls, method_name, replacement)

  @staticmethod
  def SetSetUpAttr(cls, test_names):
    """Wraps setUp() with per-class setUp() functionality."""
    # Remember that SetSetUpAttr is eventually called on each class in the
    # inheritance chain.  This line can be subtle because of inheritance.  Say
    # we've got BaseClass that defines setUp, and SubClass inheriting from it
    # that doesn't define setUp.  This method will run twice, and both times
    # cls_setUp will be BaseClass.setUp.  This is one of the tricky cases where
    # setUp will be wrapped multiple times.
    cls_setUp = cls.setUp

    # We create a new setUp method that first checks to see if we need to run
    # setUpTestCase (looking for the __tests_to_run==None flag), and then runs
    # the original setUp method.
    def setUp(self):
      # This line is unassuming but crucial to making this whole system work.
      # It sets leaf to the class of the instance we're currently testing.  That
      # is, leaf is going to be a leaf class.  It's not necessarily the same
      # class as the parameter cls that's being passed in.  For example, in the
      # case above where setUp is in BaseClass, when we instantiate a SubClass
      # and call setUp, we need leaf to be pointing at the class SubClass.
      leaf = self.__class__

      # The reason we want to do this is that it makes sure setUpTestCase is
      # only run once, not once for each class down the inheritance chain.  When
      # multiply-wrapped, this extra code is called multiple times.  In the
      # running example:
      #
      #  1) cls=BaseClass: replace BaseClass' setUp with a wrapped setUp
      #  2) cls=SubClass: set SubClass.setUp to what it thinks was its original
      #     setUp --- the wrapped setUp from 1)
      #
      # So it's double-wrapped, but that's OK.  When we actually call setUp from
      # an instance, we're calling the double-wrapped method.  It sees
      # __tests_to_run is None and fills that in.  Then it calls what it thinks
      # was its original setUp, the singly-wrapped setUp from BaseClass.  The
      # singly-wrapped setUp *skips* the if-statement, as it sees
      # leaf.__tests_to_run is not None now.  It just runs the real, original
      # setUp().

      # test_names is passed in from __init__, and holds all the test cases that
      # cls can see.  In the BaseClass call, that's probably the empty set, and
      # for SubClass it'd have your test methods.

      if leaf.__tests_to_run is None:
        leaf.__tests_to_run = set(test_names)
        self.setUpTestCase()
      cls_setUp(self)

    # Monkeypatch our new setUp method into the place of the original.
    BeforeAfterTestCaseMeta.SetMethod(cls, 'setUp', setUp)

  @staticmethod
  def SetTearDownAttr(cls):
    """Wraps tearDown() with per-class tearDown() functionality."""

    # This is analagous to SetSetUpAttr, except of course it's patching tearDown
    # to run tearDownTestCase when there are no more tests to run.  All the same
    # hairy logic applies.
    cls_tearDown = cls.tearDown

    def tearDown(self):
      cls_tearDown(self)

      leaf = self.__class__
      if leaf.__tests_to_run is not None and len(leaf.__tests_to_run) == 0:
        leaf.__tests_to_run = None
        self.tearDownTestCase()

    BeforeAfterTestCaseMeta.SetMethod(cls, 'tearDown', tearDown)

  @staticmethod
  def SetTestMethodAttrs(cls, test_names):
    """Makes each test method first remove itself from the remaining set."""
    # This makes each test case remove itself from the set of remaining tests.
    # You might think that this belongs more logically in tearDown, and I'd
    # agree except that tearDown doesn't know what test case it's tearing down!
    # Instead we have the test method itself remove itself before attempting the
    # test.

    # Note that having the test remove itself after running doesn't work, as we
    # never get to 'after running' for tests that fail.

    # Like setUp and tearDown, the test case could conceivably be wrapped
    # twice... but as noted it's an implausible situation to have an actual test
    # defined in a base class.  Just in case, we take the same precaution by
    # looking in only the leaf class' set of __tests_to_run, and using discard()
    # instead of remove() to make the operation idempotent.

    for test_name in test_names:
      cls_test = getattr(cls, test_name)

      # The default parameters here make sure that each new test() function
      # remembers its own values of cls_test and test_name.  Without these
      # default parameters, they'd all point to the values from the last
      # iteration of the loop, causing some arbitrary test method to run
      # multiple times and the others never. :(
      def test(self, cls_test=cls_test, test_name=test_name):
        leaf = self.__class__
        leaf.__tests_to_run.discard(test_name)
        return cls_test(self)

      BeforeAfterTestCaseMeta.SetMethod(cls, test_name, test)

  @staticmethod
  def SetBeforeAfterTestCaseAttr():
    # This just makes sure every TestCase has a setUpTestCase or
    # tearDownTestCase, so that you can safely define only one or neither of
    # them if you want.
    TestCase.setUpTestCase = lambda self: None
    TestCase.tearDownTestCase = lambda self: None


class TestCase(unittest.TestCase):
  """Extension of unittest.TestCase providing more powerful assertions."""

  def __init__(self, methodName='runTest'):
    super(TestCase, self).__init__(methodName)
    self.__recorded_properties = {}

  def shortDescription(self):
    """Format both the test method name and the first line of its docstring.

    If no docstring is given, only returns the method name.

    This method overrides unittest.TestCase.shortDescription(), which
    only returns the first line of the docstring, obscuring the name
    of the test upon failure.
    """
    desc = str(self)
    # NOTE: super() is used here instead of directly invoking
    # unittest.TestCase.shortDescription(self), because of the
    # following line that occurs later on:
    #       unittest.TestCase = TestCase
    # Because of this, direct invocation of what we think is the
    # superclass will actually cause infinite recursion.
    doc_first_line = super(TestCase, self).shortDescription()
    if doc_first_line is not None:
      desc = '\n'.join((desc, doc_first_line))
    return desc

  # add Python 2.3 float-related methods if we don't already have them
  if not hasattr(unittest.TestCase, 'failUnlessAlmostEqual'):

    def failUnlessAlmostEqual(self, first, second, places=7, msg=None):
      """Fail if two objects are unequal up to a number of decimal places.

      Args:
        first:
        second:
        places:  Number of decimal places to which the difference between
          first and second is rounded; if this is nonzero after rounding, the
          test fails.  Note that decimal places (from zero) are usually not
          the same as significant digits (measured from the most signficant
          digit).
        msg:  Message to print if the test fails.
      Raises:
        AssertionError: the objects are not close enough to equal.
      """
      if round(second-first, places) != 0:
        raise self.failureException(
            msg or '%r != %r within %r places' % (first, second, places))

    assertAlmostEqual = assertAlmostEquals = failUnlessAlmostEqual

  if not hasattr(unittest.TestCase, 'failIfAlmostEqual'):

    def failIfAlmostEqual(self, first, second, places=7, msg=None):
      """Fail if two objects are equal up to a number of decimal places.

      Args:
        first: The first object to compare.
        second: The second object to compare.
        places: The number of decimal places (default: 7)
          considered significant.  The difference between first and
          second is rounded to this number of decimal places before
          being compared with zero.  This is not the same as the
          number of significant digits.
        msg:  Error message used if the test fails.
      Raises:
        AssertionError: the objects are close enough to equal.
      """
      if round(second-first, places) == 0:
        raise self.failureException(
            msg or '%r == %r within %r places' % (first, second, places))

    assertNotAlmostEqual = assertNotAlmostEquals = failIfAlmostEqual

  def failIfEqual(self, first, second, msg=None):
    """Verify that first != second.

    The base unittest.failIfEqual() method only uses the '==' operator.
    Hence unittest.py never exercises the __ne__ method if your class
    defines one.   This method therefore uses both.

    Args:
      first:  First item to compare for equality.
      second:  Second item to compare for equality.
      msg:  Message to use to describe the test failure.
    Raises:
      AssertionError: if the test failed.
    """
    if first == second:
      raise self.failureException(msg or
                                  '%r == %r' % (first, second))
    if not first != second:
      raise self.failureException(msg or
                                  '%r != %r returns False' % (first, second))

  # While one could argue that the behaviour of assertNotEquals
  # and failIfEqual should be different for objects which have NaN-like
  # semantics, these methods in unittest.py are equivalent.  We assume
  # that it's better to preserve that property than to support classes
  # where __ne__ and __eq__ can both return False for the same pair of objects.
  assertNotEquals = assertNotEqual = failIfEqual

  def assertSequenceEqual(self, seq1, seq2, msg=None, seq_type=None):
    """An equality assertion for ordered sequences (like lists and tuples).

    For the purposes of this function, a valid orderd sequence type is one which
    can be indexed, has a length, and has an equality operator.

    Args:
      seq1: The first sequence to compare.
      seq2: The second sequence to compare.
      msg: Optional message to use on failure instead of a list of differences.
      seq_type: The expected datatype of the sequences, or None if no datatype
          should be enforced.
    """
    if seq_type != None:
      seq_type_name = seq_type.__name__
      assert isinstance(seq1, seq_type), ('First sequence is not a %s: %r' %
                                          (seq_type_name, seq1))
      assert isinstance(seq2, seq_type), ('Second sequence is not a %s: %r' %
                                          (seq_type_name, seq2))
    else:
      seq_type_name = 'sequence'

    differing = None
    try:
      len1 = len(seq1)
    except (TypeError, NotImplementedError):
      differing = 'First %s has no length.  Non-sequence?' % (seq_type_name)

    if differing is None:
      try:
        len2 = len(seq2)
      except (TypeError, NotImplementedError):
        differing = 'Second %s has no length.  Non-sequence?' % (seq_type_name)

    if differing is None:
      if seq1 == seq2:
        return

      for i in xrange(min(len1, len2)):
        try:
          item1 = seq1[i]
        except (TypeError, IndexError, NotImplementedError):
          differing = ('Unable to index element %d of first %s\n' %
                       (i, seq_type_name))
          break

        try:
          item2 = seq2[i]
        except (TypeError, IndexError, NotImplementedError):
          differing = ('Unable to index element %d of second %s\n' %
                       (i, seq_type_name))
          break

        if item1 != item2:
          differing = ('First differing element %d:\n%s\n%s\n' %
                       (i, item1, item2))
          break
      else:
        if len1 == len2 and seq_type is None and type(seq1) != type(seq2):
          # The sequences are the same, but have differing types.
          return
        # A catch-all message for handling arbitrary user-defined sequences.
        differing = '%ss differ:\n' % seq_type_name.capitalize()
        if len1 > len2:
          differing = ('First %s contains %d additional elements.\n' %
                       (seq_type_name, len1 - len2))
          try:
            differing += ('First extra element %d:\n%s\n' %
                          (len2, seq1[len2]))
          except (TypeError, IndexError, NotImplementedError):
            differing += ('Unable to index element %d of first %s\n' %
                          (len2, seq_type_name))
        elif len1 < len2:
          differing = ('Second %s contains %d additional elements.\n' %
                       (seq_type_name, len2 - len1))
          try:
            differing += ('First extra element %d:\n%s\n' %
                          (len1, seq2[len1]))
          except (TypeError, IndexError, NotImplementedError):
            differing += ('Unable to index element %d of second %s\n' %
                          (len1, seq_type_name))
    if not msg:
      msg = '\n'.join(difflib.ndiff(pprint.pformat(seq1).splitlines(),
                                    pprint.pformat(seq2).splitlines()))
    self.fail(differing + msg)

  def assertListEqual(self, list1, list2, msg=None):
    """A list-specific equality assertion.

    Args:
      list1: the first list to compare
      list2: the second list to compare
      msg: optional message to use on failure instead of a list of differences
    """
    self.assertSequenceEqual(list1, list2, msg, seq_type=list)

  def assertTupleEqual(self, tuple1, tuple2, msg=None):
    """A tuple-specific equality assertion.

    Args:
      tuple1: the first tuple to compare
      tuple2: the second tuple to compare
      msg: optional message to use on failure instead of a list of differences
    """
    self.assertSequenceEqual(tuple1, tuple2, msg, seq_type=tuple)

  def assertSetEqual(self, set1, set2, msg=None):
    """A set-specific equality assertion.

    Args:
      set1: the first set to compare
      set2: the second set to compare
      msg: optional message to use on failure instead of a list of differences

    For more general containership equality, assertSameElements will work
    with things other than sets.  This uses ducktyping to support different
    types of sets, and is optimized for sets specifically (parameters must
    support a difference method).
    """
    try:
      difference1 = set1.difference(set2)
    except TypeError, e:
      self.fail('invalid type when attempting set difference: %s' % e)
    except AttributeError, e:
      self.fail('first argument does not support set difference: %s' % e)

    try:
      difference2 = set2.difference(set1)
    except TypeError, e:
      self.fail('invalid type when attempting set difference: %s' % e)
    except AttributeError, e:
      self.fail('second argument does not support set difference: %s' % e)

    if not (difference1 or difference2):
      return

    if msg is not None:
      self.fail(msg)

    lines = []
    if difference1:
      lines.append('Items in the first set but not the second:')
      for item in difference1:
        lines.append(repr(item))
    if difference2:
      lines.append('Items in the second set but not the first:')
      for item in difference2:
        lines.append(repr(item))
    self.fail('\n'.join(lines))

  def assertIn(self, a, b, msg=None):
    """Just like self.assert_(a in b), but with a nicer default message."""
    if msg is None:
      msg = '"%s" not found in "%s"' % (a, b)
    self.assert_(a in b, msg)

  def assertNotIn(self, a, b, msg=None):
    """Just like self.assert_(a not in b), but with a nicer default message."""
    if msg is None:
      msg = '"%s" unexpectedly found in "%s"' % (a, b)
    self.assert_(a not in b, msg)

  def assertDictEqual(self, d1, d2, msg=None):
    assert isinstance(d1, dict), 'First argument is not a dict: %r' % d1
    assert isinstance(d2, dict), 'Second argument is not a dict: %r' % d2

    if d1 != d2:

      # Sort by keys so that the contents are diffed in the same order.
      def YieldSortedLines(d):
        for k, v in sorted(d.iteritems()):
          yield '%r: %r' % (k, v)

      self.fail(msg or ('Dicts differ:\n' + '\n'.join(difflib.ndiff(
          list(YieldSortedLines(d1)),
          list(YieldSortedLines(d2))))))

  def assertDictContainsSubset(self, expected, actual, msg=None):
    """Checks whether actual is a superset of expected."""
    missing = []
    mismatched = []
    for key, value in expected.iteritems():
      if key not in actual:
        missing.append(key)
      elif value != actual[key]:
        mismatched.append('%s, expected: %s, actual: %s' % (key, value,
                                                            actual[key]))

    if not (missing or mismatched):
      return

    missing_msg = mismatched_msg = ''
    if missing:
      missing_msg = 'Missing: %s' % ','.join(missing)
    if mismatched:
      mismatched_msg = 'Mismatched values: %s' % ','.join(mismatched)

    if msg:
      msg = '%s: %s; %s' % (msg, missing_msg, mismatched_msg)
    else:
      msg = '%s; %s' % (missing_msg, mismatched_msg)
    self.fail(msg)

  def assertSameElements(self, expected_seq, actual_seq, msg=None):
    """Assert that two sequences have the same elements (in any order)."""
    try:
      expected = dict([(element, None) for element in expected_seq])
      actual = dict([(element, None) for element in actual_seq])
      missing = [element for element in expected if element not in actual]
      unexpected = [element for element in actual if element not in expected]
      missing.sort()
      unexpected.sort()
    except TypeError:
      # Fall back to slower list-compare if any of the objects are
      # not hashable.
      expected = list(expected_seq)
      actual = list(actual_seq)
      expected.sort()
      actual.sort()
      missing, unexpected = _SortedListDifference(expected, actual)
    errors = []
    if missing:
      errors.append('Expected, but missing:\n  %r\n' % missing)
    if unexpected:
      errors.append('Unexpected, but present:\n  %r\n' % unexpected)
    if errors:
      self.fail(msg or ''.join(errors))

  def assertMultiLineEqual(self, first, second, msg=None):
    """Assert that two multi-line strings are equal."""
    assert isinstance(first, types.StringTypes), (
        'First argument is not a string: %r' % first)
    assert isinstance(second, types.StringTypes), (
        'Second argument is not a string: %r' % second)

    if first != second:
      raise self.failureException(
          msg or '\n' + ''.join(difflib.ndiff(first.splitlines(True),
                                              second.splitlines(True))))

  def assertLess(self, a, b, msg=None):
    """Just like self.assert_(a < b), but with a nicer default message."""
    if msg is None:
      msg = '"%r" unexpectedly not less than "%r"' % (a, b)
    self.assert_(a < b, msg)

  def assertLessEqual(self, a, b, msg=None):
    """Just like self.assert_(a <= b), but with a nicer default message."""
    if msg is None:
      msg = '"%r" unexpectedly not less than or equal to "%r"' % (a, b)
    self.assert_(a <= b, msg)

  def assertGreater(self, a, b, msg=None):
    """Just like self.assert_(a > b), but with a nicer default message."""
    if msg is None:
      msg = '"%r" unexpectedly not greater than "%r"' % (a, b)
    self.assert_(a > b, msg)

  def assertGreaterEqual(self, a, b, msg=None):
    """Just like self.assert_(a >= b), but with a nicer default message."""
    if msg is None:
      msg = '"%r" unexpectedly not greater than or equal to "%r"' % (a, b)
    self.assert_(a >= b, msg)

  # TODO(user): Maybe add a assertWithinTolerance(a, b, t) to
  # check (b - t) <= a <= (b + t), if it's needed by more people than just me

  def assertIsNone(self, obj, msg=None):
    """Just like self.assert_(obj is None), but with a nicer default message."""
    if msg is None:
      msg = '"%s" unexpectedly not None' % obj
    self.assert_(obj is None, msg)

  def assertIsNotNone(self, obj, msg='unexpectedly None'):
    """Included for symmetry with assertIsNone."""
    self.assert_(obj is not None, msg)

  def assertBetween(self, value, minv, maxv, msg=None):
    """Asserts that value is between minv and maxv (inclusive)."""
    if msg is None:
      msg = '"%r" unexpectedly not between "%r" and "%r"' % (value, minv, maxv)
    self.assert_(minv <= value, msg)
    self.assert_(maxv >= value, msg)

  def assertRegexMatch(self, actual_str, regexes, message=None):
    """Asserts that at least one regex in regexes matches str.

    Notes:
    1. This function uses substring matching, i.e. the matching
       succeeds if *any* substring of the error message matches *any*
       regex in the list.  This is more convenient for the user than
       full-string matching.

    2. If regexes is the empty list, the matching will always fail.

    3. Use regexes=[''] for a regex that will always pass.

    4. '.' matches any single character *except* the newline.  To
       match any character, use '(.|\n)'.

    5. '^' matches the beginning of each line, not just the beginning
       of the string.  Similarly, '$' matches the end of each line.

    6. An exception will be thrown if regexes contains an invalid
       regex.

    Args:
      actual_str:  The string we try to match with the items in regexes.
      regexes:  The regular expressions we want to match against str.
        See "Notes" above for detailed notes on how this is interpreted.
      message:  The message to be printed if the test fails.
    """
    if message is None: message = 'Regexes not found.: %s' % regexes

    if not regexes:
      self.fail('No regexes specified.')

    regex_str = '(.|\n)*((%s))(.|\n)*' % ')|('.join(regexes)
    regex = re.compile(regex_str, re.MULTILINE)

    self.assert_(regex.match(actual_str) is not None, message)

  def assertCommandSucceeds(self, command, regexes=[''], env=None,
                            close_fds=True):
    """Asserts that a shell command succeeds (i.e. exits with code 0).

    Args:
      command: List or string representing the command to run.
      regexes: List of regular expression strings.
      env: Dictionary of environment variable settings.
      close_fds: Whether or not to close all open fd's in the child after
        forking.
    """
    (ret_code, err) = GetCommandStderr(command, env, close_fds)

    command_string = GetCommandString(command)
    self.assert_(
        ret_code == 0,
        'Running command\n'
        '%s failed with error code %s and message\n'
        '%s' % (
            _QuoteLongString(command_string),
            ret_code,
            _QuoteLongString(err)))
    self.assertRegexMatch(
        err,
        regexes,
        message=(
            'Running command\n'
            '%s failed with error code %s and message\n'
            '%s which matches no regex in %s' % (
                _QuoteLongString(command_string),
                ret_code,
                _QuoteLongString(err),
                regexes)))

  def assertCommandFails(self, command, regexes, env=None, close_fds=True):
    """Asserts a shell command fails and the error matches a regex in a list.

    Args:
      command: List or string representing the command to run.
      regexes: the list of regular expression strings.
      env: Dictionary of environment variable settings.
      close_fds: Whether or not to close all open fd's in the child after
        forking.
    """
    (ret_code, err) = GetCommandStderr(command, env, close_fds)

    command_string = GetCommandString(command)
    self.assert_(
        ret_code != 0,
        'The following command succeeded while expected to fail:\n%s' %
        _QuoteLongString(command_string))
    self.assertRegexMatch(
        err,
        regexes,
        message=(
            'Running command\n'
            '%s failed with error code %s and message\n'
            '%s which matches no regex in %s' % (
                _QuoteLongString(command_string),
                ret_code,
                _QuoteLongString(err),
                regexes)))

  def assertRaisesWithRegexpMatch(self, expected_exception, expected_regexp,
                                  callable_obj, *args, **kwargs):
    """Asserts that the message in a raised exception matches the given regexp.

    Args:
      expected_exception: Exception class expected to be raised.
      expected_regexp: Regexp (re pattern object or string) expected to be
        found in error message.
      callable_obj: Function to be called.
      args: Extra args.
      kwargs: Extra kwargs.
    """
    try:
      callable_obj(*args, **kwargs)
    except expected_exception, err:
      if isinstance(expected_regexp, basestring):
        expected_regexp = re.compile(expected_regexp)
      self.assert_(
          expected_regexp.search(str(err)),
          '"%s" does not match "%s"' % (expected_regexp.pattern, str(err)))
    else:
      self.fail(expected_exception.__name__ + ' not raised')

  def assertContainsInOrder(self, strings, target):
    """Asserts that the strings provided are found in the target in order.

    This may be useful for checking HTML output.

    Args:
      strings: A list of strings, such as [ 'fox', 'dog' ]
      target: A target string in which to look for the strings, such as
        'The quick brown fox jumped over the lazy dog'.
    """
    if not isinstance(strings, list):
      strings = [strings]

    current_index = 0
    last_string = None
    for string in strings:
      index = target.find(str(string), current_index)
      if index == -1 and current_index == 0:
        self.fail("Did not find '%s' in '%s'" %
                  (string, target))
      elif index == -1:
        self.fail("Did not find '%s' after '%s' in '%s'" %
                  (string, last_string, target))
      last_string = string
      current_index = index

  def assertTotallyOrdered(self, *groups):
    """Asserts that total ordering has been implemented correctly.

    For example, say you have a class A that compares only on its attribute x.
    Comparators other than __lt__ are omitted for brevity.

    class A(object):
      def __init__(self, x, y):
        self.x = xio
        self.y = y

      def __hash__(self):
        return hash(self.x)

      def __lt__(self, other):
        try:
          return self.x < other.x
        except AttributeError:
          return NotImplemented

    assertTotallyOrdered will check that instances can be ordered correctly.
    For example,

    self.assertTotallyOrdered(
      [None],  # None should come before everything else.
      [1],     # Integers sort earlier.
      ['foo'],  # As do strings.
      [A(1, 'a')],
      [A(2, 'b')],  # 2 is after 1.
      [A(2, 'c'), A(2, 'd')],  # The second argument is irrelevant.
      [A(3, 'z')])

    Args:
     groups: A list of groups of elements.  Each group of elements is a list
       of objects that are equal.  The elements in each group must be less than
       the elements in the group after it.  For example, these groups are
       totally ordered: [None], [1], [2, 2], [3].
    """

    def CheckOrder(small, big):
      """Ensures small is ordered before big."""
      self.assertFalse(small == big,
                       '%r unexpectedly equals %r' % (small, big))
      self.assertTrue(small != big,
                      '%r unexpectedly equals %r' % (small, big))
      self.assertLess(small, big)
      self.assertFalse(big < small,
                       '%r unexpectedly less than %r' % (big, small))
      self.assertLessEqual(small, big)
      self.assertFalse(big <= small,
                       '%r unexpectedly less than or equal to %r'
                       % (big, small))
      self.assertGreater(big, small)
      self.assertFalse(small > big,
                       '%r unexpectedly greater than %r' % (small, big))
      self.assertGreaterEqual(big, small)
      self.assertFalse(small >= big,
                       '%r unexpectedly greater than or equal to %r'
                       % (small, big))

    def CheckEqual(a, b):
      """Ensures that a and b are equal."""
      self.assertEqual(a, b)
      self.assertFalse(a != b, '%r unexpectedly equals %r' % (a, b))
      self.assertEqual(hash(a), hash(b),
                       'hash %d of %r unexpectedly not equal to hash %d of %r'
                       % (hash(a), a, hash(b), b))
      self.assertFalse(a < b, '%r unexpectedly less than %r' % (a, b))
      self.assertFalse(b < a, '%r unexpectedly less than %r' % (b, a))
      self.assertLessEqual(a, b)
      self.assertLessEqual(b, a)
      self.assertFalse(a > b, '%r unexpectedly greater than %r' % (a, b))
      self.assertFalse(b > a, '%r unexpectedly greater than %r' % (b, a))
      self.assertGreaterEqual(a, b)
      self.assertGreaterEqual(b, a)

    # For every combination of elements, check the order of every pair of
    # elements.
    for elements in itertools.product(*groups):
      elements = list(elements)
      for index, small in enumerate(elements[:-1]):
        for big in elements[index + 1:]:
          CheckOrder(small, big)

    # Check that every element in each group is equal.
    for group in groups:
      for a in group:
        CheckEqual(a, a)
      for a, b in itertools.product(group, group):
        CheckEqual(a, b)

  def getRecordedProperties(self):
    """Return any properties that the user has recorded."""
    return self.__recorded_properties

  def recordProperty(self, property_name, property_value):
    """Record an arbitrary property for later use.

    Args:
      property_name: str, name of property to record; must be a valid XML
        attribute name
      property_value: value of property; must be valid XML attribute value
    """
    self.__recorded_properties[property_name] = property_value


def _SortedListDifference(expected, actual):
  """Finds elements in only one or the other of two, sorted input lists.

  Returns a two-element tuple of lists.  The first list contains those
  elements in the "expected" list but not in the "actual" list, and the
  second contains those elements in the "actual" list but not in the
  "expected" list.  Duplicate elements in either input list are ignored.

  Args:
    expected:  The list we expected.
    actual:  The list we actualy got.
  Returns:
    (missing, unexpected)
    missing: items in expected that are not in actual.
    unexpected: items in actual that are not in expected.
  """
  i = j = 0
  missing = []
  unexpected = []
  while True:
    try:
      e = expected[i]
      a = actual[j]
      if e < a:
        missing.append(e)
        i += 1
        while expected[i] == e:
          i += 1
      elif e > a:
        unexpected.append(a)
        j += 1
        while actual[j] == a:
          j += 1
      else:
        i += 1
        try:
          while expected[i] == e:
            i += 1
        finally:
          j += 1
          while actual[j] == a:
            j += 1
    except IndexError:
      missing.extend(expected[i:])
      unexpected.extend(actual[j:])
      break
  return missing, unexpected


def GetCommandString(command):
  """Returns an escaped string that can be used as a shell command.

  Args:
    command: List or string representing the command to run.
  Returns:
    A string suitable for use as a shell command.
  """
  if isinstance(command, types.StringTypes):
    return command
  else:
    return shellutil.ShellEscapeList(command)


def GetCommandStderr(command, env=None, close_fds=True):
  """Runs the given shell command and returns a tuple.

  Args:
    command: List or string representing the command to run.
    env: Dictionary of environment variable settings.
    close_fds: Whether or not to close all open fd's in the child after forking.

  Returns:
    Tuple of (exit status, text printed to stdout and stderr by the command).
  """
  if env is None: env = {}
  # Forge needs PYTHON_RUNFILES in order to find the runfiles directory when a
  # Python executable is run by a Python test.  Pass this through from the
  # parent environment if not explicitly defined.
  if os.environ.get('PYTHON_RUNFILES') and not env.get('PYTHON_RUNFILES'):
    env['PYTHON_RUNFILES'] = os.environ['PYTHON_RUNFILES']

  use_shell = isinstance(command, types.StringTypes)
  process = subprocess.Popen(
      command,
      close_fds=close_fds,
      env=env,
      shell=use_shell,
      stderr=subprocess.STDOUT,
      stdout=subprocess.PIPE)
  output = process.communicate()[0]
  exit_status = process.wait()
  return (exit_status, output)


def _QuoteLongString(s):
  """Quotes a potentially multi-line string to make the start and end obvious.

  Args:
    s: A string.

  Returns:
    The quoted string.
  """
  return ('8<-----------\n' +
          s + '\n' +
          '----------->8\n')


class TestProgramManualRun(unittest.TestProgram):
  """A TestProgram which runs the tests manually."""

  def runTests(self, do_run=False):
    """Run the tests."""
    if do_run:
      unittest.TestProgram.runTests(self)


def main(*args, **keys):
  """Executes a set of Python unit tests.

  Args:
   args: positional arguments passed through to unittest.TestProgram
   keys: keyword arguments passed through to unittest.TestProgram()
  """
  helpflag = app.HelpFlag()
  if helpflag.name not in FLAGS:
    flags.DEFINE_flag(helpflag)  # Register help flag IFF not prev. registered

  argv = app.RegisterAndParseFlagsWithUsage()

  test_runner = keys.get('testRunner')

  # Make sure tmpdir exists
  if not os.path.isdir(FLAGS.test_tmpdir):
    os.makedirs(FLAGS.test_tmpdir)

  # Run main module setup, if it exists
  main = sys.modules['__main__']
  if hasattr(main, 'setUp') and callable(main.setUp):
    main.setUp()

  # Set sys.argv so the unittest module can do its own parsing
  sys.argv = argv

  try:
    result = None
    test_program = TestProgramManualRun(*args, **keys)
    if test_runner:
      test_program.testRunner = test_runner
    else:
      test_program.testRunner = unittest.TextTestRunner(
          verbosity=test_program.verbosity)
    result = test_program.testRunner.run(test_program.test)
  finally:
    # Run main module teardown, if it exists
    if hasattr(main, 'tearDown') and callable(main.tearDown):
      main.tearDown()

  sys.exit(not result.wasSuccessful())