# Which answer leads Retirement

The Retirement page runs two models over the same plan: a flat real
return every year ("steady markets") and the replay of every return
sequence since 1928 ("past markets"). They disagree — a flat 4% real
return is gloomier than the history it is meant to summarise for a
stock-heavy portfolio — so a page led by the flat model would say "not by
72" above a table whose history column reads 99% at 70.

## The verdict is the history replay

ONE server function decides the headline: `retirement._verdict`, called
inside `retirement.project` and sent as `verdict`.

- `verdict.age` is the earliest retire age whose history replay lasts to
  the plan's end in at least `HIST_LEAD_PCT` (90%) of past sequences —
  "lasts in 9 of 10 past markets at 65". `in_ten` is the share in tenths,
  rounded down (`ok × 10 ÷ n`), so the sentence never claims more than
  the replay found; `ok` / `n` / `pct` are the exact counts.
- No age reaches it: `age` is null, `at_age` is the last age tried with
  its share, and `catch_up` lists the extra monthly saving (rounded up to
  $10) that would make the customary age and the last age reach it.
- `tone` is `good` or `bad` and is the hero's colour; every row carries
  `hist_lasts` (its replay reaches the threshold), the colour of the
  every-age table's verdict column.
- `steady_age` is the flat-return model's earliest age (`earliest`, kept
  for older clients) — shown second, as "steady markets", with its own
  catch-up (`catch_up`, unchanged).

Web and phone both read `verdict` (there is no retirement mail); no client
compares a share with a threshold.

## The chart is the plan the verdict describes

`growth_path` is the median, year by year, of the balance across every
past sequence when retiring at the verdict's age (the customary age when
none works), from this month to the plan's end age — saving until then,
drawing the spend after. Its points are `YYYY-MM` from the current month.
`growth_path_retire_age` names the age it retires at. `grow_to_67` stays
the steady model's keep-working balance at 67, as its tile says.

## Saving starts from the ledger

Without it, a plan nobody has told about saving would project "saving
nothing" while payroll contributions land in a 401(k) every month.

`retirement.ledger_saving` reads the last 12 months of deposits into the
accounts the projection treats as retirement money (tax-deferred and
Roth, the same classification `current_buckets` uses): workplace-plan
contribution rows, and money arriving in such an account from outside
(`TRANSFER_IN_INVESTMENT_AND_RETIREMENT_FUNDS`, `TRANSFER_IN_DEPOSIT`,
`TRANSFER_IN_ACCOUNT_TRANSFER`). The monthly average divides by 12, or by
the months the accounts' ledger covers when that is shorter (never less
than three). It is snapped to the page's $50 grid. Each deposit is counted in
the bucket of the account it landed in (`td_monthly`, `roth_monthly`;
`monthly` is their sum) — see "Saving is split by tax treatment".

- With no what-if and no saved answer, that split is the plan's saving
  and `inputs.saving_basis` is `ledger`, worded "from your last 12
  months". A typed figure (what-if or saved) replaces it.
- `inputs.saving_source` keeps its three values for older clients: a
  ledger figure reads as `saved` there, so an older build shows the
  figure it projects rather than "saving not set".
- `inputs.saving_mo` is the monthly saving the projection counts; the
  catch-up sentence says it adds to that figure, or to nothing.

### What counts as saving, and over how long

ONE function decides the figure: `retirement.ledger_saving`.

- A deposit that is the household's own money moving between accounts
  the projection already counts AS SAVING — an IRA converted to a Roth,
  an old 401(k) rolled into an IRA — is not new saving: the balance it
  left was already in the retirement money. Such a deposit is paired as
  `forecast_score.own_transfer_ids` pairs (one to one, the opposite
  amount, within its days, the nearest day first), and the deposit's OWN
  partner leg decides (`retirement._own_transfer_partners` keeps the
  partner the pairing chose): it is dropped when that leg is on another
  tax-deferred or Roth account (`bucket_of`). A partner on checking,
  savings or a brokerage stays: a deposit into a brokerage is never
  counted as saving, so money routed checking → brokerage → Roth IRA is
  counted once, at the Roth. An unrelated transfer of the same amount out
  of some other account the same week is not the deposit's partner and
  cancels nothing. A deposit whose name says rollover or conversion is
  dropped even when the account it came from is not linked.
- The average divides by the TIME the accounts' ledger covers, in days
  (`days × 12 ÷ 365` months, at most 12, never fewer than three), as
  `default_spend` annualizes the spend: a partial first month and the
  month in progress are not whole months of saving. `months` in the
  result is that figure to one decimal, always a float (the three-month
  floor included).
- Known limits, left as they are: an unlinked rollover or transfer of
  assets whose name says neither rollover nor conversion counts as
  saving; a single lump-sum contribution is averaged over the months
  covered like any other deposit, so a young ledger with one annual
  contribution reads high.

