"""The cash forecast is scored against simulated households with known
truth, through the production feed and the real `forecast.build`
(specs/forecast-benchmark.md). First half of the default run: the
biweekly hand payer, the monthly autopay household and the minimum-payment
revolver.

Fails when a persona gets worse than `forecast_bench/baseline.json` beyond
tolerance, when a persona the design claims to handle passes a hard
ceiling, when a walk does not start on the day it was given, or when the
product reads the wall clock somewhere new. The run is also byte-identical
twice.
"""

import unittest

from .forecast_bench import bench, checks, personas


class ForecastBenchmarkFirstHalf(checks.DefaultBenchmark):
    PERSONAS = personas.DEFAULT_A

    def test_the_same_household_scores_the_same_twice(self):
        key = self.PERSONAS[0]
        again = bench.default_run([key])

        def strip(r):
            p = dict(r["personas"][key])
            p.pop("seconds")
            return bench.dump(p)

        self.assertEqual(strip(self.result), strip(again))


if __name__ == "__main__":
    unittest.main()
