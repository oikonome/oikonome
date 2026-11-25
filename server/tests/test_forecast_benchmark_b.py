"""The cash forecast is scored against simulated households with known
truth (specs/forecast-benchmark.md). Second half of the default run: the
debit-only household, the payday savings sweeper and the near-zero
student. The rules are the first half's (test_forecast_benchmark_a).
"""

import unittest

from .forecast_bench import checks, personas


class ForecastBenchmarkSecondHalf(checks.DefaultBenchmark):
    PERSONAS = personas.DEFAULT_B


if __name__ == "__main__":
    unittest.main()
