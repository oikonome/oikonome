"""Re-importing a file after the importer's reading of it changed.

A file row's id is a hash of account|date|signed cents|masked name plus
its occurrence among identical rows (base.FileImportIds). When a rule
changes how a line is read — the sign a Debit/Credit column gives, the
year a statement's January line falls in, the sign a statement line's
wording or running balance gives — re-importing the same file computes a
new id for that line, and the upsert would add the corrected row beside
the one the earlier reading stored: the money counted twice, once each
way, or once in each year.

The repair here is deliberately narrow. The earlier id is recomputed
exactly (never guessed from content), the stored row must be proved to be
the same printed line, and the row is then RE-KEYED to the new id
(adopt._rewrite_txn_id: the row and everything pointing at it move, the
old id stops existing). Keeping the old id instead would leave it on a
corrected row while it is also the new-rule id of a real, different line
— a refund and a purchase of one amount on one day, or last year's
January line — and a later file holding that line would upsert onto the
corrected row and overwrite it.

Where an earlier re-import already added the corrected row beside the old
one, the old one is retired onto it (adopt._retire_twin), carrying every
note, receipt and override across.
"""

from __future__ import annotations

import json

from .base import STOOD_IN_KEY, mask_file_raw


def _cents(x) -> int | None:
    try:
        return round(float(x) * 100)
    except (TypeError, ValueError):
        return None


def _public(raw) -> dict | None:
    """raw without the keys the app adds after the import (batch tags,
    feed notes, enrichment markers — every one starts with '_'), in the
    shape JSONB gives back: a None key is "null", a tuple is a list."""
    if not isinstance(raw, dict):
        return None
    shaped = json.loads(json.dumps(raw, default=str))
    return {k: v for k, v in shaped.items() if not str(k).startswith("_")}


def same_line_raw(stored_raw, new_raw, name, merchant) -> bool:
    """True when a stored file row was read from the very cells `new_raw`
    holds. raw is the whole scrubbed source row, date and printed amount
    cells included, so equal raw is the same printed line; the stored raw
    was masked on the way in (and by the stored-row migrations), so the
    new one is masked the same way before they are compared. A stored raw
    of an older shape simply fails to match, which leaves it alone."""
    a = _public(stored_raw)
    b = _public(mask_file_raw(new_raw, name, merchant))
    return a is not None and b is not None and a == b


def batch_owners(raw) -> set[str]:
    """The import batches that own a stored row: its `_batches` array, or
    the single `_batch` tag rows carried before the array existed."""
    if not isinstance(raw, dict):
        return set()
    arr = raw.get("_batches")
    if isinstance(arr, list):
        return {str(b) for b in arr if isinstance(b, str) and b}
    one = raw.get("_batch")
    return {one} if isinstance(one, str) and one else set()


def stored(conn, account_id: str, ids) -> dict[str, dict]:
    """The stored rows on `account_id` among `ids`, by id."""
    ids = sorted({i for i in ids if i})
    if not ids:
        return {}
    return {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, date, amount, raw, removed, retired_at,"
        "       raw->>'line' AS line"
        "  FROM transactions WHERE account_id = %s AND id = ANY(%s)",
        (account_id, ids)).fetchall()}


def same_file_batches(conn, account_id: str, source: str,
                      unchanged_ids) -> set[str]:
    """The batches that imported this same file before: every `source`
    batch on `account_id` that owns ALL of `unchanged_ids` — the rows of
    this file whose id the reading change did not move, which are
    already stored. Another file (last year's statement) cannot own this
    one's unmoved rows, so ownership of them identifies the file without
    a fingerprint. No unmoved row means no proof, and the set is empty."""
    ids = sorted(set(unchanged_ids))
    if not ids:
        return set()
    rows = conn.execute(
        "SELECT raw FROM transactions WHERE account_id = %s AND id = ANY(%s)",
        (account_id, ids)).fetchall()
    if len(rows) != len(ids):
        return set()
    common: set[str] | None = None
    for r in rows:
        own = batch_owners(r["raw"])
        common = own if common is None else common & own
        if not common:
            return set()
    return {r["id"] for r in conn.execute(
        "SELECT id FROM import_batches"
        " WHERE id = ANY(%s) AND source = %s AND account_id = %s",
        (sorted(common), source, account_id)).fetchall()}


