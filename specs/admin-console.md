# Hosted admin console

The operator's surface: answer "who's on the box and is their sync
healthy" and run support actions without psql.

## Access model

- **Off by default.** Every `/admin/console` route 404s unless
  `OIKONOME_ADMIN_TOKEN` is set (docker/.env; `openssl rand -hex 32`).
- **Own credential, own session.** The credential is an operator
  **passkey** (below); the token is break-glass (constant-time
  compare, 5/h rate limit). Either way success mints an admin session
  cookie — separate name (`oikonome_admin`), `path=/admin`, HttpOnly,
  SameSite=Strict, 4 h TTL, sha256 at rest in `admin_sessions`. Tenant
  sessions can never reach the console; the console holds no tenant
  session.
- **Optional IP allowlist.** `OIKONOME_ADMIN_IPS` (comma-separated
  CIDRs, resolved through the OIKONOME_TRUSTED_PROXIES client-IP policy)
  cloaks the console with 404 outside the list. The cloak is a router
  dependency so it fires before form validation (an in-handler check would
  answer 422 on cloaked instances).
- **Admin-role-only tables.** All console reads/writes run on
  `admin_connect()`; the app role has NO grants on `admin_sessions` /
  `admin_audit` (migration 026 + migrate.py re-grant block), so SQL
  injection through the tenant app can neither forge an operator
  session nor scrub the audit trail.

## Surface

Dashboard (single server-rendered page, `templates/admin.html`):

- **Fleet**: per-tenant row — owner email, created, status, user count
  (+unverified badge), account/txn counts, last active (max session
  `last_seen`), last sync, sync errors in 24 h. One grouped scan per
  table (not per-tenant correlated subqueries), capped at 500 rows. One
  operator column beside the account count: **Plaid** — the Items held
  under the instance's own Plaid credentials (`plaid_item_ledger`, not
  `items`, so an Item left behind without an `items` row is still
  counted).
- **Job queue depth** (arq zset in Redis; renders "unreachable" rather
  than erroring when Redis is down).
- **Invite mint** (email + optional note) — link rendered once and
  emailed; outstanding invites listed.
- **Unverified users** (latest 50) with re-send verification.
- **Audit trail**: last 20 `admin_audit` rows. Every state-changing
  action (login/logout/invite/approve/resend) writes one.

An installed add-on (`server/oikonome/ext.py`) may register **further
panes and actions** of its own — the console renders whatever the entry
points give it, and the product's own surface is the list above.

## Ending an account, and the data that leaves with it (migration 051)

