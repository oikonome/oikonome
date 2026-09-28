# Development

How to set up a dev environment, run the tests, and understand the shape of
the codebase — the stack, tenancy, and the money conventions every change
must keep. For running an instance, see `README.md` and
`docs/quickstart.md`.

Oikonome is a self-hostable personal-finance app built around a **daily
verdict email**: are you on budget today, and what's the one number to hit.

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
integrations/        outbound: the local MCP server, Home Assistant and Prometheus examples
specs/               design notes for shipped features
```

## Prerequisites

- Python 3.14 (what the container ships; 3.12+ runs the suite), Node 22+, and Docker **or** Podman (with Compose).
- A local Postgres 18 for the test/dev database.

## Set up

```bash
# a throwaway Postgres for tests + the dev server
podman run -d --name oikonome-dev-postgres -p 127.0.0.1:5433:5432 \
  -e POSTGRES_PASSWORD=devpass postgres:18-alpine

# server deps
cd server && python -m venv .venv && .venv/bin/pip install -e .

# apply the schema
.venv/bin/python -m oikonome.db.migrate
```

## Everyday commands (see `Makefile`)

| Task | Command |
|---|---|
| Full test suite | `make test` |
| Security + correctness lint only | `make lint` |
| Dev server (inner loop) | `make dev` |
| Build the SPA | `make spa` |
| Container smoke test | `make container-check` |
| Fresh-install test | `make install-test` |

The dev server runs with `OIKONOME_DEV=1` (relaxed cookie flags for
`http://localhost`). Point a browser at the port it prints; the SPA is at
`/app`.

## Tests

The suite is `unittest`-style against a **real Postgres** (`oikonome_test`,
created automatically). Each test gets a fresh tenant, so the whole run is
also a continuous tenant-isolation check — a cross-tenant leak makes
unrelated tests fail.

```bash
cd server && .venv/bin/python -m unittest discover -t . -s tests
```

Override the DB with `OIKONOME_TEST_ADMIN_DSN` / `OIKONOME_TEST_DSN` if your
Postgres isn't at `127.0.0.1:5433`.

**Write the failing test first, then the fix.** The suite is the spec; don't
weaken a test to make a change pass.

### The lint gate

`make test` runs `make lint` first — ruff with **F** (pyflakes) and **S**
(flake8-bandit security) selected. It is not a formatter: no style rules are
enabled and there is no `ruff format` step, because reformatting working code
would bury the signal the gate exists to carry.

The S ruleset is selected wholesale so rules nothing here has tripped — `eval`,
`pickle`, `yaml.load`, `shell=True`, `verify=False`, hardcoded passwords,
unverified SSL contexts — stay at zero without anyone adding them one by one.
The handful of ignored rules are listed with reasons in
`server/pyproject.toml`; each is a debit, not a dismissal. Some of what the
ruleset catches is invisible on the page — stdlib XML parsers expand entities
unbounded, which is why `tests/test_inventory_xml_safety.py` exists alongside
it.

Install it with `server/.venv/bin/pip install -e 'server[dev]'`. If it is
missing the target warns and continues, so a fresh checkout still gets a
working `make test`.

### Inventory tests — the guards that check the other guards

The `tests/test_inventory_*.py` files assert properties over the whole
codebase rather than over one behaviour:

| File | Invariant |
|---|---|
| `test_inventory_rls.py` | every table with `tenant_id` has RLS, or is a named control-plane exemption |
| `test_inventory_stepup.py` | every credential-shaped route requires step-up auth, or is exempted with a reason |
| `test_inventory_demo.py` | every data/security door refuses a demo tenant, or is exempted with a reason |
| `test_inventory_xml_safety.py` | no module parses user XML with stdlib `ElementTree` |
| `test_inventory_account_kind.py` | `kind` is the composite `type/subtype`, and nothing compares it with bare equality — a `kind === "depository"` test matches nothing, ever, and fails silently |
| `test_inventory_role_permissions.py` | every mutating route has decided what a household MEMBER may do with it — not deciding defaults to permissive |

They exist because these guards are applied route by route and table by
table, and the failure that actually happens is *route 168* — the new one
that nobody remembered to guard. Reading the routes by hand finds them
too, slowly and probabilistically; these files do it on every run.

