"""Restoring an archive again after the reconnect guard carried some of
its rows onto the bank's ids adds no second copy of those rows.

A household restores an archive taken before the bank was linked, then
links it: the account is adopted onto the bank's id, and every restored
charge the backfill re-delivered near the seam is moved onto the id the
bank gave it — the old id is deleted, and nothing remembers it. Restoring
the same archive again files its rows onto the live account (the account
lineage says where it went), and the insert finds a row only by id, so
every carried row came back next to its twin: a month of charges counted
twice in spending, budgets, bills and net worth, reported as imported.
The same holds for an older archive restored after a newer one whose
lineage came with it, and for the file-import rows a carry renamed.
A re-restore is a no-op, so the carried twins must be recognised.
"""

import unittest

from oikonome.db import tenancy
from oikonome.sync import adopt, csvimport, export, restore

from .util import make_db, write_config

CSV = """Date,Description,Amount
2026-03-10,SAFEWAY STORE,-42.50
2026-03-10,SAFEWAY STORE,-42.50
2026-03-11,EMPLOYER PAYROLL,2000.00
2026-03-12,CORNER BAKERY,-7.25
"""
MAPPING = {"date": "Date", "amount": "Amount", "name": "Description"}


def _ids(conn):
    return [r["id"] for r in conn.execute(
        "SELECT id FROM transactions ORDER BY date, id").fetchall()]


class ReRestoreAfterACarry(unittest.TestCase):

    def setUp(self):
        a = self.a = make_db()
        self.addCleanup(a.close)
        write_config(a)
        csvimport.import_csv(a, "chk", CSV, MAPPING)
        item = a.execute("SELECT item_id FROM accounts WHERE id = 'chk'"
                         ).fetchone()["item_id"]
        rows = a.execute("SELECT id, name FROM transactions"
                         " ORDER BY date, id").fetchall()
        self.safeway = [r["id"] for r in rows if r["name"] == "SAFEWAY STORE"]
        bakery = [r["id"] for r in rows if r["name"] == "CORNER BAKERY"][0]
        a.execute("INSERT INTO transaction_notes (txn_id, note)"
                  " VALUES (%s, 'birthday cake')", (bakery,))
        self.before_link = export.build_zip(a)
        # the link: the account is adopted onto the bank's id, and the
        # guard carries two re-delivered charges onto the bank's ids —
        # one of the two identical Safeway charges, and the bakery
        self.assertTrue(adopt._rewrite_account_id(a, "chk", "plaid-chk",
                                                  item))
        a.execute("INSERT INTO account_adoptions (account_id,"
                  " old_account_id, item_id, cutoff_date)"
                  " VALUES ('plaid-chk', 'chk', %s, '2026-03-12')", (item,))
        self.assertTrue(adopt._rewrite_txn_id(a, self.safeway[0], "plaid-t1"))
        self.assertTrue(adopt._rewrite_txn_id(a, bakery, "plaid-t2"))
        self.live = _ids(a)
        self.assertEqual(len(self.live), 4)

    def test_restoring_the_same_archive_again_adds_no_rows(self):
        out = restore.restore_zip(self.a, self.before_link)
        self.assertEqual(_ids(self.a), self.live)
        self.assertEqual(out.get("transactions", 0), 0)
        notes = self.a.execute("SELECT txn_id FROM transaction_notes"
                               ).fetchall()
        self.assertEqual([n["txn_id"] for n in notes], ["plaid-t2"])

    def test_an_older_archive_after_a_newer_one_adds_no_rows(self):
        after_link = export.build_zip(self.a)
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, "re-restore-carry")
        finally:
            admin.close()
        b = tenancy.tenant_connect(tid)
        self.addCleanup(b.close)
        restore.restore_zip(b, after_link)
        self.assertEqual(_ids(b), self.live)
        restore.restore_zip(b, self.before_link)
        self.assertEqual(_ids(b), self.live)

    def test_a_charge_deleted_since_the_archive_still_comes_back(self):
        # the un-carried Safeway twin was deleted by the person; nothing
        # live holds it, so the restore brings it back as it always did —
        # under either archived id, since the two charges are identical
        self.a.execute("DELETE FROM transactions WHERE id = %s",
                       (self.safeway[1],))
        restore.restore_zip(self.a, self.before_link)
        names = [r["name"] for r in self.a.execute(
            "SELECT name FROM transactions ORDER BY date, name").fetchall()]
        self.assertEqual(names, ["SAFEWAY STORE", "SAFEWAY STORE",
                                 "EMPLOYER PAYROLL", "CORNER BAKERY"])
        self.assertIn("plaid-t1", _ids(self.a))


if __name__ == "__main__":
    unittest.main()
