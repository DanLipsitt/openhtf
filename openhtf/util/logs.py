# Copyright 2014 Google Inc. All Rights Reserved.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""Logging mechanisms for use in OpenHTF.

There are two types of logs within OpenHTF, framework logs, and test logs.
Framework logs are logs that are not included in test record output, while
test logs are.  Framework logs are typically internal to OpenHTF, and are
only interesting when debugging weird framework behavior.

Test logs are primarily generated by test authors, and are included in test
record output.  The preferred way to do this is via the 'test' first parameter
passed to test phases:

  def MyLoggingPhase(test):
    test.logger.info('My log line')

In order to facilitate adding logs to the test record output from places
outside the test phase (without forcing the author to pass the logger object
around), a user can directly use a logger instance assocated with the Test's
output TestRecord.  This is accessible via the get_record_logger_for function
in this module, which take's a Test's UID and returns a Python Logger:

  import logging
  from openhtf.util import logs

  class MyHelperClass(object):
    def __init__(self)
      self.test_uid = ''

    def MyRandomMethod(self):
      logs.get_record_logger_for(self.test_uid).info(
          'Log this to currently running test.')

  def MyPhase(test, helper):
    helper.MyRandomMethod()

  if __name__ == '__main__':
    helper = MyHelperClass()
    my_test = openhtf.Test(MyPhase.with_args(helper=helper))
    helper.test_uid = my_test.uid
    my_test.Excute()

Framework logs are by default output only to stderr at a warning level.  They
can be additionally logged to a file (with a different level) via the
--log-file and --log-file-level flags.  The --quiet flag may be set to suppress
all framework log output to stderr.  The --verbosity flag may be used to set
the log level threshold for framework logs output to stderr.

Test record logs are by default output to stdout at a debug level.  There is
no way to change this, if you don't like it redirect stdout to /dev/null.
"""

import argparse
import collections
import logging
import os
import re
import sys
import traceback

from openhtf import util
from openhtf.util import argv
from openhtf.util import functions


DEFAULT_LEVEL = 'warning'
DEFAULT_LOGFILE_LEVEL = 'warning'
QUIET = False
LOGFILE = None

LEVEL_CHOICES = ['debug', 'info', 'warning', 'error', 'critical']
ARG_PARSER = argv.ModuleParser()
ARG_PARSER.add_argument(
    '--verbosity', default=DEFAULT_LEVEL, choices=LEVEL_CHOICES,
    action=argv.StoreInModule, target='%s.DEFAULT_LEVEL' % __name__,
    help='Console log verbosity level (stderr).')
ARG_PARSER.add_argument(
    '--quiet', action=argv.StoreInModule, target='%s.QUIET' % __name__,
    proxy=argparse._StoreTrueAction, help="Don't output logs to stderr.")
ARG_PARSER.add_argument(
    '--log-file', action=argv.StoreInModule, target='%s.LOGFILE' % __name__,
    help='Filename to output logs to, if any.')
ARG_PARSER.add_argument(
    '--log-file-level', default=DEFAULT_LOGFILE_LEVEL, choices=LEVEL_CHOICES,
    action=argv.StoreInModule, target='%s.DEFAULT_LOGFILE_LEVEL' % __name__,
    help='Logging verbosity level for log file output.')

LOGGER_PREFIX = 'openhtf'
RECORD_LOGGER = '.'.join((LOGGER_PREFIX, 'test_record'))

_LOGONCE_SEEN = set()

LogRecord = collections.namedtuple(
    'LogRecord', 'level logger_name source lineno timestamp_millis message')


def get_record_logger_for(test_uid):
  return logging.getLogger('.'.join((RECORD_LOGGER, test_uid)))


def initialize_record_logger(test_uid, test_record, notify_update):
  logger = get_record_logger_for(test_uid)
  # All record loggers have a shared parent that's separately configured, so
  # we want to propagate to that logger.
  logger.propagate = True
  # Just in case, make sure we don't have any extra handlers hanging around.
  logger.addHandler = RecordHandler(test_record, notify_update)
  return logger


def log_once(log_func, msg, *args, **kwargs):
  """"Logs a message only once."""
  if msg not in _LOGONCE_SEEN:
    log_func(msg, *args, **kwargs)
    # Only check the message since it's likely from the source code so lifespan
    # is long and no copies are made.
    _LOGONCE_SEEN.add(msg)


class MacAddressLogFilter(logging.Filter):
  """A filter which redacts mac addresses if it sees one."""

  MAC_REPLACE_RE = re.compile(r"""
        ((?:[\dA-F]{2}:){3})       # 3-part prefix, f8:8f:ca means google
        (?:[\dA-F]{2}(:|\b)){3}    # the remaining octets
        """, re.IGNORECASE | re.VERBOSE)
  MAC_REPLACEMENT = r'\1<REDACTED>'

  def __init__(self):
    super(MacAddressLogFilter, self).__init__()

  def filter(self, record):
    if self.MAC_REPLACE_RE.search(record.getMessage()):
      # Update all the things to have no mac address in them
      record.msg = self.MAC_REPLACE_RE.sub(self.MAC_REPLACEMENT, record.msg)
      record.args = tuple([
          self.MAC_REPLACE_RE.sub(self.MAC_REPLACEMENT, str(arg))
          if isinstance(arg, basestring)
          else arg for arg in record.args])
    return True


# We use one shared instance of this, it has no internal state.
MAC_FILTER = MacAddressLogFilter()


class RecordHandler(logging.Handler):
  """A handler to save logs to an HTF TestRecord."""

  def __init__(self, test_record, notify_update):
    super(RecordHandler, self).__init__(level=logging.DEBUG)
    self._test_record = test_record
    self._notify_update = notify_update
    self.addFilter(MAC_FILTER)

  def emit(self, record):
    """Save a logging.LogRecord to our test record.

    LogRecords carry a significant amount of information with them including the
    logger name and level information.  This allows us to be a little clever
    about what we store so that filtering can occur on the client.

    Args:
      record: A logging.LogRecord to log.
    """
    message = record.getMessage()
    if record.exc_info:
      message += '\n' + ''.join(traceback.format_exception(
          *record.exc_info))
    message = message.decode('utf8', 'replace')

    log_record = LogRecord(
        record.levelno, record.name, os.path.basename(record.pathname),
        record.lineno, int(record.created * 1000), message
    )
    self._test_record.log_records.append(log_record)
    self._notify_update()


@functions.call_once
def setup_logger():
  """Configure logging for OpenHTF."""
  record_logger = logging.getLogger(RECORD_LOGGER)
  record_logger.propagate = False
  record_logger.setLevel(logging.DEBUG)
  record_logger.addHandler(logging.StreamHandler(stream=sys.stdout))

  logger = logging.getLogger(LOGGER_PREFIX)
  logger.propagate = False
  logger.setLevel(logging.DEBUG)
  formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
  if LOGFILE:
    try:
      cur_time = str(util.time_millis())
      file_handler = logging.FileHandler('%s.%s' % (LOGFILE, cur_time))
      file_handler.setFormatter(formatter)
      file_handler.setLevel(DEFAULT_LOGFILE_LEVEL.upper())
      file_handler.addFilter(MAC_FILTER)
      logger.addHandler(file_handler)
    except IOError as exception:
      print ('Failed to set up log file due to error: %s. '
             'Continuing anyway.' % exception)

  if not QUIET:
    console_handler = logging.StreamHandler(stream=sys.stderr)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(DEFAULT_LEVEL.upper())
    console_handler.addFilter(MAC_FILTER)
    logger.addHandler(console_handler)
