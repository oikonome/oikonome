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
by design. Bank sync counts as a success when at least one connection
synced; its row then names how many failed ("3 ok, 1 failed"), and each
failing connection has its own row below. The daily email row stays green
on a day the worker deliberately sent nothing, and says why: no budget is
set yet, or there is no confirmed, unmuted address to mail.

### <bank name> (<aggregator>)
`error:*` status: the aggregator connection needs re-auth. SimpleFIN:
check your bridge subscription. Plaid: re-link.
A bank you **disconnected** yourself stays that way until you bring it
back — MX and SimpleFIN from the account's ✎ row (**reconnect**), a whole
SimpleFIN bridge by connecting a new setup token; see the Accounts guide.

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
on demand if you want to see the message. Doctor's daily email row says
*not sent: no budget is set yet* when this is the cause.

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
the door. Four refusals you may meet, none of them a fault:

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
- **"these files total more than 60 MB — import the folder in smaller
  batches"** (413) — a folder analyze is sent as one request, so the
  whole folder counts against one limit. Select part of the folder,
  analyze and run it, then do the rest.
- **An HTML "413 Request Entity Too Large" or "504 Gateway Timeout"
  page** instead of one of the messages above — that answer came from a
  reverse proxy in front of the app, not the app. Raise the proxy's body
  limit and read timeout as shown in [the reverse-proxy
  guide](reverse-proxy.md#upload-size-and-time-limits).

Beyond the uploads themselves, **analyzing a folder or a tax document is
limited to 20 attempts an hour** and a single-file import to 60. These
are fixed, not `.env` settings.

Everything that is *not* a file upload — signing in, saving settings,
ordinary API calls — is capped at a **1 MB** request body, which is far
more than any of them legitimately send.

### running a folder import says "another folder import is still running"
A household runs one folder import at a time, and the import runs in the
background after you press Import, so this (a 409) means an earlier batch —
started here, in another tab or by another member — has not finished.
Nothing is lost: the refused batch stays analyzed, so wait for the other
import to finish and press Import again. If the batch has expired by then,
the message says to re-analyze the files instead.

### a PDF is refused as too large
Separate from the upload limits above, a PDF is measured by what it
unpacks to before anything reads it: at most about **1.5 MB** of drawing
content per page, **40 megapixels** of embedded images per page, 500
pages, and about 2 MB of extracted text. These are fixed, not `.env`
settings. The messages:

- **"this PDF expands past the size limit for a statement"** (or the
  *text-size* limit) on the Import page — usually a scanned or
  image-heavy statement. Download the same period as **CSV or OFX/QFX**
  instead; if the bank offers only PDFs, a statement re-exported or
  printed to PDF from the bank's site often comes out much smaller.
- **"this PDF's first page expands past the size limit — try
  re-exporting it, or upload an image"** on a receipt — save or
  screenshot the receipt as a JPEG/PNG and attach that.
- **"this file is far larger than any tax document"** on the tax-document
  card — a plain-text upload is capped like a PDF's extracted text;
  upload the transcript, W-2 or return itself rather than a bundle.

Running the server outside the provided container image? These checks
need **pypdf 6.11 or newer** (`server/pyproject.toml` pins it); with an
older pypdf, PDF imports fail with an import error naming
`LimitReachedError` — upgrade it, or reinstall the server package.

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
Grey with a time means the phone has not refreshed it for over an hour,
or not since the day turned —
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
session and device, and emails the account that it happened. On an
owner's account it also deletes the logins of members who joined through
that owner's invites in the last 7 days (a thief holding the session may
have planted them): the confirmation lists them first, and if the
household changed while you were confirming, nothing is cleared — run it
again. Re-invite any that are genuine. The password
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
restart.** Sign in with it and enrol a new key; enrolling the first key
asks for the operator token once more, because a signed-in browser alone
is not enough to add a credential that outlasts the session.

Setting up a passkey for the first time works the same way: sign in with
the token, enrol (re-entering the token when asked), and only then set
`OIKONOME_ADMIN_REQUIRE_PASSKEY=1` in `docker/.env` and apply it
(`docker compose up -d`). From that moment the token no longer
signs in, and a console tab you opened with it is signed out at its next
click — sign in again with the passkey. On a hosted instance the token
never signs in at all; there, `oikonome admin-enrol` (above) is the way to
the first key.

Neither is a way around the console's security: both need a shell inside
the container, which means you already control the host. The console's own
"remove" button still demands a live passkey touch, and it refuses to
remove the last key while the console is passkey-only or
`OIKONOME_ADMIN_REQUIRE_PASSKEY` is set; with an operator token that can
still sign in, the last key may go.

## Still stuck?
Open a GitHub issue with the **diagnostic bundle** from /doctor attached
(it contains check results and versions — never tokens or transactions,
and no hostnames, bank names or collector labels, so it is safe to attach to
a public issue; the signed-in /doctor page still shows them).
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

