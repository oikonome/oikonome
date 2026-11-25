# Plaid Link sessions

How a bank link made through Plaid's Hosted Link is started, finished and
answered. The code is `sync/plaid.py` (`Client.create_hosted_link`,
`link_item`), `sync/plaid_user.py`, `jobs/plaid_links.py` (the session
record and `complete_add`, which every door finishes through) and the
link doors in `web/pages.py`.

## Several banks in one session

A new link may let the person connect more than one bank before they
leave Link (Plaid's multi-institution Link): after the first bank, Link
offers to add another.

### The link token

`create_hosted_link(multi_item=True)` is asked for by the add door only
when all of these hold:

* it is a plain add — never a re-auth (update mode is for repairing one
  Item) and never a relink for a wider history window (which replaces ONE
  bank by design);
* the household has room for at least two more connections under the
  institution cap (or has none), counting the add sessions already open.
  At the last slot a second bank could only be handed back.

Then the body is the ordinary new-link body plus `enable_multi_item_link:
true` and `user_id`. The ordinary body is unchanged: `client_id`,
`secret`, `client_name`, `language`, `country_codes`, `user`
(`client_user_id` = the household id), `hosted_link`, `products`,
`transactions` (`days_requested`), and when they apply
`required_if_supported_products` and `webhook`. Update mode sends
`access_token` (and `update`, `additional_consented_products`) and never
the flag.

The answer carries `multi_item`: whether the token really allows several
banks. It is false when the deployment switch `OIKONOME_PLAID_MULTI_ITEM`
is `0`/`off`/`false`/`no` (the request is then byte for byte the earlier
one), when no Plaid user could be had, or when Plaid answered the flagged
request with a 400 — that request is asked once more as the ordinary one,
so linking never breaks on the flag. The refusal is logged at warning with
Plaid's error code only.

### The Plaid user

Multi-institution Link needs a Plaid user: `POST /user/create
{client_user_id}` answers `user_id` (201). A user belongs to ONE Plaid
client in ONE environment (a client_id is the same in sandbox and
production; only the secret differs), and a household links under the
instance's keys or under its own, so `plaid_users` keeps one row per
(household, keys): `client_id` holds the client_id in production and
`<client_id>@<environment>` anywhere else (`plaid_user.key`). An id made
under one client or environment is never sent with another's. A
household that switches keys or environment gets a new user the next
time it links.

* Made lazily, at the first link start that wants one, under a
  per-household advisory lock: two starts at once make one user (the
  second finds the first's row).
* A 400 for the household's own id (Plaid's answer to a repeat of a
  `client_user_id` is not documented to us; a user made earlier whose
  answer was lost is the likely cause) is asked once more under
  `<household id>:<6 hex>`. That `client_user_id` is stored and sent in
  `user` with every link token carrying the user.
* **Pauses** (`plaid_users.multi_item_off_until`, `off_reason`,
  `cooldowns`; persisted, so every web process sees them): several banks
  per sign-in are paused for the household and keys when /user/create is
  refused both times (a row with no user records it), or when Plaid
  refuses two flagged link tokens in a row with the stored user. A pause
  lasts 24 hours, doubling with each pause in a row up to 7 days; the row
  and its user are KEPT, and the same user is tried once more after the
  pause — no pause mints another user. An accepted token clears the
  counts. A transport failure or 5xx pauses nothing.
* Not a secret, but it names the household at Plaid: never logged.
* Not exported, not restored (`EXPORT_SKIP`, `ARCHIVE_TABLE_SKIP`): it is
  an id at Plaid tied to this instance's keys. A household restored
  elsewhere gets a new one when it next links.
* Erased with the household (every `tenant_id` table is). Nothing removes
  the user at Plaid: once the household's Items are released the id holds
  no access to any bank and is inert.

### Finishing a session with several banks

`/link/token/get` reports one `item_add_results` entry per bank, each with
its own `public_token` and `institution`; the session is finished at
Plaid when `finished_at` is stamped (the person pressed finish, or closed
Link from inside it — the LINK `SESSION_FINISHED` webhook, status
`success` or `exited`). The deprecated single-token `on_success` is never
read.

Every door — the client's poll, the LINK webhook (`ITEM_ADD_RESULT` with
its `public_token`, `SESSION_FINISHED` with `public_tokens[]`) and the
sweep — runs `complete_add` under the household's sync lock, with the
Plaid client of the keys that made the session's link token
(`plaid_link_sessions.client_id`, `session_client`): a public token can
only be exchanged, and its Item removed, under those keys. The instance's
own keys are always at hand; a household's own keys only while they are
still its keys. A bank brought under keys that are gone cannot be
exchanged here: the finisher logs it at warning by the session's token
hash and answers "error" (a residual — nothing can remove an Item whose
token we cannot exchange).

* Each bank is exchanged as soon as it is reported, and its outcome is
  recorded under its token's hash the moment it is known (`exchanged`),
  so no door exchanges a token twice. A webhook's tokens are Plaid's word
  on what the session made: one missing from `/link/token/get` is settled
  all the same. They ride the worker job (`plaid_link_finish`, registered
  with `keep_result=0`, so its arguments leave Redis with the job), never
  a stored row. A job that finds the household's lock held is queued
  again with the same tokens 20 s later, up to 6 times.
