"""Synthetic households for the restore round-trip property test.

One builder per table the full-data export carries. Every household is
drawn from a seeded `random.Random`, so a seed names one household exactly,
and every name, amount and address in it is invented.

Ids are drawn from SHARED pools (`rt-tx-07`, the fifth entity uuid, ...),
so two households built from different seeds hold some of the same ids for
different things: that is the colliding-ids case of a restore into another
household, and it comes for free.

What each table's "same row" is, as the restore decides it, is `IDENTITY`:
for a table with a declared lander (sync/landers.py) it is read from that
lander's identity, so the test and the restore share the one definition;
the rest are still written down here, read off sync/restore.py, until
their loops become landers.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import random
import uuid
from collections import Counter
from decimal import Decimal

from oikonome.db import tenancy
from oikonome.engine.compat import MAX_AMOUNT, jsonb
from oikonome.sync.landers import LANDERS as _LANDERS, Counted, Group

# The seeds every run covers. OIKONOME_RT_SEEDS adds more: a count ("40"
# runs seeds 1..40) or a comma list ("101,102").
FIXED_SEEDS = (1, 2, 3, 4, 5, 6, 7, 8)


def seeds() -> tuple[int, ...]:
    extra = os.environ.get("OIKONOME_RT_SEEDS", "").strip()
    if not extra:
        return FIXED_SEEDS
    if "," in extra or not extra.isdigit():
        more = [int(s) for s in extra.split(",") if s.strip()]
    else:
        more = list(range(1, int(extra) + 1))
    return tuple(dict.fromkeys(FIXED_SEEDS + tuple(more)))


_NS = uuid.UUID("6f1c2a52-9a5e-4c1e-9d0b-3c0de0ff1ce5")


def uid(kind: str, n: int) -> str:
    """The n-th id of a pool: the same in every household that draws it."""
    return str(uuid.uuid5(_NS, f"{kind}-{n}"))


# Text the round trip must carry unchanged: right-to-left scripts,
# combining marks, emoji, CSV metacharacters, line breaks inside a cell,
# and cells a spreadsheet would read as a formula (the export quotes them,
# the restore takes exactly that quote off). Nothing here holds a run of
# eight digits, which every door cuts as a card number: that is content
# the restore deliberately rewrites (see `rewritten_rows`).
LEADS = ("", "", "", "=", "+", "-", "@", "'")
MIDDLES = ("", " שלום", " مرحبا", " étê", " 🧾🏠",
           ' "quoted", here', " a,b;c", " line\r\nbreak", " tab\tin",
           " ünïcödé", " 1+1")
WORDS = ("Invented Grocer", "Made-up Hardware", "Fictional Diner",
         "Sample Pharmacy", "Pretend Fuel", "Placeholder Books",
         "Example Utility", "Imaginary Gym")


def text(rng: random.Random, base: str | None = None) -> str:
    base = base or rng.choice(WORDS)
    return f"{rng.choice(LEADS)}{base}{rng.choice(MIDDLES)}"


MONEY_EDGES = (0.0, 0.01, -42.5, 42.5, 1234.56, -0.01, 99.99,
               MAX_AMOUNT - 0.01, -(MAX_AMOUNT - 0.01))


def money(rng: random.Random) -> float:
    if rng.random() < 0.5:
        return rng.choice(MONEY_EDGES)
    return round(rng.uniform(-500, 500), 2)


def ts(rng: random.Random) -> dt.datetime:
    """A fixed moment in the past: a timestamp the restore must carry, never
    one it could have set itself (those are recent, see `table_digest`)."""
    return dt.datetime(2025, rng.randint(1, 12), rng.randint(1, 28),
                       rng.randint(0, 23), rng.randint(0, 59),
                       rng.randint(0, 59), rng.randint(0, 999999),
                       tzinfo=dt.timezone.utc)


def day(rng: random.Random) -> dt.date:
    return dt.date(2025, rng.randint(1, 12), rng.randint(1, 28))


def pick(rng, pool: int, k: int) -> list[int]:
    return sorted(rng.sample(range(pool), k))


def receiving(h) -> bool:
    """Whether `h` is a scenario's destination household (built from the
    seed plus 1000) rather than an archive's source. The rows a lander
    replaces on a condition are drawn per role, so a merge into another
    household meets the condition: the destination holds the machine's
    row, the archive the person's."""
    return h.seed >= 1000


IMAGE = b"\x89PNG\r\n\x1a\n" + b"invented receipt bytes "


# --------------------------------------------------------------------------
# the household under construction

_NULLABLE: dict[str, set[str]] = {}


def nullable(conn, table: str) -> set[str]:
    if table not in _NULLABLE:
        _NULLABLE[table] = {r["column_name"] for r in conn.execute(
            """SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                  AND is_nullable = 'YES' AND is_generated = 'NEVER'
                  AND column_name <> 'tenant_id'""",
            (table,)).fetchall()}
    return _NULLABLE[table]


# Nullable columns a row cannot land without: the restore refuses or
# re-types a row whose cell is blank. Every other nullable column is NULL
# in the first row each builder writes.
KEEP_NOT_NULL = {
    "transactions": {"date"},
    "bill_proposals": {"payee", "kind", "amount"},
    "receipt_items": {"amount"},
    # a blank method reads as a person's alias ("manual"): content the
    # restore rewrites, so it is in `rewritten_rows`, not here
    "merchant_canonical": {"method"},
}


# Nullable columns the settle step fills on every row (see `settle`): the
# identity pass files every charge under a merchant, minting one for a
# charge it cannot name, so no settled household holds a NULL there.
FILLED_BY_SETTLE = {
    "transactions": {"merchant_id"},
    # every stored form is given an upload (taxdocs.adopt_unexplained)
    "income_documents": {"upload_id"},
    # every receipt is decided (receipt_identity.decide_pending)
    "receipts": {"slip_decided_at"},
}


class Household:
    def __init__(self, conn, seed: int):
        self.conn = conn
        self.seed = seed
        self.rng = random.Random(seed)
        self.ids: dict[str, list] = {}
        self.amount: dict[str, float] = {}

    def put(self, table: str, rows: list[dict], *, null_first=True):
        """Insert `rows`; the first has every nullable column NULL that
        the restore lets be NULL."""
        if rows and null_first:
            keep = KEEP_NOT_NULL.get(table, set())
            rows[0] = {k: (None if k in nullable(self.conn, table)
                           and k not in keep else v)
                       for k, v in rows[0].items()}
        for row in rows:
            cols = list(row)
            self.conn.execute(
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES "
                f"({', '.join(['%s'] * len(cols))})",
                [jsonb(v) if isinstance(v, (dict, list))
                 and not (table == "receipts"
                          and k in ("unmatched_txn_ids", "not_same_slip")) else v
                 for k, v in row.items()])
        return rows


# --------------------------------------------------------------------------
# builders, in the order a household is filled (parents first)

def b_items(h):
    rows = [{"id": f"rt-item-{n}", "aggregator": "test",
             "institution_id": f"ins_{n}", "institution_name": text(h.rng),
             "status": "good", "linked_at": ts(h.rng),
             "raw": {"note": text(h.rng)}, "logo": None,
             "brand_color": "#123456", "url": "https://bank.example.test",
             "archived_reason": None, "archived_at": None}
            for n in pick(h.rng, 6, 3)]
    h.put("items", rows)
    h.ids["items"] = [r["id"] for r in rows]


