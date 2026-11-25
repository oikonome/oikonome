"""A Bills income row prints the last real deposit, to the cent.

The plan keeps a paycheck's planned amount (whole, edited by hand or by
detection), but the deposit the bank posted is what Transactions shows. An
income row printing the planned figure beside a ledger that says a few
cents more reads as two answers for one paycheck. The row prints
`bills.row_amount`: the most recent deposit matched to the series, else the
planned amount; the plan itself (`amount`, the edit form's prefill, every
budget figure) is unchanged (specs/shown-figures.md).
"""

import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import bills

from .util import _ensure_db, add_bill, add_txn, seed_accounts, write_config

PAYER = "Northwind Tools"
PAYROLL = "NORTHWIND TOOLS PAYROLL DIRECT DEP"


class TheRowAmountItself(unittest.TestCase):
    def test_an_income_row_prints_the_last_matched_deposit(self):
        self.assertEqual(bills.row_amount(
            1960.0, {}, {"last_match_amount": 1960.43}), 1960.43)

    def test_an_income_row_with_no_deposit_prints_its_plan(self):
        self.assertEqual(bills.row_amount(1960.0, {}, {}), 1960.0)
        self.assertEqual(bills.row_amount(1960.0, {}, None), 1960.0)

    def test_a_bill_row_still_prints_its_amount_with_its_fees(self):
        raw = {"companions": [{"tokens": "fee", "amount": 1.5,
                               "window_days": 3}]}
        self.assertEqual(bills.row_amount(
            -90.0, raw, {"last_match_amount": 88.0}), 91.5)


def _client() -> TestClient:
    import os
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    return TestClient(appmod.app)


class TheBillsReplyPrintsIt(unittest.TestCase):
    def setUp(self):
        self.client = _client()
        self.client.post("/api/signup", data={
            "email": f"inc-row-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        tid = self.client.get("/api/me").json()["tenant_id"]
        self.conn = tenancy.tenant_connect(tid)
        seed_accounts(self.conn)
        write_config(self.conn)
        today = dt.date.today()
        self.paid_on = today - dt.timedelta(days=3)
        add_bill(self.conn, PAYER, 1960.0, frequency="WEEKLY", interval=2,
                 next_due=self.paid_on + dt.timedelta(days=14),
                 last_seen=self.paid_on, income=True)

    def tearDown(self):
        self.conn.close()

    def _row(self):
        rows = {b["payee"]: b for b in
                self.client.get("/api/bills").json()["bills"]}
        return rows[PAYER]

    def test_the_row_prints_the_deposit_and_keeps_the_plan(self):
        add_txn(self.conn, self.paid_on - dt.timedelta(days=14), -1960.0,
                PAYROLL, account="chk", primary="INCOME")
        add_txn(self.conn, self.paid_on, -1960.43, PAYROLL, account="chk",
                primary="INCOME")
        row = self._row()
        self.assertEqual(row["amount_with_fees"], 1960.43)
        self.assertEqual(row["amount"], 1960.0)

    def test_a_one_time_income_still_to_come_prints_its_plan(self):
        """An earlier deposit of similar size from the same payer is not
        the one-time income the row is waiting for."""
        today = dt.date.today()
        add_bill(self.conn, "Harbor Lights Pottery", 1200.0, frequency=None,
                 next_due=today + dt.timedelta(days=140), income=True)
        add_txn(self.conn, today - dt.timedelta(days=250), -930.0,
                "HARBOR LIGHTS POTTERY PAYMENT", account="chk",
                primary="INCOME")
        rows = {b["payee"]: b for b in
                self.client.get("/api/bills").json()["bills"]}
        self.assertEqual(rows["Harbor Lights Pottery"]["amount_with_fees"],
                         1200.0)

    def test_with_no_deposit_the_row_prints_the_plan(self):
        row = self._row()
        self.assertEqual(row["amount_with_fees"], 1960.0)


if __name__ == "__main__":
    unittest.main()
