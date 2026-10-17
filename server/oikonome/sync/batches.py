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
from ..web.data import _as_array, detach_pairs


def create(conn, source: str, account_id: str | None,
           filename: str | None) -> str:
    bid = uuid.uuid4().hex[:12]
    # an uploaded file's name is the browser's word; U+0000 would fail the
    # insert and the import with it
    if filename:
        filename = filename.replace("\x00", "")
    # banks name exports after the full account number, and this record
    # rides /export and the portability archive: keep the last four only
    from .base import mask_long_digits
    filename = mask_long_digits(filename)
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
    from .base import STOOD_IN_KEY
    ids = [t.id for t in txns]
    prior: dict[str, list] = {}
    stood_in: dict[str, dict] = {}
    if ids:
        for r in conn.execute(
                "SELECT id, raw->'_batches' AS batches, raw->>'_batch' AS latest,"
                " raw->%s AS stood_in"
                " FROM transactions WHERE id = ANY(%s)",
                (STOOD_IN_KEY, ids)).fetchall():
            arr = r["batches"]
            if not arr and r["latest"]:
                arr = [r["latest"]]            # pre-array single tag
            prior[r["id"]] = arr or []
            if isinstance(r["stood_in"], dict):
                stood_in[r["id"]] = r["stood_in"]
    for t in txns:
        merged = [b for b in prior.get(t.id, []) if b != batch_id] + [batch_id]
        t.raw = {**(t.raw or {}), "_batches": merged, "_batch": batch_id}
        # a bank feed relies on this row (see base.STOOD_IN_KEY); a
        # re-import rewrites raw wholesale and must not drop that note
        if t.id in stood_in:
            t.raw[STOOD_IN_KEY] = stood_in[t.id]
    return txns


def recent(conn, limit: int = 10) -> list:
    """The latest batches, each with `annotations`: what a person has since
    attached to this batch's rows — receipts, notes, reimbursement
    pairings, business tags, category overrides, hand splits (one each,
    however many parts). A rollback hard-deletes
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
                + CASE WHEN EXISTS (SELECT 1 FROM transaction_splits s
                                     WHERE s.txn_id = o.id) THEN 1 ELSE 0 END
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
    `create` would otherwise leave a phantom row (row_count 0, the column's
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
    directly) that also carries the overlap warning as `.warning`, the rows
    kept as `.kept`, and dict-style access (result["deleted"],
    result["warning"], result["kept"])."""

    warning: str | None
    kept: int
    kept_without_account: int

    def __new__(cls, deleted: int, warning: str | None = None,
                kept: int = 0, kept_without_account: int = 0):
        obj = super().__new__(cls, deleted)
        obj.warning = warning
        obj.kept = kept
        obj.kept_without_account = kept_without_account
        return obj

    def __getitem__(self, key):
        if key == "deleted":
            return int(self)
        if key == "warning":
            return self.warning
        if key == "kept":
            return self.kept
        if key == "kept_without_account":
            return self.kept_without_account
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


class ArchivedBusinessRows(RuntimeError):
    """The batch alone owns rows booked to an archived business. Nothing
    was changed; the caller answers 409."""

    def __init__(self, message: str = "this import holds transactions of "
                 "an archived business — restore the business first"):
        super().__init__(message)


class KeptRowsUnconfirmed(RuntimeError):
    """The undo would keep rows held only by a note naming no feed account,
    and the caller asked to be told first. Nothing was changed; the caller
    answers 409 with `.kept` and lets the owner retry with force_kept."""

    def __init__(self, kept: int):
        self.kept = kept
        rows = f"{kept} imported row{'s' if kept != 1 else ''}"
        super().__init__(
            f"{rows} in this import may stand in for charges an older bank "
            f"feed set aside, and the feed may never send those charges "
            f"again. Undo anyway to remove {'them' if kept != 1 else 'it'} "
            f"with the import.")


def kept_without_account_note(n: int) -> str:
    """What an undo says about the rows it kept only for a note naming no
    feed account."""
    return (f"{n} row{'s were' if n != 1 else ' was'} kept: an older bank "
            f"feed may rely on {'them' if n != 1 else 'it'} (it set its own "
            f"copy aside without noting which account, and may never send "
            f"{'those charges' if n != 1 else 'that charge'} again). The "
            f"import stays listed with "
            f"{'them' if n != 1 else 'it'}; undo it again and choose to "
            f"delete anyway to remove {'them' if n != 1 else 'it'}.")


