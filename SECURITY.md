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
compose stack (aggregator tokens, webhook signing secrets, TOTP seeds,
the instance's web-push signing key and the tokens of bank connections
still waiting to be released at Plaid encrypt at rest under it — the installer generates it; keep a
copy, restores need the same key; backup + rotation procedure:
[docs/master-key.md](docs/master-key.md)), run behind TLS, keep the compose
Postgres unexposed, enable TOTP on your account.

Two doors are worth knowing about because they read the whole household at
once. The **full export** and the **database dump** are not plain links: each
download needs a one-shot ticket minted by a fresh step-up (password, or a
live code where a second factor is enrolled) and expires within a minute, so
a stolen signed-in tab cannot quietly stream them. The **continuity
packet** (a PDF of every account, debt, bill and business) takes the same
fresh step-up, to download or to email, and its note (where the will and
the passwords are) is read by the household owner alone: anyone else in
the household is told that one is written, never what it says. The **operator console**
(`/admin/console`) is off unless you enable it, and the way back in after
losing its passkey is a command inside the container — see
[docs/troubleshooting.md](docs/troubleshooting.md). The operator's audit
log of console actions is kept for 400 days, trimmed nightly.

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
they made. Webhooks are treated as standing credentials too: a password or
email change, a mailed reset, a first second factor, and removing or
leaving all pause the webhooks that person created, until someone switches
them back on in Settings, which is itself a step-up action. An owner's
password or email change, a second factor turned on or off, a mailed reset
and an operator clear also pause the household's own outbound settings —
the mail relay saved in Settings, extra email recipients and the AI
endpoint — until the owner confirms them in Settings ("These are mine —
resume"); until then mail goes through the instance's own relay, the paused
recipients get nothing, and the AI runs on the instance's own backend. The
owner's password, email or second-factor change also pauses the household's
text-message number until it is verified again, and ends browser
notifications in every browser but the one making the change; a browser's
notifications end with its sign-in. Every owner rotation and recovery
also cancels the owner's unspent reset links and pending invitations, and
removes every member who joined through the owner's invitations in the
last seven days (`OIKONOME_RECOVERY_MEMBER_WINDOW_DAYS`, `0` turns it
off), since a stolen session could have invited a login of its own; the
owner is shown who was removed. Where the household saved its own Plaid
or MX keys, the same changes hold new bank links on them — a new link, a
bank re-auth and the MX widget are refused until the owner confirms the
keys or removes them — because those keys decide who receives the bank
login typed into a new link. Banks already linked keep syncing. Disconnecting a bank, exchange or
bridge discards its stored credential (a Plaid connection keeps its token
only until its removal at Plaid goes through), so no backup carries a live
secret for a connection nobody uses. A **webhook signing secret** (`oikwh_`) is the other direction: it is
shown once, stored encrypted under the household's key, left out of the
data export, and signs every delivery (HMAC-SHA256 over timestamp and
body) so the receiver can refuse a forgery or a replay. Minting a script
token, adding a webhook, changing its URL or switching it on, restoring a
data archive, and pointing the mail relay, the AI endpoint or the
daily-email list somewhere new are step-up actions. So is removing a
stored tax or income document that is an earnings record or wrote more
than one year (one tap would otherwise blank decades of figures); the
rule is checked again under the removal's locks, so a document that
gained a year after the confirm was shown still asks. Putting a removed
document back within its 30 days does not. A folder import runs
in the background on a plan frozen when it is submitted: any step-up, and
any export archive in the folder being restored, happen in that
submitting request, never later in the job, and the job stops for good
when the session that started it is signed out or loses permission to
import. A data restore whose starter is already signed out stops before
writing, and one whose starter is signed out while it runs pauses the
mail relay, recipients and AI endpoint the archive brings in. A restore
carries a stored AI key or mail-relay password only back onto the
destination it was saved for (the relay's login is a credential too, since
some relays take an API token as the username: it is stored encrypted
under the household's key like the password, and a closed month's budget
snapshot keeps neither): an archive that names another address for
the same entry gets no credential, and the owner types it again. A
change to where the household's AI text goes — endpoint, model, key or
which backend a task uses — sends a security notice, whether it came
from Settings or from a restore. The one-tap buttons in the daily
email are signed links for one action on one object, addressed to the
recipient, and expire after seven days. Pressing one is a write like any
other: the address must still hold an owner or member account in good
standing — a suspended, deleting or read-only household, or a hosted
account with no second factor, gets a refusal (muting an alert is the
exception for a read-only household, as it is in the app) — and a button
whose object has changed since the mail went out does nothing. The
buttons (and the unsubscribe link) are keyed from `OIKONOME_MASTER_KEY`,
so rotating the key voids the ones already sent.

A **read-only** household (a lapsed or cancelled subscription, when an
add-on gates one) can still read everything and still take every way
out: export its data, delete or leave the account, remove a member or
make them view-only, revoke script tokens, invites and support access,
unlink a bank, delete a webhook and mute an alert — plus the person's own
account security (password, second factor, passkeys, sessions, devices).
Those writes are one declared list of exact routes; every other write is
refused.

The household's setup is the owner's to read as well as to change. The
mail relay, where each email goes and when, the AI endpoint and its
consent, and the bank-connection keys are not sent to members or
viewers at all (the household's time zone is, since every screen dates
by it); a member still sees whether their own email and channels reach
them. System health follows the same line: a member or viewer reads
that the mail relay and the AI endpoint are configured and whether they
answer, but not the relay's or endpoint's address, the model names,
the words of a last-run error, or who the daily email reaches.

What each credential change takes away is fixed per change, in one place,
and does not vary by which screen made it. **Regenerating recovery codes**
is the routine top-up: it signs out the person's other sessions and
devices, ends their browser notifications elsewhere and cancels their
unspent reset links and pending recoveries — nothing else, so script
tokens, webhooks, invitations, members and the household's settings are
untouched. **A second factor turned on or off**, a first passkey, and
removing a passkey while another factor stays (which issues a new sheet)
add the rest: script tokens, webhooks, the household's outbound settings
and text number, pending invitations and recently invited members, and
support access. **A password or email change** adds the passkeys (the one
that confirmed the change is kept when it is the only second factor);
it replaces the recovery sheet when the client can show the new one and
voids it when no second factor remains. **A mailed reset or an operator
clear** takes everything, including every factor and code. Whenever a
new sheet is issued every code from the old one stops working, and a
sign-in that spent an old code moments before the reissue gets no
session. The door-by-door list is in the Security guide (in-app Help).

Every change to a sign-in credential (password, authenticator, passkey,
recovery codes) is announced to the account's own address, so a change
made from a stolen session is never silent; the account's own phones get
it as a security push that opens the Security screen. On a hosted instance a
sign-in email change does not move the account until the new mailbox
answers: the link goes to the new address, works once within 24 hours,
and the account moves only when the button on the page it opens is
pressed. The address the account left is then told, with the new one
partly masked, and — when that old address was confirmed — given a
single-use link, good for 72 hours, that puts the account back on it,
signs out every device, revokes script tokens, pauses webhooks and mails
a password reset there; it is that reset, not the link, that strips
whatever a stranger enrolled. Pending resets for the old address die
with the move, and for seven days after the account is put back a
factor-clearing reset cannot be requested, since whoever moved it may
still read that inbox (a recovery code or an operator clear still
works). The full flow is in the Security guide (in-app Help).

A mailed password reset strips every second factor, so on an account with
one enrolled it demands a recovery code as well — inbox control alone must
never equal account ownership. The way back in for someone who has lost the
password, the authenticator and the codes at once is therefore deliberately
slow, and there are only two: a **self-serve reset with a seven-day
cooling-off period**, announced to the account on every channel it has with
a one-click cancel link good for the wait it announces (and cancelled by
any sign-in, or from Settings by a signed-in owner), and an **operator clear**
— console action or `./oikonome.sh clear-2fa <email>` — which the operator
runs only after checking identity out of band. Both void TOTP, passkeys and
recovery codes, evict every session and device, and mail the account that
it happened; neither changes the password.

Wrong second-factor guesses are counted in two places. At sign-in and on a
mailed reset they count toward a per-account lock on those doors. Inside a
signed-in session — step-up, elevation, re-binding an authenticator, and
every door that asks for the password again — wrong passwords and codes
alike count toward a lock on that session alone, keyed by the credential that
authenticated the request, so a stolen session can neither guess without
limit nor lock the owner out of signing in. Every guess takes its slot in
the budget *before* it is checked and a right answer hands it back, so a
burst of concurrent wrong guesses cannot all be verified ahead of the
count catching up. Either lock escalates: fifteen wrong guesses in 15
minutes, or thirty in a day however slowly they come, lock the door for
15 minutes, and each further lock within a week doubles (30 minutes, an
hour, … up to a day). A right code is not counted as a miss, and a
lock is tripped only by misses already made, never by a right code. Each
lock in a day raises that day's threshold by another thirty. Signing in
successfully resets neither the day's misses nor that history; a week
without a lock clears the history.
The refusal says how long the lock still holds. The first time in a day
that a correct password is followed by enough wrong codes to lock sign-in,
the account is sent a security notice (mail and push) saying its password
is known to someone. The session lock tells the account the same way: the
first time in a day that a signed-in browser or device locks its own
confirmations with wrong passwords, codes or passkeys, the account it is
signed in to gets the same kind of notice, on the same channels and at the
same rate — one notice for the lock, none for each attempt, and none again
that day whichever session locks next. Passkey sign-in is not behind this lock, and neither
is the emailed password reset: a reset link's recovery-code step has its
own plain budget (fifteen per 15 minutes, never escalating, no notice),
because the link reaches only the mailbox and yields a new password, not
a session. A completed reset lifts the sign-in lock. On hosted, a passkey-only
account that elevates with a recovery code must give its password with it:
the code stands in for the passkey, never for the whole proof.

Full account, routing and loan numbers are not kept in the raw copy of a
row stored beside each account, loan and transaction — that copy leaves the
instance verbatim in the export. Identifier columns in file imports are cut
to their last four digits, and so is any run of eight or more digits in the
line a statement import keeps, in the description of every
file-imported transaction, and in a file row's free-text cells (memo,
note, payee, description, details and the like, and any cells past the
header row). A card number counts however its groups are joined — a
space, dash, dot or slash, and also a tab, a line break or a no-break
space, as spreadsheets and statements write them. Upgrading applies the same cuts to rows
stored earlier, and a restore applies them to older archives. An AI
endpoint URL keeps its host and path in the export, with any password or
key inside it masked.

