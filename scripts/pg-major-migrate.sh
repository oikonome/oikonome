#!/usr/bin/env bash
# Migrate a bundled-Postgres data folder across a MAJOR version in place.
#
#   scripts/pg-major-migrate.sh <install-dir>
#
# A data folder written by an older major can't be opened by the newer
# server image (postgres crash-loops on "database files are incompatible
# with server"). When the folder's PG_VERSION differs from the major the
# compose file pins, this dumps everything (roles included — the app/admin
# roles keep their passwords; the app's own nightly dump has no roles) with
# the OLD image, moves the folder aside as a rollback point, initialises the
# new major through the stack's own postgres service, and reloads.
#
# Exit 0 when nothing needed doing or the migration succeeded; non-zero
# with the old folder untouched (or kept aside) otherwise. Called by
# install.sh on every install/upgrade and by deploy scripts
# that bring a stack up with a plain `compose up`. External/managed
# databases (COMPOSE_FILE=…external-db…) are never touched.
set -euo pipefail
# The dump this writes is pg_dumpall output: every database in plaintext SQL
# — the whole ledger, every email and phone number — plus the `ALTER ROLE …
# PASSWORD` lines carrying the postgres/oikonome_app/oikonome_admin
# credentials. It is kept on disk whenever a step after it fails, so its
# lifetime is not bounded by this run. Same treatment scripts/backup.sh
# gives the identical artifact: umask 077 means the redirect creates it 0600
# from its first byte, with no world-readable window to race. It also makes
# the fresh data folder below 0700, which is what a PGDATA has to be anyway
# (postgres refuses to start on a group/world-accessible one).
umask 077
ROOT=${1:?install dir}
# where this script's helpers are, resolved before the cd below
SCRIPTS=$(cd "$(dirname "$0")" && pwd)
cd "$ROOT/docker"
say()  { printf "  %s\n" "$*"; }
die()  { printf "  ERROR: %s\n" "$*" >&2; exit 1; }
# dotenv_get / data_dir_resolve / pg_data_version: .env read the way
# compose reads it, so this looks at the folder postgres actually mounts
. "$SCRIPTS/env-file.sh"

case $(dotenv_get .env COMPOSE_FILE) in *external-db*) exit 0 ;; esac

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  ENGINE=docker; COMPOSE="docker compose"
elif command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then
  ENGINE=podman; COMPOSE="podman compose"
else
  ENGINE=podman; COMPOSE="podman-compose"
fi
# rootless podman maps host uid to the container's postgres uid via
# compose.override.yaml; on rootful docker the folder belongs to the
# container's uid. A passwordless sudo is used when there is one; without
# it the moves below still work (they rename entries of the parent folder,
# which the operator owns), and the version is read through a container.
SUDO=""
[ "$ENGINE" = docker ] && [ "$(id -u)" != 0 ] && command -v sudo >/dev/null \
  && sudo -n true 2>/dev/null && SUDO="sudo -n"

ABS_DATA=$(data_dir_resolve "$PWD" ../data/postgres)

WANT=$(grep -oE 'image: docker.io/library/postgres:[0-9]+' compose.yaml | head -1 | grep -oE '[0-9]+$')
[ -n "$WANT" ] || exit 0
# "No cluster here" (a fresh install) and "a cluster this user cannot read"
# are different answers: the second must stop the upgrade, because starting
# the new major on an old folder leaves postgres crash-looping.
rc=0
HAVE=$(pg_data_version "$ABS_DATA" "$ENGINE" "docker.io/library/postgres:${WANT}-alpine") || rc=$?
if [ "$rc" -ne 0 ]; then
  die "can't read which Postgres version wrote $ABS_DATA (its files belong to the database container and a probe container could not read them either) — nothing was changed. Check that $ENGINE can run containers, or re-run the upgrade as root."
fi
[ -n "$HAVE" ] || exit 0
[ "$HAVE" != "$WANT" ] || exit 0

