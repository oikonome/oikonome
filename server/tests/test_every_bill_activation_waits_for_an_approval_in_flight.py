"""Every door that can leave a bill active takes its turn on the claim lock.

An approval holds the household's bill-claim lock from its check for a
bill already claiming these charges to the commit of the bill it makes.
A door that adds, renames, restores or approves a bill without waiting for
it could land a bill that check never saw, and the charge would be
counted twice. Each door is driven through its real entry point while
another session holds the lock: it must still be waiting a second later
(so it asks for the lock, and asks before writing), and it must succeed
once the lock is let go (so it satisfies the bill writers' guard, which
refuses a write inside a transaction that does not hold the lock).
"""

import datetime as dt
import os
import threading
import unittest
from unittest import mock

from psycopg.types.json import Jsonb

from oikonome.db import tenancy
from oikonome.engine import bills
from oikonome.notify import mailact
from oikonome.web import api, fields, pages

from .util import TEST_DB, add_bill, make_db, write_config


def _offer(conn, pid, payee, amount=15.49):
    conn.execute(
        """INSERT INTO bill_proposals (id, kind, bill_type, payee, amount,
               frequency, "interval", next_due, evidence, status, created_at)
           VALUES (%s, 'add', 'occurrence', %s, %s, 'MONTHLY', 1, %s, %s,
                   'pending', now())""",
        (pid, payee, amount, dt.date.today() + dt.timedelta(days=10),
         Jsonb({})))


class EveryBillActivationWaitsTests(unittest.TestCase):
    def setUp(self):
        self.a = make_db()
        write_config(self.a)
        self.tid = self.a.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.holder = tenancy.tenant_connect(self.tid, os.environ.get(
            "OIKONOME_TEST_DSN",
            f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}"))
        self.user = {"tenant_id": self.tid, "email": "me@example.dev",
                     "role": "owner", "id": None}

    def tearDown(self):
        self.a.close()
        self.holder.close()

    def _waits_then_lands(self, door):
        out: dict = {}

        def run():
            try:
                out["r"] = door()
            except BaseException as e:      # surfaced on the main thread
                out["e"] = e
        with self.holder.transaction():
            bills.hold_bill_claims(self.holder)
            t = threading.Thread(target=run)
            t.start()
            t.join(1.0)
            waited = t.is_alive()
        t.join(15)
        self.assertFalse(t.is_alive(), "the door never finished")
        if "e" in out:
            raise out["e"]
        self.assertTrue(waited, "the door did not wait for the claim lock")
        return out["r"]

    def _active(self):
        return sorted(r["payee"] for r in self.a.execute(
            "SELECT payee FROM bills WHERE active = 1"))

    def _save(self, **body):
        return api.bills_save_api(user=self.user, body=fields.Fields(body))

    def test_a_hand_add(self):
        r = self._waits_then_lands(lambda: self._save(
            payee="Lanternfish Gym", amount=31.0, cadence="MONTHLY:1"))
        self.assertTrue(r, r)
        self.assertEqual(self._active(), ["Lanternfish Gym"])

    def test_a_hand_rename(self):
        add_bill(self.a, "Lanternfish Gym", 31.0)
        r = self._waits_then_lands(lambda: self._save(
            payee="Lanternfish Fitness", orig_payee="Lanternfish Gym",
            amount=31.0, cadence="MONTHLY:1"))
        self.assertTrue(r, r)
        self.assertEqual(self._active(), ["Lanternfish Fitness"])

    def test_a_save_that_brings_an_archived_bill_back(self):
        add_bill(self.a, "Lanternfish Gym", 31.0)
        bills.archive_bill(self.a, "Lanternfish Gym")
        r = self._waits_then_lands(lambda: self._save(
            payee="Lanternfish Gym", amount=33.0, cadence="MONTHLY:1",
            restore=True))
        self.assertTrue(r, r)
        self.assertEqual(self._active(), ["Lanternfish Gym"])

    def test_the_restore_door(self):
        add_bill(self.a, "Lanternfish Pool", 22.0)
        bills.archive_bill(self.a, "Lanternfish Pool")
        r = self._waits_then_lands(lambda: api.bills_restore_api(
            user=self.user, body=fields.Fields({"payee": "Lanternfish Pool"})))
        self.assertTrue(r, r)
        self.assertEqual(self._active(), ["Lanternfish Pool"])

    def test_an_approval(self):
        _offer(self.a, "prop:x", "Spinwheel Audio")
        r = self._waits_then_lands(lambda: api.bills_proposal_api(
            user=self.user,
            body=fields.Fields({"action": "approve", "pid": "prop:x"})))
        self.assertEqual(r.get("payee"), "Spinwheel Audio", r)
        self.assertEqual(self._active(), ["Spinwheel Audio"])

    def test_an_approval_with_a_new_name(self):
        _offer(self.a, "prop:x", "SPINWHEEL AUDIO*1234")
        r = self._waits_then_lands(lambda: api.bills_proposal_api(
            user=self.user, body=fields.Fields({
                "action": "approve", "pid": "prop:x",
                "adjust": {"name": "Spinwheel Audio"}})))
        self.assertEqual(r.get("payee"), "Spinwheel Audio", r)
        self.assertEqual(self._active(), ["Spinwheel Audio"])

    def test_an_approval_with_a_new_amount(self):
        _offer(self.a, "prop:x", "Spinwheel Audio")
        r = self._waits_then_lands(lambda: api.bills_proposal_api(
            user=self.user, body=fields.Fields({
                "action": "approve", "pid": "prop:x",
                "adjust": {"amount": 17.99}})))
        self.assertEqual(r.get("payee"), "Spinwheel Audio", r)
        self.assertEqual(
            abs(bills.get_bill(self.a, "Spinwheel Audio")["amount"]), 17.99)

    def test_approve_all(self):
        _offer(self.a, "prop:x", "Spinwheel Audio")
        _offer(self.a, "prop:y", "Northwind Stream", 9.99)
        r = self._waits_then_lands(
            lambda: api.bills_proposals_approve_all(user=self.user,
                                                    body=fields.Fields({})))
        self.assertEqual(r.get("approved"), 2, r)
        self.assertEqual(self._active(),
                         ["Northwind Stream", "Spinwheel Audio"])

    def test_the_form_approval(self):
        _offer(self.a, "prop:x", "Spinwheel Audio")
        r = self._waits_then_lands(lambda: pages.bills_proposal(
            user=self.user, pid="prop:x", action="approve"))
        self.assertEqual(r.status_code, 303)
        self.assertIn("approved", r.headers["location"])
        self.assertEqual(self._active(), ["Spinwheel Audio"])

    def test_the_email_button_approval(self):
        _offer(self.a, "prop:x", "Spinwheel Audio")
        tok = mailact.parse(mailact.token(
            self.tid, "me@example.dev", "bill", "prop:x", "approve"))
        with mock.patch.object(mailact, "_standing", return_value=None):
            r = self._waits_then_lands(lambda: mailact.apply(tok))
        self.assertNotIn(r.get("state"), ("error", "failed"), r)
        self.assertEqual(self._active(), ["Spinwheel Audio"])


if __name__ == "__main__":
    unittest.main()
