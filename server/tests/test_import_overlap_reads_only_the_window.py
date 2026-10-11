"""The feed-vs-file-import guard reads only the file rows a pull could match.

Every pull of every feed item builds the guard. Reading the household's
whole imported history each time — a decade of rows, tokenised in Python,
hourly per item — is work that matches nothing, because the incoming
charges are days old. The guard reads the incoming dates' window (± the
matching slack) and nothing else, and a later page that reaches further
reads only what lies outside what it already has, so a file row consumed
on an earlier page is never offered again.
"""

import datetime as dt
import unittest

from oikonome.sync import base
from oikonome.sync.base import ImportOverlapGuard, Transaction

from .util import add_txn, make_db, seed_accounts

D0 = dt.date(2026, 6, 15)


def _feed(tid, date, amount, name):
    return Transaction(id=tid, account_id="checking", date=date,
                       amount=amount, name=name)


class ImportOverlapWindowTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        seed_accounts(self.conn)
        # ten years of imported history, one row a month
        for i in range(120):
            add_txn(self.conn, D0 - dt.timedelta(days=30 * (i + 2)),
                    10 + i, f"OLD SHOP {i}", txn_id=f"csv:old-{i}")
        add_txn(self.conn, D0, 42.0, "CORNER MARKET", txn_id="csv:recent-a")
        add_txn(self.conn, D0, 42.0, "CORNER MARKET", txn_id="csv:recent-b")

    def _indexed(self, guard):
        return {c["id"] for cands in guard._by_cents.values() for c in cands}

    def test_nothing_is_read_before_a_page_brings_rows(self):
        guard = ImportOverlapGuard(self.conn)
        self.assertEqual(self._indexed(guard), set())

    def test_a_page_reads_only_its_window(self):
        guard = ImportOverlapGuard(self.conn)
        kept, skipped = base.filter_import_duplicates(
            self.conn, [_feed("p1", D0 + dt.timedelta(days=1), 42.0,
                              "CORNER MARKET")], guard)
        self.assertEqual((kept, skipped), ([], 1))
        self.assertTrue(self._indexed(guard) <= {"csv:recent-a",
                                                 "csv:recent-b"},
                        "file rows far outside the page's dates were read")

    def test_a_consumed_row_is_not_offered_again_by_a_wider_page(self):
        guard = ImportOverlapGuard(self.conn)
        first = [_feed("p1", D0, 42.0, "CORNER MARKET")]
        _, skipped = base.filter_import_duplicates(self.conn, first, guard)
        self.assertEqual(skipped, 1)
        # the next page reaches further back AND covers the same day: two
        # more same-day charges, and only one file row is left to cover them
        second = [_feed("p2", D0, 42.0, "CORNER MARKET"),
                  _feed("p3", D0, 42.0, "CORNER MARKET"),
                  _feed("p4", D0 - dt.timedelta(days=40), 5.0, "ELSEWHERE")]
        kept, skipped = base.filter_import_duplicates(self.conn, second,
                                                      guard)
        self.assertEqual(skipped, 1)
        self.assertEqual({t.id for t in kept}, {"p3", "p4"})

    def test_an_old_charge_still_finds_its_old_file_row(self):
        old = D0 - dt.timedelta(days=30 * 50)
        guard = ImportOverlapGuard(self.conn)
        _, skipped = base.filter_import_duplicates(
            self.conn, [_feed("p9", old + dt.timedelta(days=2), 58.0,
                              "OLD SHOP 48")], guard)
        self.assertEqual(skipped, 1)


if __name__ == "__main__":
    unittest.main()