# The maintenance lock every whole-database operation shares (backup.sh,
# restore, reset, uninstall). Without it a scheduled backup can connect
# during the reload: the reload's DROP DATABASE then fails on the open
# connection and stops the upgrade, or the backup dumps a half-loaded
# database that passes every check and becomes the newest "good" backup.
# Taken only when there is a migration to run, so an ordinary upgrade
# never waits on a backup. A caller that already holds it (oikonome.sh's
# maint_lock) says so in OIKONOME_MAINT_LOCK_HELD — re-opening the file
# here would wait on that caller's own lock.
ROOT_ABS=$(cd "$ROOT" && pwd)
if [ "${OIKONOME_MAINT_LOCK_HELD:-}" != "$ROOT_ABS" ] && command -v flock >/dev/null 2>&1; then
  exec 9>"$ROOT_ABS/.maintenance.lock"
  flock -w "${OIKONOME_MAINT_LOCK_WAIT:-600}" 9 \
    || die "a backup/restore/reset is still running on this instance — wait for it, then re-run the upgrade (nothing was changed)"
  # say who holds it, so a backup waiting behind a long migration names
  # the migration rather than whoever held the lock last
  . "$SCRIPTS/maint-lock.sh"
  maint_holder_write "$ROOT_ABS" pg-major-migrate
  trap 'maint_holder_clear "$ROOT_ABS"' EXIT
fi

say "Database folder is Postgres $HAVE; this release runs Postgres $WANT."
say "Migrating in place (dump with $HAVE → reload into $WANT)…"
STAMP=$(date +%Y%m%d-%H%M%S)
DUMP="$(dirname "$ABS_DATA")/pg${HAVE}-to-${WANT}-${STAMP}.sql"
$COMPOSE stop >/dev/null 2>&1 || true
$COMPOSE rm -f -s postgres >/dev/null 2>&1 || true
MIG=oikonome-pgmig-$$
USERNS=""
if [ "$ENGINE" = podman ] && [ "$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null)" = "true" ]; then
  USERNS="--userns=keep-id:uid=999,gid=999"
fi
# No password and no network: the entrypoint asks for POSTGRES_PASSWORD only
# when it initialises an EMPTY folder, and every statement below runs through
# `exec` over the container's local socket. A password on this command line
# would sit in the client's argv, readable by every account on the host, for
# as long as the old-major image takes to pull.
$ENGINE run -d --rm --name "$MIG" $USERNS --network none \
  -v "$ABS_DATA:/var/lib/postgresql/data:Z" -e PGDATA=/var/lib/postgresql/data \
  "docker.io/library/postgres:${HAVE}-alpine" >/dev/null \
  || die "couldn't start postgres:$HAVE to read the old database"
for i in $(seq 1 60); do $ENGINE exec "$MIG" pg_isready -U postgres -q 2>/dev/null && break; sleep 1; done
# Exact per-table row counts of the oikonome database (one "table<TAB>rows"
# line per public table, sorted). Taken from the OLD cluster before the dump
# and from the NEW one after the reload; a table that shrank or vanished
# means rows were lost in transit, whatever psql's exit code said.
# (query_to_xml is the one built-in that runs a generated count(*) per
# table without plpgsql; counts are exact, unlike pg_stat's n_live_tup.)
TALLY_SQL="select table_name || E'\t' || (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', table_schema, table_name), false, true, '')))[1]::text from information_schema.tables where table_schema='public' and table_type='BASE TABLE' order by 1"
tally() { "$@" psql -U postgres -d oikonome -tAc "$TALLY_SQL" 2>/dev/null | tr -d '\r'; }
OLD_TALLY=$(tally $ENGINE exec "$MIG" || true)
# An empty tally makes the row-loss check below vacuous (nothing to compare
# against, so any loss "verifies") — refuse to proceed rather than migrate
# without the safety net. Nothing has been changed yet at this point.
if [ -z "$OLD_TALLY" ]; then
  $ENGINE stop "$MIG" >/dev/null 2>&1 || true
  die "couldn't count the old database's rows — refusing to migrate without a row-loss check (nothing was changed)"
fi
if ! $ENGINE exec "$MIG" pg_dumpall -U postgres --clean --if-exists > "$DUMP" \
   || [ "$(wc -c < "$DUMP")" -lt 1024 ]; then
  $ENGINE stop "$MIG" >/dev/null 2>&1 || true
  die "dump of the Postgres $HAVE database failed — nothing was changed (see $DUMP)"
