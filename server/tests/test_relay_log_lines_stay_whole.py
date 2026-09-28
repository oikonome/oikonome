"""A refused request cannot write a second line into the relay's log.

The relay runs as a process of its own, so the one-line record factory the
instance and the worker install at startup does not reach it. That gap is
worse here than anywhere: the edge gate refuses some requests before any
credential is looked at and logs the request path when it does, and the
ASGI server hands the app a percent-DECODED path — so a stranger asking
for `/v1/%0A...` would be choosing the text of the log line that follows
the refusal, with nothing but a socket.
"""

import importlib
import logging
import unittest

from fastapi.testclient import TestClient

from oikonome.relay import app as relay

# What a caller can actually put on the wire: the request target is
# percent-encoded, and the ASGI server decodes it back into scope["path"].
FORGED = "/v1/%0AWARNING%20relay:%20push%20key=someone-else%20sent=1"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


class RelayLogLinesStayWhole(unittest.TestCase):
    def setUp(self):
        self.h = _Capture()
        self.h.setFormatter(logging.Formatter("%(message)s"))
        self.log = logging.getLogger("oikonome.relay")
        self.log.addHandler(self.h)

    def tearDown(self):
        self.log.removeHandler(self.h)

    def test_a_forged_path_cannot_start_a_new_log_line(self):
        """The oversize refusal takes no key, so this is what an
        unauthenticated caller can write."""
        client = TestClient(relay.app)
        r = client.post(FORGED, content=b"x" * (relay.MAX_BODY_BYTES + 1))
        self.assertEqual(r.status_code, 413, r.text)
        self.assertTrue(self.h.lines, "the refusal logged nothing")
        for line in self.h.lines:
            self.assertNotIn("\n", line)
            self.assertNotIn("\r", line)
        self.assertIn("\\n", self.h.lines[0])

    def test_the_relay_process_installs_the_flattening_record_factory(self):
        """uvicorn imports this module and serves it — there is no
        entrypoint function to install from, so the import itself has to."""
        before = logging.getLogRecordFactory()
        logging.setLogRecordFactory(logging.LogRecord)
        try:
            importlib.reload(relay)
            self.assertTrue(getattr(logging.getLogRecordFactory(),
                                    "_oikonome_flat", False))
        finally:
            logging.setLogRecordFactory(before)


if __name__ == "__main__":
    unittest.main()
