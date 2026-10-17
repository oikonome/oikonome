# Quick start (self-hosted)

Requirements: Docker (or Podman) with Compose, plus curl and openssl. If
any of those are missing, the installer detects it, shows the install
commands for your system, and re-checks before proceeding — it guides you
through installing them, and never installs anything itself without
asking. Five minutes to a working instance.

**Platforms.** Linux is the tested happy path. **Windows**: run
everything inside WSL2 (Ubuntu) with Docker Desktop's WSL integration —
clone and install from the WSL shell exactly as below. **macOS**: works
with Docker Desktop or OrbStack (the scripts carry BSD-tool fallbacks),
but it's lightly tested — please report anything odd via the in-app
Feedback & bug reports tab.

```bash
git clone https://github.com/oikonome/oikonome.git && cd oikonome
./oikonome.sh install
```

**First time here?** Two pointers before you start. `./oikonome.sh demo`
stands up a throwaway instance full of synthetic data, so you can see
the product working before committing your real accounts — see
[Try it with fake data first](#try-it-with-fake-data-first-demo-instance),
then install for real. And report anything broken or confusing from the
in-app **Feedback & bug reports** tab (`/feedback`) as you go; the most common
first-day questions are answered in the [FAQ](faq.md).

`./oikonome.sh` is the one manager entry point. Run it with no arguments
for the interactive menu: it first lists every Oikonome instance on the
machine (pick one by number, or **n** for a new real or demo instance),
then offers, for the instance you chose:

```
1) Install / Upgrade
2) Status
3) Logs (follow; Ctrl-C returns here)
4) Backup now
5) Restore from backup
6) Reset (wipe data, fresh wizard)
7) Uninstall (remove everything)
8) Reset data only (keep logins; wizard runs again)
p) Reset a user's password (locked out — prints a one-time link)
f) Clear a user's second factor (lost authenticator AND recovery codes)
l) Smart categorization (local LLM): off — turn on
d) New demo instance (synthetic data, own port)
s) Switch instance
q) Quit
```

(The **l** row reads `ON — turn off` once the bundled LLM is enabled.)
Or use the subcommands directly:

```bash
./oikonome.sh status            # containers, port, version, last backup, health
./oikonome.sh logs              # follow app + worker logs
./oikonome.sh backup            # pg_dump into ./backups inside the install
./oikonome.sh restore <file>    # replace the database from a dump (confirm-gated)
./oikonome.sh reset             # wipe the data + generated passwords, fresh setup wizard; keeps your settings and the master key your backups need (confirm-gated; --new-key replaces the key; either way it is saved beside the backups)
./oikonome.sh reset-data [tenant-id]  # wipe the data but keep logins; wizard runs again (confirm-gated)
./oikonome.sh reset-password <email>  # locked out? one-time reset link, no SMTP needed
./oikonome.sh clear-2fa <email> # lost the authenticator AND the recovery codes (confirm-gated)
./oikonome.sh uninstall         # remove the whole instance (confirm-gated)
./oikonome.sh start-at-boot     # Podman only: start the stack after a host reboot (off: start-at-boot off)
```

`demo`, `https`, `llm` and `script` are covered in their own sections
below; `./oikonome.sh help` prints the full usage line.

(`./install.sh` and `./uninstall.sh` still work directly, if you prefer.)

The installer generates the database passwords and the encryption master key
(protects bank tokens at rest), builds, and starts everything (SMTP for
the daily email is not asked for here — Settings → Email & Push or
`docker/.env` takes it later). Re-running it is always safe —
it keeps your `.env` and data. Prefer doing it by hand? Copy
`docker/.env.example` to `docker/.env`, fill every value (the master key is
32 random bytes, **urlsafe** base64: `openssl rand -base64 32 | tr '+/' '-_'`),
then `docker compose up -d` from `docker/`.

When it's up, the installer prints a one-time **setup link**
(`http://localhost:8042/setup?token=…`; if 8042 is taken it auto-picks the
next free port, and `OIKONOME_PORT` in `docker/.env` pins it). Open that
link — the **setup wizard** creates your account. The link is only printed
to a real terminal: if you ran the installer through a pipe, `tee`, or a
script, run `./oikonome.sh status` on the host and it prints the link
again (opening the bare URL before the account exists lands on a 403 —
the token is the key). Then, in order:

1. **Appearance** — **Auto** (the default: follows the phone or
   computer's light/dark setting), **Light** or **Dark**. A tap applies at
   once and is saved for the household, so every device you sign in on
   matches. Change it later from the account menu (**Theme:**) in the
   browser or Settings → Appearance on the phone.
2. **Connect** — **Connect an account** links a bank, card, or brokerage
   (hosted goes straight to Plaid; self-host gets the provider grid —
   Plaid / MX / SimpleFIN / scripts / files). Linking pulls transactions,
   holdings and liabilities. Everything you've linked is listed
   underneath, one row each, with a status dot, how many accounts came
   with it, when it last pulled, and a Disconnect button. **Import a file
   instead** works just as well. Below the grid, **How much history should
   come in?** picks **Last month**, **3 months**, **6 months**, **1 year**
   or **Everything available** (the default). It saves on the tap and
   applies to every bank you link from then on (banks share up to two
   years) and every file you import (rows older than the window are left
   out, and the import says how many). More history means better bill
   detection and a budget seeded from real months; less keeps the first
   sync quick. To bring older history in later, widen the window and
   import from Accounts → Import. Click **Done connecting** when you've
   added everything. When the instance enforces an institution limit, the
   card also says how many of the allowed institutions are connected, and
   the Connect button is disabled once they're spent (see the accounts
   guide); self-host is never capped.
3. **Bills & budgets** — first a question:
   - **Find them for me, I'll adjust** (recommended) — waits for your
     bank's history to arrive (delivered minutes after linking; you see
     each connection's count and how far back it reaches), then the finder
     proposes the bills and paychecks it can prove. **Approve**, **Dismiss**,
     or **Adjust** one first — change its name, amount, cadence or due day,
     then **Save & approve**; the charges that proved it stay attached.
     **Approve all** takes the lot. The wait is deliberate: income, bills
     and the pre-filled budget are all derived from that history.
     Dismissals are remembered.
   - **I'll enter them myself** — an empty list and a short form (name,
     amount, cadence, due day; bill or paycheck). Each one matches its
     charges by merchant from then on. This also turns off the nightly
     search for *new* bills and paychecks (see the Bills guide); you can
     still run **Find bills & income** on the Bills page any time.
