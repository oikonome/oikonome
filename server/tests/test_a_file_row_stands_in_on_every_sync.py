"""A file-imported row stands in for a bank-feed row on every sync.

The import-overlap guard consumes a file row once per sync run, so two
same-amount charges in ONE pull stay two. Across runs it offers every file
row again: a feed that re-sends its window each pull (SimpleFIN, MX), or
settles a pending charge under a new id, keeps finding the file row that
already represents the charge, and the charge is counted once.

Known limitation, accepted with that rule: a genuine second charge of the
same amount at the same shop, inside the import window, that arrives on a
LATER sync is also taken as covered and set aside. The guard cannot tell it
from a re-sent or settled row without remembering what it set aside, and
remembering that is what made the earlier design count settled charges
twice.
"""
import datetime as dt
import unittest

from oikonome.engine import budget
from oikonome.sync import base, batches, csvimport, simplefin

from .test_simplefin import CSV_MAPPING, _transport
from .util import make_db, write_config

DAY = dt.date(2026, 9, 30)


class StandsInEverySyncTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name)"
            " VALUES ('files', 'manual', 'Files')")
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype)"
            " VALUES ('filechk', 'files', 'Checking (file)', 'depository',"
            " 'checking')")
        batch = batches.create(self.conn, "csv", "filechk", "a.csv")
        rows = batches.tag(self.conn, [base.Transaction(
            id="csv:last", account_id="filechk", date=DAY, amount=5.25,
            name="LANTERN COFFEE")], batch)
        base.upsert_transactions(self.conn, rows)
        batches.finish(self.conn, batch, 1)

    def tearDown(self):
        self.conn.close()

    def _feed(self, tid, day, amount=5.25):
        return base.Transaction(
            id=tid, account_id="chk", date=day, amount=amount,
            name="LANTERN COFFEE 0042", merchant_name="Lantern Coffee",
            raw={"transaction_id": tid})

    def _sync(self, *rows):
        """One sync run: a fresh guard, as every hourly pull builds."""
        kept, skipped = base.filter_import_duplicates(self.conn, list(rows))
        base.upsert_transactions(self.conn, kept)
        return kept, skipped

    def test_one_file_row_covers_one_feed_row_per_run(self):
        kept, skipped = self._sync(
            self._feed("feed:a", DAY),
            self._feed("feed:b", DAY + dt.timedelta(days=1)))
        self.assertEqual(([t.id for t in kept], skipped), (["feed:b"], 1))

    def test_a_same_amount_charge_on_a_later_sync_is_set_aside(self):
        """The known limitation in the module docstring, pinned so a change
        to it is a decision, not an accident."""
        self.assertEqual(self._sync(self._feed("feed:a", DAY))[1], 1)
        later, skipped = self._sync(
            self._feed("feed:b", DAY + dt.timedelta(days=2)))
        self.assertEqual((later, skipped), ([], 1))

    def test_a_charge_outside_the_import_window_is_always_new(self):
        kept, skipped = self._sync(
            self._feed("feed:far", DAY + dt.timedelta(days=10)))
        self.assertEqual(([t.id for t in kept], skipped), (["feed:far"], 0))

    def test_an_unusable_amount_matches_nothing(self):
        kept, skipped = self._sync(self._feed("feed:inf", DAY, float("inf")))
        self.assertEqual(skipped, 0)


class SimpleFinPendingThenPostedTests(unittest.TestCase):
    """A SimpleFIN pending hold the file covers, then its posted version
    under a new id, is one charge in the ledger."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _pull(self, txn):
        payload = {"errors": [], "accounts": [
            {"id": "acc-chk", "name": "Everyday Checking", "currency": "USD",
             "balance": "1500.00", "org": {"name": "Demo Bank"},
             "transactions": [txn]}]}
        return simplefin.sync(self.conn, "sfin-demo",
                              "https://u:p@bridge.test/simplefin",
                              transport=_transport(payload))

    def test_counted_once(self):
        csvimport.import_csv(self.conn, "chk",
                             "Date,Amount,Description\n"
                             "2025-07-15,-42.50,SAFEWAY STORE 123\n",
                             CSV_MAPPING)
        self.assertEqual(self._pull(
            {"id": "p1", "posted": 1752537600, "amount": "-42.50",
             "description": "SAFEWAY STORE 123", "payee": "Safeway",
             "pending": True})["skipped_import_duplicates"], 1)
        # posted a day later under the id the bank gives it on posting
        self.assertEqual(self._pull(
            {"id": "t1", "posted": 1752624000, "amount": "-42.50",
             "description": "SAFEWAY STORE 123", "payee": "Safeway"}
        )["skipped_import_duplicates"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM transactions WHERE id LIKE 'sfin:%'"
        ).fetchone()["n"], 0)
        st = budget.month_status(self.conn, dt.date(2025, 7, 15))
        self.assertEqual(round(st["total_actual"], 2), 42.50)
        # and the file row is marked, so undoing the import keeps it
        raw = self.conn.execute(
            "SELECT raw FROM transactions WHERE id LIKE 'csv:%'"
        ).fetchone()["raw"]
        self.assertEqual(raw[base.STOOD_IN_KEY]["source"], "simplefin-org")


if __name__ == "__main__":
    unittest.main()