def b_business_entity(h):
    rows = [{"id": uid("ent", n), "name": text(h.rng, "Invented Works LLC"),
             "structure": h.rng.choice(("single_member_llc", "sole_prop",
                                        "s_corp")),
             "state": "XX", "formation_date": day(h.rng),
             "business_start_date": day(h.rng), "ein_last4": "0042",
             "registered_agent": text(h.rng), "fiscal_year_end": "12-31",
             "status": "active", "home_office_sqft": h.rng.randint(0, 400),
             "income_tax_rate": Decimal("22.50"), "filing_status": "single",
             "tax_reserve_account_id": None, "created_at": ts(h.rng),
             "updated_at": ts(h.rng)}
            for n in pick(h.rng, 4, 2)]
    h.put("business_entity", rows)
    h.ids["business_entity"] = [r["id"] for r in rows]


def b_merchants(h):
    ns = pick(h.rng, 12, 6)
    rows = []
    for i, n in enumerate(ns):
        rows.append({"id": uid("merchant", n),
                     # a shared pool: another household may hold the same
                     # entity under its own id, and the restore folds them
                     "plaid_entity_id": (f"ent_{h.rng.randrange(6)}"
                                         if i % 2 else None),
                     "name": text(h.rng), "name_source": "layer1",
                     "kind": "merchant", "logo_url": None,
                     "website": "https://shop.example.test",
                     "phone": "555-0100", "mcc": "5411",
                     "parent_id": None, "merged_into": None,
                     "created_at": ts(h.rng), "updated_at": ts(h.rng)})
    # entity ids are unique per household
    seen = set()
    for r in rows:
        if r["plaid_entity_id"] in seen:
            r["plaid_entity_id"] = None
        seen.add(r["plaid_entity_id"])
    # name twins: two merchants, one name, no entity to tell them apart
    rows[-1]["name"] = rows[-2]["name"]
    rows[-1]["plaid_entity_id"] = rows[-2]["plaid_entity_id"] = None
    rows[-1]["name_source"] = "manual"
    h.put("merchants", rows)
    h.ids["merchants"] = [r["id"] for r in rows]


def b_accounts(h):
    ns = pick(h.rng, 9, 4)
    rows = []
    for i, n in enumerate(ns):
        card = i % 2 == 1
        rows.append({"id": f"rt-acct-{n}",
                     "item_id": h.rng.choice(h.ids["items"]),
                     "name": text(h.rng, "Everyday Account"),
                     "display_name": text(h.rng),
                     "official_name": text(h.rng),
                     "type": "credit" if card else "depository",
                     "subtype": "credit card" if card else "checking",
                     "type_user_set": bool(i % 3 == 0),
                     "mask": f"{n:04d}", "balance_current": money(h.rng),
                     "balance_available": money(h.rng),
                     "balance_limit": 5000.0 if card else None,
                     "currency": "USD", "raw": {"k": text(h.rng)},
                     "entity_id": (h.rng.choice(h.ids["business_entity"])
                                   if i == 2 else None),
                     "user_removed_at": ts(h.rng) if i == 3 else None,
                     "owner": h.rng.choice(("mine", "ours", None))})
    h.put("accounts", rows)
    h.ids["accounts"] = [r["id"] for r in rows]


def b_transactions(h):
    rng = h.rng
    ns = pick(rng, 60, 14)
    rows = []
    for n in ns:
        rows.append({
            "id": f"rt-tx-{n:02d}", "account_id": rng.choice(h.ids["accounts"]),
            "date": day(rng), "authorized_date": day(rng),
            "amount": money(rng), "name": text(rng),
            "merchant_name": text(rng),
            "category_primary": rng.choice(("FOOD_AND_DRINK",
                                            "GENERAL_MERCHANDISE",
                                            "TRANSPORTATION")),
            "category_detailed": "FOOD_AND_DRINK_GROCERIES",
            "category_plaid": "FOOD_AND_DRINK",
            "category_plaid_detailed": "FOOD_AND_DRINK_GROCERIES",
            "category_plaid_confidence": "HIGH",
            "category_override": None, "pending": 0,
            "pending_transaction_id": None, "payment_channel": "in store",
            "removed": 0, "raw": {"memo": text(rng)},
            "entity_id": None, "retired_at": None,
            "merchant_id": rng.choice(h.ids["merchants"] + [None]),
            "merchant_name_set_aside": None, "check_number": None,
            "payment_processor": None,
            "location_city": text(rng, "Inventedville"),
            "location_region": "XX", "location_address": text(rng, "1 Any St"),
            "location_postal": "00000", "location_lat": 0.5,
            "location_lon": -0.5, "location_store": "12", "mcc": "5411",
            "category_source": "plaid", "owner_override": None,
            "merchant_outlet": None, "override_source": None})
    # every merchant is some charge's, or the identity pass prunes it
    # before the export (and a merge offer naming it would dangle)
    for row, m in zip(rows[1:], h.ids["merchants"]):
        row["merchant_id"] = m
    # same-content twins under different ids (two identical coffees on one
    # day)
    twin = dict(rows[-1])
    twin["id"] = f"rt-tx-twin-{rng.randrange(4)}"
    rows.append(twin)
    # a person's own category
    rows[2]["category_override"] = "HOME_IMPROVEMENT"
    rows[2]["override_source"] = "user"
    h.put("transactions", rows)
    h.ids["transactions"] = [r["id"] for r in rows]
    h.amount = {r["id"]: r["amount"] for r in rows}


def b_bills(h):
    rows = [{"id": f"rec:rt-bill-{n}", "type": h.rng.choice(("bill", "income")),
             "payee": text(h.rng), "amount": round(h.rng.uniform(1, 900), 2),
             "frequency": "MONTHLY", "monthly_amount": None,
             "due_on": day(h.rng), "category": "UTILITIES",
             "merchant": text(h.rng), "is_completed": 0, "active": 1,
             "raw": {}}
            for n in pick(h.rng, 8, 3)]
    for i, r in enumerate(rows):
        r["monthly_amount"] = r["amount"]
        # the day it was set up, and one with none (set up before it was
        # kept): both come back as they went
        # (its due day stands in, so no draw of the builder's moves)
        r["created_on"] = r["due_on"] if i % 2 == 0 else None
    h.put("bills", rows)
    h.ids["bills"] = [r["id"] for r in rows]


def b_bill_proposals(h):
    rows = [{"id": f"prop-{n}", "kind": "bill", "bill_type": "occurrence",
             "payee": text(h.rng), "amount": round(h.rng.uniform(1, 90), 2),
             "frequency": "MONTHLY", "interval": 1, "next_due": day(h.rng),
             "evidence": {}, "llm_tag": None,
             "status": h.rng.choice(("rejected", "approved", "pending")),
             "created_at": ts(h.rng), "decided_at": ts(h.rng)}
            for n in pick(h.rng, 6, 2)]
    h.put("bill_proposals", rows)
    # one offer both households hold: still asked here, answered there
    h.put("bill_proposals", [
        {"id": "prop-shared", "kind": "bill", "bill_type": "occurrence",
         "payee": "Invented Shared Utility", "amount": 25.0,
         "frequency": "MONTHLY", "interval": 1, "next_due": "2031-05-01",
         "evidence": {}, "llm_tag": None,
         "status": "pending" if receiving(h) else "rejected",
         "created_at": "2031-01-02T03:04:05+00:00",
         "decided_at": None if receiving(h) else
         "2031-01-03T03:04:05+00:00"}], null_first=False)


