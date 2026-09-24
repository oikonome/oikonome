# Security policy

Report vulnerabilities privately — do NOT open a public issue.

Email: **security@oikonome.com** (or use GitHub's private vulnerability
reporting on this repository).

Commitment: first response within **48 hours**; coordinated disclosure
after a fix ships. In-scope: the server, the importers, tenant isolation,
token handling. Out of scope: your own deployment's misconfiguration
(the /doctor page is the first stop), third-party aggregators.

The stack runs least-privilege by default: app and worker containers run
as a non-root user and connect with roles that cannot do DDL or reach
superuser — the postgres superuser credential exists only in the one-shot
migration container.

Hardening notes for deployers: `OIKONOME_MASTER_KEY` is required by the
compose stack (aggregator tokens, webhook signing secrets, TOTP seeds and
the instance's web-push signing key encrypt at rest under it — the installer generates it; keep a
copy, restores need the same key; backup + rotation procedure:
[docs/master-key.md](docs/master-key.md)), run behind TLS, keep the compose
Postgres unexposed, enable TOTP on your account.

Two doors are worth knowing about because they read the whole household at
once. The **full export** and the **database dump** are not plain links: each
download needs a one-shot ticket minted by a fresh step-up (password, or a
live code where a second factor is enrolled) and expires within a minute, so
a stolen signed-in tab cannot quietly stream them. The **continuity
packet** (a PDF of every account, debt, bill and business) takes the same
fresh step-up, to download or to email. The **operator console**
(`/admin/console`) is off unless you enable it, and the way back in after
losing its passkey is a command inside the container — see
[docs/troubleshooting.md](docs/troubleshooting.md).

Some credentials are made for programs rather than people, and each opens
as little as its job needs. A **push** script token (`oik_`) can only
import rows. A **read** script token — the integration token behind
`/metrics`, the Home Assistant sensors and the local MCP server — can only
`GET` `/api/integrations/*` and `/metrics`, rate-limited per address; it
cannot import, change a setting or see a credential. A phone's
**widget token** (`oikw_`) opens exactly one door, today's verdict and
allowance, and dies with the device sign-in it hangs off. The three tokens
are stored only as a hash (the script tokens are shown once, when minted).
A password change ends every one that person made, and removing a member
or owner from the household — or their leaving it — ends the script tokens
they made. A **webhook signing secret** (`oikwh_`) is the other direction: it is
shown once, stored encrypted under the household's key, left out of the
data export, and signs every delivery (HMAC-SHA256 over timestamp and
body) so the receiver can refuse a forgery or a replay. Minting a script
token and adding a webhook are step-up actions. The one-tap buttons in the daily
email are signed links for one action on one object, addressed to the
recipient, and expire after seven days. Pressing one is a write like any
other: the address must still hold an owner or member account in good
standing — a suspended, deleting or read-only household, or a hosted
account with no second factor, gets a refusal — and a button whose object
has changed since the mail went out does nothing. They (and the
unsubscribe link) are keyed from `OIKONOME_MASTER_KEY`, so rotating the
key voids the ones already sent.

A mailed password reset strips every second factor, so on an account with
one enrolled it demands a recovery code as well — inbox control alone must
never equal account ownership. The way back in for someone who has lost the
password, the authenticator and the codes at once is therefore deliberately
slow, and there are only two: a **self-serve reset with a seven-day
cooling-off period**, announced to the account on every channel it has with
a one-click cancel link good for the wait it announces (and cancelled by
any sign-in), and an **operator clear**
— console action or `./oikonome.sh clear-2fa <email>` — which the operator
runs only after checking identity out of band. Both void TOTP, passkeys and
recovery codes, evict every session and device, and mail the account that
it happened; neither changes the password.

Request bodies are bounded before they are read, not after. Anything that
is not an upload is capped at **1 MB**; the routes that genuinely take a
file get the upload budget, and those additionally carry a per-address
rate window, a per-address and process-wide cap on how many bodies may be
arriving at once, and a stall timeout — a client that stops sending loses
its slot and gets a `408` rather than holding a door shut. That last one
matters on a public instance: without it a handful of deliberately slow
connections from one host could refuse every upload on the box. These are
fixed limits, not `.env` settings. Set `OIKONOME_TRUSTED_PROXIES` if
anything fronts the app, or all of them see the proxy as the only client
(see [docs/reverse-proxy.md](docs/reverse-proxy.md)).

On/off settings in `docker/.env` are read as booleans: only `1`, `true`,
`yes` or `on` enable them. Writing `OIKONOME_AUTOBAN=0` really does turn
auto-ban off, and a defense you meant to enable will not be on unless it is
spelled one of those four ways.

A note for anyone running a scanner over this repository: `devpass`,
`apppass` and `adminpass` are the passwords of the throwaway local scratch
database the development docs, Makefile and CI start on `127.0.0.1:5433`.
They are not secrets and are never a deployed value — every installed
instance generates its own database credentials at install time.
