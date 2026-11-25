# The cash forecast benchmark

The cash forecast must be good for every kind of household, not only the one it was tuned on,
and an estimator change must be judged on evidence. `server/tests/forecast_bench/` simulates
households with known ground truth, feeds them to the app through the production feed code, and
scores `forecast.build` against what really happened. Every name and amount in it is invented.

## The simulator
- `world.simulate(persona, seed)` is pure and deterministic (generators seeded by a CRC of
  persona, seed and stream; never the global generator or the clock): accounts, a ledger with
  Plaid's categories and pending→posted settlement, card statements and payments, bills and
  paychecks with business-day rules, and the true end-of-day available checking balance (plus
  `World.counterfactual(day)`, the balance had the household not reacted to a dip after that
  day). Randomness is drawn first; the walk over the days then applies what depends on the
  balances (statements, payments, sweeps, overdraft fees, the reaction), so the counterfactual
  re-walks the same draws with the reaction off.
- Personas are parameter sets (`personas.PERSONAS`, each knob documented there): biweekly hand
  payer, monthly autopay, minimum-payment revolver, debit only, two earners, weekly gig income,
  hourly, quarterly commission, retiree, near-zero student, payday savings sweep, landlord,
  seasonal utilities, annual bills, two weeks of history, a job change, two checking accounts,
  a non-US calendar, a first check of each year smaller than December's. Every persona carries weekday and pay-cycle rhythm, one-offs from many
  merchants, pending lag, price drift, a missed bill paid late with a fee, a refund, and a
  reaction to a dip.
- `feed.SimPlaid` serves the World through `sync.plaid.sync` and `sync_liabilities` (an httpx
  transport: `/accounts/get`, `/transactions/sync` with a cursor per served day,
  `/liabilities/get`, `/item/get`). A due date already passed reads as the next statement's.
- `BenchHousehold.night(day)` runs what the nightly sweep runs before a forecast — the Plaid
  pull, the liabilities pull, `budget.snapshot_month`, then the bill schedule: `planted` enters
  every true series seen twice (its last real amount, its next date by its own rule) through
  `bills.save_bill`; `detected` runs `bills.run(conn, today=day)` and approves every new-bill
  offer, rolling a passed due date on the bench's day. The forecast is
  `forecast.build(conn, today=day)`.
- The engine must read no clock when given `today`. `household.ClockWatch` records every read
  of the wall clock inside the product during a night and a forecast (`date.today()`,
  `datetime.now()`, `localtime.now_local`/`household_day`, and inside the forecast SQL's
  `CURRENT_DATE`/`now()`), by function. The reads not yet fixed are listed with their reason in
  `bench.KNOWN_CLOCK_READS`; the clock test is an expected failure while the list is not empty,
  and a read not on the list fails outright.

## Scores
For a forecast made on day `d`, `P` is a walk's balance at the end of `d + h` and `A` the true
available checking balance that day (`e = P − A`, positive = rosier than reality), divided by
the household's scale `I` (its mean monthly income, or its mean monthly spending when larger).
Per persona and horizon (7, 14, 30, 45, 60 days): mean absolute error and bias; for the headline
walk (`pace_stmt`, "your usual"; `pace` on an engine without it) the low-point error (amount and
median days), shortfall precision and recall (against the counterfactual balance, and against
the real one), lead time, band warning recall and coverage when the reply carries a band
(`band.lo`, else a `pace_low` walk). Every walk in the reply is scored under the engine's own
name, so a new scenario is measured the day it appears.

## Rules
- Every `make test` runs six sentinel personas (biweekly hand payer, monthly autopay, revolver,
  debit only, payday sweep, student) on four fixed days in two modules,
  `test_forecast_benchmark_a.py` / `_b.py`, each under 10 s. They fail on a regression beyond
  tolerance against `baseline.json` (`max(5 %, 0.005 × I)` for an error, `0.01 × I` for |bias|,
  0.05 for a rate, `max(5 %, 1 day)` for days), past a hard ceiling (for a persona the design
  claims and has not been measured weak: `nmae_30 ≤ 0.25`, `|bias_30| ≤ 0.10`, recall ≥ 0.7
  over at least three dips), on a walk that does not start on its day, and on a new clock
  read. A run is byte-identical twice.
- `make test-long ONLY=forecast_benchmark` runs every persona, three seeds, planted and detected
  bills, weekly days over the last twelve months of a 24-month world, and writes
  `forecast_bench/runs/<sha>-long.json` (not tracked).
- `make forecast-scoreboard BASE=<ref>` runs this checkout's benchmark against the engine at
  `BASE` (a temporary detached worktree with its own test database) and this checkout's engine
  on identical households, and prints the change per persona. `MODE=long`, `SEEDS=n`,
  `ONLY=<regex>`, `JOBS=n`.
- `make forecast-baseline` is the only way the baseline moves; a worse value needs `WHY="..."`,
  kept in the file's history. An estimator change bumps `forecast.FORECAST_MODEL` and carries
  the ratcheted baseline.
- Personas the design does not handle yet are marked `known_weak` with the reason; they record
  what the design does not cover yet, not exemptions from being measured.

## The nightly replay

`forecast_bench/replay.replay(persona, seed)` steps a household through `REPLAY_NIGHTS` = 180
consecutive nights from `REPLAY_START`: each night the feed's pull, the month's snapshot and
the planted bills (`BenchHousehold.night`), then the forecast's own record
(`forecast_score.record`, what the nightly forecast step runs). It then grades the kept nights
exactly as the app does (specs/forecast-grading.md) and reports:

- **The grader is right**: every graded miss equals the world's true checking balance on the
  target day less the forecast's prediction for it, to the cent.
- **"Why it missed" adds up**: for one in three 30-day misses whose night still holds its
  inputs, the parts sum to the miss to the cent.
- **The sentence's figure** is the 80th percentile of the 30-day misses of the last 90 days,
  rounded up.
- **The band's coverage**: the quantiles learned from the misses whose day came in the first
  `BAND_LEARN_NIGHTS` = 100 nights, against the truth of the next `BAND_TEST_NIGHTS` = 50
  nights' predictions, per horizon. The target is 8 in 10 (`COVERAGE_RANGE` 0.70–0.90).

`tests/test_forecast_benchmark_replay.py` (long mode, every persona, seeds 1–3) holds the first
three, and the band (`replay.band_at`: the pooled band as Today draws it, its heavier-line floor
included): the mean coverage over the personas the design claims is 0.70–0.92 at 14, 30, 45 and
60 days, no claimed persona's mean over those horizons and seeds is under 0.55, and the median
half-width at each horizon is under `replay.WIDTH_MAX` of a month's money. One persona's coverage
over 50 nights is not gated: a perfectly calibrated band lands inside 0.70–0.92 for one persona
and three seeds only about half the time at 60 days (two or three separate 60-day stretches),
so the gate is the average, and each persona's figure is reported. The known-weak personas are
reported, never gated. `replay.replay_raw` dumps a replay's predictions and truths for studying
a band offline.
