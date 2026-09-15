"""Reset a tenant's DATA while keeping logins.

Wipes every RLS domain table — transactions, accounts, bills, receipts,
settings, the lot — for one tenant (or every tenant). Control-plane tables
(users, sessions, passwords, invites, script tokens) are untouched, so
everyone keeps signing in with the same credentials; tenant_settings goes
too, so config re-seeds and the guided wizard runs again on the next visit.

The table list comes live from pg_class (every table with row security
enabled), so new domain tables are covered automatically.

Wiping EVERY tenant has to be asked for. On a single-household self-host
"all tenants" and "my data" are the same sentence, but the same command on
a multi-tenant instance is every customer's ledger, and nothing in the
old bare invocation distinguished the two. So `--all` is required whenever
there is more than one tenant, and `--summary` prints exactly what would
be destroyed (per tenant: owner, tables, rows) so the caller's confirmation
can name it.

CLI (used by `./oikonome.sh reset-data`):
    python -m oikonome.db.reset --summary        # count, destroy nothing
    python -m oikonome.db.reset <tenant>         # one tenant
    python -m oikonome.db.reset --all            # every tenant
"""

from __future__ import annotations

import sys

from . import tenancy


def domain_tables(conn) -> list[str]:
    """Every public table with row security enabled = the domain tables."""
    return [r["relname"] for r in conn.execute(
        """SELECT c.relname FROM pg_class c
           JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'public' AND c.relrowsecurity
           ORDER BY c.relname""").fetchall()]


def reset_tenant_data(tenant_id) -> dict[str, int]:
    """Delete one tenant's rows from every domain table (the tenant-scoped
    connection's RLS guarantees nothing else can be touched). Returns
    {table: rows_deleted} for what was actually wiped."""
    conn = tenancy.tenant_connect(tenant_id)
    try:
        counts: dict[str, int] = {}
        for t in domain_tables(conn):
            n = conn.execute(f'DELETE FROM "{t}"').rowcount
            if n:
                counts[t] = n
        return counts
    finally:
        conn.close()


def all_tenants() -> list[tuple[str, str]]:
    """Every tenant as (id, who) — `who` names it the way an operator would
    recognise it (the owner's email), so a confirmation can say whose data
    is about to go rather than printing a bare UUID."""
    admin = tenancy.admin_connect()
    try:
        rows = admin.execute(
            """SELECT t.id::text AS id,
                      coalesce(min(u.email) FILTER (WHERE u.role = 'owner'),
                               min(u.email), '(no accounts)') AS who
                 FROM tenants t LEFT JOIN users u ON u.tenant_id = t.id
                GROUP BY t.id ORDER BY 2""").fetchall()
        return [(r["id"], r["who"]) for r in rows]
    finally:
        admin.close()


def count_tenant_data(tenant_id) -> dict[str, int]:
    """{table: rows} for one tenant — what reset_tenant_data WOULD delete.
    Same RLS-scoped connection, so it counts exactly the rows that DELETE
    would reach."""
    conn = tenancy.tenant_connect(tenant_id)
    try:
        counts: dict[str, int] = {}
        for t in domain_tables(conn):
            n = conn.execute(f'SELECT count(*) AS n FROM "{t}"').fetchone()["n"]
            if n:
                counts[t] = n
        return counts
    finally:
        conn.close()


# How many households the inventory names one by one. The point of the
# summary is to make a person recognise what they are about to destroy,
# and a wall of hundreds of lines does the opposite; past this the rest
# are counted, not listed.
SUMMARY_DETAIL = 10


def _rows_for(tenant_ids: list[str]) -> dict[str, int]:
    """{tenant_id: row count} for the FEW tenants the summary names.

    Counting every tenant is what makes this prompt dangerous to print:
    per-tenant it is a fresh connection and a query per domain table, and
    one grouped union over every table instead just moves the cost into
    Postgres (it exhausts shared memory on a real tenant count). The
    confirmation does not need a number for households it does not list,
    so it asks only about the ones it shows."""
    out: dict[str, int] = {}
    for tid in tenant_ids:
        out[tid] = sum(count_tenant_data(tid).values())
    return out


def summary(tenants: list[tuple[str, str]]) -> str:
    """Human-readable inventory of what a wipe would destroy."""
    shown = tenants[:SUMMARY_DETAIL]
    counts = _rows_for([tid for tid, _ in shown])
    lines = [f"  {who}  ({tid})  {counts.get(tid, 0)} rows"
             for tid, who in shown]
    if not tenants:
        return "no tenants — nothing to wipe"
    if len(tenants) > SUMMARY_DETAIL:
        # the total is deliberately absent: counting the rest is the cost
        # this bound exists to avoid, and a partial sum presented as a
        # total would understate what the wipe destroys
        lines.append(f"  … and {len(tenants) - SUMMARY_DETAIL} more")
        head = f"{len(tenants)} tenant(s) — the first {SUMMARY_DETAIL} shown"
    else:
        head = (f"{len(tenants)} tenant(s), {sum(counts.values())} rows "
                f"of financial data")
    return "\n".join([head, *lines])


def main(argv: list[str]) -> int:
    args = argv[1:]
    want_all = "--all" in args
    want_summary = "--summary" in args
    rest = [a for a in args if not a.startswith("--")]

    if rest:
        tenants = [(rest[0], "")]
    else:
        tenants = all_tenants()
        # More than one tenant means this is not a single household, and
        # "wipe everything" must be typed, not defaulted into.
        if len(tenants) > 1 and not want_all and not want_summary:
            print(summary(tenants))
            print("refusing to wipe every tenant without --all; "
                  "pass one tenant id to wipe just that one", file=sys.stderr)
            return 2

    if want_summary:
        print(summary(tenants))
        return 0

    total = 0
    for tid, _who in tenants:
        counts = reset_tenant_data(tid)
        n = sum(counts.values())
        total += n
        print(f"tenant {tid}: {n} rows wiped across "
              f"{len(counts)} tables")
    print(f"done — {total} rows total; logins kept, wizard will run again")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
