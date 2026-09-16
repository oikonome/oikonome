"""The Amazon-orders push door. A host-side script scrapes
order history (order numbers, item titles, per-order totals, refunds)
and POSTs it here with a script token, like the plan-CSV/Coinbase
endpoints. This is the product-side half: upsert into amazon_orders
(date-drift refresh, items-json patch), then run the app's own
order↔transaction matcher.

Payload: {orders: [{dedup_key, account?, date, amount, payee?, seller?,
memo?, category?, order_number?, is_refund?,
payment_method?, items?}]}
"""

from __future__ import annotations

import math

from ..engine.compat import as_date, jsonb

MAX_ORDERS = 10_000


def import_orders(conn, orders: list[dict]) -> dict:
    if not isinstance(orders, list) or not orders:
        raise ValueError("payload needs a non-empty orders list")
    if len(orders) > MAX_ORDERS:
        raise ValueError(f"too many orders (max {MAX_ORDERS})")

    def count():
        return conn.execute(
            "SELECT count(*) n FROM amazon_orders").fetchone()["n"]

    before = count()
    drifted = patched = accepted = 0
    # rows in THIS push are never re-dates of each other: a re-date sends
    # the new key in place of the old one, while a split shipment charged
    # twice sends both keys side by side
    pushed_keys = [str(r.get("dedup_key")) for r in orders
                   if isinstance(r, dict) and r.get("dedup_key")]
    with conn.transaction():
        # one push at a time per tenant: the drift re-date is a read
        # (no row under the new key, one candidate under the old) followed
        # by a rename, and a concurrent push still carrying the OLD key
        # could insert it back between the two — the same order twice
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                     ("oikonome:amazon-import:" + str(conn.execute(
                         "SELECT current_setting('app.tenant_id', true) AS t"
                     ).fetchone()["t"]),))
        for r in orders:
            if not isinstance(r, dict):
                continue
            if not r.get("dedup_key") or not r.get("date"):
                continue
            # parse once, skip junk here: an unparsed string riding into
            # the ::date cast below turns a malformed date from the push
            # door (reachable with a script token) into a Postgres
            # InvalidDatetimeFormat no handler catches — a bare 500 for one
            # bad row in a nightly batch. Skip the row, keep the push.
            try:
                rdate = as_date(r["date"])
            except (ValueError, TypeError):
                continue
            if rdate is None:
                continue
            items = r.get("items") or []
            if not isinstance(items, list):
                # the column is a list and every reader treats it as one;
                # an object or a number here would be stored verbatim and
                # detonate the ledger search for the whole household
                items = []
            # a row without a usable amount is skipped like a row without
            # a usable date — one bad row must not 400 the whole push
            try:
                # a JSON boolean is not a dollar amount — float(True) is
                # 1.0, so it has to be refused by type, not by parsing
                if isinstance(r.get("amount"), bool):
                    raise TypeError
                amount = float(r["amount"])
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(amount):
                continue
            order_number = str(r.get("order_number") or "")
            accepted += 1
            # Amazon sometimes re-dates an order a few days later (ship vs
            # order date) — refresh the existing row instead of duplicating.
            # ONLY when the order number identifies the order (a blank one
            # would match every same-amount charge in a ten-day window and
            # merge distinct orders), only when exactly one row qualifies
            # (a split shipment charged twice is two real rows), and never
            # when the incoming key already has a row of its own (the
            # rename would collide with it).
            drift = None
            if order_number and not conn.execute(
                    "SELECT 1 FROM amazon_orders WHERE dedup_key=%s",
                    (r["dedup_key"],)).fetchone():
                cands = conn.execute(
                    """SELECT dedup_key FROM amazon_orders
                       WHERE account=%s AND order_number=%s AND is_refund=%s
                         AND ABS(amount - %s) < 0.005 AND date != %s
                         AND ABS(date - %s) <= 10
                         AND NOT (dedup_key = ANY(%s)) LIMIT 2""",
                    (r.get("account") or "main", order_number,
                     int(bool(r.get("is_refund"))), amount,
                     rdate, rdate, pushed_keys)).fetchall()
                if len(cands) == 1:
                    drift = cands[0]
            if drift:
                conn.execute(
                    "UPDATE amazon_orders SET date=%s, dedup_key=%s "
                    "WHERE dedup_key=%s",
                    (rdate, r["dedup_key"], drift["dedup_key"]))
                conn.execute("UPDATE amazon_matches SET dedup_key=%s "
                             "WHERE dedup_key=%s",
                             (r["dedup_key"], drift["dedup_key"]))
                conn.execute("UPDATE amazon_summaries SET dedup_key=%s "
                             "WHERE dedup_key=%s",
                             (r["dedup_key"], drift["dedup_key"]))
                drifted += 1
                continue
            cur = conn.execute(
                """INSERT INTO amazon_orders (dedup_key, account, date,
                       amount, payee, seller, memo, category,
                       category_source, order_number, is_refund,
                       payment_method, items_json, inserted_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'rules',%s,%s,%s,%s,now())
                   ON CONFLICT (tenant_id, dedup_key) DO NOTHING""",
                (r["dedup_key"], r.get("account") or "main",
                 rdate, amount,
                 r.get("payee") or "Amazon", r.get("seller") or "",
                 r.get("memo") or "", r.get("category") or "",
                 r.get("order_number") or "",
                 int(bool(r.get("is_refund"))),
                 r.get("payment_method") or "", jsonb(items)))
            if cur.rowcount == 0 and items:
                # an earlier itemless push learned its line items later
                patched += conn.execute(
                    """UPDATE amazon_orders SET items_json=%s, memo=%s,
                           category=CASE WHEN category_source='llm'
                                         THEN category ELSE %s END
                       WHERE dedup_key=%s AND items_json='[]'::jsonb""",
                    (jsonb(items), r.get("memo") or "",
                     r.get("category") or "", r["dedup_key"])).rowcount

    # the order↔txn matcher is a full-table pass — skip it when
    # this push changed nothing (a nightly collector mostly re-pushes the
    # same window). Late-arriving TRANSACTIONS still get matched by the
    # nightly worker sweep, which runs run_match unconditionally.
    after = count()
    changed = (after - before) or drifted or patched
    if changed:
        from ..engine import amazon_match, llm_categorize
        # run_match rebuilds amazon_matches with a DELETE-then-reinsert from
        # a snapshot it read earlier, so two chains running at once overwrite
        # each other with stale matches (and their two full-table writes can
        # deadlock). The nightly sweep and the hourly sync already serialize
        # on this per-tenant lock; this push is the third door into the same
        # chain and takes it too.
        #
        # try-lock, not wait: this is the synchronous body of a collector's
        # HTTP request, and the pass it would be waiting on is a full-table
        # sweep — blocking a request thread for minutes is worse than not
        # matching now. Nothing is lost by skipping, for the same reason the
        # "no order changes" branch below is safe: the nightly sweep runs
        # run_match unconditionally, and the response says which happened.
        with llm_categorize.amazon_chain_lock(conn) as got:
            match = amazon_match.run_match(conn) if got else {
                "skipped": "the Amazon match chain is already running; "
                           "the next sweep matches these orders"}
    else:
        match = {"skipped": "no order changes in this push"}

    # the heartbeat says the collector RAN AND WAS UNDERSTOOD: a push whose
    # every row was rejected must not read as fresh, or a broken scraper
    # looks healthy on the Doctor page for as long as it keeps posting
    # junk. An honestly empty push (nothing new to report) still counts.
    from . import heartbeat
    if not orders or accepted:
        heartbeat.stamp(conn, "amazon-orders", rows=accepted,
                        label="Amazon orders")

    return {"ok": True, "pushed": len(orders), "new": after - before,
            "date_drift_refreshed": drifted, "items_patched": patched,
            "orders": {"before": before, "after": after},
            "amazon_match": match}
