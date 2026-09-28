"""install.sh prints the one-time claim link only to a person at a
terminal.

The console's Reset re-runs the installer through the host agent, which
captures stdout to a log the console then shows. Printing the fresh setup
token there unconditionally would hand the account-claim URL to anyone who
can read that log.
"""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _success_box(env: dict) -> str:
    src = (ROOT / "install.sh").read_text()
    start = src.index("SETUP_TOKEN=$(grep")
    end = src.index('B "1. The setup wizard', start)
    block = src[start:end]
    with tempfile.TemporaryDirectory() as d:
        # a fake curl answering as the real server does: GET / redirects an
        # unclaimed instance to /setup; a tokenless GET /setup is a 403
        Path(d, "curl").write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *'/setup'*) printf 403 ;;\n"
            "  *redirect_url*) printf 'http://localhost:8042/setup' ;;\n"
            "  *) printf 303 ;;\n"
            "esac\n")
        os.chmod(Path(d, "curl"), 0o755)
        Path(d, ".env").write_text("OIKONOME_SETUP_TOKEN=tok-SECRET-123\n")
        script = f"cd {d}\nPORT=8042\n" + block
        r = subprocess.run(["bash", "-c", script], capture_output=True,
                           text=True,
                           env={**os.environ, **env,
                                "PATH": f"{d}:{os.environ.get('PATH', '')}"})
        return r.stdout + r.stderr


class ClaimLinkTests(unittest.TestCase):
    def test_non_interactive_output_never_carries_the_token(self):
        out = _success_box({"OIKONOME_ASSUME_YES": "1"})
        self.assertNotIn("tok-SECRET-123", out)
        self.assertIn("oikonome.sh status", out)

    def test_a_captured_stdout_is_treated_as_non_interactive(self):
        # no OIKONOME_ASSUME_YES, but stdout is a pipe (not a tty)
        out = _success_box({})
        self.assertNotIn("tok-SECRET-123", out)

    def test_the_probe_never_puts_the_token_in_a_url(self):
        for name in ("install.sh", "oikonome.sh"):
            src = (ROOT / name).read_text()
            self.assertFalse(re.search(r"curl[^\n]*setup\?token=", src),
                             f"{name}: the readiness probe must not carry "
                             "the token")

    def test_an_unclaimed_instance_is_detected_through_the_redirect(self):
        # the box must reach the setup branch at all — a tokenless GET
        # /setup is a 403 by design, so probing it never detected anything
        out = _success_box({"OIKONOME_ASSUME_YES": "1"})
        self.assertIn("No account yet", out)
        self.assertNotIn("Open:   http://localhost", out)

    def test_status_withholds_the_link_off_a_terminal_too(self):
        src = (ROOT / "oikonome.sh").read_text()
        i = src.index("tok=$(env_get OIKONOME_SETUP_TOKEN)")
        self.assertIn('[ ! -t 1 ]', src[i:i + 900])


if __name__ == "__main__":
    unittest.main()
