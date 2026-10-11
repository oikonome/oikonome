"""A note or category written as a pending hold settles lands on the
posted row.

A feed settling a pending hold into its posted row carries what a person
wrote on the hold across and retires it, in one commit under the pairing
lock. An edit checked before that commit and written after it would sit on
the retired hold, hidden, with the posted row showing nothing. The edit
doors check and write under the same lock, and an edit aimed at a hold a
settlement has already retired goes to the posted row that names it. The
nightly re-anchor finds that posted row by the same link even when it
settled at a different amount (a tip, a fuel pre-auth).
"""

import datetime as dt
import threading
import time
import unittest
from unittest import mock

from oikonome.db import tenancy
from oikonome.sync import reanchor
from oikonome.web import api, data

from .util import add_txn, make_db, write_config

DAY = dt.date.today() - dt.timedelta(days=2)


class EditRacingSettlement(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.user = {"tenant_id": self.tid, "role": "owner", "id": None,
                     "email": "owner@example.dev"}
        add_txn(self.conn, DAY, 38.00, "CORNER BISTRO", account="chk",
                pending=1, txn_id="hold-1")
        rec = mock.patch.object(api._activity, "record")
        rec.start()
        self.addCleanup(rec.stop)

    def _settle(self, conn):
        add_txn(conn, DAY, 45.60, "CORNER BISTRO", account="chk",
                txn_id="post-1")
        conn.execute("UPDATE transactions SET raw = jsonb_build_object("
                     "'pending_transaction_id', 'hold-1') WHERE id='post-1'")
        conn.execute("UPDATE transactions SET removed=1 WHERE id='hold-1'")

    def _note(self, tid):
        r = self.conn.execute("SELECT note FROM transaction_notes "
                              "WHERE txn_id=%s", (tid,)).fetchone()
        return r["note"] if r else None

    def test_a_note_waits_for_a_settlement_in_flight(self):
        other = tenancy.tenant_connect(self.tid)
        self.addCleanup(other.close)
        done = threading.Event()
        with other.transaction():
            other.execute(data._REIMB_LOCK)
            t = threading.Thread(target=lambda: (api.set_txn_note(
                "hold-1", user=self.user, body={"note": "tip for Sam"}),
                done.set()))
            t.start()
            time.sleep(0.5)
            self.assertFalse(done.is_set(),
                             "the note did not wait for the settlement")
            self._settle(other)
        t.join(10)
        self.assertTrue(done.is_set())
        self.assertEqual(self._note("post-1"), "tip for Sam")
        self.assertIsNone(self._note("hold-1"))

    def test_an_edit_on_a_retired_hold_goes_to_its_posted_row(self):
        self._settle(self.conn)
        api.set_txn_note("hold-1", user=self.user, body={"note": "n"})
        self.assertEqual(self._note("post-1"), "n")
        api.set_category("hold-1", user=self.user,
                         body={"category": "FOOD_AND_DRINK"})
        r = self.conn.execute("SELECT category_override FROM transactions "
                              "WHERE id='post-1'").fetchone()
        self.assertEqual(r["category_override"], "FOOD_AND_DRINK")

    def test_an_owner_waits_for_a_settlement_in_flight(self):
        other = tenancy.tenant_connect(self.tid)
        self.addCleanup(other.close)
        done = threading.Event()
        with other.transaction():
            other.execute(data._REIMB_LOCK)
            t = threading.Thread(target=lambda: (api.set_txn_owner_api(
                "hold-1", user=self.user, body={"owner": "Alex"}),
                done.set()))
            t.start()
            time.sleep(0.5)
            self.assertFalse(done.is_set(),
                             "the owner edit did not wait for the settlement")
            self._settle(other)
        t.join(10)
        self.assertTrue(done.is_set())
        r = self.conn.execute("SELECT owner_override FROM transactions "
                              "WHERE id='post-1'").fetchone()
        self.assertEqual(r["owner_override"], "Alex")

    def test_owner_split_bulk_and_business_on_a_retired_hold_go_to_its_posted_row(self):
        self._settle(self.conn)
        out = api.set_txn_owner_api("hold-1", user=self.user,
                                    body={"owner": "Alex"})
        self.assertEqual(out["id"], "post-1")
        api.set_split_api("hold-1", user=self.user, body={"parts": [
            {"category": "FOOD_AND_DRINK", "amount": 40.00},
            {"category": "ENTERTAINMENT", "amount": 5.60}]})
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM transaction_splits WHERE txn_id='post-1'"
        ).fetchone())
        api.clear_split_api("hold-1", user=self.user)
        self.assertFalse(self.conn.execute(
            "SELECT 1 FROM transaction_splits WHERE txn_id='post-1'"
        ).fetchone())
        api.business_flag("hold-1", user=self.user)
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM business_flags WHERE txn_id='post-1'").fetchone())
        with mock.patch.object(api, "_log_bulk"):
            res = api.transactions_bulk(user=self.user, body={
                "ids": ["hold-1"], "action": "category",
                "category": "FOOD_AND_DRINK"})
        self.assertEqual(res["applied"], 1)
        r = self.conn.execute("SELECT owner_override, category_override "
                              "FROM transactions WHERE id='post-1'").fetchone()
        self.assertEqual(r["owner_override"], "Alex")
        self.assertEqual(r["category_override"], "FOOD_AND_DRINK")

    def test_reanchor_follows_the_settlement_link_across_an_amount_change(self):
        self._settle(self.conn)
        row = {"id": "hold-1", "account_id": "chk", "amount": 38.00,
               "date": DAY, "mname": "CORNER BISTRO", "name": "CORNER BISTRO"}
        self.assertEqual(reanchor.find_twin(self.conn, row),
                         ("post-1", "matched"))


if __name__ == "__main__":
    unittest.main()
