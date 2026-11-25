"""The synthetic-household forecast benchmark (specs/forecast-benchmark.md).

`personas` holds the household kinds as parameter sets, `world` simulates
one with known ground truth, `feed` serves it to the production Plaid sync,
`household` steps a tenant through the nights the forecast depends on,
`metrics` scores `forecast.build` against the truth and `bench` runs and
compares. Every name and amount in here is invented.

Imports inside the package are relative and the only absolute imports are
`oikonome` and `tests.util`, so the scoreboard can run a copy of this
package against another checkout's engine.
"""
