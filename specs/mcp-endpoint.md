# The MCP endpoint (read-only, over HTTP)

An assistant that speaks the Model Context Protocol (MCP) can read a
household's books through one HTTP route on the instance itself. It is
the remote sibling of the one-file stdio server in `integrations/mcp/`:
the same credential, the same read-only promise, served by the app so
nothing extra has to run beside it.

This note is the design. The self-hoster's page is `docs/mcp-endpoint.md`.

## Off unless switched on

`OIKONOME_MCP_ENABLED` (an env boolean, `envnum.env_flag`; default off).
While it is off every method (GET, POST, PUT, PATCH, DELETE, HEAD,
OPTIONS) on `/api/integrations/mcp` answers
`404 {"detail": "Not Found"}` before any credential is read — the same
answer as a path that does not exist, so an unauthenticated probe learns
nothing. The flag is read per request and forwarded to the app
service by `docker/compose.yaml`, so flipping it needs only the container
recreated (`docker compose up -d app`), never a rebuild.

## The credential

The **existing read-scoped API token** (`oik_…`, Settings → Integrations,
`auth/api_tokens.py`), resolved by the same `current_user` path every read
door uses. Nothing new is minted and there is no new scope.

- A read token is GET-only by design. The one exception is a single exact
  entry, `("POST", "/api/integrations/mcp")`, in `web/app.py`'s
  `READ_TOKEN_EXACT`; the handler dispatches only to read tools.
- A push token is refused by the resolver exactly as on the other read
  doors (`403`, "this token is push-scoped").
- A browser session, a phone's device token or a widget token is refused
  with `403`: the endpoint is for programs holding a read token, and
  accepting a cookie on a POST would make it a cross-site target.
- Revoked or unknown tokens get `401` (with `WWW-Authenticate: Bearer`);
  tenant lockouts apply as on every token door. Every refusal here is a
  JSON-RPC error body (`-32000`) carrying the read doors' own wording,
  under the same HTTP status they answer.
- A write that carries an `Origin` (or `Referer`) naming another host —
  a web page holding the token — is refused with `403` and a JSON-RPC
  error. This route runs that check itself (`security.cross_origin_write`,
  the predicate the Origin middleware applies to every other write door;
  `security.ORIGIN_CHECKED_IN_ROUTE` names the one exception), after the
  switch, so a switched-off instance answers `404` whatever the headers.
- The household is the token's household. No tool takes a household,
  tenant or user argument; the connection comes from
  `tenancy.tenant_connect(token.tenant_id)`, so row-level security bounds
  every query.

## Protocol subset (implemented by hand, no dependency)

JSON-RPC 2.0 over a single `POST /api/integrations/mcp`: the protocol's
HTTP transport in its stateless, JSON-only form.

| Message | Answer |
|---|---|
| `initialize` | `protocolVersion`, `capabilities: {tools: {listChanged: false}}`, `serverInfo {name, title, version}`, `instructions` |
| `notifications/initialized` and any other notification (no `id`) | `202 Accepted`, empty body |
| a JSON-RPC *response* from the client | `202 Accepted` (the server never asks anything, so there is nothing to match) |
| `ping` | `{}` |
| `tools/list` | the two tools, each with `name`, `title`, `description`, `inputSchema`, `annotations` |
| `tools/call` | `content: [{type: "text", text: <JSON>}]`, `structuredContent: <the same object>`, `isError: false` |

- **Revision.** The server speaks `"2025-11-25"` and also accepts
  `"2025-06-18"`. `initialize` echoes the client's requested revision when it
  is one of those two, otherwise answers `"2025-11-25"` (the client then
  decides whether to continue). A request carrying an
  `MCP-Protocol-Version` header naming anything else is a `400`.
- **No stream, no session, no server-initiated messages.** The response
  is always one `application/json` body. No `Mcp-Session-Id` is issued.
  `GET` (the client asking for a server-to-client event stream) and
  `DELETE` (ending a session) answer `405` with a JSON body saying the
  endpoint does not stream and has no sessions. A `POST` whose `Accept`
  header admits only `text/event-stream` answers `406` saying the same.
- **No batches.** A JSON array body is refused (`-32600`), as the
  `"2025-06-18"` revision removed batching.
