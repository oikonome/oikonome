"""The per-sync categorize pass must not sweep the whole ledger.

On a ledger of tens of thousands of rows the unscoped apply costs minutes to
update ZERO rows — at the end of EVERY sync, cron included — and emits no
progress while it runs, which is what the header chip reports as
"finishing up…". These tests pin the two properties scoping buys:

 * scoping is an OPTIMISATION, never a behaviour change — a scoped apply
   writes exactly what the unscoped one would for the merchants in scope;
 * an empty scope means "nothing was touched", so apply does no work at
   all rather than falling back to the full sweep (that bug would be
   silent: correct output, minutes late).

The nightly run deliberately keeps the full sweep — that is where a rule
disagreeing with an untouched merchant reconciles.
"""

import unittest

from oikonome.engine import llm_categorize

from .test_llm_categorize import add_raw_txn, cache
from .util import make_db


class ApplyScopeTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _cat(self, tid):
        return self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id=%s",
            (tid,)).fetchone()["category_primary"]

    def _two_merchants(self):
        """One row each for two merchants, both with a cached rule that
        disagrees with the row's current generic category."""
        add_raw_txn(self.conn, "a1", "2025-07-01", 12, "Bean Haus",
                    primary="GENERAL_MERCHANDISE", raw={})
        add_raw_txn(self.conn, "b1", "2025-07-01", 30, "Ace Hardware",
                    primary="GENERAL_MERCHANDISE", raw={})
        cache(self.conn, "Bean Haus", "FOOD_AND_DRINK")
        cache(self.conn, "Ace Hardware", "HOME_IMPROVEMENT")

    def test_empty_scope_does_nothing(self):
        """canons=[] means nothing was touched. It must NOT be read as
        'no scope given' and fall through to the full sweep."""
        self._two_merchants()
        self.assertEqual(0, llm_categorize.apply(self.conn, canons=[]))
        self.assertEqual("GENERAL_MERCHANDISE", self._cat("a1"))
        self.assertEqual("GENERAL_MERCHANDISE", self._cat("b1"))

    def test_none_scope_still_sweeps_everything(self):
        """canons=None keeps the old full-table behaviour — the nightly
        run() depends on it."""
        self._two_merchants()
        self.assertEqual(2, llm_categorize.apply(self.conn))
        self.assertEqual("FOOD_AND_DRINK", self._cat("a1"))
        self.assertEqual("HOME_IMPROVEMENT", self._cat("b1"))

    def test_scope_writes_only_its_own_merchants(self):
        self._two_merchants()
        self.assertEqual(1, llm_categorize.apply(
            self.conn, canons=["Bean Haus"]))
        self.assertEqual("FOOD_AND_DRINK", self._cat("a1"))
        self.assertEqual("GENERAL_MERCHANDISE", self._cat("b1"))

    def test_scoped_matches_unscoped_for_rows_in_scope(self):
        """Row-equivalence: whatever the full sweep would write for a
        merchant, the scoped sweep writes identically."""
        self._two_merchants()
        llm_categorize.apply(self.conn, canons=["Bean Haus", "Ace Hardware"])
        scoped = (self._cat("a1"), self._cat("b1"))
        self.conn.execute(
            "UPDATE transactions SET category_primary='GENERAL_MERCHANDISE'")
        llm_categorize.apply(self.conn)
        self.assertEqual(scoped, (self._cat("a1"), self._cat("b1")))

    def test_scope_accepts_a_raw_descriptor(self):
        """flow_classify collects raw descriptors while apply_seed collects
        canonicals; both land in the same touched set, so a raw name that is
        nobody's canonical must still scope correctly."""
        self._two_merchants()
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical) "
            "VALUES (%s,%s)", ("Bean Haus #42", "Bean Haus"))
        add_raw_txn(self.conn, "a2", "2025-07-02", 9, "Bean Haus #42",
                    primary="GENERAL_MERCHANDISE", raw={})
        llm_categorize.apply(self.conn, canons=["Bean Haus"])
        # the canonical expands to its raw variants
        self.assertEqual("FOOD_AND_DRINK", self._cat("a2"))
        self.assertEqual("GENERAL_MERCHANDISE", self._cat("b1"))

    def test_canon_singular_still_works(self):
        """The interactive recategorize paths pass canon=<one merchant>."""
        self._two_merchants()
        self.assertEqual(1, llm_categorize.apply(
            self.conn, canon="Bean Haus"))
        self.assertEqual("FOOD_AND_DRINK", self._cat("a1"))
        self.assertEqual("GENERAL_MERCHANDISE", self._cat("b1"))


class TouchedCollectionTests(unittest.TestCase):
    """categorize_new scopes apply() to what its own passes wrote, so those
    passes have to report it."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_apply_seed_reports_what_it_cached(self):
        add_raw_txn(self.conn, "s1", "2025-07-01", 20, "Costco",
                    primary=None, raw={})
        stats = {}
        llm_categorize.apply_seed(self.conn, stats)
        if stats.get("seeded"):
            self.assertIn("Costco", stats["touched"])

    def test_flow_classify_reports_what_it_matched(self):
        add_raw_txn(self.conn, "f1", "2025-07-01", 400,
                    "CHASE CREDIT CRD AUTOPAY", primary=None, raw={})
        stats = {}
        llm_categorize.flow_classify(self.conn, stats)
        self.assertIn("CHASE CREDIT CRD AUTOPAY", stats["touched"])

    def test_categorize_new_does_not_leak_touched_into_stats(self):
        """`touched` is internal plumbing — callers log these stats."""
        add_raw_txn(self.conn, "c1", "2025-07-01", 12, "Bean Haus",
                    primary="GENERAL_MERCHANDISE", raw={})
        cache(self.conn, "Bean Haus", "FOOD_AND_DRINK")
        stats = llm_categorize.categorize_new(self.conn)
        self.assertNotIn("touched", stats)

    def test_categorize_new_still_sweeps_the_whole_ledger(self):
        """Pins WHY scoping categorize_new was reverted: a cached rule has to
        reach a row nothing in this run touched. Scoping broke exactly this
        (test_llm_categorize.test_applies_cached_map_when_unconfigured), so
        the per-sync pass stays unscoped until apply() itself is rewritten."""
        add_raw_txn(self.conn, "q1", "2025-07-01", 12, "Bean Haus",
                    primary="GENERAL_MERCHANDISE", raw={})
        cache(self.conn, "Bean Haus", "FOOD_AND_DRINK")
        # nothing to seed, nothing to flow-classify, no backend configured
        self.conn.execute("DELETE FROM merchant_categories WHERE source='seed'")
        stats = llm_categorize.categorize_new(self.conn)
        self.assertEqual(1, stats["rows_updated"])
        self.assertEqual("FOOD_AND_DRINK", self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id='q1'"
        ).fetchone()["category_primary"])


if __name__ == "__main__":
    unittest.main()