def b_import_batches(h):
    rows = [{"id": f"batch-{n}", "source": "csv",
             "account_id": h.rng.choice(h.ids["accounts"]),
             "filename": text(h.rng, "invented") + ".csv",
             "row_count": h.rng.randint(0, 50), "created_at": ts(h.rng)}
            for n in pick(h.rng, 5, 2)]
    h.put("import_batches", rows)


def _some_txns(h, k):
    return h.rng.sample(h.ids["transactions"], k)


def b_manual_categories(h):
    h.put("manual_categories", [
        {"transaction_id": t, "category": text(h.rng, "GROCERIES"),
         "set_at": ts(h.rng), "bill_id": None, "link_made": False,
         "link_prior": None} for t in _some_txns(h, 3)])


def b_reimbursements(h):
    a, b, c = _some_txns(h, 3)
    # the partial link goes on a charge with a cent of room and no full
    # pair: a restore cuts links that repay past their charge, so one that
    # did would not come back as it was archived
    d = next((t for t in h.ids["transactions"]
              if t not in (a, b, c) and abs(h.amount[t]) >= 0.01), None)
    rows = [{"expense_id": a, "reimburse_id": b, "partial": 0,
             "amount": money(h.rng), "created_at": ts(h.rng)}]
    if d is not None:
        rows.append({"expense_id": d, "reimburse_id": c, "partial": 1,
                     "amount": 0.01, "created_at": ts(h.rng)})
    h.put("reimbursements", rows)


def b_reimburse_flags(h):
    h.put("reimburse_flags", [{"txn_id": t, "partial": 1,
                               "expected": money(h.rng),
                               "flagged_at": ts(h.rng)}
                              for t in _some_txns(h, 2)])


def b_business_flags(h):
    h.put("business_flags", [{"txn_id": t, "flagged_at": ts(h.rng)}
                             for t in _some_txns(h, 2)])


def b_business_txn_class(h):
    h.put("business_txn_class", [
        {"txn_id": t, "bucket": h.rng.choice(("organizational", "operating")),
         "sched_c_line": "L8", "note": text(h.rng), "updated_at": ts(h.rng)}
        for t in _some_txns(h, 2)])


def b_transaction_notes(h):
    h.put("transaction_notes", [{"txn_id": t, "note": text(h.rng),
                                 "updated_at": ts(h.rng)}
                                for t in _some_txns(h, 3)])


def b_account_links(h):
    g = uid("group", h.rng.randrange(3))
    h.put("account_links", [{"group_id": g, "account_id": a, "home_rank": i,
                             "failover_alerted": None,
                             "created_at": ts(h.rng)}
                            for i, a in enumerate(h.ids["accounts"][:2])],
          null_first=False)


def b_account_adoptions(h):
    # an adoption belongs to a linked account (the first is the NULL row)
    acct = h.ids["accounts"][1]
    item = h.conn.execute("SELECT item_id FROM accounts WHERE id = %s",
                          (acct,)).fetchone()["item_id"]
    h.put("account_adoptions", [
        {"account_id": acct, "old_account_id": f"{acct}-before-{k}",
         "item_id": item, "adopted_at": ts(h.rng),
         "reconciled_at": ts(h.rng), "cutoff_date": day(h.rng),
         "history_from": day(h.rng), "live_from": day(h.rng),
         "guard_ledger": {"rows": 2}} for k in range(2)])


def b_account_sources(h):
    h.put("account_sources", [
        {"door": "manual",
         "source_hmac": "n:" + hashlib.sha256(
             f"{a}-{h.rng.random()}".encode()).hexdigest(),
         "account_id": a, "created_at": ts(h.rng)}
        for a in h.ids["accounts"][:2]])


def b_receipts(h):
    ns = pick(h.rng, 6, 3)
    txns = _some_txns(h, 2)
    rows = []
    for i, n in enumerate(ns):
        rows.append({"id": uid("receipt", n),
                     "txn_id": txns[i] if i < 2 else None,
                     "image": IMAGE + bytes([n]), "mime": "image/png",
                     "kind": "receipt", "status": "uploaded",
                     "parsed": {"total": 12.5, "merchant": text(h.rng)},
                     "error": None, "created_at": ts(h.rng),
                     "parsed_at": ts(h.rng), "matched_at": None,
                     "match_method": None, "unmatched_txn_ids": [],
                     "image_optimized": None, "optimized_mime": None,
                     # which slip it is: its own, unless a person said
                     # otherwise
                     "slip_id": None, "slip_source": None,
                     "slip_decided_at": None, "not_same_slip": []})
    rows[1]["match_method"] = "manual"
    # a person's "two purchases" between the second and the third
    rows[1]["not_same_slip"] = [rows[2]["id"]]
    rows[2]["not_same_slip"] = [rows[1]["id"]]
    rows[1]["matched_at"] = ts(h.rng)
    h.put("receipts", rows)
    h.ids["receipts"] = [r["id"] for r in rows]


def b_receipt_items(h):
    rows = []
    for rid in h.ids["receipts"]:
        for line in range(1, h.rng.randint(1, 3) + 1):
            rows.append({"receipt_id": rid, "line": line,
                         "description": text(h.rng, "invented apples"),
                         "qty": h.rng.choice((1.0, 2.5, None)),
                         "amount": money(h.rng), "tag": "food"})
    h.put("receipt_items", rows)


def b_entity_membership(h):
    rows = []
    for i, ent in enumerate(h.ids["business_entity"]):
        rows.append({"id": uid("member", h.rng.randrange(1000)),
                     "entity_id": ent, "member_name": text(h.rng, "Pat Example"),
                     "ownership_pct": Decimal("50.000"), "is_manager": True,
                     "created_at": ts(h.rng), "is_household": i == 0})
        rows.append({"id": uid("member", 1000 + h.rng.randrange(1000)),
                     "entity_id": ent, "member_name": text(h.rng, "Sam Example"),
                     "ownership_pct": Decimal("50.000"), "is_manager": False,
                     "created_at": ts(h.rng), "is_household": False})
    h.put("entity_membership", rows)


def b_equity_movement(h):
    ents = h.ids["business_entity"]
    t1, t2 = _some_txns(h, 2)
    h.put("equity_movement", [
        {"id": uid("equity", h.rng.randrange(1000)),
         "entity_id": ents[0], "kind": "draw",
         "amount": Decimal("250.00"), "date": day(h.rng),
         "member_id": None, "txn_id": None, "form": None,
         "note": None, "created_at": ts(h.rng)},
        {"id": uid("equity", 1000 + h.rng.randrange(1000)),
         "entity_id": ents[-1], "kind": "contribution",
         "amount": Decimal("999999999999.99"), "date": day(h.rng),
         "member_id": None, "txn_id": t1, "form": "cash",
         "note": text(h.rng), "created_at": ts(h.rng)},
        {"id": uid("equity", 2000 + h.rng.randrange(1000)),
         "entity_id": ents[0], "kind": "draw",
         # the smallest movement the page records: it refuses none that
         # is not positive, and so does the restore
         "amount": Decimal("0.01"), "date": day(h.rng),
         "member_id": None, "txn_id": t2, "form": None,
         "note": text(h.rng), "created_at": ts(h.rng)}])


