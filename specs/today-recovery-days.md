# Days to get back on budget (spec)

The spend-at-most tiles on the Today detail face say what a category's
daily budget was and what the month's spending has moved it to
("daily reduced from $93 to $74"). The question that line raises is
"how many days do I stop spending to get the $93 back?" — so the tile
answers it, per tile: Food, Everything else, and every carve-out child.

## The arithmetic

For a tile with `month_budget`, month-to-date `actual`, `days_left`
(today included) and `plan = month_budget / days_in_month`:

- `remaining = month_budget − actual`
- `recover_days` = the smallest `n ≥ 0` with
  `remaining / (days_left − n) ≥ plan`, i.e.
  `n = ceil(days_left − remaining / plan)`.
- `0` means nothing to recover (the forward rate already meets the plan).
- Unrecoverable this month, reported as `null`: `remaining ≤ 0`, or
  `n ≥ days_left` (every remaining day would have to be a no-spend day,
  leaving no day to spend the recovered daily on).
- An exact division (`remaining / plan` an integer) must not round up on
  float noise: the ceiling is taken with a hair of slack.

Computed ONCE, in `todayview.allowances._allow` via
`todayview.recover_days`, beside `daily_note`, so the page, the email
HTML and the plain text say the same number. The line is only composed
when the daily is reduced (`daily_note_tone == "neg"`); a raised daily
carries no line.

## The copy

- `skip N days of spending and the daily is back to $93`
- N = 1: `one no-spend day and the daily is back to $93`
- unrecoverable: `can't get back to $93 this month`

`$93` is the rounded plan daily, the same figure the note above it
names.

## Surfaces (all in the same change — the email mirrors Today)

- Web `Today.tsx`, detail face only, a muted line under the daily note.
  The summary face is unchanged.
- Mobile Today detail, the same line in the same place.
- Email: `today_body.html` (detail face) renders the line under the
  note; the plain text appends it inside the note's parentheses:
  `(daily reduced from $93 to $74; skip 3 days of spending and the
  daily is back to $93)`.

The API carries `recover_days` and `recover_note` on every allowance
entry (`allow.food`, `allow.other`, each of `allow_kids`).

Tests: `server/tests/test_today_recovery_days.py` — the n=0 edge, the
exact-division edge, unrecoverable, a carve-out child on its own
numbers, and the sentence on both email surfaces.
