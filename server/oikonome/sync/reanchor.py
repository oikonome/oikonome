"""What a person wrote on a transaction follows the charge, not the row id.

A transaction is retired (`removed = 1`) in several ways that are not the
pending→posted settlement: a removal delta after an Item re-link retires a
posted row while the same charge lives on under the new Item's id; a
stateless connector re-sends its window and a held charge vanishes; a
full-replace push restates an account. Every read hides removed rows, so a
category, a note, a receipt, a reimbursement pairing or a business tag left
on one is invisible from that moment, to every read and every export.
The settlement carry in `sync/base.py`
covers only the path where the aggregator names the predecessor.

This module is the general answer: for every removed row that still carries
something a person wrote, find the ONE live row that is the same charge —
same account, same amount, dated within a week, preferring the same name —
and move the children over, guarded so a child the live row already has is
never overwritten (notes are appended, exact duplicates dropped). Two
look-alikes is ambiguous and is left alone and counted; no look-alike is
counted too. Runs after every removal and once a night, and by hand as
`oikonome reanchor-stranded`.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# the tables that anchor a person's work to a transaction id
CHILD_TABLES = (
    ("manual_categories", "transaction_id"),
    ("transaction_notes", "txn_id"),
    ("business_flags", "txn_id"),
    ("business_txn_class", "txn_id"),
    ("reimburse_flags", "txn_id"),
    ("receipts", "txn_id"),
    ("amazon_matches", "transaction_id"),
    ("costco_matches", "transaction_id"),
    ("equity_movement", "txn_id"),
    ("transaction_splits", "txn_id"),
)

# One row per transaction, with the columns that make two of them the SAME
# answer. A child that cannot move because the live row has its own stays on
# the retired row unless it is a copy of what is already there — losing a
# person's differing answer to a hidden row is not a tidy-up, it is a
# deletion.
ONE_PER_TXN = (
    ("manual_categories", "transaction_id", ("category", "bill_id")),
    ("business_flags", "txn_id", ()),
    ("business_txn_class", "txn_id", ("bucket", "sched_c_line", "note")),
    ("reimburse_flags", "txn_id", ("partial", "expected")),
    ("amazon_matches", "transaction_id", ("dedup_key",)),
    ("costco_matches", "transaction_id", ("dedup_key",)),
)

# Letters and digits of any script: a household's merchants are not all
# spelled in ASCII, and a name that tokenises to nothing can never match.
_WORD = re.compile(r"[^\W_]+")


def _line(name: str | None) -> str:
    """A statement line reduced to what makes it the same line: case and
    runs of whitespace dropped, nothing else."""
    return " ".join((name or "").casefold().split())


def _tokens(name: str | None) -> list[str]:
    """The words of a statement line that carry identity.

    The line goes through the merchant canonicaliser first, so a payment
    processor's prefix ('PAYPAL *', 'SQ *'), a store number or a trailing
    city is not mistaken for the payee: two unrelated sellers paid through
    one processor share a prefix and nothing else. Pure numbers carry no
    identity either."""
    if not name:
        return []
    from ..engine.merchant_dedup import canonical_merchant
    label = canonical_merchant(name) or name
    return [w for w in _WORD.findall(label.casefold()) if not w.isdigit()]


def _same_payee(a: str | None, b: str | None) -> bool:
    """Do two statement lines name the same payee?

    The same charge re-numbered usually keeps its line verbatim, and that
    alone is evidence in any script. Beyond it, a hold that posts can gain
    or lose a store number or a processor prefix, so equality alone would
    refuse honest carries: the same identifying words, or one line's words
    contained in the other's, is evidence enough. A shared leading word is
    NOT — one brand sells unrelated things under one first word, and moving
    a receipt onto the wrong charge is worse than leaving it where it is.
    No words at all is no evidence and no match."""
    la, lb = _line(a), _line(b)
    if la and la == lb:
        return True
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    sa, sb = set(ta), set(tb)
    return sa <= sb or sb <= sa


def _names(row: dict) -> tuple:
    """Every string that can name this row's payee.

    The merchant name (else the bank line), the bank line itself, and an
    aggregator name the resolver moved OFF the row's key. The set-aside name
    is still evidence of what the charge was: a hold and its posted twin are
    often spelled differently by the bank ('CORNER DELI' settling as
    'CORNER DELI MKT 0001'), and the aggregator's one name for both
    is then the only string the two share. Leaving it out strands the
    person's note and receipt on a row no read will ever show again."""
    return (row["mname"], row["name"], row.get("aside"))


