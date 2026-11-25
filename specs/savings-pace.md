# Savings-goal pace (spec)

How a savings goal answers "am I where I should be to have it by my date?".
The code is `engine/savings.py`: `schedule_days` (the rule), `progress` (the
verdict each surface prints) and `check_milestones` (the nightly mail and the
stored plan).

## The rule

A goal with a target and a target date has a **plan**: reach the target by
the date, starting from what was saved on the day that plan was set.

- **The schedule** is the straight line from (plan day, saved that day) to
  (target date, target), in days.
- **Off pace**: what is saved today is less than the schedule asked for
  38 days ago.
- **On pace again**, once a goal has been off pace: what is saved today is at
  least what the schedule asked for 7 days ago.
- **No verdict** for the first 38 days of a plan, on a day before the plan
  day, and while the goal is funded.
- **The date passing**: from the fourth day after the target date, a goal
  with money still to go is off pace. On the date and the three days after,
  the rule above still decides, so a finishing deposit that posts after a
  weekend does not read as a miss.

The verdict reads the saved total on each day and nothing else. It does not
measure a rate, detect a rhythm, choose a window or compare one deposit with
another, so the shape of the saving cannot change it: weekly, monthly, both
at once, a windfall, a deposit moved by a holiday.

**Why 38 days.** A saver who deposits once a month is, the day before each
deposit, a whole deposit behind any straight line. The grace has to cover
one cycle: 31 days, the 35 a "fourth Thursday of the month" can run, and
three more for a weekend or a slow posting.

**Why two thresholds.** With one, a saver putting in a few percent less than
the date needs would read on pace at every deposit and off pace a few days
later, and be mailed every month. Coming back takes real money.

The three numbers are `PACE_GRACE_DAYS`, `PACE_RETURN_DAYS` and
`PACE_POSTING_DAYS`.

## What is not part of the verdict

- **The 3-month figure** (`saved_90d`, "$X in the last 3 months") is the
  net contributions of the window `pace_since` opens — the day after the
  same day three calendar months back, 89 to 92 days, which is why it is
  never called a day count — printed as exactly that: it makes
  no claim about a month, so a steady monthly saver whose window holds two
  or four deposits reads two or four deposits. `rate_90d` is the same net
  divided by three, or by the months the ledger covers when it is younger;
  the goal line never prints it; it stays in the payload for released apps,
  the lenses' fallback estimate and the arrival date. Nothing is decided
  by either.
- **The arrival date** (`eta`) of a goal with a target and no date is today
  plus the money left at that rate. A goal with a date shows its verdict
  instead once it has one.
- **`monthly_plan`** is the forecast's commitment and is not compared with
  anything here.
- A goal with no target date has no verdict.

## The plan and where it is kept

Tenant config `savings_goal_state` holds one entry per goal:

```json
{"cushion": {"milestone": 25, "on_pace": true,
             "plan": {"day": "2026-03-09", "saved": 1000.0,
                      "for": [6500.0, "2027-02-01", "sav", [], 1000.0]}}}
```

`plan.day` and `plan.saved` are the line's starting point. `plan.for` is
what the plan was made for: target, target date, account, match words and
starting balance. `plan` is `null` for a goal without a target date.
`on_pace` is the last verdict the nightly sweep recorded and `milestone` the
last funded milestone it mailed.

The nightly sweep sets and keeps the plan:

| event | what happens to the plan |
|---|---|
| a new dated goal, or a date added to a goal | starts that night, at what is saved that night |
| target, date, account, match words or starting balance changed | a new plan starts that night; until the sweep runs the goal shows no verdict, then none for 38 days |
| the goal is renamed | kept: an entry whose name no goal carries any more and whose `for` matches is the renamed goal's plan |
| a restore that brings the state back with the goals | kept |
| a goal with no plan recorded and a ledger behind it (an install from before plans were kept, a restore without the state) | the line is drawn from 38 days back, at what the ledger shows saved on that day, never from before the account's first row; a verdict from the second night |
| the milestone mail could not be delivered | the plan is stored anyway, and so is a verdict that mailed nothing; the milestone and a fall onto off pace wait for the retry |

What is saved on each day is read from the ledger every time (the same rows
and the same partition between goals sharing an account as the saved total),
so the page, a past day's view and the nightly sweep give the same answer
from the same rows. Only the line's starting point is stored.

## The mail

The nightly sweep mails once when a goal's verdict becomes off pace, and not
again until it has been on pace in between:

- `cushion: off pace for <target date> — $3,000 saved; the straight line to
  $6,500 asked for $3,560 by now. $410/mo from here gets there`
- `cushion: off pace — <target date> has passed with $500 still to go`

The mail quotes the balance and the schedule, never the monthly rate.

A goal whose line was drawn from 38 days back stays quiet until it has
read on pace under that line once (`plan.quiet`, removed that night). Such
a line starts from the balance of 38 days ago and gives no credit for
having been ahead before then, so an off-pace verdict under it may come
from the line and not from the saver: the page shows the verdict, the first
night changes nothing, and no mail goes out. From the night the goal reads
on pace, a later fall is mailed like any other.

