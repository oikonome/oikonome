"""Which receipts are the same slip: a stored fact, decided in one place.

Every receipt is in exactly one slip. `receipts.slip_id` is the slip's
label (the id of a receipt that was in it; NULL = a slip of its own), and
two receipts are the same slip for exactly two reasons, each written down
when it happens:

1. a person's word ("Same receipt", "Two purchases") is final, both ways;
2. the same image (equal bytes, same kind) is one slip, wherever each
   copy sits, unless a person said otherwise.

Nothing else joins two receipts. Two receipts READ alike (store, printed
day, total) but not the same bytes are *suspected* (`suspected`): both
count, and the person is asked on every panel that shows either copy and
on the Receipts page lines. A reading never joins by itself: every rule
that tried (on placement, on timing, on the one charge a stranded copy
could have become) hid a real purchase or depended on what the bank had
sent so far. See specs/receipts.md, "A receipt is one slip".

Neither rule reads where a receipt sits or what it was read as, so a
receipt is decided once, when it is stored; moving it, retiring its row
or reading it again changes nothing here. The one automatic case that
reads a look-alike is the nightly release's hold-back
(`receipt_match._unplaced_rows`): judged fresh from the current rows each
night, never stored.
"""

from __future__ import annotations

import logging

log = logging.getLogger("oikonome.receipts")

IMAGE, PERSON = "image", "person"

# the reading two photos are compared by; idx_receipts_reading (migration
# 232) is built on the same expressions
_STORE = "lower(btrim(coalesce({r}.parsed->>'merchant', '')))"
_DAY = "({r}.parsed->>'date')"
_TOTAL = ("(CASE WHEN jsonb_typeof({r}.parsed->'total') = 'number'"
          " THEN round(({r}.parsed->>'total')::numeric, 2) END)")


def read(r: str) -> str:
    """SQL over the receipts alias `r`: it was read as a store, an ISO
    printed day and a total (the reading two receipts are compared by)."""
    return (f"({r}.kind = 'receipt' AND {r}.status = 'parsed'"
            f" AND {r}.parsed ? 'total'"
            f" AND {_STORE.format(r=r)} <> ''"
            f" AND {_TOTAL.format(r=r)} IS NOT NULL"
            f" AND {_DAY.format(r=r)} ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$')")


def read_alike(a: str, b: str) -> str:
    """SQL: the receipts `a` and `b` were read as the same store, day and
    total (both read; `read`)."""
    return (f"({read(a)} AND {read(b)}"
            f" AND ({_STORE.format(r=b)}, {_DAY.format(r=b)},"
            f" {_TOTAL.format(r=b)}) = ({_STORE.format(r=a)},"
            f" {_DAY.format(r=a)}, {_TOTAL.format(r=a)}))")


def refused(a: str, b: str) -> str:
    """SQL: a person said some receipt of `a`'s slip is another purchase
    than some receipt of `b`'s (both receipts aliases). The answer is about
    the slips, so it holds for every copy of either."""
    ka = f"COALESCE({a}.slip_id, {a}.id)"
    kb = f"COALESCE({b}.slip_id, {b}.id)"
    one = ("EXISTS (SELECT 1 FROM receipts rm"
           " CROSS JOIN LATERAL unnest(rm.not_same_slip) AS ru(oid)"
           " JOIN receipts ro ON ro.id = ru.oid"
           " WHERE COALESCE(rm.slip_id, rm.id) = {x}"
           " AND COALESCE(ro.slip_id, ro.id) = {y})")
    return f"({one.format(x=ka, y=kb)} OR {one.format(x=kb, y=ka)})"


_MEMBER = """
    SELECT r.id::text AS id, r.kind, r.image_sha256 AS sha, r.created_at,
           r.slip_id::text AS slip_id, r.slip_source,
           r.not_same_slip::text[] AS not_same
      FROM receipts r"""


def _lock(conn) -> None:
    from ..sync.reanchor import hold_pairing_lock
    hold_pairing_lock(conn)


def _key(m: dict) -> str:
    return m["slip_id"] or m["id"]


def _load(conn, ids) -> list[dict]:
    if not ids:
        return []
    return [dict(r) for r in conn.execute(
        _MEMBER + " WHERE r.id = ANY(%s::uuid[])", (list(ids),)).fetchall()]


