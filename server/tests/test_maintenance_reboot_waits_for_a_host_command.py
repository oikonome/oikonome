"""The nightly maintenance reboot never lands under a console command the
host is still running.

A reboot in the middle of an upgrade kills it mid-build or mid-migrate:
the schema is left part-upgraded under the old containers and the command
reads "running" forever. The worker can see the command watcher's result
files and its drop-box, so it defers while either says a command is queued
or in flight — but a result left "running" by a crash longer ago than the
watcher's own lock ceiling does not hold the reboot off forever.
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from oikonome.db import tenancy
from oikonome.jobs import worker

from .util import TEST_DB, _admin_dsn, _ensure_db


class MaintenanceRebootWaitsForAHostCommandTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def tearDown(self):
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            admin.execute("DELETE FROM broadcast WHERE id=1")
            admin.commit()
        finally:
            admin.close()

    def _run(self, td, *, result=None, queued=False):
        root = Path(td)
        (root / "ops" / "watchers").mkdir(parents=True)
        for kind in ("command", "reboot"):
            (root / "ops" / "watchers" / kind).write_text("")
        (root / "ops" / "cmd-results").mkdir()
        (root / "cmd").mkdir()
        (root / "ops" / "host-health.json").write_text(
            json.dumps({"reboot_required": True}))
        if result is not None:
            (root / "ops" / "cmd-results" / "cmd-1.json").write_text(
                json.dumps(result))
        if queued:
            (root / "cmd" / "command-requested").write_text("x")
        with mock.patch.dict(os.environ, {"OIKONOME_STATE_DIR": td,
                                          "OIKONOME_AUTO_REBOOT": "1"}):
            out = worker._nightly_reboot_check()
        return out, (root / "cmd" / "reboot-requested").exists()

    def test_a_running_upgrade_defers_the_reboot(self):
        with tempfile.TemporaryDirectory() as td:
            out, flagged = self._run(td, result={
                "id": "cmd-1", "cmd": "upgrade", "state": "running",
                "started": int(time.time()) - 120})
        self.assertEqual(out, "host-command-running")
        self.assertFalse(flagged)

    def test_a_queued_command_defers_the_reboot(self):
        with tempfile.TemporaryDirectory() as td:
            out, flagged = self._run(td, queued=True)
        self.assertEqual(out, "host-command-running")
        self.assertFalse(flagged)

    def test_a_finished_command_does_not_defer_the_reboot(self):
        with tempfile.TemporaryDirectory() as td:
            out, flagged = self._run(td, result={
                "id": "cmd-1", "cmd": "upgrade", "state": "done", "rc": 0,
                "started": int(time.time()) - 120,
                "finished": int(time.time()) - 60})
        self.assertEqual(out, "reboot-requested")
        self.assertTrue(flagged)

    def test_a_running_result_older_than_the_lock_ceiling_is_ignored(self):
        """A command killed by a crash leaves its result "running" for good;
        that must not defer every maintenance reboot after it."""
        with tempfile.TemporaryDirectory() as td:
            out, flagged = self._run(td, result={
                "id": "cmd-1", "cmd": "upgrade", "state": "running",
                "started": int(time.time()) - 7 * 3600})
        self.assertEqual(out, "reboot-requested")
        self.assertTrue(flagged)


if __name__ == "__main__":
    unittest.main()