## What each surface prints

Today on the web, Today in the app and both renders of the daily email print
the same goal line from the same payload (`report.gather`):

| goal | line |
|---|---|
| target and date, on its schedule | `on pace for MM/DD` |
| target and date, behind it | `off pace for MM/DD` |
| target and date, no verdict yet; or target with no date | `~MM/DD at the current rate` |
| no target | `$X in the last 3 months` |
| funded | `funded ✓` |
| rate at or below zero, no deposit in the window | `no contributions in 3 months` |
| rate at or below zero, deposits outweighed by withdrawals | `no net saving in the last 3 months` |

The Budget page's goals card prints `on pace` / `off pace` for the same
verdict. `contributed_90d` in the payload tells the last two rows apart.

## Savers it is checked against

`tests/test_savings_pace_is_read_against_the_schedule.py` starts a plan on
every day of three consecutive calendar years for each saver and reads
it on every day up to its target date.
`tests/test_savings_pace_nightly_keeps_its_plan.py` runs the nightly sweep
night after night on a ledger. "On plan" means no slack at all: the last
deposit before the date is the one that finishes the goal.

| saver | verdict | mail |
|---|---|---|
| on plan: monthly on the 1st, the 30th or the last day, with or without weekends moved to Monday or Friday | on pace every day from day 39, the night before the finishing deposit included | none |
| on plan: weekly; every other week; the 1st and the 15th | same | none |
| on plan: weekly beside monthly, on a date or on the same weekday; every other week beside a monthly top-up | same | none |
| on plan: the fourth Thursday of the month | same | none |
| on plan: deposits a day or two either side of their day; holiday weeks early; skipped weeks in a fixed pattern | same | none |
| on plan, plus one extra deposit of 1.5, 3 or 6 times the usual | on pace every day | none |
| far ahead, then one deposit skipped | on pace every day | none |
| a fast start ($1,000 a month, then $500) or a change of rhythm | on pace every day | none |
| stops with most of the goal left | off pace 35 to 69 days after the last deposit, for good | one |
| stops near the end | off pace when the line passes the balance or the date passes | one |
| skips one deposit and carries on | off pace from four or more days after the miss | one |
| skips one and doubles the next | off pace about a week after the miss, on pace the day the double lands | one |
| puts in 97 % of what the date needs, for two years | on pace for months, then off pace for good | one |
| one withdrawal | off pace the day it posts if it is large, otherwise when the line passes the lower balance | one |
| only withdrawals | off pace from day 39 | one |
| a new goal with 0 to 3 deposits and then nothing | no verdict for 38 days, on pace until the line passes what was put in, then off pace | one |
| finishing deposit two days after the date | never off pace | none |
| date passes with one deposit missing | off pace from the fourth day after | one |
| funded, later withdrawn below the target | no verdict while funded, judged again afterwards | one |

## Stated limitations

- **A saver who fell behind and then sped up reads off pace until the gap
  is closed.** $500 a month for half the plan and $1,000 for the rest,
  finishing exactly on the date, is behind the straight line from early on
  until the last deposits: one mail, when the saver first fell behind. The
  verdict means "behind schedule", not "would not make it at the recent
  rate".
- **A saver far ahead who stops is told late**: on pace until the line
  catches up with the balance.
- **A goal that was already behind when its line was drawn from 38 days
  back is never mailed for that**, only for a fall after it has caught up.
- **Savers who deposit less often than monthly, or at irregular sizes and
  gaps, are behind the line between deposits.** Measured the same way as
  the table above. Each cell is the share of days read off pace, then the
  off-pace mails per plan of about eleven months; the columns are how much
  of what the saver puts in the plan needs.

| saver | all of it | nine tenths | four fifths | seven tenths |
|---|---|---|---|---|
| $1,500 every third month | 31.7 % · 2.95 | 18.1 % · 2.63 | 8.7 % · 1.43 | 6.1 % · 0.85 |
| $3,000 every sixth month | 54.0 % · 1.59 | 49.3 % · 1.59 | 43.2 % · 1.59 | 35.5 % · 1.59 |
| $200 to $800 at gaps of 20 to 40 days | 9.0 % · 1.30 | 1.3 % · 0.41 | 0.5 % · 0.19 | 0.2 % · 0.08 |

  These twelve figures are pinned by a test, so a change to them is
  deliberate.
- **A plan shorter than the grace** gets one check, after its date.
- **A row that arrives late with an earlier date**, or one that is removed,
  changes the replay, so a verdict can change after a sync correction.
- **The saved total and the rate count every matched row, including one
  dated after the day being viewed; the verdict counts rows up to that
  day.** They differ only for a past day's view or a row dated ahead.
- **An instance that copies its settings from another** copies
  `savings_goal_state` with them: the plan day and starting balance are the
  source's, read against the copy's own ledger.
