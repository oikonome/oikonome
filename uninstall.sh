#!/usr/bin/env bash
# Oikonome uninstaller — the counterpart to install.sh.
# Stops the stack and removes this ENTIRE install folder (code, config,
# and DATA — everything lives in here by design). Run from the install
# folder:  ./uninstall.sh
set -euo pipefail
cd "$(dirname "$0")"
HERE=$(pwd)
# dotenv_get / canon_path / data_dir_resolve — the docker/.env reads shared
# with oikonome.sh and install.sh, so all three agree with compose
if [ -f "$HERE/scripts/env-file.sh" ]; then
  . "$HERE/scripts/env-file.sh"
elif [ -f docker/.env ]; then
  # without the reader, where the database lives cannot be told — and
  # guessing "inside" would release its banks and drop the docker/.env a
  # surviving database needs
  echo "  ✗ scripts/env-file.sh is missing from $HERE — this install folder is incomplete; nothing was touched."
  exit 1
else
  # a re-run over a half-removed tree: docker/.env is already gone, so
  # there is nothing to read
  dotenv_get() { :; }; data_dir_resolve() { :; }; canon_path() { printf '%s\n' "$1"; }
fi
# OIKONOME_DATA_DIR can point the database OUTSIDE this folder — read it
# now (docker/.env is gone once the folder is removed) so the end of the
# run can say the data survived and offer to delete it too. Read the way
# compose reads it (quotes, ` # comment`, `export`), relative values
# resolved against docker/, and both sides canonicalised before the
# "inside the install folder?" test: `../../x` relative to docker/ is
# outside, though its spelling starts with this folder's path, and a
# database judged inside is treated as deleted — banks released, its
# docker/.env (passwords + master key) not kept.
EXT_DATA=$(data_dir_resolve "$HERE/docker" "")
if [ -n "$EXT_DATA" ]; then
  HERE_REAL=$(canon_path "$HERE")
  case $EXT_DATA in
    "$HERE_REAL"|"$HERE_REAL"/*) EXT_DATA="" ;;   # inside the install tree — removed with it
  esac
fi
# Name of the instance about to die — the compose project name, else the
# folder name; plus its port, for a second identifying fact on the banner.
INAME=$(dotenv_get docker/.env COMPOSE_PROJECT_NAME)
INAME=${INAME:-$(basename "$HERE")}
IPORT=$(dotenv_get docker/.env OIKONOME_PORT)
# The master key, held in memory only: every backup and every kept external
# database was encrypted under it (bank connections, webhook secrets,
# two-factor seeds), and docker/.env, its only copy, goes with the folder.
# Whatever is kept below is kept WITH the key, so it can still be restored.
# Read as compose reads it (`export`, quotes, last assignment wins): the
# key saved must be the one the stack encrypted the backups under.
MKEY=$(dotenv_get docker/.env OIKONOME_MASTER_KEY)
# An external (managed) database — COMPOSE_FILE=…external-db… — is not in
# this folder at all, and this script never drops it: it survives the
# uninstall with every account and the whole ledger in it. Say so, and keep
# the docker/.env that opens it (provider admin password, role passwords,
# master key), or it can neither be reopened nor decrypted.
EXT_DB=""
case $(dotenv_get docker/.env COMPOSE_FILE) in *external-db*) EXT_DB=1 ;; esac
EXT_DB_NAME=$(dotenv_get docker/.env OIKONOME_DB_NAME)
EXT_DB_HOST=$(dotenv_get docker/.env OIKONOME_DB_HOST)
ENV_COPY=""
if [ -n "$EXT_DATA" ] || [ -n "$EXT_DB" ]; then
  ENV_COPY=$(cat docker/.env 2>/dev/null || true)
fi
PROJ=$(dotenv_get docker/.env COMPOSE_PROJECT_NAME)

# big_banner TEXT — render TEXT very large so the wrong instance is obvious
# before anyone confirms. figlet/toilet if present (nicer); otherwise a heavy
# boxed, wide-spaced, uppercased fallback that needs no dependency.
big_banner() {
  local t=$1
  if command -v figlet >/dev/null 2>&1; then
    figlet -w 120 -- "$t" | sed 's/^/    /'
  elif command -v toilet >/dev/null 2>&1; then
    toilet -w 120 -- "$t" | sed 's/^/    /'
  else
    local up spaced
    up=$(printf '%s' "$t" | tr '[:lower:]' '[:upper:]')
    spaced=$(printf '%s' "$up" | sed 's/./& /g')
    echo "    ========================================================"
    echo
    echo "        $spaced"
    echo
    echo "    ========================================================"
  fi
}

echo
echo "  #############################################################"
if [ -n "$EXT_DB" ] || [ -n "$EXT_DATA" ]; then
  # the database is not in this folder — the banner must not claim it
  echo "  ##  PERMANENT DELETE — this removes the instance and       ##"
  echo "  ##  everything below. This CANNOT be undone.               ##"
else
  echo "  ##  PERMANENT DELETE — this removes the instance, its      ##"
  echo "  ##  DATABASE, and everything below. This CANNOT be undone. ##"
fi
echo "  #############################################################"
echo
echo "  You are about to delete:"
echo
big_banner "$INAME"
echo
echo "    folder: $HERE"
[ -n "$IPORT" ] && echo "    port:   $IPORT"
echo
if [ -n "$EXT_DB" ]; then
  echo "  Its database is on an external server (${EXT_DB_NAME:-oikonome} on ${EXT_DB_HOST:-?})"
  echo "  and is NOT deleted: drop it there yourself if it should go. Its"
  echo "  docker/.env is saved, owner-only, in your home folder so it can still be opened."
  echo
elif [ -n "$EXT_DATA" ]; then
  echo "  Its database folder is outside the install ($EXT_DATA):"
  echo "  you are asked about it separately at the end."
  echo
else
  echo "  Linked banks are disconnected at the provider first (their access"
  echo "  tokens exist only in this database)."
  echo
fi
# OIKONOME_ASSUME_YES: the admin-console host-agent already did the typed
# confirm + single-use nonce in the browser and a root watcher validated the
# request — skip the interactive prompt (set ONLY by that watcher).
if [ -z "${OIKONOME_ASSUME_YES:-}" ]; then
  # Require typing the INSTANCE NAME itself — not a generic word — so a
  # muscle-memory confirmation can't destroy the wrong instance. You have to
  # read the big name above and type it back.
  printf "  Type the instance name shown above ('%s') to confirm: " "$INAME"
  # prefer the terminal (curl|bash safe); fall back to stdin so scripted runs
  # (./oikonome.sh uninstall in CI) can pipe the confirmation in
  if { : </dev/tty; } 2>/dev/null; then
    read -r CONFIRM </dev/tty || CONFIRM=""
  else
    read -r CONFIRM || CONFIRM=""
  fi
  if [ "$CONFIRM" != "$INAME" ]; then
    echo "  Name did not match — aborted, nothing touched."; exit 1
  fi
fi
# ./backups lives INSIDE the folder about to be removed — "I backed up,
# then uninstalled" must not destroy the backups. Default: move them out
# to a sibling of $HOME; deleting them is the explicit opt-in.
if ls "$HERE"/backups/oikonome-*.sql.gz >/dev/null 2>&1; then
  N=$(ls -1 "$HERE"/backups/oikonome-*.sql.gz | wc -l | tr -d ' ')
  SAFE="$HOME/oikonome-backups-${PROJ:-$(basename "$HERE")}-$(date +%Y%m%d)"
  echo
  echo "  This install holds $N database backup(s) in ./backups."
  [ -n "$MKEY" ] && echo "  Kept backups take the master key they need with them (owner-only file)."
  printf "  Keep them? They move to %s  [Y/n]: " "$SAFE"
  if { : </dev/tty; } 2>/dev/null; then
    read -r KEEP </dev/tty || KEEP=""
  else
    read -r KEEP || KEEP=""
  fi
  case $KEEP in
    n|N|no|NO) echo "  OK — the backups go with the install." ;;
    *)
      mkdir -p "$SAFE"
      mv "$HERE"/backups/oikonome-*.sql.gz "$SAFE"/
      # also any key an earlier `oikonome.sh reset --new-key` saved here
      mv "$HERE"/backups/master-key-*.env "$SAFE"/ 2>/dev/null || true
      echo "  ✓ Backups moved to $SAFE"
      if [ -n "$MKEY" ]; then
        if ( umask 077
             printf '# master key the backups in this folder were taken under (docs/master-key.md)\nOIKONOME_MASTER_KEY=%s\n' \
               "$MKEY" > "$SAFE/master-key.env" ); then
          chmod 600 "$SAFE/master-key.env"
          echo "  ✓ Their master key: $SAFE/master-key.env — without it a restore"
          echo "    loses its two-factor seeds, webhook secrets and API keys; move it"
          echo "    to a password manager. (Banks released below are linked again"
          echo "    after any restore.)"
        else
          echo "  ✗ Couldn't save the master key next to the backups — aborted, nothing removed."
          echo "    The backups are in $SAFE; the key is still in $HERE/docker/.env."
          exit 1
        fi
      fi
      ;;
  esac
fi
# The docker/.env a kept database needs goes beside it NOW, before anything
# is stopped or deleted: written at the end, it is lost whenever the
# removal below stops early (container-owned files, no sudo), and with it
# the passwords and the master key that kept database cannot do without.
KEEP_ENV=""
if [ -n "$ENV_COPY" ]; then
  if [ -n "$EXT_DATA" ]; then
    KEEP_ENV="${EXT_DATA%/}.docker.env"
  else
    KEEP_ENV="$HOME/oikonome-${INAME}-database-$(date +%Y%m%d-%H%M%S).docker.env"
  fi
  if ( umask 077; printf '%s\n' "$ENV_COPY" > "$KEEP_ENV" ); then
    chmod 600 "$KEEP_ENV"
  else
    echo "  ✗ Couldn't save docker/.env to $KEEP_ENV — aborted, nothing removed."
    echo "    The database kept after an uninstall cannot be opened without it."
    exit 1
  fi
fi
# tolerate a RE-RUN over a half-removed tree (a prior uninstall that
# couldn't delete container-owned files leaves no docker/ dir — the
# compose teardown already happened; skip straight to the removal ladder)
cd docker 2>/dev/null || { HALF_REMOVED=1; }
if [ -z "${HALF_REMOVED:-}" ]; then
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then C="docker compose";
elif command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then C="podman compose";
else C="podman-compose"; fi
case $C in docker*) ENGINE=docker ;; *) ENGINE=podman ;; esac
# Linked banks hold their access tokens only in this database. When the
# database dies with the folder, release them at the provider first (the
# same release a reset makes), or every Item keeps billing and keeps its
# bank consent with no handle left to remove it. A database that outlives
# the uninstall keeps its tokens, so its links stay as they are.
if [ -z "$EXT_DB" ] && [ -z "$EXT_DATA" ]; then
  APPC=$($ENGINE ps \
           --filter "label=com.docker.compose.project=${PROJ:-oikonome}" \
           --filter "label=com.docker.compose.service=app" \
           --format '{{.Names}}' 2>/dev/null | head -1 || true)
  if [ -n "$APPC" ]; then
    echo "  Disconnecting linked banks at the provider…"
    $ENGINE exec "$APPC" python -m oikonome.db.reset --release-links \
      || echo "  ! Some bank links could not be released (listed above) — remove them in the provider's dashboard."
  else
    echo "  ! The app isn't running, so linked banks can't be released here — remove them"
    echo "    in the provider's dashboard (Plaid: Items), or they keep billing."
  fi
fi
# -v: drop the project's named volumes too (the bundled-Ollama model
# cache is gigabytes and would otherwise outlive the instance).
# Secret placeholders: compose.yaml marks them required, and a mangled/
# deleted .env must not stop the down from running (values don't matter
# for stopping containers).
POSTGRES_PASSWORD=${POSTGRES_PASSWORD:-gone} \
  OIKONOME_APP_PASSWORD=${OIKONOME_APP_PASSWORD:-gone} \
  OIKONOME_ADMIN_PASSWORD=${OIKONOME_ADMIN_PASSWORD:-gone} \
  OIKONOME_MASTER_KEY=${OIKONOME_MASTER_KEY:-gone} \
  $C down -v >/dev/null 2>&1 || true
# each install owns image oikonome:<project> — remove it too.
# An install without a project name shares its tag (oikonome:local
# historically, oikonome:oikonome now) with whatever else built it, so
# those are left alone.
if [ -n "$PROJ" ]; then
  { docker rmi "oikonome:$PROJ" 2>/dev/null \
    || podman rmi "oikonome:$PROJ" 2>/dev/null; } >/dev/null || true
fi
fi # HALF_REMOVED: no docker/ dir — teardown already ran on a prior pass
cd /
# rootless podman leaves postgres's files owned by a subuid — plain rm
# gets EPERM on data/postgres, so retry inside the user namespace
rm -rf "$HERE" 2>/dev/null || true
if [ -e "$HERE" ] && command -v podman >/dev/null 2>&1; then
  podman unshare rm -rf "$HERE" 2>/dev/null || true
fi
# rootful docker leaves postgres's files owned by the container uid (e.g.
# 999) — a throwaway ROOT container clears them without host sudo, so an
# unattended/non-root caller (the admin-console host-agent) can finish.
# ops-state/ gets the same pass: the host-watcher installers run as root,
# and one installed before they handed their markers to the owner left a
# root-owned directory there.
if [ -e "$HERE" ] && command -v docker >/dev/null 2>&1; then
  for d in data ops-state; do
    [ -e "$HERE/$d" ] || continue
    docker run --rm -v "$HERE/$d":/d busybox \
      find /d -mindepth 1 -delete >/dev/null 2>&1 || true
  done
  rm -rf "$HERE" 2>/dev/null || true
fi
# last resort: a passwordless sudo (server deploy users) finishes silently;
# anything else still gets the manual instruction
if [ -e "$HERE" ]; then
  sudo -n rm -rf "$HERE" 2>/dev/null || true
fi
if [ -e "$HERE" ]; then
  echo "  ✗ Couldn't remove everything under $HERE (container- or root-owned files?)."
  echo "    Left behind:"
  find "$HERE" -mindepth 1 -maxdepth 3 2>/dev/null | head -n 10 | sed 's/^/      /'
  echo "    Run: sudo rm -rf '$HERE'"
  exit 1
fi
echo "  ✓ Oikonome instance removed: $HERE"
if [ -n "$EXT_DB" ]; then
  echo
  echo "  NOT deleted: the database ${EXT_DB_NAME:-oikonome} on ${EXT_DB_HOST:-the external server}."
  echo "  It still holds every account and the whole ledger. Drop it with your"
  echo "  provider's tools if it should go (DROP DATABASE ${EXT_DB_NAME:-oikonome}; and"
  echo "  the roles oikonome_app, oikonome_admin)."
  echo "  Its docker/.env (passwords + master key): $KEEP_ENV"
  echo "    — copy it back to docker/.env to reopen it; keep it private."
fi
# external database (OIKONOME_DATA_DIR outside the install tree): never
# deleted silently — keeping the data is the default, deletion needs the
# same explicit typed confirmation as the uninstall itself
if [ -n "$EXT_DATA" ] && [ -e "$EXT_DATA" ]; then
  echo
  echo "  Note: this instance kept its DATABASE outside the install folder:"
  echo "    $EXT_DATA"
  echo "  It was NOT removed (a future install pointing OIKONOME_DATA_DIR"
  echo "  there picks it back up — with the matching docker/.env passwords)."
  printf "  Type 'delete-data' to delete it too, or Enter to keep it: "
  if { : </dev/tty; } 2>/dev/null; then
    read -r WIPE </dev/tty || WIPE=""
  else
    read -r WIPE || WIPE=""
  fi
  if [ "$WIPE" = "delete-data" ]; then
    echo "  ! Its linked banks were not released (the database was being kept):"
    echo "    remove them in the provider's dashboard (Plaid: Items)."
    # same subuid story as above: rootless podman's postgres files need
    # the user namespace to delete
    rm -rf "$EXT_DATA" 2>/dev/null || true
    if [ -e "$EXT_DATA" ] && command -v podman >/dev/null 2>&1; then
      podman unshare rm -rf "$EXT_DATA" 2>/dev/null || true
    fi
    if [ -e "$EXT_DATA" ]; then
      echo "  ✗ Couldn't remove $EXT_DATA (container-owned files?)."
      echo "    Run: sudo rm -rf '$EXT_DATA'"
      exit 1
    fi
    # its passwords and key go with it
    [ -n "$KEEP_ENV" ] && rm -f "$KEEP_ENV"
    echo "  ✓ Database removed: $EXT_DATA"
  else
    echo "  Kept: $EXT_DATA"
    if [ -n "$KEEP_ENV" ]; then
      echo "  Its docker/.env (passwords + master key): $KEEP_ENV"
      echo "    — copy it back to docker/.env on the next install; keep it private."
    fi
  fi
fi
