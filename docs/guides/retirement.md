# Retirement

The Retirement page answers one question: **when can you stop working,
and how much can you spend without running out?** It is a deterministic
projection — no random simulation — run against your real balances.

## Where the numbers start

The defaults come from your own data, not placeholders:

- **Current age** is computed from your birthdate. The first-run
  **Retirement wizard** (it opens by itself on your first visit; later,
  **re-run wizard** on the page hero or Settings → wizards brings it
  back) is where the birthdate is set and saved — even **Skip** keeps
  one you have already typed. No birthdate set, age defaults to 45.
- **Target spend $/yr** defaults to your household's actual
  last-12-months spending (the product's spend definition — see the
  concepts guide), rounded to the nearest $1,000. Too little history
  to be meaningful and a generic fallback is used instead.
- **SS at claim age** comes from Social Security estimates stored with
  your settings — the monthly amounts from your SSA statement, keyed by
  claim age 62–70. There is no screen for entering them: they are
  loaded through the settings JSON API (`ss_estimates`, an object of
  `{age: monthly $}`), and nothing in the app computes a benefit for
  you. The page picks the estimate for your exact chosen claim age; if
  no estimate exists for that age (or none are loaded), Social Security
  is $0 — there is no interpolation between entered ages. The hero's
  **Assumptions** line tells you which case you are in: it reads
  **SS at 67 ($X/mo)** when an estimate was found and **(no SSA
  statement)** when none was.

Every field on the page is a **what-if override**. The form sits behind
**adjust ▾** on the Assumptions line — spend, return, inflation, plan-to
age, SS claim age, stock %, employer and taxable saving per month, and
the age to resume saving — and **Recalculate** reruns the model with
your figures. Nothing you type there is saved; the wizard's own answers
(spend, saving, market assumptions) likewise only pre-fill this form for
the visit, with the birthdate the one thing it stores.

## The projection model

Everything is modeled in **today's dollars**: the growth rate used is
the real return, (1 + return) ÷ (1 + inflation) − 1, and spending is a
constant real amount per year.

Your accounts are grouped into four tax buckets by type and name:
**tax-deferred** (401a/401k/403b/457b, non-Roth IRAs including SEP and
SIMPLE, Keogh, SARSEP, profit-sharing and Thrift Savings plans,
pensions, anything the bank labels plainly "retirement", and
"traditional" in the name), **Roth**, **taxable** (brokerage and
crypto), and **cash** (bank accounts). Duplicate-source shadow accounts are excluded so nothing
counts twice (see the accounts guide). Credit-card debt is netted out
of the buckets up front, in the same order withdrawals draw.

Each simulated year:

- Investment buckets grow at the real return. **Cash keeps pace with
  inflation** (0% real). There is no control for a different cash yield
  on the page yet; the projection API accepts one (a nominal percentage)
  for scripts and integrations, and pricing cash at 0% nominal shows why
  the default is the kinder assumption — at 0% nominal,
  $100k is worth about $74k of today's dollars after ten years of 3%
  inflation.
- **RMDs** start at the age SECURE 2.0 sets for your birth year (72 if
  born 1950 or earlier, 73 if born 1951–1959, 75 from 1960; without a
  birthdate, 75) — working or retired, since an IRA's distribution cannot be deferred by employment.
  The IRS Uniform Lifetime Table forces a tax-deferred withdrawal each
  year, taxed as ordinary income; while you work it is reinvested
  taxable, and in retirement anything beyond that year's spending is.
- Before retirement, from the **resume saving at age** you set, the
  employer amount lands in tax-deferred and the taxable amount in
  brokerage.
- After retirement, income arrives first — Social Security from your
  claim age (taxed as 85% ordinary income) and the year's RMD.
- The rest of the year's spend is withdrawn **tax-smart**: cash →
  taxable → tax-deferred → Roth. Taxable withdrawals are haircut by an
  effective capital-gains rate on an assumed half-gains mix;
  tax-deferred by an effective ordinary rate; cash and Roth spend
  dollar-for-dollar. All figures shown are after tax.

A retirement age is **feasible** if the money lasts to your plan-to
age. **Max sustainable spend** is found by binary search: the largest
constant real spend that just barely survives. The per-age table runs
from your current age to 72, and the earliest feasible row is the
headline answer.

## Replayed against history

The average-return projection hides the thing that actually breaks
retirements: a crash in the first years. Two checks confront it.

**Historical replay** reruns your exact plan — same buckets, draw
order, taxes, and RMDs — against **every contiguous sequence of real
annual returns since 1928** (S&P 500 with dividends and 10-year
Treasuries, inflation-adjusted, blended by your stock %; sequences wrap
around at the end of the record). The result is a plain fraction:
survived N of 98 historical start years. The card also computes the
spend level with **90% historical success** and the spend that
**survived all of history**. The per-age table carries the same success
% for each retirement age. Deterministic — no Monte Carlo.

**Stress scenarios** override the real return for the first years *of
retirement*: a −35% crash in year one, a crash with slow recovery
(−20%, −10%, 0%), and a lost decade (0% real for 10 years). The table
shows the earliest feasible age and max spend under each, and the
"crash-proof spend" KPI is the worst of them.

## Gotchas

- The tax treatment is **effective flat rates**, not a bracket engine —
  no IRMAA, no Roth conversions, no state tax. Good for direction, not
  for tax planning.
- Spending is constant in real terms for life; real retirements spend
  unevenly. There is no modeling of one-off costs.
- Bucket classification is a name/subtype heuristic — a Roth account
  without "roth" in its subtype or name lands in the wrong bucket.
  Check the "By tax treatment" table against reality.
- Social Security uses only the statement amounts you entered; the app
  never computes a benefit from earnings.
- Historical sequences wrap: a start year late in the record splices
  the oldest years on after the newest. Pre-1928 conditions and
  futures unlike the past are, by construction, not represented.
- Cash only keeps pace with inflation (0% real) — it never earns the
  portfolio's return, so a large cash bucket drags the projection exactly
  as it drags real portfolios. A cash-yield control is not on the page
  yet; the projection API takes one (a nominal yield, converted to real
  against your inflation figure) for scripts.