def b_compliance_obligation(h):
    h.put("compliance_obligation", [
        {"id": uid("oblig", h.rng.randrange(1000) + 1000 * i),
         "entity_id": h.rng.choice(h.ids["business_entity"]),
         "title": text(h.rng, "Invented annual report"),
         "due_date": day(h.rng), "recurrence": "yearly",
         "fee": Decimal("25.00"), "url": "https://state.example.test/file",
         "note": text(h.rng), "created_at": ts(h.rng)} for i in range(2)])


def b_mileage_log(h):
    h.put("mileage_log", [
        {"id": uid("miles", h.rng.randrange(1000) + 1000 * i),
         "entity_id": h.rng.choice(h.ids["business_entity"]),
         "date": day(h.rng), "miles": Decimal("12.5"),
         "purpose": text(h.rng, "site visit"), "note": text(h.rng),
         "created_at": ts(h.rng)} for i in range(2)])


def b_vendor_1099(h):
    ent = h.ids["business_entity"][0]
    m = h.ids["merchants"][0]
    name = h.conn.execute("SELECT name FROM merchants WHERE id = %s",
                          (m,)).fetchone()["name"]
    h.put("vendor_1099", [
        {"id": uid("vendor", h.rng.randrange(1000)), "entity_id": ent,
         "merchant": text(h.rng, "Hand Typed Vendor"), "reportable": True,
         "tin_last4": "1234", "note": text(h.rng), "created_at": ts(h.rng),
         "merchant_id": None, "merged_from": []},
        {"id": uid("vendor", 1000 + h.rng.randrange(1000)), "entity_id": ent,
         "merchant": name, "reportable": False, "tin_last4": None,
         "note": None, "created_at": ts(h.rng), "merchant_id": m,
         "merged_from": [{"name": text(h.rng)}]}])


def b_transaction_splits(h):
    from oikonome.engine import splits
    done = 0
    for t in h.ids["transactions"]:
        amt = h.amount[t]
        if amt <= 1 or amt > 1e6 or not splits.splittable(h.conn, t):
            continue
        part = round(amt / 3, 2)
        splits.set_split(h.conn, t, [
            {"category": "GENERAL_MERCHANDISE", "amount": part},
            {"category": text(h.rng, "HOME_IMPROVEMENT"),
             "amount": round(amt - part, 2)}])
        done += 1
        if done == 2:
            break
    if not done:
        # every drawn charge was a refund or a money edge: give one a split
        t = h.ids["transactions"][0]
        h.conn.execute("UPDATE transactions SET amount = 30 WHERE id = %s",
                       (t,))
        h.amount[t] = 30.0
        splits.set_split(h.conn, t, [{"category": "A", "amount": 10},
                                     {"category": "B", "amount": 20}])
    # a store receipt's parts: the restore leaves these for the store match
    # to rebuild, so they are the one split it never lands
    store = next(t for t in reversed(h.ids["transactions"])
                 if not h.conn.execute(
                     "SELECT 1 FROM transaction_splits WHERE txn_id = %s",
                     (t,)).fetchone())
    h.conn.execute(
        """INSERT INTO transaction_splits (txn_id, line, category, amount,
                                          source)
           VALUES (%s, 1, 'FOOD_AND_DRINK', %s, 'amazon')""",
        (store, h.amount[store]))


def b_activity_log(h):
    h.put("activity_log", [
        {"id": uid("activity", h.rng.randrange(1000) + 1000 * i),
         "at": ts(h.rng), "actor_user_id": None,
         "actor": "pat@example.test", "kind": "category", "action": "set",
         "target": h.rng.choice(h.ids["transactions"]),
         "label": text(h.rng), "summary": text(h.rng, "set a category"),
         "detail": {"ids": [h.ids["transactions"][0]], "why": text(h.rng)}}
        for i in range(3)], null_first=False)
    # the nullable cells, blank
    h.conn.execute(
        """INSERT INTO activity_log (id, at, actor, kind, action, label,
                                     summary)
           VALUES (%s, %s, 'sam@example.test', 'note', 'set', '', 'a note')""",
        (uid("activity", 5000 + h.rng.randrange(1000)), ts(h.rng)))


def b_alerts_log(h):
    rows = []
    for i in range(3):
        first, last = sorted((day(h.rng), day(h.rng)))
        rows.append({"kind": h.rng.choice(("low_balance", "large_charge")),
                     "severity": "warn",
                     "message": text(h.rng, f"Invented alert {i}"),
                     "first_seen": first, "last_seen": last,
                     "active": i % 2, "dismissed": 1 if i < 2 else 0,
                     "mute_changed_at": None, "seen_at": None})
    h.put("alerts_log", rows, null_first=False)


def b_liabilities(h):
    h.put("liabilities", [{"account_id": a, "as_of": ts(h.rng),
                           "raw": {"apr": 19.99}}
                          for a in h.ids["accounts"][1::2]])


def b_holdings(h):
    h.put("holdings", [
        {"account_id": h.ids["accounts"][0], "symbol": s,
         "name": text(h.rng, "Invented Fund"), "quantity": 2.5,
         "price": money(h.rng), "value": money(h.rng), "as_of": ts(h.rng),
         "raw": {"k": text(h.rng)}} for s in ("INVT", "FAKE")])


def b_crypto_holdings(h):
    h.put("crypto_holdings", [
        {"account_id": h.ids["accounts"][-1], "currency": c,
         "quantity": 1.5, "native_usd": money(h.rng), "as_of": ts(h.rng),
         "raw": {"k": "v"}} for c in ("XYZ", "ABC")])


def b_networth_recorded(h):
    h.put("networth_recorded", [
        {"month": f"2025-{m + 1:02d}", "total": money(h.rng),
         "source": "manual"} for m in pick(h.rng, 12, 2)])


def b_networth_snapshot(h):
    h.put("networth_snapshot", [
        {"date": dt.date(2025, 6, d + 1), "total": money(h.rng),
         "by_class": {"cash": 10.5, "debt": -0.01}}
        for d in pick(h.rng, 28, 2)])


def b_forecast_nights(h):
    h.put("forecast_nights", [
        {"as_of": dt.date(2025, 6, d + 1), "recorded_at": ts(h.rng),
         "model": "w3", "checking_id": h.rng.choice(h.ids["accounts"]),
         "start_checking": money(h.rng),
         "fresh": True, "restored": True,
         "walks": {"usual": {"at": [100, 200, 300, 400, 500], "min": 50,
                             "min_day": 3, "neg_day": None}},
         "pace": {"daily": 42.5}, "inputs": {"events": []}}
        for d in pick(h.rng, 28, 2)])


def b_budget_snapshots(h):
    h.put("budget_snapshots", [
        {"year": 2025, "month": m + 1,
         "config": {"food_monthly": 900, "other_monthly": 700.5},
         "bills": {"rows": [{"payee": text(h.rng), "amount": 80.0,
                             "raw": {}}]},
         "captured_at": ts(h.rng), "source": "close"}
        for m in pick(h.rng, 12, 2)])


