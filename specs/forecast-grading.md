# The cash forecast grades itself

The 60-day cash forecast (specs/today-figures.md) is a prediction. A prediction a household is
asked to plan around should say how good it has been — on this household, not on average.
Every night the app keeps what the forecast said; when each day it pointed at arrives, it
compares the prediction with the real checking balance, and the page says, in plain words, how
close it has landed. Amounts in examples are invented.

## What is kept each night

- `forecast_score.record`, run by the forecast's step (`jobs/worker.forecast_all`, every ten
  minutes → `forecast_sweep` → `forecast_tenant`: once per household-local day, the first tick
  after the household's own clock passes 04:00 and that night's bill detection has run — a
  household whose 04:00 falls between an hour before the instance's nightly (04:17 on the
  instance's clock) and ninety minutes after it waits until its own `nightly-detect` heartbeat is
  newer than that start (`_detection_pending`, read from the household's row, never a
  process counter; past the ninety minutes a household whose detection failed is kept anyway). The step has its own lock: it yields to a
  nightly holding the household, and a nightly reaching a household mid-build runs as ever.
  One query finds the due households — their clock past 04:00 (each day read in Python by
  `localtime.tenant_tz`, the resolver the step keeps it by) and no marker (`job_runs` "forecast", its note the local day) for their
  local today — so an idle tick costs that query alone; every household visited is marked,
  one with no checking account (which keeps no night) too, so each is built once a day; a
  tick starts no household past `FORECAST_TICK_BUDGET_S` = 240 s, the rest due on the next
  tick, under a job timeout of 600 s — the same moment of every household's day in its own zone, no day
  doubled or skipped across a daylight-saving change — in the same build that stores what the
  forecast learned for the day; a Today build after 04:00 local keeps it first when the step
  has not reached the household yet, `record_if_due`), writes one `forecast_nights` row per household-local day: the checking account the walk
  started from and its balance; for each scenario (your usual, pay all cards now, pay on the due
  date, the heavier month) the predicted balance 7, 14, 30, 45 and 60 days ahead
  (`forecast_score.HORIZONS`), the low point and its day, the card payments assumed by each
  horizon; the pace, its shares and the heavier factor; the forecast's model
  (`forecast.FORECAST_MODEL`); and, for `SCORE_INPUTS_NIGHTS` = 120 nights, the inputs that
  cannot be rebuilt later (each card's mode and lag, the card debt, the dated bills, paychecks
  and payments on their way, the bills a card pays, the one-offs left out). Amounts are whole
  cents, days are offsets from the night. Rows are kept `NIGHTS_KEPT` = 400 nights; the record
  trims both keeps each night.
- The first record of a day stands (`ON CONFLICT DO NOTHING`): a console re-run is harmless,
  and nothing is ever back-filled — a forecast "as issued" cannot be rebuilt honestly once the
  schedule, balances and settings of that night are gone. A night the sweep did not run has no
  row.
- No checking account, or one whose balance nobody has set: no row.
- `fresh` says the checking account was updated by a pull, a push or an import in the
  `FRESH_HOURS` = 36 hours before the row was written (the account's own update stamp, or a
  successful sync of its connection). A balance typed in by hand stamps the account as updated
  (both doors that set one), so a hand-kept checking is graded like a linked one. An account
  never stamped at all (no update recorded) is not fresh: nothing says when its balance was
  true. A stale row is never graded and never used as a real balance.
- `restored` marks a row a restore landed; this instance's own rows are never compared with it.

## How a forecast is graded

- The real balance on a day is the starting balance the forecast recorded that night — the same
  account, the same rule, the same moment of the household's day. No separate table of actual
  balances exists.
- A prediction for day `t` is compared only when both nights are fresh, the checking account is
  the same, and the pair does not cross a restore. A night that was not kept is not rebuilt: the
  next night's balance holds charges still pending that post dated after the missing day, so
  walking it back by the posted rows between is wrong whenever a hold straddles the gap (the
  pairs that need it are skipped as `offline`; `rebuilt` is always false). Each skip is counted under its reason
  (`stale`, `account_changed`, `restore`, `offline`) and the panel says how many forecasts and
  why; a night never kept counts each of its horizons whose day has come as offline too. The
  band's separate stretches of history count the nights actually kept, not the days since the
  first one.
