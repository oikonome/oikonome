#!/usr/bin/env bash
# Drop the per-worktree test databases nothing is using any more.
#
# `make worktree` gives each parallel checkout its own database
# (OIKONOME_TEST_DB=oikonome_test_<name>) because the control-plane tables
# have no RLS. Removing the worktree does not remove the database, so they
# accumulate.
#
# Explicit, never automatic: a database with no connections right now may
# still belong to a worktree someone comes back to tomorrow. This prints
# every drop, skips the one this shell would use, and skips any database
# with a live connection.
set -uo pipefail

KEEP="${OIKONOME_TEST_DB:-oikonome_test}"
ADMIN="${OIKONOME_TEST_ADMIN_DSN:-postgresql://postgres:devpass@127.0.0.1:5433}"
PY=server/.venv/bin/python
[ -x "$PY" ] || PY=python3

"$PY" - "$ADMIN" "$KEEP" "${1:-}" <<'PY'
import sys
import psycopg
admin, keep, arg = sys.argv[1], sys.argv[2], sys.argv[3]
dry = arg != "--yes"
with psycopg.connect(f"{admin}/postgres", autocommit=True) as c:
    rows = c.execute(
        """SELECT d.datname, pg_database_size(d.datname) AS bytes,
                  (SELECT count(*) FROM pg_stat_activity a
                    WHERE a.datname = d.datname) AS conns
             FROM pg_database d
            WHERE d.datname LIKE %s AND d.datname <> %s
            ORDER BY bytes DESC""", ("oikonome\\_test%", keep)).fetchall()
    total = 0
    for name, size, conns in rows:
        if conns:
            print(f"  skip {name} ({conns} live connection(s))")
            continue
        total += size
        if dry:
            print(f"  would drop {name} ({size / 1e6:.0f} MB)")
        else:
            c.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
            print(f"  dropped {name} ({size / 1e6:.0f} MB)")
    print(f"{'would free' if dry else 'freed'} {total / 1e9:.2f} GB"
          f"  (keeping {keep})")
    if dry:
        print("re-run with --yes to actually drop them")
PY
