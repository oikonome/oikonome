#!/usr/bin/env bash
# Refuse to start a suite that would collide with one already running.
#
# WHY THIS IS MECHANICAL AND NOT A NOTE IN A README: the control-plane
# tables (users, sessions, password_resets, …) have NO row-level security
# and are global to the test database, so two suites sharing one database
# produce phantom failures in test_auth_hardening / test_passkeys that look
# exactly like a broken diff. Hours get spent bisecting a change that was
# always fine.
#
# OIKONOME_TEST_DB (server/tests/util.py) solves this — every parallel
# worker exports its own database name. But an override nobody knows about
# leaves every run on the default, colliding anyway. An advisory convention
# nobody can see is not a control; this is the check that actually fires.
#
# Exit 1 blocks the run. Set OIKONOME_TEST_ALLOW_CONCURRENT=1 to override
# when you know the other run targets a different database.
set -uo pipefail

DB="${OIKONOME_TEST_DB:-oikonome_test}"
SELF=$$

# Other unittest processes on this box.
#
# Matching the command-line STRING is not enough: any shell whose cmdline
# merely mentions "unittest discover" matches too — a wait-loop watching
# for suites, or the very command that invokes this guard. That self-match
# deadlocks wait loops against each other (each sees the others and waits
# forever, while no suite is running at all), and makes this guard refuse a
# run with nothing in flight.
#
# So: find candidates by cmdline, then keep only those whose EXECUTABLE is
# actually python (/proc/<pid>/comm). A bash wrapper never survives that.
others=""
for pid in $(pgrep -f "unittest discover" 2>/dev/null || true); do
  [ "$pid" = "$SELF" ] && continue
  comm=$(cat "/proc/$pid/comm" 2>/dev/null || true)
  case "$comm" in python*) others="$others $pid" ;; esac
done
others=$(echo $others)
[ -z "$others" ] && exit 0

if [ "${OIKONOME_TEST_ALLOW_CONCURRENT:-}" = "1" ]; then
  echo "test-db-guard: another suite is running — proceeding anyway" >&2
  echo "               (OIKONOME_TEST_ALLOW_CONCURRENT=1, db=$DB)" >&2
  exit 0
fi

# Can we tell which database the other run is using? Only if it was started
# with the env var visible in /proc. Absent that, assume the default.
collide=0
for pid in $others; do
  env_db=$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null \
           | sed -n 's/^OIKONOME_TEST_DB=//p' | head -1)
  [ -z "$env_db" ] && env_db="oikonome_test"
  [ "$env_db" = "$DB" ] && collide=1
done

[ "$collide" -eq 0 ] && exit 0

cat >&2 <<EOF
✗ test-db-guard: another suite is already running against '$DB'.

  Control-plane tables have no RLS, so both runs would see each other's
  users/sessions and fail in ways that look like YOUR diff. Refusing.

  Working in parallel? Give this worktree its own database:

      export OIKONOME_TEST_DB=oikonome_test_\$(basename "\$PWD")
      make test

  Or wait for the other run to finish. Override with
  OIKONOME_TEST_ALLOW_CONCURRENT=1 if you know it targets a different db.
EOF
exit 1