def b_income_annual(h):
    h.put("income_annual", [
        {"year": y, "total_income": money(h.rng), "agi": money(h.rng),
         "taxable_income": money(h.rng), "tax_paid": money(h.rng),
         "wages": money(h.rng), "primary_wages": None,
         "investment_income": 12.5, "capital_gain": -0.01,
         "spouse_wages": None, "ss_earnings": None,
         "medicare_earnings": None, "filing_status": "joint",
         "joint": 1, "source": "manual", "note": text(h.rng)}
        for y in (2022, 2024)])


def b_income_uploads(h):
    # one stored return that wrote the household's yearly figures, one
    # removed inside the put-back window holding a form. Its own random
    # stream, so the builders after it draw what they drew before it
    rng = random.Random(f"income-uploads-{h.seed}")
    uid = lambda: str(uuid.UUID(int=rng.getrandbits(128), version=4))  # noqa: E731
    # two more as the migration makes them: the same kind, no file name,
    # one moment — told apart by their uid alone
    same = ts(rng)
    h.put("income_uploads", [
        {"id": 3, "uid": uid(), "kind": "transcript",
         "filename": text(rng, "invented-transcript") + ".pdf",
         "stored_at": ts(rng), "stored_by": None, "removed_at": None,
         "removed_by": None, "held_documents": None},
        {"id": 5, "uid": uid(), "kind": "w2", "filename": None,
         "stored_at": ts(rng),
         "stored_by": None, "removed_at": ts(rng), "removed_by": None,
         "held_documents": [{"id": 9, "year": 2021, "form": "W-2",
                             "owner": "primary",
                             "payer": text(rng, "Invented Works"),
                             "ein_last4": None, "primary_amount": 100.0,
                             "amounts": None, "notes": None}]},
        *({"id": i, "uid": uid(), "kind": "document", "filename": None,
           "stored_at": same, "stored_by": None, "removed_at": None,
           "removed_by": None, "held_documents": None} for i in (7, 8)),
    ], null_first=False)


def b_income_upload_values(h):
    # the figures the stored return wrote are the ones the year holds, so
    # working each year out again moves nothing
    h.put("income_upload_values", [
        {"upload_id": 3, "year": r["year"],
         "fields": {"total_income": r["total_income"], "agi": r["agi"]}}
        for r in h.conn.execute(
            "SELECT year, total_income, agi FROM income_annual "
            "WHERE total_income IS NOT NULL AND agi IS NOT NULL "
            "ORDER BY year").fetchall()], null_first=False)


def b_income_documents(h):
    payer = text(h.rng, "Invented Employer")
    box1 = round(h.rng.uniform(10000, 90000), 2)
    base = {"year": 2024, "form": "W-2", "payer": payer, "ein": None,
            "ein_last4": "4242", "primary_amount": box1,
            "amounts": {"box2": 1200.5}, "notes": None}
    ids = pick(h.rng, 30, 5)
    h.put("income_documents", [
        # the same form twice, one per spouse: two documents, never one
        {**base, "id": ids[0], "owner": "primary"},
        {**base, "id": ids[1], "owner": "spouse"},
        {"id": ids[2], "year": h.rng.choice((2023, 2024, 2025)),
         "form": "W-2", "owner": "primary",
         "payer": text(h.rng, "Other Employer"), "ein": None,
         "ein_last4": None, "primary_amount": 0.01, "amounts": None,
         "notes": text(h.rng),
         # a payer a person typed keeps its mark
         "payer_named_by_person": True},
        {"id": ids[3], "year": 2024, "form": "1099-INT", "owner": "spouse",
         "payer": text(h.rng, "Invented Bank"), "ein": None,
         "ein_last4": None, "primary_amount": 12.34,
         "amounts": {"box1": 12.34}, "notes": None},
        {"id": ids[4], "year": 2023, "form": "1099-DIV", "owner": "primary",
         "payer": None, "ein": None, "ein_last4": None,
         "primary_amount": None, "amounts": None, "notes": None},
    ], null_first=False)


def b_merchant_canonical(h):
    rows = [{"raw_merchant": text(h.rng, f"INVENTED RAW {i}"),
             "canonical": text(h.rng),
             "method": "manual" if i else "llm", "as_of": ts(h.rng),
             "merchant_id": h.rng.choice(h.ids["merchants"])}
            for i in range(3)]
    h.put("merchant_canonical", rows)
    h.ids["aliases"] = [r["raw_merchant"] for r in rows]
    # one descriptor both households alias: a machine's here, a person's
    # in the archive
    h.put("merchant_canonical", [
        {"raw_merchant": "INVENTED SHARED RAW", "canonical":
         "Invented Machine Name" if receiving(h) else "Invented Own Name",
         "method": "llm" if receiving(h) else "manual",
         "as_of": "2031-01-02T03:04:05+00:00", "merchant_id": None}],
        null_first=False)


def b_merchant_renames(h):
    # ids come from the instance-wide sequence, as every writer and a
    # restore take them; fixed ids would collide with rows a restore has
    # already numbered past
    h.put("merchant_renames", [
        {"raw_merchant": h.rng.choice(h.ids["aliases"]),
         "from_canonical": text(h.rng) if i else None,
         "to_canonical": text(h.rng), "at": ts(h.rng),
         "rules": {"moved": [text(h.rng)]} if i else None}
        for i in range(3)], null_first=False)


def b_merchant_categories(h):
    h.put("merchant_categories", [
        {"merchant": text(h.rng, f"Rule Merchant {i}"),
         "category_primary": "FOOD_AND_DRINK",
         "source": "user" if i else "llm", "classified_at": ts(h.rng),
         "disabled": i == 2} for i in range(3)], null_first=False)
    # one merchant both households have a rule for: the model's here, the
    # person's in the archive
    h.put("merchant_categories", [
        {"merchant": "Invented Shared Merchant",
         "category_primary": "SHOPPING" if receiving(h) else "TRAVEL",
         "source": "llm" if receiving(h) else "user",
         "classified_at": "2031-01-02T03:04:05+00:00", "disabled": False}],
        null_first=False)


def b_merchant_merge_proposals(h):
    from oikonome.engine.merchant_merge import _sig
    a, b, c = h.ids["merchants"][:3]
    h.put("merchant_merge_proposals", [
        {"id": _sig(a, b), "from_merchant_id": a, "into_merchant_id": b,
         "evidence": {"score": 0.5}, "status": "rejected",
         "created_at": ts(h.rng), "decided_at": ts(h.rng)},
        {"id": _sig(b, c), "from_merchant_id": c, "into_merchant_id": b,
         "evidence": {}, "status": "pending", "created_at": ts(h.rng),
         "decided_at": None}], null_first=False)


def b_amazon_orders(h):
    rows = [{"dedup_key": f"amz-{n}", "account": "main", "date": day(h.rng),
             "amount": money(h.rng), "payee": "Amazon",
             "seller": text(h.rng), "memo": text(h.rng), "category": "",
             "category_source": "rules", "order_number": f"112-{n}",
             "is_refund": 0, "payment_method": "card",
             "items_json": [{"title": text(h.rng), "price": 4.5}],
             "inserted_at": ts(h.rng)} for n in pick(h.rng, 8, 2)]
    h.put("amazon_orders", rows)
    h.ids["amazon"] = [r["dedup_key"] for r in rows]


