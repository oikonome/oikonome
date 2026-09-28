"""Amazon order↔transaction matcher: cent-exact matching, date window,
sign correspondence, mask tiebreaker, investment/transfer exclusions, and
the LOAD-BEARING ordering — manual_categories re-applied LAST."""

import unittest

from oikonome.engine import amazon_match
from oikonome.engine.compat import as_date

from .util import add_txn, make_db


def add_order(conn, dedup_key, date, amount, category,
              payment_method="", is_refund=0):
    conn.execute(
        """INSERT INTO amazon_orders (dedup_key, account, date, amount, payee,
               category, is_refund, payment_method)
           VALUES (%s,'acct',%s,%s,'Amazon',%s,%s,%s)""",
        (dedup_key, as_date(date), amount, category, is_refund, payment_method))


def override(conn, txn_id):
    return conn.execute(
        "SELECT category_override FROM transactions WHERE id=%s",
        (txn_id,)).fetchone()["category_override"]


class AmazonMatchTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_exact_amount_date_window_match(self):
        # order (charge, negative) 2 days before the card posting
        add_order(self.conn, "o1", "2025-07-08", -23.45, "Apparel")
        t = add_txn(self.conn, "2025-07-10", 23.45, "AMZN Mktp US*123")
        res = amazon_match.run_match(self.conn)
        self.assertEqual(res["matched"], 1)
        self.assertEqual(override(self.conn, t), "Amazon - Apparel")
        m = self.conn.execute(
            "SELECT dedup_key FROM amazon_matches WHERE transaction_id=%s",
            (t,)).fetchone()
        self.assertEqual(m["dedup_key"], "o1")

    def test_unmatched_amazon_charge_stays_visible(self):
        t = add_txn(self.conn, "2025-07-10", 9.99, "Amazon Prime")
        res = amazon_match.run_match(self.conn)
        self.assertEqual(res["unmatched"], 1)
        self.assertEqual(override(self.conn, t), "Amazon - Unmatched")

    def test_non_amazon_rows_untouched(self):
        t = add_txn(self.conn, "2025-07-10", 23.45, "SAFEWAY STORE")
        add_order(self.conn, "o1", "2025-07-08", -23.45, "Apparel")
        amazon_match.run_match(self.conn)
        self.assertIsNone(override(self.conn, t))

    def test_date_window_bounds(self):
        # order 8 days before posting: outside DATE_WINDOW_BEFORE=7
        add_order(self.conn, "old", "2025-07-02", -50.0, "Tools")
        t = add_txn(self.conn, "2025-07-10", 50.0, "AMZN Mktp")
        amazon_match.run_match(self.conn)
        self.assertEqual(override(self.conn, t), "Amazon - Unmatched")

    def test_sign_correspondence_refunds(self):
        # refund (txn negative) must match the refund order (positive),
        # never the equal-cent charge order (negative)
        add_order(self.conn, "chg", "2025-07-09", -10.0, "Apparel")
        add_order(self.conn, "ref", "2025-07-09", 10.0, "Refunds", is_refund=1)
        t = add_txn(self.conn, "2025-07-10", -10.0, "AMZN Mktp US refund")
        amazon_match.run_match(self.conn)
        self.assertEqual(override(self.conn, t), "Amazon - Refunds")
        m = self.conn.execute(
            "SELECT dedup_key FROM amazon_matches WHERE transaction_id=%s",
            (t,)).fetchone()
        self.assertEqual(m["dedup_key"], "ref")

    def test_investment_and_transfer_rows_excluded(self):
        # AMZN stock trade on an investment account + card-payment transfer:
        # neither may be stamped 'Amazon - Unmatched'
        self.conn.execute(
            """INSERT INTO accounts (id, item_id, name, type, subtype)
               VALUES ('inv', 'it1', 'Brokerage', 'investment', 'brokerage')""")
        stock = add_txn(self.conn, "2025-07-10", 500.0, "BUY AMZN", account="inv")
        xfer = add_txn(self.conn, "2025-07-10", 120.0, "Transfer to Amazon Card",
                       primary="TRANSFER_OUT", account="chk")
        res = amazon_match.run_match(self.conn)
        self.assertIsNone(override(self.conn, stock))
        self.assertIsNone(override(self.conn, xfer))
        self.assertEqual(res["amazon_plaid_txns"], 0)

    def test_mask_tiebreaker(self):
        self.conn.execute("UPDATE accounts SET mask='1234' WHERE id='card'")
        # two same-cent orders the same day; only one paid with ...1234
        add_order(self.conn, "other", "2025-07-09", -15.0, "Tools",
                  payment_method="Visa ...9999")
        add_order(self.conn, "mine", "2025-07-09", -15.0, "Apparel",
                  payment_method="Visa ...1234")
        t = add_txn(self.conn, "2025-07-10", 15.0, "AMZN Mktp", account="card")
        amazon_match.run_match(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT dedup_key FROM amazon_matches WHERE transaction_id=%s",
            (t,)).fetchone()["dedup_key"], "mine")
        self.assertEqual(override(self.conn, t), "Amazon - Apparel")

    def test_manual_categories_reapplied_last(self):
        """LOAD-BEARING: a human recategorization survives every re-match."""
        add_order(self.conn, "o1", "2025-07-08", -23.45, "Apparel")
        t = add_txn(self.conn, "2025-07-10", 23.45, "AMZN Mktp US")
        self.conn.execute(
            "INSERT INTO manual_categories (transaction_id, category) "
            "VALUES (%s, 'Groceries')", (t,))
        amazon_match.run_match(self.conn)
        self.assertEqual(override(self.conn, t), "Groceries")
        amazon_match.run_match(self.conn)            # nightly replay
        self.assertEqual(override(self.conn, t), "Groceries")

    def test_recompute_from_scratch_clears_stale_state(self):
        add_order(self.conn, "o1", "2025-07-08", -23.45, "Apparel")
        t = add_txn(self.conn, "2025-07-10", 23.45, "AMZN Mktp US")
        amazon_match.run_match(self.conn)
        self.assertEqual(override(self.conn, t), "Amazon - Apparel")
        # order disappears (e.g. re-imported window) → override degrades to
        # Unmatched, stale match row cleared
        self.conn.execute("DELETE FROM amazon_orders WHERE dedup_key='o1'")
        amazon_match.run_match(self.conn)
        self.assertEqual(override(self.conn, t), "Amazon - Unmatched")
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM amazon_matches WHERE transaction_id=%s",
            (t,)).fetchone())

    def test_one_order_matches_at_most_one_txn(self):
        add_order(self.conn, "o1", "2025-07-09", -12.0, "Tools")
        t1 = add_txn(self.conn, "2025-07-09", 12.0, "AMZN Mktp A")
        t2 = add_txn(self.conn, "2025-07-10", 12.0, "AMZN Mktp B")
        res = amazon_match.run_match(self.conn)
        self.assertEqual(res["matched"], 1)
        # oldest-first determinism: t1 (07-09, delta 0) wins
        self.assertEqual(override(self.conn, t1), "Amazon - Tools")
        self.assertEqual(override(self.conn, t2), "Amazon - Unmatched")

    def test_authorized_date_preferred(self):
        # posting drifted late but authorized_date sits in the window
        add_order(self.conn, "o1", "2025-07-01", -30.0, "Tools")
        t = add_txn(self.conn, "2025-07-12", 30.0, "AMZN Mktp")
        self.conn.execute(
            "UPDATE transactions SET authorized_date=%s WHERE id=%s",
            (as_date("2025-07-05"), t))
        amazon_match.run_match(self.conn)
        self.assertEqual(override(self.conn, t), "Amazon - Tools")


