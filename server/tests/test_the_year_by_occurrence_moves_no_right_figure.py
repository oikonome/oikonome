"""Reading the Year's bills by occurrence moves no figure that was right
(specs/today-figures.md, "The Year is its months, occurrence by
occurrence").

The Year once matched a late bill to the closed month it was due in by
bill and nearest date; it now folds the months' occurrence rows by key.
For the same household every figure the Year page prints
(year_households.figures) must be what the matching code printed —
ordinary generated households, the straddling ones and each named
shape. A figure may move only where tests/test_the_year_is_its_months_
occurrence_by_occurrence names the invariant the old figure broke; such
a move is listed in EXPLAINED with that invariant.

How the comparison is made (test_envelopes_move_nothing_but_the_split's
way):

* With OIKONOME_REFERENCE_SERVER=<the server directory of a checkout of
  the matching code>, every household is built twice — here, and under
  the reference code in a subprocess (tests/reference_year_figures.py)
  on its own test database (`<OIKONOME_TEST_DB>_ref`) — and the figures
  must be equal. OIKONOME_PROPERTY_SEEDS=<n> sets how many households.
* Without it, each household's figures are compared with the digest
  recorded from the reference code
  (tests/fixtures/year_by_occurrence_reference.json; re-record with
  OIKONOME_RECORD_REFERENCE=1 beside the reference).
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import unittest

from . import props, year_households as yh
from .test_envelopes_move_nothing_but_the_split import _diff, _ties, digest
from .util import TEST_DB

HERE = pathlib.Path(__file__).resolve().parent
RECORD = HERE / "fixtures" / "year_by_occurrence_reference.json"
REF = os.environ.get("OIKONOME_REFERENCE_SERVER", "").strip()

# (kind, seed) -> the invariant the reference's figure broke.
EXPLAINED: dict[tuple[str, int], str] = {
    ("same_date_legacy_archived", 0):
        "3: a late bill took another bill's not-paid row on its date, so "
        "the Year dropped a not paid its month printed",
    ("paid_then_merchant_changed", 0):
        "4: the Year carried late, outside the plan, an occurrence June "
        "counted paid",
    ("one_to_two_dates", 0):
        "3: the nearest date paired the 27th with June's 29th, so the "
        "date June planned was said outside the plan",
}

_MIDMONTH = ("late_on_the_2nd", "early_in_the_month_before",
             "two_dates_one_late", "missed_in_a_closed_month",
             "partial_withdrawal", "still_late")


def _families():
    yield "ordinary", props.seeds(default=6)
    yield "straddling", props.seeds(default=12)
    for name in yh.SHAPES:
        # each read on its own day, and the first six early in the month
        yield name, (0,) + (yh.EARLY_DAYS if name in _MIDMONTH else ())


def _reference(kind, seeds) -> subprocess.Popen:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("PYTHON")}
    env["OIKONOME_TEST_DB"] = f"{TEST_DB}_ref"
    return subprocess.Popen(
        [sys.executable, str(HERE / "reference_year_figures.py"), REF, kind,
         *map(str, seeds)],
        stdout=subprocess.PIPE, env=env, text=True, cwd=REF)


def _mine(kind, seed) -> dict:
    conn, today, close = yh.build(kind, seed)
    try:
        return yh.figures(conn, today)
    finally:
        close()


class NoRightFigureMovesTests(unittest.TestCase):
    maxDiff = None

    def test_against_the_reference_code(self):
        if not REF:
            self.skipTest("OIKONOME_REFERENCE_SERVER not set: the recorded "
                          "digests are checked instead")
        record, failed, moved = {}, [], []
        for kind, seeds in _families():
            proc = _reference(kind, seeds)
            try:
                for seed in seeds:
                    mine = _mine(kind, seed)
                    line = proc.stdout.readline()
                    self.assertTrue(line, "the reference run stopped")
                    theirs = json.loads(line)
                    self.assertEqual(theirs["seed"], seed)
                    diff = _diff(_ties(mine), _ties(theirs["figures"]))
                    if diff and (kind, seed) in EXPLAINED:
                        moved.append((kind, seed))
                    elif diff:
                        failed.append(f"{kind} {seed}: " + "; ".join(diff[:6]))
                        print(failed[-1], file=sys.stderr, flush=True)
                    record[f"{kind}:{seed}"] = digest(theirs["figures"])
            finally:
                proc.stdout.close()
                proc.wait(timeout=600)
        self.assertEqual(failed, [], "households moved")
        self.assertEqual(sorted(moved), sorted(EXPLAINED),
                         "an explained move no longer moves")
        if os.environ.get("OIKONOME_RECORD_REFERENCE") == "1":
            RECORD.write_text(json.dumps(record, indent=1, sort_keys=True)
                              + "\n")

    def test_against_the_recorded_reference(self):
        recorded = json.loads(RECORD.read_text())
        for kind, seeds in _families():
            for seed in seeds:
                key = f"{kind}:{seed}"
                if key not in recorded or (kind, seed) in EXPLAINED:
                    continue
                with self.subTest(household=key):
                    self.assertEqual(digest(_mine(kind, seed)),
                                     recorded[key])


if __name__ == "__main__":
    unittest.main()