def b_amazon_matches(h):
    h.put("amazon_matches", [
        {"transaction_id": t, "dedup_key": k, "matched_at": ts(h.rng)}
        for t, k in zip(_some_txns(h, 2), h.ids["amazon"])])


def b_amazon_summaries(h):
    h.put("amazon_summaries", [
        {"dedup_key": k, "summary": text(h.rng, "invented apples"),
         "created_at": ts(h.rng)} for k in h.ids["amazon"]],
        null_first=False)


def b_costco_receipts(h):
    rows = [{"dedup_key": f"cst-{n}", "account": "main", "date": day(h.rng),
             "amount": money(h.rng), "receipt_type": "warehouse",
             "warehouse": text(h.rng), "category": "",
             "category_source": "rules", "summary": text(h.rng),
             "is_refund": 0, "payment_method": "card",
             "items_json": [{"item": text(h.rng), "amount": 3.25}],
             "inserted_at": ts(h.rng)} for n in pick(h.rng, 8, 2)]
    h.put("costco_receipts", rows)
    h.ids["costco"] = [r["dedup_key"] for r in rows]


def b_costco_matches(h):
    h.put("costco_matches", [
        {"transaction_id": t, "dedup_key": k, "matched_at": ts(h.rng)}
        for t, k in zip(_some_txns(h, 2), h.ids["costco"])])


def b_tenant_settings(h):
    from oikonome.engine import budget
    # amounts as a restore stores them (a decimal, never an integer)
    budget.save_config(h.conn, {"food_monthly": 1000.0 + h.seed,
                                "other_monthly": 1000.5,
                                "dynamic_variable_budget": False})


def b_recipient_invites(h):
    """A recipient who said no: the one answer every archive carries (an
    acceptance travels only for the household's own members, and an
    unanswered invite not at all). Control plane: written as admin."""
    admin = tenancy.admin_connect()
    try:
        admin.execute(
            """INSERT INTO recipient_invites (tenant_id, email, created_at,
                                              declined_at)
               VALUES (%s, %s, %s, %s)""",
            (tenant_of(h.conn), f"rt-{h.rng.randrange(5)}@example.test",
             ts(h.rng), ts(h.rng)))
    finally:
        admin.close()


BUILDERS = {
    "items": b_items,
    "business_entity": b_business_entity,
    "merchants": b_merchants,
    "accounts": b_accounts,
    "transactions": b_transactions,
    "bills": b_bills,
    "bill_proposals": b_bill_proposals,
    "import_batches": b_import_batches,
    "manual_categories": b_manual_categories,
    "reimbursements": b_reimbursements,
    "reimburse_flags": b_reimburse_flags,
    "business_flags": b_business_flags,
    "business_txn_class": b_business_txn_class,
    "transaction_notes": b_transaction_notes,
    "account_links": b_account_links,
    "account_adoptions": b_account_adoptions,
    "account_sources": b_account_sources,
    "receipts": b_receipts,
    "receipt_items": b_receipt_items,
    "entity_membership": b_entity_membership,
    "equity_movement": b_equity_movement,
    "compliance_obligation": b_compliance_obligation,
    "mileage_log": b_mileage_log,
    "vendor_1099": b_vendor_1099,
    "transaction_splits": b_transaction_splits,
    "activity_log": b_activity_log,
    "alerts_log": b_alerts_log,
    "liabilities": b_liabilities,
    "holdings": b_holdings,
    "crypto_holdings": b_crypto_holdings,
    "networth_recorded": b_networth_recorded,
    "networth_snapshot": b_networth_snapshot,
    "budget_snapshots": b_budget_snapshots,
    "income_annual": b_income_annual,
    "income_uploads": b_income_uploads,
    "income_upload_values": b_income_upload_values,
    "income_documents": b_income_documents,
    "merchant_canonical": b_merchant_canonical,
    "merchant_renames": b_merchant_renames,
    "merchant_categories": b_merchant_categories,
    "merchant_merge_proposals": b_merchant_merge_proposals,
    "amazon_orders": b_amazon_orders,
    "amazon_matches": b_amazon_matches,
    "amazon_summaries": b_amazon_summaries,
    "costco_receipts": b_costco_receipts,
    "costco_matches": b_costco_matches,
    "tenant_settings": b_tenant_settings,
    "recipient_invites": b_recipient_invites,
    # last, so the draws it takes move no other table's fixture
    "forecast_nights": b_forecast_nights,
}

# Tables the export carries and the restore never loads, each with why.
# A table the export gains lands here or in BUILDERS, or the coverage
# guard fails.
RECORDS_ONLY: dict[str, str] = {}


def build(conn, seed: int) -> Household:
    h = Household(conn, seed)
    for b in BUILDERS.values():
        b(h)
    settle(conn)
    return h


# Each person's W-2 wage figure as the W-2s set it (specs/w2-wages.md): the
# sum of box 1 over their read W-2s, into a figure nothing else wrote.
_W2_FIGURES = """
    WITH s AS (
        SELECT year,
               SUM(primary_amount) FILTER (WHERE owner = 'primary') AS p,
               SUM(primary_amount) FILTER (WHERE owner = 'spouse') AS sp
          FROM income_documents
         WHERE form = 'W-2' AND primary_amount IS NOT NULL
           AND primary_amount <> 0
         GROUP BY year)
    INSERT INTO income_annual (year, primary_wages, primary_wages_w2,
                               spouse_wages, spouse_wages_w2, source)
    SELECT year, p, p, sp, sp, 'w2' FROM s
    ON CONFLICT (tenant_id, year) DO UPDATE SET
        primary_wages = EXCLUDED.primary_wages,
        primary_wages_w2 = EXCLUDED.primary_wages_w2,
        spouse_wages = EXCLUDED.spouse_wages,
        spouse_wages_w2 = EXCLUDED.spouse_wages_w2,
        source = COALESCE(income_annual.source || '+w2', 'w2')"""


def settle(conn) -> None:
    """What a restore runs once its rows have landed, run on the household
    first: a source the nightly passes have already seen, so a restore of
    it reproduces it rather than finishing their work."""
    from oikonome.engine import (activity, bills, categories,
                                 merchant_identity, taxdocs)
    from oikonome.sync import account_sources
    conn.execute(_W2_FIGURES)
    # every form and figure the stored list cannot explain is given an
    # upload, as the nightly sweep and the restore give one
    taxdocs.adopt_household(conn)
    categories.backfill_override_source(conn)
    account_sources.backfill(conn)
    bills.cut_detected_keys(conn)
    activity.cut_stored_names(conn)
    merchant_identity.resolve(conn)
    # which slip each receipt is: decided on upload in a live household
    from oikonome.engine import receipt_identity
    receipt_identity.decide_pending(conn)


# --------------------------------------------------------------------------
# content the restore deliberately rewrites on the way in. Built into a
# household of its own: the round trip there is checked for idempotence
# only (a second restore, or one into its own household, writes nothing).

