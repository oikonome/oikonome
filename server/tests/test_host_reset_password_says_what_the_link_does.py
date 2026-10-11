"""The host-side reset-password link must describe the reset it performs.

The printed link is the same reset the emailed one performs: on an account
with an authenticator or a passkey it asks for a recovery code first, and
using it removes every second factor. An operator told otherwise hands the
link over as "your authenticator still works", and the account holder meets
a recovery-code wall they were told was not there.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANAGER = ROOT / "oikonome.sh"


class HostResetPasswordMessageTests(unittest.TestCase):

    def _run(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        inst = root / "install"
        inst.mkdir()
        shutil.copy(MANAGER, inst / "oikonome.sh")
        shutil.copytree(ROOT / "scripts", inst / "scripts")
        (inst / "docker").mkdir()
        (inst / "docker" / ".env").write_text(
            "COMPOSE_PROJECT_NAME=acme\nOIKONOME_PORT=8999\n")
        binp = root / "bin"
        binp.mkdir()
        (binp / "docker").write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi\n'
            'if [ "$1" = "ps" ]; then echo app_1; exit 0; fi\n'
            'if [ "$1" = "exec" ]; then echo "/reset?token=abc"; exit 0; fi\n'
            "exit 0\n")
        (binp / "docker").chmod(0o755)
        env = dict(os.environ, PATH=f"{binp}:{os.environ['PATH']}")
        r = subprocess.run(
            ["bash", str(inst / "oikonome.sh"), "reset-password",
             "someone@example.dev"], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        return r.stdout

    def test_it_does_not_promise_two_factor_survives(self):
        out = self._run()
        self.assertIn("/reset?token=abc", out)
        self.assertNotIn("unchanged", out,
                         "the script says two-factor is untouched, but the "
                         "reset removes every factor")

    def test_it_says_a_recovery_code_is_asked_for_and_what_using_it_does(self):
        out = self._run()
        self.assertIn("recovery code", out)
        self.assertIn("removes", out)
        self.assertIn("clear-2fa someone@example.dev", out)


if __name__ == "__main__":
    unittest.main()
