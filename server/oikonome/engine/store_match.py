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
"""
from __future__ import annotations

import datetime as dt
import itertools
import re
from dataclasses import dataclass

from .compat import as_date


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


def run_match(conn, store: Store) -> dict:
    """Match the store's transactions against its purchase records."""
    prefix = f"{store.label} - "
    split_col = (f", {store.split_records[0]} AS split_kind"
                 if store.split_records else "")
    records = [dict(r) for r in conn.execute(
        f"SELECT dedup_key, date, amount, category, payment_method{split_col} "
        f"FROM {store.records}")]

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
    with conn.transaction():
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

    key = store.label.lower()
    return {
        f"{key}_plaid_txns": len(store_txns),
        "matched": len(matches),
        "unmatched": len(store_txns) - len(matches),
    }