class AmazonItemSearchTests(unittest.TestCase):
    """Ledger search reaches INTO matched Amazon orders: a term matching an
    item title (or the rendered summary) finds the charge, and the result
    carries a per-item price total — the transaction sum alone overstates a
    mixed order."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _matched_order(self, key, txn_amt, items, date="2025-07-08",
                       summary=None):
        from oikonome.engine.compat import jsonb
        self.conn.execute(
            """INSERT INTO amazon_orders (dedup_key, account, date, amount,
                   payee, category, is_refund, items_json)
               VALUES (%s,'acct',%s,%s,'Amazon','Grocery',0,%s)""",
            (key, as_date(date), -txn_amt, jsonb(items)))
        t = add_txn(self.conn, date, txn_amt, "AMZN Mktp US")
        self.conn.execute(
            "INSERT INTO amazon_matches (transaction_id, dedup_key) "
            "VALUES (%s,%s)", (t, key))
        if summary:
            self.conn.execute(
                "INSERT INTO amazon_summaries (dedup_key, summary) "
                "VALUES (%s,%s)", (key, summary))
        return t

    def test_item_title_matches_and_per_item_total(self):
        from oikonome.web import data
        # mixed box: honey + a pan; the txn is $90 but honey is $40
        t1 = self._matched_order("o1", 90.0, [
            {"title": "Acme Raw Wildflower Honey", "price": 40.0},
            {"title": "Acme Cast Iron Pan", "price": 50.0}])
        # second honey-only order
        self._matched_order("o2", 42.5, [
            {"title": "Raw Wildflower Honey 250g", "price": 42.5}],
            date="2025-07-01")
        # noise that must not match
        add_txn(self.conn, "2025-07-05", 12.0, "SAFEWAY STORE")
        rows, total, amount_sum, az, _, _hits = data.search_transactions(
            self.conn, "wildflower honey")
        self.assertEqual(total, 2)
        self.assertEqual(amount_sum, 132.5)          # txn totals
        self.assertEqual(az, {"count": 2, "sum": 82.5, "unpriced": 0})
        self.assertIn(t1, [r["id"] for r in rows])

    def test_priceless_items_are_counted_but_flagged(self):
        """An item with no (or unparsable) price is IN the count but cannot
        be in the sum — the result must say how many, or '$40 on 2 items'
        reads as if the $40 covered both."""
        from oikonome.web import data
        self._matched_order("o1", 90.0, [
            {"title": "Wildflower Honey Sample"},                  # no price key
            {"title": "Wildflower Honey Jar", "price": 40.0},
            {"title": "Wildflower Honey Sticks", "price": "n/a"}])  # unparsable
        _, _, _, az, _, _hits = data.search_transactions(self.conn, "wildflower honey")
        self.assertEqual(az, {"count": 3, "sum": 40.0, "unpriced": 2})

    def test_summary_matches_without_title_hit(self):
        from oikonome.web import data
        t = self._matched_order("o1", 25.0, [
            {"title": "XYZ Brand 12-pack", "price": 25.0}],
            summary="sparkling water")
        rows, total, _, az, _, _hits = data.search_transactions(
            self.conn, "sparkling water")
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["id"], t)
        self.assertEqual(rows[0]["item_summary"], "sparkling water")
        self.assertIsNone(az)             # no TITLE matched → no item total

    def test_non_amazon_search_carries_no_amazon_chrome(self):
        from oikonome.web import data
        add_txn(self.conn, "2025-07-05", 12.0, "SAFEWAY STORE")
        rows, total, _, az, _, _hits = data.search_transactions(self.conn, "safeway")
        self.assertEqual(total, 1)
        self.assertIsNone(az)


class AmazonChainSingleFlightTests(unittest.TestCase):
    """Two Amazon match/summarize chains must never run concurrently for one
    tenant. run_match rebuilds amazon_matches with a DELETE-then-reinsert
    from a pre-transaction snapshot, so a second concurrent run overwrites
    the first with stale state; the chain is reachable from BOTH the hourly
    sync and the nightly sweep, whose own advisory locks never contend."""

    def setUp(self):
        self.conn = make_db()
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        from oikonome.engine.compat import jsonb
        self.conn.execute(
            """INSERT INTO amazon_orders (dedup_key, account, date, amount,
                   payee, category, is_refund, items_json)
               VALUES ('k1','acct','2025-07-08',-20.0,'Amazon','Grocery',0,%s)""",
            (jsonb([{"title": "Widget", "price": 20.0}]),))
        add_txn(self.conn, "2025-07-08", 20.0, "AMZN Mktp US")

    def tearDown(self):
        self.conn.close()

    def _matches(self):
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM amazon_matches").fetchone()["n"]

    def test_sync_chain_skips_while_another_chain_holds_the_lock(self):
        from oikonome.db import tenancy
        from oikonome.engine import llm_categorize
        other = tenancy.tenant_connect(self.tid)
        try:
            got = other.execute(
                f"SELECT pg_try_advisory_lock({llm_categorize._AMAZON_LOCK})"
                " AS ok").fetchone()["ok"]
            self.assertTrue(got)
            llm_categorize.categorize_new(self.conn)
            self.assertEqual(self._matches(), 0)     # chain skipped
        finally:
            # session-level lock on a POOLED connection: unlock explicitly
            # or the next borrower inherits it
            other.execute(
                f"SELECT pg_advisory_unlock({llm_categorize._AMAZON_LOCK})")
            other.close()
        llm_categorize.categorize_new(self.conn)
        self.assertEqual(self._matches(), 1)         # next pass catches up

    def test_push_door_skips_while_another_chain_holds_the_lock(self):
        """The collector's push door (POST /api/import/amazon →
        sync.amazon_orders.import_orders) is the THIRD entrance to this
        chain and must serialize on the same lock. Calling run_match
        unlocked there lets a push arriving during the nightly sweep race
        it: whichever DELETE-then-reinsert commits last wins, and the
        sweep's older snapshot silently drops the matches for the orders
        the push just added. Skipping is safe — orders still
        land, and the nightly sweep runs run_match unconditionally."""
        from oikonome.db import tenancy
        from oikonome.engine import llm_categorize
        from oikonome.sync import amazon_orders
        order = {"dedup_key": "push1", "account": "acct",
                 "date": "2025-09-01", "amount": -5.0, "payee": "Amazon"}
        other = tenancy.tenant_connect(self.tid)
        try:
            self.assertTrue(other.execute(
                f"SELECT pg_try_advisory_lock({llm_categorize._AMAZON_LOCK})"
                " AS ok").fetchone()["ok"])
            out = amazon_orders.import_orders(self.conn, [order])
            self.assertEqual(out["new"], 1)          # the orders still land
            self.assertIn("skipped", out["amazon_match"])
            self.assertEqual(self._matches(), 0)     # no racing rebuild
        finally:
            other.execute(
                f"SELECT pg_advisory_unlock({llm_categorize._AMAZON_LOCK})")
            other.close()
        # lock free → the next push through the same door does match
        out = amazon_orders.import_orders(
            self.conn, [dict(order, dedup_key="push2", date="2025-09-02")])
        self.assertNotIn("skipped", out["amazon_match"])
        self.assertEqual(self._matches(), 1)

    def test_lock_is_released_after_the_chain(self):
        from oikonome.engine import llm_categorize
        llm_categorize.categorize_new(self.conn)
        with llm_categorize.amazon_chain_lock(self.conn) as got:
            self.assertTrue(got)

    def test_waiting_acquire_used_by_nightly(self):
        from oikonome.engine import llm_categorize
        with llm_categorize.amazon_chain_lock(self.conn, wait=True) as got:
            self.assertTrue(got)
        with llm_categorize.amazon_chain_lock(self.conn) as got:
            self.assertTrue(got)


class AmazonPushBadInputTests(unittest.TestCase):
    """The push door is reachable with a script token, so a malformed row
    is untrusted input — it must be skipped, never a bare 500. An
    unscreened date string rides into a ::date cast and raises a Postgres
    error no handler catches."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_malformed_date_row_is_skipped_not_500(self):
        from oikonome.sync import amazon_orders
        out = amazon_orders.import_orders(self.conn, [
            {"dedup_key": "bad", "date": "not-a-date", "amount": 10},
            {"dedup_key": "good", "date": "2024-01-02", "amount": 12},
        ])
        # the good row lands, the junk one is silently dropped
        self.assertEqual(out["new"], 1)
        keys = {r["dedup_key"] for r in self.conn.execute(
            "SELECT dedup_key FROM amazon_orders").fetchall()}
        self.assertEqual(keys, {"good"})


if __name__ == "__main__":
    unittest.main()
