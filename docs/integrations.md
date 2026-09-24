# Integrations: webhooks, metrics, a local MCP server

Your instance can tell other programs what is happening with your money,
and answer their questions — without giving any of them your password.
Three doors, all in **Settings → Integrations** (owner only):

| Door | What it is | Who uses it |
|---|---|---|
| **Webhooks** | The instance POSTs a signed JSON event to a URL you choose when something happens | Home Assistant automations, a chat bot, a script of your own |
| **Read endpoints** | `GET /api/integrations/summary` and friends — a bounded, read-only JSON picture of the books | Home Assistant REST sensors, dashboards, the MCP server |
| **`/metrics`** | The same summary in the Prometheus text format | Prometheus, Grafana Agent, anything that scrapes |

The read endpoints and `/metrics` take an **integration token**: a script
token minted with the `read` scope. A read token can reach these doors and
nothing else — it cannot import a row, change a setting, or see a
credential. (Collector scripts use the other scope, `push`, which is the
mirror image: import only. One token is never both.)

Everything on this page runs on a self-hosted instance unchanged. On a
hosted instance webhook URLs must be `https` and public, like every other
URL a household supplies — see [deployment-modes.md](deployment-modes.md).

## Integration tokens

Settings → Integrations → **Integration tokens** → name what will use it
("grafana", "home-assistant") → **Create token**. The token is shown once.
Send it as a bearer:

```
Authorization: Bearer oik_…
```

Revoke it from the same card whenever you like. A password or email change
revokes every token you minted, so re-mint after one.

## Read endpoints

All under your instance's base URL, all `GET`, all JSON. Every session role
may read them too (they disclose nothing the pages do not).

| Path | Returns |
|---|---|
| `/api/integrations/summary` | The picture: `verdict`, `today.left` (dollars left to spend today), `today.allowance`, `today.spent`, `month` (budget, spent, expected, variance, days left, headroom), `cash` (checking, card debt, bills due in 14 days), `net_worth`, `accounts[]` with balances (`counted: false` marks the second copy of a linked account), `bills_due[]` (next 7 days), `alerts[]`, `connections[]` with `last_sync_age_seconds`, `transactions` counts |
| `/api/integrations/accounts` | Every account: id, name, institution, type, balance, available, transaction count, `counted` (false for the second copy of a linked account — the money math reads the other one, so skip it when summing balances) |
| `/api/integrations/transactions?q=&since=&until=&account=&category=&limit=&page=` | The universal ledger search, newest first, paged (`limit` ≤ 200). Amounts are positive for money out |
| `/api/integrations/spending?months=3` | Spend by month and by category over the last N months — the Spending page's figures: a linked account counted once, a hidden one not at all, a split charge in each of its categories at its share. Each category key (`?` for none) can be passed to `transactions?category=` to list the rows behind it |
| `/api/integrations/bills?days=30` | Unpaid bill occurrences due in the next N days, and the bill catalogue |
| `/api/integrations/alerts` | Active, undismissed alerts |
| `/api/integrations/networth?months=12` | Today's net worth, by institution and asset class, and the recorded nightly series |
| `/api/integrations/events` | The webhook event catalogue |

The summary is computed once and rendered twice: `/metrics` is the same
numbers, so a sensor reading the JSON and a dashboard reading the scrape
never disagree.

### Home Assistant: REST sensors

`configuration.yaml`:

```yaml
rest:
  - resource: https://money.example.lan/api/integrations/summary
    headers:
      Authorization: Bearer oik_…
    scan_interval: 900
    sensor:
      - name: "Money left today"
        value_template: "{{ value_json.today.left }}"
        unit_of_measurement: "$"
      - name: "Budget verdict"
        value_template: "{{ value_json.verdict }}"
      - name: "Bills due this week"
        value_template: "{{ value_json.bills_due | sum(attribute='amount') }}"
        unit_of_measurement: "$"
      - name: "Net worth"
        value_template: "{{ value_json.net_worth.total }}"
        unit_of_measurement: "$"
```

A fuller example, with a per-account balance sensor and a webhook-driven
automation, is in [`integrations/home-assistant/`](../integrations/home-assistant/).

## `/metrics` (Prometheus)

`GET /metrics` with the read token as a bearer returns the exposition
format. Every value is a gauge; money is in dollars. A refused scrape —
no token, a revoked one, a push-scoped one — gets a `401`/`403` with a
JSON `detail`, never a redirect to the sign-in page, so the scraper's
target page says why it is down.