- **Delete-with-grace.** The console delete defaults to a scheduled
  deletion: the tenant flips to `status='pending_delete'` + `delete_after`
  (= now + `OIKONOME_DELETE_GRACE_DAYS`, default 7) and is frozen exactly
  like suspension (403 on every request but logout, with a "scheduled for
  deletion — contact support to cancel" message). The nightly worker
  (`worker.purge_scheduled_deletions`) runs the real irreversible wipe once
  the window lapses, via the same `_purge_tenant` path (external-service
  release + audit) the immediate route uses. An operator can **restore**
  (cancel → active) any time before purge. An explicit **delete now**
  (`mode=immediate`) purges at once, unrecoverably, for the abuse case;
  `OIKONOME_DELETE_GRACE_DAYS=0` makes every delete immediate.
- **Portable data export.** `POST /tenant-export` streams a ZIP of the
  tenant's data as JSON (one file per tenant-scoped table + a manifest),
  read through the tenant's RLS connection so it can't cross tenants.
  Secrets (tokens, TOTP seeds, password hashes) and file blobs are excluded
  by design (blob rows carry metadata only). Always audited, and refused
  (`tenant_export_refused`) when no live support consent is on file. Module: `server/oikonome/tenant_export.py`.
- **Consented data access.** The console shows NO tenant data by default.
  A tenant grants a **time-boxed, revocable** window from Settings →
  Support access (`support_consents` table; owner-only; 1–168 h). The
  drill-down surfaces the live consent state; data-touching operator
  actions are refused without it and audited either way.

## Operator credentials

A single shared secret that reaches the **root host-agent** (`restore`,
`reset`, `uninstall`, reboot) is unattributable, un-rotatable without an
edit + restart, phishable, and plaintext on the host. Three layers
(migration 072) guard that door instead, in value order:

1. **Operator passkey as the credential.** Own `admin_credentials` table
   on the admin role (`auth/admin_passkeys.py`) — deliberately not the
   tenant `passkeys` table, which FKs to `users`. Resident key + user
   verification both REQUIRED, enforced on the signed response, so console
   sign-in is usernameless and the key itself proves the human. Login uses
   the discoverable-credential flow with **no allow-list**: publishing
   credential ids to an unauthenticated caller would hand a prober the
   operator's key inventory. Every enrolment gets its own random user
   handle, so a second key on the same authenticator does not overwrite
   the first. `admin_sessions.credential_id` records which key signed in
   and **every `admin_audit` row carries an `actor`** — `passkey:<label>`
   or `token`.
2. **Step-up on the destructive host commands.** `restore`/`reset`/
   `uninstall` arm a single-use nonce + a typed confirm behind the session
   cookie, and also require a **fresh assertion**
   minted seconds earlier (`/passkey/stepup`), single-use, hashed at rest,
   and bound to the session that minted it — so a stolen cookie, and a
   ticket taken from another browser, both fail. Removing an operator key
   needs one too (that is the downgrade path back to token-only), and the
   last key cannot be removed while the require-flag is set.
3. **Cloudflare Access verified at the origin** (`web/cfaccess.py`). With
   `OIKONOME_CF_ACCESS_TEAM` + `_AUD` set, every console request must
   carry a valid `Cf-Access-Jwt-Assertion` (RS256 against the team JWKS,
   issuer, audience, expiry) or gets the same 404 as any probe. An edge
   policy binds nothing if the origin is directly reachable. No new
   dependency — an Access token is a plain JWT and `cryptography` is
   already required. **Fails closed**, including on a JWKS fetch failure;
   the escape hatch is unsetting the two vars and restarting.

**Hosted is passkey-only by construction; self-host keeps the token.**
The split is `OIKONOME_HOSTED`, not operator discipline: hosted is
passkey-only, and the operator token stays available for non-hosted
instances. A hosted instance has a real domain and a cert, so it can
always enrol a key and `oikonome admin-enrol` covers bootstrap and lost-key
recovery — a shared secret there is a phishing target that buys nothing. A
self-host on plain `http://` over the LAN has no secure context and can
enrol nothing, so taking its token away would leave it with no console at
all.

So `_token_login_enabled()` returns **False whenever OIKONOME_HOSTED is
set**, whatever is in `.env`. A token on a hosted box still switches the
console ON but can never open it. The tidy hosted configuration is `OIKONOME_ADMIN_ENABLED=1`
with no token at all — nothing to phish, nothing in plaintext on the host.

Self-host postures:

1. **Token only** — the default, and the only thing a LAN self-host can do.
2. **Token as break-glass** — `OIKONOME_ADMIN_REQUIRE_PASSKEY=1` for a
   self-host that does have TLS. The flag is ignored while zero keys are
   enrolled: bootstrapping is token → enrol → flip, and enrolment needs a
   session, so honouring it on an empty table would brick a fresh box.
3. **No token at all** — `OIKONOME_ADMIN_ENABLED=1` with no
   `OIKONOME_ADMIN_TOKEN`.

The login route checks `_token_login_enabled()`, never the raw env,
because `compare_digest(_h(""), _h(""))` is **true**: an unset token must
never reach the compare. With a tokenless posture available, `_enabled()`
alone does not keep that branch unreachable.

**Recovery is host-issued, and posture decides which.** Under 2, removing
the credentials restores the token (`oikonome admin-keys --reset`, no
restart). Under 3 that recovers to *nothing*, so the answer is
`oikonome admin-enrol`: a single-use 15-minute ticket, hashed at rest,
printed as a `/admin/console/enrol?t=…` link that enrols exactly one key
with no session. It is also how the FIRST key gets enrolled on a console
that never had a token. Mintable only from inside the container — the
authorization boundary is "you already control the host", the same one
`reset-password` uses.

The session TTL is **4 h** because this session reaches the host.

Tests: `server/tests/test_admin_console.py` (gating, cloaking,
sessions, actions+audit, app-role privilege denial) +
`server/tests/test_account_erasure_export_and_consent.py` (export shape +
secret exclusion, consent lifecycle, scheduled-purge windowing, grant/revoke
API)
+ `server/tests/test_admin_passkey.py` (enrol/sign-in, token
refused under the flag, step-up demanded and single-use and
session-bound, actor on the audit rows, Access token forgeries, and the
token-only self-host path).