Request bodies are bounded before they are read, not after. Anything that
is not an upload is capped at **1 MB**; the routes that genuinely take a
file get the upload budget, and those additionally carry a per-address
rate window, a per-address and process-wide cap on how many bodies may be
arriving at once, and a stall timeout — a client that stops sending loses
its slot and gets a `408` rather than holding a door shut. That last one
matters on a public instance: without it a handful of deliberately slow
connections from one host could refuse every upload on the box. These are
fixed limits, not `.env` settings. What an accepted upload may *expand*
to is bounded too: a PDF is measured before it is read or rendered — at
most about 1.5 MB of drawing content and 40 megapixels of embedded images
per page, 500 pages, about 2 MB of extracted text — with every decoder
capped, so a small compressed file cannot inflate into a large one inside
the server; a plain-text tax upload carries the same text ceiling, and
an image is refused for decoding past 40 megapixels. File imports bound
their own shape before reading it: an OFX/QFX file is capped at 10 MB, a
CSV at 200 columns (40 for the budgeting-app registers), and every data
row is read through a bounded reader (128 KB per line), so one enormous
row cannot balloon the shared process.

Password hashing is memory-hard (argon2id, about 64 MiB per hash), so
only a few hashes run at once (`OIKONOME_ARGON2_CONCURRENCY`, default 3)
and a sign-in that waits past `OIKONOME_ARGON2_WAIT` seconds is answered
`503` with `Retry-After`, rather than a burst of sign-ins exhausting the
container's memory; a database with no free connection sheds signups the
same way. Tripping a rate limit is not an auto-ban strike (only failed
sign-ins are), so many honest clients behind one carrier address are not
banned, and an IPv6 client is counted as its whole /64 for every
per-address limit, strike and ban; the sign-in human check also counts
each IPv6 /48 (15 failed sign-ins in 15 minutes challenges the whole /48 —
a challenge only, never a ban or strike). Set `OIKONOME_TRUSTED_PROXIES` if
anything fronts the app, or all of them see the proxy as the only client
(see [docs/reverse-proxy.md](docs/reverse-proxy.md)). Under rootless
Podman the app cannot see real client addresses at all, so auto-ban, the
per-address limits and `OIKONOME_ADMIN_IPS` act on the pod network's
address; `./oikonome.sh` warns when it detects this, and the same page
gives the two remedies.

On/off settings in `docker/.env` are read as booleans: only `1`, `true`,
`yes` or `on` enable them. Writing `OIKONOME_AUTOBAN=0` really does turn
auto-ban off, and a defense you meant to enable will not be on unless it is
spelled one of those four ways. Left unset or empty, each keeps its
documented default (auto-ban's is on).

A note for anyone running a scanner over this repository: `devpass`,
`apppass` and `adminpass` are the passwords of the throwaway local scratch
database the development docs, Makefile and CI start on `127.0.0.1:5433`.
They are not secrets and are never a deployed value — every installed
instance generates its own database credentials at install time.
