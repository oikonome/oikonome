"""A JSON integer too wide for a double is refused like any non-number.

JSON has no size limit on integers, so a pushed amount or a stored setting
can arrive as a 400-digit int. float() raises OverflowError on that, not
ValueError, so a guard that catches only TypeError/ValueError lets it
escape: the whole push or page answers 500 instead of skipping (or
refusing) the one bad value the way it does for "N/A" or NaN.
"""

import unittest

from oikonome.engine import debt
from oikonome.sync import base, simplefin

from .util import make_db

HUGE = 10 ** 400


class HelpersTreatAHugeIntegerAsNoNumberTests(unittest.TestCase):

    def test_debt_inputs(self):
        self.assertIsNone(debt._num(HUGE))

    def test_bridge_balance(self):
        self.assertIsNone(simplefin._amount(HUGE))

    def test_account_balance(self):
        self.assertIsNone(base._finite_or_none(HUGE, "acct"))


class PushesSkipTheRowTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_order_push(self):
        from oikonome.sync import amazon_orders
        out = amazon_orders.import_orders(self.conn, [
            {"dedup_key": "huge", "date": "2024-01-02", "amount": HUGE},
            {"dedup_key": "good", "date": "2024-01-02", "amount": 12},
        ])
        self.assertEqual(out["new"], 1)

    def test_receipt_push(self):
        from oikonome.sync import costco_receipts
        costco_receipts.import_receipts(self.conn, [
            {"dedup_key": "huge", "date": "2024-01-02", "amount": HUGE}])
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM costco_receipts WHERE dedup_key='huge'"
        ).fetchone())

    def test_bridge_pull_skips_the_row(self):
        """One oversized row in a SimpleFIN pull is one skipped row; the
        rest of the bridge still lands."""
        import json
        import httpx
        payload = {"errors": [], "accounts": [
            {"id": "acc-chk", "name": "Checking", "currency": "USD",
             "balance": "10.00", "org": {"name": "Demo Bank"},
             "transactions": [
                 {"id": "huge", "posted": 1752537600, "amount": HUGE,
                  "description": "X"},
                 {"id": "inf", "posted": 1752537600, "amount": "-1e999",
                  "description": "Y"},
                 {"id": "ok", "posted": 1752537600, "amount": "-4.00",
                  "description": "KIOSK"}]}]}

        def handler(request):
            return httpx.Response(200, text=json.dumps(payload))
        base.upsert_transactions(self.conn, [base.Transaction(
            id="csv:k", account_id="chk",
            date=__import__("datetime").date(2025, 7, 15), amount=9.0,
            name="KIOSK")])          # a file row, so the overlap guard runs
        simplefin.sync(self.conn, "sfin-demo",
                       "https://u:p@bridge.test/simplefin",
                       transport=httpx.MockTransport(handler))
        ids = {r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions WHERE id LIKE 'sfin:%'")}
        self.assertEqual(ids, {"sfin:acc-chk:ok"})

    def test_mx_pull_skips_the_row(self):
        from oikonome.sync import mx
        from .test_mx import PAYLOAD, _save_creds, _transport
        from .util import write_config
        import copy
        write_config(self.conn)
        _save_creds(self.conn)
        payload = copy.deepcopy(PAYLOAD)
        payload["GET /users/USR-1/transactions"]["transactions"].append(
            {"guid": "TRN-HUGE", "account_guid": "ACT-1", "amount": HUGE,
             "type": "DEBIT", "transacted_at": "2026-07-12T12:00:00Z",
             "description": "X", "status": "POSTED"})
        mx.sync(self.conn, transport=_transport(payload))
        ids = {r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions WHERE id LIKE 'mx:%'")}
        self.assertEqual(ids, {"mx:TRN-1", "mx:TRN-2"})

    def test_recurring_stream_amount(self):
        import httpx
        from oikonome.sync import plaid

        def handler(request):
            return httpx.Response(200, json={
                "outflow_streams": [
                    {"stream_id": "s1",
                     "average_amount": {"amount": HUGE}},
                    {"stream_id": "s2",
                     "average_amount": {"amount": 12.0}}],
                "inflow_streams": []})
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name,"
            " access_token) VALUES ('pl1', 'plaid', 'Bank', 'tok')")
        from unittest import mock
        with mock.patch.object(plaid, "credentials", return_value=(
                "cid", "secret", "https://sandbox.plaid.test")):
            out = plaid.recurring_streams(
                self.conn, "pl1", transport=httpx.MockTransport(handler))
        self.assertEqual([s["stream_id"] for s in out], ["s2"])

    def test_transaction_upsert(self):
        n = base.upsert_transactions(self.conn, [base.Transaction(
            id="huge-1", account_id="chk", date=__import__("datetime").date(
                2024, 1, 2), amount=HUGE, name="X")])
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