4. **Budgets** — Food & drink and Everything else per month, with your
   approved bills and income above and savings as what's left. On the
   finder path the figures are pre-filled from your history; on the
   enter-them-myself path they start blank, with a suggestion from your
   history beside each one to tap if you want it. That's all the daily
   verdict needs.
5. **Email & push** — the same on every instance. **Daily verdict**:
   **Email**, **Push**, **Both** or **Off**, and the hour it sends. For
   email, **Short summary instead of the full report** sends just the
   verdict card (on budget or not, left to spend, pinned bills). Choosing
   push registers the browser you are on (the browser asks permission; on
   http:// it cannot, the phone app can); **Push to this device** does the
   same on its own. **Who gets the email** lists the household's
   addresses, each with its own on/off, and **Add another address**
   invites one more — a new address gets one confirmation email first and
   can unsubscribe from any message. A self-hosted instance also shows
   whether its outbound mail account (SMTP) is set up, with a link to
   Settings → Email & Push; until it is, nothing can be emailed.
6. **Done** — a summary of what you set up (appearance, accounts and
   history window, bills and paychecks approved, budgets, delivery and
   recipients), and when the first email or notification arrives. Then
   **Go to Today**. Every step but the Appearance choice and the history
   wait is skippable, and the green **Finish setup** pill in the nav
   resumes at the first unfinished step — so does **Finish setup** under
   Settings → Wizards, in the browser and the phone app.

Signing in before setup is finished takes you straight back into the
wizard, so you never land on an empty dashboard wondering where to start.
Press **save & finish later** and it stops doing that for the rest of the
browser session — the pill is still there whenever you want it.

## One instance, one household

A self-hosted instance is claimed once, through the setup link the
installer prints, and stays a single household from then on: `/signup`
sends visitors to the sign-in page rather than letting anyone reachable
on the network create a second account on your box. To give a partner
their own login, invite them under **Settings → Users** (the same card
is **Security & household** in the mobile app) — that shares *this*
household, with its accounts and history, which is almost always what
"add my partner" means. (Someone who really wants a separate household
runs their own instance.)

An invitee who opens the link while another account is already signed
in on the same browser is offered **sign out and accept the invite**,
so the invitation is never swallowed by the session that happens to be
open.

## Try it with fake data first (demo instance)

Want to see the product working before wiring up your real accounts?

```bash
./oikonome.sh demo
```

stands up a **separate throwaway instance** on its own port, generates
~10 years of synthetic household data (paychecks, rent/mortgage, bills,
two credit cards, savings, brokerage/401(k)/crypto, fees, an alert
history, a tax history), and prints a URL + ready-made login — the login
page shows the credentials pre-filled (it's a demo; they're not a
secret), and first sign-in lands on a fully working Today page, no
setup steps. The data
is random per run (the printed seed reproduces a household exactly:
`./oikonome.sh demo <seed>`); everything is fictional.

A demo instance is editable where editing teaches you something —
recategorize, pair reimbursements, approve bill proposals — but its
**data doors are locked**: aggregator connections, file imports, script
tokens, and email sending are disabled (shown in place, greyed out), and
so are password/2FA/passkey changes, since the printed credentials are
the login. **Settings are read-only** for the same reason: the login is
shared, so one visitor rewriting the budget or the notification schedule
would rewrite it for everyone after them. Page preferences that only
affect what you see — the Today page's summary/detail face, light/dark —
still work; each browser and phone remembers its own rather than the
account remembering for everyone. Install normally when you're ready for real data. Discard the
demo with its own `./oikonome.sh uninstall` — or sweep every demo
instance at once (each run creates its own `~/oikonome-demo-*` folder,
so parallel demos coexist) with:

```bash
./oikonome.sh demo clean
```

## Smart categorization (optional LLM)

Merchants your bank/aggregator leaves in a vague bucket get categorized in
layers — this matters most for SimpleFIN, which sends sparse merchant
text and no category:

1. **Your corrections** (always win): change any transaction's category
   (including a custom name — pick *✎ custom category…*) and every past
   and future transaction of that merchant follows automatically.
2. **The bank's own label**, where it is concrete and confident: nothing
   below may move a transaction out of a category the bank asserted with
   high confidence; the layers below only sharpen vague buckets.
3. **Known-brand rules** (built in, no LLM needed): thousands of chains from
   the community-maintained [Name Suggestion
   Index](https://github.com/osmlab/name-suggestion-index) categorize
   instantly and deterministically.
4. **The local classifier** — one pass, two sources. Your **per-household
   overlay** answers first: each night the instance retrains a small model
   from your own accumulated corrections (automatic, no setup; it works
   even if no model file is mounted). Where it abstains, the
   **operator-provided model** takes its turn: a small trained model that
   predicts a category with a real confidence and only answers above a
   floor — below it the merchant is left alone rather than guessed at.
   Both run entirely on the instance (your data never leaves it).
   Enable the shipped model by pointing `OIKONOME_CATEGORIZER_MODEL` in
   `.env` at a joblib artifact (the compose file mounts `./models` into
   the containers at `/models`, read-only); `OIKONOME_CATEGORIZER_MIN_CONF`
   tunes its abstention floor (default 0.55), and
   `OIKONOME_CATEGORIZER_OVERLAY_MIN_CONF` the overlay's stricter one
   (default 0.70). Any scikit-learn pipeline that maps a merchant
   descriptor string to one of the standard primary categories works —
   training one on your own corrected ledger gives the best results.
   Needs the `oikonome[model]` extra (already in the official image).
5. **The LLM** handles only what the layers above left unanswered — plus
   Amazon order summaries. Receipt images/PDFs parse with a **vision-capable** model:
   the bundled chat tiers are text-only, so run
   `./oikonome.sh llm vision` (pulls qwen2.5vl:3b) and set it under
   Settings → AI → Vision model.

If the model backend stops answering (a model removed from Ollama, a
host that is down), the nightly run says so instead of failing quietly:
an alert on Today and in the daily email, and a red "last run" row under
Smart categorization on the Doctor page naming the failure. It clears on
the next run that produces answers.

New transactions are categorized **at sync time** (so onboarding, the
scheduled sync and a manual ↻ sync of one connection show real categories
right away, not the next morning); the
nightly run does the heavier passes (Amazon item categories + summaries,
recurring-bill tagging). Three ways to run the LLM, easiest first:

- **Bundled local model** (recommended, private, free, zero config): the
  installer asks *"Enable built-in smart categorization?"* — press Enter to
  accept (a non-interactive install takes the default, which is off; turn it
  on afterwards with the command below). It picks a permissively-licensed Qwen2.5 model sized to your
  machine's RAM (7B/~5GB on ≥12GB hosts — noticeably smarter; 1.5B/~1GB
  otherwise), downloads it into an internal Ollama container, and wires the
  app to it. Turn it on or off (or switch sizes) any time:

  ```bash
  ./oikonome.sh llm on                # auto-picks a model by RAM
  ./oikonome.sh llm on qwen2.5:7b     # or name one explicitly
  ./oikonome.sh llm off               # stop it (models stay cached)
  ```

  It runs entirely on this host — no API key, nothing leaves the box. It does
  use noticeable CPU/RAM during a sync; a GPU makes it much faster (see the
  note in `docker/compose.yaml`). The Ollama container's memory cap is sized
  to the model you enable (up to ~10 GB for the 7B vision tier — parsing a
  receipt photo genuinely peaks that high); `OLLAMA_MEM_LIMIT` in
  `docker/.env` overrides it.

- **Bring your own local endpoint**: already run llama.cpp, Ollama, or LM
  Studio elsewhere? Point the wizard (or Settings → AI) at its URL + model
  name, no API key. The URL is resolved from INSIDE the app container, so
  `localhost` there is the container itself, not the machine running it:
  for a model server on the same host use `http://host.docker.internal:11434`
  (Docker Desktop), `http://host.containers.internal:11434` (Podman), or the
  host's LAN address (Docker Engine on Linux, where the server must also
  listen on that address rather than loopback only).
