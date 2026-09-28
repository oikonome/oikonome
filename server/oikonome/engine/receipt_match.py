"""Pair a receipt snapped BEFORE its charge with the charge when it lands.

A receipt can be photographed at the till, days before the card charge
reaches the ledger. Such a receipt is stored with no transaction — it is
WAITING — and parsed as usual. Whenever the ledger grows (every importer and
aggregator funnels through `sync.base.upsert_transactions`) and whenever a
waiting receipt's parse finishes, this module looks for the one charge the
receipt belongs to and attaches it.

What counts as the charge, in order:

* **Exact amount.** The charge's cents equal the receipt's total, it posted
  between AUTO_DAYS_BEFORE days before and AUTO_DAYS_AFTER days after the
  receipt's date, it is money out, on a counted account, and it carries no
  receipt already. One such charge is the match. Several are narrowed by the
  merchant: the one whose name matches the receipt's store wins. Still
  several — two identical charges at one store — is a TIE, and the receipt
  keeps waiting and offers them for a person to choose. A charge's
  descriptor often names the business differently from its receipt (a
  restaurant's legal name, a payment processor's prefix), so a lone
  exact-cents charge under another name still matches — but only once the
  window has closed, so a charge from the right store arriving a day later
  is not beaten to the receipt by a stranger that happened to cost the
  same.
* **Near amount.** A charge within the tolerance band (a tip added after
  the receipt printed, a foreign-currency conversion) matches only when its
  merchant also matches the receipt's, and only when exactly one does. It
  also beats a lone exact-cents charge under ANOTHER name: the store's own
  charge with a tip on top is better evidence than a stranger that happens
  to cost what the slip printed. And it waits for the window to close, like
  the stranger does — the store's exact charge can still arrive, and a
  daily habit (the same coffee every morning) always has yesterday's
  slightly different charge sitting in the band.

A person's word always wins: a hand match is never second-guessed — not
even when its charge later disappears from the ledger, when the receipt
goes back to the waiting list for the person rather than to the matcher —
an unmatch remembers the charge so the matcher never pairs the two again,
and every write here happens under the lock every transaction-pairing
writer takes, so a concurrent import cannot attach the same receipt twice
or two receipts to one charge.

Days are the household's own (`localtime.household_day`), not the server
clock's, so the window closes on the same day the waiting list says it
does.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import re

from .merchant_sql import DISPLAY_MERCHANT, MC_JOIN

log = logging.getLogger("oikonome.receipts")

# The window an automatic match looks in, around the receipt's own date.
# Card charges post after the purchase — usually one to three days, a
# weekend or a hotel hold can stretch it toward a week — and a receipt
# dated a day or two AFTER its posting is a receipt read in another time
# zone or a restaurant's end-of-day batch.
AUTO_DAYS_BEFORE = 2
AUTO_DAYS_AFTER = 7

# The wider window hand-match candidates are offered from.
OFFER_DAYS_BEFORE = 7
OFFER_DAYS_AFTER = 21

# A charge may exceed the receipt by a tip written on the slip after it
# printed (up to 25%, at least $1), or differ by a card network's currency
# conversion (3% either way, at least $0.50).
TIP_FRACTION = 0.25
TIP_FLOOR = 1.00
FX_FRACTION = 0.03
FX_FLOOR = 0.50

# A receipt still waiting this long after its date is flagged in the list as
# unlikely to match by itself. It is never deleted: a person decides.
STALE_DAYS = 30

# How long an automatic match stays in the "matched recently" list, where a
# person can see what the matcher did and undo it.
RECENT_DAYS = 14

# The most candidates the waiting list offers per receipt.
OFFER_LIMIT = 3

# The most charges read per receipt, and the most waiting receipts one list
# page or one matcher pass reads. A pass takes the NEWEST receipts first:
# a fresh snap is the one whose charge is arriving now, and a pile of old
# receipts nothing will ever match must not starve it.
PER_RECEIPT = 200
PAGE_LIMIT = 200
SWEEP_LIMIT = 1000

# A receipt total above this is a misread or a crafted value, not a
# purchase — the same ceiling every money door in the app applies. Past it
# the cents arithmetic overflows (1e307 * 100 is infinity).
MAX_TOTAL = 1e12

# The dates a receipt can plausibly carry. Outside them the window
# arithmetic (a year either side) runs off the calendar Python represents,
# and one such row would fail every pass over the whole list.
DATE_MIN = dt.date(1900, 1, 1)
DATE_MAX = dt.date(2999, 12, 31)

_WORD = re.compile(r"[^\W_]+")


# ---- receipt facts ----------------------------------------------------------

def _finite(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def _as_date(v) -> dt.date | None:
    if isinstance(v, dt.datetime):
        d = v.date()
    elif isinstance(v, dt.date):
        d = v
    elif isinstance(v, str):
        try:
            d = dt.date.fromisoformat(v.strip()[:10])
        except ValueError:
            return None
    else:
        return None
    return d if DATE_MIN <= d <= DATE_MAX else None


def facts(row: dict) -> dict:
    """The three things the matcher reads off a receipt: total, date, store.

    The date is the one printed on the receipt when it is a plausible one —
    not after the day it was snapped (a misread year), not more than a year
    before it. Otherwise the snap date stands in: a receipt photographed
    before its charge posts is almost always photographed the day it was
    printed."""
    parsed = row.get("parsed") if isinstance(row.get("parsed"), dict) else {}
    total = _finite(parsed.get("total"))
    snapped = _as_date(row.get("created_at"))
    printed = _as_date(parsed.get("date"))
    if printed is not None and snapped is not None and not (
            snapped - dt.timedelta(days=366) <= printed
            <= snapped + dt.timedelta(days=1)):
        printed = None
    merchant = parsed.get("merchant")
    merchant = str(merchant).strip() if merchant else ""
    return {"total": round(total, 2)
            if total is not None and 0 < total <= MAX_TOTAL else None,
            "date": printed or snapped,
            "merchant": merchant or None}


def band(total: float) -> tuple[float, float]:
    """The amounts a charge may carry and still be this receipt."""
    lo = total - max(FX_FLOOR, total * FX_FRACTION)
    hi = total + max(TIP_FLOOR, total * TIP_FRACTION)
    return round(lo, 2), round(hi, 2)


# ---- merchant evidence -------------------------------------------------------

def _tokens(name: str | None) -> list[str]:
    """The identifying words of a store name, through the same merchant
    canonicaliser the ledger uses (a processor prefix, a store number and a
    trailing city are not the store). Two-letter words and bare numbers
    carry no identity."""
    if not name:
        return []
    from .merchant_dedup import canonical_merchant
    label = canonical_merchant(name) or name
    return [w for w in _WORD.findall(label.casefold())
            if len(w) >= 3 and not w.isdigit()]


def merchant_match(receipt_name: str | None, names) -> str:
    """'same', 'different' or 'unknown': does a charge name the receipt's
    store?

    'same' when either name's words, run together, contain the other's
    ('CORNERDELI' against 'Corner Deli'), or when at least half the shorter
    name's words are shared. 'different' when both have words and share
    none; 'unknown' when either has nothing to compare."""
    ra = _tokens(receipt_name)
    if not ra:
        return "unknown"
    squash_a = "".join(ra)
    seen = False
    for n in names:
        tb = _tokens(n)
        if not tb:
            continue
        seen = True
        squash_b = "".join(tb)
        if (len(squash_a) >= 4 and len(squash_b) >= 4
                and (squash_a in squash_b or squash_b in squash_a)):
            return "same"
        shared = len(set(ra) & set(tb))
        if shared and shared * 2 >= min(len(set(ra)), len(set(tb))):
            return "same"
    return "different" if seen else "unknown"


# ---- candidates --------------------------------------------------------------

_MERCHANT_RANK = {"same": 0, "unknown": 1, "different": 2}


def _shadows(conn) -> list[str]:
    from . import links
    return list(links.shadow_ids(conn))


def candidates(conn, row: dict, *, before: int = OFFER_DAYS_BEFORE,
               after: int = OFFER_DAYS_AFTER) -> list[dict]:
    """Charges this receipt could be, best first.

    Money out on a counted account, live, not carrying a receipt already,
    not one a person took this receipt off, inside the amount band and the
    date window. Ranked: exact cents first, then the merchant evidence,
    then the days between the receipt and the posting, then how far the
    amount is off."""
    return candidates_for(conn, [row], before=before, after=after)[0]


def _safe_facts(row: dict) -> dict:
    try:
        return facts(row)
    except Exception:                                  # noqa: BLE001
        # facts() guards every value it reads; this is the backstop that
        # keeps one unreadable row from failing the list it sits in
        log.warning("receipt %s has unreadable facts", row.get("id"),
                    exc_info=True)
        return {"total": None, "date": None, "merchant": None}


def candidates_for(conn, rows: list[dict], *,
                   before: int = OFFER_DAYS_BEFORE,
                   after: int = OFFER_DAYS_AFTER) -> list[list[dict]]:
    """`candidates` for many receipts in ONE query, in the order given.

    The waiting list is re-read on every snap, match and poll, and a
    matcher pass runs on every sync; a query per receipt made both cost a
    round trip for every receipt waiting."""
    out: list[list[dict]] = [[] for _ in rows]
    fs = [_safe_facts(r) for r in rows]
    keys, los, his, froms, tos, skips = [], [], [], [], [], []
    for i, (row, f) in enumerate(zip(rows, fs)):
        if f["total"] is None or f["date"] is None:
            continue
        lo, hi = band(f["total"])
        keys.append(i)
        los.append(lo)
        his.append(hi)
        froms.append(f["date"] - dt.timedelta(days=before))
        tos.append(f["date"] + dt.timedelta(days=after))
        skips.append(json.dumps([str(x) for x in
                                 (row.get("unmatched_txn_ids") or [])]))
    if not keys:
        return out
    found = conn.execute(
        f"""SELECT q.k, c.*
              FROM unnest(%s::int[], %s::float8[], %s::float8[],
                          %s::date[], %s::date[], %s::text[])
                   AS q(k, lo, hi, dfrom, dto, skip)
             CROSS JOIN LATERAL (
                SELECT t.id, t.date, t.amount, t.name, t.pending,
                       COALESCE(t.merchant_name, t.name) AS mname,
                       {DISPLAY_MERCHANT} AS payee,
                       a.name AS account,
                       CASE WHEN q.skip <> '[]' THEN (
                          SELECT jsonb_agg(jsonb_build_object(
                                   'id', s.id, 'amount', s.amount,
                                   'name', COALESCE(s.merchant_name, s.name),
                                   'alts', (
                            SELECT COALESCE(jsonb_agg(jsonb_build_object(
                                     'amount', o.amount,
                                     'names', jsonb_build_array(
                                         COALESCE(o.merchant_name, o.name),
                                         o.name))), '[]')
                              FROM (SELECT o.* FROM transactions o
                                     WHERE o.account_id = s.account_id
                                       AND o.removed = 0 AND o.pending = 0
                                       AND o.id <> t.id AND o.id <> s.id
                                       AND o.date > s.date - 7
                                       AND o.date < s.date + 7
                                       AND abs(o.date - s.date)
                                           <= abs(t.date - s.date)
                                       AND {_in_band('o.amount', 's.amount')}
                                     LIMIT 20) o)))
                            FROM transactions s
                           WHERE s.account_id = t.account_id
                             AND s.date > t.date - 7
                             AND s.date < t.date + 7
                             AND s.id <> t.id
                             AND (s.pending <> 0 OR s.removed <> 0)
                             AND q.skip::jsonb ? s.id
                             AND {_in_band('t.amount', 's.amount')})
                       END AS refused_holds
                  FROM transactions t
                  {MC_JOIN}
                  LEFT JOIN accounts a ON a.id = t.account_id
                 WHERE t.removed = 0
                   AND t.amount >= q.lo AND t.amount <= q.hi
                   AND t.date >= q.dfrom AND t.date <= q.dto
                   AND NOT (t.account_id = ANY(%s))
                   AND NOT (q.skip::jsonb ? t.id)
                   AND NOT EXISTS (SELECT 1 FROM receipts x
                                    WHERE x.txn_id = t.id
                                      AND x.kind = 'receipt')
                 ORDER BY t.date, t.id
                 LIMIT %s) c""",
        (keys, los, his, froms, tos, skips, _shadows(conn), PER_RECEIPT)
    ).fetchall()
    for r in found:
        i = r["k"]
        f = fs[i]
        posted = _as_date(r["date"])
        if posted is None:
            continue
        out[i].append({
            "txn_id": r["id"], "date": posted, "amount": float(r["amount"]),
            "payee": r["payee"] or r["mname"] or r["name"],
            "account": r["account"], "pending": bool(r["pending"]),
            "exact": round(float(r["amount"]) * 100)
            == round(f["total"] * 100),
            "merchant_match": merchant_match(
                f["merchant"], (r["payee"], r["mname"], r["name"])),
            "day_gap": (posted - f["date"]).days,
            "refused_twin": _refused_twin(
                float(r["amount"]), (r["payee"], r["mname"], r["name"]),
                r["refused_holds"]),
        })
    for i in keys:
        total = fs[i]["total"]
        out[i].sort(key=lambda c: (_precedence(c),
                                   _MERCHANT_RANK[c["merchant_match"]],
                                   abs(c["day_gap"]),
                                   abs(c["amount"] - total), c["txn_id"]))
    return out


def _precedence(c: dict) -> int:
    """The order `decide` prefers charges in, so the one it would choose
    is among the first OFFER_LIMIT a person is shown: an exact charge not
    at another store, then the receipt's own store off by a tip, then an
    exact charge under another name, then the rest."""
    if c["exact"]:
        return 0 if c["merchant_match"] != "different" else 2
    return 1 if c["merchant_match"] == "same" else 3


def _in_band(amount: str, base: str) -> str:
    """SQL: `amount` lies in band(`base`) — the tip/currency tolerance a
    charge may differ from the amount it settles by."""
    return (f"{amount} >= {base} - GREATEST({FX_FLOOR}, {base} * "
            f"{FX_FRACTION}) - 0.005 AND {amount} <= {base} + GREATEST("
            f"{TIP_FLOOR}, {base} * {TIP_FRACTION}) + 0.005")


def _cents(v) -> int:
    return round(float(v) * 100)


def _refused_twin(amount: float, names, holds) -> bool:
    """Is this charge likely the settled form of a HOLD a person took this
    receipt off?

    A hold the person refused (pending, or since retired) that sits on the
    charge's account within a week, and that the charge could settle —
    the same cents, or inside the hold's tip/currency band from the same
    store (an aggregator with no pending link posts a tipped charge under
    a new id and retires the hold with nothing naming the two as one).

    The block lapses when the hold has a settlement of its own: another
    live posted row on its account that could settle it just as well and
    is at least as close to it in days. Then THIS charge is a separate
    purchase — the same coffee bought the next day, while the refused hold
    was the day before's — and the rules may pair it. Two rows equally
    close both lapse, and the matcher then sees a tie and leaves the pick
    to a person."""
    if not holds:
        return False
    for h in holds:
        def settles(amt, their_names, h=h) -> bool:
            return (_cents(amt) == _cents(h["amount"])
                    or merchant_match(h["name"], their_names) == "same")
        if not settles(amount, names):
            continue
        if not any(settles(o["amount"], o["names"])
                   for o in (h.get("alts") or [])):
            return True
    return False


def window_closed(receipt_date: dt.date | None,
                  today: dt.date | None = None) -> bool:
    """Has the automatic window passed — could no further charge still
    arrive inside it?"""
    if receipt_date is None:
        return True
    today = today or dt.date.today()
    return today > receipt_date + dt.timedelta(days=AUTO_DAYS_AFTER)


def decide(cands: list[dict], *, closed: bool = True
           ) -> tuple[str | None, str]:
    """(txn id, reason) — the one charge to attach, or None and why not.

    Pure: takes candidates as `candidates` returns them, keeps the ones in
    the automatic window, and applies the rules in the module docstring.
    `closed` says whether the window has passed (`window_closed`)."""
    # A charge that is likely the settled form of a HOLD a person took this
    # receipt off (same account, inside a week, the same cents or the same
    # store inside the hold's tip band, and the refused row a pending hold
    # or already retired — `_refused_twin` has the rule, and when it lapses)
    # is never paired automatically.
    # The refusal is remembered by id, and the posted row can arrive — with
    # no link back to its hold, before or after the hold's removal — in a
    # sync whose matcher pass would pair the receipt straight back. It
    # stays on offer: a person may still choose it. A refused charge that
    # is itself posted and live is its own purchase, and says nothing about
    # an identical one the next day (the same coffee, bought again).
    auto = [c for c in cands
            if -AUTO_DAYS_BEFORE <= c["day_gap"] <= AUTO_DAYS_AFTER
            and not c.get("refused_twin")]
    exact = [c for c in auto if c["exact"]]
    near = [c for c in auto
            if not c["exact"] and c["merchant_match"] == "same"]
    if exact:
        best = min(_MERCHANT_RANK[c["merchant_match"]] for c in exact)
        top = [c for c in exact
               if _MERCHANT_RANK[c["merchant_match"]] == best]
        if top[0]["merchant_match"] != "different":
            if len(top) > 1:
                return None, "tie"
            return top[0]["txn_id"], ("exact" if len(exact) == 1
                                      else "exact+merchant")
        # the only exact charges are at other stores: the receipt's own
        # store, off by a tip, is the better answer when it is here — even
        # when several strangers happen to cost the slip's total
        if not near:
            if len(top) > 1:
                return None, "tie"
            if not closed:
                return None, "early"
            return top[0]["txn_id"], ("exact" if len(exact) == 1
                                      else "exact+merchant")
    if len(near) > 1:
        return None, "tie"
    if len(near) == 1:
        if not closed:
            return None, "early"
        return near[0]["txn_id"], "near+merchant"
    return None, "none"


# ---- writes ------------------------------------------------------------------

def _lock(conn) -> None:
    # the lock every transaction-pairing writer takes — the pending→posted
    # settlement (which carries receipts to the posted row), the removal
    # re-anchor and the reimbursement links — so a receipt is never attached
    # to a row that is being retired under it, and two matchers never both
    # find the same charge free
    from ..sync.reanchor import hold_pairing_lock
    hold_pairing_lock(conn)


_WAITING_COLS = ("id, kind, status, parsed, created_at, unmatched_txn_ids,"
                 " match_method")


def _attach(conn, receipt_id: str, txn_id: str, method: str,
            require_free: bool) -> bool:
    free = ("AND NOT EXISTS (SELECT 1 FROM receipts x WHERE x.txn_id = %s"
            " AND x.kind = 'receipt')") if require_free else ""
    # the rules never pair a receipt with a charge a person took it off,
    # judged by the receipt's refusals as they stand NOW, not as the
    # matcher read them: a rename that carried a refusal onto a new id
    # after the read would otherwise leave a stale list that misses it. A
    # person's own pairing is theirs to make, refusal or not.
    refused = ("AND NOT (%s = ANY(COALESCE(unmatched_txn_ids, '{}')))"
               if method != "manual" else "")
    params = [txn_id, method, receipt_id, txn_id]
    if require_free:
        params.append(txn_id)
    if refused:
        params.append(txn_id)
    return conn.execute(
        f"""UPDATE receipts SET txn_id = %s, matched_at = now(),
                   match_method = %s
             WHERE id = %s::uuid AND txn_id IS NULL
               AND EXISTS (SELECT 1 FROM transactions t
                            WHERE t.id = %s AND t.removed = 0)
               {free}
               {refused}
            RETURNING id""", params).fetchone() is not None


def _household_day(conn) -> dt.date:
    from .. import localtime
    return localtime.household_day(conn)


def _try_one(conn, row: dict, cands: list[dict],
             today: dt.date) -> str | None:
    if (row["kind"] != "receipt" or row["status"] != "parsed"
            or row.get("match_method") is not None):
        return None
    txn_id, _why = decide(cands, closed=window_closed(
        facts(row)["date"], today))
    if txn_id and _attach(conn, str(row["id"]), txn_id, "auto",
                          require_free=True):
        return txn_id
    return None


def match_receipt(conn, receipt_id: str,
                  today: dt.date | None = None) -> str | None:
    """Try to pair one waiting receipt now. Returns the charge it was
    attached to, or None (still waiting)."""
    today = today or _household_day(conn)
    with conn.transaction():
        _lock(conn)
        row = conn.execute(
            f"SELECT {_WAITING_COLS} FROM receipts"
            " WHERE id = %s::uuid AND txn_id IS NULL",
            (receipt_id,)).fetchone()
        if row is None:
            return None
        return _try_one(conn, row, candidates(
            conn, row, before=AUTO_DAYS_BEFORE, after=AUTO_DAYS_AFTER),
            today)


def release_stranded(conn) -> int:
    """A matched receipt whose charge was retired goes back to waiting.

    A hold the aggregator drops without naming its posted twin is retired,
    and when the re-anchor cannot find the twin either (the charge posted
    for a different amount — a tip), a receipt the matcher or a person
    paired with the hold would sit on a row no screen shows. Waiting again,
    one the matcher paired meets the posted charge on the next pass. One a
    PERSON paired keeps its 'manual' mark while it waits, which keeps the
    matcher off it: the person chose that pairing, and a different charge
    picked by the rules would silently overrule them — the list offers the
    candidates for them to choose again. Receipts attached from the
    transaction itself are the re-anchor's business and stay put."""
    return conn.execute(
        """UPDATE receipts r
              SET txn_id = NULL, matched_at = NULL,
                  match_method = CASE WHEN r.match_method = 'manual'
                                      THEN 'manual' END
             FROM transactions t
            WHERE t.id = r.txn_id AND t.removed <> 0
              AND r.match_method IS NOT NULL""").rowcount


