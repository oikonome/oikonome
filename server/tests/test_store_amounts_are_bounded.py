"""A store order or receipt amount must be a sum of money, not just finite.

A double can hold 1e308, and so can every ingest door that only asks
"is it finite". But the matchers work in cents, and 1e308 * 100 is
infinity: rounding it raises, and the household's hourly categorize pass
and its nightly sweep fail on every run from then on, for one bad row. So
each door refuses an amount past any real charge, the same bound split
amounts use, and the matcher skips a stored row it cannot work with
rather than failing the whole household.
"""
import io
import unittest
import zipfile

from oikonome.engine import amazon_match, costco_match
from oikonome.sync import amazon_orders, base, coinbase_push, costco_receipts
from oikonome.sync import restore

from .util import add_txn, make_db

HUGE = 1e308


class StoreAmountBoundTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def _count(self, table):
        return self.conn.execute(
            f"SELECT count(*) AS n FROM {table}").fetchone()["n"]

    def test_the_amazon_door_skips_an_amount_past_any_real_charge(self):
        out = amazon_orders.import_orders(self.conn, [
            {"dedup_key": "big", "account": "a", "date": "2025-09-01",
             "amount": HUGE, "payee": "Amazon"},
            {"dedup_key": "wide", "account": "a", "date": "2025-09-01",
             "amount": 10 ** 308, "payee": "Amazon"}])
        self.assertEqual(out["new"], 0)
        self.assertEqual(self._count("amazon_orders"), 0)

    def test_the_costco_door_skips_an_amount_past_any_real_charge(self):
        costco_receipts.import_receipts(self.conn, [
            {"dedup_key": "big", "date": "2025-07-10", "amount": -HUGE}])
        self.assertEqual(self._count("costco_receipts"), 0)

    def test_a_restore_leaves_out_a_row_with_such_an_amount(self):
        """Older import doors accepted such an amount, so a genuine archive
        can carry one: that row is left out and counted, the rest of the
        archive restores."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("amazon_orders.csv",
                       "dedup_key,account,date,amount\n"
                       f"big,a,2025-09-01,{HUGE!r}\n"
                       "ok,a,2025-09-01,4.5\n")
        counts = restore.restore_zip(self.conn, buf.getvalue())
        self.assertEqual(counts.get("out_of_range"), 1)
        self.assertEqual(self._count("amazon_orders"), 1)

    def test_the_matchers_skip_a_stored_row_they_cannot_work_with(self):
        add_txn(self.conn, "2025-09-02", 5.00, "AMAZON MKTPLACE")
        add_txn(self.conn, "2025-07-10", 99.00, "COSTCO WHSE")
        # stored before the doors were bounded: land sane rows, then widen
        amazon_orders.import_orders(self.conn, [
            {"dedup_key": k, "account": "a", "date": "2025-09-01",
             "amount": -5.00, "payee": "Amazon"} for k in ("big", "ok")])
        costco_receipts.import_receipts(self.conn, [
            {"dedup_key": k, "date": "2025-07-10", "amount": -99.00}
            for k in ("big", "ok")])
        for table in ("amazon_orders", "costco_receipts"):
            self.conn.execute(f"UPDATE {table} SET amount = %s"
                              " WHERE dedup_key = 'big'", (-HUGE,))
        self.assertEqual(amazon_match.run_match(self.conn)["matched"], 1)
        self.assertEqual(costco_match.run_match(self.conn)["matched"], 1)

    def test_a_failing_store_match_does_not_cost_the_hours_categories(self):
        """The sync-time pass applies the merchant map after the store
        matches; one failing match must not take that with it."""
        from unittest import mock

        from oikonome.engine import llm_categorize
        amazon_orders.import_orders(self.conn, [
            {"dedup_key": "k", "account": "a", "date": "2025-09-01",
             "amount": -5.00, "payee": "Amazon"}])
        with mock.patch.object(amazon_match, "run_match",
                               side_effect=OverflowError), \
                mock.patch.object(llm_categorize, "apply",
                                  return_value=0) as applied:
            llm_categorize.categorize_new(self.conn)
        applied.assert_called_once()

    def test_a_ledger_amount_past_any_real_charge_is_not_stored(self):
        base.upsert_transactions(self.conn, [base.Transaction(
            id="huge", account_id="card", date=None, amount=HUGE,
            name="X")])
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM transactions WHERE id='huge'").fetchone())

    def test_the_coinbase_door_refuses_such_an_amount(self):
        with self.assertRaises(ValueError):
            coinbase_push._money(HUGE, "transactions.amount")


if __name__ == "__main__":
    unittest.main()
