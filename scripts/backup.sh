#!/usr/bin/env bash
# Application-consistent Postgres backup for a compose-based Oikonome
# install. File-level backups (restic etc.) of a LIVE postgres data dir can
# be torn mid-write — this produces a dump that always restores.
#
# Usage:  ./scripts/backup.sh <install-dir> <backup-dir> [keep-days]
#   e.g.  /opt/oikonome/scripts/backup.sh /opt/oikonome /var/backups/oikonome 30
# Run it from cron/systemd nightly, with the backup dir OUTSIDE the install
# folder so removing the install never takes the backups with it.
set -euo pipefail
# A dump is the WHOLE database in plaintext SQL — every email, the entire
# transaction ledger, phone numbers, entity details. Only the aggregator
# tokens are encrypted at rest; nothing else is. So the dump gets the same
# owner-only treatment install.sh gives docker/.env: umask 077 means the
# gzip redirect below creates the file 0600 from its first byte (no
# world-readable window), and a backup directory we create ourselves lands
# 0700. A directory the operator already made is left alone — that is their
# call to share (the usage note above recommends an external location), and
# the 0600 files inside it are protected either way.
umask 077
INSTALL=${1:?install dir}; DEST=${2:?backup dir}; KEEP=${3:-30}
# Same lock ./oikonome.sh takes around restore / reset / uninstall, so a
# scheduled dump never starts under one of them (see maint_lock there).
# Non-blocking-ish: a backup that cannot get the lock within the wait is
# skipped with a message, not run against a database being replaced.
if command -v flock >/dev/null 2>&1; then
  exec 9>"$INSTALL/.maintenance.lock"
  flock -w "${OIKONOME_MAINT_LOCK_WAIT:-600}" 9 \
    || { echo "backup skipped: a restore/reset is running on this instance" >&2; exit 1; }
fi
cd "$INSTALL/docker"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  C="docker compose"
elif command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then
  C="podman compose"
else
  C="podman-compose"
fi
mkdir -p "$DEST"
STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$DEST/oikonome-$STAMP.sql.gz"
# Dump to a name the restore picker CANNOT see, and rename into place only
# once the file has been proven whole. This script is scheduled from
# cron/systemd (docs/quickstart.md), so it races every admin-triggered
# `./oikonome.sh restore|reset|uninstall` and the in-place major-version
# migration — any of which stops the postgres container or drops the
# database out from under a running pg_dump. Both gzip and psql exit 0 on
# an empty stream, so without this a dump cut off mid-write lands under the
# newest timestamp and later "restores" cleanly over a wiped database. The
# leading dot plus the .partial suffix keeps a half-written file out of the
# `oikonome-*.sql.gz` glob the picker and `newest_backup` list.
TMP="$DEST/.oikonome-$STAMP.sql.gz.partial"
trap 'rm -f "$TMP"' EXIT
fail() { rm -f "$TMP"; echo "backup FAILED: $*" >&2; exit 1; }

# --no-owner: dumps restore under ANY admin user (a managed provider has
# no `postgres` role — owner statements would abort ON_ERROR_STOP loads).
# External-db mode has no postgres container; dump through the
# migrate service's client tools + provider-admin DSN instead.
dump_to() {  # dump_to <path> — returns PG_DUMP's status, never gzip's.
             # gzip happily reports success on a stream that stopped
             # halfway, which is the whole failure this guards.
  local rc
  if grep -q "^COMPOSE_FILE=.*external-db" .env 2>/dev/null; then
    $C run --rm --no-deps -T migrate sh -c 'exec pg_dump --no-owner "$OIKONOME_ADMIN_DSN"' \
      | gzip > "$1"
    rc=${PIPESTATUS[0]}
  else
    $C exec -T postgres pg_dump --no-owner -U postgres oikonome \
      | gzip > "$1"
    rc=${PIPESTATUS[0]}
  fi
  return "$rc"
}
# `if !` is load-bearing: as a bare statement under `set -e` a failing
# pg_dump would abort the script here, skipping every check below and
# leaving the partial file behind.
if ! dump_to "$TMP"; then
  fail "pg_dump did not complete (database stopped, dropped, or unreachable?) — no backup was written"
fi
gzip -t "$TMP" 2>/dev/null || fail "the dump is not a valid gzip stream (cut off mid-write?)"
SZ=$(stat -c%s "$TMP" 2>/dev/null || stat -f%z "$TMP")
[ "$SZ" -gt 1024 ] || fail "empty dump ($SZ bytes)"
# gzip integrity only proves the container is whole — an empty dump
# compresses to a perfectly valid ~20-byte file. Require the decompressed
# stream to actually carry schema or data, the same test cmd_restore
# applies before it drops anything.
HEAD=$(gunzip -c "$TMP" 2>/dev/null | head -c 262144 || true)
case $HEAD in
  *"CREATE TABLE"*|*"COPY "*|*"INSERT INTO"*) : ;;
  *) fail "the dump contains no schema or data (no CREATE TABLE / COPY / INSERT)";;
esac
# Belt and braces on the mode: umask already made this 0600, but an
# operator running under a permissive default (or a future writer using a
# tool that ignores umask) must not silently publish the dump. Older dumps
# in DEST predate this and may still be 0644 — tighten the ones we own
# (our own oikonome-*.sql.gz naming, the same set retention deletes), and
# never let a chmod failure on a file owned by someone else fail a backup
# that otherwise succeeded.
# Warn rather than abort: a destination on a filesystem with no unix
# permission bits (a vfat/exFAT drive, some network mounts — and this
# script's usage note recommends an external location) can refuse chmod,
# and a successful dump must not be thrown away over that. It does mean
# the operator has to hear about it.
chmod 600 "$TMP" 2>/dev/null || \
  echo "warning: could not restrict permissions on the dump — anyone with" \
       "access to $DEST can read every user's data" >&2
# Verified: publish it. Same directory, so the rename is atomic — the
# picker never sees a name it cannot restore.
mv -f "$TMP" "$OUT" || fail "could not move the finished dump into place"
trap - EXIT
find "$DEST" -name 'oikonome-*.sql.gz' -type f ! -perm 600 \
  -exec chmod 600 {} + 2>/dev/null || true
find "$DEST" -name 'oikonome-*.sql.gz' -mtime +"$KEEP" -delete
# Leftovers from a run that was killed outright (SIGKILL, host reboot)
# never get their trap; they are invisible to the picker but not free.
find "$DEST" -name '.oikonome-*.sql.gz.partial' -mmin +720 -delete 2>/dev/null || true
echo "ok: $OUT"