def _members(conn, key: str) -> list[dict]:
    """Every receipt of the slip labelled `key`."""
    return [dict(r) for r in conn.execute(
        _MEMBER + " WHERE r.slip_id = %s::uuid"
        " OR (r.slip_id IS NULL AND r.id = %s::uuid)"
        " ORDER BY r.created_at, r.id", (key, key)).fetchall()]


def _refused(a: list[dict], b: list[dict]) -> bool:
    """Did a person say some receipt of one slip is another purchase than
    some receipt of the other?"""
    ida, idb = {m["id"] for m in a}, {m["id"] for m in b}
    return (any(set(m["not_same"] or ()) & idb for m in a)
            or any(set(m["not_same"] or ()) & ida for m in b))


def _merge(conn, x: dict, c: dict, why: str) -> bool:
    """Join the slips of `x` and `c` because of `why`. The slip whose
    earliest receipt is earlier keeps its label; the other's receipts take
    it, each keeping the reason it was already in a slip for, the rest
    taking `why`. False when they are one slip already or a person said
    they are not."""
    kx, kc = _key(x), _key(c)
    if kx == kc:
        return False
    a, b = _members(conn, kx), _members(conn, kc)
    if not a or not b or _refused(a, b):
        return False
    first = lambda ms: min((m["created_at"], m["id"]) for m in ms)  # noqa: E731
    target, moving = (kx, b) if first(a) <= first(b) else (kc, a)
    conn.execute(
        """UPDATE receipts SET slip_id = %s::uuid,
                  slip_source = COALESCE(slip_source, %s),
                  slip_decided_at = now()
            WHERE id = ANY(%s::uuid[])""",
        (target, why, [m["id"] for m in moving]))
    return True


def _leave(conn, m: dict, members: list[dict]) -> None:
    """Take `m` out of its slip; the rest stay one slip."""
    rest = [o for o in members if o["id"] != m["id"]]
    if _key(m) == m["id"] and rest:
        # its id is the label: the rest take the earliest one's id
        conn.execute(
            "UPDATE receipts SET slip_id = %s::uuid WHERE id = ANY(%s::uuid[])",
            (rest[0]["id"], [o["id"] for o in rest]))
    conn.execute(
        "UPDATE receipts SET slip_id = NULL, slip_source = NULL,"
        " slip_decided_at = now() WHERE id = %s::uuid", (m["id"],))


def decide(conn, receipt_id: str) -> str | None:
    """Decide which slip `receipt_id` is, under the pairing lock, and
    return its label: it joins the slip of every other receipt with the
    same image that no person has called another purchase. Idempotent: a
    second call changes nothing. Never writes where a receipt is
    (`txn_id`, `match_method`), and never overrides a person's answer."""
    with conn.transaction():
        _lock(conn)
        got = _load(conn, [receipt_id])
        if not got:
            return None
        x = got[0]
        if x["sha"] is not None:
            # one read of a finite set: no loop here runs unbounded
            for c in [dict(r) for r in conn.execute(
                    _MEMBER + " WHERE r.image_sha256 = %s AND r.kind = %s"
                    " AND r.id <> %s::uuid ORDER BY r.created_at, r.id",
                    (x["sha"], x["kind"], x["id"])).fetchall()]:
                if _merge(conn, x, c, IMAGE):
                    x = _load(conn, [receipt_id])[0]
        conn.execute("UPDATE receipts SET slip_decided_at = now()"
                     " WHERE id = %s::uuid", (receipt_id,))
        return _key(x)


def decide_quietly(conn, receipt_id) -> None:
    """`decide` for a door that must not fail because of it: a receipt it
    could not decide stays undecided and the next pass decides it."""
    if receipt_id is None:
        return
    try:
        decide(conn, str(receipt_id))
    except Exception:                                  # noqa: BLE001
        log.warning("receipt %s left for the next slip pass", receipt_id,
                    exc_info=True)


# A receipt never decided: stored before the slip columns, or inserted by a
# writer that does not decide (an older build, a writer that copies a row
# whole, a restore of an archive without the columns). Nothing un-decides a
# receipt: neither rule reads where it is or what it reads as. Over `u`.
UNDECIDED = "u.slip_decided_at IS NULL"


def has_pending(conn) -> bool:
    return conn.execute(
        f"SELECT 1 FROM receipts u WHERE {UNDECIDED} LIMIT 1"
    ).fetchone() is not None


