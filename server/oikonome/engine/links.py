"""Multi-source accounts: several sources feeding one
real-world account, linked into a group.

Model: account_links rows sharing a group_id ARE one account. home_rank
is the user's preference order (0 = favorite). Nothing is rewritten on
failure — the EFFECTIVE primary is computed at read time as the lowest
home_rank whose source is currently healthy, so:

  * shadow accounts (everyone except the effective primary) are
    invisible to spend math and net worth — no double counting;
  * they keep syncing, so the moment the primary's source dies the next
    rank's data is already complete and simply becomes visible;
  * recovery is equally automatic (the old primary wins again).

The hourly worker sweep only ALERTS on failover (edge-triggered via
failover_alerted on the group's rank-0 row) — it never moves data.

Health: an account's source is DOWN when its item.status is an error,
or when it's a live aggregator that hasn't successfully written in
STALE_HOURS. Manual/file accounts have no freshness signal and count
as healthy (they're ranked last by default anyway)."""

from __future__ import annotations

import datetime as dt

import psycopg
import uuid

STALE_HOURS = 48
# "plan_csv" is a scraping collector that stamps accounts.updated_at on
# every push, so it has the same freshness signal as an aggregator. Without
# it here a broken scraper stays the "healthy" primary forever and a
# balance linked beneath it never gets its turn.
LIVE_AGGREGATORS = ("plaid", "mx", "simplefin", "simplefin-org", "coinbase",
                    "plan_csv")

# default preference when the user doesn't reorder: richest data first
RICHNESS = {"plaid": 0, "mx": 1, "coinbase": 2, "simplefin": 3,
            "simplefin-org": 3}


def set_shadow_scope(conn) -> None:
    """(Re)compute the app.shadow_ids session var — the set every SQL money
    aggregate reads. Set once per tenant_connect; also called after a link
    mutation so a connection reused within one request stays consistent.

    Shares `tenancy.shadow_ids_sql()` with tenant_connect, and deliberately
    with `shadow_ids()` below: two definitions of "ignore these accounts"
    drift apart, and a drift produces a feature that works on half the
    app."""
    from ..db.tenancy import shadow_ids_sql
    conn.execute(
        f"SELECT set_config('app.shadow_ids', "
        f"COALESCE(({shadow_ids_sql()}), ''), false)")


def _rows(conn) -> list[dict]:
    return conn.execute(
        """SELECT l.group_id, l.account_id, l.home_rank, l.failover_alerted,
                  i.aggregator, COALESCE(i.status,'ok') AS status,
                  a.updated_at, a.user_removed_at
           FROM account_links l
           JOIN accounts a ON a.id = l.account_id
           LEFT JOIN items i ON i.id = a.item_id
           ORDER BY l.group_id, l.home_rank""").fetchall()


def _healthy(r: dict) -> bool:
    # a hidden member cannot serve: electing it primary would shadow every
    # OTHER member too, and the whole real account would vanish from the
    # money math instead of failing over to the live copy. Mirrored in
    # tenancy.shadow_ids_sql — keep the two in step.
    if r.get("user_removed_at") is not None:
        return False
    if str(r["status"]).startswith("error"):
        return False
    if r["aggregator"] in LIVE_AGGREGATORS:
        if r["updated_at"] is None:
            return False
        age = dt.datetime.now(dt.timezone.utc) - r["updated_at"]
        return age <= dt.timedelta(hours=STALE_HOURS)
    return True                       # manual/file: no freshness signal


