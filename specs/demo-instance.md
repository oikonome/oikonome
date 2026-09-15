# Demo instance: one command, ten years of synthetic life

## What the seed produces

- **Volume**: ~90–120 txns/month across pharmacy, household/pets,
  salon, kids' activities, and a steady coffee/dining/online cadence.
- **Fees**: quarterly brokerage advisory + annual card fee + stray ATM
  fees (BANK_FEES) so the Fees report has data.
- **Budgets derive from behavior**: food/other/coffee budgets are the
  household's own last-12-months averages ±~5–10%, so the verdict reads
  lived-in.
- **Retirement defaults**: the page's default spend derives
  from the tenant's own last-12-months ledger
  (`retirement.default_spend`) instead of a hardcoded figure; the demo
  config carries a plausible `ss_estimates` schedule.
- **Alerts**: seeded `alerts_log` history (~18 months, native message
  formats) + LIVE conditions — a flagged reimbursement charge and real
  detector proposals (PixelCloud Storage is generated in the ledger but
  never saved as a bill; `recurring.run()` finds it).
- **Demo-mode lockdown** (`demo_mode: true` in tenant config, exposed
  as `demo` on /api/me, enforced by `web/demoguard.py` 403s): all data
  doors (aggregators, imports, uploads, script tokens, SMTP test) and
  account security (password/2FA/passkeys/invites/account-delete)
  refused. UI shows those features disabled in place (discoverable beats
  hidden — `DemoLock` component). Doctor reports
  "disabled — demo instance" rows instead of warnings. This is the
  guard a public demo instance relies on.
- Signup stays instance-level and is NOT blocked here — a public demo
  instance closes it with `OIKONOME_DEMO=1`.
- **Credentials on the login page**: the seed
  stores the printed email/password in tenant config (`demo_login`);
  pre-auth `GET /api/demo-login` serves them ONLY when the whole
  instance is a single user whose tenant is demo-locked (multi-user,
  hosted, or real installs always get `{"demo": false}`, so a planted
  `demo_login` key reveals nothing). The login page pre-fills both
  fields and shows a demo note — no digging through terminal
  scrollback, and a public demo gets its login screen for free.

## Goal

`./oikonome.sh demo` → in a couple of minutes, a working instance with
~10 years of realistic synthetic household data showing every product
surface — verdict, money map, forecast, recurring, budgets, lenses,
cash flow, net worth, retirement — with zero setup steps.

## Decisions

1. **Separate install**: the demo stands up its own folder + next free
   port; the real instance is untouched; delete the folder to discard.
2. **Auto-created login**: `demo@example.com` + a generated password,
   printed with the URL. Zero clicks from command to Today.
3. **One archetype, randomized parameters**: a salaried household —
   income, housing (rent vs mortgage), card mix, merchant amounts,
   dates, balances all drawn from rules per deployment. The RNG seed is
   printed and `--seed N` reproduces a demo exactly.
4. **Wizard fully skipped**: budgets, bills, goals pre-configured;
   first login lands on a complete Today page. The wizard stays
   reachable manually.

## The synthetic household (rules, not fixtures)

- **Income**: biweekly payroll into checking; gross $95–165k drawn once,
  2.5–4% annual raises, ~72% take-home. Ten years of paydays.
- **Housing**: rent or mortgage (coin flip), 24–30% of take-home.
- **Bills**: electric (seasonal), water, internet, mobile, streaming ×2,
  gym, auto insurance (6-mo), life insurance — all saved as confirmed
  recurring bills with full ledger histories behind them.
- **Cards**: two credit cards carrying the variable spend (groceries,
  dining, coffee, gas, online shopping, entertainment, occasional
  medical/travel); monthly statement payments from checking
  (LOAN_PAYMENTS_CREDIT_CARD_PAYMENT); liabilities rows with due dates
  and statement balances so the forecast's card scenarios light up.
- **Savings**: monthly transfer to savings + an Emergency-fund goal
  (target + plan + destination account) so goal tracking demos.
- **Investing**: monthly checking→brokerage sweep of the leftover
  (investment-funding history), 401(k) per-paycheck contributions +
  employer match, a Roth IRA, sporadic crypto buys; balances grown at a
  drawn 6–9%/yr; holdings rows across fictional funds.
- **Taxes**: income_annual rows for every year (wages, SS/Medicare
  earnings, effective tax) so Cash Flow's reported-income and career
  charts populate.
- **Budgets/config**: food/other budgets ≈ the generated averages, one
  custom bucket carve-out (Coffee), budgeted income, primary checking,
  birthdate for Retire — wizard_steps all done.

All merchants are fictional (no real brands). Sign conventions per the
engine: positive = money out.

## Shape

- `server/oikonome/demo.py` — `seed(seed, email, password, years)`;
  ships in the product so any self-hoster can demo.
- CLI: `oikonome demo-seed [--seed N] [--years N]` — refuses on an
  instance that already has users.
- TUI: `./oikonome.sh demo` — copies this checkout to a sibling
  `oikonome-demo/` folder, installs on a free port, runs demo-seed in
  the container, prints URL + credentials + seed.

## Out of scope

Multiple personas; receipts/Amazon-orders synthesis; multi-user
households; regenerating in place (delete the folder and re-run).
