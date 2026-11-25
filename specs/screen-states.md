# Screen states: loading, failed, empty, offline

Every screen on the web and the phone fetches its data through TanStack
Query. This note sets what a person sees on each screen while its data
is loading, when a request fails, when there is nothing to show, and (on
the phone) when there is no connection: one rule for each of those
states, the shared pieces that carry them on both clients, and how each
screen applies them.

## The one rule

**A figure, a count or an empty message may only come from a query that
succeeded.** A request that failed, or has not answered yet, is never
read as "nothing". `$0`, "0 transactions", "No bills yet", "nothing
needs attention" and "all checks pass" are statements about the
household; they are wrong when the truth is "we don't know".

Everything else in this note follows from that rule, plus "mobile
matches web": the same state on the same screen says the same words on
both clients.

## The design

### States, and what each one looks like

For every query a screen renders from:

| State | Meaning | What is seen |
|---|---|---|
| Loading | first fetch, no data yet | the page's skeleton for a page, "Loading…" for a section |
| Ready | data, last fetch succeeded | the data |
| Stale | data, last fetch failed | the data, plus one stale line at the top |
| Failed | no data, fetch failed | one failed line plus Try again, **in place of** the section |
| Empty | succeeded, nothing in it | one empty line plus at most one action |

Rules:

- **A section that depends on a failed query shows the failed line in
  place, never its empty text and never its figures.** This is
  `listPhase` applied to every list and every figure, not only three
  screens.
- **A figure whose query is not ready prints the unknown glyph**, never
  `$0`. A count prints nothing, rather than 0.
- **A form never saves until it has been seeded from a successful
  read.** Its Save button is disabled with "Loading…" until then, and a
  whole-list save with no basis is refused client-side rather than sent
  unguarded.
- **A value computed from other queries waits for all of them** (the
  planner's Bills, the wizard's savings). If any failed, the computed
  box shows the failed line, and Save is disabled.
- **The control that changed the request stays on screen** when that
  request fails: steppers, range pickers, search boxes and tabs sit
  outside the gated section.