def _same_aside(row: dict, cand: dict) -> bool:
    """Do the two rows wear the same aggregator name, one of them with it
    set aside? Exact equality, as the other tie-breaks are: breaking a tie
    is about being SURE which look-alike is the charge, and a token subset
    is not sure."""
    a, b = row.get("aside"), cand.get("aside")
    return bool((a and a in (b, cand["mname"])) or (b and b == row["mname"]))


def _looks_like(row: dict, cand: dict) -> bool:
    """Any name the retired row wears against any the candidate wears — a
    re-link can restate the merchant name while the raw line stays put, and
    vice versa."""
    return any(_same_payee(x, y)
               for x in _names(row) for y in _names(cand))


def stranded(conn, ids: list[str] | None = None) -> list[dict]:
    """Removed rows that still carry a child or an override of their own.

    An override kind with no pin under it counts: a person's "use
    automatic" is an EMPTY pin wearing their kind, and on a row with no
    note or receipt it is the only thing there is to carry. Leave it and
    the twin arrives wearing no kind, which the store and bill passes read
    as a row nobody ever touched and stamp a category back onto.

    A row a PERSON retired by hand (`retired_at`) is excluded: they said
    that hold is not a charge, the retirement is meant to be reversible by
    flipping one column back, and when such a hold does post the aggregator
    names it as the predecessor — so the settlement carry moves the work
    with the link in hand instead of this module guessing at one."""
    refs = " OR ".join(
        f"EXISTS (SELECT 1 FROM {t} c WHERE c.{col} = t.id)"
        for t, col in CHILD_TABLES)
    refs += (" OR EXISTS (SELECT 1 FROM reimbursements r"
             " WHERE r.expense_id = t.id OR r.reimburse_id = t.id)")
    scope = "AND t.id = ANY(%s)" if ids is not None else ""
    return conn.execute(
        f"""SELECT t.id, t.account_id, t.date, t.amount, t.name,
                   COALESCE(t.merchant_name, t.name) AS mname,
                   t.merchant_name_set_aside AS aside,
                   t.category_override, t.owner_override, t.entity_id
              FROM transactions t
             WHERE t.removed <> 0 AND t.retired_at IS NULL {scope}
               AND (t.category_override IS NOT NULL
                    OR t.override_source IS NOT NULL
                    OR t.owner_override IS NOT NULL
                    OR t.entity_id IS NOT NULL OR {refs})""",
        ((ids,) if ids is not None else ())).fetchall()


def find_twin(conn, row: dict) -> tuple[str | None, str]:
    """(live id, verdict): the one live row that is the same charge.

    Same account and amount, inside a week of the retired row, and wearing
    the same payee. Several candidates narrow to the ones wearing the same
    name exactly; still several is 'ambiguous' and nothing moves — a
    repeated coffee is not a re-numbered charge.

    The window stops SHORT of seven days on either side and one candidate
    still has to look like the same payee: a charge of the same amount
    exactly a week away is what a weekly subscription looks like, and a
    lone same-amount stranger is a different purchase that happened to cost
    the same. Either would take the person's note and receipts onto a
    charge they never wrote them about."""
    cands = conn.execute(
        """SELECT id, COALESCE(merchant_name, name) AS mname, name,
                  merchant_name_set_aside AS aside, date
             FROM transactions
            WHERE removed = 0 AND id <> %s AND account_id = %s
              AND abs(amount - %s) < 0.005
              AND date > %s - 7 AND date < %s + 7
            ORDER BY abs(date - %s), id""",
        (row["id"], row["account_id"], row["amount"], row["date"],
         row["date"], row["date"])).fetchall()
    if not cands:
        return None, "no_twin"
    if len(cands) > 1:
        same = [c for c in cands if c["mname"] == row["mname"] or c["name"] == row["name"]]
        if len(same) != 1:
            # the set-aside name gets a say only where the strings the ledger
            # shows leave a tie, so a candidate wearing the retired row's own
            # bank line is never passed over for one wearing a name that was
            # moved off a key
            picked = {c["id"] for c in same}
            same = same + [c for c in cands
                           if c["id"] not in picked and _same_aside(row, c)]
        if len(same) == 1:
            return same[0]["id"], "matched"
        return None, "ambiguous"
    if not _looks_like(row, cands[0]):
        return None, "no_twin"
    return cands[0]["id"], "matched"


