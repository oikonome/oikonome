"""The benchmark's nightly replay (specs/forecast-benchmark.md, "The
nightly replay"; specs/forecast-grading.md): every persona stepped through
REPLAY_NIGHTS consecutive nights of what the nightly sweep does, the
forecast's own record included, then graded. Long mode only
(`make test-long ONLY=forecast_benchmark_replay`), about a minute per
persona.

Holds: the grader is right (every graded miss is the world's true balance
less the forecast's prediction, to the cent); "why it missed" adds up to
each miss to the cent; the track-record sentence's figure is the 80th
percentile of the misses at the horizon it states, rounded up; and the band — learned from
the first BAND_LEARN_NIGHTS nights, drawn as Today draws it, against the
next BAND_TEST_NIGHTS nights' truth — holds 7 to 9 times in 10 on average
over the personas the design claims and seeds 1–3 at 14 to 60 days, no
claimed persona far below, at a width under replay.WIDTH_MAX. Writes the
table to `forecast_bench/runs/replay-<sha>.json` (not tracked)."""

import json
import statistics
import subprocess
import unittest

from . import props
from .forecast_bench import bench, personas, replay


def _sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short=12", "HEAD"],
                              capture_output=True, text=True,
                              cwd=bench.HERE).stdout.strip() or "unknown"
    except OSError:
        return "unknown"


SEEDS = (1, 2, 3)


@unittest.skipUnless(props.LONG, "long mode only (OIKONOME_PROPERTY=long)")
class TheForecastGradesItselfOnSimulatedHouseholds(unittest.TestCase):
    PERSONAS = tuple(personas.PERSONAS)

    @classmethod
    def setUpClass(cls):
        cls.runs = [replay.replay(k, s) for k in cls.PERSONAS for s in SEEDS]
        bench.RUNS.mkdir(exist_ok=True)
        (bench.RUNS / f"replay-{_sha()}.json").write_text(
            json.dumps(cls.runs, indent=1, sort_keys=True, default=str))
        for r in cls.runs:
            print(f"replay {r['persona']} seed {r['seed']}: coverage "
                  f"{r['coverage']} width {r['width']} within {r['within']}")

    def claimed(self):
        return [r for r in self.runs if not r["known_weak"]]

    def test_the_grader_is_right(self):
        wrong = {(r["persona"], r["seed"]): r["grader_wrong"]
                 for r in self.runs if r["grader_wrong_n"]}
        self.assertEqual(wrong, {})
        for r in self.runs:
            self.assertGreater(r["pairs"], 500, r["persona"])
            # the nights dropped at random are skipped, never rebuilt
            self.assertTrue(r["dropped"], r["persona"])
            self.assertGreater(r["skips"]["offline"], 0, r["persona"])

    def test_why_it_missed_adds_up_to_the_cent(self):
        for r in self.runs:
            self.assertGreater(r["drills"], 0, r["persona"])
            self.assertEqual(r["drill_wrong"], 0, r["persona"])

    def test_the_sentence_is_the_80th_percentile_at_its_horizon(self):
        for r in self.runs:
            # ninety days hold too few separate 30-day stretches for a
            # frequency: it is said for 14 or 7 days
            self.assertIn(r["sentence_horizon"], (7, 14), r["persona"])
            self.assertEqual(r["within"], r["within_want"], r["persona"])

    def test_the_band_holds_eight_times_in_ten(self):
        lo, hi = replay.COVERAGE_RANGE
        off = {}
        for h in replay.GATED_HORIZONS:
            covs = [r["coverage"][str(h)] for r in self.claimed()
                    if r["coverage"][str(h)] is not None]
            mean = statistics.mean(covs)
            if not lo <= mean <= hi:
                off[h] = round(mean, 3)
        self.assertEqual(off, {})

    def test_no_claimed_household_is_left_far_outside_its_band(self):
        by: dict[str, list] = {}
        for r in self.claimed():
            by.setdefault(r["persona"], []).extend(
                r["coverage"][str(h)] for h in replay.GATED_HORIZONS
                if r["coverage"][str(h)] is not None)
        low = {p: round(statistics.mean(v), 3) for p, v in by.items()
               if statistics.mean(v) < replay.COVERAGE_FLOOR}
        self.assertEqual(low, {})

    def test_the_band_is_not_uselessly_wide(self):
        wide = {}
        for h in replay.GATED_HORIZONS:
            widths = [r["width"][str(h)] for r in self.claimed()
                      if r["width"][str(h)] is not None]
            if statistics.median(widths) > replay.WIDTH_MAX[h]:
                wide[h] = statistics.median(widths)
        self.assertEqual(wide, {})


if __name__ == "__main__":
    unittest.main()