def groups(conn) -> list[dict]:
    """[{group_id, members:[{account_id, home_rank, healthy, primary}]}] —
    primary = effective (health-aware) primary."""
    out: dict = {}
    for r in _rows(conn):
        out.setdefault(str(r["group_id"]), []).append(
            {"account_id": r["account_id"], "home_rank": r["home_rank"],
             "healthy": _healthy(r),
             "hidden": r.get("user_removed_at") is not None,
             "failover_alerted": r["failover_alerted"]})
    result = []
    for gid, members in out.items():
        # a healthy member serves; with nothing healthy the top-ranked
        # VISIBLE member keeps serving (stale numbers beat a group that
        # vanishes from every total the moment both sources break); with
        # every member hidden nobody serves. Mirrored by the ORDER BY in
        # tenancy.shadow_ids_sql — keep the two in step.
        primary = next((m for m in members if m["healthy"]), None)
        if primary is None:
            primary = next((m for m in members if not m["hidden"]), None)
        for m in members:
            m["primary"] = m is primary
        result.append({"group_id": gid, "members": members})
    return result


def shadow_ids(conn) -> list[str]:
    """Every account the money math must ignore.

    Two sources, one list, because every caller wants the same thing —
    "accounts that exist but must not be counted":

    * linked non-primaries — a dual-source account must not count twice;
    * accounts the user has HIDDEN (`user_removed_at`). Hiding is stronger
      than archiving: the connection stays live and the history is kept,
      but the account contributes to nothing — no balance in net worth, no
      transactions in spend, budgets, bills, savings or retirement.

    Returned by every aggregate through this one helper on purpose. Hidden
    accounts are not merely dropped from the Accounts list with their
    balance nulled: that would leave their TRANSACTIONS moving the budget and
    the cash-flow chart — which is not what "hide this account" means to
    anyone who clicks it.
    """
    out = []
    for g in groups(conn):
        out += [m["account_id"] for m in g["members"] if not m["primary"]]
    out += [r["id"] for r in conn.execute(
        "SELECT id FROM accounts WHERE user_removed_at IS NOT NULL").fetchall()]
    return sorted(set(out))


def suggestions(conn, dismissed: list[str] | None = None) -> list[dict]:
    """Likely same-account pairs across DIFFERENT items: same non-trivial
    mask + same type, neither already linked. Returned pairs are proposals
    only — the owner confirms (spec: never silently merge money data)."""
    dismissed_set = set(dismissed or [])
    rows = conn.execute(
        """SELECT a.id, COALESCE(a.display_name, a.name) AS name, a.mask,
                  a.type, a.item_id, i.institution_name, i.aggregator
           FROM accounts a LEFT JOIN items i ON i.id = a.item_id
           WHERE a.mask IS NOT NULL AND length(a.mask) >= 2
             AND a.id NOT IN (SELECT account_id FROM account_links)
           ORDER BY a.id""").fetchall()
    out = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if a["item_id"] == b["item_id"]:
                continue
            if a["mask"] != b["mask"] or a["type"] != b["type"]:
                continue
            key = "|".join(sorted((a["id"], b["id"])))
            if key in dismissed_set:
                continue
            out.append({"key": key,
                        "a": dict(a), "b": dict(b)})
    return out


def default_order(conn, account_ids: list[str]) -> list[str]:
    """Richest data first. What a source actually delivered outranks what
    its aggregator usually delivers: an account carrying transactions or
    holdings goes ahead of one that only ever reported a balance, and among
    equals the aggregator order
    decides (Plaid > MX > SimpleFIN > files)."""
    rows = {r["id"]: (r["aggregator"] or "csv", r["has_data"])
            for r in conn.execute(
        """SELECT a.id, i.aggregator,
                  (EXISTS (SELECT 1 FROM transactions t
                            WHERE t.account_id = a.id AND t.removed = 0)
                   OR EXISTS (SELECT 1 FROM holdings h
                               WHERE h.account_id = a.id)) AS has_data
           FROM accounts a
           LEFT JOIN items i ON i.id = a.item_id
           WHERE a.id = ANY(%s)""", (account_ids,)).fetchall()}
    return sorted(account_ids,
                  key=lambda x: (0 if rows.get(x, ("csv", False))[1] else 1,
                                 RICHNESS.get(rows.get(x, ("csv", False))[0], 9)))


