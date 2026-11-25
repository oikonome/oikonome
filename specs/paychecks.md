# Paychecks: which payers count as pay

The Cash Flow income tab and flow picture split what came in into
paychecks, interest & dividends, and everything else. The wages tag and
the household's income bills alone are not enough: an employer the
household has left — no bill any more, its rows imported from statements
with no tag — would print its years of payroll as "Other income", and a
payroll the feed does not tag would read "Other" until somebody added a
bill for it.

A paycheck is any payer with a regular,
payroll-shaped run of deposits, past or present, and the household can
say otherwise for each payer.

## The one function

`engine/paychecks.payer_verdicts(conn, memo=)` decides, for every payer
that ever paid into a bank account, whether it is a paycheck payer. Each
payer is the display merchant of its deposits folded by its letters and
digits (`paychecks.payer_key`, the fold the income-by-source list already
uses), so the key a client sees beside a source row is the key the
verdict is filed under. The rows are the ledger's income
(`reporting._INCOME_WHERE` over the bank accounts, excluded accounts left
out), over the whole history, so an earlier employer is judged on the
years it paid.

A payer is a paycheck payer when, in this order:

1. the household said so (`paycheck_payers` in the settings: `true`
   counts it, `false` never does — the household's word beats every rule
   below, the wages tag included);
2. any of its deposits carries the aggregator's wages tag
   (`INCOME_WAGES`);
3. any of its deposits matches an active income bill's merchant key (the
   series the forecast projects and Bills tracks);
4. its deposits read as payroll (`paychecks.classify_payer`):
   - **payroll-shaped**: a deposit's description names payroll
     (payroll, salary, wages, paycheck, direct deposit, a payroll
     processor's name);
   - **a regular cadence**: a run of at least four deposits (same-day
     deposits merged; a gap of more than 62 days ends a run) where at
     least 70 % of the gaps fall in one band — weekly (5–9 days), twice a
     month or biweekly (12–19), or monthly (26–35);
   - **a check's size**: at least 70 % of that run's deposits within
     0.6 to 1.6 times the run's median deposit.

A deposit is then `paychecks` when its payer is a paycheck payer;
otherwise it is `interest` by its interest/dividend tag or words, and
`other` for the rest (`PayerVerdicts.kind`). A payer the household marked
"never" whose rows are tagged as interest still reads as interest.

The verdict changes which KIND a deposit is, never whether it is income:
the total that came in, the Year lens, the monthly and yearly emails and
the month plan's received half are the ledger's income and read the same
before and after.

## What each surface reads back

- **Cash Flow → Income** (`GET /api/reports/income/window`): `kinds`,
  `prev_kinds`, `paycheck_share` and `kind_changes` are split by the
  verdict. `source_paychecks` runs beside `sources`, in its order:
  `{key, paycheck, override}` — the payer's key, whether its deposits
  count as paychecks, and the household's word (`"paycheck"`, `"never"`
  or null when the rules decide).
- **Cash Flow → flow picture** (`GET /api/reports/cashflow/flow`): the
  income kinds are split by the same verdict. A closed run's cached totals
  are keyed on the verdicts' fingerprint, so a payer turning into a
  paycheck payer (a fourth regular deposit, a household override) moves
  every window that holds its deposits.
- **Today, Budget and Bills & Income**: the month plan's income is
  landed income plus the paychecks still due. Still due needs a schedule,
  which only an income series has (`budget.income_series`); a payer the
  classifier calls a paycheck with no series adds no "still due" — Today
  keeps asking for the paycheck to be added under Bills & Income. The
  landed half already counts every deposit the ledger counts, whatever
  its kind. An income bill whose payer the household marked
  "never" is still income the month expects: its deposits read as
  "other" on Cash Flow, and its occurrences still count toward the plan,
  because the plan counts income, not paychecks. Turning the bill off is
  how the household stops the projection.
- **Year lens, monthly and yearly email**: print the total income, which
  the verdict does not move.

No client decides a kind or a share; both clients print what the server
sends and the toggle writes the household's word only.

## The household's word

`POST /api/income/payer` with `{key, count_as}`: `"paycheck"`, `"never"`
or `"auto"` (forget the word, let the rules decide). Stored in the
settings document as `paycheck_payers: {key: bool}` under the settings
row lock, members and the owner only (a viewer's write is refused at the
door, as every write is). The reply carries the stored word.

Both clients put the control on each income-by-source row: the web a
select ("Paycheck" / "Not a paycheck", with the automatic choice naming
what the rules decided), the phone the same three choices from the row's
paycheck chip. After a save both refetch the income window and the flow
picture; nothing is recomputed on the client.

- **Older phone**: no `source_paychecks`, no control; its kinds are the
  server's, so it prints the new split.
- **Older server**: no `source_paychecks`, so neither client draws the
  control; the door does not exist and is never called.
- **Stale tab**: the save sends a key and a word, not a list, so it
  cannot overwrite another tab's word for a different payer; the refetch
  brings the new split.
- **Restore**: `paycheck_payers` rides the settings document; a restored
  value not shaped `{key: bool}` is dropped entry by entry
  (`restore._CONFIG_SHAPES`). Keys are letters and digits, so a merchant
  rename that changes the display name leaves the old word inert and the
  rules decide for the new name.

## Money back is not income

Two kinds of deposit look like income but are the household's own money
coming back; `reporting._NOT_MONEY_BACK_SQL`, inside `_INCOME_WHERE`
beside `_NOT_OWN_MONEY_SQL`, takes them out in the one place every income
figure reads:

- **a card payment coming back**: a deposit stamped as a credit-card
  payment (a returned card autopay). The payment it undid never counted
  as spending, so its return is not income. Any other payment that
  bounces — a utility ACH return, a mortgage payment reversed — undid a
  payment that DID count as spending; that return stays income, so the
  month's net reads as if neither row had posted;
- **money back from a brokerage**: a deposit whose description names a
  brokerage (the investment providers `_NOT_OWN_MONEY_SQL` already knew,
  plus brokerage words and further broker names).

A deposit tagged as wages, or whose description names payroll, is pay
whoever sends it (an employer can share a broker's name) and is never
taken out. Refunds from a shop stay income.

A bounced payment that counted as spending and its return both stay in
the month, so the net is right without pairing them; linking the return
to the payment as money back takes both out, as for any reimbursement.
