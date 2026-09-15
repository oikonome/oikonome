# Deployment modes: self-hosted vs hosted

Oikonome runs in two modes:

- **Self-hosted** (the default): one household runs its own instance.
  `OIKONOME_HOSTED` is unset.
- **Hosted**: an operator runs the instance for other people. The operator
  sets `OIKONOME_HOSTED=1` (passed through to both the server and worker
  containers by `docker/compose.yaml`).

The server reads the flag from the environment
(`envnum.env_flag("OIKONOME_HOSTED")`, so `0`, `false`, `off`, `no` and
empty all mean self-host — only an affirmative value turns hosted mode on),
the login/setup templates through the `hosted()` Jinja global
(`server/oikonome/web/todayview.py`), and the SPA through the `hosted` field
of `/api/me`.

In one sentence: hosted mode assumes **accounts belong to people the
operator does not live with**. Households arrive by invite rather than
through the first-boot setup wizard, mailbox control is proved before mail
or access is granted, a second factor is mandatory, the operator console is
passkey-only, outbound requests to tenant-supplied URLs may not reach
private addresses, and the shared infrastructure the instance runs on is
not described to tenants.

An **installed add-on** (`server/oikonome/ext.py`) may add gating, intake
and console panes of its own on top of either mode; with nothing installed
the product gates nothing and caps nothing. See `DEVELOPMENT.md` →
*Extension points*.

**Rule: every new mode-dependent behavior adds its row to the table below
in the same commit that introduces it.** This table is the contract for
what diverges between the two modes — if it isn't listed here, the two
modes must behave identically.

