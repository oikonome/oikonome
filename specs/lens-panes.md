# Lens-page top panes

The four panes at the top of Today — Verdict, Spend-at-most, Why, This
month's plan — follow the rules below. The same four slots appear on every
lens, scaled to its timeframe.

## Every fact has ONE home

| Fact | Home | Not repeated in |
|------|------|-----------------|
| Cash state (headroom, out-of-money date) | Verdict pane, bottom section | Spend-at-most |
| Month-end projection | Verdict pane's projection line | Why pane |
| Plan numbers (income/bills/budgets/surplus) | This month's plan pane (summary only, links to /budget) | Settings |
| $/day to hit | Spend-at-most | Verdict (it shows spent-of-budget, not the rate) |

## Cash: quiet until it matters (three states)

- **OK** (checking covers cards + bills before next paycheck, forecast never
  negative in horizon): **no cash line at all.** Silence is the signal.
- **Tight** (headroom < ~1 paycheck OR forecast dips below $0 later than 14
  days out): one calm neutral-color line — "Cash gets tight around 9/10
  (low −$1,400)" — no emoji, no red.
- **Critical** (headroom negative today OR forecast crosses $0 within 14
  days): red block that says what to do:
  "Short $2,100 by 7/16 — cover from savings or slow spending to $X/day."

## Wording & tone

- No alarm emoji, no ALL-CAPS beyond the verdict chip.
- The Why pane is substantive or quiet: name actual drivers with dollars
  ("Food is pacing +$310 over — Cedar Market 7/12 $220"); when nothing is
  over, name the nearest risk ("Closest to the line: Food, $55/day left").
  If there is genuinely nothing to say, one short line, not a full-height
  card.
- Verdict copy states the state + the one number, present tense, no
  scolding.

## Lens parity

Week/Month/Year mirror the same four slots (verdict · act-number · why ·
plan summary) with timeframe-appropriate content (Week: pace vs week
budget; Month: report card; Year: trend), through shared components so
surfaces can't drift.

## Email parity

today_body.html mirrors the SPA panes; the same dedup + tone rules apply
to the daily email.

## Out of scope

Verdict math — these rules govern display and language only.