def rewritten_rows(h) -> None:
    """A card number in every free-text cell a door cuts, a descriptor
    past the longest real one, a blank cell the restore reads as a
    default."""
    number = "4111 1111 1111 1111"
    x = h.conn.execute
    acct = h.ids["accounts"][0]
    x("""INSERT INTO transactions (id, account_id, date, amount, name,
                                   merchant_name, removed, raw)
         VALUES ('rt-rw-1', %s, '2025-03-03', 12.5, %s, %s, 0, '{}')""",
      (acct, f"CARD {number} INVENTED", "X" * 900))
    x("""INSERT INTO merchants (id, name, name_source, kind)
         VALUES (%s, %s, 'layer1', 'merchant')""",
      (uid("merchant-rw", 1), f"Invented {number}"))
    # held by a charge, as every merchant the resolver keeps is
    x("UPDATE transactions SET merchant_id = %s WHERE id = 'rt-rw-1'",
      (uid("merchant-rw", 1),))
    x("""INSERT INTO bills (id, type, payee, amount, frequency, active, raw)
         VALUES ('rec:rt-rw-bill', 'bill', %s, 10, 'MONTHLY', 1, '{}')""",
      (f"Invented Loan {number}",))
    x("""INSERT INTO alerts_log (kind, severity, message, first_seen,
                                 last_seen, active, dismissed)
         VALUES ('large_charge', 'warn', %s, '2025-01-02', '2025-01-03', 0, 1)""",
      (f"Charge on {number}",))
    x("""INSERT INTO merchant_canonical (raw_merchant, canonical, method)
         VALUES (%s, %s, 'llm')""",
      (f"RAW {number}", f"Canonical {number}"))
    # an alias with no method: the restore reads the blank cell as a
    # person's alias
    x("""INSERT INTO merchant_canonical (raw_merchant, canonical, method)
         VALUES ('INVENTED RAW UNSAID', 'Invented Unsaid', NULL)""")
    x("""INSERT INTO activity_log (id, at, actor, kind, action, label,
                                   summary)
         VALUES (%s, '2025-02-02', 'pat@example.test', 'bill', 'set', %s, %s)""",
      (uid("activity-rw", 1), f"Bill {number}", f"renamed {number}"))
    x("""INSERT INTO import_batches (id, source, account_id, filename)
         VALUES ('batch-rw', 'csv', %s, %s)""", (acct, f"stmt-{number}.csv"))


# --------------------------------------------------------------------------
# identity: what the restore counts as "the same row" today

class Key:
    """One unique notion of a row: the columns that name it, an optional
    predicate saying which rows it covers, and how a SOURCE row's values
    read once the restore has followed its maps (merchants folded onto the
    destination's own)."""

    def __init__(self, *cols, where=None, follow=None, width=None):
        self.cols, self.where, self.follow = cols, where, follow
        # the leading columns that ARE the identity; any after them are
        # what `follow` needs to say where the row went
        self.width = len(cols) if width is None else width

    def ident(self, row):
        return None if row is None else row[:self.width]

    def sql(self, table, lands=None):
        conds = [c for c in (self.where, lands) if c]
        return (f"SELECT {', '.join(self.cols)} FROM {table}"
                + (f" WHERE {' AND '.join(conds)}" if conds else ""))


def _follow_merchants(*at):
    def follow(row, merchant_map):
        return tuple(merchant_map.get(v, v) if i in at else v
                     for i, v in enumerate(row))
    return follow


def _follow_merge_pair(row, merchant_map):
    from oikonome.engine.merchant_merge import _sig
    _pid, frm, into = row
    if frm not in merchant_map and into not in merchant_map:
        return row
    frm, into = merchant_map.get(frm, frm), merchant_map.get(into, into)
    if frm == into:
        return None            # the restore reports it merged
    return (_sig(frm, into), frm, into)


_DOC = ("year", "form", "payer", "ein_last4", "primary_amount",
        "amounts::text", "notes")


# Each landed table's identity as the inline restore decided it before the
# landers, written down by hand (sync/restore.py as of the round-trip
# test's first version). A lander's identity is checked against it, so a
# lander cannot change what "the same row" means and pass its own test.
FROZEN_IDENTITY: dict[str, tuple[str, ...]] = {
    "bill_proposals": ("id",),
    "import_batches": ("id",),
    "manual_categories": ("transaction_id",),
    "reimbursements": ("expense_id", "reimburse_id"),
    "reimburse_flags": ("txn_id",),
    "business_flags": ("txn_id",),
    "business_txn_class": ("txn_id",),
    "transaction_notes": ("txn_id",),
    "account_links": ("account_id",),
    "account_adoptions": ("account_id", "old_account_id"),
    "account_sources": ("door", "source_hmac", "account_id"),
    "receipts": ("id",),
    "receipt_items": ("receipt_id", "line"),
    "compliance_obligation": ("id",),
    "mileage_log": ("id",),
    "activity_log": ("id",),
    "liabilities": ("account_id",),
    "holdings": ("account_id", "symbol"),
    "crypto_holdings": ("account_id", "currency"),
    "networth_recorded": ("month",),
    "networth_snapshot": ("date",),
    "forecast_nights": ("as_of",),
    "merchant_canonical": ("raw_merchant",),
    "merchant_categories": ("merchant",),
    "merchant_merge_proposals": ("id",),
    "amazon_orders": ("dedup_key",),
    "amazon_matches": ("transaction_id",),
    "amazon_summaries": ("dedup_key",),
    "costco_receipts": ("dedup_key",),
    "costco_matches": ("transaction_id",),
    "business_entity": ("id",),
    "entity_membership": ("id",),
    "equity_movement": ("id",),
    "vendor_1099": ("id",),
    "bills": ("id",),
    "budget_snapshots": ("year", "month"),
    "merchant_renames": ("raw_merchant", "from_canonical", "to_canonical",
                         "at"),
    "transaction_splits": ("txn_id",),
    "income_annual": ("year",),
    # new with the stored list (migration 231): never restored inline
    "income_uploads": ("uid",),
    "income_upload_values": ("upload_id", "year"),
    "income_documents": ("year", "form", "payer", "ein_last4",
                         "primary_amount", "amounts", "notes"),
    "items": ("id",),
    "merchants": ("id",),
    "accounts": ("id",),
    "transactions": ("id",),
    "alerts_log": ("kind", "message"),
}


# Where a source row's identity reads differently once the restore has
# followed its maps, the extra columns that say where it went: the key's
# leading columns are still the lander's identity.
_FOLLOW = {
    # the pair, whichever way round it was offered
    "merchant_merge_proposals": (("from_merchant_id::text",
                                  "into_merchant_id::text"),
                                 _follow_merge_pair),
    # a merchant folded onto the destination's own is held under its id
    "merchants": ((), _follow_merchants(0)),
}


# The other unique keys a landed table keeps beside its identity, as the
# round-trip test checked them before the landers: a row held under one
# of them is held.
_ALSO = {
    "merchants": [Key("plaid_entity_id",
                      where="plaid_entity_id IS NOT NULL")],
    "equity_movement": [
        Key("txn_id", "kind", where="txn_id IS NOT NULL"),
        # the one fronted movement a charge may carry
        Key("txn_id", where="txn_id IS NOT NULL AND kind IN "
                            "('contribution', 'reimbursement')")],
    "vendor_1099": [
        Key("entity_id::text", "merchant"),
        Key("entity_id::text", "merchant_id::text",
            where="merchant_id IS NOT NULL", follow=_follow_merchants(1))],
}


# Where the test reads an identity in its own spelling elsewhere (the
# documents' owners are counted by `_DOC`), the key reads it the same way.
_READ_AS = {"income_documents": _DOC}