Each file holds a **pinned** set (things guarded today, which may not
silently lose their guard) and a **pattern** rule (new routes matching a
sensitive shape must be guarded or explicitly triaged). When one fails,
the fix is usually to add the guard — not to add the route to the
exemption list. Exemptions require a written reason and the tests check
that the reason is non-empty and that the route still exists.

### Never run two suites against one test database

The control-plane tables (`users`, `sessions`, `password_resets`, …) have
**no row-level security** — they are global to the test database. Two suites
running at once therefore see each other's rows and fail in
`test_auth_hardening` / `test_passkeys` in ways that look exactly like a
broken diff. If you get a non-deterministic failure there, **re-run alone
before suspecting your change.**

`make test` refuses to start when another suite is already running
against your database (`scripts/test-db-guard.sh`). Override with
`OIKONOME_TEST_ALLOW_CONCURRENT=1` only when you know the other run targets
a different one.

To actually run in parallel, give each checkout its own database:

```bash
export OIKONOME_TEST_DB=oikonome_test_myname
make test
```

### The test database is dropped at the start of every run

A test creates a fresh TENANT and never deletes it — RLS is the isolation,
so nothing has to be cleaned up for correctness. But the database outlives
the run, so rows pile up across runs, and the control-plane tables have no
tenant filter to shrink the scan: left alone, the default `oikonome_test`
grows to millions of rows and the suite slows to a crawl that looks like a
hang. So `make test` drops it first
(`scripts/test-db-fresh.sh`) and `tests/util.py` recreates it — the full migration
set takes a couple of seconds, which is cheaper than one slow test, and every
run starts from the schema a fresh clone gets.

The drop happens at the START, so a failed run's database is still there to
inspect. `OIKONOME_TEST_KEEP_DB=1 make test` keeps whatever is there.

Databases belonging to worktrees that no longer exist are not dropped
automatically — one may be a checkout you come back to. Sweep them when you
want the disk: `make test-db-prune` lists what it would drop,
`make test-db-prune ARGS=--yes` drops it.

## Working on two things at once

**One checkout, one writer.** Two checkouts pointed at one test database
collide: the control-plane tables have no row-level security, so each run
sees the other's rows and the failures land in `test_auth_hardening` /
`test_passkeys` looking exactly like a broken diff. One index also means
one staging area, so a broad `git add` in a shared tree stages work that
belongs to the other change.

So give each line of work its own checkout and its own database:

```bash
make worktree name=money      # ../oikonome-wt-money on branch wt/money
cd ../oikonome-wt-money
source .envrc                 # exports OIKONOME_TEST_DB=oikonome_test_money
make test
```

Each worktree gets its own index, HEAD, branch, and test database, sharing
one object store — so merges are ordinary git and staging can only ever
pick up your own work.

`make test` refuses to start while another suite is already running against
the same database, so the collision fails loudly instead of silently.

One further caution: **scope by subsystem, not by file.** Nearly every
change here touches `api.py`, `report.py` and the SPA, so two parallel
changes in genuinely separate subsystems are fine and three inside one are
usually negative-value — serialising costs minutes, colliding costs
correctness confidence.

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

## Two deployment modes, one codebase

The same tagged release runs two ways, differing only in deployment config:

- **Self-hosted** (the default): `git clone` + `./oikonome.sh install`
  (Docker/Podman Compose; everything — code, config, database — lives in one
  folder), one household per instance.
- **Hosted** (`OIKONOME_HOSTED=1`): an operator runs the instance for other
  people — the same image behind a managed Postgres and a TLS proxy.

Every self-hosted-vs-hosted behavior divergence is cataloged in [docs/deployment-modes.md](docs/deployment-modes.md) — mode-dependent changes add their row there in the same commit.

`main` is the development line; **tagged releases (`v0.x`) are what people
install.** Run `./oikonome.sh https <domain>` to front an instance with
automatic-cert TLS (Caddy).

### Extension points

An operator who runs the product for other people layers their own
concerns on top — gating, extra routes and jobs, extra console panes — as a
separate Python package that registers through the entry-point groups in
`server/oikonome/ext.py`. The product itself never imports such a package
by name: it asks `ext.gate` its policy questions (`tier_allows`,
`institution_cap`, `write_blocked`, `provision`, …) and, with nothing
installed, gets the defaults a household running its own instance expects —
nothing gated, nothing capped, every feature allowed. The
web app includes any registered routers, the worker appends registered
jobs to its cron table, `migrate` applies registered migration
directories after its own, and the Jinja environment searches registered
template directories. Discovery never raises: a broken add-on logs and
the instance runs without it.