def decide_pending(conn, limit: int = 500) -> int:
    """Decide up to `limit` undecided receipts, oldest first. Cheap when
    there are none. Returns how many were decided."""
    rows = conn.execute(
        f"""SELECT u.id::text AS id,
                   EXISTS (SELECT 1 FROM receipts o
                            WHERE o.id <> u.id AND o.kind = u.kind
                              AND o.image_sha256 = u.image_sha256) AS paired
              FROM receipts u WHERE {UNDECIDED}
             ORDER BY u.created_at, u.id LIMIT %s""", (limit,)).fetchall()
    # a receipt no other has the image of is a slip of its own: decided in
    # one statement, so a backlog costs the same whatever its size
    alone = [r["id"] for r in rows if not r["paired"]]
    if alone:
        conn.execute("UPDATE receipts SET slip_decided_at = now()"
                     " WHERE id = ANY(%s::uuid[])", (alone,))
    n = len(alone)
    for rid in (r["id"] for r in rows if r["paired"]):
        try:
            decide(conn, rid)
            n += 1
        except Exception:                              # noqa: BLE001
            log.warning("receipt %s could not be decided", rid,
                        exc_info=True)
    return n


# how many `decide_pending` passes one call of `decide_backlog` makes at
# most: a household's whole backlog after the migration in one nightly,
# and a bound however many receipts cannot be decided
BACKLOG_PASSES = 40
# a restore decides every receipt it landed: a bound sized for the largest
# household, the rest left to the next matcher pass and nightly
RESTORE_PASSES = 400


def decide_backlog(conn, passes: int = BACKLOG_PASSES) -> int:
    """`decide_pending` until nothing is left undecided, at most `passes`
    times. Runs outside every lock of its caller: each decision takes the
    pairing lock for itself. Returns how many were decided."""
    n = 0
    for _ in range(passes):
        got = decide_pending(conn)
        n += got
        if not got:
            break
    return n


# ---- a person's answer -------------------------------------------------------

class NotAPair(ValueError):
    """The two receipts cannot be compared (one is gone, they are the same
    receipt, or they are not the same kind)."""


def _pair(conn, a: str, b: str) -> tuple[dict, dict]:
    if a == b:
        raise NotAPair("a receipt is always its own slip")
    got = {m["id"]: m for m in _load(conn, [a, b])}
    if a not in got or b not in got:
        raise LookupError("no such receipt")
    if got[a]["kind"] != got[b]["kind"]:
        raise NotAPair("a receipt and a check are never one slip")
    # an answer is about two receipts the person is asked about: one slip
    # already (its copies), the same file, or read alike (a refusal does
    # not unmake that: "Same receipt" withdraws it). Any other two are two
    # purchases nobody asked about, and joining them would hide one.
    if not conn.execute(
            f"""SELECT 1 FROM receipts x, receipts y
                 WHERE x.id = %s::uuid AND y.id = %s::uuid
                   AND (COALESCE(x.slip_id, x.id) = COALESCE(y.slip_id, y.id)
                        OR x.image_sha256 = y.image_sha256
                        OR {read_alike("x", "y")})""", (a, b)).fetchone():
        raise NotAPair("these two receipts are not read alike: only a "
                       "receipt that reads the same store, day and total "
                       "is asked about")
    return got[a], got[b]


def same_slip(conn, a: str, b: str, *, request: bool = True) -> str:
    """A person says the two are the same receipt: their slips become one,
    and every receipt in it is held there by the person's word. An earlier
    "Two purchases" between the two slips is withdrawn by it."""
    from ..sync.reanchor import hold_pairing_lock_for_request
    with conn.transaction():
        (hold_pairing_lock_for_request if request else _lock)(conn)
        x, y = _pair(conn, a, b)
        ids = [m["id"] for m in _members(conn, _key(x))
               + _members(conn, _key(y))]
        conn.execute(
            """UPDATE receipts SET not_same_slip = ARRAY(
                   SELECT u FROM unnest(not_same_slip) u
                    WHERE u <> ALL(%s::uuid[]))
                WHERE id = ANY(%s::uuid[])""", (ids, ids))
        _merge(conn, x, y, PERSON)
        x = _load(conn, [a])[0]
        conn.execute(
            "UPDATE receipts SET slip_source = %s, slip_decided_at = now()"
            " WHERE id = ANY(%s::uuid[])",
            (PERSON, [m["id"] for m in _members(conn, _key(x))]))
        return _key(x)


