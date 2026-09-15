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

log = logging.getLogger("oikonome.equity")

KINDS = ("contribution", "draw", "distribution", "reimbursement")


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
    except (TypeError, ValueError):
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
    # One movement per (transaction, kind) — migration 067's partial unique
    # index. A double-submit from the Transactions category dropdown used to
    # record the same owner contribution twice and silently double owner
    # equity. DO NOTHING + a re-read means the second click is a
    # no-op returning the first movement, rather than an error the user has
    # to interpret. Movements with no txn_id are unconstrained, as before.
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


def capital_summary(conn, entity_id: str) -> dict:
    """Totals by kind + the capital-account balance.

    The balance includes RETAINED EARNINGS, because that is what an owner's
    capital account is: contributions + cumulative net income − draws.
    Omitting net income makes a profitable pass-through that distributes
    its profit report a negative capital account, which is wrong — earned
    and distributed profit is not a loss. The movements-only figure is
    still reported as `movements_balance` — it is a real sub-total, it
    just isn't the capital account."""
    rows = conn.execute(
        "SELECT kind, COALESCE(SUM(amount),0) AS s FROM equity_movement "
        "WHERE entity_id = %s GROUP BY kind", (entity_id,)).fetchall()
    by = {r["kind"]: float(r["s"]) for r in rows}
    contrib = by.get("contribution", 0.0)
    draws = by.get("draw", 0.0) + by.get("distribution", 0.0)
    from . import books
    net_income = 0.0
    try:
        # cumulative, all years: net operating income is what accrues to the
        # owner. Guarded — equity must render even if the books read fails.
        # _pnl_core, NOT pnl: pnl attaches this very summary, which would
        # recurse forever (caught only by a RecursionError — found in test)
        net_income = float(
            books._pnl_core(conn, entity_id).get("net_operating") or 0)
    except Exception:                                     # noqa: BLE001
        log.warning("capital summary: net income unavailable", exc_info=True)
    return {"contributions": round(contrib, 2),
            "draws": round(by.get("draw", 0.0), 2),
            "distributions": round(by.get("distribution", 0.0), 2),
            "reimbursements": round(by.get("reimbursement", 0.0), 2),
            "net_income": round(net_income, 2),
            "movements_balance": round(contrib - draws, 2),
            "capital_balance": round(contrib + net_income - draws, 2)}


def _txn_row(conn, txn_id: str) -> tuple:
    """The expense (amount, date) for a fronted business cost — ONE query.
    positive = money out (an expense); contribute/reimburse only make sense for
    an expense, so a deposit/revenue row is rejected."""
    r = conn.execute("SELECT amount, date FROM transactions WHERE id = %s",
                     (txn_id,)).fetchone()
    if not r:
        raise ValueError("no such transaction")
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

    The pair is atomic on purpose. Engine connections are autocommit, so the
    reassignment used to land the moment it ran, before record_movement had
    validated anything (member_id shape, the amount ceiling). A rejected
    movement then returned a 400 while the transaction stayed permanently
    booked to the business with nothing recording why — the P&L and capital
    summary silently wrong, and no 'unassign' anywhere in the UI to undo it.
    Either both halves happen or neither does."""
    amt, txn_date = _txn_row(conn, txn_id)
    with conn.transaction():
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
        date=date, note=note or "capitalized personally-paid expense")


def reimburse_expense(conn, entity_id: str, txn_id: str, *,
                      member_id=None, date=None, note=None) -> dict:
    """The business owes the member back for a fronted cost: assign the
    transaction to the entity and record a reimbursement (a settlement)."""
    return _front_expense(
        conn, entity_id, txn_id, kind="reimbursement", member_id=member_id,
        date=date, note=note or "reimbursement of personally-paid expense")