| Behavior | Self-hosted | Hosted | Mechanism |
|---|---|---|---|
| Accounts | The instance is claimed once through `/setup` (gated by `OIKONOME_SETUP_TOKEN`) and stays one household; `/signup` 403s and `/` redirects to `/setup` or `/app/` | `/setup` is gone (redirect + 404); `/signup` creates households, invite-gated unless `OIKONOME_OPEN_SIGNUP=1` — and an invite is still honoured when the gate is open | `OIKONOME_HOSTED` / `OIKONOME_OPEN_SIGNUP` checks in `signup()`, `index()`, `setup_page()`, `setup_submit()`; `auth/signup_invites.py` |
| Email verification | Accounts are created verified; an email change keeps them verified | Accounts start unverified and a single-use link stamps `verified_at`; an email change re-verifies. An address whose earlier account was never verified can be reclaimed through the mailbox | `hosted` branches in `signup()` / the email-change route; `auth/signup_reclaim.py` |
| Household invite delivery | The mint returns a one-time link for the owner to pass along (and mails it when SMTP and `OIKONOME_BASE_URL` are set) | The mint requires an address, mails the link there, withholds the URL, and binds the claim to that address inside the burn | `OIKONOME_HOSTED` check in `invites.create()`; `invites.bound_address()` inside `claim()` |
| Second factor | Optional | Mandatory: a session holding neither TOTP nor a passkey may read but not write until one is enrolled | `OIKONOME_HOSTED` check in `current_user()` |
| Passkeys (WebAuthn) | Absent — every passkey route 404s, and a LAN instance on plain http has no secure origin to enrol one anyway | Available: register / login / list / delete, with step-up re-auth on posture changes | `_require_hosted()` on the passkey routes; the SPA additionally needs a secure context |
| Operator console credential | The operator token; optionally passkeys with the token as break-glass (`OIKONOME_ADMIN_REQUIRE_PASSKEY=1`), or no token at all | Passkey only — the token is refused as a credential whatever `.env` says, and only still acts as the on-switch | `adminconsole._token_login_enabled()`; `auth/admin_passkeys.py`; see `specs/admin-console.md` |
| Public-intake spam defense | Off (there is no public form); opt in with `OIKONOME_SPAM_DEFENSE=1` | On: a refreshed public blocklist plus a budgeted reputation lookup, with flagged addresses dropped silently | `OIKONOME_SPAM_DEFENSE` (defaults to `OIKONOME_HOSTED`), `web/spamdefense.py` |
| Outbound URLs a tenant supplies (LLM, SimpleFIN claim, SMTP host) | Private and LAN addresses allowed — a local model and a LAN relay are first-class; only the cloud metadata address is blocked | Every resolved address must be public, and the fetch pins DNS so a rebinding resolver cannot swap addresses between check and connect. The operator's own env-configured LLM backend stays exempt | `OIKONOME_HOSTED` checks in `web/netguard.py`; config provenance in `engine/llm_categorize.py` |
| Report email recipients | Free-form addresses (the operator owns the SMTP account) | Every recipient must hold an account on the tenant — the settings door refuses outsiders, restore drops them, the worker filters at send time. `OIKONOME_HOSTED_FREEFORM_RECIPIENTS=1` lifts it for a test instance | `web/mailguard.py`, `jobs/worker.py` |
| Which owner receives household mail | Always the owner, verified or not | Only a verified owner; an all-unverified tenant receives nothing | `OIKONOME_HOSTED` check in the worker's recipient resolution |
| Mail delivery configuration | Tenant SMTP settings win, `OIKONOME_SMTP_*` is the fallback, and the setup wizard offers an SMTP step | Same resolution order, but mail is normally the operator's env SMTP: the wizard omits the step and a tenant host that fails the address guard falls back to env | `report.resolve_smtp()`; the SPA's step list keyed on `me.hosted` |
| Bounce webhook | Off unless `OIKONOME_EMAIL_WEBHOOK_TOKEN` is set — a local relay has none to send, and the send path still detects a refused address on its own | Set, so a hard bounce is known when the relay reports it rather than one send later | unset ⇒ the route 404s |
| Unsent one-time links | A link the instance could not email is logged in full, so the operator can hand it over | Only a fingerprint and a "not emailed" note; the link never lands in a log line | `OIKONOME_HOSTED` check in `_log_link()` |
| Bring-your-own aggregator keys | Plaid / MX / SimpleFIN keys are saved per tenant | Refused on every keys door — connections run under the operator's credentials, and a restored connections bundle has its keys and items stripped | `_no_byo_on_hosted()`; `OIKONOME_HOSTED` check in `sync/connections_bundle.py` |
| Aggregator extra products (liabilities, holdings) | Requested at link time and pulled daily — the keys and the bill are the household's own; `OIKONOME_PLAID_PRODUCTS_EXTRA=none` opts out of both halves | Requested only when the installed gate or `OIKONOME_PLAID_PRODUCTS_EXTRA` names them, so a separately-billed product is never attached to an Item silently | `extra_products` in `sync/plaid.py` |
| Connect surface (SPA) | A provider comparison grid with bring-your-own-key setup flows | One "Connect your accounts" door (live when the operator's aggregator credentials are set — `/api/me.bank_link`), plus file imports and API scripts | `me.hosted` + `me.bank_link` in `components/ConnectHub.tsx` |
| Full database dump | Available to the owner of a single-tenant instance (`pg_dump`) | 403 — `pg_dump` bypasses RLS; the per-tenant export ZIP is the way out. A multi-tenant instance is refused even without the flag | `OIKONOME_HOSTED` check plus the real tenant count in `web/pages.py` |
| Settings and Doctor disclosure | SMTP fields, aggregator key fields and the env LLM backend are shown; Doctor reports database size, disk, memory and the SMTP host | Those fields are withheld and the shared-infrastructure rows drop out — Doctor says delivery is managed by the host | `OIKONOME_HOSTED` checks in `_settings_view()` and `web/doctor.py` |
| Feedback package contents | Includes the recent process log ring only while the instance demonstrably holds at most one tenant | Never includes it — the ring is process-wide and would carry other households' lines; the tenant-scoped doctor bundle stays | `feedback._single_tenant_instance()` |
| Operator notice on a new household | None | One email per new household (the address and the household id), sent off the request so a slow relay never fails a signup | `OIKONOME_OPERATOR_EMAIL` + SMTP; `_notify_operator_signup` |
| `/api/me` `hosted` flag | `false` | `true` — the one switch the SPA keys every client-side divergence on | `OIKONOME_HOSTED` read in the `/api/me` handler |

Note: **demo mode is not a deployment mode.** A demo instance is a
per-tenant flag (synthetic data, disabled data and security doors) and
behaves the same under either mode; it has no `OIKONOME_HOSTED` dependency.
