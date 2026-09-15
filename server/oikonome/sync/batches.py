"""Import batches: tag every file import, list them, one-click rollback.
Batch ownership rides in each row's raw JSONB:

  * `_batches` — JSON ARRAY of every batch id that (re)imported the row.
    ids are content-hashed, so re-importing an overlapping file UPSERTS the
    same row; the new batch id is APPENDED (tag() merges against the rows
    already in the table because upsert overwrites raw wholesale).
  * `_batch`  — the latest batch id, kept for display.

Rollback removes the batch id from each row's array and only DELETEs rows
whose array becomes empty — rolling back a re-import no longer destroys
rows an earlier batch legitimately created. No transactions schema change.
"""

from __future__ import annotations

import json
import uuid

# `_batches` is only ever written as an array here, but a row restored from
# an archive written elsewhere can hold anything, and `jsonb_array_length`
# raises on a non-array exactly the way `jsonb_array_elements` does — which
# would take out the whole Import page or a rollback rather than one row.
# Neither `raw ? '_batches'` nor `@>` says anything about the shape, so the
# read sites below go through the read layer's shape guard.
from ..web.data import _as_array


def create(conn, source: str, account_id: str | None,
           filename: str | None) -> str:
    bid = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO import_batches (id, source, account_id, filename) "
        "VALUES (%s,%s,%s,%s)", (bid, source, account_id, filename))
    return bid


def finish(conn, batch_id: str, row_count: int) -> None:
    conn.execute("UPDATE import_batches SET row_count=%s WHERE id=%s",
                 (row_count, batch_id))


def tag(conn, txns: list, batch_id: str) -> list:
    """Stamp batch ownership into each normalized Transaction's raw.

    Called just before upsert_transactions, whose ON CONFLICT overwrites raw
    wholesale — so the append-on-reimport merge happens HERE: any _batches
    array already on the row (same content-hashed id from an earlier
    overlapping import) is carried forward with this batch id appended."""
    ids = [t.id for t in txns]
    prior: dict[str, list] = {}
    if ids:
        for r in conn.execute(
                "SELECT id, raw->'_batches' AS batches, raw->>'_batch' AS latest"
                " FROM transactions WHERE id = ANY(%s)", (ids,)).fetchall():
            arr = r["batches"]
            if not arr and r["latest"]:
                arr = [r["latest"]]            # pre-array single tag
            prior[r["id"]] = arr or []
    for t in txns:
        merged = [b for b in prior.get(t.id, []) if b != batch_id] + [batch_id]
        t.raw = {**(t.raw or {}), "_batches": merged, "_batch": batch_id}
    return txns


def recent(conn, limit: int = 10) -> list:
    """The latest batches, each with `annotations`: what a person has since
    attached to this batch's rows — receipts, notes, reimbursement
    pairings, business tags, category overrides. A rollback hard-deletes
    the rows and everything cascades with them; the confirm has to be
    able to say so, because the import did not create any of it."""
    rows = [dict(r) for r in conn.execute(
        """SELECT id, source, account_id, filename, row_count, created_at
           FROM import_batches ORDER BY created_at DESC LIMIT %s""",
        (limit,)).fetchall()]
    for b in rows:
        b["annotations"] = 0
    ids = [b["id"] for b in rows if (b["row_count"] or 0) > 0]
    if not ids:
        return rows
    # ONE pass over the ledger for every listed batch (this runs on each
    # Import page view): the rows a rollback would delete are those owned
    # by exactly that batch, and every child table hanging off them goes
    # with the cascade (migration 067 + the receipts FK) — count them all
    counts = conn.execute(
        """WITH owned AS (
             SELECT t.id, t.category_override,
                    COALESCE(t.raw->'_batches'->>0, t.raw->>'_batch') AS bid
               FROM transactions t
              WHERE (t.raw->>'_batch' = ANY(%s) AND NOT t.raw ? '_batches')
                 OR (t.raw->'_batches' ?| %s
                     AND jsonb_array_length(""" +
        _as_array("t.raw->'_batches'") + """) = 1))
           SELECT bid,
                  (SELECT count(*) FROM receipts r WHERE r.txn_id = o.id)
                + (SELECT count(*) FROM transaction_notes n WHERE n.txn_id = o.id)
                + (SELECT count(*) FROM reimbursements m
                    WHERE m.expense_id = o.id OR m.reimburse_id = o.id)
                + (SELECT count(*) FROM reimburse_flags f WHERE f.txn_id = o.id)
                + (SELECT count(*) FROM business_flags g WHERE g.txn_id = o.id)
                + (SELECT count(*) FROM business_txn_class c WHERE c.txn_id = o.id)
                + (SELECT count(*) FROM manual_categories k
                    WHERE k.transaction_id = o.id)
                + CASE WHEN o.category_override IS NOT NULL THEN 1 ELSE 0 END
                  AS n
             FROM owned o""", (ids, ids)).fetchall()
    per: dict[str, int] = {}
    for c in counts:
        per[c["bid"]] = per.get(c["bid"], 0) + int(c["n"] or 0)
    for b in rows:
        b["annotations"] = per.get(b["id"], 0)
    return rows


