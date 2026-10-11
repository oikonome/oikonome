"""A bill keeps paying a merchant that was merged into another.

The history page's "Merge into…" files every row of one display name under
another merchant and promises the bill, if any, follows. A bill names its
merchants by id AND name (raw.merchant_refs); the id follows merged_into,
but a ref that carries only the name (a bill mirrored or restored from
another instance) names nothing once the old name is gone, and the name is
what both clients list. So the merge re-points every bill that paid the
old name at the survivor in the same transaction — one chip, never a
duplicate — and the merge's undo puts the old chip back.
"""

import datetime as dt
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import bills, budget, merchant_dedup, merchant_identity
from oikonome.engine.compat import as_dict, jsonb

from .util import (TEST_DB, TODAY, _ensure_db, add_bill, add_txn, make_db,
                   seed_accounts, write_config)

TWIN = "Brightwave Fiber"
SURVIVOR = "Brightwave"
PAYEE = "Home Internet"


class MergeCarriesTheBill(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        for back in (64, 34):
            add_txn(self.conn, TODAY - dt.timedelta(days=back), 80.0,
                    "BRIGHTWAVE FIBER ONLINE PMT", merchant=TWIN,
                    primary="RENT_AND_UTILITIES")
        add_txn(self.conn, TODAY - dt.timedelta(days=200), 15.0,
                "BRIGHTWAVE STORE", merchant=SURVIVOR)
        merchant_identity.resolve(self.conn)

    def tearDown(self):
        self.conn.close()

    def _bill(self, merchants):
        add_bill(self.conn, PAYEE, 80.0, frequency="MONTHLY",
                 next_due=TODAY - dt.timedelta(days=4),
                 last_seen=TODAY - dt.timedelta(days=34),
                 merchants=merchants)

    def _raw(self):
        return as_dict(self.conn.execute(
            "SELECT raw FROM bills WHERE payee=%s", (PAYEE,)).fetchone()["raw"])

    def _merge(self):
        with self.conn.transaction():
            merchant_dedup.rename(self.conn, TWIN, SURVIVOR)
        return [r["id"] for r in self.conn.execute(
            "SELECT id FROM merchant_renames ORDER BY id DESC").fetchall()]

    def _undo(self, change_ids):
        for cid in change_ids:
            with self.conn.transaction():
                merchant_dedup.undo(self.conn, cid)

    def _next_charge_matches(self):
        new = add_txn(self.conn, TODAY - dt.timedelta(days=3), 80.0,
                      "BRIGHTWAVE PAYMENT", merchant=SURVIVOR,
                      primary="RENT_AND_UTILITIES")
        merchant_identity.resolve(self.conn)
        survivor = self.conn.execute(
            "SELECT merchant_id::text AS m FROM transactions WHERE id=%s",
            (new,)).fetchone()["m"]
        ids = budget.bill_merchant_ids(
            self.conn, [{"payee": PAYEE, "raw": self._raw()}])[PAYEE]
        self.assertIn(survivor, ids)
        h = bills.analyze_bills(self.conn, TODAY, payee=PAYEE)[PAYEE]
        self.assertEqual(h["last_match"], TODAY - dt.timedelta(days=3))

    def test_the_merge_moves_the_chip_and_the_next_charge_pays_the_bill(self):
        self._bill([TWIN])
        changes = self._merge()
        self.assertEqual(self._raw()["merchant_names"], [SURVIVOR])
        self._next_charge_matches()
        self._undo(changes)
        self.assertEqual(self._raw()["merchant_names"], [TWIN])
        self.assertNotIn("merchant_merges", self._raw())

    def test_a_ref_known_only_by_name_follows_too(self):
        # a bill mirrored from another instance: its ref has no id here
        self._bill([TWIN])
        self.conn.execute(
            "UPDATE bills SET raw = raw || %s::jsonb WHERE payee=%s",
            (jsonb({"merchant_refs": [{"id": None, "name": TWIN}]}), PAYEE))
        changes = self._merge()
        refs = self._raw()["merchant_refs"]
        self.assertEqual([r["name"] for r in refs], [SURVIVOR])
        self.assertIsNotNone(refs[0]["id"])
        self._next_charge_matches()
        self._undo(changes)
        self.assertEqual(self._raw()["merchant_names"], [TWIN])

    def test_a_bill_that_already_paid_the_survivor_gets_no_second_chip(self):
        self._bill([TWIN, SURVIVOR])
        changes = self._merge()
        self.assertEqual(self._raw()["merchant_names"], [SURVIVOR])
        self.assertEqual(len(self._raw()["merchant_refs"]), 1)
        # undo gives back both: the survivor was the bill's before the merge
        self._undo(changes)
        self.assertEqual(self._raw()["merchant_names"], [SURVIVOR, TWIN])

    def test_the_payee_is_the_persons_label_and_stays(self):
        add_bill(self.conn, TWIN, 80.0, frequency="MONTHLY",
                 next_due=TODAY - dt.timedelta(days=4), merchants=[TWIN])
        self._merge()
        row = self.conn.execute("SELECT payee, raw FROM bills WHERE payee=%s",
                                (TWIN,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(as_dict(row["raw"])["merchant_names"], [SURVIVOR])

    def test_an_edit_after_the_merge_is_not_undone(self):
        self._bill([TWIN])
        changes = self._merge()
        self._bill([SURVIVOR, "Elsewhere Mobile"])
        self._undo(changes)
        self.assertNotIn(TWIN, self._raw()["merchant_names"])

    def test_a_pending_offer_of_the_old_name_offers_the_survivor(self):
        self._bill([])
        bills._insert_proposal(
            self.conn, pid="prop:merge-offer", kind="merchant", payee=TWIN,
            amount=80.0, evidence={"bill_payee": PAYEE, "merchant_name": TWIN})
        self._merge()
        p = self.conn.execute("SELECT payee, evidence, status FROM bill_proposals "
                              "WHERE id='prop:merge-offer'").fetchone()
        self.assertEqual(p["status"], "pending")
        self.assertEqual(p["payee"], SURVIVOR)
        self.assertEqual(as_dict(p["evidence"])["merchant_name"], SURVIVOR)


class MergeTakesProposalsBeforeBills(unittest.TestCase):
    """A merge re-points bills and the pending offers that name the old
    merchant. An offer's approval locks the offer and then its bill; a
    merge taking the bills first and then the offer would wait on the
    approval while the approval waits on it, and Postgres breaks that
    cycle by killing one side — the person's approve, or the nightly
    repair with every merge of its night."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        add_txn(self.conn, TODAY - dt.timedelta(days=34), 80.0,
                "BRIGHTWAVE FIBER ONLINE PMT", merchant=TWIN)
        add_txn(self.conn, TODAY - dt.timedelta(days=200), 15.0,
                "BRIGHTWAVE STORE", merchant=SURVIVOR)
        merchant_identity.resolve(self.conn)
        add_bill(self.conn, PAYEE, 80.0, frequency="MONTHLY",
                 next_due=TODAY - dt.timedelta(days=4), merchants=[TWIN])
        bills._insert_proposal(
            self.conn, pid="prop:race", kind="merchant", payee=TWIN,
            amount=80.0, evidence={"bill_payee": PAYEE, "merchant_name": TWIN})
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        dsn = os.environ.get(
            "OIKONOME_TEST_DSN",
            f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}")
        self.other = tenancy.tenant_connect(tid, dsn)
        self.watch = tenancy.tenant_connect(tid, dsn)
        self.addCleanup(self.other.close)
        self.addCleanup(self.watch.close)

    def _waiting(self, pid):
        return self.watch.execute(
            "SELECT wait_event_type = 'Lock' AS w FROM pg_stat_activity "
            "WHERE pid = %s", (pid,)).fetchone()["w"]

    def test_a_merge_waits_on_an_approval_without_deadlocking(self):
        import threading
        import time

        import psycopg
        failed = []

        def merge():
            try:
                with self.other.transaction():
                    merchant_dedup.rename(self.other, TWIN, SURVIVOR)
            except psycopg.Error as e:            # the deadlock victim
                failed.append(e)

        approve = self.conn
        with approve.transaction():
            # the approval holds its offer...
            approve.execute("SELECT id FROM bill_proposals WHERE id='prop:race' "
                            "FOR UPDATE")
            t = threading.Thread(target=merge)
            t.start()
            deadline = time.monotonic() + 10
            while not self._waiting(self.other.info.backend_pid):
                self.assertLess(time.monotonic(), deadline,
                                "the merge never reached the offer")
                time.sleep(0.05)
            # ...and goes on to its bill while the merge waits on the offer
            try:
                approve.execute("SET LOCAL lock_timeout = '3s'")
                approve.execute("SELECT id FROM bills WHERE payee=%s FOR UPDATE",
                                (PAYEE,))
            except psycopg.Error as e:
                failed.append(e)
                raise
        t.join(15)
        self.assertFalse(t.is_alive())
        self.assertEqual(failed, [])
        raw = as_dict(self.conn.execute(
            "SELECT raw FROM bills WHERE payee=%s", (PAYEE,)).fetchone()["raw"])
        self.assertEqual(raw["merchant_names"], [SURVIVOR])

    def test_an_approval_stamping_the_rows_never_deadlocks_with_a_merge(self):
        """An approval holds its offer and its bill, then stamps the bill's
        category onto the rows the bill now matches. A merge of those rows'
        merchant must wait for the offer before it moves any row, or each
        side holds what the other needs next."""
        import threading
        import time

        import psycopg
        failed = []

        def merge():
            try:
                with self.other.transaction():
                    merchant_dedup.rename(self.other, TWIN, SURVIVOR)
            except psycopg.Error as e:            # the deadlock victim
                failed.append(e)

        approve = self.conn
        with approve.transaction():
            approve.execute("SELECT id FROM bill_proposals WHERE id='prop:race' "
                            "FOR UPDATE")
            approve.execute("SELECT id FROM bills WHERE payee=%s FOR UPDATE",
                            (PAYEE,))
            t = threading.Thread(target=merge)
            t.start()
            deadline = time.monotonic() + 10
            while not self._waiting(self.other.info.backend_pid):
                self.assertLess(time.monotonic(), deadline,
                                "the merge never reached the offer")
                time.sleep(0.05)
            try:
                approve.execute("SET LOCAL lock_timeout = '3s'")
                approve.execute(
                    "UPDATE transactions SET category_override = "
                    "'RENT_AND_UTILITIES' WHERE name = %s",
                    ("BRIGHTWAVE FIBER ONLINE PMT",))
            except psycopg.Error as e:
                failed.append(e)
                raise
        t.join(15)
        self.assertFalse(t.is_alive())
        self.assertEqual(failed, [])


class HistoryReplySaysWhetherTheNameIsAMerchant(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"mmb-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            add_txn(conn, TODAY - dt.timedelta(days=5), 80.0,
                    "BRIGHTWAVE FIBER ONLINE PMT", merchant=TWIN)
            merchant_identity.resolve(conn)
        finally:
            conn.close()

    def test_merchant_in_use(self):
        got = self.client.get("/api/bills/history",
                              params={"payee": TWIN}).json()
        self.assertIs(got["merchant_in_use"], True)
        got = self.client.get("/api/bills/history",
                              params={"payee": "Nobody Here"}).json()
        self.assertIs(got["merchant_in_use"], False)


if __name__ == "__main__":
    unittest.main()