def rollback(conn, batch_id: str, force_kept: bool = False,
             ask_first: bool = False) -> RollbackResult:
    """Remove this batch's ownership: strip the id from each row's _batches
    array, hard-DELETE only rows left with no owning batch (these are OUR
    import copies, not aggregator rows; files can always be re-imported),
    and drop the batch record. Returns rows removed (int-compatible), plus
    a .warning when a later overlapping import may have deduped against
    this batch's rows.

    A row a bank feed relies on (base.relied_on_by_feed: the feed set its
    own copy of the charge aside because this row represented it, and will
    not send it again) is not deleted. It stays as an ordinary ledger row,
    no longer owned by the batch, so the batch still reads as undone; the
    result counts it as .kept and the warning says why.

    A row kept only by a note that names no feed account (written by an
    upgrade or an archive, where the feed account was never recorded) is
    different: nothing can tell whether a feed still relies on it, so the
    owner decides. Without `force_kept` the row is kept and STAYS in the
    batch, which stays listed, so the choice can still be made later; the
    result counts these as .kept_without_account and the warning says why.
    `ask_first` refuses with KeptRowsUnconfirmed, changing nothing, when
    there are any; `force_kept` (the owner, told the count) deletes them.
    Rows a named feed account still relies on are kept either way.

    Refuses with base.SyncRunning, changing nothing, while a sync holds the
    household's lock: the sync filters its whole pull against the import
    rows it read at its start, and would drop feed rows as covered by rows
    this delete removed.

    Refuses with ArchivedBusinessRows, changing nothing, while any row the
    batch alone owns belongs to an archived business: an archived
    business's rows are retention records, and only the owner's
    delete-forever may destroy them. Checked under the lock and the
    transaction, so a business archived (or a row assigned to one) after a
    door's own look cannot slip through."""
    from .base import household_delete_lock
    with household_delete_lock(conn):
        return _rollback_locked(conn, batch_id, force_kept is True,
                                ask_first)


def _archived_business_rows(conn, ids: list[str]) -> set[str]:
    """The rows among `ids` booked to a business that is no longer active.
    An archived business's rows are retention records that only the
    owner's delete-forever may destroy. FOR SHARE on the business row
    orders this look against an archive committing at the same moment."""
    if not ids:
        return set()
    return {r["id"] for r in conn.execute(
        "SELECT t.id FROM transactions t "
        "LEFT JOIN accounts a ON a.id = t.account_id "
        "JOIN business_entity e "
        "  ON e.id = COALESCE(t.entity_id, a.entity_id) "
        "WHERE t.id = ANY(%s) AND e.status <> 'active' "
        "FOR SHARE OF e", (list(ids),)).fetchall()}


def _let_go(conn, ids: list[str]) -> None:
    """What has to happen before rows are hard-deleted: a pair on a row
    about to go is unlinked first, so its counterpart lets go of the
    transfer category the link gave it (see detach_pairs); a receipt
    paired with one goes back to waiting instead of down with the row.
    Callers hold the pairing lock."""
    from .base import release_matched_receipts
    detach_pairs(conn, ids)
    release_matched_receipts(conn, ids)


def annotated(conn, ids: list[str]) -> set[str]:
    """The rows among `ids` a person has put something on — the set
    recent() counts: receipts, notes, reimbursement pairings and flags,
    business tags and classes, category overrides, hand splits."""
    if not ids:
        return set()
    return {r["id"] for r in conn.execute(
        """SELECT t.id FROM transactions t
            WHERE t.id = ANY(%s) AND (
                  t.category_override IS NOT NULL
               OR EXISTS (SELECT 1 FROM receipts r WHERE r.txn_id = t.id)
               OR EXISTS (SELECT 1 FROM transaction_notes n
                           WHERE n.txn_id = t.id)
               OR EXISTS (SELECT 1 FROM reimbursements m
                           WHERE m.expense_id = t.id
                              OR m.reimburse_id = t.id)
               OR EXISTS (SELECT 1 FROM reimburse_flags f
                           WHERE f.txn_id = t.id)
               OR EXISTS (SELECT 1 FROM business_flags g
                           WHERE g.txn_id = t.id)
               OR EXISTS (SELECT 1 FROM business_txn_class c
                           WHERE c.txn_id = t.id)
               OR EXISTS (SELECT 1 FROM manual_categories k
                           WHERE k.transaction_id = t.id)
               OR EXISTS (SELECT 1 FROM transaction_splits s
                           WHERE s.txn_id = t.id))""",
        (list(ids),)).fetchall()}


