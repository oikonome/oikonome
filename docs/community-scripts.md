# Community scripts — bring your own data source

Some data has no aggregator: a 401(k) portal, a bank Plaid dropped, a
retailer's order history. Oikonome's answer is deliberately NOT built-in
scraping — it's a **contract**: run your own script, on your own machine,
on your own credentials, and hand the results to the app through the same
doors everything else uses. Your script's brittleness stays yours; your
credentials never touch Oikonome.

These are **community scripts**. They live outside the app's support
boundary but inside its ecosystem: a documented plug-in shape, example
implementations in [`community-scripts/`](../community-scripts/), and
stable doors to push through.

## The contract (three doors, pick per source)

1. **The import hub** — POST a file to `/api/import` (multipart:
   `file`, `account_id`, `amount_sign`, and `new_account_name` to create
   the account the file lands in instead). CSV/OFX/QFX/QIF/PDF and several
   app exports are auto-detected; unknown CSVs get a column-mapping
   response. Idempotent: re-imports dedup against everything already
   known, and every batch is undoable.
2. **Purpose-built endpoints** — e.g. `POST /api/import/plan-activity`
   (`{plan, csv, balance?, mask?, provider?, institution?}`) for a
   workplace plan's activity CSV,
   `POST /api/import/costco` (`{receipts: […]}`) for Costco receipts with
   their line items, `POST /api/import/coinbase`
   (`{items: […], accounts: […], transactions: […]}`) for a normalized
   Coinbase snapshot — rows the source stopped reporting are retired
   inside the pushed prefix — and `POST /api/import/amazon`
   (`{orders: […]}`) for Amazon order history, which the app then matches
   to card charges itself. All of them take a script token. More arrive
   as they earn their keep.
3. **Container exec (self-host power move)** — for full-fidelity upserts
   (balances, restatements, removed rows), pipe JSON into the app
   container and use the sync layer directly:
   `podman exec -i <app-container> python -c '<bridge>'` with
   `oikonome.sync.base` upserts. This is how host-side collectors feed an
   instance; the [coinbase example](../community-scripts/coinbase/) shows
   the full bridge pattern.

## The script convention (what a community script looks like)

A community script is one folder — in this repo under
[`community-scripts/<name>/`](../community-scripts/), or anywhere in your
own repo with the same shape:

```
community-scripts/<name>/
├── script.toml          # manifest: what it is, which door it uses
├── README.md            # setup, credentials handling, how to run
├── <name>_sync.py       # the script itself (self-contained)
└── systemd/             # sample unit pair: timer + service with
    ├── oikonome-<name>.service   #   ExecStartPost push
    └── oikonome-<name>.timer
```

**The manifest (`script.toml`)** is a few lines of metadata so humans and
tooling can tell scripts apart:

```toml
name = "coinbase"
description = "One line: what it collects and where it pushes."
source-kind = "api"            # api | scrape | file-export
doors = ["container-exec"]     # import-hub | endpoint | container-exec
schedule-suggestion = "daily"  # how often it makes sense to run
```

**The README** must cover setup end-to-end, and in particular how
credentials are handled. The rule: **credentials never live in the shared
folder** — never a literal in the source, never a file committed next to
the script. The *shared* copy must be safe to publish as-is. In your
*installed* copy, the convention is a `credentials.json` beside the script
(that's what `script add` writes) or an env-var-named config file (e.g.
`~/.config/oikonome-scripts/<name>/`), your choice via the env override.

**The script** should be self-contained (stdlib plus at most a couple of
ubiquitous deps like `httpx`/`cryptography`), take everything
machine-specific from environment variables, derive **stable original
IDs** from source data so re-runs upsert instead of duplicating, and
support `--dry-run` so a user can see what would be pushed before
anything is written.

**The systemd pair** shows the intended split: `ExecStart=` collects,
`ExecStartPost=` pushes — so a failed collection never publishes
half-scraped data, and a failed push never marks the collection bad.

## Authenticating pushes: script tokens

HTTP-door scripts (import hub, endpoints) authenticate with a **script
token** — mint one in the web UI under **Settings → Connections → Script
tokens** and put it in your credentials file:

```json
{ "url": "http://localhost:8042", "api_token": "oik_…" }
```

Tokens are deliberately narrow: they can push through the import
endpoints and update a manual account balance — nothing else. They can't
read your ledger, can't change settings, survive password changes, and
are revocable one-by-one (the panel shows each token's last use). Prefer
one token per script, named after it. The older email+password login
fallback still works but puts your real login password on disk — migrate
when convenient. Container-exec scripts don't need either (they already
have host access).

