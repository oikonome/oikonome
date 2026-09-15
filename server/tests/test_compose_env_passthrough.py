"""Every env knob the app reads must actually REACH the container.

`compose.yaml` forwards an explicit allowlist, so a knob that is documented
in `.env.example` and read by the app but missing from that list does nothing
in a container: the app quietly keeps its default and the setting looks
ignored (an unforwarded `OIKONOME_SUPPORT_EMAIL` names no address).
`OIKONOME_POOL_MAX` and `OIKONOME_CONTROL_POOL_MAX` are the two the managed-PG
step-up procedure tells the operator to raise; raising an unforwarded knob is
a silent no-op.

Three invariants, all mechanical:

1. Documented + read by the app ⇒ forwarded by compose.
2. The DB-pool knobs must reach BOTH the app and the worker: they share one
   connection budget, and a pool sized in only one half silently blows it.
   (A knob is web-only only when nothing the worker imports reads it, which
   the third test derives from the worker's own import graph.)

3. Every forwarded key must tolerate an EMPTY value, because compose
   materializes every declared key: an unset knob arrives as "" and must mean
   "use the code's default", never crash (`int("")`) or read as "off".
"""

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "docker" / "compose.yaml"
ENV_EXAMPLE = REPO / "docker" / ".env.example"
PKG = REPO / "server" / "oikonome"

# Documented knobs consumed OUTSIDE the app process — by compose itself
# (ports, volumes, DSN assembly) or by oikonome.sh on the host (Caddyfile
# generation, mail relay wiring). They are not expected in the container.
HOST_SIDE = {
    "OIKONOME_PORT", "OIKONOME_HTTP_PORT", "OIKONOME_HTTPS_PORT",
    "OIKONOME_DATA_DIR", "OIKONOME_BEHIND_CLOUDFLARE",
    "OIKONOME_EMAIL_FROM",
    # NOT OIKONOME_POSTMARK_TOKEN: delivery.py reads it INSIDE the container
    # (the bounce/suppression lookups), and compose forwards it to both app
    # and worker. Listing it here would exempt the exact forward that has to
    # be there, silently disabling bounce handling with nothing failing.
}


def _documented() -> set[str]:
    text = ENV_EXAMPLE.read_text()
    return set(re.findall(r"^#?\s*(OIKONOME_[A-Z0-9_]+)=", text, re.M))


def _read_by_app() -> set[str]:
    """Names appearing in the package's Python source. Deliberately crude —
    a superset is fine: a name that isn't really read costs one forwarded
    empty string, while a missed one is a silently dead setting."""
    out = subprocess.run(
        ["grep", "-rho", "OIKONOME_[A-Z0-9_]*", "--include=*.py", str(PKG)],
        capture_output=True, text=True, check=False).stdout.split()
    return set(out)


def _forwarded() -> dict[str, int]:
    """name → how many services forward it."""
    text = COMPOSE.read_text()
    found: dict[str, int] = {}
    for name in re.findall(r"(OIKONOME_[A-Z0-9_]+):\s*\$\{", text):
        found[name] = found.get(name, 0) + 1
    return found


def _service_env(service: str) -> set[str]:
    """Env keys declared under one compose service's `environment:` block."""
    text = COMPOSE.read_text()
    lines = text.splitlines()
    out: set[str] = set()
    in_svc = in_env = False
    for ln in lines:
        if re.match(rf"^  {re.escape(service)}:\s*$", ln):
            in_svc = True
            continue
        if in_svc and re.match(r"^  \S", ln):      # next service
            break
        if in_svc and re.match(r"^    environment:\s*$", ln):
            in_env = True
            continue
        if in_env and re.match(r"^    \S", ln):    # next key at service level
            in_env = False
        if in_env:
            m = re.match(r"^\s+([A-Z0-9_]+):", ln)
            if m:
                out.add(m.group(1))
    return out