- **A refetch never blanks data already shown**: a new filter or period
  keeps the previous rows dimmed (the phone's month lens is the model)
  until the new ones arrive. If the new request fails, the old rows go,
  and the failed line names the new filter.
- **A 401 on any read signs out**, on the web as on the phone.

### Words

One pure function on each client, kept as a byte-identical twin block
(`webapp/src/api/states.ts`, `mobile/src/lib/states.ts`) and pinned by a
case table run against both in the server suite, the way the money
twins are:

`failLine(error, what)` → `{ text, retry }`

| Cause | text (what = "bills") | Try again |
|---|---|---|
| no connection | "Couldn't load bills — you're offline." (phone) / "Couldn't load bills — can't reach the server." (web) | yes |
| 5xx | "Couldn't load bills — something went wrong on the server." | yes |
| 503 busy / restart | the existing `busyText` / `restartingText` | yes |
| 403 | the server's sentence, else "Your role doesn't include bills." | no |
| 409 | the server's sentence | yes |
| 429 | "Too many requests — wait a minute, then try again." | yes, never automatic |
| 401 | not shown: signs out | — |
| other 4xx | the server's sentence (`errText`) | no |

`staleLine(error, updatedAt)`:

- no connection: "Offline — showing what was here at 9:41."
- any other cause: "Couldn't refresh — showing what was here at 9:41."

The time is a clock time for today, otherwise a date ("Mar 3").

Empty lines are written per screen. Each is one short line, plus at most
one action. A filtered list that comes back empty always says "Nothing
matches these filters." with no action: the way out is the filter just
used. "Connect a bank" is offered only when the household has no
accounts at all.

### A read that never answers

A request the server or the network never answers would otherwise wait
as long as the platform lets it, leaving the screen on "Loading…" with
no way out but a reload. The rule:

- **A read (GET or HEAD) with no response after 30 seconds is
  abandoned** and fails exactly as a request that could not reach the
  server does: a `TypeError` with no status (marked `timedOut`), so
  `failLine` says "can't reach the server." / "you're offline.",
  `staleLine` keeps the last figures greyed, and the phone's offline
  gate counts it as no answer. It is never a 401 (nothing signs out)
  and never an abort (the web reads an abort as the account switch).
- **A write (POST, PUT, PATCH, DELETE) is never timed out by the
  client.** Its outcome would be unknown, and a second attempt could
  double it; uploads and the assistant's answers legitimately run long.
- **The limit is time to the response's headers.** Once the server has
  started to answer the clock stops, so a long body or a stream is not
  cut off, and no path needs an exemption for streaming.
- **A caller's own signal keeps working.** The limit and the outer
  signal (the caller's, else the account epoch's) are combined by hand,
  since `AbortSignal.any` is not on every engine the clients run on.
  The outer signal cancels the read before the answer and while its
  body is read; the client lets go of it once the body is read
  (`readFinished`).
- **A read abandoned at the limit is asked again once**, not the three
  times a dead connection gets (`retryRead`): 30 s, a 1 s pause, 30 s,
  then the failed line with Try again. Three retries would be two
  silent minutes.

One function decides it, `fetchRead` in the screen-states twin block
(`webapp/src/moneymath.ts`, `mobile/src/lib/pure.ts`), with the limit
`READ_LIMIT_MS` beside it; the case table runs it against both. One
function on each client hands every request to it:

| Client | Function | Covers |
|---|---|---|
| Web | `rawFetch` (`webapp/src/api/client.ts`) | every request the API client makes, the multipart ones included |
| Phone | `fetchStamped` (`mobile/src/lib/connection.ts`) | every request the API client makes, sign-in included |

Left off the limit, by name: the web's `saveFile` (a file the server
builds whole before its first byte is a download the person asked for
and is watching, with its own toast, not a screen left loading). The
phone's file downloads go through the platform's downloader, not fetch.
The few requests outside the API client (the restart polls of the
health door, the phone widget's background refresh, the sign-in page's
look at what the server offers) are not screen reads and are unchanged.

An older client has no limit and waits as before; nothing on the server
changes, and nothing is stored, so a restore and a stale tab behave as
they did.

### Shared pieces (one per client, same props, same words)

| Web (`webapp/src/components`) | Phone (`mobile/src/components`) | Does |
|---|---|---|
| `Loaded` | `Loaded` | `<Loaded q={q} what="bills" skeleton>{(d) => …}</Loaded>`: loading → skeleton or "Loading…", failed → `FailedLine`, ready → children; adds `StaleLine` above when stale. `empty={(d) => …}` + `emptyLine` covers the empty case. |
| `FailedLine` | `FailedLine` | `failLine` text + "Try again" (calls `refetch`) when `retry`. Replaces the raw-text notes and the phone's "Couldn't reach the server…" lines. |
| `StaleLine` | `StaleLine` (replaces `StaleBanner`) | `staleLine` text, one line, warn tone. |
| `EmptyState` (exists) | `EmptyState` (new twin) | one line + optional action. The web title + text pair collapses to one line where the text only repeats the title. |
| `money(null)` → `·` (exists) | `money(null)` → `·` | the unknown glyph on both (see Choices made, below). |
| `knownOr(q, pick)` | `knownOr(q, pick)` | returns the picked value only when `q.isSuccess`, else `null`; the replacement for `q.data?.x ?? 0`. |

Query-client changes:

- Web `main.tsx`: a `QueryCache` `onError` fires `oiko-auth-lost` for an
  `AuthError` that is not a re-auth reply, the same test the mutation
  cache uses.
- Phone `lib/query.ts`: the web's retry rule (network, 5xx and 408 only,
  up to three times with the same backoff). Offline is told from the
  app's own requests, with no native module, and the reads on screen
  that failed meanwhile are asked again once an answer comes back
  (Choices made, below).
- Phone offline copy: the encrypted persisted cache keeps the reads
  `keptOffline` names (Choices made, below).

Guards that keep it fixed:

