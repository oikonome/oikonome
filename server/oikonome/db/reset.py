"""Reset a tenant's DATA while keeping logins.

Wipes every RLS domain table — transactions, accounts, bills, receipts,
settings, the lot — for one tenant (or every tenant). Control-plane tables
(users, sessions, passwords, invites, script tokens) are untouched, so
everyone keeps signing in with the same credentials; tenant_settings goes
too, so config re-seeds and the guided wizard runs again on the next visit.

The table list comes live from pg_class (every table with row security
enabled), so new domain tables are covered automatically.

The items table holds the only copy of each linked bank's access token, so
a reset releases those links at the provider BEFORE the rows go, the way
the erasure doors do: otherwise every Item stays live and billed with no
handle left to remove it, and the wizard that runs next links the same
banks a second time. The release is best effort (a provider outage must
not make a reset impossible); what did not release is recorded with its
item ids for the provider's dashboard and printed. The add-on's erasure
release is deliberately NOT called: a data reset keeps the logins, and
whatever the household pays for goes on.

Wiping EVERY tenant has to be asked for. On a single-household self-host
"all tenants" and "my data" are the same sentence, but the same command on
a multi-tenant instance is every customer's ledger. So `--all` is required whenever
there is more than one tenant, and `--summary` prints exactly what would
be destroyed (per tenant: owner, tables, rows) so the caller's confirmation
can name it.

CLI (used by `./oikonome.sh reset-data`):
    python -m oikonome.db.reset --summary        # count, destroy nothing
    python -m oikonome.db.reset <tenant>         # one tenant
    python -m oikonome.db.reset --all            # every tenant
    python -m oikonome.db.reset --release-links  # release only (full reset)
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


class ResetResult(dict):
    """{table: rows_deleted}, plus what the provider release reported
    (``released``, the same shape as erasure.release_external's result)."""
    released: dict

    def __init__(self, counts: dict, released: dict):
        super().__init__(counts)
        self.released = released


def release_bank_links(tenant_id) -> dict:
    """Release every Plaid Item and the platform-side MX user of a tenant.
    Never raises; a shortfall is reported as ``plaid_failed`` (item ids, or
    "all" when the release never got going) / ``mx_user='error'``, which is
    what erasure.record_pending_release keys its retry marker on."""
    from ..sync import mx, plaid
    results: dict = {}
    try:
        n = plaid.release_tenant_items(tenant_id, reason="reset")
        results["plaid_items"] = int(n)
        failed = list(getattr(n, "failed_ids", ()) or ())
        if failed or int(n) < int(getattr(n, "expected", 0) or 0):
            results["plaid_failed"] = failed or "all"
    except Exception as e:                                 # noqa: BLE001
        print(f"warning: bank-link release failed for {tenant_id}: {e}",
              file=sys.stderr)
        results["plaid_items"] = 0
        results["plaid_failed"] = "all"
    try:
        results["mx_user"] = mx.release_tenant_user(tenant_id)
    except Exception as e:                                 # noqa: BLE001
        print(f"warning: MX release failed for {tenant_id}: {e}",
              file=sys.stderr)
        results["mx_user"] = "error"
    return results


def hand_back_open_links(tenant_id) -> dict:
    """Finish the tenant's bank-link sessions still open, handing back every
    Item the person made at Plaid, before the release (as the erasure does):
    a link finished at the bank moments ago has no items row yet, so the
    release cannot see it, and once the session goes nothing can exchange
    or remove it. Never raises; {link_token: word}, and a session that was
    not handed back is warned about by link token."""
    from ..jobs import plaid_links
    try:
        out = plaid_links.hand_back_open_sessions(tenant_id)
    except Exception as e:                                 # noqa: BLE001
        print(f"warning: open bank links of {tenant_id} not handed back: "
              f"{e}", file=sys.stderr)
        return {}
    for lt, word in out.items():
        if word in ("busy", "error"):
            print(f"warning: an open bank link of {tenant_id} was not "
                  f"handed back ({word}): {lt}", file=sys.stderr)
    return out


def _release_and_record(admin, tenant_id) -> dict:
    """Hand back open link sessions, release the tenant's bank links and,
    when any did not release, leave the retry marker. The item ids are
    read while the rows still exist: they are the only handle an operator
    has to finish a failed release."""
    from .. import erasure
    hand_back_open_links(tenant_id)
    released = release_bank_links(tenant_id)
    retry_ids = erasure.collect_retry_ids(tenant_id)
    erasure.record_pending_release(admin, tenant_id, released, retry_ids)
    return released


def release_links_only(tenant_id) -> dict:
    """Release one tenant's bank links without deleting anything — for the
    full instance reset, which deletes the whole database folder once the
    stack is down and so has to release while the app can still run."""
    from .. import erasure
    admin = tenancy.admin_connect()
    try:
        erasure.hold_writers(admin, tenant_id)
        try:
            return _release_and_record(admin, tenant_id)
        finally:
            erasure.release_writers(admin, tenant_id)
    finally:
        admin.close()


# What a bank link left open across a data reset answers when it finishes.
RESET_REFUSAL = ("this household's data was reset while the bank link was "
                 "open — connect the bank again")


def reset_tenant_data(tenant_id) -> ResetResult:
    """Delete one tenant's rows from every domain table (the tenant-scoped
    connection's RLS guarantees nothing else can be touched). Returns
    {table: rows_deleted} for what was actually wiped, with the provider
    release result on ``.released``.

    Holds every background writer's per-tenant lock across the release and
    the wipe (erasure.WRITER_LOCKS): a sync, categorize pass or nightly
    sweep already running finishes first, and none can start and write
    settings, bills or alerts into a half-wiped tenant. The deletes run in
    one transaction, so a failure part-way leaves the data whole."""
    from .. import erasure
    admin = tenancy.admin_connect()
    try:
        erasure.hold_writers(admin, tenant_id)
        try:
            released = _release_and_record(admin, tenant_id)
            conn = tenancy.tenant_connect(tenant_id)
            try:
                counts: dict[str, int] = {}
                with conn.transaction():
                    for t in domain_tables(conn):
                        n = conn.execute(f'DELETE FROM "{t}"').rowcount
                        if n:
                            counts[t] = n
            finally:
                conn.close()
            # the link sessions are control-plane rows the RLS wipe does not
            # reach. A finished or update-mode one is spent and goes. An
            # add session still open stays on record, marked: the person
            # may yet finish at the bank, and that row is the only handle
            # on the Item it makes — so the finisher (the LINK webhook, the
            # sweep, a poll) exchanges the token and hands the Item back
            # rather than put a bank into the household just wiped, or
            # leave it billing with nothing here able to remove it
            from ..jobs import plaid_links
            plaid_links.refuse_open_adds(admin, tenant_id, RESET_REFUSAL)
            admin.execute(
                "DELETE FROM plaid_link_sessions WHERE tenant=%s "
                "AND NOT (kind='add' AND done_at IS NULL)",
                (str(tenant_id),))
        finally:
            # session-level, so it must not be left to the connection's close
            erasure.release_writers(admin, tenant_id)
    finally:
        admin.close()
    return ResetResult(counts, released)


def unreleased(released: dict, retry_ids: dict | None = None,
               queue_survives: bool = True) -> list[str]:
    """Human lines for whatever the provider release could not finish.

    `queue_survives` says whether the nightly retry will ever run: true
    for one household's reset, where plaid_release_queue outlives the
    household's rows; false for the full instance reset
    (``--release-links``), which deletes the database holding that queue
    — and the master key its tokens are sealed under — right after."""
    out = []
    failed = released.get("plaid_failed")
    if isinstance(failed, list) and failed and not queue_survives:
        out.append("bank links still live at Plaid — this reset deletes the "
                   "database and its retry queue, so nothing here will "
                   "release them later: remove these items in the Plaid "
                   "dashboard now: " + ", ".join(map(str, failed)))
    elif isinstance(failed, list) and failed:
        out.append("bank links still live at Plaid (the nightly run retries "
                   "the release for 30 days; after that, remove them in the "
                   "Plaid dashboard): " + ", ".join(map(str, failed)))
    elif failed:
        out.append("bank links could not be released at Plaid; their item "
                   "ids are recorded in the admin audit log "
                   "(external_release_pending)")
    if released.get("mx_user") == "error":
        out.append("the MX user could not be deleted at MX")
    return out


def release_queue_leftovers() -> list[str]:
    """Retry plaid_release_queue once and return a line per Item still in
    it — the full instance reset deletes the queue and the key its tokens
    are sealed under, so whatever is named here keeps billing until it is
    removed in the Plaid dashboard."""
    from ..jobs import reaper
    try:
        reaper.retry_queued_releases()
    except Exception as e:                                 # noqa: BLE001
        print(f"warning: the queued bank-link releases were not retried: "
              f"{e}", file=sys.stderr)
    try:
        admin = tenancy.admin_connect()
        try:
            rows = admin.execute(
                "SELECT item_id, reason FROM plaid_release_queue "
                "ORDER BY queued_at").fetchall()
        finally:
            admin.close()
    except Exception as e:                                 # noqa: BLE001
        return [f"the queue of failed bank-link releases could not be read "
                f"({type(e).__name__}); check plaid_release_queue before "
                f"the database goes"]
    return [f"bank link still live at Plaid ({r['reason']}) — remove item "
            f"{r['item_id']} in the Plaid dashboard" for r in rows]


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


def _rows_for(tenant_ids: list[str]) -> dict[str, dict[str, int]]:
    """{tenant_id: {table: rows}} for the FEW tenants the summary names.

    Counting every tenant is what makes this prompt dangerous to print:
    per-tenant it is a fresh connection and a query per domain table, and
    one grouped union over every table instead just moves the cost into
    Postgres (expensive at scale). The
    confirmation does not need a number for households it does not list,
    so it asks only about the ones it shows."""
    out: dict[str, int] = {}
    for tid in tenant_ids:
        out[tid] = count_tenant_data(tid)
    return out


def summary(tenants: list[tuple[str, str]]) -> str:
    """Human-readable inventory of what a wipe would destroy."""
    shown = tenants[:SUMMARY_DETAIL]
    per = _rows_for([tid for tid, _ in shown])
    counts = {tid: sum(c.values()) for tid, c in per.items()}
    lines = []
    for tid, who in shown:
        line = f"  {who}  ({tid})  {counts.get(tid, 0)} rows"
        # each linked bank is released at the provider by the wipe, and has
        # to be linked again afterwards; say so before the confirmation
        links = per.get(tid, {}).get("items", 0)
        if links:
            line += f", {links} bank link(s) to disconnect"
        lines.append(line)
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
    if "--release-links" in args:
        # every tenant, nothing deleted; exit 1 when anything stayed live so
        # the caller can say so before the database goes
        stuck = 0
        for tid, who in all_tenants():
            lines = unreleased(release_links_only(tid),
                               queue_survives=False)
            stuck += len(lines)
            for line in lines:
                print(f"  NOT RELEASED ({who}, {tid}): {line}")
        # The queue of releases that failed during earlier erasures, and the
        # holds on links that were mid-exchange, belong to no tenant left
        # and die with the database: one last try, then name what remains
        for line in release_queue_leftovers():
            stuck += 1
            print(f"  NOT RELEASED: {line}")
        return 1 if stuck else 0
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
        for line in unreleased(counts.released):
            print(f"  NOT RELEASED: {line}")
    print(f"done — {total} rows total; logins kept, wizard will run again")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