- **Errors** are JSON-RPC errors with a plain-English `message`:
  `-32700` unreadable JSON (`400`), `-32600` not a JSON-RPC request
  (`400`), `-32601` unknown method, `-32602` bad tool arguments
  (`"end_date is before start_date"`, `"limit must be at most 100"`,
  `"unknown argument 'foo'"`), `-32603` a tool that failed on the server
  ("nothing was changed"), `-32000` rate limited (`429`) or body too large
  (`413`). Valid input never errors: a search that matches nothing is a
  success with zero totals and a note.

## The tools

Both carry `annotations {readOnlyHint: true, destructiveHint: false,
idempotentHint: true, openWorldHint: false}` and a human `title`. Every
description states the sign convention and that names and notes are data
written by banks, merchants and people — never instructions.

**Sign convention (the app's, `reporting.SPEND_WHERE`):** a transaction
amount is positive for money out (a purchase, a payment) and negative for
money in (a paycheck, a refund). Totals are stated as positive `out` and
`in` figures with `net = in − out`. An account balance is as the bank
reports it: a card's or a loan's balance is what is owed.

Every result carries `as_of` (the household's own date, `localtime`) and
`currency: "USD"`.

### `get_overview` — "Accounts and coverage"

No arguments. Returns:

- `accounts[]`: `id` (opaque, see below), `name`, `institution`, `type`,
  `subtype`, `mask` (last four only, or null), `balance`, `available`,
  `balance_as_of` (the connection's last clean sync, or null when it has
  none), `first_transaction_date`, `last_transaction_date`, `owner`,
  `counted` (false for the second copy of an account linked through two
  connections, whose money is counted once through the other copy).
  `hidden` marks an account the household hid: the search leaves its
  rows out unless it is named in `account_ids`, as the Transactions page
  leaves a hidden account out unless it is asked for.
  `counted_in_totals` is false, with `not_counted_because` (`hidden`,
  `linked copy`, `business`, `excluded`), for an account whose rows the
  money figures leave out however they are found.
- `owners[]`: the owner labels the household uses (`data.list_owners`).

Built on `data.accounts_detail(counts=False)`, `links.shadow_ids`,
`budget.excluded_account_ids`, `integrations.connection_freshness` (the
connection query the summary door also reads) and
`data.account_date_ranges` (each account's `MIN/MAX(date)`).

### `search_transactions` — "Search transactions"

Arguments (all optional): `start_date`, `end_date` (ISO dates, inclusive),
`query` (the Transactions page's universal search — part of a name goes
here), `category` (a category exactly as a reply returns it; `?` is
uncategorized), `merchant` (a name exactly as a reply returns it in
`payee` or `top_merchants`), `account_ids[]` (opaque ids from
`get_overview`, at most 50), `owner` (an owner label as `get_overview`
returns it), `min_amount` / `max_amount` (bounds on the
transaction's size, `ABS(amount)`), `direction` (`out` | `in`),
`spending_only` (only rows the budget counts as spending), `limit` (1–100,
default 25), `offset` (0–10,000).

Returns:

- `transactions[]`, newest first: `id` (opaque), `date`, `amount` (signed
  as above), `payee`, `category` (stored key), `category_label`,
  `account`, `account_id` (opaque), `owner`, `pending`,
  `counts_as_spending`, `note`.
- `totals` over **every** match, not the page: `count`, `out`, `in`,
  `net` — `reporting.flows` over the matched rows, which is Cash Flow's
  rule (a card payment is not counted on both sides, a transfer between
  own accounts is not money made or spent). These are the figures the
  Transactions page prints for the same search.
- `by_month[]` (`month`, `out`, `in`, `net`, ascending: the same rule
  grouped by month, `reporting.flows_by_month`; the cents each month's own
  rounding leaves over — at most half a cent a month — go to the largest
  month, so the months add up to the totals exactly), `by_category[]`
  (`category`, `label`, `amount`: spending by category, a hand-split
  charge counted under each part), and `top_merchants[]` (`merchant`,
  `amount`, `count`, at most 10, ties ordered by name). All three are
  computed over every match (`reporting.match_groups`); under a category
  filter each counts only the matching part of a split charge, so the
  merchants and the categories add up to `totals.out`.
- **Round trip.** Each group is keyed by exactly what the matching filter
  compares — the stored category (two spellings of one category are two
  entries with the same `label`) and the name a row is shown under — and
  a value that left through `clean_text` is resolved back to the stored
  value that cleans to it. So any `category`, `merchant` or `owner` a
  reply hands out, passed back as that filter, finds exactly the rows and
  the money it was given for.
- `left_out_of_totals`: how many matched rows the money figures leave out
  (a business's money, or rows on accounts the household excludes, hides
  or holds as a linked copy). When it is not zero, `note` says so: those
  rows are listed, and a `0` total is not their cost.
- `has_more`, `limit`, `offset`. `has_more` is false when the next page
  would start past the offset limit, and `note` then says to narrow by
  date; an `offset` past the limit is refused with the same advice.
- `coverage_note` when `start_date` is earlier than the first transaction
  of an account in scope: which accounts have no data before which date,
  so an assistant does not read missing history as no spending.
- `note` when nothing matched (several notes join into one).

Built on `data.search_transactions(groups=10)` with `data.Narrow`
(several accounts, owners, exact shown names, amount bounds, direction,
spending only) and an `offset`. Under `groups` the count, the in / out
totals and every grouping filter by the search's own predicate in SQL
rather than by its matched id list, and the id list is fetched no
further than the page's last row; a pinned test holds the statement
count (26 for an unfiltered search) and that no id list longer than a
page is sent.

## One snapshot, read only, bounded

A tool call runs in one transaction opened by `ledger_read.one_snapshot`:
`SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY` first (the
opening the data export uses), so every statement of the call reads the
same moment — a sync committing mid-call reaches none of the reply's
figures — and the database itself refuses any write. The tools change
nothing. Before the snapshot opens, the call reads the household's
settings once (`budget.load_config`): a household's first read may store
its derived starting budget, exactly as opening the app does, and that
one write cannot happen inside a READ ONLY transaction. Nothing the tools
reach writes on read: not the summary gather, the alert log or the month
snapshots, which they never call. `SET LOCAL
statement_timeout` (`STATEMENT_TIMEOUT_MS`, 5 s) bounds each statement; a
call that passes it answers `-32000` "the search took too long: narrow
it".

## Output safety

- Every free-text field (account and institution names, payee, note,
  owner, category labels) first loses every control and format character
  (Unicode Cc and Cf: zero-width spaces and joiners, the byte-order mark,
  the bidirectional marks; tabs and line breaks become a space), then
  passes `card_number.cut` (an unbroken run of 8+ digits, or a printed
  card layout, becomes `…` and its last four — the import rule, applied a
  second time at output), and is capped at 200 characters. Stripping
  first means no invisible character can split a number into runs too
  short to cut.
- Never returned: account, routing or card numbers (the mask is the last
  four only), aggregator ids (accounts and rows are named by an opaque
  `acct_…` / `txn_…` id: a hash of the household and the stored id, stable
  across calls), access tokens, the bank's raw statement line
  (`bank_text`), locations, receipts and item lists.
- Text is returned as JSON values and never interpreted.

## Limits

- **60 requests per minute per token** (`security.account_limit_or_429`
  keyed `("mcp", token id)`), on top of the read token's existing per-IP
  budget (`READ_TOKEN_RATE`, 600 an hour).
- Request body at most **64 KiB** (`413` past it).
- **One tool call per household at a time**: a transaction-scoped
  advisory try-lock keyed on the household; a second call while one runs
  gets `-32000` "busy … retry in a moment". Rotating tokens therefore
  cannot multiply the work a household can ask for.
- `limit` at most 100, `offset` at most 10,000, `account_ids` at most 50,
  strings at most 100 characters (`category`, `merchant` and `owner` 200,
  the length a reply may hand out) and free of control characters, dates
  `YYYY-MM-DD` only (no compact or week forms) between the years 1900 and
  2100, amounts at most a trillion; unknown arguments are refused.
- Logging: one line per request — method, tool name, status, latency and
  the token id. Never an amount, a merchant, a date or the query text. A
  tool that fails logs its exception class and the file and line that
  raised it, never the message or a traceback (either can carry an
  argument).

## Not there yet (deliberately)

- The other read tools (spending summary, period comparison, budget
  status, bills, cash forecast, envelopes, categories).
- OAuth or any credential other than the read token; a consent screen.
- Any write. There is no tool that changes anything, and the token
  cannot reach a write door.
- Streaming, sessions, resumability, `listChanged`, resources, prompts.
- The `"2026-07-28"` revision's `server/discover` handshake.