- `test_no_raw_error_text_on_screen.py` reads every client source and
  fails on a raw error rendering (see "Every remaining error line",
  below).
- The twin case table for `failLine` / `staleLine`.

## How each screen applies the rules

Each part applies on the web and the phone alike. They are ordered by
harm: a wrong value that gets saved, then a wrong figure, a wrong
"nothing", a blank or stuck screen, raw error text, and missing empty
states.

- **Forms never save unseeded values.** `saveWaits(seeded, readFailed)` (twin block, case table)
   gives every held Save its reason: "Loading…", or "Couldn't load
   what's saved, so nothing can be saved over it yet."; `saveWholeLists`
   throws `SettingsUnreadError` for a whole-list body with no settings
   view under it, before anything is sent (a view without versions is an
   older server and still saves). The server keeps accepting an
   unversioned whole-list save: refusing one would change the contract
   that a client sending no versions replaces the list, which API
   callers and the suite rely on.
- **Budget figures wait for their inputs.** `plannerWaits(bills, history)` over `inputPhase(q)` (twin
   block, case table) decides it on both clients; `UNKNOWN_FIGURE` ("·",
   one constant per client — the web's `money(null)` prints it too) marks
   Bills and Savings while unknown, the balance graphic and the excess
   line are left out, and Save is held with the reason ("Loading…",
   "Couldn't load your bills, so the plan can't be worked out or saved
   yet.", "Couldn't load your spending history, so the plan can't be
   filled in or saved yet."). The saves themselves refuse a missing
   input. A failed settings or onboarding read ends the planners'
   "loading…" in the failed sentence and Try again.
- **Shared words and pieces.** The screen-states twin block (`moneymath.ts` / `pure.ts`,
   case table in `money-cases.mjs`) holds `failLine(error, what,
   client)`, `staleLine(error, updatedAt, client, now)`, `retryRead` /
   `retryReadDelay`, `knownOr` + `figureGreyed`, and `busyText` /
   `restartingText`, which `api/client.ts` and `lib/api.ts` re-export
   rather than keep copies of their own. The 403 fallback is "Your role
   doesn't include <what>." (Choices made, below); the stale time is a
   clock time with AM/PM today ("9:41 AM"), else "Mar 3". `Loaded`,
   `FailedLine`, `StaleLine` on both clients, `EmptyState` on the phone
   (the web's takes an optional line). Phone `money(null)` prints
   "·". The web's `AuthError` carries `status = 401`; a read's 401 signs
   out through a `QueryCache` handler, except the `["me"]` read and once
   the cache holds no `me` (so the reads aborted by that sign-out cannot
   start another). Both clients retry by `retryRead`. `StaleLine` replaces
   the phone's `StaleBanner`, and every phone "Couldn't reach the server"
   line is `FailedLine` (or `failLine(…).text` where the screen holds a string),
   so a 5xx, 403 or 429 is named as what it is.
- **Transactions and Accounts figures.** `knownOr` takes an answer on
   screen (the kept copy after a failed refetch, or the previous
   filter's standing in) and `figureGreyed` greys both (Choices made, 1). Web Transactions: the search count and total and the month
   count come from `knownOr` ("·" when unknown, no count at all for the
   month), the hero card greys, the raw "Couldn't refresh — …: Error:
   500 …" note is the `StaleLine`, and a failed first read is the
   `FailedLine` in the list's place. Phone Transactions: the pile is
   shown only for the question it answers (`pileFor`), the previous
   question's placeholder page is never merged into it, the hero greys,
   and the empty list is the `FailedLine` when the read failed. Receipts
   (both): `StaleLine` / `FailedLine` above the list. Accounts (both):
   "Import" / "manual account" only once the connections read answered,
   and a failed connections or settings read is a `FailedLine` at the top
   of the list; the raw web error is the `FailedLine`. The phone detail
   with no hand-off reads the row from `GET /api/transactions/one?id=`,
   which answers the list door's row exactly (the `Txn` model), on the
   household's connection, so another household's id is a 404 like a row
   that does not exist; a settled hold answers as the posted row it
   became. It shows "Loading…", then the row; a 404 (no such row, or a
   server older than the door) says "This transaction is no longer open
   here." with "Find it on Transactions"; any other failure is the failed
   line.
- **Business.** Overview (both): every tile from `knownOr` — "·" for an
   unread profit, cash, deadline or count (never "—" or "0 — nothing
   outstanding"), greyed while kept, and one
   `FailedLine` under the tiles per failed read. Review queue (both): a
   failed read keeps the card with the failed line instead of "Nothing to
   review" (web) or nothing (phone). Tax set-aside (both), Books' P&L
   and transaction list (both), 1099 payees (both), the compliance
   calendar (both), the entity's Accounts (both) and both setup wizards'
   account pickers: a failed read says so where the section sits, and
   "No …" lines wait for an answer. The page-level reads
   in Business (summary, flagged worksheet, the delete impact) fail to
   the failed line too. These stay plain sections
   rather than `Loaded` wrappers: each already had its own header and
   pending state, and the wrapper would only have moved them.
- **Header and health.** The web bell is "Alerts" until its read has answered;
   "nothing needs attention" only from an answer (the phone's bell and
   More badge show nothing without one).
   System health (both): a failed first read is the `FailedLine`; a poll
   that fails after an answer puts the `StaleLine` over the kept results,
   greys the verdict and drops "auto-refreshes — checked 10:02". On a
   hosted instance only an unlinked-records list that answered empty sends
   the page to Settings; a failed read says so in place.
- **Settings rosters and security.** The sidebar's "no 2FA or passkey" warning waits for the
   passkey list to answer; with it unread and no authenticator, the
   Security line claims nothing. Passkeys, sessions, mobile devices, the
   household's people and invitations, webhooks, the delivery log and
   tokens (both clients where the card exists): a failed read is the
   failed line where the list sits, the card keeps its place, the "No …"
   lines wait for an answer, and a kept copy carries the stale line. A
   failed text-and-push status never reads as "isn't configured": the
   reasons wait for an answer and the failed line takes the SMS card's
   place. The web page's failed first settings read ends in the failed
   line rather than "loading…" for good, and the page carries the stale
   line; the phone's Settings, Security and Email & Push carry it on
   their reads. These stay plain sections rather than `Loaded` wrappers,
   for the reason given under Business.
- **Reports' secondary cards.** Each card keeps its place with the failed line where its read
   failed: Spending's this-month card (web; the phone has no such card),
   investment fees and interest-and-fees paid (both), the stored W-2s and
   recent imports (both, so the undo is never silently gone), the matched
   pairs (both — the web card keeps its search box). The merge pickers
   (both), the Merchants list (both) and the category lists in Rules
   (both, the web rule editor included) say "no match" / "none yet" only
   from an answer. The nightly finder switch (both) shows nothing while
   unread and the failed line when the read failed, never a default "on";
   a goal's account picker (both) is the failed line instead of "no
   account (plan only)". Today (both): a failed onboarding read puts the
   failed line where the setup card would be. The setup wizard: Connect's
   "connect at least one source" waits for the setup state (the hint is
   "Loading…" or the failed line's words, and Continue is held); the
   Done summary (both) prints "·" for a line whose read has not answered; the
   bills panes (both) tally only from an answered list, and "found
   nothing" waits for a scan that ran.
- **Blank and stuck screens.** A failed first read is the failed line, under whatever
   controls the screen keeps: phone Bills (under the month stepper, naming
   the month when it is a past one), Net worth (both), web Line items
   (under the search form), and the Budget page (both: "this month's plan"
   where the hero and this month sit, "the budget settings" where the
   cards that edit them sit). The phone's bill and bill-history screens
   with no bill named, and the web's bill history, say "Pick a bill on
   Bills & Income." with that one link, rather than a spinner on a read
   that never starts; their read failures are the failed line.
   The wizard's proposals (phone) and the member's history choice (both)
   end in the failed line. Phone Activity holds its pages by the filter
   they answer (`pagesAfter`, pure-logic tested): a filter change or a
   pull keeps them, dimmed, until the new answer; a failed one drops them
   for the failed line; a refetch of a page already held replaces it; and
   a filter that answered empty says "Nothing matches these filters."
- **Keep the control on screen.** Web Bills keeps its title and month stepper while a month
    loads and when it fails (the failed line names a past month). A range
    that fails keeps its card and range picker, with the failed line
    naming the range, on Bill history (both: the last answer for the bill
    stands, a new range dims the list until it answers, and only the
    transaction list says it failed), Cash Flow's overview, flow and
    income (both) and Spending (both). The phone's Cash Flow tabs stay
    when the overview fails, so Income (its own read) is reachable. Web
    Debt keeps the last plan the server sent and puts a refusal beside the
    fields in the server's own sentence, as the phone does. Web Rules
    keeps the page, search box and focus while a search loads (the
    previous answer dimmed), and a failed search keeps the hero and the
    box with "·" for the count and the failed line where the groups sit;
    each group keeps its rows while its next page or search loads. The
    web Month and Year lenses keep the old period on screen, dimmed, while
    a step loads, as the phone does, and a failed one is the failed line.
- **Every remaining error line.** Page-level error lines (web Today, Help, Alerts,
    Reimburse, Feedback, a merchant's detail, Retirement, the Assistant's
    status and history, the deposit search; phone merchant detail,
    Assistant, deposit and category pickers, Social Security estimates)
    are the failed line. A write's failure line says itself too:
    `failLine` takes the verb ("Couldn't send your report — you're
    offline."), and `errText` on both clients falls back to `failCause`
    (twin block, case table) when the server sent no sentence of its own —
    "You're offline." / "Can't reach the server.", "Something went wrong on
    the server.", "The server refused the request." — so no line prints
    "TypeError: …", a bare status, a JSON body or a page. The native
    passkey sheet's own error text is not read out. The raw-text
    guard (`test_no_raw_error_text_on_screen.py`) reads every client
    source and fails on a raw rendering: `String(error)` in JSX, a
    hand-made "Couldn't …: {errText(…)}" line, an error's message pasted
    into a sentence, a JSON body or stack on screen, a bare status, and the
    old "tap / pull down / reload to retry" lines. No guard looks for
    `?? 0`: telling a real zero from an unread one is a
    reading of each line, not a pattern.
- **Empty states.** The words are one table, `EMPTY_LINES` in the twin block,
    and "Connect or import" is offered only when an accounts read answered
    with none (`noAccountsYet`, behind `useNoAccounts` and `NoAccountsLine`
    on both clients: the web's goes to Accounts, the phone's to the
    connect hub). Transactions (both): a filter, search, owner, scope or
    reimbursement view that matches nothing is "Nothing matches these
    filters."; an empty month is "No transactions this month.", or the
    connect line with nothing linked. Bill history (both): a narrow range
    is "None in this range.". Month lens (both): the connect line above a
    new household's month; the phone keeps its by-category and biggest
    cards with "No spending." / "No spending posted.". Net worth (both,
    with nothing linked and nothing entered by hand) and Debt (both, in
    place of "Nothing owed") are the connect line. Cash Flow income (both):
    "No income in this range." instead of "$0 came in". Spending (both):
    any window with nothing in it is "No spending in this range." (or the
    connect line) under the range picker; the phone has the web's
    "ledger starts this month" line for the default year. Business books
    (both): "No business transactions yet.". Merchants (both): "No
    merchants yet." with no search or filter, else the filtered line. The
    web's activity log, both Alerts histories and Line items say the
    filtered line for a filter that matches nothing. Web Import says "No
    file imports yet." instead of hiding the card. Reimburse, the
    Assistant's not-set-up line and Bills say the same line and the same
    one action on both clients; the Bills action is the finder button's
    own name, "Find bills & income", runs it, and is not offered to a
    viewer. Phone Feedback says a failed read above its form. The Bills
    calendar's failed line is the shared one on both. Left as they are:
    the Bills calendar shows only once bills exist on both clients (the
    web gates it the same way, so they already match); a member without
    access is under Choices made, below.

## Choices made

1. **A figure whose read fails shows its last known value, greyed**,
   under the one line "Couldn't refresh — showing what was here at 9:41
   AM." (the time of the copy). With nothing kept, it shows the unknown
   dot "·" (`UNKNOWN_FIGURE`). Never $0 or "0 transactions" for a failed
   read: "Spent this month $0" states something untrue, while a greyed
   $1,240 under the stale line states the last thing known. Rejected: a
   dash ("—"), a second glyph for the same unknown.
2. **A failed section keeps its place**: "Couldn't load <section> —
   <reason>. Try again". A section that closes up reads as never there:
   an investment fees card that vanishes says no fees were ever charged.
   Rejected: hiding each failed section, with one failed line at the top
   of the page naming everything that failed.
3. **The phone's offline copy adds the Month and Year lenses (the
   current month and year) and Help.** Which reads the encrypted
   persisted cache keeps is one pure rule, `keptOffline(key, now)`
   (`lib/pure.ts`, pure-logic tested): `today`, `bills`, `accounts`,
   `alerts`, `me`, `settings`, `onboarding`, `connections`, `calendar`,
   `categories`, `report`, `holdings`, `account-links`,
   `assistant-status`, `reimburse` and `help`; the Month lens only for
   the household's current month and the Year lens only for its current
   year; the Transactions tab's opening page (the current month,
   unfiltered). "Current" is the household's day (the zone from the
   cached `me`), the day those screens open on, worked out once a minute
   rather than per query. The three screens carry the stale line, so
   offline they show the kept copy under "Offline — showing what was
   here at 9:41 AM.". The cache shape is unchanged, so the persist buster
   is too. Rejected: every screen's first view.
4. **Offline saves: on the phone, buttons that save are greyed** with "You're
   offline." beside them, rather than spinning and then failing. There is
   no native module: the signal is the app's own requests. Every fetch
   the query client settles goes through `offlineAfter` (`lib/pure.ts`)
   — no answer at all (`noAnswer`, the shared failed-read cause, counted
   on a read's first failed attempt) is offline, any answer, a 5xx or a
   refusal included, is online, an aborted request says nothing
   (`lib/connection.ts`, bound to the query client; a cache patch is not
   an answer). On a device the global fetch is expo/fetch, whose
   no-answer failure is a plain `Error` reading "fetch failed: …", not a
   `TypeError`; `noAnswer` counts both, and not expo/fetch's cancel.
   Evidence is ordered by when each attempt started: a request or a look
   that went out before the latest answer and then got none is stale and
   ignored (every request is stamped as it goes out, `fetchStamped`).
   `ConnectionWatch` in the root layout looks at `/healthz` on every
   return to the app and every 15 s while offline, never while a look is
   in flight; no answer in 5 s is offline. Back online, the reads on
   screen that failed meanwhile are asked again. One gate,
   `components/offline.tsx`: `useOffline()`, `offlineGrey()` and
   `OfflineNote` ("You're offline."), read by the writes on the bill
   editor, Bills (add, find, proposals, archived, attention rows), the
   budget planner, income scenarios, savings goals and bucket rules, a
   transaction's edits (category, split, reimbursement and business
   flags, entity, note, stuck pending, receipts), Settings (backup, mail,
   AI, investment fees, Plaid enrichment, email, tokens, webhooks) and
   Reimburse. Sign-out resets the verdict. Rejected: leaving writes to
   fail with their own error.
5. **A member whose role excludes a page**: the page leaves their menu,
   and a link to it says "Your role doesn't include <page>." Built from
   the server's existing role information only. Every role currently
   reads every page (`permissions.py`: "everybody in a household reads
   everything"), so no menu entry is removed; a read refused with a 403
   and no sentence of its own says "Your role doesn't include <what>."
   Rejected: the line alone, with the page still in the menu.