def _from_lander(table: str) -> tuple[str, list[Key]]:
    """The lander's own identity, read as text so a uuid, a date and a
    string compare the same way on either side."""
    lander = _LANDERS[table]
    mode = {Counted: "counted", Group: "group"}.get(type(lander.presence),
                                                    "one")
    cols = _READ_AS.get(table) or tuple(f"{c}::text"
                                        for c in lander.identity.columns)
    assert tuple(c.split("::")[0] for c in cols) == \
        lander.identity.columns, table
    if table in _FOLLOW:
        extra, follow = _FOLLOW[table]
        return (mode, [Key(*cols, *extra, width=len(cols), follow=follow),
                       *_ALSO.get(table, [])])
    return (mode, [Key(*cols), *_ALSO.get(table, [])])


# table -> (mode, [Key, ...]). The first key is the one the multiset is
# taken over; every key is unique in a household unless the mode is
# "counted". Modes:
#   one     — at most one row per key; a restore into another household
#             keeps every key either side held
#   counted — the W-2 rule (specs/w2-wages.md): per key, the destination
#             ends with max(source, destination) copies
#   group   — rows travel as one group per key (a charge's split): the
#             destination keeps its own group, else takes the source's
#   single  — the settings document: one row, merged, never counted
IDENTITY: dict[str, tuple[str, list[Key]]] = {
    "tenant_settings": ("single", []),
    "recipient_invites": ("one", [Key("email")]),
}
# every table a lander lands takes its identity from the lander
IDENTITY.update({t: _from_lander(t) for t in _LANDERS})

# Which archived rows a restore lands. The rest have one stated outcome
# ("rebuilt": a store receipt's split parts, an alert nobody dismissed;
# an invite nobody answered lands nothing).
LANDS = {
    "alerts_log": "dismissed = 1",
    "transaction_splits": "source = 'user'",
    "recipient_invites": "declined_at IS NOT NULL",
}

# Control-plane tables: read over the admin connection, scoped by hand.
CONTROL = {"recipient_invites"}


def _norm(v):
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, (dt.datetime, dt.date, Decimal, uuid.UUID)):
        return str(v)
    if isinstance(v, (bytes, memoryview)):
        return "sha:" + hashlib.sha256(bytes(v)).hexdigest()[:16]
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True, default=str)
    return v


def tenant_of(conn) -> str:
    return conn.execute(
        "SELECT current_setting('app.tenant_id', true) AS t").fetchone()["t"]


def query(conn, table, sql):
    if table in CONTROL:
        admin = tenancy.admin_connect()
        try:
            glue = " AND " if " WHERE " in sql else " WHERE "
            return admin.execute(sql + glue + "tenant_id = %s",
                                 (tenant_of(conn),)).fetchall()
        finally:
            admin.close()
    return conn.execute(sql).fetchall()


def key_rows(conn, table, key: Key, *, landing=False) -> list[tuple]:
    """Every row's values under `key`, whole (see `Key.ident`)."""
    lands = LANDS.get(table) if landing else None
    return [tuple(_norm(v) for v in r.values())
            for r in query(conn, table, key.sql(table, lands))]


def identities(conn, table, *, landing=False) -> Counter:
    _mode, keys = IDENTITY[table]
    if not keys:
        return Counter()
    return Counter(keys[0].ident(r)
                   for r in key_rows(conn, table, keys[0], landing=landing))


# --------------------------------------------------------------------------
# golden digests

# Columns whose value a restore sets for itself: a fresh serial, the
# tenant. Timestamps a restore stamps with now() are recognised by value
# (see `_VOLATILE_SINCE`), so a carried timestamp is still checked.
VOLATILE = {
    "*": {"tenant_id", "search_text"},
    "alerts_log": {"id"},
    # a bill's uid is drawn by the database when the builder writes it
    "bills": {"uid"},
    "merchant_renames": {"id"},
    "income_documents": {"id", "upload_id"},
    # an upload's id is renumbered when taken, and what names it follows;
    # one the settle or a restore makes for an unexplained figure or form
    # is given a fresh uid by the database
    "income_uploads": {"id", "uid"},
    "income_upload_values": {"upload_id"},
    "recipient_invites": {"id", "invited_by"},
}

# every timestamp the builders write is in 2025; one later than this was
# written by the run itself
_VOLATILE_SINCE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def _digest_cell(v, aliases):
    if isinstance(v, dt.datetime):
        aware = v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)
        if aware >= _VOLATILE_SINCE:
            return "<now>"
    v = _norm(v)
    if isinstance(v, str) and aliases:
        for raw, label in aliases.items():
            if raw in v:
                v = v.replace(raw, label)
    return v


def minted_ids(conn) -> dict[str, str]:
    """Merchants a pass here minted (the identity resolver, from the
    ledger), by a random id: each gets a label read from what it is, so a
    digest does not change with the id the database drew. The builders'
    own ids are name-based (uuid5) and stay as they are."""
    rows = conn.execute(
        "SELECT id::text AS id, name, plaid_entity_id, merged_into::text AS m"
        " FROM merchants").fetchall()
    # Two minted merchants can share a name (the resolver's "?" for a
    # charge with no readable payee); told apart by what points at them,
    # never by the order the table returns them in
    refs: dict[str, list] = {}
    for r in conn.execute(
            "SELECT merchant_id::text AS m, 't:' || id AS ref"
            "  FROM transactions WHERE merchant_id IS NOT NULL"
            " UNION ALL SELECT merchant_id::text, 'a:' || raw_merchant"
            "  FROM merchant_canonical WHERE merchant_id IS NOT NULL"
            " UNION ALL SELECT merged_into::text, 'm:' || name"
            "  FROM merchants WHERE merged_into IS NOT NULL"
            " UNION ALL SELECT parent_id::text, 'p:' || name"
            "  FROM merchants WHERE parent_id IS NOT NULL").fetchall():
        refs.setdefault(r["m"], []).append(r["ref"])
    minted = sorted((r for r in rows if uuid.UUID(r["id"]).version != 5),
                    key=lambda r: (r["name"], r["plaid_entity_id"] or "",
                                   r["m"] or "",
                                   sorted(refs.get(r["id"], []))))
    out, seen = {}, Counter()
    for r in minted:
        base = hashlib.sha256(f"{r['name']}|{r['plaid_entity_id']}".encode()
                              ).hexdigest()[:10]
        seen[base] += 1
        out[r["id"]] = f"minted:{base}:{seen[base]}"
    return out


def table_rows(conn, table, aliases=None) -> list[str]:
    skip = VOLATILE["*"] | VOLATILE.get(table, set())
    aliases = minted_ids(conn) if aliases is None else aliases
    return sorted(
        json.dumps({k: _digest_cell(v, aliases) for k, v in r.items()
                    if k not in skip}, sort_keys=True, default=str)
        for r in query(conn, table, f"SELECT * FROM {table}"))


def table_digest(conn, table, aliases=None) -> str:
    return hashlib.sha256("\n".join(
        table_rows(conn, table, aliases)).encode()).hexdigest()[:20]


def household_digest(conn, tables) -> dict[str, str]:
    aliases = minted_ids(conn)
    return {t: table_digest(conn, t, aliases) for t in sorted(tables)}
