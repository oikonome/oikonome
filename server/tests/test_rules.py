"""learned categorization rules are inspectable + controllable —
list with provenance, disable (stops applying), re-enable (re-sweeps),
delete. A disabled rule must not categorize."""

import unittest

from oikonome.engine import llm_categorize
from oikonome.web import data

from .test_canonical_rules import canon, cat
from .test_llm_categorize import add_raw_txn, cache
from .util import make_db


class RulesTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_disabled_rule_does_not_categorize(self):
        cache(self.conn, "NETFLIX.COM", "ENTERTAINMENT")
        self.conn.execute("UPDATE merchant_categories SET disabled=true "
                          "WHERE merchant='NETFLIX.COM'")
        add_raw_txn(self.conn, "a", "2025-07-01", 15, "NETFLIX.COM",
                    primary="OTHER",
                    raw={"personal_finance_category": {"primary": "OTHER"}})
        llm_categorize.apply(self.conn)
        self.assertEqual(cat(self.conn, "a"), "OTHER")   # rule inert

    def test_reenable_sweeps_past_rows(self):
        cache(self.conn, "NETFLIX.COM", "ENTERTAINMENT")
        self.conn.execute("UPDATE merchant_categories SET disabled=true "
                          "WHERE merchant='NETFLIX.COM'")
        add_raw_txn(self.conn, "a", "2025-07-01", 15, "NETFLIX.COM",
                    primary="OTHER",
                    raw={"personal_finance_category": {"primary": "OTHER"}})
        llm_categorize.apply(self.conn)
        self.assertEqual(cat(self.conn, "a"), "OTHER")
        data.set_rule_disabled(self.conn, "NETFLIX.COM", False)   # re-enable
        self.assertEqual(cat(self.conn, "a"), "ENTERTAINMENT")

    def test_list_rules_shows_provenance_and_count(self):
        canon(self.conn, "NFLX", "Netflix")
        cache(self.conn, "Netflix", "ENTERTAINMENT")            # llm rule
        data.upsert_user_rule = getattr(data, "upsert_user_rule", None)
        add_raw_txn(self.conn, "a", "2025-07-01", 15, "NFLX", primary="OTHER")
        rules = data.list_rules(self.conn)["rules"]
        by_m = {r["merchant"]: r for r in rules}
        self.assertIn("Netflix", by_m)
        self.assertEqual(by_m["Netflix"]["source"], "llm")
        self.assertFalse(by_m["Netflix"]["disabled"])
        self.assertEqual(by_m["Netflix"]["count"], 1)          # NFLX→Netflix

    def test_delete_rule(self):
        cache(self.conn, "NETFLIX.COM", "ENTERTAINMENT")
        self.assertEqual(data.delete_rule(self.conn, "NETFLIX.COM"), 1)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM merchant_categories WHERE merchant='NETFLIX.COM'"
        ).fetchone())

    def test_user_edit_reenables_a_disabled_rule(self):
        cache(self.conn, "NETFLIX.COM", "ENTERTAINMENT")
        self.conn.execute("UPDATE merchant_categories SET disabled=true "
                          "WHERE merchant='NETFLIX.COM'")
        llm_categorize.upsert_user_rule(self.conn, "NETFLIX.COM", "SHOPPING")
        row = self.conn.execute(
            "SELECT disabled, source, category_primary FROM merchant_categories "
            "WHERE merchant='NETFLIX.COM'").fetchone()
        self.assertFalse(row["disabled"])
        self.assertEqual(row["source"], "user")
        self.assertEqual(row["category_primary"], "SHOPPING")


if __name__ == "__main__":
    unittest.main()