How an existing household moves: one with an IRA→Roth conversion or a
linked rollover in the last year sees its ledger saving fall by that
amount ÷ 12; one that funds a retirement account from its brokerage sees
those deposits counted again (the previous rule dropped them, and they
were counted nowhere); one whose plan ledger is younger than a year sees it rise
(up to a month's worth). A typed or saved saving is untouched.

### Which bucket an account is in

ONE function decides it: `retirement.bucket_of`.

- A bank account (`depository`) is cash whatever else its name says:
  "Traditional Savings" is cash. A health savings subtype makes it
  tax-deferred, and so does the word IRA in its name (a credit union's
  "IRA Share" or "IRA CD" is typed as a bank account) — Roth when the
  name also has the word Roth. The word is read as a product writes it,
  in capitals too, never as a person called Ira
  (`retirement._names_an_ira`, word by word): a checking subtype is
  never an IRA; the possessive ("Ira's", "IRA'S") never counts; checking,
  draft, primary or joint anywhere makes it cash; a retirement word
  (Roth, traditional, SEP, SIMPLE, rollover) decides; otherwise IRA
  counts as the whole name ("IRA"), as the first word followed by a
  deposit-product word (savings, share, money, market, CD, certificate,
  term: "IRA SAVINGS", "IRA Money Market") or as the last word after one
  ("Share Certificate IRA"). "IRA SAMPLE SAVINGS" is cash: the word after
  IRA is not a product word. A person's account named "Ira CD" reads as
  an IRA — accepted: a CD named only for its owner is rare.
- For other accounts the name is read as a fallback: "roth" as a word
  (not "Brothers") makes it Roth, "traditional" tax-deferred.
- Tax-deferred also takes the annuity subtypes (fixed, variable, other —
  gains come out as ordinary income), Plaid's literal "health
  reimbursement arrangement", and the Canadian and UK pre-tax plans
  (RRSP, RRIF, LIRA, LIF, LRIF, LRSP, PRIF, RLIF, SIPP). A TFSA is Roth
  (withdrawals are tax-free). An RDSP is left out, like an RESP.

How an existing household moves: a bank account with "traditional" or
"roth" but no "IRA" in its name moves to cash (grows at the cash rate,
no tax, no RMD); one named as an IRA (share, CD, savings) is retirement
money, tax-deferred or Roth, and its deposits count as saving; an annuity moves from taxable to tax-deferred, so its spendable
value falls by the ordinary-rate haircut.

## Saving is split by tax treatment

One employer-plan saving figure projected as tax-deferred would tax a
household's Roth saving as ordinary income at withdrawal and force it out
by RMDs, under-projecting a Roth-only saver.

ONE function decides the split: `retirement.saving_plan(what_if, answers,
found)`, returning `td_mo`, `roth_mo`, `taxable_mo` (each clamped to
$0–$10M and on the $50 grid), `employer_mo` (the older clients' figure,
below), `basis`, `source`, `parts` and `hint`.

- The base is the saved answers when any of `employer_mo`, `roth_mo`,
  `taxable_mo` is a marked answer — a saving answer that is missing from
  that block is $0 (an old answer with no Roth figure counts no Roth,
  never one guessed from the ledger). Else it is the ledger split: the
  total is snapped to the grid first, the Roth part snapped and capped at
  the total, and tax-deferred is the rest — so the parts always sum to
  the figure the page shows, and a household with no Roth deposits gets
  exactly the figure it got before. Else nothing.
- Each what-if replaces ITS OWN field only (`employer_mo` is the
  tax-deferred figure, `roth_mo` the Roth one, `taxable_mo` the taxable
  one); the other fields keep the base's figure. Any what-if makes the
  basis `what_if`. The one exception is the older client's query below:
  while the Roth figure is the ledger's, an `employer_mo` sent WITHOUT
  `roth_mo` is the whole non-taxable saving, the ledger's Roth first
  (Roth = the lesser of the ledger's Roth and that figure, tax-deferred
  the rest).
- `employer_mo` never changed meaning for an older client: it is "the
  saving that is not taxable-account saving". In `inputs` it is
  `td_mo + roth_mo` while the Roth figure is the ledger's (no Roth answer
  saved, none asked), else `td_mo`. The split is read from the new
  fields `inputs.td_mo` (tax-deferred) and `inputs.roth_mo` (Roth). In
  a saved answer `employer_mo` is the tax-deferred figure: a total an
  older client saved there is projected as tax-deferred, as it always
  was. A current client binds its tax-deferred box to `td_mo` and always
  sends `roth_mo` / `ret_wizard_roth_mo` where the server offers it (a
  blank Roth box is $0), so "answered $0" and "never asked" stay apart.
