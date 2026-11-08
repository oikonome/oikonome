"""Which live row a transaction id stands for now.

A list can show a pending hold after a sync has posted it: the posted
charge arrives under a new id and the hold is retired (removed = 1). A
person acting on what they were shown still names the hold, and every door
that takes a transaction id from a client has to send that action to the
posted row the hold became — with the hold's id it would land on a row
every read hides, or find nothing and fail.

The posted row is found by the link the feed gave it, never by amount (a
tip or a fuel pre-authorisation posts at a different total). That link is
believed only as far as the settlement itself believes it:

- the hold was a pending row and has been retired;
- the posted row is live, on the SAME account;
- the link is the `pending_transaction_id` column the settlement writes,
  or — for a row settled before the column was written, which carries the
  link only in what the feed sent — `raw->>'pending_transaction_id'`, but
  never from a file import, whose `raw` is the uploader's own text.

The column wins where both are present: a restore or an account adoption
re-keys the column with the ids, and leaves `raw` as the feed wrote it.

None of these take a lock; the callers do (data.live_edit_ids shares the
pairing lock, entities.live_txn_id takes it outright and row-locks)."""


def _file_import_patterns() -> list[str]:
    from ..sync.base import FILE_IMPORT_ID_PREFIXES
    return [p + "%" for p in FILE_IMPORT_ID_PREFIXES]


_SUCCESSORS = """
    SELECT DISTINCT ON (p.id) p.id AS hold, t.id
      FROM transactions p
      JOIN transactions t
        ON t.removed = 0 AND t.account_id = p.account_id AND t.id <> p.id
       AND (t.pending_transaction_id = p.id
            OR (t.pending_transaction_id IS NULL
                AND t.raw->>'pending_transaction_id' = p.id
                AND NOT t.id LIKE ANY(%s)))
     WHERE p.id = ANY(%s) AND p.removed = 1 AND p.pending = 1
     ORDER BY p.id, t.id"""


def successors(conn, holds: list[str]) -> dict[str, str]:
    """{hold id: the live posted row it settled into} for each of `holds`
    that is a retired hold with one; the others are left out."""
    holds = [h for h in dict.fromkeys(holds) if h]
    if not holds:
        return {}
    return {r["hold"]: r["id"] for r in conn.execute(
        _SUCCESSORS, (_file_import_patterns(), holds)).fetchall()}


def resolve(conn, ids: list[str]) -> dict[str, str]:
    """{id: the live row it stands for} — the row itself while it is on the
    ledger, else the posted row a settled hold became. Ids with neither
    are left out."""
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        return {}
    out = {r["id"]: r["id"] for r in conn.execute(
        "SELECT id FROM transactions WHERE id = ANY(%s) AND removed = 0",
        (ids,)).fetchall()}
    gone = [i for i in ids if i not in out]
    if gone:
        out.update(successors(conn, gone))
    return out
