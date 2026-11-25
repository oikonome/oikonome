# A Spending figure opens exactly the rows it summed (spec)

Every figure on the Spending tab of Cash Flow is a link to the
transactions that figure counted — not every row filed under the name, not
the merchant's whole history: the rows, and only the rows, the figure
summed, listed under a chip that prints the figure itself. The contract is
the one in `specs/counted-figures.md`: one deciding function on the
server, clients print.

## The one function

`engine.reporting.spending_window_ledger(conn, today, range_key, *,
category=None, merchant=None, side="current")` returns
`{txn_ids, amount, label, from, to}`.

- It and `spending_window` read one scope, `_spending_window_scope`
  (the window's dates for the side asked for) over `_spend_scope` (the
  spend predicate `SPEND_WHERE`, the excluded accounts, the dates). The
  category totals (`_spend_cats`) and the merchant totals
  (`_spend_merchants`, folded by `_fold_merchants`) are the same queries
  in both. A change to what counts as spending moves the figure and its
  rows together.
- `category`: the value the ledger stores ("FOOD_AND_DRINK"), which is
  how `spending_window` names each category — the spelling carrying most
  of the category's spend when the ledger holds it both underscored and
  spaced, since the figure folds the two (`PART_CAT`). The drill reads
  either spelling through that same fold, so an older client sending the
  spaced form ("FOOD AND DRINK") opens the same rows. Rows with a part in
  that category (a split row is listed once, counted for its part).
  Amount = the category's figure. Because the link carries a stored key,
  the Transactions page can keep it as its category filter when the list
  is narrowed by a search, an account or a date, and its category list
  has the option.
- `merchant`: rows whose display merchant folds to the same payer key as
  the name (`paychecks.payer_key`, punctuation and case) — the fold the
  figure used, so "A-B-C" and "A B C" are one merchant here too.
- neither: the window's whole spend; amount = the headline total (the sum
  of the category figures, as the tab adds it).
- `side="previous"`: the previous equal window the tab compares against —
  the window a dropped merchant was paid in, and the "before" of a mover.
  A range with no previous window, an unknown range or an unknown side is
  a `ValueError` (400 at the door).
- `label`: the chip's words — the figure's name and the days it was read
  over in the tab's own heading words ("Food and Drink · Jun 2026",
  "All spending · Jul 1–15 so far", "Old Gym · May 2026, the window
  before").

Spending counts every row `SPEND_WHERE` keeps: a pending hold counts like
a posted charge; refunds, transfers, card payments, shadow twins of a
linked account, business rows and excluded accounts never list. A
partly reimbursed charge is listed and counted for the part kept.

## The door

`GET /api/transactions?spend=<range>[&cat=…|&merchant=…][&side=previous]`
is a third engine-named branch beside `bucket` and `counted`: every other
filter stands down. The reply is the counted branch's shape plus
`drill_label`; `amount_sum` is the figure, `per_day` divides it over the
window's days the ledger has seen. The download door takes the same
parameters, so a drill downloads as the rows it lists — the same ids,
each at its whole amount as the list prints it (the figure is the chip's
and the hero's, not a sum of the file), and a bad drill is a 400 there
too. `GET /api/reports/spending/window` says `figures_open_rows: true`, the
door a client asks before it links a figure.

## What each client reads

- **Web** (`pages/Spending.tsx`, `spendDrillHref`): the headline total,
  each category's name and amount, each merchant's name and amount and each
  "What changed" row link to `/transactions?spend=…`; a category mover
  opens the current window, a merchant mover (paid only before) the
  previous one. A merchant row also carries a small "history" link to
  `/bills/history`, the merchant's identity page.
- **Phone** (`app/spending.tsx`, `spendDrillParams`): the same targets —
  each bar row presses through, the headline total and each mover are
  links, a merchant row carries "history" beside its name. A phone talks
  to servers older than itself, so it links the figures only when the
  window reply says `figures_open_rows: true` (`spendDrillOffered`); a
  server from before the drill sends nothing and its figures are plain
  text.
- **Transactions (both)**: `spend`, `merchant`, `side` are read from the
  link with `cat`; the chip prints `drill_label` verbatim once the reply
  answering THIS link is on screen, the hero prints `amount_sum` as the
  total. Clearing the chip returns to the month browse. On the phone, a
  reply to a `spend=` link that carries no `drill_label` is a server that
  dropped the parameter and listed something else (`spendDrillState`
  "unsupported"): the tab says in one line that this server can't list a
  figure's rows and returns to the month browse.

## The merchant's history page is a different question

`/bills/history` lists every row filed under a name over any timeframe —
holds, refunds and twins included, by design — with its monthly totals and
bill fit. It is never presented as "the rows behind this figure".

## Older client, stale tab, restore

- **Older client**: never sends `spend`; nothing it reads changes.
  `drill_label` is an added field.
- **Stale tab**: a link opened after midnight or a month end reads the
  window of the day it is opened; the chip prints the days it read, so a
  figure and its rows read over different days say so.
- **Restore**: carries transactions and settings, never a figure; the
  drill is recomputed from what is landed.

## Two drills, two predicates

The lens drill (`counted=1`, Month and Year category labels) follows the
lens predicates (bill occurrences, envelopes); the Spending drill follows
the Spending tab's. Each chip names its figure and window, so the two
never read as the same list.
