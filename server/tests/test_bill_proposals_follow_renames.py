"""A pending proposal stays actionable across a rename of the bill it names.

`bill_proposals` points at a bill by PAYEE STRING — there is no bill_id FK —
so the invariant a rename has to keep is: renaming a bill is a DISPLAY
change, and nothing that was actionable before it may become permanently
unapprovable after it. Without the migration, `_apply_approved` looks up the
old name, finds nothing, and every Approve answers "bill '<old>' no longer
exists" forever: a legitimate cadence correction or fee attachment that
happened to be pending at rename time is stuck with no way out.

Both rename doors are covered, because the migration lives inside
`bills.rename_bill` itself rather than in one of its callers.
"""

import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import bills
from oikonome.engine.compat import as_dict

from .util import TODAY, _ensure_db, add_bill, make_db, seed_accounts, \
    write_config

NEXT = TODAY + dt.timedelta(days=10)


def _pending(conn, pid):
    return conn.execute("SELECT * FROM bill_proposals WHERE id=%s",
                        (pid,)).fetchone()


class ARenameCarriesItsPendingProposalsAlong(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_cadence_proposal_can_still_be_approved_after_the_rename(self):
        add_bill(self.conn, "Old Gym", 40.0, next_due=NEXT)
        bills._insert_proposal(
            self.conn, pid="prop:freq", kind="frequency_change",
            payee="Old Gym", amount=40.0, frequency="YEARLY", interval=1,
            next_due=NEXT, evidence={})
        bills.rename_bill(self.conn, "Old Gym", "New Gym")

        self.assertEqual(_pending(self.conn, "prop:freq")["payee"], "New Gym")
        out = bills.apply_proposal(self.conn, "prop:freq", "approve")
        self.assertNotIn("error", out, out)
        self.assertEqual(bills.get_bill(self.conn, "New Gym")["frequency"],
                         "EVERY_YEAR")

    def test_a_remove_proposal_can_still_be_approved_after_the_rename(self):
        add_bill(self.conn, "Old Cable", 90.0, next_due=NEXT)
        bills._insert_proposal(self.conn, pid="prop:rm", kind="remove",
                               payee="Old Cable", amount=90.0)
        bills.rename_bill(self.conn, "Old Cable", "New Cable")

        out = bills.apply_proposal(self.conn, "prop:rm", "approve")
        self.assertNotIn("error", out, out)
        self.assertEqual(bills.get_bill(self.conn, "New Cable")["active"], 0)

    def test_an_attach_proposal_follows_the_host_bill_it_names(self):
        # kind='attach' is the one shape whose `payee` is the FEE's label —
        # the bill it joins is named inside the evidence, so the evidence
        # has to move too or the fee attaches to nothing.
        add_bill(self.conn, "Old Power", 120.0, next_due=NEXT)
        bills._insert_proposal(
            self.conn, pid="prop:fee", kind="attach", payee="Late Fee",
            amount=9.0, evidence={"bill_payee": "Old Power",
                                  "tokens": "late fee"})
        bills.rename_bill(self.conn, "Old Power", "New Power")

        out = bills.apply_proposal(self.conn, "prop:fee", "approve")
        self.assertNotIn("error", out, out)
        host = bills.get_bill(self.conn, "New Power")
        self.assertTrue(bills.companions_of(as_dict(host["raw"])))

    def test_a_decided_proposal_keeps_the_name_it_was_decided_under(self):
        # history is not rewritten: only rows still awaiting a decision move
        add_bill(self.conn, "Old Water", 30.0, next_due=NEXT)
        bills._insert_proposal(self.conn, pid="prop:done", kind="remove",
                               payee="Old Water", amount=30.0,
                               status="auto")
        bills.rename_bill(self.conn, "Old Water", "New Water")
        self.assertEqual(_pending(self.conn, "prop:done")["payee"],
                         "Old Water")

    def test_a_proposal_naming_an_unrelated_bill_is_untouched(self):
        add_bill(self.conn, "Old Phone", 60.0, next_due=NEXT)
        add_bill(self.conn, "Insurance", 200.0, next_due=NEXT)
        bills._insert_proposal(self.conn, pid="prop:other", kind="remove",
                               payee="Insurance", amount=200.0)
        bills.rename_bill(self.conn, "Old Phone", "New Phone")
        self.assertEqual(_pending(self.conn, "prop:other")["payee"],
                         "Insurance")


class TheBillsPageRenameDoorMigratesThemToo(unittest.TestCase):
    """The API route is the door a person actually uses; it must inherit the
    same behaviour without repeating it."""

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"billrename-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = cls._conn()
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    @classmethod
    def _conn(cls):
        return tenancy.tenant_connect(cls.tid)

    def test_saving_a_bill_under_a_new_name_moves_its_pending_proposals(self):
        conn = self._conn()
        try:
            add_bill(conn, "Old Trash", 45.0, next_due=NEXT)
            bills._insert_proposal(conn, pid="prop:route", kind="remove",
                                   payee="Old Trash", amount=45.0)
        finally:
            conn.close()

        r = self.client.post("/api/bills/save", json={
            "orig_payee": "Old Trash", "payee": "New Trash",
            "amount": "45", "cadence": "MONTHLY:1",
            "next_due": NEXT.isoformat()})
        self.assertEqual(r.status_code, 200, r.text)

        conn = self._conn()
        try:
            self.assertEqual(_pending(conn, "prop:route")["payee"],
                             "New Trash")
            out = bills.apply_proposal(conn, "prop:route", "approve")
            self.assertNotIn("error", out, out)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
