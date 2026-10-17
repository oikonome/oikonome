"""Match a store's own purchase records — online orders, warehouse-club
receipts — to the card transactions that paid for them, and carry the store's
per-purchase category onto the transaction as an override.

Aggregators give every charge at such a store the same merchant and the
same generic category; the store's records know what was bought. A charge
matches a record by exact amount + date proximity (+ card mask as a
tiebreaker), and the transaction's category_override becomes
"<Store> - <category>".

Sign conventions: engine positive = money out; store records hold charges
negative (refunds positive). So a match is txn.amount == -record.amount.

Matching is recomputed from scratch on every run (idempotent). Human
decisions (manual_categories) are re-applied LAST so a run can never
clobber a manual override — that ordering is load-bearing.

A record whose items name more than one category also carves the charge
into those categories as a split (transaction_splits, source = the store):
a receipt that is part groceries and part household goods counts each part
in its own category, instead of the whole charge in its dominant one. The parts are
the items' categories weighted by their amounts and scaled to what the
card was charged (tax and rounding spread pro rata, the last cent on the
largest part), so they add up to the row the way a hand split must. They
are the record's reading, recomputed with the match on every run, and they
never displace a person's: a row with a hand split, or whose category the
person pinned, is left alone.
"""
from __future__ import annotations

import datetime as dt
import itertools
import json
import math
import re
from dataclasses import dataclass

from . import splits as _splits
from .compat import MAX_AMOUNT, as_date


@dataclass(frozen=True)
class Store:
    label: str              # "Amazon" — the override prefix "<label> - "
    name_re: re.Pattern     # which transactions belong to the store
    records: str            # table of the store's purchases
    matches: str            # table of (transaction_id, dedup_key)
    # the record date may PRECEDE card posting by this many days …
    window_before: int
    # … or trail it slightly (authorized vs posted)
    window_after: int
    # stamp "<label> - Unmatched" on the store's charges no record explains,
    # so gaps stay visible. Right for a store whose every charge is opaque
    # (an online marketplace); wrong for one whose unmatched charges — fuel,
    # membership — already carry a useful aggregator category (a warehouse
    # club).
    mark_unmatched: bool
    # A record that may be paid as SEVERAL charges — an online order billed
    # per shipment, or its return refunded in parts: (column, value) naming
    # such records in the records table, and how many days after the
    # record's date the last of its charges may post. None: every record is
    # exactly one charge.
    split_records: tuple[str, str] | None = None
    split_window: int = 0


AMAZON = Store(label="Amazon",
               name_re=re.compile(r"amazon|amzn", re.IGNORECASE),
               records="amazon_orders", matches="amazon_matches",
               window_before=7, window_after=3, mark_unmatched=True)

# a warehouse receipt is dated the day the card was run, so posting only
# trails it; the store's online orders charge at shipment, up to a few days
# after the receipt's order date
COSTCO = Store(label="Costco",
               name_re=re.compile(r"costco", re.IGNORECASE),
               records="costco_receipts", matches="costco_matches",
               window_before=5, window_after=2, mark_unmatched=False,
               # the web store bills each shipment as it leaves, so one order
               # arrives as two or three charges over the following week or
               # two; a warehouse receipt is always a single swipe
               split_records=("receipt_type", "online"), split_window=14)

# bounds on the search for a record's split charges: the nearest few
# unmatched charges, in combinations of up to four
_SPLIT_MAX_CANDIDATES = 12
_SPLIT_MAX_PARTS = 4


def _days_after(later: dt.date, earlier: dt.date) -> int:
    return (as_date(later) - as_date(earlier)).days


def _split_combo(cands: list[dict], want_cents: int,
                 payment_method: str | None) -> tuple[dict, ...] | None:
    """The fewest charges (two to four) whose cents add up exactly to the
    record. Among sets of that size, charges on the record's card win,
    then the set whose last charge posted earliest."""
    for k in range(2, min(_SPLIT_MAX_PARTS, len(cands)) + 1):
        best = None
        for combo in itertools.combinations(cands, k):
            if sum(round(t["amount"] * 100) for t in combo) != want_cents:
                continue
            off_card = sum(0 if (t["mask"] and t["mask"] in (payment_method or ""))
                           else 1 for t in combo)
            key = (off_card, max(as_date(t["date"]) for t in combo))
            if best is None or key < best[0]:
                best = (key, combo)
        if best:
            return best[1]
    return None


def _usable(amount) -> bool:
    try:
        return (amount is not None and math.isfinite(float(amount))
                and abs(float(amount)) <= MAX_AMOUNT)
    except (TypeError, ValueError, OverflowError):
        return False


