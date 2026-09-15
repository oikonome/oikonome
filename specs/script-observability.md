# Community-script observability (spec)

Scripts panel in the web UI: last push per collector, staleness warnings,
opt-in stale alerts — via push-heartbeats. The scripts themselves stay
host-side; the product only ever sees their pushes.

Decisions:

1. **Heartbeats are recorded server-side, automatically.** The product's
   sync entry points stamp a heartbeat row on every successful push —
   zero required changes to collectors; any script that writes through a
   product door gets observability for free. Scripts that write raw SQL
   through the container-exec door can add a one-line
   `heartbeat.stamp(conn, source, rows)` themselves (the community-script
   examples do).
2. **Staleness is per-script expected interval.** Default derivation:
   a source that pushes on a second, different calendar day within 3
   days of its previous push is a recurring collector → auto-watched at
   24h (a manual CSV import repeated a month later never auto-watches). Warn at 2× the interval
   missed (nightly script silent >48h → stale). `expected_hours` is
   editable per script in the panel; 0 = never warn (one-time imports
   stay quiet — and they never auto-watch in the first place).
3. **Alerts are email, opt-in, edge-triggered.** One email when a
   watched script crosses its threshold (worker nightly sweep, existing
   `report.send` SMTP path), not repeated daily; `alerted_at` guard
   resets when the script pushes again, so a later relapse re-alerts.
   The schedule config can absorb cadence options later.
4. **UI: a "Data collectors" section on the Doctor page** (Doctor is the
   health surface). One row per script: label, last push age, rows,
   fresh/stale badge, interval editor, alert toggle.

## Mechanics

- `script_heartbeats` (migration 009, mirrored in schema.sql, RLS like
  every domain table): PK (tenant_id, source), label, first_push,
  last_push, last_rows, expected_hours (NULL = undecided → auto-derive;
  0 = off), alerts (0/1), alerted_at.
- `oikonome/sync/heartbeat.py` → `stamp(conn, source, rows, label)`:
  upsert; on update refresh last_push/last_rows, keep the freshest
  non-null label, clear alerted_at, and auto-watch (NULL→24) when the
  previous push was on an earlier calendar day.
- Stamp call sites: `base.upsert_transactions` (per item via the txns'
  accounts; simplefin/plaid items excluded — the worker's bank-sync
  check owns those), `plan_csv.import_csv`. Container-exec bridges and the
  community-script examples call stamp explicitly.
- Doctor `checks()` gains one row per *watched* source (warn severity);
  the panel itself is `GET /api/doctor/scripts` +
  `PATCH /api/doctor/scripts/{source}` {expected_hours, alerts}.
- Worker: stale-alert sweep piggybacks the nightly job; edge-triggered
  per the alerted_at guard, recipients/sender per the daily email path.
