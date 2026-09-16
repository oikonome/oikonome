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
./oikonome.sh reset             # wipe data + secrets, fresh setup wizard (confirm-gated)
./oikonome.sh reset-data [tenant-id]  # wipe the data but keep logins; wizard runs again (confirm-gated)
./oikonome.sh reset-password <email>  # locked out? one-time reset link, no SMTP needed
./oikonome.sh clear-2fa <email> # lost the authenticator AND the recovery codes (confirm-gated)
./oikonome.sh uninstall         # remove the whole instance (confirm-gated)
```

`demo`, `https`, `llm` and `script` are covered in their own sections
below; `./oikonome.sh help` prints the full usage line.

(`./install.sh` and `./uninstall.sh` still work directly, if you prefer.)

The installer generates the database passwords and the encryption master key
(protects bank tokens at rest), builds, and starts everything (SMTP for
the daily email is not asked for here — the setup wizard's email step or
`docker/.env` takes it later). Re-running it is always safe —
it keeps your `.env` and data. Prefer doing it by hand? Copy
`docker/.env.example` to `docker/.env`, fill every value (the master key is
32 random bytes, **urlsafe** base64: `openssl rand -base64 32 | tr '+/' '-_'`),
then `docker compose up -d` from `docker/`.

The installer prints your instance's URL when it's up —
http://localhost:8042 by default (if 8042 is taken, it auto-picks the next
free port; `OIKONOME_PORT` in `docker/.env` pins it). Open it — the
**setup wizard** creates your account. Then, in order:

1. **Connect** — **Connect an account** links a bank, card, or brokerage
   (hosted goes straight to Plaid; self-host gets the provider grid —
   Plaid / MX / SimpleFIN / scripts / files). Linking pulls transactions,
   holdings and liabilities. Everything you've linked is listed
   underneath, one row each, with a status dot, how many accounts came
   with it, when it last pulled, and a Disconnect button. Click **Done
   connecting** when you've added everything. Optional file imports for
   deeper history live under Connect / Accounts. When the instance enforces an
   institution limit, the card also says how many of the allowed
   institutions are connected, and the Connect button is disabled once
   they're spent (see the accounts guide); self-host is never capped.
2. **Bills** — waits for your bank's full history to arrive (up to two
   years, delivered minutes after linking; you see each connection's
   count and how far back it reaches), then approve/reject the bills
   and paychecks detected from it. The wait is deliberate: income, bills
   and the pre-filled budget are all derived from that history.
   Rejections are remembered.
3. **Budgets** — Food and Everything-else per month plus expected income,
   pre-filled from your data. That's all the daily verdict needs.
4. **Daily email delivery** (self-host only — hosted skips it) — optional:
   give the instance an outbound SMTP account so the daily verdict and
   alerts can reach your inbox, with guides for the common providers.
5. **Done** — a recap, and how the daily verdict reaches you: **Email**,
   **Push notification**, **Both** or **Off**, and the hour. Choosing push
   registers the browser or phone you are on (the browser asks
   permission; on http:// it cannot, the phone app can). For email, pick
   **Verdict only** (one card: on budget or not, left to spend, pinned
   bills) or the **Full report** (the whole Today page).
   It goes to every login in your household — add a partner under
   Settings → Users to include them (self-host can also add any address
   under Settings → Email & Push; self-host needs SMTP first). Then you land
   on Today. Every step but the history wait is skippable, and the green
   **Finish setup** pill in the nav resumes at the first unfinished step —
   so does **Finish setup** under Settings → Wizards, in the browser and
   the phone app.

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
hourly sync and a manual ↻ sync of one connection show real categories
right away, not the next morning); the
nightly run does the heavier passes (Amazon item categories + summaries,
recurring-bill tagging). Three ways to run the LLM, easiest first:

- **Bundled local model** (recommended, private, free, zero config): the
  installer asks *"Enable built-in smart categorization?"* — press Enter to
  accept. It picks a permissively-licensed Qwen2.5 model sized to your
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
  Studio elsewhere? Point the wizard (or Settings → AI)
  at its URL (e.g.
  `http://localhost:11434`) + model name, no API key.
