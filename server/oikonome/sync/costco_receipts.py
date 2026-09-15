"""the Costco-receipts push door — a host-side script pulls warehouse,
gas-station and costco.com receipts (line items with the register's
abbreviated descriptions, departments and amounts, already categorized
per item) and POSTs them here with a script token, like the Amazon door.
This is the product-side half: upsert into costco_receipts, then run the
app's own receipt↔transaction matcher.

Payload: {receipts: [{dedup_key, account?, date, amount, receipt_type?,
warehouse?, category?, summary?, is_refund?, payment_method?, items?}]}

Unlike Amazon orders, a receipt's categories are decided entirely by the
collector's rules (department map + keyword list) — no model refines them
here — so a re-push REFRESHES a row's items, category and summary: a
better department map on the host reaches every receipt already stored.
"""

from __future__ import annotations

import math

from ..engine.compat import as_date, jsonb

MAX_RECEIPTS = 10_000
RECEIPT_TYPES = ("warehouse", "gas", "online")


def import_receipts(conn, receipts: list[dict]) -> dict:
    if not isinstance(receipts, list) or not receipts:
        raise ValueError("payload needs a non-empty receipts list")
    if len(receipts) > MAX_RECEIPTS:
        raise ValueError(f"too many receipts (max {MAX_RECEIPTS})")

    def count():
        return conn.execute(
            "SELECT count(*) n FROM costco_receipts").fetchone()["n"]

    before = count()
    accepted = refreshed = 0
    with conn.transaction():
        for r in receipts:
            if not isinstance(r, dict):
                continue
            if not r.get("dedup_key") or not r.get("date"):
                continue
            # parse once, skip junk: a malformed date or a non-numeric
            # amount from a scripted caller must skip ITS row, never 500
            # the whole nightly batch
            try:
                rdate = as_date(r["date"])
            except (ValueError, TypeError):
                continue
            if rdate is None:
                continue
            try:
                if isinstance(r.get("amount"), bool):
                    raise TypeError
                amount = float(r["amount"])
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(amount):
                continue
            rtype = str(r.get("receipt_type") or "warehouse")
            if rtype not in RECEIPT_TYPES:
                rtype = "warehouse"
            items = r.get("items") or []
            if not isinstance(items, list):
                items = []
            accepted += 1
            cur = conn.execute(
                """INSERT INTO costco_receipts (dedup_key, account, date,
                       amount, receipt_type, warehouse, category,
                       category_source, summary, is_refund, payment_method,
                       items_json, inserted_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,'rules',%s,%s,%s,%s,now())
                   ON CONFLICT (tenant_id, dedup_key) DO UPDATE SET
                       items_json = EXCLUDED.items_json,
                       category = EXCLUDED.category,
                       summary = EXCLUDED.summary,
                       warehouse = EXCLUDED.warehouse,
                       payment_method = EXCLUDED.payment_method
                   WHERE costco_receipts.category_source = 'rules'
                     AND (costco_receipts.items_json IS DISTINCT FROM EXCLUDED.items_json
                          OR costco_receipts.category <> EXCLUDED.category
                          OR costco_receipts.summary <> EXCLUDED.summary
                          OR costco_receipts.warehouse <> EXCLUDED.warehouse
                          OR costco_receipts.payment_method <> EXCLUDED.payment_method)
                   RETURNING (xmax = 0) AS inserted""",
                (str(r["dedup_key"]), str(r.get("account") or "main"),
                 rdate, amount, rtype, str(r.get("warehouse") or "")[:80],
                 str(r.get("category") or "")[:60],
                 str(r.get("summary") or "")[:300],
                 int(bool(r.get("is_refund"))),
                 str(r.get("payment_method") or "")[:60], jsonb(items)))
            row = cur.fetchone()
            if row is not None and not row["inserted"]:
                refreshed += 1

    after = count()
    changed = (after - before) or refreshed
    if changed:
        from ..engine import costco_match, llm_categorize
        # the matcher rebuilds costco_matches DELETE-then-reinsert from a
        # snapshot; the nightly sweep and the hourly sync already serialize
        # the store chains on this per-tenant lock, and this push is the
        # third door into it. try-lock, not wait: this is the synchronous
        # body of a collector's request, and the nightly sweep matches
        # unconditionally anyway.
        with llm_categorize.amazon_chain_lock(conn) as got:
            match = costco_match.run_match(conn) if got else {
                "skipped": "the store match chain is already running; "
                           "the next sweep matches these receipts"}
    else:
        match = {"skipped": "no receipt changes in this push"}

    # the heartbeat says the collector RAN AND WAS UNDERSTOOD: a push whose
    # every row was rejected must not read as fresh
    from . import heartbeat
    if not receipts or accepted:
        heartbeat.stamp(conn, "costco-receipts", rows=accepted,
                        label="Costco receipts")

    return {"ok": True, "pushed": len(receipts), "new": after - before,
            "refreshed": refreshed,
            "receipts": {"before": before, "after": after},
            "costco_match": match}