def create(conn, account_ids: list[str]) -> str:
    """Link accounts as one; order = home_rank. ValueError before any
    write on bad input."""
    ids = [str(a) for a in account_ids]
    if len(ids) < 2 or len(set(ids)) != len(ids):
        raise ValueError("link needs two or more distinct accounts")
    n = conn.execute("SELECT COUNT(*) AS n FROM accounts WHERE id = ANY(%s)",
                     (ids,)).fetchone()["n"]
    if n != len(ids):
        raise ValueError("unknown account in link")
    try:
        with conn.transaction():
            if conn.execute(
                    "SELECT 1 FROM account_links WHERE account_id = ANY(%s) "
                    "LIMIT 1", (ids,)).fetchone():
                raise ValueError("an account is already part of a link")
            gid = str(uuid.uuid4())
            for rank, aid in enumerate(ids):
                conn.execute(
                    "INSERT INTO account_links (group_id, account_id, "
                    "home_rank) VALUES (%s,%s,%s)", (gid, aid, rank))
    except psycopg.errors.UniqueViolation:
        raise ValueError("an account is already part of a link") from None
    set_shadow_scope(conn)
    return gid


def unlink(conn, group_id: str) -> int:
    n = conn.execute(
        "DELETE FROM account_links WHERE group_id = %s::uuid",
        (group_id,)).rowcount
    set_shadow_scope(conn)
    return n


def reorder(conn, group_id: str, account_ids: list[str]) -> None:
    current = {r["account_id"] for r in conn.execute(
        "SELECT account_id FROM account_links WHERE group_id = %s::uuid",
        (group_id,)).fetchall()}
    if set(account_ids) != current:
        raise ValueError("order must name exactly the linked accounts")
    for rank, aid in enumerate(account_ids):
        conn.execute(
            "UPDATE account_links SET home_rank = %s, failover_alerted = NULL "
            "WHERE group_id = %s::uuid AND account_id = %s",
            (rank, group_id, aid))
    set_shadow_scope(conn)


def failover_pending(conn) -> list[dict]:
    """Groups in a REAL failover — the home favorite is DOWN and a healthy
    lower-ranked backup is serving — that haven't been alerted yet.
    Auto-clears the edge once the favorite is healthy again.

    Does NOT stamp the alert: the worker stamps only after a
    successful send via mark_failover_alerted, so a send failure retries.
    Requires the backup to be genuinely healthy: when EVERY
    member is down, groups() falls back to members[0]=the favorite, so
    'favorite is primary' — we must not false-clear the edge then, and we
    must not alert an all-down group as a failover)."""
    due = []
    for g in groups(conn):
        favorite = min(g["members"], key=lambda m: m["home_rank"])
        primary = next(m for m in g["members"] if m["primary"])
        real_failover = (not favorite["healthy"] and primary is not favorite
                         and primary["healthy"])
        if real_failover and favorite["failover_alerted"] is None:
            due.append({"group_id": g["group_id"],
                        "down": favorite["account_id"],
                        "using": primary["account_id"]})
        elif favorite["healthy"] and favorite["failover_alerted"] is not None:
            # only clear on genuine recovery of the favorite, never on the
            # all-down fallback (favorite unhealthy but happens to be [0])
            conn.execute(
                "UPDATE account_links SET failover_alerted = NULL "
                "WHERE group_id = %s::uuid AND account_id = %s",
                (g["group_id"], favorite["account_id"]))
    return due


def mark_failover_alerted(conn, group_ids: list[str]) -> None:
    """Stamp the edge on each group's favorite (min home_rank) row —
    called by the worker AFTER the alert email actually sent."""
    if not group_ids:
        return
    conn.execute(
        """UPDATE account_links SET failover_alerted = now()
           WHERE group_id = ANY(%s::uuid[]) AND home_rank = (
               SELECT MIN(home_rank) FROM account_links l2
               WHERE l2.group_id = account_links.group_id)""",
        (list(group_ids),))
