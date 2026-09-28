"""A log record is one line: text chosen by someone else cannot end it
early and forge the next."""
import logging
import unittest

from oikonome import logsafe


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


class LogLinesStayWhole(unittest.TestCase):
    def setUp(self):
        logsafe.install()
        self.h = _Capture()
        self.h.setFormatter(logging.Formatter("%(message)s"))
        self.log = logging.getLogger("oikonome.test.logsafe")
        self.log.addHandler(self.h)
        self.log.setLevel(logging.INFO)

    def tearDown(self):
        self.log.removeHandler(self.h)

    def test_breaks_in_arguments_are_escaped(self):
        self.log.info("delivery: soft failure for %s", "a@b\nINFO forged")
        self.assertEqual(self.h.lines, ["delivery: soft failure for a@b\\nINFO forged"])

    def test_breaks_in_the_message_itself_are_escaped(self):
        self.log.info("line one\r\nline two")
        self.assertEqual(self.h.lines, ["line one\\r\\nline two"])

    def test_non_string_arguments_pass_through(self):
        self.log.info("%d rows for %s", 3, {"k": "v\n"})
        self.assertEqual(self.h.lines, ["3 rows for {'k': 'v\\n'}"])

    def test_install_is_idempotent(self):
        before = logging.getLogRecordFactory()
        logsafe.install()
        self.assertIs(logging.getLogRecordFactory(), before)

    def test_an_exception_argument_is_flattened_for_s_and_r(self):
        e = ValueError("first line\nINFO forged second line")
        self.log.info("failed: %s", e)
        self.log.info("failed: %r", e)
        self.assertEqual(self.h.lines[0], "failed: first line\\nINFO forged second line")
        self.assertEqual(self.h.lines[1],
                         "failed: ValueError('first line\\nINFO forged second line')")