- **Cloud**: any OpenAI-compatible API — endpoint URL + model name + your
  API key (encrypted at rest, never shown again). Anthropic's API works
  as-is: URL `https://api.anthropic.com`, one of its model ids (Sonnet for
  the bulk work, Opus for the assistant), the same id as the vision model
  for receipts, and an Anthropic API key. A URL pasted with a trailing
  `/v1` is fine.

Skip it and everything still works; categorization is just manual. Two
config layers: the wizard/Settings values are stored per tenant and **win**;
the `OIKONOME_LLM_URL` / `OIKONOME_LLM_MODEL` / `OIKONOME_LLM_API_KEY` env
vars in `.env` are the operator-level default used when no tenant values
are set.

## Email (SMTP)

Set in `.env`: `OIKONOME_SMTP_HOST`, `OIKONOME_SMTP_PORT` (587; use 2525 on cloud hosts that block 587),
`OIKONOME_SMTP_USER`, `OIKONOME_SMTP_PASSWORD`, `OIKONOME_SMTP_FROM`.
Any provider works (Fastmail, Proton bridge, SES SMTP, your own postfix).
Saving a new relay in Settings (host, port, user, sender, password or
STARTTLS) asks you to confirm who you are first — with no relay in
`.env`, the Settings relay is what carries your password-reset link. When
`.env` has a relay, password-reset and sign-in links and the
second-factor-cleared notices always go through that one, even if a
relay is saved in Settings (the Settings relay still wins for the reports
and other household mail), so keep it working: the mail health check
probes it, and when it fails the link is written to the app log instead
(`./oikonome.sh logs`). For
the same reason, an owner's password, email or 2FA change, a mailed reset
and a cleared set of factors pause a relay saved in Settings (along with
any extra email recipients and an AI endpoint saved there) until the owner presses
**These are mine — resume** on the banner in Settings. Meanwhile mail goes
through the relay in `.env`; with none there, nothing is sent, and a
password-reset or sign-in link is written to the app log instead
(`./oikonome.sh logs`).