def two_purchases(conn, a: str, b: str, *, request: bool = True
                  ) -> list[str]:
    """A person says the two are different purchases: `a` leaves the slip
    it shares with `b`. The answer is about the slips, not the two copies
    tapped: every receipt of `a`'s slip remembers every receipt of `b`'s
    and the other way round, so no rule ever joins them again and the
    person is not asked about another copy of the same slip. Either of the
    two left on a retired row goes back to waiting in the same step
    (`receipt_match.release_refused`), so the answer never leaves a
    purchase counting nothing with the question gone. Returns the
    receipts released."""
    from ..sync.reanchor import hold_pairing_lock_for_request
    with conn.transaction():
        (hold_pairing_lock_for_request if request else _lock)(conn)
        x, y = _pair(conn, a, b)
        # both sides are read before anything is relabelled: leaving can
        # move the label off `a`, and a list read after would name `a` on
        # both sides and `b` on neither
        if _key(x) == _key(y):
            together = _members(conn, _key(x))
            mine = [a]
            theirs = [m["id"] for m in together if m["id"] != a]
            _leave(conn, x, together)
        else:
            mine = [m["id"] for m in _members(conn, _key(x))]
            theirs = [m["id"] for m in _members(conn, _key(y))]
        for one, other in ((mine, theirs), (theirs, mine)):
            conn.execute(
                """UPDATE receipts SET slip_decided_at = now(),
                          not_same_slip = ARRAY(
                              SELECT DISTINCT u FROM unnest(
                                  not_same_slip || %s::uuid[]) u)
                    WHERE id = ANY(%s::uuid[])""", (other, one))
        from . import receipt_match
        return receipt_match.release_refused(conn, [a, b])


# ---- reading it back ---------------------------------------------------------

def slips(conn, receipt_ids, *, tag: str | None = None) -> dict[str, dict]:
    """For each receipt id: its slip's label (`key`), the receipt counted
    for the slip (`counted`), whether this one `counts`, and if not `why`
    ('same_slip': another copy is counted; 'off_ledger': the counted copy
    is on a retired row), plus every member of the slip (`members`: id,
    txn_id, place 'live' | 'waiting' | 'retired', counted).

    One rank picks the counted copy: on the ledger or waiting before a
    retired row; then one with lines; then, for a tag report, one carrying
    `tag`; then a live charge before waiting; then the earliest. Reads only
    the slips of the receipts asked about."""
    ids = list(dict.fromkeys(str(i) for i in receipt_ids))
    if not ids:
        return {}
    rows = conn.execute(
        """WITH keys AS (
               SELECT DISTINCT COALESCE(slip_id, id) AS k FROM receipts
                WHERE id = ANY(%(ids)s::uuid[])),
           m AS (
               SELECT r.id, COALESCE(r.slip_id, r.id) AS k, r.txn_id,
                      r.created_at,
                      CASE WHEN r.txn_id IS NULL THEN 1
                           WHEN t.removed = 0 THEN 0 ELSE 2 END AS place,
                      EXISTS (SELECT 1 FROM receipt_items x
                               WHERE x.receipt_id = r.id) AS has_lines,
                      EXISTS (SELECT 1 FROM receipt_items x
                               WHERE x.receipt_id = r.id
                                 AND x.tag = %(tag)s::text) AS tagged
                 FROM receipts r
                 LEFT JOIN transactions t ON t.id = r.txn_id
                WHERE r.slip_id IN (SELECT k FROM keys)
                   OR (r.slip_id IS NULL AND r.id IN (SELECT k FROM keys)))
           SELECT id::text AS id, k::text AS k, txn_id, place,
                  first_value(id::text) OVER (
                      PARTITION BY k
                      ORDER BY place = 2, NOT has_lines, NOT tagged,
                               place, created_at, id) AS counted
             FROM m ORDER BY created_at, id""",
        {"ids": ids, "tag": tag}).fetchall()
    places = {0: "live", 1: "waiting", 2: "retired"}
    by_key: dict[str, list[dict]] = {}
    info: dict[str, dict] = {}
    for r in rows:
        counted = r["id"] == r["counted"]
        member = {"id": r["id"], "txn_id": r["txn_id"],
                  "place": places[r["place"]],
                  "counted": counted and r["place"] != 2}
        by_key.setdefault(r["k"], []).append(member)
        why = (None if member["counted"]
               else "off_ledger" if counted else "same_slip")
        info[r["id"]] = {"key": r["k"], "counted": r["counted"],
                         "counts": member["counted"], "why": why}
    for v in info.values():
        v["members"] = by_key[v["key"]]
    return {i: info[i] for i in ids if i in info}


