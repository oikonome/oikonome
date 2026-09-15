"""The maintenance lock serialises backup against restore/reset/uninstall
without the backup deadlocking on itself.

flock is per open-file-description: `./oikonome.sh backup` taking the lock
and then running scripts/backup.sh — which re-opens the same file and takes
it again — makes every on-demand backup wait the whole timeout and "skip".
So only backup.sh takes it, and a held lock makes backup.sh refuse fast.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

APP = pathlib.Path(__file__).resolve().parents[2]


class BackupLockTests(unittest.TestCase):
    def test_cmd_backup_does_not_take_the_lock_backup_sh_takes(self):
        src = (APP / "oikonome.sh").read_text()
        body = src[src.index("cmd_backup() {"):src.index("}", src.index("cmd_backup() {"))]
        self.assertNotIn("maint_lock", body.replace("# NO maint_lock", ""))
        for fn in ("cmd_restore", "cmd_reset", "cmd_uninstall"):
            i = src.index(fn + "() {")
            self.assertIn("maint_lock", src[i:i + 600], fn)
        self.assertIn("flock -w", (APP / "scripts/backup.sh").read_text())

    def test_a_held_lock_makes_backup_sh_refuse_instead_of_racing(self):
        with tempfile.TemporaryDirectory() as td:
            install = pathlib.Path(td) / "inst"; install.mkdir()
            (install / "docker").mkdir()
            lock = install / ".maintenance.lock"
            holder = subprocess.Popen(
                ["bash", "-c", f"exec 9>'{lock}'; flock 9; sleep 30"])
            try:
                # wait until the holder really holds it
                for _ in range(50):
                    probe = subprocess.run(
                        ["bash", "-c", f"exec 9>'{lock}'; flock -n 9"],
                        capture_output=True)
                    if probe.returncode != 0:
                        break
                    import time; time.sleep(0.1)
                r = subprocess.run(
                    ["bash", str(APP / "scripts/backup.sh"), str(install),
                     str(pathlib.Path(td) / "dest")],
                    capture_output=True, text=True,
                    env={**os.environ, "OIKONOME_MAINT_LOCK_WAIT": "1"},
                    timeout=30)
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn("backup skipped", r.stderr)
            finally:
                holder.kill()
