#!/usr/bin/env bash
# Oikonome uninstaller — the counterpart to install.sh.
# Stops the stack and removes this ENTIRE install folder (code, config,
# and DATA — everything lives in here by design). Run from the install
# folder:  ./uninstall.sh
set -euo pipefail
cd "$(dirname "$0")"
HERE=$(pwd)
# OIKONOME_DATA_DIR can point the database OUTSIDE this folder — read it
# now (docker/.env is gone once the folder is removed) so the end of the
# run can say the data survived and offer to delete it too. Relative
# values resolve against docker/, exactly as compose does.
EXT_DATA=$(grep "^OIKONOME_DATA_DIR=" docker/.env 2>/dev/null | cut -d= -f2- || true)
if [ -n "$EXT_DATA" ]; then
  case $EXT_DATA in
    /*) : ;;
    *)  EXT_DATA="$HERE/docker/$EXT_DATA" ;;
  esac
  case $EXT_DATA in
    "$HERE"/*) EXT_DATA="" ;;   # inside the install tree — removed with it
  esac
fi
# Name of the instance about to die — the compose project name, else the
# folder name; plus its port, for a second identifying fact on the banner.
INAME=$(grep "^COMPOSE_PROJECT_NAME=" docker/.env 2>/dev/null | cut -d= -f2- || true)
INAME=${INAME:-$(basename "$HERE")}
IPORT=$(grep "^OIKONOME_PORT=" docker/.env 2>/dev/null | cut -d= -f2- || true)

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
echo "  ##  PERMANENT DELETE — this removes the instance, its      ##"
echo "  ##  DATABASE, and everything below. This CANNOT be undone. ##"
echo "  #############################################################"
echo
echo "  You are about to delete:"
echo
big_banner "$INAME"
echo
echo "    folder: $HERE"
[ -n "$IPORT" ] && echo "    port:   $IPORT"
echo
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
  PROJ=$(grep "^COMPOSE_PROJECT_NAME=" docker/.env 2>/dev/null | cut -d= -f2)
  SAFE="$HOME/oikonome-backups-${PROJ:-$(basename "$HERE")}-$(date +%Y%m%d)"
  echo
  echo "  This install holds $N database backup(s) in ./backups."
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
      echo "  ✓ Backups moved to $SAFE"
      ;;
  esac
fi
# tolerate a RE-RUN over a half-removed tree (a prior uninstall that
# couldn't delete container-owned files leaves no docker/ dir — the
# compose teardown already happened; skip straight to the removal ladder)
cd docker 2>/dev/null || { HALF_REMOVED=1; }
if [ -z "${HALF_REMOVED:-}" ]; then
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then C="docker compose";
elif command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then C="podman compose";
else C="podman-compose"; fi
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
PROJ=$(grep "^COMPOSE_PROJECT_NAME=" .env 2>/dev/null | cut -d= -f2)
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
if [ -e "$HERE/data" ] && command -v docker >/dev/null 2>&1; then
  docker run --rm -v "$HERE/data":/d busybox \
    find /d -mindepth 1 -delete >/dev/null 2>&1 || true
  rm -rf "$HERE" 2>/dev/null || true
fi
# last resort: a passwordless sudo (server deploy users) finishes silently;
# anything else still gets the manual instruction
if [ -e "$HERE" ]; then
  sudo -n rm -rf "$HERE" 2>/dev/null || true
fi
if [ -e "$HERE" ]; then
  echo "  ✗ Couldn't remove everything under $HERE (container-owned files?)."
  echo "    Run: sudo rm -rf '$HERE'"
  exit 1
fi
echo "  ✓ Oikonome instance removed: $HERE"
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
    echo "  ✓ Database removed: $EXT_DATA"
  else
    echo "  Kept: $EXT_DATA"
  fi
fi