def retirable(row) -> bool:
    """A stored row an earlier re-import left beside its corrected twin
    may be retired onto it — unless it is already out of the ledger, or
    a bank feed relies on it (base.STOOD_IN_KEY), which a retirement
    would hide from that feed's reconciliation."""
    raw = row.get("raw") if row else None
    return (row is not None and not row.get("removed")
            and row.get("retired_at") is None
            and not (isinstance(raw, dict) and STOOD_IN_KEY in raw))


def land(conn, moves: list[tuple[str, str]],
         retires: list[tuple[str, str]]) -> int:
    """Carry out re-keys (old id -> new id) and twin retirements (keep,
    lose); the number done.

    A re-key lands only on a free id, and one row's new id can be the id
    another row of the same file is moving off (two same-day lines of one
    amount numbered differently by the two readings), so the moves are
    repeated until none makes progress. A move whose target never frees
    up is left undone, which is the state before this repair."""
    from .adopt import _retire_twin, _rewrite_txn_id
    done = 0
    pending = list(moves)
    while pending:
        left = [(o, n) for o, n in pending if not _rewrite_txn_id(conn, o, n)]
        done += len(pending) - len(left)
        if len(left) == len(pending):
            break
        pending = left
    for keep, lose in retires:
        _retire_twin(conn, keep, lose)
        done += 1
    return done


def absorbing_row(conn, account_id: str, prefix: str, date, amount,
                  names, used: set[str]) -> str | None:
    """The live row of another source that covers a file line the
    cross-source dedup absorbed: same account, same signed cents, within
    the dedup window, preferred in the order the dedup prefers (nearest
    day first, a shared name token before a same-day amount-only match,
    and never two names that conflict outright). `used` holds rows
    already given another line in this pass; None when nothing fits."""
    from .dedup import WINDOW_DAYS, _distinct_names, _tokens
    cents = _cents(amount)
    if cents is None:
        return None
    rows = conn.execute(
        "SELECT id, date, amount, name, merchant_name FROM transactions"
        " WHERE account_id = %s AND removed = 0 AND id NOT LIKE %s"
        "   AND date BETWEEN %s::date - %s AND %s::date + %s"
        "   AND abs(amount - %s) < 0.005",
        (account_id, prefix + ":%", date, WINDOW_DAYS, date, WINDOW_DAYS,
         float(amount))).fetchall()
    mine = _tokens(*names)
    best = None
    for r in rows:
        if r["id"] in used or _cents(r["amount"]) != cents:
            continue
        delta = abs((r["date"] - date).days)
        theirs = _tokens(r["name"], r["merchant_name"])
        overlap = bool(mine & theirs)
        if not overlap and (delta or _distinct_names(mine, theirs)):
            continue
        key = (delta, 0 if overlap else 1, r["id"])
        if best is None or key < best:
            best = key
    return best[2] if best else None


