"""Owner-equity movements + the reimbursement workflow.

Capital contributions, owner draws/distributions, and reimbursements are their
own classes — they hit the entity's capital account, never personal or business
spend/income. The capital balance = Σ contribution + cumulative net income − Σ (draw +
distribution) — an owner's capital account includes retained earnings, so a
profitable entity that distributes its profit does NOT read as negative
(the movements-only sub-total is still reported as
`movements_balance`). Reimbursements are the business settling a
member-fronted cost (tracked, but not part of the capital balance).

Reimbursement workflow for a business cost paid on a personal card:
  * contribute_expense — the member donates the cost as capital: the
    transaction becomes business AND a matching contribution is recorded
    (capital ↑).
  * reimburse_expense — the business owes the member back: the transaction
    becomes business AND a reimbursement is recorded (a settlement, not
    equity).
Both reconcile — same expense on the business books, different treatment of who
funded it.
"""
from __future__ import annotations

import logging
import math

from .budget import FEED_REMOVED_KEY as _FEED_REMOVED_KEY

log = logging.getLogger("oikonome.equity")

KINDS = ("contribution", "draw", "distribution", "reimbursement")

# The two ways a personally-paid business cost is explained. One expense
# takes exactly one of them, for exactly one entity.
FRONTED_KINDS = ("contribution", "reimbursement")

# The note a fronted booking writes on its movement. The booking door takes
# no note of its own, so this text is what tells a movement that once
# explained a charge from one entered by hand.
FRONTED_NOTES = {"contribution": "capitalized personally-paid expense",
                 "reimbursement": "reimbursement of personally-paid expense"}


# Which movements (alias `m`) the capital figures count. A fronted movement
# explains a cost on the books, and the books skip every removed row: a
# charge the bank feed withdrew, a stuck hold the person or a connector
# retired, a file row retired onto another source's copy. While the
# movement sits on such a row it stops counting, at the same moment the
# cost does, whatever removed it. A row that comes back (a retirement is
# reversible) brings its movement back; a hold that settled had its
# movement carried onto the posted charge, which is live. Only the
# withdrawn ones are ever deleted (release_withdrawn_fronted).
COUNTS_IN_CAPITAL = """NOT (m.kind IN ('contribution', 'reimbursement')
    AND EXISTS (SELECT 1 FROM transactions wt
                 WHERE wt.id = m.txn_id AND wt.removed <> 0))"""


class FrontedExpenseConflict(ValueError):
    """The transaction is already explained by a different movement — the
    other kind, or the same kind for another entity."""


def _row(r: dict) -> dict:
    return {"id": str(r["id"]), "entity_id": str(r["entity_id"]),
            "kind": r["kind"], "amount": float(r["amount"]),
            "date": r["date"].isoformat() if r["date"] else None,
            "member_id": str(r["member_id"]) if r["member_id"] else None,
            "txn_id": r["txn_id"], "form": r["form"], "note": r["note"]}