- An account that moves. A reconnect moves the checking account (or a card) to the id the
  fresh link minted, and a restore-merge folds a restored account into its live twin; it is
  the same account under a new name, so the kept nights move with it in the same transaction
  as the ledger: `checking_id` is one of `adopt.ACCOUNT_REFS`, and the card keys of
  `pace.shares` (the only account ids inside a night's JSON: the walks are per scenario, the
  inputs name a card by its name) are renamed by `adopt._carry_forecast_nights`, a share
  already on the new id added to. A pair across the move is graded as if nothing happened. A
  restore spells a night's `checking_id` and its share keys the way it spells the accounts
  (the comma and taken-name renames), so a restored night names the account its ledger lands
  on. A night whose checking account the household no longer has (kept before the move carried
  nights) is skipped as `account_changed` (`forecast_score.on_kept_accounts`): its ledger reads
  as empty, which is no record of $0 in card payments or transfers. Every column that holds an
  account id is either in `ACCOUNT_REFS` or in `adopt.NOT_ACCOUNT_REFS` with its reason
  (`tests/test_account_reference_coverage.py`).
- Errors (real − predicted) are computed from the kept rows when read
  (`forecast_score.graded_pairs`, pure); nothing per pair is stored, so a restore, a pin change
  or the keep never needs a second rule to re-grade.
- "The forecast" is the usual scenario, the headline. Paying all cards now and paying on the due
  date are what-ifs: they are compared with what the household really paid its cards out of
  checking (`forecast_score.card_paying`), never graded for accuracy. "Pay all cards now"
  assumes today's card debt and then every dollar the usual walk charges to a card (its card
  shares of the pace, the envelope drips on a card, the bills a card pays) on the day it is
  charged.

## What the household sees

- `forecast.score.text` (web, phone, both emails, under the chart), from the misses whose day
  came in the last 90 days at the horizons with at least 30 results spread over at least 45
  days. Overlapping misses are not independent results — thirty 30-day misses over 90 days are
  about four separate stretches — so a frequency is said only for the longest of 30, 14 or 7
  days with at least `INDEPENDENT_MIN` = 6 separate stretches ((span + h) ÷ h): "Over the last 3
  months, the 14-day forecast landed within $250 of your real balance 4 times in 5." (the 80th
  percentile of its misses, rounded up to a plain figure: $10 steps under $200, $50 under
  $1,000, $100 above, never under $10). A longer horizon without enough separate stretches says
  its typical (median) miss and the weeks it rests on: "Over your last 13 weeks the 30-day
  forecast was typically within $600." Either part may stand alone.
- When taking the money the household moved between its OWN accounts out of the misses at
  least halves their total size, the figures are the ones without it and the sentence says so:
  "…, not counting money you moved between your own accounts." A transfer on checking counts
  only when it pairs one to one with a transfer row on another of the household's accounts —
  both legs transfer-typed, the opposite amount, within `OWN_TRANSFER_DAYS` = 3 days, the nearest
  day first (`forecast_score.own_transfer_ids`; the engine keeps no other pairing of transfer
  legs). A payment to another person is not, nor is a refund of the opposite amount.
- `forecast.score.bias_text`, only when the misses lean one way, at the longest horizon whose
  misses of the last 60 days cover `INDEPENDENT_MIN` separate stretches (7 days in practice:
  sixty days hold about three separate 30-day stretches): at least 70 % share a sign, at least
  30 of them, and the median is at least $100 and 5 % of the plan's monthly income: "Lately the
  7-day forecast has come out about $250 above your real balance." The forecast line itself is not moved; the band leans instead.
- `forecast.score.note`, the first 60 days after the first night, web and phone only, until a
  horizon qualifies: "The forecast now checks itself against your real balance each night; the
  first results show here in about 4 weeks." A new household sees nothing else: no replayed past
  is ever shown as a record.
- **The band**: once BAND_MIN_PAIRS = 30 misses are graded, 30 of them at 7 days (about five
  weeks of nights), each scenario's shaded band on the chart is the scenario's walk plus the
  forecast's own misses, pooled across horizons (`forecast_score.band_pool`): every miss whose
  day came in the last 180 days, divided by the household's own growth h^a (`growth_power`: the
  power fitted to the median |miss| at 7, 14 and 30 days, held in 0.3–1.0, 0.5 when it cannot be
  fitted), the last 90 days counted twice, the current model's misses alone once they number 30;
  the pool's 5th and 95th percentiles, times k^a on day k, k counted in days after today (the
  walk's series starts at today, a balance already known: no width there). While day k has fewer than
  BAND_FLOOR_INDEPENDENT = 6 separate k-day stretches of kept history behind it, the band is
  never narrower than the heavier-month line sits below the walk that day (and as far above).
  It leans when the misses lean. Sent as `band: {lo, hi}` on every scenario for every day of the
  chart, with the sentence "The shaded band is the range the forecast's own past misses
  suggest." and the legend words `band_label` ("range of past misses"), which the clients print
  as sent — no frequency is claimed in them. A band of no width (misses that never moved) is
  floored at the heavier line too. Its edges are computed unrounded and rounded once. Before the
  band qualifies, the heavier-month line, unchanged.