STARTTLS certificates are verified. If your relay presents a
self-signed certificate — Proton Mail Bridge on your own network is the
common case — set `OIKONOME_SMTP_NO_VERIFY=1` in `.env` and restart to skip
verification (the connection is still encrypted, just not authenticated).
The flag applies to the relay in `.env`, and to one saved in the setup
wizard or Settings → Email delivery when that relay is on your own
network — `localhost`, a private address (`192.168.x.x`, `10.x.x.x`,
`172.16–31.x.x`) or a bare hostname with no dot — so a Bridge you
configured there works too. A relay saved there under a public or dotted
name keeps its certificate check. Leave STARTTLS on: turning it off to get past the certificate
error would send the relay password in the clear.

SMTP carries the daily email, password-reset links, and the
**Feedback & bug reports** tab: with it configured, feedback and bug
reports email themselves to the inbox you name in `OIKONOME_FEEDBACK_TO`.
Without SMTP, or with no inbox named, nothing is lost — the tab hands
you the same report as a downloadable .zip to email manually, and it
says so up front.

The daily email starts once a budget exists — before that there is no
verdict to send, so the sweep skips it rather than mailing "on budget, $0
of $0" every morning.

New logins are asked to confirm their address. The mail is one sentence
and one link, and **clicking it is the whole task** — no button on the
far side: the page says *Email confirmed* and forwards itself to
sign-in a few seconds later. Opening the same link twice (or a mail
scanner opening it first) still shows the confirmation rather than
"already used".