def run_match(conn, store: Store) -> dict:
    """Match the store's transactions against its purchase records."""
    prefix = f"{store.label} - "
    split_col = (f", {store.split_records[0]} AS split_kind"
                 if store.split_records else "")
    records = [dict(r) for r in conn.execute(
        f"SELECT dedup_key, date, amount, category, payment_method, items_json"
        f"{split_col} FROM {store.records}")]
    by_key = {r["dedup_key"]: r for r in records}

    # Budget purchase rows only: a bare name regex would stamp
    # '<Store> - Unmatched' onto brokerage *stock trades* (a store's ticker
    # in an investment account) and onto a store-card payment's TRANSFER
    # rows. Skip linked shadow accounts — a shadow's duplicate
    # charge could win the one-to-one match and leave the PRIMARY stamped
    # unmatched, which the budget sees (it drops shadows). Matching only
    # the primary keeps the food/other split right.
    from . import links
    shadows = links.shadow_ids(conn)
    plaid_rows = [dict(r) for r in conn.execute(
        """SELECT t.id, t.date, t.authorized_date, t.amount, t.name,
                  t.merchant_name, a.mask
           FROM transactions t LEFT JOIN accounts a ON a.id = t.account_id
           LEFT JOIN items i ON i.id = a.item_id
           WHERE t.removed = 0
             AND COALESCE(a.type,'') != 'investment'
             AND COALESCE(a.subtype,'') != 'crypto'
             AND NOT (t.account_id = ANY(%s))
             AND COALESCE(t.category_primary,'')
                 NOT IN ('TRANSFER_IN','TRANSFER_OUT','LOAN_PAYMENTS')""",
        (shadows,))]
    store_txns = [p for p in plaid_rows
                  if store.name_re.search(f"{p['name'] or ''} {p['merchant_name'] or ''}")]
    # Matching is in cents. A stored amount that is no sum of money (NULL,
    # or past any real charge, written before the doors bounded it)
    # overflows that conversion, and one such row would fail this pass for
    # the whole household on every run; it is skipped instead.
    records = [r for r in records if _usable(r["amount"])]
    store_txns = [t for t in store_txns if _usable(t["amount"])]

    # Index records by absolute cent amount for exact-amount candidates
    by_cents: dict[int, list[dict]] = {}
    for row in records:
        by_cents.setdefault(round(abs(row["amount"]) * 100), []).append(row)

    used: set[str] = set()
    matches: list[tuple[str, str, str]] = []  # (txn_id, dedup_key, override)

    # Oldest first so backfilled history matches deterministically
    for txn in sorted(store_txns, key=lambda t: t["date"]):
        cents = round(abs(txn["amount"]) * 100)
        txn_date = txn["authorized_date"] or txn["date"]
        best = None
        for cand in by_cents.get(cents, []):
            if cand["dedup_key"] in used:
                continue
            # Sign must correspond: charge (+) ↔ record charge (-)
            if (txn["amount"] > 0) != (cand["amount"] < 0):
                continue
            # posting date minus record date: card posting trails the purchase
            delta = _days_after(txn_date, cand["date"])
            if not (-store.window_after <= delta <= store.window_before):
                continue
            mask_bonus = 0 if (txn["mask"] and txn["mask"] in (cand["payment_method"] or "")) else 1
            score = (mask_bonus, abs(delta))
            if best is None or score < best[0]:
                best = (score, cand)
        if best:
            cand = best[1]
            used.add(cand["dedup_key"])
            matches.append((txn["id"], cand["dedup_key"],
                            f"{prefix}{cand['category']}"))

    # Split records second, over what the exact pass left: a record that
    # is still unexplained may be the SUM of a few charges posted over its
    # split window. Only records the store names as splittable take part —
    # a warehouse receipt summed from unrelated swipes would be a false
    # match that the exact rule never makes.
    if store.split_records:
        kind_value = store.split_records[1]
        taken = {m[0] for m in matches}
        for rec in sorted((r for r in records
                           if r["dedup_key"] not in used
                           and r.get("split_kind") == kind_value),
                          key=lambda r: r["date"]):
            want = round(-rec["amount"] * 100)      # engine sign: + = money out
            if not want:
                continue
            near = []
            for t in store_txns:
                if t["id"] in taken or not t["amount"]:
                    continue
                if (t["amount"] > 0) != (want > 0):
                    continue
                delta = _days_after(t["authorized_date"] or t["date"], rec["date"])
                if -store.window_after <= delta <= store.split_window:
                    near.append((abs(delta), t))
            near.sort(key=lambda x: x[0])
            combo = _split_combo([t for _, t in near[:_SPLIT_MAX_CANDIDATES]],
                                 want, rec["payment_method"])
            if combo:
                used.add(rec["dedup_key"])
                for t in combo:
                    taken.add(t["id"])
                    matches.append((t["id"], rec["dedup_key"],
                                    f"{prefix}{rec['category']}"))

    matched_ids = {m[0] for m in matches}
    kind = store.label.lower()
    txn_amount = {t["id"]: t["amount"] for t in store_txns}
    # the parts each matched charge would carry, from its record's items;
    # None where the record reads as one category (or cannot be read)
    auto_splits = [(txn_id, parts) for txn_id, dedup_key, _ in matches
                   if (parts := record_parts(by_key[dedup_key].get("items_json"),
                                             txn_amount[txn_id], prefix))]
    split_rows = 0
    with conn.transaction():
        # The parts were computed from the amount read above, outside this
        # transaction. Lock the rows they are for, first and in id order
        # (transactions before their splits, as splits.set_split takes
        # them), so a sync cannot post a new amount between the check
        # below and the insert; a row whose amount already moved is
        # skipped there, and the next run splits what it now says.
        if auto_splits:
            conn.execute(
                "SELECT id FROM transactions WHERE id = ANY(%s) "
                "ORDER BY id FOR UPDATE",
                (sorted(t for t, _ in auto_splits),))
        # Recompute from scratch: clear prior store overrides + matches —
        # THIS store's, by kind, never a person's pin that happens to
        # start with the store's name
        conn.execute(f"DELETE FROM {store.matches}")
        conn.execute("UPDATE transactions SET category_override = NULL, "
                     "override_source = NULL WHERE override_source = %s", (kind,))
        # an item match outranks a bill's stamp (the store knows what was
        # bought; the bill only knows who was paid) and never a person's
        # pin; the kind on the row decides, not the order of writers
        for txn_id, dedup_key, override in matches:
            conn.execute(
                """UPDATE transactions SET category_override=%s, override_source=%s
                    WHERE id=%s AND (override_source IS NULL
                                     OR override_source IN ('amazon','costco','bill'))""",
                (override, kind, txn_id))
            conn.execute(
                f"""INSERT INTO {store.matches} (transaction_id, dedup_key)
                    VALUES (%s,%s)
                    ON CONFLICT (tenant_id, transaction_id) DO UPDATE SET
                        dedup_key=EXCLUDED.dedup_key, matched_at=now()""",
                (txn_id, dedup_key))
        if store.mark_unmatched:
            # "Unmatched" is the absence of a record: it fills an empty slot
            # and displaces nothing — not a bill's stamp, not a pin, and not
            # a row a person CLEARED, whose kind is 'user' over a NULL
            # override precisely so the placeholder cannot come back
            for txn in store_txns:
                if txn["id"] not in matched_ids:
                    conn.execute(
                        """UPDATE transactions SET category_override=%s, override_source=%s
                            WHERE id=%s AND override_source IS NULL""",
                        (prefix + "Unmatched", kind, txn["id"]))
        # a person's pins, restated: with every writer guarding its own
        # rank the guards above already leave them alone; this is the belt
        # to those braces, and it names only the PERSON's rows — a
        # bill's stamp is not a human decision and must not be put back
        # over an item match here. The empty pin is their answer of "no
        # category", so it restates as a NULL override still carrying
        # their kind — cleared, and out of every automatic writer's reach.
        conn.execute("""UPDATE transactions SET category_override =
                          (SELECT NULLIF(category, '') FROM manual_categories m
                           WHERE m.transaction_id = transactions.id),
                          override_source = 'user'
                        WHERE id IN (SELECT transaction_id FROM manual_categories
                                      WHERE bill_id IS NULL)""")
        # The record's split, recomputed from scratch like the match: THIS
        # store's parts go, then come back for every matched charge whose
        # items read as several categories. After the pins above, because
        # a pinned row is the person's whole-row answer and gets no parts;
        # a row carrying a hand split keeps it, whatever the receipt says.
        #
        # Parts go only where the match itself took the row — its override
        # is this store's kind. That excludes a person's pin, and equally
        # any override the store's rank did not displace above: the split
        # is the record's reading at a finer grain, and a row whose
        # whole-row category the store may not write may not have its
        # parts written by the store either.
        conn.execute("DELETE FROM transaction_splits WHERE source = %s", (kind,))
        for txn_id, parts in auto_splits:
            ok = conn.execute(
                f"""SELECT t.amount FROM transactions t
                     WHERE t.id = %s AND t.removed = 0
                       AND t.override_source = %s
                       AND NOT EXISTS (SELECT 1 FROM transaction_splits sp
                                        WHERE sp.txn_id = t.id)
                       AND ({_splits.SPLITTABLE_SQL})""",
                (txn_id, kind)).fetchone()
            if not ok or not _usable(ok["amount"]) or (
                    round(ok["amount"] * 100) != round(txn_amount[txn_id] * 100)):
                continue
            for line, p in enumerate(parts, 1):
                conn.execute(
                    """INSERT INTO transaction_splits
                           (txn_id, line, category, amount, source)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (txn_id, line, p["category"], p["amount"], kind))
            split_rows += 1

    key = store.label.lower()
    return {
        f"{key}_plaid_txns": len(store_txns),
        "matched": len(matches),
        "unmatched": len(store_txns) - len(matches),
        "split": split_rows,
    }


# ---- the record's split --------------------------------------------------

def item_quantity(item: dict) -> float:
    """How many of the item were bought, for weighting a unit price. A
    quantity that is missing or not a positive count of things (zero, a
    negative, text, a value whose product with the price is no sum of
    money) counts as one: a bad quantity says nothing about the price, so
    it does not cost the record its reading."""
    q = item.get("quantity")
    try:
        q = float(q)
        price = abs(float(item.get("price")))
    except (TypeError, ValueError, OverflowError):
        return 1.0
    if not math.isfinite(q) or q <= 0 or not _usable(price * q):
        return 1.0
    return q


def _item_buckets(items) -> dict[str, float] | None:
    """What the record's items add up to per category. None when the items
    cannot carry a split: no list, an item without a category, an amount
    that is not money. Costco items carry `amount` (price × quantity, as the
    register printed it), taken as it stands; Amazon items carry the UNIT
    `price` and a `quantity`, so six of a $2 item weigh $12, not $2."""
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except ValueError:
            return None
    if not isinstance(items, list) or not items:
        return None
    out: dict[str, float] = {}
    for it in items:
        if not isinstance(it, dict):
            return None
        cat = it.get("category")
        if "amount" in it:
            amt = it["amount"]
        else:
            amt = it.get("price")
        if not isinstance(cat, str) or not cat.strip() or not _usable(amt):
            return None
        try:
            amt = float(amt)
        except (TypeError, ValueError):
            return None
        if "amount" not in it:
            amt *= item_quantity(it)
        out[cat.strip()] = out.get(cat.strip(), 0.0) + amt
    return out


def record_parts(items, charge: float, prefix: str) -> list[dict] | None:
    """The split a matched charge takes from its record's items, as
    [{category, amount}, …] in the row's sign, or None when the record
    reads as one category (or cannot be read) and the single override is
    the whole answer.

    Buckets are the items' amounts per category; a negative bucket (a
    storewide coupon on its own line) is netted pro rata into the
    positive ones first, so a coupon lowers each category in proportion
    to its share. The
    buckets are then scaled to the cents the card was charged — tax,
    shipping and rounding spread in proportion — with the largest
    remainders taking the leftover cents, so the parts sum to the row
    exactly. A bucket that rounds to nothing is dropped; fewer than two
    parts left is no split. Only money out splits: a refund is one row."""
    if not _usable(charge) or charge <= 0:
        return None
    buckets = _item_buckets(items)
    if not buckets:
        return None
    owed = -sum(v for v in buckets.values() if v < 0)
    pos = {c: v for c, v in buckets.items() if v > 0}
    total = sum(pos.values())
    if owed > 0 and total > 0:
        pos = {c: v - owed * (v / total) for c, v in pos.items()}
        pos = {c: v for c, v in pos.items() if v > 0}
        total = sum(pos.values())
    if len(pos) < 2 or total <= 0:
        return None
    want = round(charge * 100)
    raw = {c: want * v / total for c, v in pos.items()}
    cents = {c: int(math.floor(x)) for c, x in raw.items()}
    short = want - sum(cents.values())
    for c in sorted(raw, key=lambda c: (raw[c] - cents[c], pos[c]), reverse=True)[:short]:
        cents[c] += 1
    parts = [{"category": f"{prefix}{c}", "amount": n / 100}
             for c, n in sorted(cents.items(), key=lambda kv: -kv[1]) if n > 0]
    if len(parts) < 2:
        return None
    try:
        return _splits.validate(charge, parts)
    except _splits.SplitError:
        return None