* Each bank goes through `link_item` on its own, so every per-household
  rule applies per bank: the institution cap, a login already held —
  including one added earlier in the same session — the bank-link hold,
  keys changed mid-link. Every refusal comes AFTER the exchange, and the
  refused Item is removed at once (`/item/remove`; queued for the nightly
  retry if that fails).
* The answer:
  * until Plaid stamps the session finished, and while it has brought a
    new bank — or Link reported an event (`link_sessions[].events`, when
    present) — within the quiet time: `{"done": false, "kind": "busy",
    "results", "summary", "progress"}`;
  * stamped finished, or quiet: the finished answer. A quiet one carries
    `"quiet": true` — the bank window may still be open.
  * The quiet time is `QUIET_S` (90 s) once a client that says it keeps
    its window open on a quiet answer (`v=2` on the poll, kept on the row
    as `client_v`) has looked, else `OLD_CLIENT_QUIET_S` (5 min): a phone
    built before this closes its in-app browser on the answer, and a
    bank's own sign-in page can be silent for minutes. 5 minutes is still
    well inside every client's 35-minute wait.
  A one-bank session finishes at its bank, as before. A token whose
  exchange got no answer (a transport failure) keeps an unfinished
  session open for the next look.
* **A finished several-bank session keeps taking banks** for its window,
  `results_until` = Plaid's `expiration` for the link token (from the
  `/link/token/create` answer) plus the 6 hours `/link/token/get` keeps
  results; 10 hours from the start when the answer names none; never more
  than 30 hours. We send no expiration of our own: the request field is
  not one we know. Inside the window every door still settles a bank not
  yet in `exchanged` — a result that lagged the finished stamp, a bank
  added after the quiet answer — keeping it when the household may (cap,
  repeat and hold rules as above) or handing it back, and rewrites the
  finished answer to include it. `tenant_for_link` resolves such a
  session, and the sweep looks at it again every minute in its first
  hour after finishing, every 15 minutes up to four hours, then hourly.
  Its row is kept past `KEEP_S` until the window closes.
* **The residual:** a bank Plaid reports after the window closes is not
  exchanged here; the webhook logs it at warning with Plaid's link session
  id only. It is bounded by the token's own lifetime: Link cannot add a
  bank to an expired token.
* A session given up to make room under the cap (`forget`) is set aside,
  not deleted, when it can bring several banks: its later banks are still
  exchanged, and the cap check refuses (and hands back) any past it.
* An erased household's several-bank session inside its window stays as
  a tombstone marked ERASED, stripped at the erasure to what the hand-back
  needs: the link token, the keys' client_id and the hashes of the tokens
  already settled — no bank names, Item ids or answers. Every bank it
  brings is exchanged and handed straight back under the keys that made
  it. A household on its own keys took them with it: the first look at
  such a tombstone closes it (finished, window shut — logged by session,
  and the Item bills that household's own Plaid account, which can remove
  it), so nothing retries it. It is deleted by the sweep once its window
  has closed (at most 30 hours). A row whose household no longer exists
  hands its banks back whatever its reply says. A reset marks its
  sessions the same way.
* **The marks survive every rewrite.** `reply.refuse` (every later bank
  is handed back: a reset, an erasure, the day's ceiling) and
  `reply.cancelled` (set aside) are carried, in SQL against the row on
  record (`plaid_links._reply_sql`), through every door that rewrites
  `reply`: the finished answer, a late bank's rewrite, set-aside and the
  sweep's abandonment. An erased household's row is written with no bank
  names, results or summary — only done, kind, the error and the mark.
* A late bank is taken by the kind the session was STARTED as (`add`),
  not by how its first answer ended (`add_failed`, `exited`).
* The window each bank records is the token's (`days_requested`), the
  same for every bank of the session.
* A finisher that lost its record of an exchange finds the Item by the
  token's hash, kept on the row (`items.raw.link_token_key`); a row from
  before the hash was kept is found by its Plaid session only when that
  names exactly one unaccounted Item.

### The cap and the repeat limit inside one session

Plaid's "add another bank" cannot be stopped from here, and every bank
the household may not keep is a real Item at Plaid for the minutes before
it is removed. Two bounds:

* **A session that overreaches** — more than 2 of its banks refused for
  the cap (overshoot included) or as a repeat — pauses several banks per
  sign-in for the household and keys for 24 hours (the same
  `multi_item_off_until`, with its reason) and says so in the activity
  log. One bank per sign-in still works.
* **The day's ceiling**: once 6 freshly linked banks have been handed back
  in a day (ledger reasons `repeat`, `overshoot`, `link refused…`), new
  bank links are refused for the rest of the day: "Too many banks were
  linked and handed back today — connect more banks tomorrow" (the start
  door answers it, and the line under the buttons says it). It holds
  inside a sitting too: a several-bank session that brings a bank once
  the ceiling is reached hands it — and every later one — back, saying
  so (marked `refuse` on the row), and pauses several banks per sign-in.
  This bounds what a sitting keeps, not how many banks a person signs in
  to inside a window already open.