def match_waiting(conn, today: dt.date | None = None) -> dict:
    """Try the waiting parsed receipts, newest first. Cheap when nothing
    waits — one indexed probe — which is the answer on nearly every sync.
    One receipt that cannot be read is logged and passed over; it never
    stops the rest."""
    probe = conn.execute(
        """(SELECT 1 FROM receipts WHERE txn_id IS NULL LIMIT 1)
           UNION ALL
           (SELECT 1 FROM receipts r JOIN transactions t ON t.id = r.txn_id
             WHERE r.match_method IS NOT NULL AND t.removed <> 0 LIMIT 1)
           LIMIT 1""").fetchone()
    if probe is None:
        return {"matched": 0, "released": 0}
    today = today or _household_day(conn)
    matched = 0
    with conn.transaction():
        _lock(conn)
        released = release_stranded(conn)
        # only rows the matcher may pair: parsed, with a total read off
        # them, and not left for a person (a hand match whose charge went)
        rows = conn.execute(
            f"""SELECT {_WAITING_COLS} FROM receipts
                 WHERE txn_id IS NULL AND kind = 'receipt'
                   AND status = 'parsed' AND match_method IS NULL
                   AND parsed ? 'total'
                 ORDER BY created_at DESC, id
                 LIMIT %s""", (SWEEP_LIMIT,)).fetchall()
        every = candidates_for(conn, rows, before=AUTO_DAYS_BEFORE,
                               after=AUTO_DAYS_AFTER)
        # a charge this pass has just given to one receipt is no longer
        # free for the next — the same answer a fresh query would give
        taken: set[str] = set()
        for row, cands in zip(rows, every):
            try:
                with conn.transaction():
                    got = _try_one(
                        conn, row,
                        [c for c in cands if c["txn_id"] not in taken],
                        today)
            except Exception:                          # noqa: BLE001
                log.warning("waiting receipt %s passed over by the matcher",
                            row["id"], exc_info=True)
                continue
            if got:
                taken.add(got)
                matched += 1
    return {"matched": matched, "released": released}