class ComposePassthroughTests(unittest.TestCase):
    def test_every_app_read_documented_knob_is_forwarded(self):
        need = (_documented() & _read_by_app()) - HOST_SIDE
        fwd = _forwarded()
        missing = sorted(n for n in need if n not in fwd)
        self.assertEqual(
            missing, [],
            "documented + read by the app but NOT forwarded by compose — "
            "setting these does nothing in a container: " + ", ".join(missing))

    def test_pool_knobs_reach_both_app_and_worker(self):
        """Both halves draw on ONE managed-PG connection budget. A pool
        sized in the app but not the worker (or vice versa) silently
        overshoots it — which is the whole reason the ceiling is where it
        is."""
        fwd = _forwarded()
        one_sided = sorted(n for n in (
            "OIKONOME_POOL_MAX", "OIKONOME_CONTROL_POOL_MAX",
            "OIKONOME_TENANT_POOL_TIMEOUT", "OIKONOME_CONTROL_POOL_TIMEOUT")
            if fwd.get(n, 0) < 2)
        self.assertEqual(one_sided, [],
                         "pool knob not forwarded to app AND worker: "
                         + ", ".join(one_sided))

    def test_every_knob_the_worker_can_reach_is_forwarded_to_the_worker(self):
        """Derived from the worker's actual import graph, not from a belief.

        A hand-written list of "web-only" knobs is a belief about what the
        worker does, and a wrong one is invisible: the worker reads the
        installed gate, the pool sizes and the mail settings too, so a knob
        forwarded to the app alone silently takes its default in the worker
        container.

        So: import the worker, see which oikonome modules that actually
        pulls in, and require every knob they read to be forwarded to the
        worker service. A new import edge updates the requirement on its
        own.
        """
        # In a SUBPROCESS on purpose: run under the full suite, sys.modules
        # already holds most of the package because other tests imported it,
        # so an in-process walk measures the SUITE's import graph rather than
        # the worker's and fails on unrelated knobs. A fresh interpreter that
        # imports only the worker is the actual question being asked.
        probe = subprocess.run(
            [sys.executable, "-c",
             "import sys, importlib;"
             "importlib.import_module('oikonome.jobs.worker');"
             "print('\\n'.join(m.__file__ for n, m in sys.modules.items()"
             " if n.startswith('oikonome.') and getattr(m, '__file__', None)))"],
            capture_output=True, text=True, cwd=str(REPO / "server"))
        self.assertEqual(probe.returncode, 0,
                         f"could not import the worker: {probe.stderr}")
        names: set[str] = set()
        for path in probe.stdout.split():
            try:
                names |= set(re.findall(r"OIKONOME_[A-Z0-9_]+",
                                        Path(path).read_text()))
            except OSError:
                continue
        worker_env = _service_env("worker")
        need = (names & _documented()) - HOST_SIDE
        missing = sorted(n for n in need if n not in worker_env)
        self.assertEqual(
            missing, [],
            "the worker imports code that reads these documented knobs, but "
            "compose never forwards them to the worker container — every "
            "branch behind them silently takes its default path: "
            + ", ".join(missing))

class EmptyMeansUnsetTests(unittest.TestCase):
    """Compose materializes every declared key, so "" must behave exactly as
    absent for each forwarded knob."""

    def setUp(self):
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def test_pool_knobs_survive_empty_values(self):
        """In a SUBPROCESS on purpose: the module-level knobs are read at
        import, and reloading tenancy in-process wipes the monkeypatched
        state other suites rely on."""
        env = dict(os.environ)
        for k in ("OIKONOME_POOL_MAX", "OIKONOME_CONTROL_POOL_MAX",
                  "OIKONOME_TENANT_POOL_TIMEOUT",
                  "OIKONOME_CONTROL_POOL_TIMEOUT"):
            env[k] = ""                        # int("")/float("") would raise
        r = subprocess.run(
            [sys.executable, "-c",
             "from oikonome.db import tenancy as t;"
             "print(t.TENANT_POOL_TIMEOUT, t.CONTROL_POOL_TIMEOUT)"],
            capture_output=True, text=True, env=env, cwd=str(REPO / "server"))
        self.assertEqual(r.returncode, 0,
                         f"import crashed on empty pool knobs: {r.stderr}")
        self.assertEqual(r.stdout.split(), ["5.0", "5.0"])

    def test_empty_spam_flag_does_not_read_as_off(self):
        """The dangerous one: hosted defaults spam defense ON, and an empty
        forwarded value must not silently disable it."""
        from oikonome.web import spamdefense
        os.environ["OIKONOME_HOSTED"] = "1"
        os.environ["OIKONOME_SPAM_DEFENSE"] = ""
        self.assertTrue(spamdefense.enforced())
        os.environ["OIKONOME_SPAM_DEFENSE"] = "0"      # explicit off still off
        self.assertFalse(spamdefense.enforced())

    def test_empty_numeric_spam_knobs_fall_back(self):
        from oikonome.web import spamdefense
        os.environ["OIKONOME_SPAM_API_BUDGET_MS"] = ""
        os.environ["OIKONOME_SPAM_CACHE_TTL_DAYS"] = ""
        self.assertEqual(spamdefense._budget_ms(), 400)
        self.assertEqual(spamdefense._cache_ttl_days(), 7)

    def test_empty_spam_api_keeps_the_hosted_default(self):
        from oikonome.web import spamdefense
        os.environ.pop("OIKONOME_DEV", None)
        os.environ["OIKONOME_SPAM_API"] = ""
        self.assertTrue(spamdefense._live_enabled())
        os.environ["OIKONOME_SPAM_API"] = "off"
        self.assertFalse(spamdefense._live_enabled())


if __name__ == "__main__":
    unittest.main()
