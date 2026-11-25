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
- **Target spend $/yr** is the spend you saved in the wizard; without
  one it is your household's actual last-12-months spending (the
  product's spend definition — see the concepts guide), rounded to the
  nearest $1,000. With too little history for that, the page asks what
  you plan to spend a year and projects nothing until you answer — there
  is no assumed spend.
- **Saving per month** is the saving you saved in the wizard. With none
  saved, it is what your ledger shows going into your retirement accounts
  (401(k), 403(b), 457(b), IRA, Roth — workplace-plan contributions and
  deposits) over the last 12 months, as a monthly average, rounded to
  $50 and marked **from your last 12 months**. Money moved from one of
  your retirement accounts into another (a Roth conversion, a rollover)
  is not counted as saving; money moved in from checking, savings or a
  brokerage is. Accounts with less than a year of history are averaged
  over the time they cover. With neither, the projection counts no new
  saving and the hero says **saving not set — add it**, which opens a
  box that saves the answer.
- Saving is split by how it is taxed: **tax-deferred** (the employer
  box: 401(k), 403(b), a traditional IRA), **Roth** (Roth IRA, Roth
  401(k)) and **taxable** (a brokerage). The ledger figure is split by
  the account each deposit landed in. A line under the Assumptions line
  lists each part, for example "$600/mo into tax-deferred accounts —
  taxed when you take it out · $250/mo into Roth accounts — not taxed
  when you take it out". If your saved answer has no Roth figure, no
  Roth saving is counted; when your last 12 months show Roth deposits,
  the page says so and points you to the walkthrough's saving step.
- **SS at claim age** comes from the Social Security estimates you enter
  from your SSA statement: open **adjust ▾** on the Assumptions line (or
  click **no SSA statement** there) and fill **Social Security $/mo by
  claim age**, one monthly amount for each age from 62 to 70 you have a
  figure for, then **Save** (the phone app's Retirement screen has the
  same fields under its own **adjust ▾**; a viewer sees the page but not
  the fields). Nothing in the app computes a benefit for
  you. The page uses the estimate for your chosen claim age; an age
  between two entered ages takes the straight line between them, an age
  outside them is the nearest one adjusted the way SSA adjusts a claim
  (smaller for each month earlier, larger for each month later up to
  70), and with none entered Social
  Security is $0. The hero's **Assumptions** line tells you which case
  you are in: **SS at 67 ($X/mo)** with the figure for that age, and
  **(no SSA statement)** when you have none at all.

Every other field on the page is a **what-if override**. The form sits
behind **adjust ▾** on the Assumptions line — spend, return, inflation, plan-to
age, SS claim age, stock %, employer, Roth and taxable saving per month, and
the age to resume saving — and **Recalculate** reruns the model with
your figures. Nothing else you type in that form is saved; the
projection starts from your saved wizard answers each visit. The SS
claim age is remembered with those answers, and **Save** keeps the
Social Security amounts below it for the household.

The wizard itself saves each step as you answer it, birthdate included,
so closing the page, reloading it or opening it on another device picks
up at the first question you have not answered, with your earlier
answers filled in. Return and inflation may be negative: each takes any
figure from −20% to 30%, the same range the projection uses. An answer
outside its range is refused on its own step, which says the range and
waits for a fix instead of moving on.

## The projection model

Everything is modeled in **today's dollars**: the growth rate used is
the real return, (1 + return) ÷ (1 + inflation) − 1, and spending is a
constant real amount per year.

