# Home Assistant

Two ways in, both in [`configuration.yaml`](configuration.yaml):

- **REST sensors** poll `/api/integrations/summary` with an integration
  token (Settings → Integrations → Integration tokens) — dollars left
  today, the verdict, bills due this week, net worth, and one balance per
  account.
- **A webhook trigger** receives `transactions.new` (or any event) from a
  webhook you add in Settings → Integrations, pointed at
  `https://<home-assistant>/api/webhook/oikonome`. The example automation
  notifies your phone about any charge over $200.

Replace `money.example.lan` with your instance and `oik_…` with the token.
Home Assistant's webhook trigger does not check signatures, so keep
`local_only: true` when the hub and the instance share a network, or put
the hub behind your reverse proxy's authentication. Full reference:
[docs/integrations.md](../../docs/integrations.md).
