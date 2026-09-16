"""One-to-one, name-aware cross-source dedup shared by the file importers.

A file import overlapping an aggregator's window must not double-count a
charge. A per-row rule (`amount = %s` within ±3 days, LIMIT 1, matched row
never consumed) is not enough: one existing row would absorb unlimited
import rows, and with no merchant check $100 at merchant X would suppress
an unrelated $100 at merchant Y. So:

  * candidates (other-source rows on the account, batch window ±3 days)
    are fetched ONCE per import;
  * import rows match greedily: same exact amount, closest date first,
    name-token overlap REQUIRED unless the dates are identical (bank
    descriptors can be opaque — "POS DEBIT 4417" — so a same-day
    exact-amount hit is trusted when a side carries no substantive name
    tokens). When BOTH sides carry real merchant tokens and they
    conflict outright, a same-day exact-amount coincidence is two
    different purchases (two $4.50 coffees), not a duplicate;
  * each existing row consumes at most one import row.

Skipped rows are still reported as skipped_duplicates by the callers.
"""

from __future__ import annotations

import datetime as dt
import re

WINDOW_DAYS = 3

# generic merchant-string filler that must not count as a name signal
# ("PETCO STORE" vs "SAFEWAY STORE" is NOT an overlap)
_STOPWORDS = {
    "store", "stores", "shop", "inc", "llc", "ltd", "corp", "company",
    "the", "and", "com", "www", "pos", "online", "web", "payment",
    "purchase", "debit", "credit", "card", "ach", "check", "transaction",
}


def _tokens(*names: str | None) -> set[str]:
    """Lowercase alpha-ish tokens (len >= 3, not pure digits, no generic
    filler) — the overlap test unit. 'SAFEWAY STORE 123' vs 'Safeway'
    share {'safeway'}."""
    toks: set[str] = set()
    for n in names:
        if not n:
            continue
        for t in re.split(r"[^a-z0-9]+", n.lower()):
            if len(t) >= 3 and not t.isdigit() and t not in _STOPWORDS:
                toks.add(t)
    return toks


def _distinct_names(a: set[str], b: set[str]) -> bool:
    """True when both sides carry substantive merchant tokens that share
    NOTHING — not even a squished-name substring ('burgerking' vs
    {'burger','king'}). That's positive evidence of two different
    merchants; an empty side (opaque bank descriptor) is no evidence."""
    if not a or not b or (a & b):
        return False
    return not any(x in y or y in x for x in a for y in b)


# The candidate fetch and the per-row scan both run synchronously inside
# the request handler, so both need hard bounds (rowcap.py caps the import
# file's rows; these cap the other side of the product). The fetch ceiling
# is far past any real account's history, so only a pathological account
# refuses.
# The per-claim ceiling bounds the worst case where thousands of existing
# rows share one (amount, date) cell: past it the row is treated as new
# (a possible duplicate imports; nothing real is lost).
MAX_CANDIDATES = 200_000
SCAN_CEILING = 400


class Deduper:
    """Per-import matcher. Build it with the batch's dates (one candidate
    fetch), then `claim()` each import row in file order.

    Candidates are indexed by (cents, date) so a claim only ever examines
    rows that could actually match — a flat list makes every claim scan the
    account's whole fetched history, an O(rows × candidates) product that
    pins a worker for hours on one big import."""

    def __init__(self, conn, account_id: str, exclude_prefix: str,
                 dates: list[dt.date], *,
                 max_candidates: int | None = None,
                 scan_ceiling: int | None = None,
                 max_date: dt.date | None = None):
        self._by_cell: dict[tuple[int, dt.date], list[dict]] = {}
        self._scan_ceiling = scan_ceiling or SCAN_CEILING
        if not dates:
            return
        limit = max_candidates or MAX_CANDIDATES
        lo, hi = min(dates), max(dates)
        # max_date caps the candidate pool at a caller-known boundary —
        # the reconnect guard passes its restore cutoff so rows the
        # in-flight backfill itself inserted can never absorb later pages.
        cand_hi = hi + dt.timedelta(days=3)
        if max_date is not None and max_date < cand_hi:
            cand_hi = max_date
        rows = conn.execute(
            """SELECT date, amount, name, merchant_name FROM transactions
               WHERE removed = 0 AND account_id = %s
                 AND id NOT LIKE %s
                 AND date BETWEEN %s - INTERVAL '3 days' AND %s
               LIMIT %s""",
            (account_id, exclude_prefix + ":%", lo, cand_hi,
             limit + 1)).fetchall()
        if len(rows) > limit:
            raise ValueError(
                "this account has too much existing history in the file's "
                "date range to check for duplicates — split the file into "
                "smaller date ranges and import them one at a time")
        for r in rows:
            cell = (round(float(r["amount"]) * 100), r["date"])
            self._by_cell.setdefault(cell, []).append({
                "tokens": _tokens(r["name"], r["merchant_name"]),
                "used": False,
            })

    def claim(self, date: dt.date, amount: float, *names: str | None) -> bool:
        """True when an existing other-source row covers this import row;
        the matched candidate is consumed and can't absorb another row.

        Preference order is the full-scan key (delta, overlap):
        same-day with a name signal, then same-day amount-only (unless the
        names conflict outright), then the nearest off-day with a name
        signal."""
        cents = round(amount * 100)
        row_tokens = _tokens(*names)
        scanned = 0
        same_day_fallback = None
        for delta in range(WINDOW_DAYS + 1):
            days = ((date,) if delta == 0
                    else (date - dt.timedelta(days=delta),
                          date + dt.timedelta(days=delta)))
            for d in days:
                for c in self._by_cell.get((cents, d), ()):
                    if c["used"]:
                        continue
                    scanned += 1
                    if scanned > self._scan_ceiling:
                        return False    # bounded work beats a perfect match
                    overlap = bool(row_tokens & c["tokens"])
                    if overlap:
                        c["used"] = True
                        return True     # nothing at a later delta beats this
                    if delta == 0 and same_day_fallback is None and \
                            not _distinct_names(row_tokens, c["tokens"]):
                        same_day_fallback = c
            if delta == 0 and same_day_fallback is not None:
                # (0, no-overlap) still outranks any off-day overlap match
                same_day_fallback["used"] = True
                return True
        return False
