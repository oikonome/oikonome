"""`./oikonome.sh https off` must undo what `https <domain>` pinned.

Turning HTTPS on writes three things into docker/.env: the domain, a base
URL of https://<domain>, and (when the operator had none) a trusted-proxy
range covering the container network so the app believes Caddy's
X-Forwarded-* headers. Turning it off used to clear only the domain. The
app then kept building every emailed link (password reset, invites, the
daily email's buttons) against an https host nothing serves any more, and
kept believing forwarded headers from the container network with no proxy
in front — where a client connecting straight to the app port can claim any
address or scheme it likes.

The real function is lifted out of the script and run against a scratch
.env with the engine calls stubbed.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run_https_off(env_text: str) -> dict[str, str]:
    """Run the real script from a scratch install folder whose prereq check
    is stubbed to a no-op engine, so nothing touches a container."""
    with tempfile.TemporaryDirectory() as d:
        inst = Path(d)
        (inst / "docker").mkdir()
        (inst / "scripts").mkdir()
        shutil.copy(ROOT / "oikonome.sh", inst / "oikonome.sh")
        shutil.copy(ROOT / "scripts" / "version-stamp.sh",
                    inst / "scripts" / "version-stamp.sh")
        (inst / "scripts" / "check-prereqs.sh").write_text(
            "require_prereqs() { COMPOSE=true; ENGINE=true; }\n")
        envf = inst / "docker" / ".env"
        envf.write_text(env_text)
        r = subprocess.run(["bash", str(inst / "oikonome.sh"), "https", "off"],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stdout + r.stderr
        out = {}
        for line in envf.read_text().splitlines():
            k, _, v = line.partition("=")
            out[k] = v
        return out


class HttpsOffTests(unittest.TestCase):
    def test_off_drops_the_base_url_and_proxy_trust_https_pinned(self):
        env = _run_https_off(
            "COMPOSE_PROFILES=tls\n"
            "OIKONOME_DOMAIN=fin.example.test\n"
            "OIKONOME_BASE_URL=https://fin.example.test\n"
            "OIKONOME_TRUSTED_PROXIES=10.88.0.0/15,172.16.0.0/12\n")
        self.assertEqual(env.get("OIKONOME_DOMAIN", ""), "")
        self.assertEqual(env.get("OIKONOME_BASE_URL", ""), "",
                         "base URL still points at the https domain")
        self.assertEqual(env.get("OIKONOME_TRUSTED_PROXIES", ""), "",
                         "container network still trusted with no proxy")

    def test_off_keeps_values_the_operator_set_themselves(self):
        env = _run_https_off(
            "COMPOSE_PROFILES=tls,llm\n"
            "OIKONOME_DOMAIN=fin.example.test\n"
            "OIKONOME_BASE_URL=https://money.example.test\n"
            "OIKONOME_TRUSTED_PROXIES=192.0.2.10/32\n")
        self.assertEqual(env["OIKONOME_BASE_URL"], "https://money.example.test")
        self.assertEqual(env["OIKONOME_TRUSTED_PROXIES"], "192.0.2.10/32")
        self.assertEqual(env["COMPOSE_PROFILES"], "llm")


if __name__ == "__main__":
    unittest.main()
