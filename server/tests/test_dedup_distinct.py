"""Same-day + same-amount alone must not eat genuinely distinct rows.

Both cross-source matchers (the file importers' Deduper and the
aggregator-side ImportOverlapGuard) match a same-day exact-amount hit.
Opaque bank descriptors ("POS DEBIT 4417") carry no substantive name
tokens, and those match on amount+date alone. But when BOTH sides carry
real merchant tokens and they conflict outright (no overlap, not even a
squished-name substring), the rows are two different purchases and both
must survive; otherwise two $4.50 coffees at two different shops on the
same day collapse into one.
"""

import datetime as dt
import unittest

from oikonome.sync import base, csvimport
from oikonome.sync.dedup import _distinct_names, _tokens

from .util import add_txn, make_db, write_config

CSV_MAPPING = {"date": "Date", "amount": "Amount", "name": "Description"}


class DistinctNamesTests(unittest.TestCase):
    def test_conflicting_substantive_names_are_distinct(self):
        self.assertTrue(_distinct_names(_tokens("BLUE BOTTLE"),
                                        _tokens("STARBUCKS")))

    def test_opaque_descriptor_is_never_distinct(self):
        # "POS DEBIT 4417" is all stopwords/digits — no name evidence
        self.assertFalse(_distinct_names(_tokens("POS DEBIT 4417"),
                                         _tokens("SAFEWAY FUEL")))
        self.assertFalse(_distinct_names(_tokens("SAFEWAY FUEL"),
                                         _tokens("POS DEBIT 4417")))

    def test_squished_names_are_not_distinct(self):
        # bank descriptors squish spaces: BURGERKING vs BURGER KING
        self.assertFalse(_distinct_names(_tokens("BURGERKING 123"),
                                         _tokens("BURGER KING")))


class DeduperDistinctTests(unittest.TestCase):
    """Import-side (file import over existing aggregator rows)."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_two_distinct_same_day_same_amount_purchases_both_survive(self):
        # existing aggregator row: $4.50 at Starbucks; the file carries a
        # $4.50 charge at a DIFFERENT coffee shop the same day
        add_txn(self.conn, dt.date(2026, 7, 10), 4.50, "STARBUCKS",
                account="chk", merchant="Starbucks")
        r = csvimport.import_csv(
            self.conn, "chk",
            "Date,Amount,Description\n2026-07-10,-4.50,BLUE BOTTLE\n",
            CSV_MAPPING)
        self.assertEqual(r["skipped_duplicates"], 0)
        self.assertEqual(r["imported"], 1)

    def test_opaque_descriptor_still_matches_same_day(self):
        # the aggregator-overlap case the matcher exists for: the bank CSV
        # names are opaque, only amount+date can link them
        add_txn(self.conn, dt.date(2026, 7, 10), 4.50, "BLUE BOTTLE",
                account="chk", merchant="Blue Bottle")
        r = csvimport.import_csv(
            self.conn, "chk",
            "Date,Amount,Description\n2026-07-10,-4.50,POS DEBIT 4417\n",
            CSV_MAPPING)
        self.assertEqual(r["skipped_duplicates"], 1)
        self.assertEqual(r["imported"], 0)

    def test_squished_name_still_matches_same_day(self):
        add_txn(self.conn, dt.date(2026, 7, 10), 12.00, "BURGER KING",
                account="chk", merchant="Burger King")
        r = csvimport.import_csv(
            self.conn, "chk",
            "Date,Amount,Description\n2026-07-10,-12.00,BURGERKING 4432\n",
            CSV_MAPPING)
        self.assertEqual(r["skipped_duplicates"], 1)


class OverlapGuardDistinctTests(unittest.TestCase):
    """Aggregator-side (incoming sync over earlier file imports)."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _incoming(self, name, amount=4.50, day=10):
        return base.Transaction(
            id=f"sfin:acc-chk:{name}", account_id="chk",
            date=dt.date(2026, 7, day), amount=amount, name=name)

    def test_distinct_merchants_same_day_both_kept(self):
        # earlier file import: $4.50 at Blue Bottle
        csvimport.import_csv(
            self.conn, "chk",
            "Date,Amount,Description\n2026-07-10,-4.50,BLUE BOTTLE\n",
            CSV_MAPPING)
        kept, skipped = base.filter_import_duplicates(
            self.conn, [self._incoming("STARBUCKS")])
        self.assertEqual(skipped, 0)
        self.assertEqual(len(kept), 1)

    def test_opaque_import_row_still_covers_same_day(self):
        csvimport.import_csv(
            self.conn, "chk",
            "Date,Amount,Description\n2026-07-10,-4.50,POS DEBIT 4417\n",
            CSV_MAPPING)
        kept, skipped = base.filter_import_duplicates(
            self.conn, [self._incoming("SAFEWAY FUEL")])
        self.assertEqual(skipped, 1)
        self.assertEqual(kept, [])

    def test_name_overlap_within_window_still_covers(self):
        # the migration case: posting drift + matching name
        csvimport.import_csv(
            self.conn, "chk",
            "Date,Amount,Description\n2026-07-08,-4.50,SAFEWAY STORE 123\n",
            CSV_MAPPING)
        kept, skipped = base.filter_import_duplicates(
            self.conn, [self._incoming("Safeway")])
        self.assertEqual(skipped, 1)


if __name__ == "__main__":
    unittest.main()