fi
# Belt and braces on the mode, in case a future writer reaches for a tool
# that ignores umask — the dump outlives every failure path below, so it
# must never be readable by other accounts on the host.
chmod 600 "$DUMP" 2>/dev/null || true
$ENGINE stop "$MIG" >/dev/null 2>&1 || true
# The fresh cluster already has role postgres + db oikonome (POSTGRES_DB).
# --clean/--if-exists covers the database, but pg_dumpall's `DROP ROLE IF
# EXISTS postgres` / `CREATE ROLE postgres` both fail on every fresh cluster
# ("current user cannot be dropped", "role already exists") — they are the
# only errors a clean reload produces, so drop those two statements from the
# dump and run the rest under ON_ERROR_STOP. Without it psql skips the
# failing statement and exits 0, so a half-loaded table reports success.
# (The following ALTER ROLE postgres … PASSWORD line is kept: it carries the
# old password onto the new cluster.)
sed -i -e '/^DROP ROLE IF EXISTS postgres;$/d' -e '/^CREATE ROLE postgres;$/d' "$DUMP"
# Built BEFORE the move, not after it: every abort from here on has to be
# able to tell the operator where their database went. "couldn't start
# postgres" is exactly the abort a self-hoster hits with a bad image or a
# full disk; without the rollback path in the message it reads as if the
# upgrade destroyed the database — one panicked `reset` or `rm -rf` away
# from actually destroying the folder that still holds it.
OLD_DIR="$ABS_DATA.pg${HAVE}-${STAMP}"
ROLLBACK="old data untouched at $OLD_DIR; put it back with: $COMPOSE stop; rm -rf $ABS_DATA; mv $OLD_DIR $ABS_DATA"
$SUDO mv "$ABS_DATA" "$OLD_DIR"
$SUDO mkdir -p "$ABS_DATA"
$COMPOSE up -d postgres >/dev/null 2>&1 \
  || die "couldn't start postgres:$WANT — YOUR DATABASE IS SAFE: $ROLLBACK"
for i in $(seq 1 90); do $COMPOSE exec -T postgres pg_isready -U postgres -q 2>/dev/null && break; sleep 1; done
if ! $COMPOSE exec -T postgres pg_isready -U postgres -q 2>/dev/null; then
  die "postgres:$WANT never became ready (90s) — YOUR DATABASE IS SAFE: $ROLLBACK"
fi
if ! $COMPOSE exec -T postgres psql -U postgres -q -v ON_ERROR_STOP=1 -f - < "$DUMP" >/dev/null 2>"$DUMP.log"; then
  chmod 600 "$DUMP.log" 2>/dev/null || true
  die "reload into Postgres $WANT stopped on an error — see $DUMP.log (dump kept at $DUMP); $ROLLBACK"
fi
NEW_TALLY=$(tally $COMPOSE exec -T postgres || true)
N=$(printf '%s\n' "$NEW_TALLY" | grep -c . || true)
[ "${N:-0}" -gt 0 ] || die "migrated database looks empty — $ROLLBACK"
# Every table the old cluster had must be present with at least as many rows.
LOST=$(python3 - "$OLD_TALLY" "$NEW_TALLY" <<'PY'
import sys
def parse(t):
    out = {}
    for line in t.splitlines():
        if "\t" in line:
            name, n = line.rsplit("\t", 1)
            out[name] = int(n or 0)
    return out
old, new = parse(sys.argv[1]), parse(sys.argv[2])
for name, n in sorted(old.items()):
    if new.get(name, -1) < n:
        print(f"{name}: {n} -> {new.get(name, 'missing')}")
PY
)
if [ -n "$LOST" ]; then
  printf '  %s\n' "$LOST" >&2
  die "reload lost rows (above) — dump kept at $DUMP; $ROLLBACK"
fi
# Verified: neither the plaintext dump nor psql's stderr from loading it
# has any reason to stay on disk (both are kept on every failure path).
rm -f "$DUMP" "$DUMP.log"
say "Postgres $HAVE → $WANT migrated ($N tables, row counts verified). Old folder kept at $OLD_DIR — delete it once you're happy."