| Metric | Labels | Meaning |
|---|---|---|
| `oikonome_verdict` | `verdict` | 1 on the row that applies: `ON BUDGET`, `OVER BUDGET`, `UNDER BUDGET` |
| `oikonome_left_today_dollars` | | variable money left to spend today |
| `oikonome_today_allowance_dollars`, `oikonome_spent_today_dollars` | | today's allowance and what has posted against it |
| `oikonome_bucket_left_today_dollars` | `bucket` | the same, per budget bucket |
| `oikonome_month_budget_dollars`, `_month_spent_`, `_month_expected_`, `_month_variance_` | | the month verdict's numbers; variance positive = over pace |
| `oikonome_month_fixed_spent_dollars`, `oikonome_month_days_left` | | bills paid so far; days left including today |
| `oikonome_headroom_dollars` | | checking − card debt − bills due (when checking is known) |
| `oikonome_checking_dollars`, `oikonome_card_debt_dollars` | | the cash runway inputs |
| `oikonome_net_worth_dollars`, `oikonome_net_worth_with_property_dollars` | | net worth |
| `oikonome_account_balance_dollars` | `account`, `institution`, `type`, `id`, `counted` | per account (a card's is what is owed); `counted="0"` is the second copy of a linked account |
| `oikonome_bills_due_7d_dollars`, `oikonome_bills_due_7d_count` | | unpaid, due in the next week |
| `oikonome_alerts_active` | `severity` | active alerts |
| `oikonome_connection_sync_age_seconds`, `oikonome_connection_ok` | `connection` (+ `aggregator`) | freshness and status per bank connection |
| `oikonome_transactions_total`, `_uncategorized`, `_pending` | | ledger counts |
| `oikonome_info` | `version` | the instance |

Scrape job:

```yaml
scrape_configs:
  - job_name: oikonome
    scheme: https
    metrics_path: /metrics
    authorization:
      credentials: oik_…
    static_configs:
      - targets: ["money.example.lan"]
```

Every 15–60 seconds is fine; the summary is cheap, and a read token is
rate-limited to 600 requests an hour per address. A ready-made scrape
config is in [`integrations/prometheus/`](../integrations/prometheus/).

## Webhooks

Settings → Integrations → **Webhooks** → **Add a webhook**: a name, the
URL, and the events you want. Creating one is a step-up action (your
password, or your second factor) because it points your ledger at a
server. The **signing secret** is shown once; put it in the receiver.

### The request

```
POST <your url>
Content-Type: application/json
User-Agent: Oikonome-Webhooks/<version>
X-Oikonome-Event: transactions.new
X-Oikonome-Delivery: <delivery id>      # stable across retries
X-Oikonome-Signature: t=<unix seconds>,v1=<hex>
```

```json
{
  "id": "<delivery id>",
  "event": "transactions.new",
  "created_at": "<ISO 8601 timestamp>",
  "attempt": 1,
  "data": { "...": "the event's payload, below" }
}
```

Answer with any **2xx** within ten seconds. Anything else — a 4xx, a 5xx,
a timeout, a refused connection — is retried after a minute, then five,
thirty, two hours, twelve hours, and then given up. Thirty failed attempts
in a row switch the webhook off; Settings says so, and the checkbox turns
it back on once the receiver is fixed. The **Send a test** button posts a
`test.ping` right away and shows what the receiver said; **deliveries**
lists the last twenty with their state.

### Verifying the signature

`v1` is HMAC-SHA256, hex, over `"<t>.<raw body>"` with the secret. Check
it against the raw bytes before parsing, and refuse a `t` more than a few
minutes old:

```python
import hmac, hashlib, time

def verify(secret: str, header: str, body: bytes, tolerance: int = 300) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(","))
    ts = int(parts["t"])
    if abs(time.time() - ts) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(parts["v1"], expected)
```

### Events

| Event | When | `data` |
|---|---|---|
| `transactions.new` | A sync brought rows into the ledger | `count`, `transactions[]` (up to 50: id, date, amount, payee, bank_text, account, account_id, category, pending) |
| `sync.completed` | A sync finished, rows or not | `connections[]` (id, name, ok, error), `new_transactions`, `ok` |
| `alert.raised` | An alert appeared on the Today page (or came back after clearing) | `kind`, `severity`, `message`, `link`, `date` |
| `connection.changed` | A bank connection broke or recovered | `connections[]` (name, from, to) |
| `report.daily` | The daily verdict, at the household's report hour — even with email, SMS and push all off | the whole `/api/integrations/summary` document |
| `networth.snapshot` | The nightly net-worth snapshot was recorded | `date`, `total`, `with_property`, `by_institution[]` |
| `activity.logged` | Someone in the household changed a category, bill, note, rule… | `actor`, `kind`, `action`, `target`, `label`, `summary` (the sentence), `detail` |
| `test.ping` | The test button | `message`, `webhook` |

Subscribe to `*` to receive every event, including ones added later.

### Home Assistant: a webhook trigger

Point the webhook at `https://<home-assistant>/api/webhook/<some-id>` and
use it as an automation trigger:

```yaml
automation:
  - alias: "Big charge just posted"
    trigger:
      - platform: webhook
        webhook_id: oikonome-txns
        allowed_methods: [POST]
        local_only: true
    condition:
      - "{{ trigger.json.event == 'transactions.new' }}"
      - "{{ trigger.json.data.transactions | selectattr('amount', '>', 200) | list | count > 0 }}"
    action:
      - service: notify.mobile_app_phone
        data:
          message: >
            {% for t in trigger.json.data.transactions if t.amount > 200 %}
            {{ t.payee }} ${{ t.amount }}{{ ", " if not loop.last }}{% endfor %}
```

Home Assistant's webhook trigger does not verify signatures; keep
`local_only: true` (or put it behind your reverse proxy's auth) if the
instance and the hub share a network.

## The local MCP server

[`integrations/mcp/oikonome_mcp.py`](../integrations/mcp/oikonome_mcp.py)
is a read-only [MCP](https://modelcontextprotocol.io) server over the read
endpoints, so an assistant can answer "how much can I spend today?" or
"what did we pay the dentist last year?" from your own books. One file,
Python 3.10+, no packages; it runs on the machine where the assistant runs
and talks to your instance over HTTP with a read token.

Add it to your client's MCP config (most take a JSON `mcpServers` entry):

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

Tools: `summary`, `accounts`, `transactions` (q, since, until, account,
category, limit, page), `spending` (months), `bills` (days), `alerts`,
`net_worth` (months). Each is exactly one GET against the table above —
there is nothing the server can do that the token cannot.

## What these doors never carry

No credentials (aggregator tokens, SMTP passwords, API keys), no other
household's rows (every query runs under the same row-level isolation as
the pages), and nothing a viewer could not already see. Webhook secrets
are stored encrypted under the household's key and are not part of the
data export; a restore into another instance does not carry your webhooks.
