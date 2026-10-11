"""Resolving an import's merchants reads the ledger a fixed number of times.

A file import with no merchant column gives every row its bank line as its
identity key, so each distinct line is a group that may adopt the merchant
other rows under the same line already have. Asked one line at a time,
that question read the household's whole ledger twice per line, inside
the one transaction that holds the household's identity lock: a large
import of mostly unique lines became quadratic and held every later
resolve for that household behind it. The answers are now gathered for
every line together, so the reads do not grow with the number of lines.
"""
import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import merchant_identity
from oikonome.sync import base

from .util import make_db

DAY = dt.date(2026, 3, 1)


class ResolveReadsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        # an established line the import's rows can adopt from
        base.upsert_transactions(self.conn, [base.Transaction(
            id=f"feed:{i}", account_id="chk", date=DAY, amount=4.0 + i,
            name="LANTERN COFFEE 0042", merchant_name="Lantern Coffee")
            for i in range(3)])
        merchant_identity.resolve(self.conn)

    def _ledger_reads(self, n_lines, tag):
        rows = [base.Transaction(
            id=f"csv:{tag}{i}", account_id="chk", date=DAY, amount=1.0 + i,
            name=f"KIOSK {tag.upper()} {i:04d} SOMEWHERE")
            for i in range(n_lines)]
        rows.append(base.Transaction(
            id=f"csv:{tag}-adopt", account_id="chk", date=DAY, amount=9.0,
            name="LANTERN COFFEE 0042"))
        for t in rows:
            t.raw = {}
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name)"
            " SELECT * FROM unnest(%s::text[], %s::text[], %s::date[],"
            " %s::float8[], %s::text[])",
            ([t.id for t in rows], [t.account_id for t in rows],
             [t.date for t in rows], [t.amount for t in rows],
             [t.name for t in rows]))
        real = self.conn.execute
        reads = []

        def counting(sql, *a, **kw):
            if "FROM transactions" in str(sql):
                reads.append(sql)
            return real(sql, *a, **kw)
        with mock.patch.object(self.conn, "execute", side_effect=counting):
            merchant_identity.resolve(self.conn,
                                      txn_ids=[t.id for t in rows])
        adopted = self.conn.execute(
            "SELECT m.name FROM transactions t JOIN merchants m"
            " ON m.id = t.merchant_id WHERE t.id = %s",
            (f"csv:{tag}-adopt",)).fetchone()
        self.assertEqual(adopted["name"], "Lantern Coffee")
        return len(reads)

    def test_reads_do_not_grow_with_the_number_of_bank_lines(self):
        few = self._ledger_reads(3, "a")
        many = self._ledger_reads(40, "b")
        self.assertEqual(many, few)


if __name__ == "__main__":
    unittest.main()