def record_movement(conn, entity_id: str, *, kind: str, amount, date,
                    member_id=None, txn_id=None, form=None,
                    note=None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    try:
        amt = round(float(amount), 2)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("amount must be a number")
    # float() takes "NaN"/"Infinity" and JSON parsing does too; NaN slips
    # past both range checks below (every comparison is False) and a stored
    # NaN turns the capital SUM into NaN, which no JSON response can carry
    if not math.isfinite(amt):
        raise ValueError("amount must be a number")
    if amt <= 0:
        raise ValueError("amount must be positive")
    # NUMERIC(14,2) tops out below a trillion — past that psycopg raises
    # NumericValueOutOfRange, a 500 where the caller deserves the same 400
    # the "must be positive" check gives.
    if amt > 999_999_999_999.99:
        raise ValueError("amount is too large")
    # a malformed member_id is a bad request, not a DB error — mirrors the
    # _valid_uuid guard entities.get_entity already applies to entity_id
    if member_id is not None:
        from .entities import _valid_uuid
        if not _valid_uuid(member_id):
            raise ValueError("no such member")
    from .compat import req_date
    date = req_date(date)
    # One movement per (transaction, kind) — a partial unique index. A
    # double-submit from the Transactions category dropdown must not record
    # the same owner contribution twice and double owner equity. DO NOTHING
    # + a re-read means the second click is a no-op returning the first
    # movement, rather than an error the user has to interpret. Movements
    # with no txn_id are unconstrained.
    r = conn.execute(
        """INSERT INTO equity_movement
             (entity_id, kind, amount, date, member_id, txn_id, form, note)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT DO NOTHING RETURNING *""",
        (entity_id, kind, amt, date, member_id, txn_id, form, note)).fetchone()
    if r is None and txn_id is not None:
        r = conn.execute(
            "SELECT * FROM equity_movement WHERE txn_id=%s AND kind=%s",
            (txn_id, kind)).fetchone()
        # the unique key carries no entity: the movement that won may belong
        # to another entity, and handing it back as this one's would report
        # success for a booking that never happened
        if r is not None and str(r["entity_id"]) != str(entity_id):
            raise FrontedExpenseConflict(
                f"that transaction already has a {kind} recorded for "
                "another business")
    if r is None:
        raise ValueError("could not record the movement")
    return _row(r)


def list_movements(conn, entity_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM equity_movement WHERE entity_id = %s "
        "ORDER BY date, created_at", (entity_id,)).fetchall()
    return [_row(r) for r in rows]


def delete_movement(conn, movement_id: str, entity_id: str) -> bool:
    # scope by entity too (not just tenant via RLS) so one entity's endpoint
    # can't delete another entity's row within the same tenant
    return conn.execute(
        "DELETE FROM equity_movement WHERE id = %s AND entity_id = %s",
        (movement_id, entity_id)).rowcount > 0


def capital_summary(conn, entity_id: str, *, lifetime=None,
                    today=None) -> dict:
    """Totals by kind + the capital-account balance.

    The balance includes RETAINED EARNINGS, because that is what an owner's
    capital account is: contributions + cumulative net income − draws.
    Omitting net income makes a profitable pass-through that distributes
    its profit report a negative capital account, which is wrong — earned
    and distributed profit is not a loss. The movements-only figure is
    still reported as `movements_balance` — it is a real sub-total, it
    just isn't the capital account. `lifetime` is the entity's all-history
    transaction list (books.entity_transactions) when the caller has it;
    `today` the household's day (read from its settings when omitted),
    which says how much of the start-up allowance has been deducted."""
    rows = conn.execute(
        f"""SELECT m.kind, COALESCE(SUM(m.amount),0) AS s
              FROM equity_movement m
             WHERE m.entity_id = %s AND {COUNTS_IN_CAPITAL}
             GROUP BY m.kind""", (entity_id,)).fetchall()
    by = {r["kind"]: float(r["s"]) for r in rows}
    contrib = by.get("contribution", 0.0)
    draws = by.get("draw", 0.0) + by.get("distribution", 0.0)
    from . import books
    net_income = 0.0
    try:
        # cumulative, all years: net income is what accrues to the owner,
        # and it takes the start-up allowance deducted so far as well as
        # operating costs. Guarded — equity must render even if the books
        # read fails.
        if today is None:
            from .. import localtime
            from . import budget
            today = localtime.now_local(budget.load_config(conn)).date()
        net_income = books.net_income_through(conn, entity_id, today.year,
                                              lifetime, lifetime)
    except Exception:                                     # noqa: BLE001
        log.warning("capital summary: net income unavailable", exc_info=True)
    balance = round(contrib + net_income - draws, 2)
    household_capital, pct = _household_capital(conn, entity_id, net_income)
    return {"contributions": round(contrib, 2),
            "draws": round(by.get("draw", 0.0), 2),
            "distributions": round(by.get("distribution", 0.0), 2),
            "reimbursements": round(by.get("reimbursement", 0.0), 2),
            "net_income": round(net_income, 2),
            "movements_balance": round(contrib - draws, 2),
            "capital_balance": balance,
            "household_share_pct": pct,
            "household_capital": household_capital}


def _household_capital(conn, entity_id: str, net_income: float):
    """The household member's own capital account, and their ownership %.

    Each member of a multi-member business has their own capital account:
    what THEY put in, less what THEY took out, plus their ownership share of
    the net income. A movement naming the household member is wholly
    theirs; one naming another member of this business is wholly that
    member's; one naming nobody (or someone who is not a member here) is
    split by ownership, like the net income. A business whose movements
    name no one — and every sole owner — gets ownership % x the whole
    account. (None, None) when no member is the household or its share is
    not set."""
    ours = conn.execute(
        "SELECT id, ownership_pct FROM entity_membership "
        "WHERE entity_id = %s AND is_household "
        "ORDER BY created_at, id LIMIT 1",
        (entity_id,)).fetchone()
    if not ours or ours["ownership_pct"] is None:
        return None, None
    pct = float(ours["ownership_pct"])
    rows = conn.execute(
        f"""SELECT CASE WHEN m.member_id = %s THEN 'ours'
                        WHEN mem.id IS NOT NULL THEN 'theirs'
                        ELSE 'shared' END AS whose,
                   COALESCE(SUM(CASE WHEN m.kind = 'contribution'
                                     THEN m.amount
                                     WHEN m.kind IN ('draw', 'distribution')
                                     THEN -m.amount ELSE 0 END), 0) AS net
              FROM equity_movement m
              LEFT JOIN entity_membership mem
                     ON mem.id = m.member_id AND mem.entity_id = m.entity_id
             WHERE m.entity_id = %s AND {COUNTS_IN_CAPITAL}
             GROUP BY 1""", (ours["id"], entity_id)).fetchall()
    by = {r["whose"]: float(r["net"]) for r in rows}
    capital = (by.get("ours", 0.0)
               + (by.get("shared", 0.0) + net_income) * pct / 100.0)
    return round(capital, 2), pct


def _txn_row(conn, txn_id: str) -> tuple:
    """The expense (amount, date) for a fronted business cost — ONE query.
    positive = money out (an expense); contribute/reimburse only make sense for
    an expense, so a deposit/revenue row is rejected.

    A fronted cost is one a member paid PERSONALLY. A charge on an account
    a business owns was paid by that business, so booking it as a member's
    contribution (or as money the business owes the member) would raise a
    capital account with money that came out of a business's bank, and
    move the cost off the paying business's books. A personal charge
    already booked to a business by hand stays allowed: the account, not
    the override, says who paid."""
    r = conn.execute(
        """SELECT t.amount, t.date, a.entity_id AS account_entity
             FROM transactions t LEFT JOIN accounts a ON a.id = t.account_id
            WHERE t.id = %s FOR UPDATE OF t""", (txn_id,)).fetchone()
    if not r:
        raise ValueError("no such transaction")
    if r["account_entity"] is not None:
        raise FrontedExpenseConflict(
            "that charge was paid from a business account, not personally: "
            "only a cost a member paid can be capitalized or reimbursed")
    if r["amount"] is None:
        raise ValueError("that transaction has no amount")
    amt = float(r["amount"])
    if amt <= 0:
        raise ValueError("that transaction is money IN, not a business expense")
    return round(amt, 2), r["date"]


def _assign(conn, txn_id: str, entity_id: str) -> None:
    from . import entities
    if not entities.assign_transaction(conn, txn_id, entity_id):
        raise ValueError("no such transaction")


def _front_expense(conn, entity_id: str, txn_id: str, *, kind: str,
                   member_id, date, note) -> dict:
    """Assign a personally-paid business cost to the entity AND record the
    movement that explains it — as ONE unit.

    The pair is atomic on purpose. Engine connections are autocommit, so
    outside a transaction the reassignment would land the moment it ran,
    before record_movement had validated anything (member_id shape, the
    amount ceiling). A rejected movement would then return a 400 while the
    transaction stayed booked to the business with nothing recording why —
    the P&L and capital summary silently wrong, and no 'unassign' in the UI
    to undo it. Either both halves happen or neither does."""
    from . import entities
    with conn.transaction():
        # Under the transaction's row lock, so two picks for one expense
        # serialize: the expense is capitalized OR reimbursed, to ONE
        # entity. A second pick naming another entity would otherwise move
        # the row while the first entity keeps the contribution (one capital
        # account overstated, the other understated), and capitalize-then-
        # reimburse would count one cost twice. Repeating the same pick is
        # a no-op; changing it means deleting the first movement.
        # A pending hold that has posted is fronted as its posted row, at the
        # posted amount (entities.live_txn_id).
        txn_id = entities.live_txn_id(conn, txn_id)
        if txn_id is None:
            raise ValueError("no such transaction")
        amt, txn_date = _txn_row(conn, txn_id)
        # the entity is checked under this transaction, after the row lock
        # (the order entities.delete_forever takes them in), so an archive
        # cannot land between the check and the movement
        entities.require_active(conn, entity_id)
        prior = conn.execute(
            """SELECT m.*, e.name AS entity_name FROM equity_movement m
               LEFT JOIN business_entity e ON e.id = m.entity_id
               WHERE m.txn_id = %s AND m.kind = ANY(%s)
               ORDER BY m.created_at, m.id""",
            (txn_id, list(FRONTED_KINDS))).fetchall()
        for p in prior:
            if str(p["entity_id"]) == str(entity_id) and p["kind"] == kind:
                return _row(p)
        if prior:
            p = prior[0]
            how = ("capitalized" if p["kind"] == "contribution"
                   else "marked reimbursable")
            raise FrontedExpenseConflict(
                f"that expense is already {how} for "
                f"{p['entity_name'] or 'another business'}: delete that "
                "movement first")
        _assign(conn, txn_id, entity_id)
        return record_movement(conn, entity_id, kind=kind, amount=amt,
                               date=date or txn_date, member_id=member_id,
                               txn_id=txn_id, note=note)


def contribute_expense(conn, entity_id: str, txn_id: str, *,
                       member_id=None, date=None, note=None) -> dict:
    """A personally-paid business cost the member donates as capital: assign the
    transaction to the entity and record a matching contribution."""
    return _front_expense(
        conn, entity_id, txn_id, kind="contribution", member_id=member_id,
        date=date, note=note or FRONTED_NOTES["contribution"])


def reimburse_expense(conn, entity_id: str, txn_id: str, *,
                      member_id=None, date=None, note=None) -> dict:
    """The business owes the member back for a fronted cost: assign the
    transaction to the entity and record a reimbursement (a settlement)."""
    return _front_expense(
        conn, entity_id, txn_id, kind="reimbursement", member_id=member_id,
        date=date, note=note or FRONTED_NOTES["reimbursement"])


def release_withdrawn_fronted(conn, txn_ids: list[str], *,
                              older_than_days: int | None = None) -> int:
    """Remove the fronted movements (a capitalized cost or one marked
    reimbursable) still on charges the bank feed withdrew, once their
    twin wait is over, on a business still active (an archived one's
    records are kept as they are; FOR SHARE orders this against an archive
    committing at the same moment). Returns how many were removed.

    The books stop counting a withdrawn charge, so the cost leaves the
    entity's P&L; the movement explaining who funded it would otherwise
    stay on the capital account (or as money the business owes the
    member) for a payment that never happened. The nightly re-anchor sweep
    calls this beside budget.release_withdrawn_pairs with the same row ids
    — those its twin search found no re-numbered twin for — and the same
    `older_than_days`: a row withdrawn more recently can still be carried
    onto its twin, and the movement with it. Only posted rows the feed
    itself withdrew qualify (see release_withdrawn_pairs). Each removal is
    written to the activity log with what it was, so it is never silent."""
    from . import activity
    ids = [i for i in dict.fromkeys(txn_ids or []) if i]
    if not ids:
        return 0
    age = ""
    params: list = [ids, list(FRONTED_KINDS)]
    if older_than_days is not None:
        from .budget import (FEED_REMOVED_OLDER_SQL,
                             log_unreadable_removed_stamps)
        age = FEED_REMOVED_OLDER_SQL
        params.append(int(older_than_days))
        log_unreadable_removed_stamps(conn, ids, "release_withdrawn_fronted")
    removed = 0
    with conn.transaction():
        rows = conn.execute(
            f"""SELECT m.*, e.name AS entity_name, t.name AS txn_name
                  FROM equity_movement m
                  JOIN transactions t ON t.id = m.txn_id
                  JOIN business_entity e ON e.id = m.entity_id
                 WHERE m.txn_id = ANY(%s) AND m.kind = ANY(%s)
                   AND t.removed = 1 AND t.pending = 0
                   AND t.raw ? '{_FEED_REMOVED_KEY}'{age}
                   AND e.status = 'active'
                   FOR UPDATE OF m FOR SHARE OF e""", params).fetchall()
        for r in rows:
            conn.execute("DELETE FROM equity_movement WHERE id = %s",
                         (r["id"],))
            removed += 1
            activity.record(
                conn, "the bank feed", "business", "released",
                target=r["txn_id"],
                label=(f"{r['kind']} of {activity.money(r['amount'])} for "
                       f"{r['entity_name'] or 'a business'} on "
                       f"{r['txn_name'] or 'a charge'} the bank withdrew"),
                detail={"movement": _row(r)})
    return removed


# A fronted booking whose charge was hard-deleted before deletes took the
# movement with them: the foreign key's SET NULL kept it, unlinked, still
# counted on the capital account. Nothing removes these on its own — some
# may be right — so the owner reviews them one by one.
_UNLINKED_FRONTED = """m.txn_id IS NULL
    AND ((m.kind = 'contribution' AND m.note = %s)
         OR (m.kind = 'reimbursement' AND m.note = %s))"""


def _unlinked_params() -> tuple:
    return (FRONTED_NOTES["contribution"], FRONTED_NOTES["reimbursement"])


def unlinked_fronted(conn) -> list[dict]:
    """Fronted movements whose charge no longer exists, newest first, on
    businesses that are not archived (an archived business's records are
    kept as they are). Each row is the movement plus its business name."""
    rows = conn.execute(
        f"""SELECT m.*, e.name AS entity_name
              FROM equity_movement m
              JOIN business_entity e ON e.id = m.entity_id
             WHERE {_UNLINKED_FRONTED}
               AND e.status IS DISTINCT FROM 'archived'
             ORDER BY m.date DESC, m.created_at DESC""",
        _unlinked_params()).fetchall()
    return [{**_row(r), "entity_name": r["entity_name"]} for r in rows]


def delete_unlinked_fronted(conn, movement_id: str) -> dict | None:
    """Remove one movement listed by unlinked_fronted. A movement still
    linked to its charge, one entered by hand, or one on an archived
    business is not removed. Returns the removed row, or None."""
    from .entities import _valid_uuid, require_active
    if not _valid_uuid(movement_id):
        return None
    with conn.transaction():
        r = conn.execute(
            f"""SELECT m.*, e.name AS entity_name
                  FROM equity_movement m
                  JOIN business_entity e ON e.id = m.entity_id
                 WHERE m.id = %s AND {_UNLINKED_FRONTED}""",
            (movement_id, *_unlinked_params())).fetchone()
        if r is None:
            return None
        # the business row held FOR SHARE, so an archive waits for this
        try:
            require_active(conn, str(r["entity_id"]))
        except ValueError:
            return None
        gone = conn.execute(
            f"""DELETE FROM equity_movement m
                 WHERE m.id = %s AND {_UNLINKED_FRONTED}""",
            (movement_id, *_unlinked_params())).rowcount
        if not gone:
            return None
    return {**_row(r), "entity_name": r["entity_name"]}
