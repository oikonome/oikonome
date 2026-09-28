"""The admin console's "Recent logs" panel never shows a one-time link.

The `logs` host command captures the app's stdout, which carries the
access log — and with it every reset, invite and export-ticket link a
request line ever held. The panel is read by any operator session, so
the text is scrubbed the way the feedback bundle is.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from oikonome.web import adminconsole, feedback


class RedactionTests(unittest.TestCase):
    def test_query_string_tickets_are_scrubbed(self):
        for line in ("GET /export/download?t=abc123DEF HTTP/1.1",
                     "GET /admin/enrol?ticket=zzz&x=1",
                     "GET /verify?code=99887766 HTTP/1.1",
                     "GET /reset?token=deadbeef HTTP/1.1",
                     "GET /invite/abcdefghij HTTP/1.1"):
            out = feedback._redact(line)
            self.assertIn("<redacted>", out, line)
            for secret in ("abc123DEF", "zzz", "99887766", "deadbeef",
                           "abcdefghij"):
                self.assertNotIn(secret, out, line)

    def test_a_tail_cut_inside_a_link_still_redacts(self):
        # the panel shows the last 6000 characters; taking the tail before
        # redacting could start the text in the middle of a token, with no
        # prefix left for the patterns to recognise
        secret = "SUPERSECRETVALUE123456"
        line = f"app | GET /reset?token={secret} HTTP/1.1 200\n"
        # size the tail so the last-6000 cut lands right after "token="
        want_cut = line.index("token=") + len("token=")
        body = line + "y" * (6000 + want_cut - len(line))
        self.assertEqual(len(body) - 6000, want_cut)
        with tempfile.TemporaryDirectory() as d:
            res = Path(d) / "ops" / "cmd-results"
            res.mkdir(parents=True)
            (res / "2.json").write_text(json.dumps(
                {"id": "2", "cmd": "upgrade", "state": "done", "rc": 0}))
            (res / "2.log").write_text(body)
            with mock.patch.dict(os.environ, {"OIKONOME_STATE_DIR": d}):
                out = adminconsole._command_results()
        self.assertNotIn(secret, out[0]["log"])
        self.assertNotIn(secret[6:], out[0]["log"])

    def test_command_results_panel_is_redacted(self):
        with tempfile.TemporaryDirectory() as d:
            res = Path(d) / "ops" / "cmd-results"
            res.mkdir(parents=True)
            (res / "1.json").write_text(json.dumps(
                {"id": "1", "cmd": "logs", "state": "done", "rc": 0}))
            (res / "1.log").write_text(
                "app | GET /reset?token=SECRETTOKEN HTTP/1.1 200\n"
                "app | GET /export/download?t=SECRETTICKET 200\n")
            with mock.patch.dict(os.environ, {"OIKONOME_STATE_DIR": d}):
                out = adminconsole._command_results()
        self.assertEqual(len(out), 1)
        self.assertNotIn("SECRETTOKEN", out[0]["log"])
        self.assertNotIn("SECRETTICKET", out[0]["log"])
        self.assertIn("/reset?token=<redacted>", out[0]["log"])


if __name__ == "__main__":
    unittest.main()
