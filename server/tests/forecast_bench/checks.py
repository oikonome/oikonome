"""The assertions every default benchmark module makes, shared.

A module names its personas and inherits `DefaultBenchmark`; the run is
made once per module (setUpClass) and every test reads it.
"""

from __future__ import annotations

import unittest

from . import bench, metrics, personas


def _table(rows) -> str:
    return "\n".join("  " + "  ".join(str(c) for c in r) for r in rows)


class DefaultBenchmark(unittest.TestCase):
    PERSONAS: tuple = ()

    @classmethod
    def setUpClass(cls):
        if cls is DefaultBenchmark:
            raise unittest.SkipTest("the shared base runs nothing")
        cls.result = bench.default_run(list(cls.PERSONAS))
        cls.baseline = bench.load_baseline()

    def test_no_persona_got_worse_than_the_baseline(self):
        worse, better = [], []
        for k, got in self.result["personas"].items():
            base = (self.baseline.get("default") or {}).get(k)
            self.assertTrue(base, f"{k} has no baseline: run "
                                  "`make forecast-baseline`")
            for metric, w in metrics.compare(base, got["metrics"]).items():
                row = (k, metric, base.get(metric),
                       got["metrics"].get(metric))
                (worse if w > 0 else better if w < 0 else []).append(row)
        if better:
            print("\nforecast benchmark improved; run `make "
                  "forecast-baseline` to ratchet:\n" + _table(better))
        self.assertFalse(worse, "worse than baseline.json beyond "
                                "tolerance (persona, metric, baseline, "
                                "now):\n" + _table(worse))

    def test_claimed_personas_stay_under_the_hard_ceilings(self):
        fails = []
        for k, got in self.result["personas"].items():
            fails += [f"{k}: {x}" for x in metrics.ceiling_failures(
                personas.PERSONAS[k], got["metrics"])]
        self.assertFalse(fails, "\n".join(fails))

    def test_every_walk_starts_on_the_day_it_was_given(self):
        probs = [f"{k}: {x}" for k, got in self.result["personas"].items()
                 for x in got["horizon_problems"]]
        self.assertFalse(probs, "\n".join(probs))

    def test_a_kept_forecast_is_the_one_a_fresh_build_gives(self):
        """The engine keeps what it learned per worker for the day; a night
        after a sync, a new day or a write must not be served a stale
        fact (household.BenchHousehold.forecast builds twice to see)."""
        stale = [f"{k}: {x}" for k, got in self.result["personas"].items()
                 for x in got.get("cache_mismatches", [])]
        self.assertFalse(stale, "\n".join(stale))

    def _clock_reads(self) -> dict[str, set]:
        out: dict[str, set] = {"night": set(), "forecast": set()}
        for got in self.result["personas"].values():
            for phase, reads in got["clock_reads"].items():
                out[phase] |= set(reads)
        return out

    def test_no_clock_read_beyond_the_known_ones(self):
        """A new place that reads the wall clock fails here outright; the
        known ones are listed with their reason in bench.KNOWN_CLOCK_READS
        and carried by the expected failure below."""
        new = [f"{phase}: {r}" for phase, reads in self._clock_reads().items()
               for r in sorted(reads)
               if r not in bench.KNOWN_CLOCK_READS.get(phase, {})]
        self.assertFalse(new, "the product read the wall clock while the "
                              "bench gave it a day:\n" + "\n".join(new))

    @unittest.expectedFailure
    def test_the_engine_reads_no_clock_when_given_a_day(self):
        """Expected to fail while bench.KNOWN_CLOCK_READS is not empty: the
        failure message lists each place and why it is wrong. When the
        product passes its day down everywhere this passes, unittest
        reports an unexpected success, and the decorator and the list go
        in that commit."""
        reads = [f"{phase}: {r} — "
                 f"{bench.KNOWN_CLOCK_READS.get(phase, {}).get(r, 'new')}"
                 for phase, rs in self._clock_reads().items()
                 for r in sorted(rs)]
        self.assertFalse(reads, "\n".join(reads))
