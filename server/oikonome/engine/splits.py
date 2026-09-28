"""A hand split of one ledger row across categories.

A single charge is often several things — the warehouse-club run that was
groceries and a lawn chair, the payment to a roommate that was rent and the power
bill. A receipt's line items describe a charge but never re-bucket it (see
specs/receipts.md); a split does: the parts written here are what every
per-category rollup sees in place of the row, and they must add up to it.

Rules, enforced here and nowhere else:

  * at least two parts, each a spend category (a Plaid spend primary or a
    custom name — never a transfer, income or loan payment: the whole-row
    spend test never joins the parts, so a flow part would make the
    category rollups and the totals disagree);
  * every part's amount is non-zero and carries the row's sign, to the
    cent; the parts sum to the row's amount to the cent;
  * no category twice — two parts under one category are one part.
  * only a row the spend rollups read can be split (budget.SPEND_ONLY_SQL:
    money out, not a transfer, not a card payment, not on a loan account);
    a split on any other row would be shown and counted nowhere.

The split is an overlay. The row's own category_override / category_primary
are untouched, so the sync, the bill pass and the store matchers need know
nothing; removing the split hands the row back to them whole.
"""
from __future__ import annotations

import math

from . import categories as _cats
from .budget import SPEND_ONLY_SQL

MAX_PARTS = 20
MAX_CATEGORY_LEN = 80
# Far past any charge a household ledger holds, and far inside what a
# double rounds to exact cents.
MAX_AMOUNT = 1e12


class SplitError(ValueError):
    """A split a person wrote that cannot be stored — the message says why,
    in words the client shows as-is."""


def _cents(x: float) -> int:
    return int(round(x * 100))


def _money(x) -> bool:
    """A number that can be a charge: JSON hands over NaN and infinity as
    floats and a 400-digit integer as an int, and rounding any of them to
    cents raises ValueError/OverflowError — an unhandled 500 at the door
    instead of a refusal that names the part."""
    try:
        f = float(x)
    except OverflowError:
        return False
    return math.isfinite(f) and abs(f) <= MAX_AMOUNT


def validate(row_amount: float, parts: list) -> list[dict]:
    """Check `parts` ([{category, amount}, …]) against the row's amount and
    return them normalised (stripped category, amount rounded to the cent,
    in the order given). Raises SplitError with the reason otherwise."""
    if not isinstance(parts, list):
        raise SplitError("parts must be a list")
    if len(parts) < 2:
        raise SplitError("a split needs at least two parts")
    if len(parts) > MAX_PARTS:
        raise SplitError(f"at most {MAX_PARTS} parts")
    if row_amount is None or not _money(row_amount) or _cents(row_amount) == 0:
        raise SplitError("a zero-amount row cannot be split")
    out: list[dict] = []
    seen: set[str] = set()
    sign = 1 if row_amount > 0 else -1
    total = 0
    for i, p in enumerate(parts, 1):
        if not isinstance(p, dict):
            raise SplitError(f"part {i} must be an object")
        cat = p.get("category")
        if not isinstance(cat, str) or not cat.strip():
            raise SplitError(f"part {i} needs a category")
        cat = cat.strip()
        if len(cat) > MAX_CATEGORY_LEN:
            raise SplitError(f"part {i}: category name too long")
        if cat.upper() in _cats.FLOW:
            raise SplitError(
                "a part must be a spending category — transfers, income and "
                "loan payments are decided for the whole row")
        if cat in seen:
            raise SplitError(f"{cat.replace('_', ' ')} appears twice")
        seen.add(cat)
        amt = p.get("amount")
        if (isinstance(amt, bool) or not isinstance(amt, (int, float))
                or not _money(amt)):
            raise SplitError(f"part {i} needs an amount")
        c = _cents(float(amt))
        if c == 0:
            raise SplitError(f"part {i} has no amount")
        if c * sign < 0:
            raise SplitError("every part must go the same way as the charge")
        total += c
        out.append({"category": cat, "amount": c / 100})
    if total != _cents(row_amount):
        diff = (_cents(row_amount) - total) / 100
        raise SplitError(
            f"the parts must add up to the charge — {abs(diff):.2f} "
            f"{'left over' if diff > 0 else 'too much'}")
    return out


def for_txn(conn, txn_id: str) -> list[dict]:
    return [{"category": r["category"], "amount": r["amount"]}
            for r in conn.execute(
                "SELECT category, amount FROM transaction_splits "
                "WHERE txn_id = %s ORDER BY line", (txn_id,)).fetchall()]


# The spend test the rollups apply to a row, as SQL on `t`: a split
# re-buckets spending, so a row no spend rollup reads — a refund, a
# transfer, a card payment, the loan side of a payment — would carry parts
# nobody counts. Exactly budget.SPEND_ONLY_SQL (the predicate _spend_rows
# and reporting.SPEND_WHERE share) and nothing stricter: a row the rollups
# count is a row a person may split, whatever category it wears — the
# parts themselves refuse flow categories. Deliberately not `removed`: a
# retired row's split is waiting to follow its charge. The ledger row
# carries it as `splittable`, so the clients offer Split where this does.
SPLITTABLE_SQL = f"TRUE {SPEND_ONLY_SQL}"