- **Cloud**: any OpenAI-compatible API — endpoint URL + model name + your
  API key (encrypted at rest, never shown again).

Skip it and everything still works; categorization is just manual. Two
config layers: the wizard/Settings values are stored per tenant and **win**;
the `OIKONOME_LLM_URL` / `OIKONOME_LLM_MODEL` / `OIKONOME_LLM_API_KEY` env
vars in `.env` are the operator-level default used when no tenant values
are set.

## Email (SMTP)

Set in `.env`: `OIKONOME_SMTP_HOST`, `OIKONOME_SMTP_PORT` (587; use 2525 on cloud hosts that block 587),
`OIKONOME_SMTP_USER`, `OIKONOME_SMTP_PASSWORD`, `OIKONOME_SMTP_FROM`.
Any provider works (Fastmail, Proton bridge, SES SMTP, your own postfix).

STARTTLS certificates are verified. If your relay presents a
self-signed certificate — Proton Mail Bridge on localhost is the common
case — set `OIKONOME_SMTP_NO_VERIFY=1` to skip verification for that
relay (the connection is still encrypted, just not authenticated).

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
instance's Today page.

## HTTPS (optional)

A LAN-only install works fine over plain HTTP. Add TLS when you expose the
instance beyond your LAN (Secure cookies and remote access):

```
./oikonome.sh https oiko.example.com     # front the app with a TLS proxy
./oikonome.sh https off                  # back to plain http
```

This stands up a Caddy reverse proxy that fetches a real certificate
automatically and pins `OIKONOME_BASE_URL`. Caddy needs to complete a
cert challenge, so:

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

## Upgrades

```bash
./oikonome.sh install
```
(It pulls the latest code first when the folder is a git checkout, then
rebuilds — safe to re-run anytime.)
Migrations run automatically at start (advisory-locked; multi-node safe).
Roll back = restore your nightly `pg_dump` and pin the previous image tag
(`./oikonome.sh restore <file>` does the database side).

Most migrations are instant. A few add a column that Postgres has to
write onto every existing row, and those hold a lock on the table while
they run — on a ledger of hundreds of thousands of transactions that can
be a few minutes during which the app returns errors, so upgrade a large
instance when nobody is using it. The transaction search indexes are
built concurrently, so the ledger keeps working while they run, and an
upgrade interrupted mid-build is repaired by starting the instance again;
older index builds still run inside their migration's transaction.

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
accounts rather than trusting that nothing errored.

Backups live **inside the install folder** by default — the whole install
stays one self-contained directory. Because of that, `./oikonome.sh
uninstall` offers to move them out to `~/oikonome-backups-<project>-<date>/`
before deleting the folder (Enter keeps them; deleting them is the
explicit opt-in). For scheduled backups, run `scripts/backup.sh` from
cron/systemd with a destination **outside** the install — ideally another
disk or machine. Backup, restore, reset and uninstall share one lock
(`.maintenance.lock` in the install folder), so a scheduled dump never
starts under a restore; a run that cannot get the lock within ten minutes
says so and exits instead of racing. The wait is `OIKONOME_MAINT_LOCK_WAIT`
(seconds), read from the shell environment of the command — not from
`docker/.env` — so set it inline (`OIKONOME_MAINT_LOCK_WAIT=1200
./oikonome.sh backup`) or in the cron/systemd unit's environment. Back up the encryption master key too — a dump alone
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
Filesystem snapshots of the folder work; `pg_dump` stays the recommended
routine backup.

## Troubleshooting

Start at **/doctor** — every common failure (dead worker, bad SMTP, stale
sync, missing master key) is a named check there. Filing a bug? Attach the
diagnostic bundle from that page (never contains tokens or transactions).
