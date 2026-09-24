# Troubleshooting

**Start at /doctor.** Every check below matches a row on that page.

### database
Container can't reach Postgres: check that the `postgres` service is
healthy (`docker compose ps`). `OIKONOME_DSN` is **not** yours to set in
`.env` — `compose.yaml` builds it from `OIKONOME_APP_PASSWORD` and
overrides whatever `.env` says, so that is the variable to check.

**Postgres major upgrade.** When a release moves the bundled Postgres to a
new major version (16 → 18, say), `./oikonome.sh upgrade` migrates
your data folder in place: it dumps everything with the old server image,
moves `data/postgres` aside as `data/postgres.pg<old>-<stamp>`, initialises
the new major and reloads. Nothing is deleted — remove the old folder once
you are happy. If the container logs show *"database files are incompatible
with server"*, the migration did not run (e.g. `docker compose up` was run
by hand instead of `./oikonome.sh upgrade`); run `./oikonome.sh upgrade` and
it will. External/managed databases are untouched — upgrade those with your
provider's tools.

### bank sync (worker) / nightly detection (worker) / daily email
"never ran" or stale: the worker container isn't running or can't reach
Redis. `docker compose logs worker`. All three heartbeats are written
only on SUCCESS — a "running" worker that errors still shows stale here,
by design.

### <bank name> (<aggregator>)
`error:*` status: the aggregator connection needs re-auth. SimpleFIN:
check your bridge subscription. Plaid: re-link.

### token encryption
`OIKONOME_MASTER_KEY` unset — tokens and TOTP seeds stored plaintext.
Generate one (`python -m oikonome.db.crypto`), add to `.env`, restart.
Existing plaintext bank tokens and stored secrets are swept to
ciphertext when the worker next starts (so restart the stack after adding
the key — nothing waits for a connection to be re-saved); TOTP seeds are
swept on restart the same way.

### SMTP
`OIKONOME_SMTP_HOST` unset or wrong: the daily email can't send. Test
your SMTP credentials with any mail client first.

**Mail worked, then silently stopped.** Many cloud hosts block outbound
25/465/587. Switch `OIKONOME_SMTP_PORT=2525` — most relays accept it — and
recreate app + worker. The worker probes the
relay every half hour; when the path is dead the admin console shows a red
"Outbound mail is failing" banner, Doctor gets an **outbound mail** row, and
on a self-hosted instance every invite / reset link the app could not
email is written to the container log (`docker compose logs app | grep
"link"`); a hosted instance redacts the link instead — resend from the
admin console once mail works.

### no daily email at all, and SMTP is fine
Check whether a budget is set (**Budget** → Food and Everything-else). The
scheduled daily verdict is skipped while there is no plan to judge, since
the only thing it could report is "on budget, $0 of $0". **Send today's
email now** (in the Daily email card under Settings → Email & Push) still sends
on demand if you want to see the message.

### someone you added isn't receiving it
Adding an address invites it; it does not enrol it. The person is emailed
once and starts receiving the daily summary only after they accept — until
then **Settings → Email & Push** shows their row as *invite sent*, and nothing
else is ever sent to that address. If the invitation went astray, use
**Resend** on the row (it rotates the link, so the old one stops working).
Restored backups are the other common case: invitations are control-plane
state and don't travel in an export, so a restored recipient shows *not
invited* and needs one click to be asked again.

### mail that sends but never arrives
A mistyped address is accepted by every check there is — `@gmaill.com`
parses fine and is a real domain — so the only symptom is silence.
**Settings → Email & Push** lists every recipient with whether the instance's mail reaches
them. An address the mail server permanently refused is marked *not
arriving*, with the reason it gave, and its scheduled sends are **paused**:
mailing a dead address every morning is how a sender's reputation gets
burned for everyone else on it. Confirming the corrected address restarts
delivery.

Relays that suppress bad addresses (Postmark, SES, …) stop bouncing after
the first failure and start refusing the send instead — so the app watches
for both. If your relay can post bounce webhooks, point it at
`/api/email/webhook` and set `OIKONOME_EMAIL_WEBHOOK_TOKEN` (see
`docker/.env.example`); without it the refused sends are still detected,
just one send later. Nothing here is required — leave the token unset and
the endpoint does not exist.

### ledger
Zero transactions: connect a bank or import a file — see
[importing.md](importing.md).

### merchant logos aren't showing
Three ordinary reasons, in the order worth checking. **Show merchant
logos** may be off (Settings → Merchants) — that hides them everywhere.
The merchant may not have been identified by an aggregator that supplies
logos: file imports and SimpleFIN carry no merchant identity of their
own, and rows fall back to a two-letter mark. Or the merchant is simply
one the provider has no logo for. Imported rows *can* pick up a logo —
either by matching a merchant a Plaid connection already identified, or
through **Describe imported history with Plaid** under Settings →
Connections (see the import-history guide). The logos themselves are
fetched by the instance, not by your browser, so an instance with no
outbound internet access shows monogram marks and nothing else.

