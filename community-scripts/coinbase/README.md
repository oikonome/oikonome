# coinbase — community script (reference implementation)

Pulls every wallet and the full transaction history for each of your
Coinbase logins via the Coinbase App API (`/v2`, CDP ES256 keys), then
pushes accounts + balances + transactions into a self-hosted Oikonome
through the **container-exec door** using the app's own
`oikonome.sync.base` upserts.

> ## Prefer the native connector for Coinbase
>
> Oikonome ships a **native Coinbase connector** — link it in-app under
> Accounts (`POST /api/accounts/coinbase/link`). It stores your CDP key
> encrypted at rest, syncs hourly with everything else, and maintains
> per-coin `crypto_holdings` for net worth. **If you just want Coinbase
> data, use that.**
>
> This script exists because Coinbase is the one source with both a
> native connector *and* a host-side collector script, which makes it the
> ideal **reference implementation** of the
> community-script shape (see [docs/community-scripts.md](../../docs/community-scripts.md)):
> copy this folder when building a script for a source the app has no
> connector for.

## What it demonstrates

- **Manifest + folder convention** — `script.toml`, this README, one
  self-contained script, a systemd `.service`/`.timer` pair.
- **Credentials outside the folder** — key files live in a directory
  named by an env var, never next to the script.
- **`pull` / `push` split** — `ExecStart=` collects to a local payload
  file; `ExecStartPost=` pushes it, so a failed collection never
  publishes and a failed push never corrupts the collection.
- **Stable original ids** (`coinbase-<label>` accounts,
  `coinbase:<txn-uuid>` transactions) so re-runs upsert instead of
  duplicating — and so the native connector can later take over the same
  rows seamlessly.
- **The container-exec bridge** — payload JSON on stdin,
  `oikonome.sync.base` upserts inside the container (user category
  overrides and account classifications survive), soft `removed=1`
  reconciliation for rows the source restated, and a JSON verification
  summary printed back. The payload carries `full_replace: true` because
  this script always collects complete history — without that flag the
  app (and the bridge) only reconcile inside each account's pushed date
  window, so a partial or empty pull can never wipe history.
- **`--dry-run` + offline fixture** — the whole flow is exercisable
  without credentials or network.

## Setup

1. **Dependencies** (live pulls only; `push` and fixture mode are
   pure stdlib):

   ```bash
   pip install httpx cryptography
   ```

2. **Create a CDP API key** at Coinbase (Settings → API → Create key,
   *read-only* permissions, **ECDSA / ES256** — Ed25519 keys are rejected
   by `/v2`). Coinbase hands you a JSON file like:

   ```json
   {"name": "organizations/<org>/apiKeys/<key>", "privateKey": "-----BEGIN EC PRIVATE KEY-----..."}
   ```

3. **Store it OUTSIDE this folder**, one file per Coinbase login. The
   filename stem becomes your account label (pick anything;
   `main.json` → account `coinbase-main`):

   ```bash
   mkdir -p ~/.config/oikonome-scripts/coinbase
   cp ~/Downloads/cdp_api_key.json ~/.config/oikonome-scripts/coinbase/main.json
   chmod 600 ~/.config/oikonome-scripts/coinbase/*.json
   ```

4. **Environment** (all optional; defaults shown):

   | Variable | Default | Meaning |
   |----------|---------|---------|
   | `OIKONOME_COINBASE_KEYS` | `~/.config/oikonome-scripts/coinbase` | key-file directory |
   | `OIKONOME_COINBASE_STATE` | `~/.local/state/oikonome-scripts/coinbase/payload.json` | pull→push handoff file |
   | `OIKONOME_APP_CONTAINER` | `oikonome-app-1` | app container (podman-compose names it `oikonome_app_1`) |
   | `OIKONOME_CONTAINER_ENGINE` | `podman` | or `docker` |
   | `OIKONOME_TENANT_ID` | — | only needed with >1 active tenant |

## Run it

```bash
# offline first — no credentials, no network, no writes:
./coinbase_sync.py pull --fixture example-fixture.json --out /tmp/cb-payload.json
./coinbase_sync.py push --in /tmp/cb-payload.json --dry-run

# for real:
./coinbase_sync.py pull            # Coinbase API -> payload file
./coinbase_sync.py push --dry-run  # inspect what would be written
./coinbase_sync.py push            # upsert into the app container
```

`push` prints a verification summary from inside the container:
per-account row counts before/after, refreshed balance, and how many
stale (restated) rows were soft-removed.

## Schedule it

Copy the sample units from [`systemd/`](systemd/), adjust paths, then:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/oikonome-coinbase.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now oikonome-coinbase.timer
```

## Notes and caveats

- **Read-only by construction**: the script only ever GETs from Coinbase;
  create the API key read-only anyway.
- Coinbase accounts land **budget-excluded**: `type=investment`,
  `subtype=crypto`, every transaction `TRANSFER_IN/OUT` — crypto never
  touches spend math. Net worth comes from the account balance (wallet
  quantities × spot price, computed at pull time).
- The balance computation is **all-or-nothing**: if any held coin has no
  `/v2/prices/<code>-USD/spot` quote, the pull fails rather than publish
  a partial total.
- Unlike the native connector, this script does **not** maintain the
  per-coin `crypto_holdings` table — one more reason to prefer the
  connector for Coinbase specifically.
- Unsupported by design; see the
  [support boundary](../../docs/community-scripts.md#support-boundary-please-read).
