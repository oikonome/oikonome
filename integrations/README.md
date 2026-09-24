# integrations/

What the instance can say to other programs, and how to listen. The
product doors — webhooks, the read endpoints, `/metrics` — are in
**Settings → Integrations**; the guide is
[docs/integrations.md](../docs/integrations.md). This folder holds the
pieces that run OUTSIDE the instance:

| Folder | What |
|---|---|
| [mcp/](mcp/) | `oikonome_mcp.py` — a read-only MCP server over your ledger for any assistant that speaks MCP over stdio. One file, no packages. |
| [home-assistant/](home-assistant/) | REST sensors on the summary endpoint and a webhook-triggered automation, as YAML to paste. |
| [prometheus/](prometheus/) | A scrape job for `/metrics`. |

All of them authenticate with an **integration token** (a read-scoped
script token from Settings → Integrations), which can read these doors and
nothing else.