def suspected(conn, receipt_ids) -> dict[str, list[str]]:
    """For each receipt id, the receipts read exactly alike (store, day,
    total) that are NOT its slip and that no person has called another
    purchase: the ones the person is asked about. Display only; counting
    never reads it."""
    ids = list(dict.fromkeys(str(i) for i in receipt_ids))
    if not ids:
        return {}
    out: dict[str, list[str]] = {}
    # Each side is read once and keyed by its slip; a person's refusals
    # are read once as slip-to-slip pairs, and a look-alike pair is
    # dropped by an anti-join on them. Linear in the receipts read alike,
    # whatever the household holds.
    for r in conn.execute(
            f"""WITH a AS MATERIALIZED (
                    SELECT r.id, COALESCE(r.slip_id, r.id) AS k,
                           {_STORE.format(r="r")} AS store, {_DAY.format(r="r")} AS day,
                           {_TOTAL.format(r="r")} AS total
                      FROM receipts r
                     WHERE r.id = ANY(%s::uuid[]) AND r.kind = 'receipt'
                       AND r.parsed ? 'total' AND r.status = 'parsed'),
                 ka AS MATERIALIZED (
                    SELECT * FROM a
                     WHERE store <> '' AND total IS NOT NULL
                       AND day ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$'),
                 b AS MATERIALIZED (
                    SELECT r.id, COALESCE(r.slip_id, r.id) AS k,
                           r.created_at, {_STORE.format(r="r")} AS store,
                           {_DAY.format(r="r")} AS day, {_TOTAL.format(r="r")} AS total
                      FROM receipts r
                     WHERE r.kind = 'receipt' AND r.parsed ? 'total'
                       AND r.status = 'parsed'
                       AND ({_STORE.format(r="r")}, {_DAY.format(r="r")}, {_TOTAL.format(r="r")}) IN (
                           SELECT store, day, total FROM ka)),
                 refused AS MATERIALIZED (
                    SELECT DISTINCT COALESCE(m.slip_id, m.id) AS x,
                           COALESCE(o.slip_id, o.id) AS y
                      FROM receipts m
                     CROSS JOIN LATERAL unnest(m.not_same_slip) AS u(oid)
                      JOIN receipts o ON o.id = u.oid
                     WHERE cardinality(m.not_same_slip) > 0)
                SELECT ka.id::text AS a, b.id::text AS b
                  FROM ka JOIN b
                    ON (b.store, b.day, b.total) = (ka.store, ka.day, ka.total)
                   AND b.k <> ka.k
                 -- a person's "two purchases" is about the slips: no
                 -- receipt of either slip asks about the other
                 WHERE NOT EXISTS (
                       SELECT 1 FROM refused f
                        WHERE (f.x = ka.k AND f.y = b.k)
                           OR (f.x = b.k AND f.y = ka.k))
                 ORDER BY b.created_at, b.id""", (ids,)).fetchall():
        out.setdefault(r["a"], []).append(r["b"])
    return out