def discard(conn, batch_id: str | None) -> None:
    """Drop a batch record that never finished — a refusal raised after
    `create` used to leave a phantom row (row_count 0, the column's
    default) in Recent imports. Only when the batch owns NO rows: an
    importer that failed after some rows had already landed must keep
    its record, or those rows fall out of Recent imports and out of the
    reach of rollback while still counting in every aggregate."""
    if not batch_id:
        return
    member = json.dumps([batch_id])
    conn.execute(
        """DELETE FROM import_batches WHERE id=%s AND row_count = 0
             AND NOT EXISTS (SELECT 1 FROM transactions
                              WHERE raw->>'_batch' = %s
                                 OR raw->'_batches' @> %s::jsonb)""",
        (batch_id, batch_id, member))


class RollbackResult(int):
    """rollback()'s return: an int (rows deleted — legacy callers format it
    directly) that also carries the overlap warning as `.warning` and via
    dict-style access (result["deleted"] / result["warning"])."""

    warning: str | None

    def __new__(cls, deleted: int, warning: str | None = None):
        obj = super().__new__(cls, deleted)
        obj.warning = warning
        return obj

    def __getitem__(self, key):
        if key == "deleted":
            return int(self)
        if key == "warning":
            return self.warning
        raise KeyError(key)


def _overlap_warning(conn, batch_id: str) -> str | None:
    """A LATER batch that imported an overlapping window on the same
    accounts may have skipped rows as duplicates of THIS batch's rows;
    rollback cannot resurrect those — warn so the user re-imports."""
    member = json.dumps([batch_id])
    scope = conn.execute(
        """SELECT min(date) AS lo, max(date) AS hi,
                  array_agg(DISTINCT account_id) AS accts
           FROM transactions
           WHERE raw->'_batches' @> %s::jsonb OR raw->>'_batch' = %s""",
        (member, batch_id)).fetchone()
    if not scope or scope["lo"] is None:
        return None
    created = conn.execute(
        "SELECT created_at FROM import_batches WHERE id=%s",
        (batch_id,)).fetchone()
    if not created:
        return None
    later = conn.execute(
        """SELECT DISTINCT b.id
           FROM transactions t
                CROSS JOIN LATERAL jsonb_array_elements_text(""" +
        # the guard wraps the WHOLE expression, not just `_batches`: a row
        # that predates `_batches` has no such key, and COALESCE must still
        # be allowed to fall through to the single `_batch` id.
        _as_array("COALESCE(t.raw->'_batches', "
                  "jsonb_build_array(t.raw->>'_batch'))") + """) AS e(bid)
                JOIN import_batches b ON b.id = e.bid
           WHERE t.removed = 0 AND t.account_id = ANY(%s)
             AND t.date BETWEEN %s - INTERVAL '3 days'
                             AND %s + INTERVAL '3 days'
             AND e.bid <> %s AND b.created_at > %s""",
        (scope["accts"], scope["lo"], scope["hi"], batch_id,
         created["created_at"])).fetchall()
    if not later:
        return None
    n = len(later)
    return (f"{n} later import{'s' if n > 1 else ''} covered an overlapping "
            "date window on the same account(s) and may have skipped rows as "
            "duplicates of this batch; those rows are NOT restored by this "
            "rollback — re-import the later file(s) to recover them.")


def rollback(conn, batch_id: str) -> RollbackResult:
    """Remove this batch's ownership: strip the id from each row's _batches
    array, hard-DELETE only rows left with no owning batch (these are OUR
    import copies, not aggregator rows; files can always be re-imported),
    and drop the batch record. Returns rows removed (int-compatible), plus
    a .warning when a later overlapping import may have deduped against
    this batch's rows."""
    member = json.dumps([batch_id])
    warning = _overlap_warning(conn, batch_id)
    n = conn.execute(
        """DELETE FROM transactions
           WHERE (raw->'_batches' @> %s::jsonb
                  AND jsonb_array_length(""" +
        _as_array("raw->'_batches'") + """) = 1)
              OR (raw->>'_batch' = %s AND NOT raw ? '_batches')""",
        (member, batch_id)).rowcount
    # shared rows survive: drop this id, repoint _batch at the newest left
    conn.execute(
        """UPDATE transactions
           SET raw = jsonb_set(raw, '{_batches}',
                               (raw->'_batches') - %s::text)
                     || jsonb_build_object(
                            '_batch', ((raw->'_batches') - %s::text) -> -1)
           WHERE raw->'_batches' @> %s::jsonb""",
        (batch_id, batch_id, member))
    conn.execute("DELETE FROM import_batches WHERE id=%s", (batch_id,))
    return RollbackResult(n, warning)
