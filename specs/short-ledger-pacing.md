# Short-ledger pacing (spec)

A household whose ledger starts inside the month — a new account, a file
import, a feed with little history — has no data for the days before its
first transaction. Those days are not days of no spending. Every surface
that paces the month reads them the same way, through one function:
`budget.pace_window(conn, today)` (and `pace_window_for(first, today)`
when the first ledger day is already known).

## The window

For the month of `today`, with `first` = the ledger's first day (excluded
accounts left out):

- **Finished days** run through *yesterday* while the month is live, and
  through month end once it is closed. Today is not over, so it is never a
  finished day: not in a rate's days, not in its spend.
- **`pace_start`** = the later of the 1st and `first` (one boundary rule:
  a ledger that began last month paces from the 1st).
- **`elapsed_days`** = finished days from `pace_start`.
- **`days_left`** = today and every day after it (0 when closed).
- **`paced_days`** = days from `pace_start` to month end;
  **`share`** = `paced_days / days_in_month`. The month may spend
  `budget × share`: the whole budget unless the ledger began this month.
- **`ledger_days`** = finished days of ledger since `first`, across month
  boundaries.
- **`settled`** = not in the month's first tenth (`EARLY_MONTH_FRAC`) and
  at least `MIN_RATE_DAYS` (7) finished days of ledger.

## What reads it

| Surface | Rule |
|---|---|
| Seed and suggestion (partial months) | spend over the finished days they cover, × `days_in_month` (`monthly_rate`) |
| Verdict expected-to-date | `budget × elapsed_days / days_in_month`, plus today's spend up to today's allowance |
| Today's allowance (tiles, verdict credit) | (`budget × share` − spend before today) ÷ `days_left` |
| Hero "$X of $Y left" | `variable_budget_paced` = budget × share; `month_remaining_paced` = that − month-to-date spend |
| Pace (forecast, month lens projection) | spend through yesterday ÷ `elapsed_days`; projected over `paced_days`, judged against `budget × share` |
| Closed month (lens, report card) | expected = `budget × share`, so a first month judges what its last live day judged |
| "running $X/day" | month-to-date spend ÷ days seen, today included |
| Plan surplus accrual | the calendar's finished days (money accrues whatever the ledger saw) |

Consequences: a suggestion or seed accepted on steady spending reads ON
BUDGET the same day — including a morning with nothing posted yet — and
every day after, across the month boundary; spending exactly what the
tiles hand out never reads OVER; the hero's remainder equals the tiles'
daily rates times the days left.

## The one floor

`MIN_RATE_DAYS` decides only whether the read is **settled**. While it is
not, the verdict never calls the household UNDER, the forecast burns at
the plan rate, and the month lens projects the plan (unless even the
plan's pace is OVER). The rate itself is never stretched or shrunk by it:
a floor that divided a few days' spend by a week's worth would put the
suggestion below the household's real rate, and no verdict can then agree
with tiles that are followed — the expected-to-date would stand still
while the tiles hand out a day's money. The planner says how many days
stand behind a scaled suggestion instead (`history_days`).

## No verdict without a saved budget

Under a month of history the loader carries a provisional seed that is
never saved. The Today page, the app, the daily email and the Month and
Year lenses judge only a budget the household saved (`budget.budgets_set`)
or a month's frozen snapshot; otherwise the month lens carries
`budget_set: false` and no verdict, and the year cell is a cash-flow
("net") cell.
