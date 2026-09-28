# Prometheus

[`prometheus.yml`](prometheus.yml) is a scrape job for your instance's
`/metrics` — the month verdict, dollars left today, balances per account,
bills due, alerts and connection freshness as gauges. Replace
`money.example.lan` and `oik_…` (an integration token from Settings →
Integrations). The metric catalogue is in
[docs/integrations.md](../../docs/integrations.md).

A few queries worth a Grafana panel:

```
oikonome_left_today_dollars
oikonome_month_spent_dollars / oikonome_month_budget_dollars
sum(oikonome_account_balance_dollars{type="depository",counted="1"})
max(oikonome_connection_sync_age_seconds) / 3600
oikonome_verdict == 1
```
