# Oikonome MCP server

A read-only [MCP](https://modelcontextprotocol.io) server over your own
ledger, so an assistant can answer "how much can I spend today?", "what
did we pay the dentist last year?" or "which bills are due before Friday?"
from your books — and change nothing.

- **One file, no packages.** `oikonome_mcp.py` needs Python 3.10+ and the
  standard library. Copy it anywhere.
- **Runs where the assistant runs.** It talks to your instance over HTTP
  with an integration token (Settings → Integrations → Integration
  tokens), which can read the integrations endpoints and nothing else:
  no import, no settings, no credentials.
- **stdio transport**, JSON-RPC 2.0, one message per line — what desktop
  and command-line MCP clients speak.

## Set up

1. Mint a read token: Settings → Integrations → Integration tokens.
2. Add the server to your client.

Most clients take a JSON `mcpServers` entry in their config file:

```json
{
  "mcpServers": {
    "oikonome": {
      "command": "python3",
      "args": ["/path/to/integrations/mcp/oikonome_mcp.py"],
      "env": {
        "OIKONOME_URL": "https://money.example.lan",
        "OIKONOME_TOKEN": "oik_…"
      }
    }
  }
}
```

A command-line client that registers servers by command takes the same
three things: the `python3 /path/to/oikonome_mcp.py` command and the two
environment variables.

## Tools

| Tool | Arguments | Reads |
|---|---|---|
| `summary` | — | the verdict, left today, month pace, balances, bills due, alerts, connection freshness |
| `accounts` | — | every account with its balance |
| `transactions` | `q`, `since`, `until`, `account`, `category`, `limit`, `page` | the universal ledger search, newest first |
| `spending` | `months` | spend by category and by month |
| `bills` | `days` | unpaid bills due in the window, and the catalogue |
| `alerts` | — | active alerts |
| `net_worth` | `months` | today's net worth and the nightly series |

Each tool is one `GET` against `/api/integrations/…` — see
[docs/integrations.md](../../docs/integrations.md) for the exact shapes.

## Try it by hand

```
OIKONOME_URL=https://money.example.lan OIKONOME_TOKEN=oik_… python3 oikonome_mcp.py
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"me","version":"0"}}}
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"summary"}}
```

Logging goes to stderr; stdout carries only the protocol.
