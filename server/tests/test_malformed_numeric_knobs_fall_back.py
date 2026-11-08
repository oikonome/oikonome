"""A malformed numeric knob must never stop a process from starting.

A one-character typo in the env file (`12e6` for a pixel count, `2.5` for
a concurrency, `60s` for a TTL, a negative semaphore size) used to raise
at import time, so the web app or the worker crash-looped. Every numeric
knob read when a module loads goes through envnum.env_int / env_float,
which log the knob by name and fall back to its documented default.
"""

import json
import os
import pathlib
import re
import subprocess
import sys
import unittest

SERVER = pathlib.Path(__file__).resolve().parents[1]
PKG = SERVER / "oikonome"

# knob -> (module, attribute, documented default)
KNOBS = {
    "OIKONOME_RECEIPT_MAX_OPTIMIZE_PX": ("oikonome.engine.receipts",
                                         "MAX_OPTIMIZE_PX", 12000000),
    "OIKONOME_RECEIPT_OPTIMIZE_WORK_PX": ("oikonome.engine.receipts",
                                          "_OPTIMIZE_WORK_PX", 2400),
    "OIKONOME_RECEIPT_OPTIMIZE_CONCURRENCY": ("oikonome.engine.receipts",
                                              None, None),
    "OIKONOME_RECEIPT_PARSE_CONCURRENCY": ("oikonome.engine.receipts",
                                           None, None),
    "OIKONOME_RECEIPT_PARSE_WAIT_S": ("oikonome.engine.receipts",
                                      "_DECODE_WAIT_S", 120.0),
    "OIKONOME_ASSISTANT_CONCURRENCY": ("oikonome.engine.assistant",
                                       None, None),
    "OIKONOME_PLAID_WEBHOOK_NEG_TTL": ("oikonome.web.plaid_webhook",
                                       "_NEG_TTL", 60.0),
    "OIKONOME_PLAID_WEBHOOK_FETCH_CEILING": ("oikonome.web.plaid_webhook",
                                             "_FETCH_CEILING", 4),
    "OIKONOME_SYNC_BUCKETS": ("oikonome.jobs.worker", "SYNC_BUCKETS", 12),
    "OIKONOME_TENANT_POOL_TIMEOUT": ("oikonome.db.tenancy",
                                     "TENANT_POOL_TIMEOUT", 5.0),
    "OIKONOME_CONTROL_POOL_TIMEOUT": ("oikonome.db.tenancy",
                                      "CONTROL_POOL_TIMEOUT", 5.0),
    "OIKONOME_MIGRATE_LOCK_TIMEOUT_MS": ("oikonome.db.migrate",
                                         "SCHEMA_LOCK_TIMEOUT_MS", 2000),
    "OIKONOME_MIGRATE_LOCK_ATTEMPTS": ("oikonome.db.migrate",
                                       "SCHEMA_LOCK_ATTEMPTS", 30),
}

_PROBE = r"""
import importlib, json, sys
spec = json.loads(sys.argv[1])
for mod in ("oikonome.web.app", "oikonome.jobs.worker", "oikonome.db.migrate"):
    importlib.import_module(mod)
out = {}
for knob, (mod, attr, _want) in spec.items():
    if attr:
        out[knob] = getattr(importlib.import_module(mod), attr)
print(json.dumps(out))
"""


def _import_with(value: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for k in KNOBS:
        env[k] = value
    return subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps(KNOBS)], cwd=SERVER,
        env=env, capture_output=True, text=True, timeout=180)


class MalformedNumericKnobTests(unittest.TestCase):

    def _assert_defaults(self, value: str):
        r = _import_with(value)
        self.assertEqual(r.returncode, 0,
                         f"import failed with every knob = {value!r}:\n"
                         f"{r.stderr[-2000:]}")
        got = json.loads(r.stdout.strip().splitlines()[-1])
        for knob, (_mod, attr, want) in KNOBS.items():
            if attr:
                self.assertEqual(got[knob], want, knob)

    def test_junk_values_start_with_the_defaults(self):
        self._assert_defaults("junk")

    def test_a_unit_suffix_starts_with_the_defaults(self):
        self._assert_defaults("60s")

    def test_negative_values_start(self):
        r = _import_with("-1")
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])

    def test_fractional_counts_start(self):
        r = _import_with("2.5")
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])

    def test_no_module_parses_a_knob_bare_at_import(self):
        # the class, not the instances: a new module-level knob written as
        # int(os.environ...) / float(env_num(...)) brings the crash back
        bare = re.compile(r"^[A-Za-z_][\w]*\s*=.*\b(int|float)\(\s*"
                          r"(env_num|os\.environ|os\.getenv)")
        hits = []
        for p in PKG.rglob("*.py"):
            text = p.read_text()
            for n, line in enumerate(text.splitlines(), 1):
                if bare.search(line):
                    hits.append(f"{p.relative_to(SERVER)}:{n}: {line.strip()}")
            # a module-level semaphore sized straight from the environment
            for m in re.finditer(r"^\w+\s*=\s*(threading|asyncio)\."
                                 r"(Bounded)?Semaphore\(\s*\n?\s*int\(",
                                 text, re.M):
                hits.append(f"{p.relative_to(SERVER)}: {m.group(0).strip()}")
        self.assertEqual(hits, [])


class EnvIntFloatTests(unittest.TestCase):

    def setUp(self):
        self.addCleanup(os.environ.pop, "OIKONOME_TMP_KNOB", None)

    def test_int_parses_and_bounds(self):
        from oikonome.envnum import env_int
        cases = {"": 3, "  ": 3, "7": 7, "6e1": 60, "junk": 3,
                 "2.5": 3, "nan": 3, "inf": 3, "-4": 1, "999": 64}
        for raw, want in cases.items():
            os.environ["OIKONOME_TMP_KNOB"] = raw
            self.assertEqual(env_int("OIKONOME_TMP_KNOB", 3, lo=1, hi=64),
                             want, raw)

    def test_float_parses_and_bounds(self):
        from oikonome.envnum import env_float
        cases = {"": 5.0, "1.5": 1.5, "60s": 5.0, "nan": 5.0, "-1": 0.0}
        for raw, want in cases.items():
            os.environ["OIKONOME_TMP_KNOB"] = raw
            self.assertEqual(env_float("OIKONOME_TMP_KNOB", 5.0, lo=0.0),
                             want, raw)

    def test_a_bad_value_is_logged_by_name(self):
        from oikonome.envnum import env_int
        os.environ["OIKONOME_TMP_KNOB"] = "junk"
        with self.assertLogs("oikonome.envnum", "WARNING") as cm:
            env_int("OIKONOME_TMP_KNOB", 3)
        self.assertIn("OIKONOME_TMP_KNOB", cm.output[0])


if __name__ == "__main__":
    unittest.main()
