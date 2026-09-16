# Home page with timeframe lenses (spec)

Decisions:
1. IDENTITY: the omicron logo + "Oikonome" (top-left) is the home button;
   the "Today" nav tab is DROPPED (nav starts at Transactions). The home
   page's own header carries the lens picker: [Today | Week | Month |
   Year] + ‹ › stepper (+ month/year dropdown on those lenses). Page
   title reflects the lens ("Today", "Week of 07/07/26", "July 2026",
   "2026"). Default lens on load: Today. Deep-linkable URLs:
   /app/?lens=week&start=YYYY-MM-DD style.
2. TODAY lens: the existing page, unchanged (verdict, spend-at-most,
   bars, forecast, month plan, recent).
3. WEEK lens: Mon–Sun calendar weeks, steppable into history. Weekly
   verdict pro-rated: weekly budget = monthly × (7 ÷ days-in-month) —
   handle month-straddling weeks by summing each day's daily allowance.
   Content: week verdict + margin, per-day spend bars, bucket bars
   (week-scoped), the week's transactions.
4. MONTH lens (current month = "in progress" report card w/ projections;
   past months = FINAL report card): final/projected verdict + margin,
   bucket bars, bills paid vs planned, MTD/final by category, biggest
   transactions, income vs spend.
5. YEAR lens: 12-cell month grid — each cell that month's final verdict +
   margin (current month shows projection, future months blank); annual
   totals (income, spend, saved, rate); category totals vs prior year;
   steppable across years. Closed months are judged against their frozen
   budget snapshot; a closed month with no snapshot shows net income −
   spending instead of a verdict (see budget-snapshots.md).
6. VERDICT SEMANTICS FROZEN at the engine: all lenses are VIEWS over
   month_status/history — the daily email and the canonical monthly math
   change zero. Week/year math composes from existing engine outputs
   (daily allowances, month_status per month); no new judgment rules.
7. Link to email scheduling: the week/month report-card views
   become the future weekly/monthly email bodies — build the render
   functions email-reusable (single-source, like todayview).

Constraints: engine month_status untouched; new views compute via
existing functions per month/day; suite stays green; SPA-only nav change
plus new /api endpoints for week/month/year summaries as needed.
