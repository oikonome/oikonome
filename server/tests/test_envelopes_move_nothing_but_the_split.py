"""Envelopes as their own row move nothing but the printed split
(specs/today-figures.md, "Envelopes are their own row"): for the same
household, the plan, the verdict, the variable buckets, `total_actual`,
`bills_remaining_month`, every cash tile, the cash timeline
(forecast.build) and each closed month's verdict and totals are the
figures the code before the row computed, to the cent.

How the comparison is made. The households (tests/envelope_households.py)
are built from public fixture helpers only, so the code before the row
can build the same household from the same seed:

* With OIKONOME_REFERENCE_SERVER=<the server directory of a checkout of
  that code>, every seed is built twice — here, and under the reference
  code in a subprocess (tests/reference_figures.py) on its own test
  database (`<OIKONOME_TEST_DB>_ref`) — and the two `figures` must be
  equal. OIKONOME_PROPERTY_SEEDS=<n> sets how many households.
* Without it, each seed's figures are compared with the digest recorded
  from the reference code (tests/fixtures/envelope_split_reference.json;
  re-record with OIKONOME_RECORD_REFERENCE=1 beside the reference).
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import unittest

from . import props
from .envelope_households import EnvelopeHousehold, figures
from .util import TEST_DB

HERE = pathlib.Path(__file__).resolve().parent
RECORD = HERE / "fixtures" / "envelope_split_reference.json"
REF = os.environ.get("OIKONOME_REFERENCE_SERVER", "").strip()


def _ties(v):
    """A list of event rows (each a list: date, amount, name) in one
    order: two rows on one date keep the order their database read them
    in, which differs between the two databases and says nothing."""
    if isinstance(v, dict):
        return {k: _ties(x) for k, x in v.items()}
    if isinstance(v, list):
        v = [_ties(x) for x in v]
        if v and all(isinstance(x, list) for x in v):
            return sorted(v, key=lambda x: json.dumps(x, sort_keys=True))
    return v


def _cents(v):
    """Every float to the cent: the two runs read their rows from two
    databases, whose order can differ, so a sum may differ in its last
    binary place and never in a cent. A figure on a half cent (a sum
    of cents halved) sits either side of it by that last place, so it
    is first taken to six places: one half cent, rounded one way."""
    if isinstance(v, float):
        return round(round(v, 6), 2)
    if isinstance(v, dict):
        return {k: _cents(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_cents(x) for x in v]
    return v


def digest(fig: dict) -> str:
    return hashlib.sha256(json.dumps(_cents(_ties(fig)), sort_keys=True)
                          .encode()).hexdigest()


def reference(seeds) -> subprocess.Popen:
    """The reference code's figures for `seeds`, streamed as JSON lines:
    its own interpreter path settings dropped, so it imports its own
    package."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("PYTHON")}
    env["OIKONOME_TEST_DB"] = f"{TEST_DB}_ref"
    return subprocess.Popen(
        [sys.executable, str(HERE / "reference_figures.py"), REF,
         *map(str, seeds)],
        stdout=subprocess.PIPE, env=env, text=True, cwd=REF)


def _diff(a, b, path="") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            out += _diff(a.get(k), b.get(k), f"{path}.{k}")
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += _diff(x, y, f"{path}[{i}]")
        return out
    if isinstance(a, float) and isinstance(b, (int, float)) \
            and not isinstance(b, bool) and abs(a - b) < 0.0005:
        return []
    return [] if a == b else [f"{path}: before {b!r}, now {a!r}"]


class NothingMovesButTheSplitTests(unittest.TestCase):
    maxDiff = None

    def test_against_the_reference_code(self):
        if not REF:
            self.skipTest("OIKONOME_REFERENCE_SERVER not set: the recorded "
                          "digests are checked instead")
        seeds = props.seeds(default=8)
        proc = reference(seeds)
        record, failed = {}, []
        try:
            for seed in seeds:
                h = EnvelopeHousehold(seed)
                try:
                    mine = figures(h.conn)
                finally:
                    h.close()
                line = proc.stdout.readline()
                self.assertTrue(line, "the reference run stopped")
                theirs = json.loads(line)
                self.assertEqual(theirs["seed"], seed)
                diff = _diff(_ties(mine), _ties(theirs["figures"]))
                if diff:
                    failed.append(f"seed {seed}: " + "; ".join(diff[:6]))
                    print(failed[-1], file=sys.stderr, flush=True)
                record[str(seed)] = digest(mine)
        finally:
            proc.stdout.close()
            proc.wait(timeout=600)
        self.assertEqual(failed, [], f"{len(failed)} of {len(seeds)} "
                         "households moved")
        if os.environ.get("OIKONOME_RECORD_REFERENCE") == "1":
            RECORD.write_text(json.dumps(record, indent=1, sort_keys=True)
                              + "\n")

    def test_against_the_recorded_reference(self):
        recorded = json.loads(RECORD.read_text())
        for seed in props.seeds(default=6):
            if str(seed) not in recorded:
                continue
            with self.subTest(seed=seed):
                h = EnvelopeHousehold(seed)
                try:
                    self.assertEqual(digest(figures(h.conn)),
                                     recorded[str(seed)])
                finally:
                    h.close()


if __name__ == "__main__":
    unittest.main()
