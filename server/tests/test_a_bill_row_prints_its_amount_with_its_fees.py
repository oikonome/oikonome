"""A bill row's amount with its fees is one figure, decided on the server.

The Bills page on the web and on the phone print each bill as what one
payment charges: the bill's amount plus the fees that ride with it. Each
client used to add `amount + fee_total` itself. The sum is
`bills.amount_with_fees`, sent as the row's `amount_with_fees`, and both
clients print it (specs/shown-figures.md, "A bill row's amount with its
fees").
"""

import pathlib
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import bills

from .util import _ensure_db, seed_accounts, write_config

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _make_client() -> TestClient:
    import os
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    from oikonome.web.app import app
    return TestClient(app)


class TheSumItself(unittest.TestCase):
    def test_is_the_amount_and_every_fee(self):
        raw = {"companions": [
            {"tokens": "servicing surcharge", "amount": 1.25,
             "window_days": 3},
            {"tokens": "card fee", "amount": 0.5, "window_days": 2}]}
        self.assertEqual(bills.amount_with_fees(90.0, raw), 91.75)

    def test_a_bill_with_no_fees_is_its_amount(self):
        self.assertEqual(bills.amount_with_fees(40.0, {}), 40.0)
        self.assertEqual(bills.amount_with_fees(40.0, None), 40.0)

    def test_is_held_to_the_cent(self):
        raw = {"companions": [{"tokens": "fee", "amount": 0.1,
                               "window_days": 3}]}
        self.assertEqual(bills.amount_with_fees(0.2, raw), 0.3)


class TheBillsReplyCarriesIt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = _make_client()
        cls.client.post("/api/signup", data={
            "email": f"awf-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def test_each_row_says_what_one_payment_charges(self):
        r = self.client.post("/api/bills/save", json={
            "payee": "Zenith Water", "amount": "90", "cadence": "MONTHLY:1",
            "next_due": "2026-08-05",
            "companions": [{"tokens": "servicing surcharge", "amount": 1.5,
                            "window_days": 3}]})
        self.assertEqual(r.status_code, 200, r.text)
        rows = {b["payee"]: b for b in
                self.client.get("/api/bills").json()["bills"]}
        row = rows["Zenith Water"]
        self.assertEqual(row["fee_total"], 1.5)
        self.assertEqual(row["amount_with_fees"], 91.5)


class BothClientsPrintIt(unittest.TestCase):
    """The web prints the server's sum alone; the phone does too, and adds
    the two itself only for an older server that sends no sum."""

    def test_the_web_never_sums_amount_and_fee_total(self):
        src = (ROOT / "webapp/src/pages/Bills.tsx").read_text(encoding="utf-8")
        self.assertNotIn("b.amount + (b.fee_total", src)
        self.assertIn("money(b.amount_with_fees)", src)

    def test_the_phone_prints_the_servers_sum_first(self):
        src = (ROOT / "mobile/src/app/(tabs)/bills.tsx").read_text(
            encoding="utf-8")
        self.assertIn("b.amount_with_fees ?? b.amount + (b.fee_total || 0)",
                      src)


if __name__ == "__main__":
    unittest.main()