def match_by_hand(conn, receipt_id: str, txn_id: str) -> bool:
    """A person pairs a waiting receipt with a charge. Any live charge will
    do — including one that already carries a receipt, since one charge can
    carry several (a split tender, a gift receipt). False when the receipt
    is not waiting or the charge is not live."""
    with conn.transaction():
        _lock(conn)
        return _attach(conn, receipt_id, txn_id, "manual",
                       require_free=False)


def unmatch(conn, receipt_id: str) -> str | None:
    """Take a receipt off its charge and put it back to waiting,
    remembering the charge so the matcher never pairs the two again.
    Returns the charge it came off, or None when it was not attached (or is
    a check image, which only ever lives on its transaction)."""
    with conn.transaction():
        _lock(conn)
        was = conn.execute(
            "SELECT txn_id FROM receipts WHERE id = %s::uuid"
            " AND txn_id IS NOT NULL AND kind = 'receipt' FOR UPDATE",
            (receipt_id,)).fetchone()
        if was is None:
            return None
        conn.execute(
            """UPDATE receipts
                  SET txn_id = NULL, matched_at = NULL, match_method = NULL,
                      unmatched_txn_ids = CASE
                          WHEN %s = ANY(unmatched_txn_ids)
                          THEN unmatched_txn_ids
                          ELSE unmatched_txn_ids || %s::text END
                WHERE id = %s::uuid""",
            (was["txn_id"], was["txn_id"], receipt_id))
    return was["txn_id"]


