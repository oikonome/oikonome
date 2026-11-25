# Ledger figures: a search's headline, the range buttons, paper checks

The Transactions page states figures in three places beyond the month
tiles: a search's headline, the dates a range button sets, and what a
paper check counts as. Each is decided once, on the server; the web and
the phone print what comes back.

## A search's headline is money out / in / net

**The one function:** `engine.reporting.flows(conn, start=, end=, ids=,
category=)`. Out is spending by `SPEND_WHERE` (card payments, transfers,
loan accounts, linked shadows and business money left out; partial
reimbursements netted), in is income by `_INCOME_WHERE` over the bank
accounts, both without the excluded accounts. These are the predicates
Cash Flow's Spending and Income tabs total, so a date range with no other
filter reads exactly as Cash Flow does for the same days. `month_flows`
(the month tiles) reads its out from the same function.

A search (`GET /api/transactions` with `q`, an account, a category, a
channel, checks or dates) runs it over every row the search matched —
not the page on screen — and returns it as `TxnPage.flow`
(`money_out`, `money_in`, `net`). Under a category filter a split row
counts only its parts filed under that category, as the category
rollups do.

`flow` is null for a budget bucket's rows and a counted category's rows
(their headline is the label's own number, `bucket_amount` /
`amount_sum`) and under the business-only scope (these are household
figures). `amount_sum`, the raw sum, stays in the shape for older
clients; no current client prints it for a plain search.

**Clients:** print "Out $X · In $Y · Net ±$Z" from `flow`, with the
per-visit average and the per-day pace beside it as before. A null
`flow` prints none of the three — never a client-side sum.

**Older client:** keeps printing `amount_sum` as "total" — wrong in the
way it always was, never worse. **Stale tab:** the figures are the
response's; a refetch replaces them whole. **Restore:** nothing is
stored; the figures are read from the ledger each time.

A term some row's own text matches lists those rows only: an account
whose name also matches (a card named after the store) is returned in
`account_hits` with `rows_listed: false`, and the clients offer its rows
as a link ("also: rows on …") instead of folding every purchase on the
card into the store's count, average and figures. A term no row's text
matches names an account, and then its rows are the result
(`rows_listed: true`).

## The range buttons are Cash Flow's windows

**The one function:** `engine.reporting.ledger_ranges(conn, today)`,
which reads `_cashflow_window` for every key: "This month" is the 1st to
today, every other key is complete months ("3m" on Nov 17 is Aug 1 –
Oct 31, "1y" the twelve months that have closed), and a ledger with no
complete month yet reads the month so far, as Cash Flow does. "All" is
no date bound: the ledger lists everything, this month included.

Every `TxnPage` (month browse, search, bucket, counted category; not a
download) carries `ranges`: key → `{start, end}`, inclusive ISO dates,
"" for no bound. A range button sets the date filter to its entry and
is lit when the filter equals it.

**Clients:** read `ranges` from the page they already hold; with no page
yet the buttons wait (disabled), they do not compute dates of their own.

**A merchant's or bill's history uses the same days.** The history page's
timeframe picker (`GET /api/bills/history?window=`) bounds its transaction
list through `bills.merchant_history`, which reads `ledger_ranges` for the
key: "This month" is the 1st to today, every other key complete months
ending with last month, "All" everything — counting months back from
today would put this month's charges in a merchant's "1y" list, miss the
window's first weeks, and not add up to the Spending tab's "1y" figure
that links to it. So on the history page a bill paid this
month shows under "This month" and "All" (the page's default), not under
"3m" or "1y". The page's other parts are unchanged: the lifetime card is
lifetime, and the by-month table still ends with the month in progress.
`merchant_history` keeps `months=` (months back from today) as its own
mode for a caller that passes no timeframe; no client door uses it with a
key. Which rows BELONG to a merchant is not this section's subject: the
history lists every row filed under the name (pending holds, refunds, a
linked account's twin, rows categorised as transfers), while the Spending
figure counts spending (`SPEND_WHERE`) under the punctuation-folded name,
so the two can still differ by those rows. **Older phone / stale tab**:
sends the same keys and gets the new days. **Restore**: nothing stored.

**Older client:** keeps its own dates ("3m" = from the 1st of the month
two months back, open end) — a different window under the same label,
as before. **Stale tab across a month turn:** the buttons use the last
response's dates until the next read (any filter change refetches).
**Restore:** nothing stored.

## A paper check with no category counts as spending

A bank often files a written check as a transfer out (low confidence):
the money left, but nothing says to whom. Left as a transfer it counts
nowhere, and the month reads cheaper than it was.

**The one rule:** `engine.llm_categorize.check_classify`, run in both
categorization passes right after the bank-mechanics pass. A row with a
check number, money out, no person's override, whose working category
is still the aggregator's `TRANSFER_OUT` (category_source plaid or
import) gets `category_primary = "Checks"`, `category_source = "check"`.
It is then spending by every predicate unchanged — no reader learns a
new rule. A person's override (Transfer included) always wins, a row a
rule already moved is left alone, and the aggregator's own label stays in
`category_plaid`. A check whose money landed in another of the
household's own accounts — a deposit of the same size there within ten
days either side, read as a transfer in (or an investment account's
uncategorised cash row) — funds its own IRA or savings: it becomes
`TRANSFER_OUT` with `category_source = "flow"` instead, and a check
already under Checks is moved there once the deposit arrives.

**The payee, asked once:** a row with `needs_payee` true (still under
Checks, and no answer: no "paid to …" note, no payee read off a check
image, no image still being read) shows "Who was check #N to?" on the
web row strip and the phone's detail screen. The answer goes to
`POST /api/transactions/{id}/check-payee`, which writes it as the first
line of the row's note ("paid to …", ahead of any note already there;
a first line the door wrote before is replaced, so an answer can be
corrected, and an answer that would push the note past its cap is
refused whole) —
the hand-authored field every archive carries — and then runs it through
the check-image machinery (`categorize_check_payee`: the payee's merchant
rule, else one classification when a model is configured). An answer
that names money moving (a transfer, a card or loan payment) makes the
check a flow row instead.

**The answer is kept, the category derived from it:** a feed re-sync
re-stamps the row as a transfer, and a re-anchor carries the note but
not the category. So `check_classify`, after filing, runs every check
still under Checks that has an answer back through
`categorize_check_payee` (the rule cache only — no model call per
pass). The door's own categorise also files a check the feed re-stamped
in the moment between its note and its categorise. An answered check
therefore never asks twice and never stays under Checks while its
payee's rule can say more.

**Older client:** never asks; the check still counts as spending under
Checks. **Restore:** the category is derived, so a restored row that
still carries the aggregator's transfer label is classified on the next
categorization pass; the answer is the note, which every archive carries.