def carry(conn, old_id: str, new_id: str) -> dict | None:
    """Move everything a person wrote on `old_id` onto `new_id`, never
    overwriting what `new_id` already has. Returns rows moved per table, or
    None when the pair no longer qualifies.

    The whole move is one transaction and it starts by locking both rows
    and reading their state again. The scan that chose this pair ran
    earlier, and a full-replace pull restating an account in the meantime
    un-retires the source row (or retires the twin) — carrying then empties
    a row that is live again, in front of the person reading it. Both ids
    are locked in one ordered statement so two sweeps meeting on the same
    pair queue instead of deadlocking."""
    with conn.transaction():
        return _move(conn, old_id, new_id)


def _move(conn, old_id: str, new_id: str) -> dict | None:
    """The move itself; `carry` holds the transaction around it."""
    state = {r["id"]: r["removed"] for r in conn.execute(
        "SELECT id, removed FROM transactions WHERE id = ANY(%s) "
        "ORDER BY id FOR UPDATE", ([old_id, new_id],)).fetchall()}
    if not state.get(old_id) or state.get(new_id) != 0:
        return None
    moved: dict[str, int] = {}
    # The row's own overrides: the live row keeps its own answer if it has
    # one, and the pin travels WITH the kind that wrote it. A carried pin
    # whose kind was left behind reads as one nobody set, which the store
    # and bill passes are free to overwrite; and a person's "use automatic"
    # is an EMPTY pin with their kind on it, so a live row holding one has
    # its own answer even though the pin itself is null.
    took = "k.category_override IS NULL AND k.override_source IS NULL"
    conn.execute(
        f"""UPDATE transactions k SET
               category_override = CASE WHEN {took}
                    THEN l.category_override ELSE k.category_override END,
               override_source   = CASE WHEN {took}
                    THEN l.override_source ELSE k.override_source END,
               owner_override    = COALESCE(k.owner_override, l.owner_override),
               entity_id         = COALESCE(k.entity_id,      l.entity_id)
           FROM transactions l WHERE k.id = %s AND l.id = %s""",
        (new_id, old_id))
    # notes: one per row — the live row's own note keeps the retired one's
    # words appended rather than losing them
    conn.execute(
        """UPDATE transaction_notes k
              SET note = k.note || E'\\n' || l.note, updated_at = now()
             FROM transaction_notes l
            WHERE k.txn_id = %s AND l.txn_id = %s AND l.note <> ''
              AND position(l.note in k.note) = 0""",
        (new_id, old_id))
    n = conn.execute(
        """UPDATE transaction_notes SET txn_id = %s
            WHERE txn_id = %s AND NOT EXISTS
                  (SELECT 1 FROM transaction_notes WHERE txn_id = %s)""",
        (new_id, old_id, new_id)).rowcount
    moved["transaction_notes"] = n
    # the retired note goes only once its words are demonstrably on the
    # live row
    conn.execute(
        """DELETE FROM transaction_notes l USING transaction_notes k
            WHERE l.txn_id = %s AND k.txn_id = %s
              AND position(l.note in k.note) > 0""",
        (old_id, new_id))
    # one-row-per-transaction tables: move unless the live row has its own.
    # Two retired rows can point at one live row — the first moves, and the
    # second must not have its answers deleted for arriving late — so what
    # stays is deleted only when it is a copy of what the live row holds.
    for table, col, cols in ONE_PER_TXN:
        n = conn.execute(
            f"""UPDATE {table} SET {col} = %s
                 WHERE {col} = %s AND NOT EXISTS
                       (SELECT 1 FROM {table} WHERE {col} = %s)""",
            (new_id, old_id, new_id)).rowcount
        moved[table] = n
        same = " AND ".join(f"l.{c} IS NOT DISTINCT FROM k.{c}" for c in cols)
        conn.execute(
            f"""DELETE FROM {table} l USING {table} k
                 WHERE l.{col} = %s AND k.{col} = %s
                   {"AND " + same if same else ""}""",
            (old_id, new_id))
    # a hand split is several rows that only mean anything together: all
    # of them move or none, and a live row's own split is never mixed with
    # or replaced by the retired row's
    from ..engine.splits import move_split
    moved["transaction_splits"] = move_split(conn, old_id, new_id)
    # receipts are many per row: all of them follow
    moved["receipts"] = conn.execute(
        "UPDATE receipts SET txn_id = %s WHERE txn_id = %s",
        (new_id, old_id)).rowcount
    moved["equity_movement"] = conn.execute(
        """UPDATE equity_movement e SET txn_id = %s
            WHERE e.txn_id = %s AND NOT EXISTS
                  (SELECT 1 FROM equity_movement k
                    WHERE k.txn_id = %s AND k.kind = e.kind)""",
        (new_id, old_id, new_id)).rowcount
    # a reimbursement pairing names two rows; either side may be the one
    n = conn.execute(
        """UPDATE reimbursements r SET expense_id = %s
            WHERE r.expense_id = %s AND NOT EXISTS
                  (SELECT 1 FROM reimbursements x
                    WHERE x.expense_id = %s AND x.reimburse_id = r.reimburse_id)""",
        (new_id, old_id, new_id)).rowcount
    n += conn.execute(
        """UPDATE reimbursements r SET reimburse_id = %s
            WHERE r.reimburse_id = %s AND NOT EXISTS
                  (SELECT 1 FROM reimbursements x
                    WHERE x.reimburse_id = %s AND x.expense_id = r.expense_id)""",
        (new_id, old_id, new_id)).rowcount
    moved["reimbursements"] = n
    # a pairing that stayed is one the live row already has to the same
    # counterpart; any other pairing is this row's own and keeps its place
    conn.execute(
        """DELETE FROM reimbursements r
            WHERE (r.expense_id = %s AND EXISTS
                     (SELECT 1 FROM reimbursements x WHERE x.expense_id = %s
                       AND x.reimburse_id = r.reimburse_id))
               OR (r.reimburse_id = %s AND EXISTS
                     (SELECT 1 FROM reimbursements x WHERE x.reimburse_id = %s
                       AND x.expense_id = r.expense_id))""",
        (old_id, new_id, old_id, new_id))
    # the retired row lets go of exactly what the live row took from it,
    # and keeps an answer the live row refused: nulling that one would
    # destroy it, not tidy up
    gone = ("k.category_override IS NOT DISTINCT FROM l.category_override"
            " AND k.override_source IS NOT DISTINCT FROM l.override_source")
    conn.execute(
        f"""UPDATE transactions l SET
               category_override = CASE WHEN {gone}
                    THEN NULL ELSE l.category_override END,
               override_source   = CASE WHEN {gone}
                    THEN NULL ELSE l.override_source END,
               owner_override = CASE
                    WHEN k.owner_override IS NOT DISTINCT FROM l.owner_override
                    THEN NULL ELSE l.owner_override END,
               entity_id = CASE
                    WHEN k.entity_id IS NOT DISTINCT FROM l.entity_id
                    THEN NULL ELSE l.entity_id END
           FROM transactions k WHERE l.id = %s AND k.id = %s""",
        (old_id, new_id))
    return {k: v for k, v in moved.items() if v}