Password-reset emails also need `OIKONOME_BASE_URL` set (the `https`
command below pins it) — emailed links are never built from request
headers. Without it, the reset link is written to the server log — read it with
`./oikonome.sh logs` (the link prints in full whether or not you pipe the
output; only a run with `OIKONOME_ASSUME_YES` set — the bounded snapshot
the admin console's host agent takes — redacts one-time links) — instead
of being emailed. The daily email's
clickable Oikonome header uses the same base URL to link back to your
instance's Today page, and so do the one-tap buttons in its **Needs you**
section (confirm a proposed bill, file an uncategorized charge, mute an
alert). Recurring mail only links to a base URL that is `https` on a real
domain — a bare IP or a `.lan`/`.local` name is what spam filters drop
silently — so a LAN-only install gets the same email with the section as
plain text and no buttons.

## HTTPS (optional)

A LAN-only install works fine over plain HTTP. Add TLS when you expose the
instance beyond your LAN (Secure cookies and remote access):

```
./oikonome.sh https oiko.example.com     # front the app with a TLS proxy
./oikonome.sh https off                  # back to plain http
```

This stands up a Caddy reverse proxy that fetches a real certificate
automatically and pins `OIKONOME_BASE_URL`. `https off` stops the proxy
and clears the base URL and proxy trust that `https <domain>` wrote, so
emailed links stop pointing at the old domain; values you set yourself in
`.env` are kept. Caddy needs to complete a cert challenge, so:

- **Publicly reachable / port-forwarded** (80+443 open): works out of the box.
- **LAN-only**: point the hostname at this machine in your LAN DNS and
  forward 80/443, or use a DNS-01 provider.
- **Easiest, zero certs to manage**: [Tailscale](https://tailscale.com) —
  `tailscale serve https / http://localhost:8042` gives a trusted
  `https://…ts.net` URL on every device instantly.

(Passkey sign-in needs a secure origin — an https domain like the one
above, or `localhost` — and the passkey routes come with hosted mode
(`OIKONOME_HOSTED`); a single-household install uses password + optional
TOTP. In the phone apps, a passkey additionally needs the app build to
list the server's domain.)

(Podman note: the TLS profile uses a compose profile; on rootless Podman
without `podman compose` profile support, use Tailscale or a host Caddy.)

(Rootless Podman note: the app cannot see real client addresses there —
auto-ban, per-address limits and `OIKONOME_ADMIN_IPS` all see the pod
network instead, and with HTTPS on the app port stays open to the LAN.
`./oikonome.sh` warns when it detects this; the remedies are in
`docs/reverse-proxy.md` → "Rootless Podman hides client addresses".)

(Ports 80/443 already taken on this host — another proxy, or a second
Oikonome instance? Set `OIKONOME_HTTP_PORT` / `OIKONOME_HTTPS_PORT` in
`docker/.env`; see `docs/reverse-proxy.md` for the port-forwarding caveat.)

## On your phone

The quickest route needs no build at all: open the instance in the phone's
browser and add it to the home screen. Oikonome ships a web-app manifest, so
it launches full-screen with its own icon and no browser chrome, and the
budget-summary notification can reach you with the page closed.

(A native client lives in `mobile/` and signs in to your own instance. It is
source in this repo — see `mobile/README.md` to build it yourself. It runs
the same guided setup as the browser, step for step, and the two share the
same progress marks: you can start setup on the phone and finish it in a
browser, or the other way round.)

- **Android (Chrome):** menu → *Add to Home screen* (or the install prompt
  the browser offers on its own).
- **iOS (Safari):** Share → *Add to Home Screen*. It must be Safari — other
  iOS browsers cannot install a web app. iOS also only delivers push
  notifications to an **installed** web app, never a normal browser tab, so
  add it to the home screen first if you want the daily summary there.

Set up HTTPS first if you'll use it away from the LAN. A phone that has to
reach the instance over a VPN or Tailscale works the same way.

The native app also carries a home-screen widget — today's number and
verdict on the launcher or lock screen (see the Today guide). The
installed web app has no widget.

## Other programs (optional)

Webhooks, a Prometheus `/metrics` endpoint, Home Assistant sensors and a
local MCP server for an AI assistant are all in **Settings →
Integrations**, each on a read-only token you mint there. Nothing to set
in `.env`. See [integrations.md](integrations.md).

## Upgrades

```bash
./oikonome.sh install
```
(It pulls the latest code first when the folder is a git checkout, then
rebuilds — safe to re-run anytime. `./oikonome.sh upgrade` is the same
command, and so is **Upgrade to the latest release** in the admin console.)
Migrations run automatically at start (advisory-locked; multi-node safe).

Before it changes anything, an upgrade that brings in new code saves a
snapshot of the running database into `./backups` as
`oikonome-pre-upgrade-<date>.sql.gz`, and prints how to undo the upgrade
with it. "New code" means any code other than what last installed
successfully on this instance, however it arrived: the upgrade's own pull,
a `git pull` you ran first, a downloaded release, or `main` checked out
again after a rollback. If the database has accounts in it
and the snapshot cannot be written (a full disk, say), the upgrade stops
with nothing changed. When the database is not running there is nothing
to snapshot; the upgrade says so and points at your newest backup.

**Rolling back.** The image is rebuilt in place, so there is no previous
image to switch back to: going back means the previous code plus the
pre-upgrade snapshot.

```bash
git tag --list 'v*' --sort=-v:refname | head   # find the version you ran
git checkout <that tag>
./install.sh                                   # rebuild the old code
./oikonome.sh restore oikonome-pre-upgrade-<date>.sql.gz
```

Restore the snapshot only with the old code in place: on the new code
its migrations would simply run again. Anything recorded between the
upgrade and the restore is lost. To follow new releases again later,
`git checkout main` and `./oikonome.sh install` (that upgrade snapshots
the restored database first, like any other).

**"The published history was recreated."** If an upgrade stops saying this
checkout's history and the upstream's no longer share a commit, the public
repository was recreated and a pull can never succeed. Your `docker/.env`,
data and backups are not tracked by git, so moving the code across is safe:

```bash
./oikonome.sh backup
git reset --hard '@{upstream}'   # discards edits to tracked files (git status lists them)
./oikonome.sh install
```

Any other failed pull (usually your own edits to tracked files) is shown
with git's message. At a terminal the upgrade then warns and carries on
with the code as it is; run by the admin console or a script it stops
instead, so it is never recorded as done.

Most migrations are instant. A few add a column that Postgres has to
write onto every existing row, and those hold a lock on the table while
they run — on a ledger of hundreds of thousands of transactions that can
be a few minutes during which the app returns errors, so upgrade a large
instance when nobody is using it. The transaction search indexes are
built concurrently, so the ledger keeps working while they run, and an
upgrade interrupted mid-build is repaired by starting the instance again;
older index builds still run inside their migration's transaction.

Some migrations tidy data already stored, once. Upgrading cuts any full
account, routing or loan number left in the raw copy of an account, loan
or transaction to its last four digits, as new rows already are —
including any run of eight or more digits in a file-imported row's memo,
payee, description or other free-text cell, or in cells past its header
row. A weekly or daily bill saved before bills used the average-year rate
has its stored monthly figure restated at that rate,
so the Budget page and the Bills headline agree; restoring an older
archive does the same. An
account id that contains a comma is renamed, each comma becoming a dash
(with a short suffix if that name is taken), and everything that names the
account follows it; a script that pushes to such an account by id needs
the new id (`/api/integrations/accounts` lists every id). A connection
you disconnected earlier has its stored credential (an exchange key, a
SimpleFIN bridge URL) dropped, as a new disconnect now does; it was never
read again, and history is untouched.

The weekly report email no longer exists: a household that had it
switched on simply stops receiving it, and the daily, monthly and yearly
emails carry on as set. A hosted instance (`OIKONOME_HOSTED=1`) with no
billing add-on installed now refuses to start until `docker/.env` says
`OIKONOME_HOSTED_BILLING=none`; a single-household install is unaffected. Under rootless Podman,
`./oikonome.sh install` now prints a warning about client addresses —
see [reverse-proxy.md](reverse-proxy.md) → "Rootless Podman hides client
addresses".

## External database (optional)

`docker/compose.external-db.yaml` runs the stack against a managed or
self-run PostgreSQL server instead of the bundled one (its header lists the
`docker/.env` settings). That server must run **PostgreSQL 16 or newer**:
the schema and the app use functions older versions do not have. The
migrate step checks the version before it changes anything and stops with
a message naming the version it found. `OIKONOME_DB_NAME` may use letters,
digits, `_` and `-` (at most 63 characters), and the name is used exactly
as written, upper case included — `restore` and `reset` drop and recreate
that database by that exact name and refuse any other spelling.

## Backups

Dump the `oikonome` database nightly; that's the whole state:
```bash
./oikonome.sh backup    # → ./backups inside this install folder
```
(equivalent to `docker compose exec postgres pg_dump -U postgres oikonome | gzip`
via `scripts/backup.sh`, which also prunes dumps older than 30 days).
A backup is only published once it is whole: the dump is written under a
temporary name, checked (pg_dump's own exit status, gzip integrity, and
that there is actual schema and data inside), and only then renamed into
place. A backup interrupted halfway — the database stopped or dropped
mid-dump — fails loudly and leaves nothing behind, so the newest file in
your backup directory is always one you can restore.

Restore any dump with `./oikonome.sh restore` — it lists what's available,
refuses a file that can't be a real backup before touching anything,
confirms, snapshots the current database first (so a bad dump rolls back
instead of leaving you empty; if that snapshot can't be taken and the
database has data, the restore refuses to proceed), swaps the database in
place, and then checks the restored database really has your tables and
accounts rather than trusting that nothing errored. If the app then does
not come back up on it, the restore loads its snapshot back.

Backups live **inside the install folder** by default — the whole install
stays one self-contained directory. Because of that, `./oikonome.sh
uninstall` offers to move them out to `~/oikonome-backups-<project>-<date>/`
before deleting the folder (Enter keeps them; deleting them is the
explicit opt-in). For scheduled backups, run `scripts/backup.sh` from
cron/systemd with a destination **outside** the install — ideally another
disk or machine — and set that same absolute path as
`OIKONOME_BACKUP_DIR` in `docker/.env` (then `./oikonome.sh install`;
if the console's host command watcher is already installed, also re-run
`sudo sh scripts/install-host-command-watcher.sh <install-dir>` so the
console's Restore looks in the new folder — the console flags a watcher
that is out of date).
That one setting is the folder `./oikonome.sh backup` and `restore` use
and the one the admin console's backup card watches and restores from;
the card names it. Dumps written anywhere else still restore by path
(`./oikonome.sh restore /path/to/dump.sql.gz`), but the console cannot
see them and will report no recent backup. Backup, restore, reset, uninstall and an upgrade (from
its pull and pre-upgrade snapshot through the rebuild, and any Postgres
major-version migration) share one lock (`.maintenance.lock` in
the install folder), so a scheduled dump never starts under a restore or
a migration; a run that cannot get the lock within ten minutes
says so and exits instead of racing. The wait is `OIKONOME_MAINT_LOCK_WAIT`
(seconds), read from the shell environment of the command — not from
`docker/.env` — so set it inline (`OIKONOME_MAINT_LOCK_WAIT=1200
./oikonome.sh backup`) or in the cron/systemd unit's environment. Whatever
takes the lock records itself in `.maintenance.lock.holder` (process,
time, operation) and removes that file when it lets go, so a skipped
backup names what it waited for. A holder that still has the lock open
is reported as running, however long it takes — a Postgres migration of a
large database can take hours. The lock is reported as **stale** only
when the recorded process no longer holds it and took it more than
`OIKONOME_MAINT_LOCK_STALE_HOURS` (default 6) ago: then something that
inherited it is holding on, and the message says how to find it
(`fuser .maintenance.lock`). Never delete `.maintenance.lock` to clear a
skip — a backup that then starts under a running restore or migration can
capture a half-loaded database. Back up the encryption master key too — a dump alone
can't decrypt stored bank-connection secrets; see
[master-key.md](master-key.md) (backup + rotation).

The database lives in `data/postgres` inside the install folder, so the
whole install — code, config (`docker/.env`), and data — is one directory:
`./oikonome.sh uninstall` stops the stack and removes it all. On rootless
Podman the files stay owned by you (the installer maps your user to the
container's postgres user), and the uninstaller falls back to
`podman unshare` if a database file ended up container-owned anyway; on
rootful Docker the data files are owned by the postgres uid, so deleting
them by hand needs sudo. `OIKONOME_DATA_DIR` in
`docker/.env` moves the database elsewhere if you want it separate — the
uninstaller then leaves that data in place and asks (typed confirmation)
before deleting it.

What `reset` and `uninstall` keep, and what they lose:

- **`./oikonome.sh reset`** deletes the database (on an external
  database, it drops and recreates it there) and replaces the generated
  database and queue passwords and the setup link. It keeps the code,
  your backups and every other setting in `docker/.env` — port, HTTPS
  domain, bind address, external-database connection, admin console,
  SMTP, LLM, data folder, provider keys. The master key is kept too
  (`--new-key` replaces it), and either way a copy is saved owner-only
  beside the backups before anything is deleted; a value in `docker/.env`
  that is not a valid key stops the reset with nothing touched. Linked
  banks are disconnected at the provider first, so after the reset —
  and after restoring any backup — banks are linked again; the key is
  what lets a backup's two-factor seeds, webhook secrets and API keys
  come back.
- **`./oikonome.sh uninstall`** removes the folder: code, `docker/.env`,
  the database inside it, and the backups unless you keep them (kept
  backups take their master key with them). Linked banks are disconnected
  at the provider first. A database that lives elsewhere is never
  deleted by it: an `OIKONOME_DATA_DIR` outside the folder is offered for
  deletion at the end (typed confirmation), and an external database is
  left on its server untouched — drop it there yourself. For both, the
  `docker/.env` that opens it (passwords and master key) is saved
  owner-only before anything is removed, and their bank links are left
  as they are.

**Podman and reboots.** Every service is `restart: unless-stopped`.
Docker's daemon applies that at boot, so a Docker host brings the
instance back by itself (with `docker.service` enabled). Podman has no
daemon, so a rebooted Podman host leaves the instance down. That holds
too where `docker` is podman's wrapper (the podman-docker package); the
scripts recognise it and treat the host as Podman. Run
`./oikonome.sh start-at-boot` once: it writes a systemd unit that starts
the stack at boot — a user unit for rootless Podman, with lingering
turned on (`loginctl enable-linger`) so it starts without a login, or a
system unit when run as root. `./oikonome.sh start-at-boot off` removes
it.
Filesystem snapshots of the folder work; `pg_dump` stays the recommended
routine backup.

## Troubleshooting

Start at **/doctor** — every common failure (dead worker, bad SMTP, stale
sync, missing master key) is a named check there. Filing a bug? Attach the
diagnostic bundle from that page (never contains tokens or transactions,
hostnames, bank names or collector labels).
