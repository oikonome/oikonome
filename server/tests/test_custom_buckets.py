"""custom budget buckets.

The frozen contract (spec §4): custom buckets are a DECOMPOSITION layer over
the two fixed variable buckets — the verdict (variable actual vs variable
budget, tolerance, spend exclusions, envelope math) must be bit-identical
with and without them. These tests pin that, plus the carve-out display
math, merchant-beats-category routing, the suggest-from-history medians,
the settings CRUD surface, and the shared-template child bars.
"""

import datetime as dt
import unittest
import uuid

from oikonome.engine import budget
from oikonome.web import report

from .util import TODAY, add_bill, add_txn, make_db, write_config

COFFEE = {"name": "Coffee", "parent": "food", "monthly": 200,
          "categories": [], "merchants": ["Bean Haus"]}
FUN = {"name": "Fun", "parent": "other", "monthly": 300,
       "categories": ["ENTERTAINMENT"], "merchants": []}


class CustomBucketBase(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def status(self, **kw):
        return budget.month_status(self.conn, TODAY, **kw)


class TestDecomposition(CustomBucketBase):
    def test_category_carve_from_other(self):
        """A category bucket pulls its rows out of the parent's DISPLAY
        while the raw (verdict) numbers stay whole."""
        write_config(self.conn, custom_buckets=[FUN])
        add_txn(self.conn, TODAY, 80.0, "CINEMA", primary="ENTERTAINMENT")
        add_txn(self.conn, TODAY, 50.0, "TARGET")
        st = self.status()
        other = st["buckets"]["other"]
        self.assertEqual(other["actual"], 130.0)          # raw — frozen
        self.assertEqual(other["display_actual"], 50.0)   # after carve-out
        kids = other["children"]
        self.assertEqual([(c["name"], c["actual"]) for c in kids],
                         [("Fun", 80.0)])
        # remaining budget = parent budget minus the children's shares
        self.assertEqual(other["remaining_budget"], 700.0)
        self.assertEqual(st["buckets"]["food"]["children"], [])

    def test_category_normalization(self):
        """Config 'FOOD_AND_DRINK' matches the row's display category
        'FOOD AND DRINK' (underscores vs spaces vs case)."""
        write_config(self.conn, custom_buckets=[
            {"name": "Groceries", "parent": "food", "monthly": 400,
             "categories": ["FOOD_AND_DRINK"], "merchants": []}])
        add_txn(self.conn, TODAY, 60.0, "SAFEWAY", primary="FOOD_AND_DRINK")
        st = self.status()
        self.assertEqual(st["buckets"]["food"]["children"][0]["actual"], 60.0)
        self.assertEqual(st["buckets"]["food"]["display_actual"], 0.0)

    def test_merchant_beats_category_cross_parent(self):
        """Spec §2: a merchant rule assigns the txn regardless of category —
        a GENERAL_MERCHANDISE Bean Haus charge still lands in Coffee
        (declared under food), and comes out of OTHER's display."""
        write_config(self.conn, custom_buckets=[COFFEE])
        add_txn(self.conn, TODAY, 7.0, "BEAN HAUS #42")
        add_txn(self.conn, TODAY, 40.0, "TARGET")
        st = self.status()
        coffee = st["buckets"]["food"]["children"][0]
        self.assertEqual(coffee["actual"], 7.0)
        # the row physically sat in 'other' — its display drops the dollars
        self.assertEqual(st["buckets"]["other"]["display_actual"], 40.0)
        self.assertEqual(st["buckets"]["other"]["actual"], 47.0)  # raw frozen

    def test_first_bucket_wins_and_one_bucket_per_row(self):
        write_config(self.conn, custom_buckets=[
            {"name": "A", "parent": "other", "monthly": 100,
             "categories": ["ENTERTAINMENT"], "merchants": []},
            {"name": "B", "parent": "other", "monthly": 100,
             "categories": ["ENTERTAINMENT"], "merchants": []}])
        add_txn(self.conn, TODAY, 30.0, "CINEMA", primary="ENTERTAINMENT")
        kids = self.status()["buckets"]["other"]["children"]
        self.assertEqual([(c["name"], c["actual"]) for c in kids],
                         [("A", 30.0), ("B", 0.0)])

    def test_totals_reconcile(self):
        """display parent + Σ children == raw parent (same-parent rules)."""
        write_config(self.conn, custom_buckets=[FUN])
        for amt in (80.0, 25.0, 44.44):
            add_txn(self.conn, TODAY, amt, "CINEMA", primary="ENTERTAINMENT")
        add_txn(self.conn, TODAY, 66.0, "TARGET")
        o = self.status()["buckets"]["other"]
        self.assertAlmostEqual(
            o["display_actual"] + sum(c["actual"] for c in o["children"]),
            o["actual"], places=2)

    def test_verdict_identical_with_and_without_buckets(self):
        """THE frozen invariant: same ledger, same verdict numbers whether
        or not custom buckets are configured."""
        add_txn(self.conn, TODAY, 900.0, "CINEMA", primary="ENTERTAINMENT")
        add_txn(self.conn, TODAY, 800.0, "SAFEWAY", primary="FOOD_AND_DRINK")
        add_txn(self.conn, TODAY, 7.77, "BEAN HAUS")
        write_config(self.conn)                       # no buckets
        st0 = self.status()
        write_config(self.conn, custom_buckets=[COFFEE, FUN])
        st1 = self.status()
        for key in ("verdict", "variance", "tolerance",
                    "variable_actual", "variable_expected", "total_actual"):
            self.assertEqual(st0[key], st1[key], key)
        for b in ("food", "other", "fixed"):
            for k in ("actual", "expected", "month_budget"):
                self.assertEqual(st0["buckets"][b][k],
                                 st1["buckets"][b][k], f"{b}.{k}")

    def test_dynamic_scale_compresses_children_too(self):
        """When the dynamic budget compresses food/other, the children
        compress by the same factor — the carve-out still describes the
        compressed plan."""
        write_config(self.conn, custom_buckets=[FUN],
                     dynamic_variable_budget=True,
                     budgeted_income_monthly=1000)    # < 2000 static → 0.5
        st = self.status()
        self.assertAlmostEqual(st["variable_scale"]["factor"], 0.5)
        fun = st["buckets"]["other"]["children"][0]
        self.assertAlmostEqual(fun["month_budget"], 150.0)   # 300 × 0.5
        self.assertAlmostEqual(st["buckets"]["other"]["remaining_budget"],
                               350.0)                        # 500 − 150

    def test_no_buckets_display_equals_raw(self):
        write_config(self.conn)
        add_txn(self.conn, TODAY, 10.0, "TARGET")
        st = self.status()
        for b in ("food", "other"):
            v = st["buckets"][b]
            self.assertEqual(v["display_actual"], v["actual"])
            self.assertEqual(v["display_expected"], v["expected"])
            self.assertEqual(v["remaining_budget"], v["month_budget"])
            self.assertEqual(v["children"], [])

    def test_reason_names_overpace_child(self):
        write_config(self.conn, custom_buckets=[
            {"name": "Gadgets", "parent": "other", "monthly": 100,
             "categories": [], "merchants": ["Target"]}])
        add_txn(self.conn, TODAY, 1500.0, "TARGET")
        st = self.status()
        self.assertEqual(st["verdict"], "OVER BUDGET")
        other = next(r for r in st["reasons"] if r["bucket"] == "other")
        self.assertTrue(any("Gadgets bucket" in x and "over pace" in x
                            for x in other["detail"]), other["detail"])

    def test_ruleless_bucket_is_a_plan_placeholder(self):
        """The Budget planner's add-a-category row saves a name and a
        monthly with NO matching rules, and /api/settings accepts that
        deliberately. Such a bucket must still get its own bar —
        $0 spent against its own budget, carved out of the parent — rather
        than vanishing and leaving its dollars inside Everything else."""
        write_config(self.conn, custom_buckets=[
            {"name": "Garden", "parent": "other", "monthly": 500,
             "categories": [], "merchants": []}])
        add_txn(self.conn, TODAY, 50.0, "TARGET")
        other = self.status()["buckets"]["other"]
        self.assertEqual([(c["name"], c["actual"], c["month_budget"])
                          for c in other["children"]],
                         [("Garden", 0.0, 500.0)])
        # it claims no rows, and its budget leaves the unallocated pool
        self.assertEqual(other["actual"], 50.0)          # raw — frozen
        self.assertEqual(other["display_actual"], 50.0)
        self.assertEqual(other["remaining_budget"], 500.0)

    def test_malformed_config_entries_skipped(self):
        """Hand-edited config must not 500 the Today page."""
        write_config(self.conn, custom_buckets=[
            "junk", {"name": "", "parent": "food"},
            {"name": "NoRulesNoMoney", "parent": "food", "monthly": 0,
             "categories": [], "merchants": []},     # nothing to show at all
            {"name": "food", "parent": "other", "monthly": 5,
             "categories": ["X"], "merchants": []},      # reserved
            {"name": "Bad Parent", "parent": "fixed", "monthly": 5,
             "categories": ["X"], "merchants": []},
            FUN, dict(FUN, name="fun")])                 # dup (case-insens.)
        st = self.status()
        self.assertEqual([c["name"] for c in st["buckets"]["other"]["children"]],
                         ["Fun"])
        self.assertEqual(st["buckets"]["food"]["children"], [])


class TestSuggest(CustomBucketBase):
    def _seed_history(self):
        # 6 trailing full months (Jan–Jun 2026) + current-month noise
        months = [dt.date(2026, m, 10) for m in range(1, 7)]
        fun = [100.0, 110.0, 120.0, 130.0, 140.0, 1000.0]   # 1000 = outlier
        for d, amt in zip(months, fun):
            add_txn(self.conn, d, amt, "CINEMA", primary="ENTERTAINMENT")
        for d in months:
            add_txn(self.conn, d, 50.0, "SAFEWAY", primary="FOOD_AND_DRINK")
            add_txn(self.conn, d, 77.0, "TARGET")
        add_txn(self.conn, TODAY, 9999.0, "CINEMA",
                primary="ENTERTAINMENT")                   # current month: out

    def test_median_outlier_and_current_month_rules(self):
        write_config(self.conn, custom_buckets=[FUN])
        self._seed_history()
        out = budget.suggest_budgets(self.conn, TODAY)
        # Fun: median of [100..140,1000] = 125 → outliers >250 drop 1000 →
        # median [100,110,120,130,140] = 120. Current-month CINEMA 9999 is
        # another outlier and is dropped the same way.
        self.assertEqual(out["suggestions"]["buckets"]["Fun"], 120.0)
        self.assertEqual(out["suggestions"]["food_monthly"], 50.0)
        self.assertEqual(out["suggestions"]["other_monthly"], 80.0)  # 77→$10
        # Window is 6 trailing full months + the in-progress month (MTD
        # food on the 20th must be visible — see suggest_budgets docstring).
        cur = f"{TODAY.year:04d}-{TODAY.month:02d}"
        self.assertIn(cur, out["months"])
        self.assertEqual(len(out["months"]), 7)

    def test_months_before_ledger_are_no_data_not_zero(self):
        """A ledger that starts in April must not drag the median down with
        phantom $0 months."""
        write_config(self.conn)
        for m in (4, 5, 6):
            add_txn(self.conn, dt.date(2026, m, 10), 100.0, "TARGET")
        out = budget.suggest_budgets(self.conn, TODAY)
        # Apr–Jun history + current partial month; no phantom Jan–Mar zeros.
        self.assertEqual(out["months"],
                         ["2026-04", "2026-05", "2026-06",
                          f"{TODAY.year:04d}-{TODAY.month:02d}"])
        self.assertEqual(out["suggestions"]["other_monthly"], 100.0)

    def test_bill_shaped_history_excluded(self):
        """Rows matching a tracked bill are fixed, not variable — same rule
        as the first-run seed."""
        write_config(self.conn)
        add_bill(self.conn, "Netflix", 20.0, next_due=TODAY.replace(day=10),
                 last_seen=TODAY.replace(day=10))
        for m in (4, 5, 6):
            add_txn(self.conn, dt.date(2026, m, 10), 20.0, "NETFLIX")
            add_txn(self.conn, dt.date(2026, m, 12), 100.0, "TARGET")
        out = budget.suggest_budgets(self.conn, TODAY)
        self.assertEqual(out["suggestions"]["other_monthly"], 100.0)


class TestSharedTemplate(CustomBucketBase):
    """The email and Today page render from the SAME template — one render
    proves both (the email inliner is the stricter surface)."""

    def test_child_bars_render_indented(self):
        # one budget language — every budget category is a
        # sibling row (Food, its carve-outs, the everything-else
        # categories), then Unallocated, then Bills
        write_config(self.conn, custom_buckets=[COFFEE, FUN])
        add_txn(self.conn, TODAY, 7.0, "BEAN HAUS", primary="FOOD_AND_DRINK")
        add_txn(self.conn, TODAY, 80.0, "CINEMA", primary="ENTERTAINMENT")
        d = report.gather(self.conn, TODAY)
        subject, plain, html = report.build(d)
        for label in ("Coffee", "Fun", "Food", "Everything else", "Bills"):
            self.assertIn(label, html)
        self.assertIn("unallocated — spending outside the categories", html)
        # ("not counted in the verdict" captions are retired — the pace
        # carets say which rows count)
        # category rows indent one level (money-map label indent), baked
        # to px (1.5rem → 24px), email-safe (no rem/var())
        self.assertIn("padding-left:24px", html)
        self.assertNotIn("var(--", html)
        # plain-text mirror carries the child lines too
        self.assertIn("Coffee", plain)
        self.assertIn("Fun", plain)

    def test_without_buckets_template_unchanged(self):
        write_config(self.conn)
        add_txn(self.conn, TODAY, 42.0, "SAFEWAY", primary="FOOD_AND_DRINK")
        _, _, html = report.build(report.gather(self.conn, TODAY))
        self.assertIn("Food", html)
        self.assertIn("Everything else", html)
        self.assertNotIn("after carve-outs", html)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        from fastapi.testclient import TestClient
        from .util import _ensure_db, seed_accounts
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.db import tenancy
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"buckets-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            add_txn(conn, dt.date.today(), 80.0, "CINEMA",
                    primary="ENTERTAINMENT")
        finally:
            conn.close()

    def test_settings_roundtrip(self):
        r = self.client.post("/api/settings", json={"custom_buckets": [
            {"name": " Fun ", "parent": "other", "monthly": "300",
             "categories": ["ENTERTAINMENT", " "], "merchants": ["AMC, "]}]})
        self.assertEqual(r.status_code, 200, r.text)
        got = self.client.get("/api/settings").json()["custom_buckets"]
        self.assertEqual(got, [{"name": "Fun", "parent": "other",
                                "monthly": 300.0,
                                "categories": ["ENTERTAINMENT"],
                                "merchants": ["AMC,"]}])
        # empty list clears the key
        r = self.client.post("/api/settings", json={"custom_buckets": []})
        self.assertIsNone(self.client.get("/api/settings")
                          .json()["custom_buckets"])

    def test_settings_validation(self):
        bad = [
            [{"name": "", "parent": "other", "categories": ["X"]}],
            [{"name": "food", "parent": "other", "categories": ["X"]}],
            [{"name": "A", "parent": "nope", "categories": ["X"]}],
            [{"name": "A", "parent": "other", "categories": ["X"]},
             {"name": "a", "parent": "food", "categories": ["X"]}],
            "not-a-list",
        ]
        for payload in bad:
            r = self.client.post("/api/settings",
                                 json={"custom_buckets": payload})
            self.assertEqual(r.status_code, 400, payload)
        # a matcher-less bucket is a valid plan placeholder (the
        # wizard's add-your-own row) — it claims nothing until configured
        r = self.client.post("/api/settings", json={"custom_buckets": [
            {"name": "Pets", "parent": "other", "monthly": 90,
             "categories": [], "merchants": []}]})
        self.assertEqual(r.status_code, 200, r.text)

    def test_settings_caps_the_list_and_the_matchers(self):
        """custom_buckets was the one settings list with no
        length cap — ~20k minimal buckets fit the 1 MB body and made every
        verdict computation quadratic. Hard 400s, never a silent slice."""
        many = [{"name": f"b{i}", "parent": "other", "categories": ["X"]}
                for i in range(41)]
        r = self.client.post("/api/settings", json={"custom_buckets": many})
        self.assertEqual(r.status_code, 400)
        self.assertIn("max 40", r.json()["detail"])
        fat_cats = [{"name": "A", "parent": "other",
                     "categories": [f"c{i}" for i in range(41)]}]
        r = self.client.post("/api/settings",
                             json={"custom_buckets": fat_cats})
        self.assertEqual(r.status_code, 400)
        long_matcher = [{"name": "A", "parent": "other",
                         "merchants": ["m" * 121]}]
        r = self.client.post("/api/settings",
                             json={"custom_buckets": long_matcher})
        self.assertEqual(r.status_code, 400)
        # the ceiling itself is usable: 40 buckets save fine
        ok = [{"name": f"b{i}", "parent": "other", "categories": ["X"]}
              for i in range(40)]
        r = self.client.post("/api/settings", json={"custom_buckets": ok})
        self.assertEqual(r.status_code, 200, r.text)
        # restore a small set so later tests in this class see sane config
        r = self.client.post("/api/settings", json={"custom_buckets": []})
        self.assertEqual(r.status_code, 200, r.text)

    def test_today_full_carries_children(self):
        self.client.post("/api/settings", json={"custom_buckets": [
            {"name": "Fun", "parent": "other", "monthly": 300,
             "categories": ["ENTERTAINMENT"], "merchants": []}]})
        d = self.client.get("/api/today/full").json()
        other = d["buckets"]["other"]
        self.assertEqual(other["children"],
                         [{"name": "Fun", "actual": 80.0,
                           "expected": other["children"][0]["expected"],
                           "month_budget": 300.0}])
        self.assertEqual(other["display_actual"], other["actual"] - 80.0)
        self.assertEqual(other["remaining_budget"], 700.0)
        self.assertNotIn("children", d["buckets"]["fixed"])
        # verdict inputs stay the raw two-bucket numbers
        self.assertAlmostEqual(d["variable_actual"],
                               d["buckets"]["food"]["actual"]
                               + other["actual"], places=2)

    def test_suggest_endpoint(self):
        r = self.client.get("/api/budgets/suggest")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("suggestions", body)
        self.assertIn("food_monthly", body["suggestions"])
        self.assertIn("other_monthly", body["suggestions"])
        self.assertIsInstance(body["suggestions"]["buckets"], dict)

    def test_suggest_requires_auth(self):
        from fastapi.testclient import TestClient
        from oikonome.web.app import app
        anon = TestClient(app)
        self.assertEqual(anon.get("/api/budgets/suggest").status_code, 401)


if __name__ == "__main__":
    unittest.main()
