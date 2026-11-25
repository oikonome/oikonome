"""A bill typed in by hand is not refused for sharing a tracked merchant.

Two checks for "a bill already claims these charges" exist and they end
differently on purpose. Approving a machine offer for a merchant an active
bill already tracks withdraws the offer: the finder guessed, and what it
offered is already there. A person adding or renaming a bill by hand onto
that merchant is saying what they mean (two plans at one shop, a split
they keep apart), so the save lands. Both take the bill-claim lock; only
the approval refuses.
"""

import datetime as dt
import unittest

from psycopg.types.json import Jsonb

from oikonome.engine import bills
from oikonome.web import api, fields

from .util import add_bill, make_db, write_config


class AHandAddMayShareAMerchantTests(unittest.TestCase):
    def setUp(self):
        self.a = make_db()
        write_config(self.a)
        tid = self.a.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.user = {"tenant_id": tid, "email": "me@example.dev",
                     "role": "owner", "id": None}
        self.a.execute(
            """INSERT INTO bill_proposals (id, kind, bill_type, payee, amount,
                   frequency, "interval", next_due, evidence, status,
                   created_at)
               VALUES ('prop:q', 'add', 'occurrence', 'QUILLCAST.COM', 15.49,
                       'MONTHLY', 1, %s, %s, 'pending', now())""",
            (dt.date.today() + dt.timedelta(days=10), Jsonb({})))
        self.assertNotIn("error",
                         bills.apply_proposal(self.a, "prop:q", "approve"))

    def tearDown(self):
        self.a.close()

    def _active(self):
        return sorted(r["payee"] for r in self.a.execute(
            "SELECT payee FROM bills WHERE active = 1"))

    def test_a_hand_add_onto_a_tracked_merchant_lands(self):
        r = api.bills_save_api(user=self.user, body=fields.Fields({
            "payee": "Quillcast", "amount": 15.49, "cadence": "MONTHLY:1"}))
        self.assertTrue(r, r)
        self.assertEqual(self._active(), ["QUILLCAST.COM", "Quillcast"])

    def test_a_hand_rename_onto_a_tracked_merchant_lands(self):
        add_bill(self.a, "Spinwheel Audio", 9.99, source="manual")
        r = api.bills_save_api(user=self.user, body=fields.Fields({
            "payee": "Quillcast", "orig_payee": "Spinwheel Audio",
            "amount": 9.99, "cadence": "MONTHLY:1"}))
        self.assertTrue(r, r)
        self.assertEqual(self._active(), ["QUILLCAST.COM", "Quillcast"])

    def test_an_offer_for_the_tracked_merchant_is_withdrawn(self):
        self.a.execute(
            """INSERT INTO bill_proposals (id, kind, bill_type, payee, amount,
                   frequency, "interval", next_due, evidence, status,
                   created_at)
               VALUES ('prop:q2', 'add', 'occurrence', 'Quillcast', 15.49,
                       'MONTHLY', 1, %s, %s, 'pending', now())""",
            (dt.date.today() + dt.timedelta(days=10), Jsonb({})))
        r = bills.apply_proposal(self.a, "prop:q2", "approve")
        self.assertIn("already tracked", r.get("error", ""), r)
        self.assertTrue(bills.proposal_superseded(self.a, "prop:q2"))
        self.assertEqual(self._active(), ["QUILLCAST.COM"])


if __name__ == "__main__":
    unittest.main()