### "server is busy" reading a receipt, or the Assistant refuses
Both are deliberate limits, not failures. Reading a receipt photo and
answering an Assistant question each hold a lot of memory and a worker
thread, so only a couple run at once on an instance: an extra receipt
parse waits briefly for a slot and, if none frees in time, is marked
**failed** with a *server is busy* note — the nightly sweep only picks up
receipts still waiting to parse, so re-parse that one yourself; an extra
Assistant question is refused outright — waiting in line would hold the
very thread the limit exists to spare. Wait a moment and retry. On a big machine, raise
`OIKONOME_RECEIPT_PARSE_CONCURRENCY`, `OIKONOME_RECEIPT_PARSE_WAIT_S`
and `OIKONOME_ASSISTANT_CONCURRENCY` in `docker/.env`; the defaults suit
a small container.

### an upload is refused or times out
Uploads are the one request shape that costs an instance real memory
before anything has decided whether to accept it, so they are bounded at
the door. Three refusals you may meet, none of them a fault:

- **"too many uploads — try again later"** (429) — more than 60 uploads
  from your address in a minute. A person importing a folder never
  reaches it; a script in a loop does.
- **"too many uploads in flight — try again in a moment"** (503) — the
  instance is already taking as many bodies at once as it will hold, or
  your address is holding its share of them. Retry; the slot frees as
  soon as one finishes arriving.
- **"upload timed out — the body stopped arriving"** (408) — the
  connection went quiet mid-upload. A genuinely slow link is fine (the
  clock measures progress, not total time), but a dropped Wi-Fi
  connection loses the slot. Re-send the file.

Beyond the uploads themselves, **analyzing a folder or a tax document is
limited to 20 attempts an hour** and a single-file import to 60. These
are fixed, not `.env` settings.

Everything that is *not* a file upload — signing in, saving settings,
ordinary API calls — is capped at a **1 MB** request body, which is far
more than any of them legitimately send.

### the daily email's "Needs you" section has no buttons
The buttons link back to your instance, and recurring mail only carries
links to `OIKONOME_BASE_URL` when it is `https` on a real domain — a bare
IP or a `.lan`/`.local` name is exactly what mail providers filter. Set it
(`./oikonome.sh https <domain>` does) and the next email has buttons.
Only owners and members get the section at all; a viewer's copy stops at
the timeline. A button that says its work is already done is right —
someone settled it in the app first; a category button names the category
the charge is under now and changes nothing. A button that says email
buttons are off means the household can't be changed right now (read-only
billing, a suspension, or — on a hosted instance — no second factor
enrolled yet); sign in to see what it needs. Links older than seven days,
or sent before a master-key rotation, are dead; use the next morning's.

