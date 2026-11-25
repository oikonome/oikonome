"""The cash forecast benchmark in long mode (specs/forecast-benchmark.md):
every persona, `props.seeds(3)` seeds, planted and detected bills, weekly
forecasts over the last twelve months of a 24-month world. Never part of
`make test`; `make test-long ONLY=forecast_benchmark` runs it, about a
minute per persona and seed.

Holds the hard ceilings for the personas the design claims to handle, the
walk-starts-on-its-day rule and the clock rule, and compares with
`baseline.json`'s `long` section when one is recorded. The scores are
written to `forecast_bench/runs/<sha>-long.json` (not tracked).
"""

import unittest

from . import props
from .forecast_bench import bench, checks, metrics, personas


@unittest.skipUnless(props.LONG, "long mode only (OIKONOME_PROPERTY=long)")
class ForecastBenchmarkLong(checks.DefaultBenchmark):
    PERSONAS = tuple(personas.PERSONAS)

    @classmethod
    def setUpClass(cls):
        cls.result = bench.run(list(cls.PERSONAS), props.seeds(3),
                               mode="long",
                               bills_modes=("planted", "detected"))
        cls.baseline = bench.load_baseline()
        bench.RUNS.mkdir(exist_ok=True)
        sha = (cls.result["engine"]["sha"] or "unknown")[:12]
        (bench.RUNS / f"{sha}-long.json").write_text(bench.dump(cls.result))

    def test_no_persona_got_worse_than_the_baseline(self):
        base = self.baseline.get("long") or {}
        if not base:
            self.skipTest("no long baseline recorded")
        worse = []
        for k, got in self.result["personas"].items():
            if k in base:
                worse += [(k, m) for m, w in metrics.compare(
                    base[k], got["metrics"]).items() if w > 0]
        self.assertFalse(worse, worse)

    def test_claimed_personas_stay_under_the_hard_ceilings(self):
        fails = []
        for k, got in self.result["personas"].items():
            if "@" in k:
                continue                    # the ceilings are for planted
            fails += [f"{k}: {x}" for x in metrics.ceiling_failures(
                bench.persona_of(k), got["metrics"])]
        self.assertFalse(fails, "\n".join(fails))


if __name__ == "__main__":
    unittest.main()
