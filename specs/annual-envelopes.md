# Annual-pool envelopes

## Why

Envelopes today are a **monthly cap with no rollover**: `bill_type="envelope"`
forces `frequency="ENVELOPE"`, `next_due=None`, `monthly = amount`, and
`match_envelopes` re-zeros `used` every calendar month, overflowing the rest
into variable spend. That fits the shape it was built for — food refills
(`ENV_MIN_MONTHLY = $20/mo`, detection fingerprint is "food only with
refill-like amounts").

It cannot express an **annual** cost that arrives in lumps. Take an
invented series: six renewals, four of $15 and two of $30, **$120** a year,
landing in only 4 of 12 months — $30 in March, $15 in May, $45 in August
($15 + $30), $30 in November ($15 + $15). Modelled against it:

| cap/mo | counted as bill | leaks to variable | reserved/yr | vs actual $120 |
|---|---|---|---|---|
| $10 (annual ÷ 12) | $40 | **$80 (67%)** | $120 | $0 |
| $30 | $105 | $15 | $360 | +$240 |
| $45 (biggest month) | $120 | $0 | $540 | +$420 |

The arithmetically-correct monthly envelope leaks 67% of the bill into
variable spend — the renewal months read as overspending, which is exactly
what the envelope was supposed to prevent. The only cap that stops the leak
reserves 4.5× the real cost and permanently suppresses plan surplus. The unused
pool in the eight quiet months evaporates.

## Decisions

1. **Period = calendar year, resets Jan 1.** Simple to state and to read
   ("$60 left this year"). Consumption is derived from the ledger, so an
   envelope created mid-year correctly counts what the year already spent.
   *Rejected — rolling 12 months:* degenerate here. Trailing-12 spend equals
   the pool, so it reads exhausted on day one and every renewal overflows.
   *Rejected — first-charge anniversary:* every envelope gets its own
   invisible year boundary; no shared "this year" to reason about.
2. **Overflow stays overflow.** Once the *year's* pool is spent, further
   charges overflow to variable exactly as monthly envelopes do today. An
   annual envelope changes *when* the cap binds, not what a cap means.

## Model

`ENVELOPE:12` — the cadence select already encodes `<FREQ>:<interval>`, and
`save_bill` already takes `interval`; envelopes just ignored it. So the annual
variant needs no new field on the wire.

| | monthly envelope | annual envelope |
|---|---|---|
| cadence value | `ENVELOPE:1` | `ENVELOPE:12` |
| `raw.envelope_months` | 1 | 12 |
| `recurring.amount` | the monthly pool | **the annual pool** |
| `recurring.monthly_amount` | = amount | **amount ÷ 12** |
| cap that binds | pool, per month | pool, per calendar year |
| plan / forecast share | amount | amount ÷ 12 |

`monthly_amount = pool/12` is what makes the rest fall out for free: the plan's
`fixed_month`, `dynamic_variable_budget`'s `available = income − fixed_month`,
and the forecast's `env_daily = Σ monthly ÷ 30.4` all keep working unchanged
and now spread the annual cost evenly, which is the point.

## Mechanics

- `_envelope_bills` grows `pool` (period dollars) and `period_months`;
  `monthly` becomes `pool / period_months`. Monthly envelopes are the
  `period_months = 1` case, where `pool == monthly` — so the existing path is
  literally unchanged, not just compatible.
- `match_envelopes(rows, envelopes, matched_idx, prior_used=None)` caps at
  `pool − prior_used[payee] − used`. Monthly envelopes always pass
  `prior_used = 0` (their period *is* the month being matched), which
  collapses to today's `min(amount, monthly − used)`.
- New `_envelope_prior_used(conn, envelopes, today, excluded)`: for annual
  envelopes only, sums ledger spend matching the envelope from Jan 1 to the
  first of the current month. January short-circuits to 0. Uses the same
  first-match-wins order as `match_envelopes`, so two envelopes claiming one
  merchant can't double-count.
- Verdict recognition is untouched: `fixed_actual/fixed_expected_td += used`
  (recognized at payment — never "over"), `fixed_month += monthly`.
  The $45 of renewals in August therefore lands as $45 of *fixed* spend
  while the plan carries $10/mo — the bill never reaches the variable verdict.

## Surfaces

- Cadence select: `("ENVELOPE:12", "envelope (annual pool)")`; `_bill_cadence`
  returns `ENVELOPE:{envelope_months}`; `_cadence_label("ENVELOPE", 12)` →
  `"annual envelope"`. Recurring page gets an **Annual envelope** group.
- Bill detail (the legacy Used/Chgs/Paid envelope columns) shows the pool it's
  drawing from and what's left *this year*, not just this month's slice.

## Out of scope

- Detection/proposals: annual envelopes are **manual only**. The envelope
  fingerprint stays food-refill shaped; auto-proposing annual pools off ~10
  scattered charges/yr is a different (and much noisier) inference job.
- Other period lengths (quarterly, etc.). `envelope_months` is an int, so the
  door is open, but only 1 and 12 are offered.