### Prometheus marks the target down, or a sensor reads nothing
Try the door by hand:
`curl -H "Authorization: Bearer oik_…" https://<your instance>/metrics`.
A refusal is always a JSON reason, never the sign-in page. `401`: the
token is missing, mistyped or revoked — a password or email change
revokes every script token, so mint a new one. `403 … push-scoped`: the
token came from Settings → Connections (a collector's); integrations need
one from Settings → Integrations. `429`: a read token gets 600 requests
an hour per address; scrape less often. If `curl` works but the scraper
does not, look at the reverse proxy — an auth layer in front of the whole
site swallows the bearer header (see [reverse-proxy.md](reverse-proxy.md)).

### a webhook stopped firing
Settings → Integrations → the webhook's **deliveries** shows the last
twenty and what the receiver said. Failed deliveries retry for about
fifteen hours; thirty failed attempts in a row switch the webhook off,
and Settings says so. Fix the receiver, press **Send a test**, then tick
the webhook back on. A LAN address is fine on a self-hosted instance; the
cloud metadata address never is.

### the home-screen widget is grey or says "Sign in"
Grey with a time means the phone has not refreshed it for over an hour —
the OS schedules widget refreshes, and opening the app forces one; check
that the phone can reach the instance. **Sign in** means its credential
is gone: it dies with the device's sign-in, so a device revoked in
Settings, a password change or a sign-out all end it. Open the app and
sign in again. **Set a plan** means there is no budget yet.

### locked out (forgot password, no SMTP)
The **Forgot password** link on the sign-in page emails a reset link —
which needs SMTP configured. Without it, reset from the machine that
runs the instance:

```bash
./oikonome.sh reset-password you@example.com
```

This prints a one-time reset link (valid 1 hour, single use). It is the
same reset the emailed link performs, not a lighter one: open it, set a
new password — every existing session, signed-in device and script token
for that account is revoked, and the authenticator, every passkey and
every recovery code are removed with them. Enrol the second factor again
once you are back in.

Because of that, an account with a **second factor** (an authenticator
app or a passkey) is asked for one of the recovery codes you saved when
you set it up — on this link as much as on the emailed one: removing the
factor has to prove something besides access to the host or the inbox.
If you can still sign in, regenerate the codes under Settings → Security
before you need them; if the codes are gone too, see the next section.

### lost the password, the authenticator AND the recovery codes
Two doors, both deliberately slow, because "I can read the inbox" must
not equal "I own the account". Either works; neither needs the other.

**The person's own 7-day recovery.** They request a reset link as usual;
on the reset page, where it asks for a recovery code, they choose **Start
the 7-day recovery**. Nothing changes yet — the account is notified on
every channel it has (email with a one-click cancel, web and native push,
SMS where a verified number is enrolled), **any sign-in cancels it**, and
after seven days the next reset link works without a code and clears the
authenticator, passkeys and recovery codes just as a coded reset would.
That stays true for **30 days**; a recovery nobody finishes in that time
goes stale and the account is back to asking for a code, so start a fresh
one (it is free) rather than leaving a standing exemption on the account.
Asking again restarts the wait rather than shortening it. The cancel
link in each email stops whichever recovery is running at the time —
including one a later request restarted — for as long as the seven days
it announced. After that, or once it has been used, only the link in the
most recent email will do it (or any sign-in). The sign-in
screen's two-factor step links straight to this. It leans on mail, so it
wants `OIKONOME_SMTP_HOST` and `OIKONOME_BASE_URL` set — without them the
clock still runs, but the cancel link goes to the app log
(`docker compose logs app | grep "link"`) instead of to the person. On an
instance with no mail at all, use the operator door below.

**The operator clears the factor.** Verify who is asking *first*, out of
band — the command only records that you did:

```bash
./oikonome.sh clear-2fa you@example.com
```

(menu item **f**; it asks for the address twice.) It removes the
authenticator, every passkey and every recovery code, signs out every
session and device, and emails the account that it happened. The password
is untouched, so follow it with `./oikonome.sh reset-password` and hand
over the link. The account holder enrols a new authenticator once in.

### locked out of the admin console (lost the operator passkey)
Operators only — the console at `/admin/console` is off unless
`OIKONOME_ADMIN_TOKEN` or `OIKONOME_ADMIN_ENABLED` is set. Which recovery
you want depends on whether the operator token can sign in, and **on a
hosted instance it never can** — hosted is passkey-only by construction.

**Passkey-only console** (any hosted instance, or a self-host running
`OIKONOME_ADMIN_ENABLED=1` with no token — nothing to fall back to). On the
machine that runs the instance:

```bash
docker compose exec -T app oikonome admin-enrol
```

That prints a **single-use link, valid 15 minutes**, which enrols one new
passkey without signing in. Open it, enrol, then sign in with the new key
and drop the lost one (`oikonome admin-keys` lists them, `--remove <id>`
removes one). This is also how you enrol the *first* key on a console that
has never had one.

**Self-hosted console with an operator token** (the token is break-glass,
and `OIKONOME_ADMIN_REQUIRE_PASSKEY` is refusing it):

```bash
docker compose exec -T app oikonome admin-keys --reset
```

It removes every enrolled key. The require-flag is ignored once none are
enrolled, so **the token signs in again immediately — no `.env` edit, no
restart.** Sign in with it and enrol a new key.

Neither is a way around the console's security: both need a shell inside
the container, which means you already control the host. The console's own
"remove" button still demands a live passkey touch, and it refuses to
remove the last key while the console is passkey-only or
`OIKONOME_ADMIN_REQUIRE_PASSKEY` is set; with an operator token that can
still sign in, the last key may go.

## Still stuck?
Open a GitHub issue with the **diagnostic bundle** from /doctor attached
(it contains check results and versions — never tokens or transactions).
Issues without the bundle get auto-asked for it.

## Signing up says "this address already has an account"

Sign in if it's yours. If you never confirmed it — your own earlier
attempt, or someone else registering an address they don't own — check
that mailbox: the signup you just submitted is parked, and the instance emailed a
link that finishes it (valid one hour, one use). Following the link
replaces the unconfirmed account, asks you to choose a password there
(whatever was typed on the form is not kept), and signs you in. Nothing
changes without the link, so ignore the mail if you didn't ask for it.
The reply is the same either way on purpose — it must not tell a stranger
which addresses are sitting unconfirmed.