def splittable(conn, txn_id: str) -> bool:
    """Would the split door take this row? False for a row that does not
    exist."""
    row = conn.execute(
        f"SELECT ({SPLITTABLE_SQL}) AS ok FROM transactions t WHERE t.id = %s",
        (txn_id,)).fetchone()
    return bool(row and row["ok"])


def set_split(conn, txn_id: str, parts: list) -> list[dict]:
    """Replace the row's split with `parts` (validated against the row's
    current amount). Returns the stored parts. Raises SplitError, or
    LookupError when the row does not exist.

    The amount is read under a row lock in the same transaction as the
    write: read before it, a sync posting a new amount in between would
    leave parts adding up to a total the bank never charged, written after
    the stale sweep had already looked."""
    with conn.transaction():
        row = conn.execute(
            f"""SELECT t.amount, ({SPLITTABLE_SQL}) AS spend
                  FROM transactions t
                 WHERE t.id = %s AND t.removed = 0 FOR UPDATE OF t""",
            (txn_id,)).fetchone()
        if not row:
            raise LookupError(txn_id)
        if not row["spend"]:
            raise SplitError(
                "only spending can be split — refunds, transfers and card "
                "payments count as one whole row")
        clean = validate(row["amount"], parts)
        conn.execute("DELETE FROM transaction_splits WHERE txn_id = %s",
                     (txn_id,))
        for i, p in enumerate(clean, 1):
            conn.execute(
                "INSERT INTO transaction_splits (txn_id, line, category, amount)"
                " VALUES (%s, %s, %s, %s)",
                (txn_id, i, p["category"], p["amount"]))
    return clean


def clear_split(conn, txn_id: str) -> int:
    return conn.execute("DELETE FROM transaction_splits WHERE txn_id = %s",
                        (txn_id,)).rowcount


def move_split(conn, old_id: str, new_id: str) -> int:
    """Hand `old_id`'s split to `new_id` — the same charge under the id it
    lives on — when the row it hangs on is retired. Returns the parts moved.

    All or nothing: a split is several rows that only mean anything
    together, so it never lands beside, or over, parts `new_id` already
    has. What stays on `old_id` is deleted only when it is exactly the
    split `new_id` holds; a differing one is somebody's answer and stays
    put for the re-anchor sweep to count, not to destroy. A move onto a
    row of another amount is left to `drop_stale`, which judges the parts
    against the row that counts."""
    n = conn.execute(
        """UPDATE transaction_splits SET txn_id = %s
            WHERE txn_id = %s AND NOT EXISTS
                  (SELECT 1 FROM transaction_splits WHERE txn_id = %s)""",
        (new_id, old_id, new_id)).rowcount
    conn.execute(
        """DELETE FROM transaction_splits
            WHERE txn_id = %s AND NOT EXISTS (
                  (SELECT category, amount FROM transaction_splits
                    WHERE txn_id = %s
                   EXCEPT SELECT category, amount FROM transaction_splits
                    WHERE txn_id = %s)
                  UNION ALL
                  (SELECT category, amount FROM transaction_splits
                    WHERE txn_id = %s
                   EXCEPT SELECT category, amount FROM transaction_splits
                    WHERE txn_id = %s))""",
        (old_id, old_id, new_id, new_id, old_id))
    return n


def _stale_sql(sp: str) -> str:
    """True when the split on `{sp}.txn_id` no longer holds against its row
    `t`, read as of the statement running it."""
    return (f"""(ROUND((SELECT SUM(x.amount) FROM transaction_splits x
                        WHERE x.txn_id = {sp}.txn_id)::numeric, 2)
                 <> ROUND(t.amount::numeric, 2)
                 OR NOT ({SPLITTABLE_SQL}))""")


def stale(conn) -> list[str]:
    """Rows whose split no longer holds: the amount moved out from under
    it (a pending charge that posted for a different total), so a rollup
    would count the parts' sum, not the row; or the row stopped passing
    the spend test (a person made it a transfer, say), so no rollup reads
    the parts while the ledger still shows them. A category a machine pass
    stamps is judged only through that same test, never on its own. The
    sync path calls this to drop those splits rather than carry a lie."""
    return [r["txn_id"] for r in conn.execute(
        f"""SELECT DISTINCT sp.txn_id
             FROM transaction_splits sp
             JOIN transactions t ON t.tenant_id = sp.tenant_id AND t.id = sp.txn_id
            WHERE {_stale_sql("sp")}
            ORDER BY sp.txn_id"""
    ).fetchall()]


def drop_stale(conn) -> int:
    """Delete the stale splits. The condition is judged again inside the
    DELETE: a person re-splitting a row between the listing and the
    delete wrote parts that hold, and deleting by id alone would throw
    them away with the stale ones."""
    ids = stale(conn)
    if not ids:
        return 0
    return conn.execute(
        f"""DELETE FROM transaction_splits sp USING transactions t
             WHERE sp.txn_id = ANY(%s)
               AND t.tenant_id = sp.tenant_id AND t.id = sp.txn_id
               AND {_stale_sql("sp")}""", (ids,)).rowcount
