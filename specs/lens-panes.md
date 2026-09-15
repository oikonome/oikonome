# Lens-page top panes: clearer, calmer, deduplicated

Status: SHIPPED. Kept as the design record.

## Why

The four panes at the top of Today — Verdict, Spend-at-most, Why, This
month's plan — grew feature by feature. Each is individually defensible;
together they repeat themselves and shout. The whole top of the lens pages
needs a clearer experience.

## Decisions

1. **Pains**: duplication across panes, and wording & tone. (Explicitly NOT
   chosen: hierarchy/structure complaints, numbers-without-context — the
   grid itself is fine.)
2. **Structure**: keep the four-pane grid; fix each pane's content. No hero
   redesign, no pane merging.
3. **Cash messaging**: **quiet until it matters** — three states, see below.
4. **Scope**: same shape on all four lenses (Today/Week/Month/Year), scaled
   to their timeframe — one visual language, four zoom levels.

## The rules

### Deduplication — every fact has ONE home

| Fact | Home | Remove from |
|------|------|-------------|
| Cash state (headroom, out-of-money date) | Verdict pane, bottom section | Spend-at-most's "Cash headroom (…)" line |
| Month-end projection | Verdict pane's projection line | Why pane (no re-explaining the same delta) |
| Plan numbers (income/bills/budgets/surplus) | This-month's-plan pane (summary only, links to /budget) | — (already gone from Settings) |
| $/day to hit | Spend-at-most | Verdict (it shows spent-of-budget, not the rate) |

### Cash: quiet until it matters (three states)

- **OK** (checking covers cards + bills before next paycheck, forecast never
  negative in horizon): **no cash line at all.** Silence is the signal.
- **Tight** (headroom < ~1 paycheck OR forecast dips below $0 later than 14
  days out): one calm neutral-color line — "Cash gets tight around 9/10
  (low −$1,400)" — no emoji, no red.
- **Critical** (headroom negative today OR forecast crosses $0 within 14
  days): red block, but say what to do, not "sell-stock signal":
  "Short $2,100 by 7/16 — cover from savings or slow spending to $X/day."

### Wording & tone pass

- Kill: "sell-stock signal", 🚨, ALL-CAPS shouting beyond the verdict chip.
- The Why pane must be substantive or quiet: name actual drivers with
  dollars ("Food is pacing +$310 over — Cedar Market 7/12 $220"), never filler
  ("On plan — no category is running over its pace" → replace with the
  nearest risk: "Closest to the line: Food, $55/day left"). If there is
  genuinely nothing to say, one short line, not a full-height card.
- Verdict copy states the state + the one number, present tense, no scolding.

### Lens parity

Whatever Today's panes become, Week/Month/Year mirror the same four slots
(verdict · act-number · why · plan summary) with timeframe-appropriate
content (Week: pace vs week budget; Month: report card; Year: trend). Shared
components where the SPA allows — same doctrine as PlanBars: one
renderer so surfaces can't drift.

### Email parity

today_body.html mirrors the SPA panes; the same dedup + tone rules apply to
the daily email (it already shares the template).

## Coordination

- Collapsing the forecast scenarios (3→2) touches the same verdict-pane
  cash block — land the two together, or sequence this one first.
- The fc_lows collapse and the negative-from work already fixed the tile
  duplication inside the forecast card; this spec is about the four panes
  above it.

## Out of scope

- The forecast card, PlanBars, and the checking-allocation bar (recently
  reworked; only their *copy* may get the tone pass).
- Any verdict-math change — display and language only.
