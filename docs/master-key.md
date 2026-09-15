# The master key: backup and rotation

`OIKONOME_MASTER_KEY` (in `docker/.env`) is the root of Oikonome's
at-rest encryption. Aggregator tokens and API keys encrypt under a
per-household data key, which is itself stored wrapped by the master
key; TOTP seeds and the instance's web-push signing key encrypt directly
under it. The database alone is not enough to read those secrets — and
that is the point.

## Back it up

The installer generates the key into `docker/.env`. **A database backup
without the master key cannot decrypt its stored connection secrets** —
your money data restores fine, but every bank connection would need
re-linking and TOTP re-enrolling.

- Keep a copy of the whole `docker/.env` (or at least the
  `OIKONOME_MASTER_KEY=` line) somewhere that is *not* this machine: a
  password manager entry is ideal.
- Re-capture it whenever it changes (rotation below).
- After any restore, verify key/data agreement:

  ```bash
  docker compose -f docker/compose.yaml exec app \
    python -m oikonome.cli keycheck
  ```

  (`./oikonome.sh restore` runs this for you and warns on mismatch.)

## Rotate it

Rotate on a schedule if your policy asks for one, and immediately if the
key may have leaked (a copied `.env`, a compromised backup host). Rotation
re-wraps the stored ciphertexts under the new key — bank connections,
TOTP and browser push notifications keep working; nothing needs
re-linking or re-subscribing.

1. Generate a new key:

   ```bash
   openssl rand -base64 32 | tr '+/' '-_'
   ```

2. In `docker/.env`: set `OIKONOME_MASTER_KEY=` to the **new** key. The
   old key never needs to enter `.env` or `compose.yaml` — pass it inline
   to the one command that needs it:

   ```bash
   docker compose -f docker/compose.yaml up -d   # app picks up the new key
   docker compose -f docker/compose.yaml exec \
     -e OIKONOME_MASTER_KEY_OLD=<old key> app \
     python -m oikonome.cli rotate-master-key
   ```

   The command is idempotent — if it is interrupted, re-run it; rows
   already on the new key are skipped. If it reports MISMATCH, nothing
   was changed (wrong old key — every ciphertext must decrypt under one
   of the two keys).

3. Verify, then clean up:

   ```bash
   docker compose -f docker/compose.yaml exec app \
     python -m oikonome.cli keycheck
   ```

   Nothing to remove from `docker/.env` — step 2 deliberately never put
   the old key there, and compose does not forward it anyway. Update the
   key in your password-manager backup, and **keep the old key until a restore
   test of your newest backup passes keycheck with the new key** — dumps
   taken before the rotation decrypt only under the old one.

Podman installs: replace `docker compose` with `podman compose` (or run
`./oikonome.sh status` to see the exact engine your install uses).