def retire_absorbed(conn, account_id: str, prefix: str,
                    absorbed: list[tuple], *, taken=()) -> int:
    """Retire the row an earlier reading stored for each file line the
    cross-source dedup now absorbs; the number retired.

    A line read differently now (a flipped sign, a corrected year) can
    match a bank feed's row that the earlier reading missed, and the dedup
    then skips it before any re-key sees it: the earlier file row would
    stay beside the feed row, counting the money twice. Each entry is
    `(date, amount, names, cands, accept)`: the line as read now, its
    earlier-reading ids as `(id, amount)` pairs numbered exactly as that
    import numbered them, and `accept(stored_row)`, which proves the
    stored row is this printed line (and anything else the caller's
    reading requires). The first candidate that stands on this account
    at its reading's amount, is accepted and may be retired
    (`retirable`) goes onto the row that absorbed the line, carrying
    every note, receipt and override across. An id in `taken` (a row of
    this file stands under it now) is never retired."""
    taken = set(taken)
    ids = [c for *_, cands, _ in absorbed for c, _ in cands if c not in taken]
    if not ids:
        return 0
    have = stored(conn, account_id, ids)
    used: set[str] = set()
    retires: list[tuple[str, str]] = []
    for date, amount, names, cands, accept in absorbed:
        for c, amt in cands:
            r = have.get(c)
            if (r is None or c in used or c in taken or not retirable(r)
                    or _cents(r["amount"]) != _cents(amt) or not accept(r)):
                continue
            keep = absorbing_row(conn, account_id, prefix, date, amount,
                                 names, used)
            if keep is None:
                break
            used |= {c, keep}
            retires.append((keep, c))
            break
    return land(conn, [], retires)


def adopt_rule_change(conn, account_id: str, txns: list, old_of: list,
                      *, same_line) -> int:
    """Move each row an earlier import stored under an OLD rule's id onto
    the id the current rule gives it; the number of rows corrected.

    `old_of[i]` lists row i's id(s) under the old rule — the hashed id and
    its pre-masking twin, numbered by their own FileImportIds counter over
    every row as that import numbered them — empty when the rule gives
    row i the same amount. An entry may be an `(id, amount)` pair, and
    then the stored row's amount must also be the old rule's amount.
    `same_line(stored_raw, txn)` proves the stored row was read from the
    same printed line; for a file whose raw is its source row, pass
    same_line_raw itself and it is applied to the txn's raw, name and
    merchant.

    A row is re-keyed only when its old id stands on this account, is the
    same printed line, is claimed by no other row, and the new id is not
    already this row's own. The old id may be another row's NEW id only
    when the stored row is provably this row's line and not that one's.
    When the corrected row already stands beside the old one (an earlier
    re-import added it), the old one is retired onto it.

    `txns` is not changed: its rows already carry their new ids. Call this
    after ids.resolve and before batches.tag, so the tag reads the moved
    row's batches."""
    if same_line is same_line_raw:
        # passed bare: a file whose raw is its source row
        def same_line(raw, t):
            return same_line_raw(raw, t.raw, t.name, t.merchant_name)
    cands: list[list[tuple[str, int | None]]] = []
    for olds in old_of:
        row = []
        for o in olds or ():
            oid, amt = o if isinstance(o, tuple) else (o, None)
            if oid:
                row.append((oid, None if amt is None else _cents(amt)))
        cands.append(row)
    if not any(cands):
        return 0
    have = stored(conn, account_id,
                  [c for row in cands for c, _ in row]
                  + [t.id for t in txns])
    new_of = {}
    for i, t in enumerate(txns):
        new_of.setdefault(t.id, i)
    claimed: set[str] = set()
    moves: list[tuple[str, str]] = []
    retires: list[tuple[str, str]] = []
    for i, t in enumerate(txns):
        for oid, amt in cands[i] if i < len(cands) else ():
            r = have.get(oid)
            if r is None or oid == t.id or oid in claimed:
                continue
            if amt is not None and _cents(r["amount"]) != amt:
                continue
            if not same_line(r["raw"], t):
                continue
            j = new_of.get(oid)
            if j is not None and j != i and same_line(r["raw"], txns[j]):
                continue                   # that row's own line: not ours
            here = have.get(t.id)
            if here is not None and same_line(here["raw"], t):
                if retirable(r) and t.id not in claimed:
                    retires.append((t.id, oid))
                    claimed.add(oid)
                break
            moves.append((oid, t.id))
            claimed.add(oid)
            break
    return land(conn, moves, retires)