def reanchor_stranded(conn, ids: list[str] | None = None) -> dict:
    """Re-anchor every stranded removed row (or just `ids`) onto its live
    twin. Returns {"moved", "ambiguous", "no_twin", "left", "raced",
    "tables"}; the log line names the counts, so a sweep that finds nothing
    is silent.

    A carried row ends up in exactly one of two counts: "moved" when
    nothing is left on it, "left" when the live twin had its own answer and
    the retired row keeps hers. A left row is found again by every later
    sweep, which is the honest state — nothing can move it automatically
    without overwriting somebody's work."""
    out: dict = {"moved": 0, "ambiguous": 0, "no_twin": 0, "left": 0,
                 "raced": 0, "tables": {}}
    for row in stranded(conn, ids):
        twin, verdict = find_twin(conn, row)
        if twin is None:
            out[verdict] += 1
            continue
        try:
            with conn.transaction():
                tables = carry(conn, row["id"], twin)
                if tables is None:
                    out["raced"] += 1
                    continue
                for t, n in tables.items():
                    out["tables"][t] = out["tables"].get(t, 0) + n
                left = bool(stranded(conn, [row["id"]]))
        except Exception:                                 # noqa: BLE001
            # one row's carry must not stop the rest; the next run retries
            log.warning("re-anchor of %s onto %s failed", row["id"], twin,
                        exc_info=True)
            continue
        out["left" if left else "moved"] += 1
    if out["moved"] or out["ambiguous"] or out["left"]:
        log.info("re-anchored %d stranded row(s) onto their live twins "
                 "(%d ambiguous, %d without a twin, %d left on the retired "
                 "row, %d overtaken by a pull): %s",
                 out["moved"], out["ambiguous"], out["no_twin"], out["left"],
                 out["raced"], out["tables"])
    return out
