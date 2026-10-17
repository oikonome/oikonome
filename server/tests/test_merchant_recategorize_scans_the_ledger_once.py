"""A merchant-wide recategorize reads the ledger once for the whole token
family, not once per canonical in it: the display key it matches on cannot
be served by an index, and the write runs under the merchant-identity lock
every rename and merge waits on. The single scan must count, snapshot and
move exactly the rows the per-canonical reading did."""
import unittest

from oikonome.web import data

from .util import TODAY, _ensure_db, add_txn, make_db, write_config

_DISPLAY = data._DISPLAY_MERCHANT


class _Counting:
    """The connection, counting statements that scan the ledger by the
    display-merchant key."""

    def __init__(self, conn):
        self._conn = conn
        self.scans = 0

    def execute(self, sql, *a, **kw):
        if isinstance(sql, str) and "FROM transactions" in sql \
                and _DISPLAY in sql:
            self.scans += 1
        return self._conn.execute(sql, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class MerchantRecategorizeScansOnce(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _family(self, prefix, n):
        # n descriptors sharing the payee's words, each its own canonical
        for i in range(n):
            add_txn(self.conn, TODAY, 10.0 + i, f"{prefix} BOOKS {i:02d}",
                    primary="GENERAL_MERCHANDISE")
        add_txn(self.conn, TODAY, 5.0, f"{prefix} BOOKS 00",
                primary="TRANSFER_OUT")
        add_txn(self.conn, TODAY, 7.0, f"{prefix} BOOKS 01",
                override="ENTERTAINMENT")

    def _per_canonical(self, payee, category):
        """The count the per-canonical reading gave: every non-overridden
        row of every family canonical whose category differs."""
        ids = set()
        for c in data._family_canons(self.conn, payee):
            ids |= {r["id"] for r in data._merchant_targets(self.conn, c)
                    if r["category_primary"] != category}
        return ids

    def _preview_scans(self, payee):
        probe = _Counting(self.conn)
        data.merchant_category_preview(probe, payee, "SHOPPING_BOOKS")
        return probe.scans

    def test_preview_scans_do_not_grow_with_the_family(self):
        self._family("CORNER", 2)
        self._family("HARBOR", 9)
        small = self._preview_scans("Corner Books")
        large = self._preview_scans("Harbor Books")
        self.assertEqual(small, large)

    def test_preview_counts_what_the_per_canonical_reading_counted(self):
        self._family("HARBOR", 6)
        expected = self._per_canonical("Harbor Books", "SHOPPING_BOOKS")
        p = data.merchant_category_preview(self.conn, "Harbor Books",
                                           "SHOPPING_BOOKS")
        self.assertGreaterEqual(
            len(data._family_canons(self.conn, "Harbor Books")), 6)
        self.assertEqual(p["count"], len(expected))
        self.assertEqual(p["flow_rows"], 1)
        self.assertEqual(p["skipped_override"], 1)

    def _write_scans(self, payee):
        probe = _Counting(self.conn)
        data.set_merchant_category(probe, payee, "SHOPPING_BOOKS")
        return probe.scans

    def test_write_scans_do_not_grow_with_the_family(self):
        self._family("CORNER", 2)
        self._family("HARBOR", 9)
        self.assertEqual(self._write_scans("Corner Books"),
                         self._write_scans("Harbor Books"))

    def test_write_moves_and_snapshots_the_same_rows(self):
        self._family("HARBOR", 6)
        expected = self._per_canonical("Harbor Books", "SHOPPING_BOOKS")
        r = data.set_merchant_category(self.conn, "Harbor Books",
                                       "SHOPPING_BOOKS")
        self.assertEqual({a["id"] for a in r["undo"]["affected"]}, expected)
        self.assertEqual(r["count"], len(expected))
        moved = {x["id"] for x in self.conn.execute(
            "SELECT id FROM transactions WHERE category_primary = "
            "'SHOPPING_BOOKS' AND category_override IS NULL")}
        self.assertEqual(moved, expected)


if __name__ == "__main__":
    unittest.main()