- **How the band was chosen** (specs/forecast-benchmark.md, "The nightly replay"): one horizon's
  misses overlap — a hundred nights hold about seventy 30-day misses, three separate stretches —
  so their own 10th and 90th percentiles held the next weeks' truth only about half the time.
  Pooling the horizons by the household's growth makes the many 7-day stretches carry the
  statistic; the 5th–95th level is what the overlap costs; the heavier-line floor covers the
  horizons too long for the history kept. On the benchmark's simulated households (18 personas,
  seeds 1–3, the band learned from 37 to 180 nights and tested on the 30 to 60 nights after),
  this band held the truth 0.75–0.88 of the time on average at 14, 30, 45 and 60 days for the
  personas the design claims, where the per-horizon 10th–90th band held 0.44–0.72. Its median
  half-width there is about 0.13, 0.26, 0.38 and 0.53 of a month's money at 14, 30, 45 and 60
  days.
- **The quiet warning** (`todayview.cash_band`) reads the band's lower edge, once the band is the
  forecast's own: "Your usual pattern stays above $0 (low $3,270 on 05/12/27). At the low end
  of the forecast's past misses it would dip to -$165 around 06/08/27." Paying all cards now stays
  the other trigger; the red notice still reads only the usual walk.
- **"How has the forecast done?"** (web and phone, opened from the track-record line): the last
  90 days of 30-day predictions against the real balance, the band's coverage measured
  walk-forward — each check against the band as it stood the night that forecast was made, from
  the misses known then only (`walk_forward_coverage`; under 10 checks, no sentence): "Checked
  against later days, the real balance landed inside the shaded band 8 times in 10 (42
  checks)." — said for the longest horizon whose checks cover `INDEPENDENT_MIN` separate
  stretches ("…the real balance 14 days ahead landed inside…"), and not at all otherwise — how the household's card payments compared with the
  scenarios, and why days were not graded. Each day opens **"Why the forecast missed"**: the miss
  split into paychecks, bills, card payments, spending, money moved between accounts, refunds and
  other, and the timing of pending charges — the parts add up to the miss to the cent — and the
  five largest causes in words. A row is a bill's when the bill matcher gave it that bill's
  occurrence (the same pass that marks a bill paid, every month the window touches), a
  paycheck's when that income series claims the deposit (`forecast_score.matched_owners`);
  card payments, transfers and the rest by their categories. A payment on its way is a card
  payment.

## Doors and fields

- `/api/today/full` → `forecast.score: {text, bias_text, note, horizon, basis: "own"|"none"}`;
  each scenario gains `band: {lo, hi} | null` and `band_text`; `pace_low`, `heavy` and
  `cash_band` keep being sent.
- `GET /api/today/forecast-score` → the panel (`forecast_score.panel`), and the drill beside
  it, are rate-limited like Today and computed once per household per day and kept night
  (`panel_kept`, `miss_kept`). `forecast_score.current` computes a day's grading once when no
  night is kept today yet and keeps it beside the day's learned facts.
- `GET /api/today/forecast-score/miss?day=YYYY-MM-DD&horizon=30` → the drill
  (`forecast_score.miss`); 404 when that forecast is past its inputs' keep or was not graded.
- Both are GET, viewer-readable like Today.

## Rules

- One function each: `forecast_score.record`, `graded_pairs`, `summarise`, `band`, `panel`,
  `miss`; the sentences in `todayview.score_sentences`. Clients print what is sent.
- Export and restore carry `forecast_nights` (it cannot be rebuilt from anything else; losing it
  on a move throws away months of calibration); restored rows land `restored`, with no score,
  and are graded only against each other. Only a night this instance could have kept lands:
  dated before the household's today (a restored today or later would stand in for the night
  the instance is about to keep, since the first row of a day stands), its walks in
  `compact`'s shape (a night past `NIGHTS_KEPT` lands and the next trim takes it). The household's deletion removes them. Nothing is sent
  anywhere; there is no telemetry.
- Older phone (newer server): no track-record line, the heavier-month line. Older server (newer
  phone): the phone prints no track-record line, draws the heavier line, and offers no panel.
- Changing an estimator bumps `forecast.FORECAST_MODEL` with it; the band prefers the
  current model's misses and the panel marks the day the model changed.