def twins(conn, receipt_ids) -> dict[str, dict]:
    """What the panel and the waiting list print about each receipt's
    slip: `slip_twins` (the other copies of its slip) and `suspected`
    (receipts read alike that are not), each as a ReceiptTwin — where it
    is (the charge's date, payee and amount, or what the waiting copy
    read), and whether it is the copy counted. Plus this receipt's own
    `counts`, `why` and `counted` (the counted copy's id)."""
    from .merchant_dedup import DISPLAY_MERCHANT, MC_JOIN
    ids = list(dict.fromkeys(str(i) for i in receipt_ids))
    if not ids:
        return {}
    sus = suspected(conn, ids)
    sl = slips(conn, ids + [b for v in sus.values() for b in v])
    want = {m["id"] for v in sl.values() for m in v["members"]}
    want |= {b for v in sus.values() for b in v}
    counted_of = {m["id"]: m["counted"]
                  for v in sl.values() for m in v["members"]}
    where: dict[str, dict] = {}
    if want:
        for r in conn.execute(
                f"""SELECT r.id::text AS id, r.txn_id, t.date, t.amount,
                           t.removed, {DISPLAY_MERCHANT} AS payee,
                           r.parsed->>'merchant' AS store,
                           r.parsed->>'date' AS read_day,
                           {_TOTAL.format(r="r")} AS read_total
                      FROM receipts r
                      LEFT JOIN transactions t ON t.id = r.txn_id
                      {MC_JOIN}
                     WHERE r.id = ANY(%s::uuid[])""",
                (sorted(want),)).fetchall():
            on = r["txn_id"] is not None
            where[r["id"]] = {
                "receipt_id": r["id"], "txn_id": r["txn_id"],
                "date": (r["date"].isoformat() if on and r["date"]
                         else r["read_day"]),
                "payee": r["payee"] if on else r["store"],
                "amount": (round(float(r["amount"]), 2)
                           if on and r["amount"] is not None
                           else (float(r["read_total"])
                                 if r["read_total"] is not None else None)),
                "waiting": not on,
                "off_ledger": on and (r["removed"] or 0) != 0,
            }
    out: dict[str, dict] = {}
    for i in ids:
        s = sl.get(i)
        if s is None:
            continue
        out[i] = {
            "counts": s["counts"], "why": s["why"],
            "counted": s["counted"],
            "slip_twins": [{**where[m["id"]],
                            "counted": counted_of.get(m["id"], False)}
                           for m in s["members"]
                           if m["id"] != i and m["id"] in where],
            "suspected": _one_per_slip(
                [b for b in sus.get(i, []) if b in where], sl, where,
                counted_of),
        }
    return out


def _one_per_slip(ids, sl, where, counted_of) -> list[dict]:
    """The look-alikes to ask about, one per slip: the copy counted for
    it where one is, so the person is asked once about a slip, not once
    per photo of it."""
    best: dict[str, str] = {}
    for b in ids:
        k = sl[b]["key"] if b in sl else b
        if k not in best or (counted_of.get(b) and not counted_of.get(best[k])):
            best[k] = b
    return [{**where[b], "counted": counted_of.get(b, False)}
            for b in ids if best.get(sl[b]["key"] if b in sl else b) == b]


# ---- what a counted line says ------------------------------------------------

# The words the Receipts page and the expense report print beside a line
# (web and phone print them as sent; the server alone decides them).
LINE_SAME_SLIP = "not counted: counted on another copy of this receipt"
LINE_OFF_LEDGER = "not counted: its charge is no longer on the ledger"
LINE_DOUBLED = ("may be counted twice: another counted receipt reads the "
                "same store, day and total. Open either receipt and choose "
                "Same receipt or Two purchases")


def doubled(conn, slip: dict[str, dict], *, tag: str | None = None
            ) -> set[str]:
    """Of the receipts in `slip` (as `slips` returns them) that count, the
    ones another COUNTED slip reads exactly like (`suspected`): a total
    holding both may hold one purchase twice, until the person answers.
    For a tag report (`tag`) the other slip counts there only when its
    counted copy carries the tag."""
    counting = [i for i, v in slip.items() if v["counts"]]
    sus = suspected(conn, counting)
    if not sus:
        return set()
    others = slips(conn, {b for v in sus.values() for b in v}, tag=tag)
    counted = {m["id"] for v in others.values() for m in v["members"]
               if m["counted"]}
    if tag is not None and counted:
        counted = {r["id"] for r in conn.execute(
            "SELECT DISTINCT receipt_id::text AS id FROM receipt_items"
            " WHERE receipt_id = ANY(%s::uuid[]) AND tag = %s",
            (sorted(counted), tag)).fetchall()}
    return {a for a, bs in sus.items()
            if any(others.get(b, {}).get("counted") in counted for b in bs)}


def line_note(info: dict | None, is_doubled: bool) -> str | None:
    """The words beside one receipt's lines: why they are not counted, or
    that they may be counted twice; None when there is nothing to say."""
    if info is not None and not info["counts"]:
        return (LINE_OFF_LEDGER if info["why"] == "off_ledger"
                else LINE_SAME_SLIP)
    return LINE_DOUBLED if is_doubled else None


def group_note(n_doubled: int) -> str | None:
    """The words under a total that adds up lines of `n_doubled` receipts
    another counted receipt reads exactly like."""
    if not n_doubled:
        return None
    return (f"{n_doubled} receipt{'s' if n_doubled != 1 else ''} here "
            f"{'read' if n_doubled != 1 else 'reads'} like another counted "
            "receipt: this total may hold a purchase twice until you answer "
            "Same receipt or Two purchases on the receipt")