def retire_rows(conn, ids: list[str]) -> dict:
    """Hard-delete file-import rows an earlier import put on the wrong
    account, which the person asked to have removed. The caller proved
    each id came from that import; this decides which may go.

    The guards are rollback's: under the household delete lock (a sync
    filters its pull against the import rows it read at its start), the
    pairing lock before any row is touched, pairs unlinked and matched
    receipts released first. A row a person has annotated is kept and
    counted — removing it would take their work with it; so is a row a
    bank feed relies on (base.relied_on_by_feed), and a row of an
    archived business, which only the owner's delete-forever destroys.

    Returns {removed, kept_annotated, kept_by_feed, kept_archived}."""
    from .base import household_delete_lock, relied_on_by_feed
    from .reanchor import hold_pairing_lock
    ids = [i for i in dict.fromkeys(ids) if i]
    out = {"removed": 0, "kept_annotated": 0, "kept_by_feed": 0,
           "kept_archived": 0}
    if not ids:
        return out
    with household_delete_lock(conn):
        with conn.transaction():
            hold_pairing_lock(conn)
            live = [r["id"] for r in conn.execute(
                "SELECT id FROM transactions WHERE id = ANY(%s)"
                " ORDER BY id FOR UPDATE", (ids,)).fetchall()]
            archived = _archived_business_rows(conn, live)
            rest = [i for i in live if i not in archived]
            fed = relied_on_by_feed(conn, rest)
            rest = [i for i in rest if i not in fed]
            noted = annotated(conn, rest)
            doomed = [i for i in rest if i not in noted]
            _let_go(conn, doomed)
            if doomed:
                out["removed"] = conn.execute(
                    "DELETE FROM transactions WHERE id = ANY(%s)",
                    (doomed,)).rowcount
            out.update(kept_annotated=len(noted), kept_by_feed=len(fed),
                       kept_archived=len(archived))
    return out


def _rollback_locked(conn, batch_id: str, force_kept: bool = False,
                     ask_first: bool = False) -> RollbackResult:
    member = json.dumps([batch_id])
    warning = _overlap_warning(conn, batch_id)
    doomed_sql = """(raw->'_batches' @> %s::jsonb
                  AND jsonb_array_length(""" + _as_array("raw->'_batches'") + """) = 1)
              OR (raw->>'_batch' = %s AND NOT raw ? '_batches')"""
    from .base import kept_rows_note, log_sync, relied_on_by_feed
    from .reanchor import hold_pairing_lock
    with conn.transaction():
        # under the pairing lock, taken before any row is touched: the
        # matcher cannot hang a receipt on a doomed row between the release
        # and the delete
        hold_pairing_lock(conn)
        doomed = [r["id"] for r in conn.execute(
            "SELECT id FROM transactions WHERE " + doomed_sql,
            (member, batch_id)).fetchall()]
        if doomed and _archived_business_rows(conn, doomed):
            raise ArchivedBusinessRows()
        kept = relied_on_by_feed(conn, doomed,
                                 release_accountless=force_kept)
        # the kept rows held only by a note naming no feed account: they
        # stay in the batch, so the owner can still undo them on purpose
        held = (kept - relied_on_by_feed(conn, list(kept),
                                         release_accountless=True)
                if kept else set())
        if held and ask_first:
            raise KeptRowsUnconfirmed(len(held))
        if kept:
            doomed = [d for d in doomed if d not in kept]
        _let_go(conn, doomed)
        n = conn.execute(
            "DELETE FROM transactions WHERE (" + doomed_sql
            + ") AND NOT id = ANY(%s)",
            (member, batch_id, list(kept))).rowcount
        named = kept - held
        if named:
            conn.execute(
                "UPDATE transactions SET raw = raw - '_batch' - '_batches'"
                " WHERE id = ANY(%s)", (list(named),))
    # shared rows survive: drop this id, repoint _batch at the newest left
    conn.execute(
        """UPDATE transactions
           SET raw = jsonb_set(raw, '{_batches}',
                               (raw->'_batches') - %s::text)
                     || jsonb_build_object(
                            '_batch', ((raw->'_batches') - %s::text) -> -1)
           WHERE raw->'_batches' @> %s::jsonb AND NOT id = ANY(%s)""",
        (batch_id, batch_id, member, list(held)))
    if held:
        conn.execute("UPDATE import_batches SET row_count = %s WHERE id=%s",
                     (len(held), batch_id))
    else:
        conn.execute("DELETE FROM import_batches WHERE id=%s", (batch_id,))
    for n_kept, note_for in ((len(named), kept_rows_note),
                             (len(held), kept_without_account_note)):
        if n_kept:
            note = note_for(n_kept)
            log_sync(conn, "import-undo", 0,
                     error=f"batch {batch_id}: {note}")
            warning = f"{warning} {note}" if warning else note
    return RollbackResult(n, warning, len(kept), len(held))