Your accounts are grouped into four tax buckets by type and name:
**tax-deferred** (401a/401k/403b/457b, non-Roth IRAs including SEP and
SIMPLE, Keogh, SARSEP, profit-sharing and Thrift Savings plans,
pensions, annuities, health savings accounts, RRSP/RRIF and similar
Canadian and UK pre-tax plans, anything the bank labels plainly
"retirement", and "traditional" in an investment account's name),
**Roth** (including a TFSA), **taxable** (brokerage and crypto), and
**cash** (bank accounts — except one whose name says IRA, such as a
credit union's IRA share or IRA CD, which is tax-deferred, or Roth when
the name also says Roth). Duplicate-source shadow accounts are excluded so nothing
counts twice (see the accounts guide). Credit-card debt — the cards the
Debt page lists, at its rates, without the accounts excluded there — is
netted out of the buckets up front, in the same order withdrawals draw.

Each simulated year:

- Investment buckets grow at the real return. **Cash keeps pace with
  inflation** (0% real) unless you set a **cash yield** under
  *adjust ▾* — a nominal percentage, the same number a savings account
  advertises; leave it blank for the default. Pricing cash at 0% nominal
  shows why the default is the kinder assumption — at 0% nominal, $100k
  is worth about $74k of today's dollars after ten years of 3% inflation.
  (Scripts pass it as `cash` on the projection API.)
- **RMDs** start at the age SECURE 2.0 sets for your birth year (72 if
  born 1950 or earlier, 73 if born 1951–1959, 75 from 1960; without a
  birthdate, 75) — working or retired, since an IRA's distribution cannot be deferred by employment.
  The IRS Uniform Lifetime Table forces a tax-deferred withdrawal each
  year, taxed as ordinary income; while you work it is reinvested
  taxable, and in retirement anything beyond that year's spending is.
- Before retirement, from the **resume at** age you set under *adjust ▾*, the
  employer amount lands in tax-deferred, the Roth amount in Roth and the
  taxable amount in brokerage.
- After retirement, income arrives first — Social Security from your
  claim age (taxed as 85% ordinary income) and the year's RMD.
- The rest of the year's spend is withdrawn **tax-smart**: cash →
  taxable → tax-deferred → Roth. Taxable withdrawals are haircut by an
  effective capital-gains rate on an assumed half-gains mix;
  tax-deferred by an effective ordinary rate; cash and Roth spend
  dollar-for-dollar. All figures shown are after tax.

The headline answer is the **history replay** (below): the earliest age
whose plan lasts to your plan-to age in at least 9 of 10 past markets —
"age 62 · lasts in 9 of 10 past markets". The same plan at your return
assumption every single year is shown under it as **steady markets**,
with its own earliest age. The two can disagree: a flat return is a
gloomier summary of history than history itself for a stock-heavy plan.

**Max sustainable spend** is found by binary search: the largest
constant real spend that just barely survives steady markets. The
per-age table runs from your current age to 72; its **Past markets**
column (green from 9 in 10) carries the verdict, **Steady markets** says
whether the steady plan lasts or when it runs out, and its **Max
spend/yr** is green where it covers your target spend.

When no age in the table lasts in 9 of 10 past markets, the headline says
**not by 72** and, under it, the extra amount to save each month —
starting now, on top of the saving already counted, into taxable savings
— that makes retiring at 67 (and at 72) last in 9 of 10. Each figure is rounded up to the next $10, so it is
one that really lasts. If you are already past an age, or no monthly
amount is enough, that age is left out.

The **Projected portfolio** chart is that plan: saving until the
headline's age, then drawing your spend, to your plan-to age — the
middle (median) of every past market, year by year, in today's dollars.

**You have today** is your accounts net of card balances (the Cash row
says how much it is net of); your home and other property are not
counted.

## Replayed against history

The average-return projection hides the thing that actually breaks
retirements: a crash in the first years. Two checks confront it.

**Historical replay** reruns your exact plan — same buckets, draw
order, taxes, and RMDs — against **every contiguous sequence of real
annual returns since 1928** (S&P 500 with dividends and 10-year
Treasuries, inflation-adjusted, blended by your stock %; sequences wrap
around at the end of the record). The result is a plain fraction:
"survived N of 98 historical sequences" — one per start year since 1928. The card also computes the
spend level with **90% historical success** and **the most you could
spend and survive every year since 1928**. The per-age table carries the same success
% for each retirement age. Deterministic — no Monte Carlo.

The **Stress test** scenarios override the real return for the first years *of
retirement*: a −35% crash in year one, a crash with slow recovery
(−20%, −10%, 0%), and a lost decade (0% real for 10 years). The table
shows the earliest feasible age and max spend under each (red where it
falls short of your target spend), and the hero's
**the most you could spend and survive the worst stress scenario** is the
lowest of them.

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
- Cash never earns the portfolio's return, so a large cash bucket drags
  the projection exactly as it drags real portfolios. Left alone it only
  keeps pace with inflation (0% real); the cash-yield box under
  **adjust** sets a nominal yield, converted to real against your
  inflation figure.
