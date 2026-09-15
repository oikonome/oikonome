#!/usr/bin/env bash
# Start every suite on an EMPTY test database.
#
# WHY: tests/util.py creates a fresh TENANT per test and never deletes it —
# RLS is the isolation, so there is nothing to clean up for correctness. But
# the database is shared across RUNS, so the rows pile up forever. Left
# alone, the default `oikonome_test` grows to millions of rows and gigabytes,
# and the control-plane tables (users, sessions — no RLS, queried without a
# tenant filter) seq-scan all of it: the suite slows until it looks hung,
# with nothing wrong in the code under test.
#
# Recreating costs 1.5s for all migrations, which is cheaper than one slow
# test, and it makes a run reproducible: the schema a fresh clone gets.
#
# The drop happens at the START of a run, never at the end, so the database
# of a FAILED run is still there to inspect afterwards. OIKONOME_TEST_KEEP_DB=1
# skips the drop for the same reason.
set -uo pipefail

[ "${OIKONOME_TEST_KEEP_DB:-}" = "1" ] && exit 0

DB="${OIKONOME_TEST_DB:-oikonome_test}"
ADMIN="${OIKONOME_TEST_ADMIN_DSN:-postgresql://postgres:devpass@127.0.0.1:5433}"
PY=server/.venv/bin/python
[ -x "$PY" ] || PY=python3

# psycopg, not psql: the venv already has it and the host may not have a
# postgres client at all. A database that cannot be reached is not this
# script's problem — the suite reports that far better than a guard can.
"$PY" - "$ADMIN" "$DB" <<'PY' || exit 0
import sys
try:
    import psycopg
except Exception:
    sys.exit(0)
admin, db = sys.argv[1], sys.argv[2]
if not db.startswith("oikonome_test"):
    print(f"test-db-fresh: refusing to drop '{db}' — not a test database",
          file=sys.stderr)
    sys.exit(1)
try:
    with psycopg.connect(f"{admin}/postgres", autocommit=True) as c:
        row = c.execute("SELECT pg_database_size(datname) FROM pg_database "
                        "WHERE datname=%s", (db,)).fetchone()
        if row is None:
            sys.exit(0)                      # nothing to drop; util.py creates it
        c.execute(f'DROP DATABASE "{db}" WITH (FORCE)')
        print(f"test-db-fresh: dropped {db} ({row[0] / 1e6:.0f} MB) — "
              f"the suite recreates it")
        # The databases of OTHER worktrees are not ours to drop (one may
        # belong to a checkout someone comes back to), so this only says
        # they are there. A nudge on a run everyone does beats a background
        # job that decides on its own — and beats a note nobody reads.
        stale = c.execute(
            "SELECT count(*), coalesce(sum(pg_database_size(datname)), 0) "
            "  FROM pg_database WHERE datname LIKE %s AND datname <> %s",
            (r"oikonome\_test%", db)).fetchone()
        if stale and stale[1] > 1e9:
            print(f"test-db-fresh: {stale[0]} other test databases are using "
                  f"{stale[1] / 1e9:.1f} GB — `make test-db-prune` lists them")
except Exception as e:                       # noqa: BLE001
    print(f"test-db-fresh: skipped ({e.__class__.__name__})", file=sys.stderr)
PY
exit 0
