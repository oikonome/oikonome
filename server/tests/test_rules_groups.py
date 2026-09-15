"""The Rules page is grouped by provenance, with server-side
search + pagination. list_rules filters by source, searches merchant AND
category, pages at ~50, and always returns q-filtered per-source counts
(collapsed headers need counts without loading rows; the Model-learned
group hides at zero). Editing OR disabling a model/seed rule PROMOTES it
to source='user'; re-enabling a promoted rule keeps source='user'."""

import unittest

from oikonome.web import data

from .test_llm_categorize import cache
from .util import make_db


class RulesPaginationTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        # 60 llm rules so the default 50/page splits into 2 pages
        for i in range(60):
            cache(self.conn, f"MERCHANT {i:03d}", "GENERAL_MERCHANDISE")
        self.conn.execute("UPDATE merchant_categories SET source='seed' "
                          "WHERE merchant < 'MERCHANT 010'")

    def tearDown(self):
        self.conn.close()

    def test_pagination_pages_at_50(self):
        out = data.list_rules(self.conn, source="llm")
        self.assertEqual(out["per_page"], 50)
        self.assertEqual(out["total"], 50)
        self.assertEqual(len(out["rules"]), 50)
        out = data.list_rules(self.conn, source="llm", per_page=30)
        self.assertEqual(len(out["rules"]), 30)
        out2 = data.list_rules(self.conn, source="llm", per_page=30, page=2)
        self.assertEqual(len(out2["rules"]), 20)
        # pages are disjoint and ordered
        m1 = {r["merchant"] for r in out["rules"]}
        m2 = {r["merchant"] for r in out2["rules"]}
        self.assertFalse(m1 & m2)

    def test_per_source_counts_ride_every_response(self):
        out = data.list_rules(self.conn, source="seed")
        self.assertEqual(out["counts"], {"user": 0, "llm": 50, "seed": 10, "model": 0})
        self.assertEqual(out["total"], 10)

    def test_search_spans_merchant_and_category(self):
        out = data.list_rules(self.conn, q="MERCHANT 05")
        self.assertEqual(out["total"], 10)            # 050..059
        # category search hits too, and counts reflect the q filter
        out = data.list_rules(self.conn, q="general_merch")
        self.assertEqual(out["counts"]["llm"], 50)
        self.assertEqual(out["counts"]["seed"], 10)
        out = data.list_rules(self.conn, q="zzz-no-such")
        self.assertEqual(out["total"], 0)
        self.assertEqual(out["counts"], {"user": 0, "llm": 0, "seed": 0, "model": 0})


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _source(self, merchant):
        return self.conn.execute(
            "SELECT source, disabled FROM merchant_categories "
            "WHERE merchant=%s", (merchant,)).fetchone()

    def test_disable_promotes_to_user(self):
        cache(self.conn, "NETFLIX.COM", "ENTERTAINMENT")     # llm rule
        data.set_rule_disabled(self.conn, "NETFLIX.COM", True)
        row = self._source("NETFLIX.COM")
        self.assertEqual((row["source"], row["disabled"]), ("user", True))

    def test_reenable_keeps_promoted_source(self):
        cache(self.conn, "NETFLIX.COM", "ENTERTAINMENT")
        data.set_rule_disabled(self.conn, "NETFLIX.COM", True)
        data.set_rule_disabled(self.conn, "NETFLIX.COM", False)
        row = self._source("NETFLIX.COM")
        self.assertEqual((row["source"], row["disabled"]), ("user", False))

    def test_edit_promotes_to_user(self):
        cache(self.conn, "COSTCO", "FOOD_AND_DRINK")         # llm rule
        data.set_merchant_category(self.conn, "COSTCO", "GENERAL_MERCHANDISE")
        row = self._source("COSTCO")
        self.assertEqual(row["source"], "user")
        # and it now lists under the user group, not llm
        self.assertEqual(
            [r["merchant"] for r in
             data.list_rules(self.conn, source="user")["rules"]], ["COSTCO"])
        self.assertEqual(data.list_rules(self.conn, source="llm")["total"], 0)

    def test_empty_instance_counts_all_zero(self):
        # the SPA's hidden-when-empty + friendly empty state key off counts
        out = data.list_rules(self.conn)
        self.assertEqual(out["counts"], {"user": 0, "llm": 0, "seed": 0, "model": 0})
        self.assertEqual((out["total"], out["rules"]), (0, []))


class RulesApiParamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        import uuid as _uuid

        from fastapi.testclient import TestClient

        from oikonome.db import tenancy as _tenancy

        from .util import seed_accounts, write_config
        os.environ["OIKONOME_DEV"] = "1"
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"rul-{_uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = _tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            cache(conn, "STARBUCKS", "FOOD_AND_DRINK")
        finally:
            conn.close()

    def test_source_filter_and_counts(self):
        r = self.client.get("/api/rules?source=llm").json()
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["counts"]["llm"], 1)
        r = self.client.get("/api/rules?source=user").json()
        self.assertEqual(r["total"], 0)

    def test_bad_source_rejected(self):
        self.assertEqual(
            self.client.get("/api/rules?source=alien").status_code, 400)

    def test_q_and_page_params(self):
        r = self.client.get("/api/rules?q=starb&page=1").json()
        self.assertEqual([x["merchant"] for x in r["rules"]], ["STARBUCKS"])
        r = self.client.get("/api/rules?q=starb&page=2").json()
        self.assertEqual(r["rules"], [])
        self.assertEqual(r["total"], 1)


if __name__ == "__main__":
    unittest.main()


class RulesSearchTreatsWildcardsAsLiteralTests(unittest.TestCase):
    """The Rules search box takes text a person typed, not a LIKE pattern.

    A percent sign, an underscore or a backslash in the box has to match
    ITSELF: unescaped, `%` matches every rule (and the collapsed
    per-source headers, which reuse the same predicate, report those
    unfiltered counts as if they were search hits), and an underscore —
    which raw category labels still carry — matches any single character.
    """

    def setUp(self):
        self.conn = make_db()
        for m in ("CASHBACK 100% CLUB", "PLAIN GROCER", "A_B COFFEE",
                  "AXB COFFEE", "BACK\\SLASH BAR"):
            cache(self.conn, m, "GENERAL_MERCHANDISE")

    def tearDown(self):
        self.conn.close()

    def test_percent_finds_only_rules_containing_a_percent_sign(self):
        out = data.list_rules(self.conn, q="%")
        self.assertEqual([r["merchant"] for r in out["rules"]],
                         ["CASHBACK 100% CLUB"])
        self.assertEqual(out["total"], 1)
        # the group headers read from the same predicate, so they must
        # narrow with it rather than announce the whole table
        self.assertEqual(out["counts"]["llm"], 1)

    def test_underscore_matches_an_underscore_not_any_character(self):
        out = data.list_rules(self.conn, q="A_B")
        self.assertEqual([r["merchant"] for r in out["rules"]],
                         ["A_B COFFEE"])
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["counts"]["llm"], 1)

    def test_a_backslash_is_searchable_and_escapes_nothing(self):
        out = data.list_rules(self.conn, q="BACK\\SLASH")
        self.assertEqual([r["merchant"] for r in out["rules"]],
                         ["BACK\\SLASH BAR"])
        self.assertEqual(out["total"], 1)

    def test_ordinary_search_still_matches_merchant_and_category(self):
        self.assertEqual(data.list_rules(self.conn, q="coffee")["total"], 2)
        self.assertEqual(
            data.list_rules(self.conn, q="general_merchandise")["total"], 5)