- The projection adds `roth_annual` (`roth_mo × 12`) to the Roth bucket
  every working year from `resume_age`, beside `td_annual`, in
  `simulate`, `_max_spend`, `_growth_path`, the history replay (through
  the shared `kw`) and the `project` memo key (a key without it would
  serve one split's answer to another). The catch-up's extra saving stays
  taxable. Zero Roth saving leaves every figure bit-identical.
- `inputs.roth_mo` is the Roth figure; `inputs.saving_mo` /
  `saving_yr` are all three parts. `inputs.saving_parts` lists each part
  above $0 as `{kind, monthly, text}`, worded on the server:
  "$X/mo into tax-deferred accounts — taxed when you take it out",
  "$X/mo into Roth accounts — not taxed when you take it out",
  "$X/mo into taxable accounts — gains taxed when you sell". Their
  `monthly` figures sum to `saving_mo`.
- `inputs.saving_hint`: when the base is the saved answers, the saved
  block has no Roth answer, and the ledger shows Roth deposits, one line
  says so ("Your saved $T/mo includes about $R/mo that your last 12
  months show going into Roth accounts — save it to count it as Roth.").
  The saved tax-deferred figure may already hold that Roth saving (an
  older client saved the total there), so the offer is a split of the
  same total, never an addition: R is the ledger's Roth capped at T.
  Null otherwise, and while a saving what-if is asked.
- The boxes' seeds: `inputs.td_mo` / `inputs.roth_mo` are what a current
  client's tax-deferred and Roth boxes open on (adjust panel, the
  walkthrough's saving step). They are the figures projected, except
  while `saving_hint` is set: then they are T − R and R, summing to the
  saved total, while the projection keeps what is saved (T as
  tax-deferred) until the person saves, and `saving_parts` says what is
  projected. Saving the seeded boxes stores T − R and R, the same total.
  The walkthrough opens both saving boxes on these seeds whenever the
  server offers `roth_mo` and no Roth answer is saved (both clients'
  `retWizardPrefill`); `employer_mo` stays the saved total for an older
  client.

Web and phone print `saving_parts` (joined by " · ") under the
assumptions line and `saving_hint` when it is set; the adjust panel, the
saving ask and the walkthrough's saving step each gain a "Roth $/mo" box.

### Stored answer

`roth_mo` is a retirement answer like the others (`RET_WIZARD_RANGES`,
$0–$10M, labelled "Roth retirement $/mo"). The settings door refuses an
unknown answer key and an older self-hosted server would refuse a whole
save over it, so a client sends it as the top-level `ret_wizard_roth_mo`,
as `ret_wizard_ssage` rides; null clears it. A restore keeps it through
the answers' cleaner (`_ret_answers`, `_CONFIG_SHAPES`); an older
server's restore drops it — its projection was all tax-deferred anyway.

### Older client, stale tab, restore

- An older client (a released phone, a stale web tab) never sends
  `roth_mo` and reads only `employer_mo`, `taxable_mo`, `saving_mo` and
  `saving_yr`.
  - Ledger basis with Roth deposits: its employer box shows
    `employer_mo` = tax-deferred + the ledger's Roth, the figure it was
    always given. Its untouched Recalculate sends that figure back and
    the ledger's Roth stays inside it, so nothing moves; a typed figure
    is that whole saving. Its walkthrough confirms the box as a saved
    `employer_mo` with no Roth answer: from then on the plan counts that
    total as tax-deferred — the same total, exactly as before the split —
    and the page offers the split (`saving_hint`) to a current client.
  - A saved Roth answer (from a current device): `employer_mo` is the
    tax-deferred figure alone; its Recalculate and its walkthrough send
    that back and leave the stored Roth answer as it is, so the total
    holds and nothing is counted twice.
  - `saving_mo` / `saving_yr` are all three parts, the total it always
    showed.
- A stale tab's what-if is answered by the same function; nothing is
  stored by it.
- A newer phone on an older server reads no `td_mo`, `roth_mo`,
  `saving_parts` or `saving_hint` and shows no Roth box, parts line or
  hint; its tax-deferred box falls back to `employer_mo` (the marked
  older-server path).
- A stale tab's what-if is answered by the same function; nothing is
  stored by it.
- A newer phone on an older server reads no `roth_mo`, `saving_parts` or
  `saving_hint` and shows no Roth box, parts line or hint.

How an existing household moves: one with a saved saving answer, or with
no Roth deposits in its ledger, moves not at all (a what-if of only the
taxable figure now keeps the ledger's tax-deferred figure instead of
dropping it to $0). One whose saving comes from the ledger and whose
ledger shows Roth deposits sees those deposits move from tax-deferred to
Roth: the same total saving, no longer haircut by the ordinary-income
rate at withdrawal nor forced out by RMDs, so its spendable figures rise.

## Cash is said as it is counted

`buckets.cash` is cash after card balances are netted out (the draw
order nets them from cash first). `buckets.cards_from_cash` (and
`cards_from_taxable`, `_td`, `_roth`) say how much was netted from each,
so the page writes "after $X of card balances". "You have today" is the
financial accounts net of cards; the page says property is not counted.

## Older client, stale tab, restore

- Every field is added; `earliest`, `catch_up`, `rows[].feasible` keep
  their meaning, so an older build still shows the steady model it was
  built for, with the ledger saving in its projection.
- A stale tab's what-if query is answered by the same function; nothing
  is stored.
- Nothing here is stored: a restore brings back the ledger and the saved
  answers, and the page reads both at request time.
