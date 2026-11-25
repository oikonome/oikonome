# The MCP endpoint

Your instance can answer an assistant that speaks the
[Model Context Protocol](https://modelcontextprotocol.io) (MCP) directly,
over HTTPS, at one address:

```
https://money.example.lan/api/integrations/mcp
```

It is **read-only**: no tool it offers changes anything, and the token it
takes cannot reach a single door that writes. It is **off** until you
turn it on.

If your assistant runs on the same machine as a command it can start,
the one-file local server in [integrations.md](integrations.md#the-local-mcp-server)
does the same job without opening anything new.

## Turn it on

1. In `docker/.env`, add:

   ```
   OIKONOME_MCP_ENABLED=1
   ```

2. From the `docker/` folder, run `docker compose up -d app`: compose
   recreates the app container with the new setting. No rebuild is
   needed.
3. Sign in as the owner → **Settings → Integrations → Integration
   tokens** (these are read-only) → name it after the assistant →
   **Create token**. The token (`oik_…`) is shown once; copy it.

While the setting is off the address answers `404`, exactly as a path
that does not exist.

## Point your assistant at it

Most MCP clients ask for a URL and a header. Give them:

- **URL:** `https://<your host>/api/integrations/mcp`
- **Header:** `Authorization: Bearer oik_…`

A client configured with JSON usually takes something like:

```json
{
  "mcpServers": {
    "oikonome": {
      "url": "https://money.example.lan/api/integrations/mcp",
      "headers": { "Authorization": "Bearer oik_…" }
    }
  }
}
```

The endpoint answers plain JSON for every request: it keeps no session
and never opens an event stream, which clients handle on their own.

If a reverse proxy puts a sign-in page in front of the app, exempt
`/api/integrations/*` from it, as for the other integration doors
([reverse-proxy.md](reverse-proxy.md)) — the token is the credential.

## What it can read

Two tools:

- **`get_overview`** — every account (name, institution, type, last four
  digits, balance and when it was last updated, the first and last
  transaction date held for it, its owner, whether you hid it) and the
  owner labels in use. A hidden account's rows stay out of searches
  unless the assistant asks for that account. An account whose money the
  totals do not count (a business's, one you excluded) is marked so.
- **`search_transactions`** — transactions by date range, text, category,
  merchant, accounts, owner, size, direction or "spending only", a page
  at a time (up to 100 rows), with totals over every match: money out,
  money in and net by the same rules as the Cash Flow page, plus the
  matches by month, spending by category and the top merchants. It says
  so when the dates you ask about start before an account's history.

Amounts use the app's sign: **positive is money out**, negative is money
in. Long numbers in names are cut to their last four digits, the bank's
raw statement line is never sent, and accounts and transactions are named
by ids that mean nothing outside your instance.

## What it cannot do

Change anything: no categories, notes, bills, budgets or settings, no
imports — each call runs in a read-only database transaction. It cannot see another household on the same instance — the token
belongs to one household and every query runs under its row-level
isolation. A push-scoped token, a sign-in cookie or a phone's session is
refused, and so is a request a web page sends from another site: connect
from the assistant or a server-side client.

## Limits

60 requests a minute per token, one tool call at a time per household
(a second one meanwhile is told to retry), a 64 KiB request body, and the
read token's own budget per address. A refused request gets a JSON-RPC error
that says why in a sentence.

## Revoke it

**Settings → Integrations → Integration tokens → Revoke** on the token.
It stops on the next request. A password or email change revokes every
token you minted. To close the endpoint altogether, remove
`OIKONOME_MCP_ENABLED` from `docker/.env` and run
`docker compose up -d app` again.
