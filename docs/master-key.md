# The master key: backup and rotation

`OIKONOME_MASTER_KEY` (in `docker/.env`) is the root of Oikonome's
at-rest encryption. Aggregator tokens, API keys and webhook signing
secrets encrypt under a per-household data key, which is itself stored
wrapped by the master key; TOTP seeds and the instance's web-push signing key encrypt directly
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
- `./oikonome.sh reset` and `./oikonome.sh uninstall` write the key,
  owner-only, beside the backups they keep
  (`backups/master-key-before-reset-<date>.env`,
  `~/oikonome-backups-<project>-<date>/master-key.env`). Move it into your
  password manager and delete the file.
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
re-linking or re-subscribing. Webhooks keep their signing secrets. What
does not survive is the links in emails already sent — the one-tap
*Needs you* buttons and the unsubscribe link are signed from the key, so
they stop working; the next email carries fresh ones.

1. Generate a new key:

   ```bash
   openssl rand -base64 32 | tr '+/' '-_'
   ```

2. In `docker/.env`: set `OIKONOME_MASTER_KEY=` to the **new** key. The
   old key never needs to enter `.env` or `compose.yaml`. It also must not
   be typed into the command line: a key written there is saved in your
   shell history, and other accounts on the host can read it in `ps`
   while the command runs. Type it at a hidden prompt instead, and hand it
   to the one command that needs it on standard input:

   ```bash
   docker compose -f docker/compose.yaml up -d   # app picks up the new key
   read -rs -p 'Old master key: ' OLD_KEY; echo
   printf '%s\n' "$OLD_KEY" | docker compose -f docker/compose.yaml exec -T app \
     sh -c 'read -r OIKONOME_MASTER_KEY_OLD && export OIKONOME_MASTER_KEY_OLD &&
            exec python -m oikonome.cli rotate-master-key'
   unset OLD_KEY
   ```

   (`printf` is a shell builtin, so the key never becomes a process
   argument.) If the old key is in an owner-only file instead — such as
   the `master-key-*.env` that `./oikonome.sh reset --new-key` saves
   beside the backups — read it from there in place of the prompt:

   ```bash
   sed -n 's/^OIKONOME_MASTER_KEY=//p' backups/master-key-before-reset-<date>.env \
     | docker compose -f docker/compose.yaml exec -T app \
         sh -c 'read -r OIKONOME_MASTER_KEY_OLD && export OIKONOME_MASTER_KEY_OLD &&
                exec python -m oikonome.cli rotate-master-key'
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
