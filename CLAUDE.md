# Oikonome — contributor guide

Oikonome is a self-hostable personal-finance app built around a **daily
verdict email**: are you on budget today, and what's the one number to hit.
This file orients humans and AI assistants working in the codebase. For
running an instance, see `README.md` and `docs/quickstart.md`.

## Stack

- **Server**: FastAPI + PostgreSQL 18 (row-level security for tenancy) +
  psycopg 3 (plain SQL, no ORM) + arq/Redis for background jobs.
- **Web**: React + Vite + TypeScript SPA (`webapp/`), served by FastAPI at
  `/app`. Consumes `/api/*`.
- **Packaging**: Docker/Podman Compose; `./oikonome.sh` is the one manager
  entry point (install/upgrade/status/logs/backup/restore/reset/uninstall).

## Layout

```
server/oikonome/     the app
  db/                schema, migrations, tenancy (RLS), envelope crypto
  auth/              argon2 passwords, sessions, TOTP, passkeys, invites
  engine/            budget/verdict, forecast, recurring, reporting, links…
  sync/              aggregators (Plaid/MX/SimpleFIN) + file importers
  jobs/              arq worker (hourly sync, nightly detect, email sweep)
  relay/             standalone push relay (run by the app's publisher; not an instance)
  web/               FastAPI app, /api routes, Jinja shell, setup wizard
  tests/             pytest-style unittest suite (real Postgres)
webapp/              React SPA
mobile/              Expo/React Native app (specs/mobile.md)
docker/              Dockerfile + compose + Caddy (tls profile)
docs/                end-user docs
community-scripts/   example host-side data collectors
specs/               design notes for shipped features
```

## Dev environment

```bash
# postgres:18 on 127.0.0.1:5433 (postgres/devpass)
podman run -d --name oikonome-dev-postgres -p 127.0.0.1:5433:5432 \
  -e POSTGRES_PASSWORD=devpass postgres:18-alpine
cd server && .venv/bin/python -m oikonome.db.migrate
make test                  # full suite — must stay green
make dev                   # uvicorn inner loop (OIKONOME_DEV=1)
```

Tests create a fresh tenant per test in a shared `oikonome_test` database, so
the suite doubles as a continuous check of tenant isolation. **The suite is
the spec — never weaken a test to make a change pass; fix the change.**

See `DEVELOPMENT.md` for the fuller workflow and `Makefile` for all targets.

## Multi-tenancy (read before touching the DB)

- Every domain table has RLS keyed on `current_setting('app.tenant_id')`.
  Get a tenant-scoped connection with `tenancy.tenant_connect(tid)` — ported
  engine SQL then needs zero tenant plumbing (tenant_id is ambient + RLS).
- Control-plane tables (`users`, `sessions`, `invites`, `passwords`) have no
  RLS — use a plain app-role connection, and be careful: an unscoped bug
  there is global.
- `tenancy.admin_connect()` **bypasses RLS** — migrations and tenant
  lifecycle only.
- Secrets (aggregator tokens, API keys) are encrypted at rest with a
  per-tenant envelope (`db/crypto.py`) under `OIKONOME_MASTER_KEY`.

## SQL / psycopg conventions

Plain SQL, `%s` params, dict rows. Gotchas that bite:

- **`with conn:` CLOSES a psycopg connection** — use
  `with conn.transaction():` for a transaction block.
- `INSERT … ON CONFLICT (tenant_id, id) DO UPDATE` for upserts.
- Wrap dict values written to JSONB columns in `jsonb(...)`; a literal `%`
  in SQL that also takes params must be `%%`.
- `tenant_id` never appears in engine SQL — the ambient setting + RLS own it.

## Money conventions (violating these is a regression, not an opinion)

- **Sign convention: positive = money out** (Plaid's convention). Display
  layers flip.
- Effective category = `COALESCE(category_override, category_primary)`;
  user overrides are sacred, sync never touches them.
- Spend excludes `LOAN_PAYMENTS_CREDIT_CARD_PAYMENT` and `TRANSFER_*`
  (double-count prevention). Mortgage/loan payments DO count.
- **The verdict is variable-spend only** — fixed bills never make you "over".
- One transaction per recurring-bill occurrence. A bill carries an
  IDENTITY — the merchants it pays, by display name (`raw.merchant_names`,
  `budget.bill_merchants`) — and its charges are the ledger's rows under
  those merchants, within `budget.bill_tolerance`. Text-token matching
  (every token must appear) is the rule only for a bill with no identity;
  `budget.bill_displays` is discovery, which bootstraps a new bill and
  feeds approve-gated attach proposals nightly. Envelope bills are
  cap-only, overflow → variable.
  An envelope's cap is a pool over `envelope_months` — 1 (the month) or 12
  (the calendar year, for lumpy annual costs); the plan always carries
  pool ÷ months.
- Multi-source accounts: linked sources feed one real account; the
  healthiest live source serves data, the rest are shadow-excluded from all
  money aggregates so nothing double-counts.
- Reimbursement is EXPLICIT pairing only, never a pattern rule.

## Conventions

- **Every feature lands in BOTH clients — web and mobile — in the same
  change.** The SPA (`webapp/`) and the native app (`mobile/`) are two
  views of one product: a page, control, or wording change in one is
  ported to the other before the work is called done, matching the web's
  naming, colors, and semantics (mobile copies web, never invents).
  Server/API changes reach both for free; UI is mirrored by hand. Mobile
  code reaches phones only via a new build, which ships on its own
  cadence — parity is a code requirement, not a deploy requirement.
- **Role gates go through one helper per client** — `webapp/src/role.ts`
  (`canEdit`/`isOwner`/`isViewer`) and `mobile/src/lib/viewer.ts`
  (`useViewer`/`useOwner`). Never write `me.data?.role !== "viewer"`
  inline: the optional chain reads TRUE while `/api/me` is in flight, so a
  viewer sees owner controls until the query lands. **Unknown is not
  privileged** — until the server says who you are, you are the
  least-privileged reader. There are three roles: `canEdit` (owner or
  member) gates editing chrome; `isOwner` gates the ACCOUNT — bank
  connections, exports and restores, the household roster, the account
  section an installed add-on owns, deleting the account. Asking the wrong
  one is a bug in both directions.
  The policy both clients render is `server/oikonome/web/permissions.py`;
  the reasoning is `specs/household-roles.md`.
- **The daily email mirrors the Today page — always.** The email HTML is
  `templates/today_body.html` + the plain-text mirror in `web/report.py`;
  the SPA page is `webapp/src/pages/Today.tsx`. Any change to the Today
  page's layout, sections, wording, or number formatting MUST land in the
  email render (template, `todayview.build_context`/`money_map`, and the
  plain text) in the same change. `todayview.money_map` mirrors
  `MoneyMap.tsx`/`planRows()` line for line; `test_email_render.py` guards
  the pipeline.
- Tests before commit: `make test`.
- Match the surrounding code's style; comments explain *why*, not *what*.
- The repo contains **synthetic fixtures only** — never commit personal data,
  real account details, or secrets.

