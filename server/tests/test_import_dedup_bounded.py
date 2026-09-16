"""Cross-source import dedup does bounded work inside the request.

The Deduper runs synchronously in the upload handler, and both sides of
its work product need hard limits: the candidate fetch (the account's
existing history in the file's date window) and the per-row scan. A claim
must not scan everything fetched; otherwise a decade-spanning file over a
large account costs O(rows × candidates) comparisons, pinning a worker and
a pooled DB connection. Candidates are indexed by (amount, date) so a
claim only examines rows that could match, the fetch refuses a
pathological account, and a per-claim ceiling bounds the worst case where
thousands of rows share one amount/date cell.
"""

import datetime as dt
import unittest

from oikonome.sync.dedup import Deduper

from .util import add_txn, make_db, write_config


class CandidateFetchBoundTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_pathological_history_refuses_instead_of_scanning(self):
        d = dt.date(2026, 7, 10)
        for i in range(12):
            add_txn(self.conn, d, 4.50, f"SHOP {i}", account="chk",
                    txn_id=f"agg:{i}")
        with self.assertRaises(ValueError) as e:
            Deduper(self.conn, "chk", "csv", [d], max_candidates=10)
        self.assertIn("too much existing history", str(e.exception))

    def test_normal_history_still_dedups(self):
        d = dt.date(2026, 7, 10)
        add_txn(self.conn, d, 4.50, "STARBUCKS", account="chk",
                txn_id="agg:a")
        dd = Deduper(self.conn, "chk", "csv", [d])
        self.assertTrue(dd.claim(d, 4.50, "STARBUCKS COFFEE"))
        # one-to-one: the matched row is consumed
        self.assertFalse(dd.claim(d, 4.50, "STARBUCKS COFFEE"))


class ClaimScanBoundTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _loaded(self, ceiling, n_decoys=20):
        """A Deduper whose one (amount, date) cell holds `n_decoys`
        non-matching candidates before the real match — deterministic
        ordering, which a DB fetch can't promise."""
        d = dt.date(2026, 7, 10)
        dd = Deduper(self.conn, "chk", "csv", [], scan_ceiling=ceiling)
        cell = [{"tokens": {f"decoy{i}"}, "used": False}
                for i in range(n_decoys)]
        cell.append({"tokens": {"starbucks"}, "used": False})
        dd._by_cell[(450, d)] = cell
        return dd, d

    def test_scan_stops_at_the_ceiling(self):
        """Bounded work beats a perfect match: past the ceiling the row is
        treated as new (a possible duplicate imports; nothing is lost)."""
        dd, d = self._loaded(ceiling=5)
        self.assertFalse(dd.claim(d, 4.50, "STARBUCKS"))

    def test_the_same_match_lands_under_a_looser_ceiling(self):
        dd, d = self._loaded(ceiling=1000)
        self.assertTrue(dd.claim(d, 4.50, "STARBUCKS"))

    def test_off_day_match_prefers_the_nearest_date(self):
        """The bucketed scan prefers the nearest date for off-day name
        matches."""
        d = dt.date(2026, 7, 10)
        add_txn(self.conn, d - dt.timedelta(days=3), 4.50, "STARBUCKS",
                account="chk", txn_id="agg:far")
        add_txn(self.conn, d - dt.timedelta(days=1), 4.50, "STARBUCKS",
                account="chk", txn_id="agg:near")
        dd = Deduper(self.conn, "chk", "csv",
                     [d - dt.timedelta(days=3), d])
        self.assertTrue(dd.claim(d, 4.50, "STARBUCKS"))
        # the nearer candidate (1 day off) was the one consumed
        near_cell = dd._by_cell[(450, d - dt.timedelta(days=1))]
        self.assertTrue(all(c["used"] for c in near_cell))
        self.assertTrue(dd.claim(d - dt.timedelta(days=3), 4.50,
                                 "STARBUCKS"))
        # both consumed — a third identical row finds nothing
        self.assertFalse(dd.claim(d, 4.50, "STARBUCKS"))


if __name__ == "__main__":
    unittest.main()