# ---- reading -----------------------------------------------------------------

def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _offer(c: dict) -> dict:
    return {"txn_id": c["txn_id"], "date": c["date"].isoformat(),
            "amount": round(c["amount"], 2), "payee": c["payee"],
            "account": c["account"], "pending": c["pending"],
            "exact": c["exact"], "merchant_match": c["merchant_match"]}


def needs_details(status: str, total: float | None, ai: bool) -> bool:
    """Can this receipt only move with a person's help? True when nothing
    is going to read its total: the AI tried and could not (`failed`, or
    `parsed` with no total), or there is no AI on this instance to try
    (`uploaded` with none configured — that receipt would otherwise sit
    unread forever). A receipt being read (`parsing`), or queued for a
    model that exists, is not asked for yet."""
    if total is not None or status == "parsing":
        return False
    return status != "uploaded" or not ai


def waiting(conn, today: dt.date | None = None, *,
            limit: int = PAGE_LIMIT, offset: int = 0) -> dict:
    """The waiting list: receipts without a charge, newest first, with what
    was read off each and the charges it could be; and the receipts the
    matcher paired recently, so a person can see — and undo — what it did.
    `count` is every receipt waiting, not just this page: `limit` and
    `offset` page through the rest, so none is out of reach.
    `ai_available` says whether this instance has a vision model to read
    receipts at all; each receipt's `needs_details` says it waits on a
    person to type its total and date in, and `released` that a charge a
    person had paired it with is gone, so the matcher leaves it to them."""
    today = today or _household_day(conn)
    limit = max(1, min(int(limit), PAGE_LIMIT))
    offset = max(0, int(offset))
    from . import llm_categorize
    ai = llm_categorize.configured(conn, role="vision")
    count = conn.execute(
        "SELECT count(*) AS n FROM receipts WHERE txn_id IS NULL"
    ).fetchone()["n"]
    rows = conn.execute(
        """SELECT id, mime, kind, status, parsed, error, created_at,
                  unmatched_txn_ids, match_method,
                  (image_optimized IS NOT NULL) AS has_optimized
             FROM receipts WHERE txn_id IS NULL
            ORDER BY created_at DESC, id LIMIT %s OFFSET %s""",
        (limit, offset)).fetchall()
    parsed_rows = [r for r in rows if r["status"] == "parsed"]
    by_id = dict(zip((r["id"] for r in parsed_rows),
                     candidates_for(conn, parsed_rows)))
    out = []
    for r in rows:
        f = _safe_facts(r)
        cands = by_id.get(r["id"], [])
        try:
            why = (decide(cands, closed=window_closed(f["date"], today))[1]
                   if cands else "none")
            stale = bool(f["date"]
                         and (today - f["date"]).days >= STALE_DAYS)
        except Exception:                              # noqa: BLE001
            # still listed, so the person can fix or delete it
            log.warning("waiting receipt %s listed without a verdict",
                        r["id"], exc_info=True)
            why, stale = "none", False
        out.append({
            "id": str(r["id"]), "mime": r["mime"], "kind": r["kind"],
            "status": r["status"], "parsed": r["parsed"], "error": r["error"],
            "created_at": _iso(r["created_at"]),
            "has_optimized": bool(r["has_optimized"]),
            "total": f["total"],
            "date": f["date"].isoformat() if f["date"] else None,
            "merchant": f["merchant"],
            "tie": why == "tie",
            "released": r["match_method"] == "manual",
            "needs_details": needs_details(r["status"], f["total"], ai),
            "manual": (isinstance(r["parsed"], dict)
                       and r["parsed"].get("source") == "manual"),
            "stale": stale,
            "candidates": [_offer(c) for c in cands[:OFFER_LIMIT]],
        })
    recent = conn.execute(
        f"""SELECT r.id, r.mime, r.parsed, r.matched_at, r.match_method,
                   (r.image_optimized IS NOT NULL) AS has_optimized,
                   t.id AS txn_id, t.date, t.amount,
                   {DISPLAY_MERCHANT} AS payee
              FROM receipts r
              JOIN transactions t ON t.id = r.txn_id
              {MC_JOIN}
             WHERE r.match_method = 'auto' AND t.removed = 0
               AND r.matched_at >= now() - make_interval(days => %s)
             ORDER BY r.matched_at DESC LIMIT 50""",
        (RECENT_DAYS,)).fetchall()
    return {
        "count": count,
        "limit": limit,
        "offset": offset,
        "ai_available": ai,
        "stale_days": STALE_DAYS,
        "waiting": out,
        "recent": [{
            "id": str(m["id"]), "mime": m["mime"], "parsed": m["parsed"],
            "has_optimized": bool(m["has_optimized"]),
            "matched_at": _iso(m["matched_at"]),
            "match_method": m["match_method"],
            "txn_id": m["txn_id"], "txn_date": _iso(m["date"]),
            "txn_amount": float(m["amount"]), "payee": m["payee"],
        } for m in recent],
    }