The SPA has one slot of its own, `webapp/src/ext/index.tsx`: an `ext`
object the pages read for the account section an add-on owns in Settings,
extra cards under the household roster, lockout screens matched on the
server's frozen-account sentence, the read-only (402) toast, the wording
around an institution allowance, and the operator's legal links. The
tracked stub renders nothing. An operator drops their own copy of that
directory in `extras/webapp-ext/` and the image build puts it in the slot
before the client is built (`docker/Dockerfile`); the shape must match the
stub's, since nothing else in the product imports from it.

The mobile app has the same slot at `mobile/src/ext/index.tsx` (build
flavour flags, extra screens, the Settings account section, lockouts, the
402 dialog, allowance wording, legal links, the passkey hosts, the
operator's site), and its store identity — bundle ids, EAS project, signing
— is placeholders in `mobile/app.json` / `mobile/eas.json`. An operator's
build tooling copies its own `ext/` over the slot, adds its screens under
`src/app/`, and merges its identity into those files before `eas build`;
`mobile/README.md` → "Building your own" says what to change.

## Releases

A change reaches a running instance through the same gates at any scale:

1. **Tests green.** `make test` locally; the GitHub Actions `ci` workflow
   runs the full suite against a real Postgres, applies migrations, and
   audits dependencies for CVEs on every pull request.
2. **Install a tagged release.** Every shipped version is a tag
   (`vA.B.N`); install or upgrade to one, never an arbitrary commit.
3. **Verify live.** The instance must answer `/readyz` and report the
   expected version.
4. **Roll back if needed.** Redeploy the previous tag and re-verify
   `/readyz`.

## Making a change

1. Write/adjust a test that fails without your change.
2. Implement it; keep the money conventions above.
3. `make test` green, and if you touched the SPA, `cd webapp && npm run build`.
4. If your change affects end users, sweep the docs in `docs/` (ports,
   commands, feature availability) — stale docs are a bug.
5. Commit. See `CONTRIBUTING.md` for PR expectations.

## Versions, and how releases get published

Versions are `vA.B.N` where **N = commits since that series' `vA.B.0` anchor
tag**. `git describe --tags --match 'v[0-9]*.[0-9]*.[0-9]*'` reports the
nearest version tag and the distance past it (`vA.B.C-M-gSHA`), and
`scripts/version-stamp.sh` collapses the two by adding: `vA.B.(C+M)`. Both
halves are load-bearing:

- The `--match` glob keeps `describe` on version tags only, and matches
  **any** series — so starting a new series is one `vA.B.0` tag and nothing
  else. Never reintroduce a plain `git describe --tags`, and never pin the
  glob to one series.
- The patch is BASE + commits-since, not commits-since alone: shipped
  versions carry their own `vA.B.N` tag, so `describe` usually lands on one
  of those rather than on the anchor, and a base-blind count would restart
  the numbering.

`install.sh` and `oikonome.sh` both source that one file so they cannot
disagree about what an instance is running; `server/tests/test_version_stamp.py`
exercises it.

A stamp built from a **modified** checkout keeps its `-dirty` marker through
the collapse — `vA.B.N-dirty`, not `vA.B.N`. You will see it on a dev instance
deployed from an uncommitted tree, and it is the honest answer: that build
corresponds to no commit. The match is also end-anchored, so any other suffix
is left alone rather than collapsed into a clean-looking version.

Each commit in this repository is a release (see `HISTORY.md`), tagged with
the version an instance built from that tree reports.

- To see what changed in a version, read its commit and its tag, or the
  Doctor page of a running instance.
- The version-history panel in the admin console reads
  `server/oikonome/version_history.tsv`, a manifest baked into the package
  because the image carries no `.git`. It is **generated and gitignored**
  (`make version-history`, and `install.sh` refreshes it on every
  install/upgrade); the generator leaves an existing manifest alone in a
  clone that has no anchor tag. Commits below a later series' anchor keep
  their version — a new series never renumbers released history.

## Migrations

Numbered SQL under `server/oikonome/db/migrations/`, applied idempotently
(advisory-locked) at app start and by `python -m oikonome.db.migrate`. Add a
new numbered file; never edit a shipped one.
