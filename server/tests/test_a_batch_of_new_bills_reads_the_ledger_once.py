"""A batch of new bills reads the ledger once for their starts.

Each new bill stores where its current run of payments began
(raw.scheduled_since), which takes a read of the whole ledger. A person's
single Save pays that once; a batch (the setup wizard's approve-all, the
demo seed) must not pay it per bill. Inside `bills.starts_filled_once` the
saves hold their payees and the starts are filled in one read when the
batch ends. Every amount and name is invented
(specs/honest-figures.md, "Months before any bill are unsplit").
"""

import datetime as dt
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import bills, budget

from .util import (_ensure_db, add_bill, add_txn, make_db,
                   seed_accounts, write_config)

PAYEES = (("CONTOSO POWER", 80.0), ("NORTHWIND WATER", 40.0),
          ("FABRIKAM INTERNET", 60.0))


def _months_back(n):
    d = dt.date.today().replace(day=5)
    for _ in range(n):
        d = (d.replace(day=1) - dt.timedelta(days=1)).replace(day=5)
    return d


# four monthly payments each, the last a month ago (the API judges an
# offer by the real day)
FIRST = _months_back(4)


def _history(conn):
    for payee, amount in PAYEES:
        for n in (4, 3, 2, 1):
            add_txn(conn, _months_back(n), amount, payee, account="chk")


def _since(conn, payee):
    return conn.execute("SELECT raw->>'scheduled_since' AS s FROM bills "
                        "WHERE payee = %s", (payee,)).fetchone()["s"]


class _CountsLedgerReads:
    def _reads(self):
        """A counter of full-ledger spend reads made by the start fill."""
        real = budget._spend_rows
        calls = []

        def spy(*a, **kw):
            calls.append(1)
            return real(*a, **kw)
        return calls, mock.patch.object(budget, "_spend_rows", spy)


class ABatchOfSaves(_CountsLedgerReads, unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _history(self.conn)
        self.addCleanup(self.conn.close)

    def test_a_batch_of_new_bills_reads_the_ledger_once(self):
        calls, spy = self._reads()
        with spy, bills.starts_filled_once(self.conn):
            for payee, amount in PAYEES:
                add_bill(self.conn, payee, amount,
                         next_due=dt.date.today().replace(day=5)
                         + dt.timedelta(days=31))
            # nothing is read while the batch is open
            self.assertEqual(len(calls), 0)
        self.assertEqual(len(calls), 1)
        for payee, _ in PAYEES:
            self.assertEqual(_since(self.conn, payee), FIRST.isoformat())

    def test_a_single_save_still_fills_its_own_start(self):
        calls, spy = self._reads()
        with spy:
            add_bill(self.conn, "CONTOSO POWER", 80.0,
                     next_due=dt.date.today().replace(day=5)
                         + dt.timedelta(days=31))
        self.assertEqual(len(calls), 1)
        self.assertEqual(_since(self.conn, "CONTOSO POWER"), FIRST.isoformat())

    def test_a_failed_batch_leaves_the_starts_to_the_nightly(self):
        calls, spy = self._reads()
        with spy, self.assertRaises(RuntimeError):
            with bills.starts_filled_once(self.conn):
                add_bill(self.conn, "CONTOSO POWER", 80.0,
                         next_due=dt.date.today().replace(day=5)
                         + dt.timedelta(days=31))
                raise RuntimeError("the batch failed")
        self.assertEqual(len(calls), 0)
        self.assertIsNone(_since(self.conn, "CONTOSO POWER"))
        self.assertEqual(bills.fill_scheduled_since(self.conn), 1)


def _make_client() -> TestClient:
    import os
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    from oikonome.web.app import app
    return TestClient(app)


class TheWizardsApproveAll(_CountsLedgerReads, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = _make_client()
        cls.client.post("/api/signup", data={
            "email": f"batch-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            _history(conn)
            for i, (payee, amount) in enumerate(PAYEES):
                bills._insert_proposal(
                    conn, pid=f"prop:batch-{i}", kind="add", payee=payee,
                    amount=amount, frequency="MONTHLY",
                    next_due=dt.date.today() + dt.timedelta(days=10),
                    evidence={"hits": 4, "cycle_days": 30})
        finally:
            conn.close()

    def test_approve_all_fills_every_start_in_one_ledger_read(self):
        calls, spy = self._reads()
        with spy:
            r = self.client.post("/api/bills/proposals/approve-all")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["approved"], len(PAYEES))
        self.assertEqual(len(calls), 1)
        conn = tenancy.tenant_connect(self.tid)
        try:
            for payee, _ in PAYEES:
                self.assertEqual(_since(conn, payee), FIRST.isoformat())
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