* **Overshoot.** A bank refused at the cap is ledgered so it holds its
  place for `RELEASED_HOLD_DAYS` — the hold is what stops link,
  disconnect, link again from minting a billed Item every pass. A bank a
  several-bank session brought past the cap is not that: it was handed
  back within minutes and never synced. It is ledgered `overshoot`, which
  the hold leaves out — at most `OVERSHOOT_FREE_PER_DAY` (2) a day per
  household; past that a cap refusal holds its place as before, so
  repeated sessions past the cap still run into it.
* **Repeats.** The repeat limit is read before the exchange but applied
  only to a token found to be a repeat: a new bank in a session full of
  repeats is kept. Its words say the bank is already connected and that
  linking it again today will not help; banks refused for one reason are
  worded together ("these banks are already connected…"). Within the
  limit a repeat is ledgered `repeat` (no
  place under the cap); past it, it is still exchanged and handed back
  (an unexchanged token's Item could never be removed) but ledgered as an
  ordinary refusal, which holds its place — the cap then bounds a client
  stuck relinking.

### What the person is told

The finished answer of a session that came back with more than one bank
adds:

* `results`: one entry per bank, in Link's order — `{institution,
  connected: true}` or `{institution, connected: false, error}`, the
  institution named by our row or, for a refused bank, by Plaid's result;
  a Plaid refusal of the exchange reads "its sign-in could not be
  completed — link it again", never Plaid's raw message;
* `summary`: the sentence both clients print as it is — "Connected Harbor
  Bank and Northwind Credit Union — first sync running — accounts appear
  in a minute." followed by the banks not kept, grouped by why: "Juniper
  Savings was not connected: <why>."

The earlier fields keep their meaning: `kind` is `add` when any bank
connected (`add_failed` when none did), and `institution` is the
connected banks' names joined with ", " — so an older client, which reads
only that, shows "Linked Harbor Bank, Northwind Credit Union" and says
nothing of a bank refused beside them. A session with one bank answers
exactly as before (no `results`, no `summary`).

While the session is open the busy answer's `progress` ("Connected Harbor
Bank — add another in the bank window, or you're done.") replaces the
pending row's "Finish signing in at your bank…" on both clients. A client
that saw a bank in never reports a timeout: it answers with the banks. When
the window is closed after some banks are in (the person's cancel on the
web, the in-app browser closing on the phone), the client answers with
those banks, sets the session aside, and the server keeps finishing it.
The phone does not close the in-app browser on a `quiet` answer.

The line under every add-a-bank button is the server's
(`/api/me` → `features.bank_link_line`). It is written by the ONE function
that decides what the next start does (`pages.link_offer`), so the two
cannot disagree: a start that would be refused (read-only billing, the
bank-link hold, the day's ceiling, no room under the cap counting the add
sessions still open) has its refusal as the line; otherwise "You can
connect N more banks — several in one go: after the first, choose to add
another before you finish." when capped, or "You can connect several
banks in one go: …" when uncapped, the "several" half only when the next
start would ask for several (the switch on, room for two counting open
sessions, keys present, not paused), else "You can connect N more banks."
or nothing. A phone talking to an older server prints nothing
(`older-server path: stands in for \`bank_link_line\``).

A bank a finished session brings after its answer is written to the
household's activity log as it is settled: "connected Juniper Savings, a
bank added after the sign-in had answered", or "did not connect Juniper
Savings: <why>".

### Residual risks

* A bank Plaid reports after the window closes is not exchanged (logged
  by Plaid's session id). Bounded by the link token's own lifetime.
* A bank brought under keys the instance no longer has (a household that
  replaced or erased its own keys mid-window) cannot be exchanged or
  removed here; logged by session.
* A phone built before this change closes its in-app browser on the
  quiet answer — after 5 minutes of silence, not 90 s; a person still at
  a second bank's own sign-in page by then must start that bank again.
* Up to 6 refused banks a day (and up to 2 per session before the pause)
  are real Items at Plaid for the minutes before their removal — and a
  person who keeps adding banks in a window already open makes one more
  each time: every one is handed back, none can be prevented.
* The account answer's line under the buttons counts sessions on record
  through a 30-second per-household cache (`recent_open_cached`); the
  start door reads the record fresh.
* Webhook public tokens sit in Redis while their job is queued (seconds,
  or up to 2 minutes while the household is busy). They are single-use
  and need this instance's secret.
* Sweep load: a finished several-bank session costs about 30
  `/link/token/get` calls over its window at the 5-minute sweep tick (the
  first hour's per-minute cadence is bounded by the tick). Linear in the
  sessions finished per day.

### Not known

Plaid's documentation does not state a maximum number of banks per
session, nor how `completion_redirect_uri` behaves with several (it opens
"when the Link session is complete"). Neither is relied on: the cap is
ours, checked per bank, and the redirect page only wakes the tab.
