"""`KEY=0` in docker/.env must read as OFF in oikonome.sh, as it does in the
server.

The https-setup path decides from `OIKONOME_BEHIND_CLOUDFLARE` whether the
Caddyfile trusts Cloudflare's edge ranges and takes the visitor IP from
`Cf-Connecting-Ip`. A bare "is the value non-empty" test reads `=0` as ON,
so an origin that is NOT behind Cloudflare would trust a forged
`Cf-Connecting-Ip` from anyone inside those ranges — the opposite of what
the operator wrote. `env_flag` in the script is the shell twin of
`envnum.env_flag`; this exercises the real function, lifted out of the
script, against a scratch .env.
"""

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "oikonome.sh"


def _shell_env_flag(value: str | None, key: str = "OIKONOME_BEHIND_CLOUDFLARE") -> bool:
    src = SCRIPT.read_text()
    funcs = "".join(
        m.group(0)
        for m in re.finditer(r"^(env_get|env_flag)\(\) \{\n.*?^\}\n",
                             src, re.M | re.S))
    assert "env_flag()" in funcs, "oikonome.sh lost its env_flag helper"
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "docker").mkdir()
        if value is not None:
            (Path(d) / "docker" / ".env").write_text(f"{key}={value}\n")
        r = subprocess.run(
            ["bash", "-c", f'ROOT="$1"; {funcs}\nenv_flag {key}', "_", d],
            capture_output=True, text=True)
        assert r.returncode in (0, 1), r.stderr
        return r.returncode == 0


class InstallerEnvFlagTests(unittest.TestCase):
    def test_zero_and_friends_read_as_off(self):
        for v in ("0", "false", "False", "off", "no", "", "  "):
            self.assertFalse(_shell_env_flag(v), f"{v!r} read as ON")
        self.assertFalse(_shell_env_flag(None), "unset read as ON")

    def test_on_spellings_read_as_on(self):
        for v in ("1", "true", "TRUE", "yes", "on"):
            self.assertTrue(_shell_env_flag(v), f"{v!r} read as OFF")

    def test_cloudflare_guard_uses_the_boolean_helper(self):
        src = SCRIPT.read_text()
        self.assertIn("if env_flag OIKONOME_BEHIND_CLOUDFLARE; then", src)
        self.assertNotIn('-n "$(env_get OIKONOME_BEHIND_CLOUDFLARE)"', src)


if __name__ == "__main__":
    unittest.main()