Push-only means push-only, all the way down to the content:

- **No restore.** A token can push rows through `/api/import`, but a
  `.zip` there is a full-ledger *restore* — refused for tokens (403).
  Restore an export from a signed-in session instead.
- **Namespaced doors.** The endpoint doors only touch their own rows —
  the Coinbase door writes `coinbase-*` accounts and `coinbase:*`
  transactions and can't re-home or delete another source's data, no
  matter what ids or prefix the payload carries.
- **No cross-source pollution.** A token can push files through the
  import hub, but only into manual or import-fed accounts — never into
  an account a live aggregator (Plaid/SimpleFIN/MX) syncs, and never
  into an account another push script owns (Coinbase, a plan CSV).
- **No accidental wipes.** Absence from a push is only treated as a
  source restatement where the push actually restated: a payload that
  declares `full_replace: true` (the Coinbase example does — it always
  collects complete history) reconciles the whole account; anything else
  only prunes rows on the exact dates the push carries, so a partial,
  sparse, or empty collection never deletes existing history around it.
- **Rate-limited.** The doors accept a generous per-IP budget (well above
  any hourly collector); a runaway or leaked token can't hammer them.

### Behind a proxy, and pushing to more than one instance

Two things worth knowing before your first push to anything but localhost:

- **Send a User-Agent.** A hosted instance behind Cloudflare (or a similar
  edge) refuses the default `Python-urllib/3.x` signature outright — the
  request never reaches the app, and what you see is a `403` that reads
  exactly like a bad token. The examples set
  `User-Agent: oikonome-collector/1`; keep one in your own scripts.
- **`targets` sends the same payload to a second instance.** The examples
  accept an optional list beside the primary credentials:

  ```json
  {
    "url": "http://localhost:8042",
    "api_token": "oik_…",
    "targets": [
      {"name": "staging", "url": "https://staging.example.com",
       "api_token": "oik_…"}
    ]
  }
  ```

  Each target is pushed independently, one failure does not stop the next,
  and only the **primary** decides the exit code — a mirror that is down or
  mid-deploy must never make a nightly timer report that the real
  collection failed. Mint a separate token per instance: they are scoped
  per tenant, and one revocation should not silence every door.

## One-command install

The examples in this repo install with the manager:

```bash
./oikonome.sh script                 # list scripts + installed state
./oikonome.sh script add coinbase     # copy, credentials skeleton, timer
./oikonome.sh script remove coinbase  # disable timer (folder + creds kept)
```

`script add` copies the folder to `~/.local/share/oikonome/scripts/<name>`,
writes a `credentials.json` skeleton pointed at this instance (you paste a
token into it), and installs the systemd user units with paths rewritten —
then prints exactly what's left to do (mint the token, wire your collect
half, enable the timer). Nothing is scheduled until you enable it.

## Rules of the pattern

- **Schedule it yourself** (cron/systemd timer). Put the push in
  `ExecStartPost=` so a collection only publishes when it succeeded.
- **Original, stable IDs.** Derive each transaction id from source data
  (the source's own txn id, or a hash of date+amount+description at
  minimum) so re-runs upsert instead of duplicating.
- **Signs**: positive = money out. Match it at your boundary.
- **Never store credentials in anything shared.** Your collector's
  secrets are your own ops problem — the installed copy's
  `credentials.json` (or your env-pointed file) stays on your machine,
  never in a checkout you publish.
- **Expect breakage.** Sites change. A script failing must never take
  anything else down (the `-` prefix on ExecStartPost, isolated timers).

## Support boundary (please read)

Community scripts are **unsupported by design**. Oikonome supports the
doors — the import APIs, their dedup and idempotency guarantees — not
what you send through them or how you obtained it. Scraping may violate
your institution's terms of service; that trade-off is yours. If your
bank is supported by SimpleFIN or Plaid, use those instead — that's what
they're for. And if the app has a **native connector** for your source
(Coinbase does: link it under Accounts), prefer it over a script — the
examples exist to show the plug-in shape, not to replace connectors.

## Contributing a script

PRs adding a folder under `community-scripts/` are welcome if they follow
the convention above and contain **zero personal data** — no account ids,
usernames, masks, labels, or keys; parameterize everything. Scripts are
accepted as examples, not as supported product surface.
