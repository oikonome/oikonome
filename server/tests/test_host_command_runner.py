"""Admin-console host-agent command runner (oikonome.sh parity).

The console validates + writes an allowlisted request flag to /state/cmd; a
root watcher (scripts/install-host-command-watcher.sh, not exercised here)
runs the real command. These tests cover the CONSOLE half: allowlist,
destructive arm+nonce+typed-confirm, and that the flag it writes is correct.
"""

import json
import os
import re
import tempfile
import unittest

from fastapi.testclient import TestClient

from .util import _ensure_db

TOKEN = "test-admin-token-" + "x" * 32


class RunCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "cmd"))
        os.makedirs(os.path.join(self.tmp, "ops", "cmd-results"))
        os.environ["OIKONOME_STATE_DIR"] = self.tmp
        os.environ["OIKONOME_ADMIN_TOKEN"] = TOKEN
        self.client = TestClient(self.appmod.app)
        r = self.client.post("/admin/console/login", data={"token": TOKEN},
                             follow_redirects=False)
        assert r.status_code in (200, 303), r.text

    def tearDown(self):
        os.environ.pop("OIKONOME_ADMIN_TOKEN", None)
        os.environ.pop("OIKONOME_STATE_DIR", None)

    def _flag(self):
        p = os.path.join(self.tmp, "cmd", "command-requested")
        if not os.path.exists(p):
            return None
        with open(p) as fh:
            return json.load(fh)

    def _run(self, **data):
        return self.client.post("/admin/console/run-command", data=data,
                                follow_redirects=False)

    def test_unknown_command_rejected(self):
        r = self._run(command="rm -rf /")
        self.assertEqual(r.status_code, 200)          # dashboard notice
        self.assertIsNone(self._flag())               # nothing written

    def test_nondestructive_writes_flag(self):
        r = self._run(command="backup")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self._flag()["cmd"], "backup")

    def test_destructive_arms_and_needs_nonce_plus_confirm(self):
        # 1) arm — no flag written yet
        r = self._run(command="reset")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Confirm: reset", r.text)
        self.assertIsNone(self._flag())
        nonce = re.search(r'name="nonce" value="([^"]+)"', r.text).group(1)
        # 2) go with a valid nonce but the WRONG confirm word → not run
        self._run(command="reset", stage="go", nonce="bogus",
                       confirm="reset")
        self.assertIsNone(self._flag())
        # 3) go with the real nonce + correct confirm → flag written
        r3 = self._run(command="reset", stage="go", nonce=nonce,
                       confirm="reset")
        self.assertEqual(r3.status_code, 303)
        self.assertEqual(self._flag()["cmd"], "reset")

    def test_reset_nonce_is_single_use(self):
        nonce = re.search(r'name="nonce" value="([^"]+)"',
                          self._run(command="uninstall").text).group(1)
        self.assertEqual(self._run(command="uninstall", stage="go",
                                   nonce=nonce, confirm="uninstall"
                                   ).status_code, 303)
        os.remove(os.path.join(self.tmp, "cmd", "command-requested"))
        # replay the same nonce → refused, no new flag
        self._run(command="uninstall", stage="go", nonce=nonce,
                  confirm="uninstall")
        self.assertIsNone(self._flag())


if __name__ == "__main__":
    unittest.main()
