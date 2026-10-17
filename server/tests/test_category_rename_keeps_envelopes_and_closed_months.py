"""A category rename relabels everything that matches on the category name:
a category-pool envelope keeps claiming the renamed rows, and every closed
month keeps judging its custom buckets and envelopes against the same rows,
with its frozen amounts left exactly as they were."""
import datetime as dt
import unittest

from oikonome.engine import budget
from oikonome.web import data, lenses

from .util import TODAY, add_bill, add_txn, make_db, write_config


class CategoryRenameKeepsEnvelopesAndClosedMonths(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, custom_buckets=[{
            "name": "Kids Activities", "parent": "other", "monthly": 100,
            "categories": ["Kids Activities"], "merchants": []}])

    def tearDown(self):
        self.conn.close()

    def _envelope(self, st, payee):
        return {e["payee"]: e for e in
                st["buckets"]["fixed"]["envelopes"]}[payee]

    def test_category_envelope_keeps_claiming_its_rows(self):
        """An envelope that counts rows of its category must still claim
        them after the category is renamed, and show the new name."""
        add_bill(self.conn, "Kids Pool", 300.0, bill_type="envelope",
                 frequency=None, interval=1, category="Kids Activities",
                 match_category=True)
        add_txn(self.conn, TODAY, 45.0, "SWIM CLUB",
                override="Kids Activities")
        before = self._envelope(budget.month_status(self.conn, TODAY),
                                "Kids Pool")["used"]
        self.assertAlmostEqual(before, 45.0, places=2)

        data.rename_category(self.conn, "Kids Activities", "Kids")

        after = self._envelope(budget.month_status(self.conn, TODAY),
                               "Kids Pool")["used"]
        self.assertAlmostEqual(after, 45.0, places=2)
        self.assertEqual(self.conn.execute(
            "SELECT category FROM bills WHERE payee='Kids Pool'"
        ).fetchone()["category"], "Kids")

    def test_closed_month_bucket_and_envelope_keep_their_spend(self):
        """A frozen month is recomputed from the live rows, which now carry
        the new name: its bucket and category envelope must keep their
        spend, and its frozen caps must not change."""
        add_bill(self.conn, "Kids Pool", 300.0, bill_type="envelope",
                 frequency=None, interval=1, category="Kids Activities",
                 match_category=True)
        add_txn(self.conn, dt.date(2026, 6, 8), 40.0, "SWIM CLUB",
                override="Kids Activities")
        add_txn(self.conn, dt.date(2026, 6, 9), 25.0, "ART CAMP",
                override="Kids Activities")
        self.assertTrue(budget.snapshot_month(self.conn,
                                              dt.date(2026, 6, 15)))
        frozen_before = self.conn.execute(
            "SELECT config, bills FROM budget_snapshots "
            "WHERE year=2026 AND month=6").fetchone()
        r0 = lenses.month_summary(self.conn, 2026, 6, today=TODAY)
        env0 = {e["payee"]: e for e in r0["bills"]["envelopes"]}

        data.rename_category(self.conn, "Kids Activities", "Kids")

        r1 = lenses.month_summary(self.conn, 2026, 6, today=TODAY)
        self.assertEqual(r1["budget_source"], "snapshot")
        self.assertEqual(r1["bills_source"], "snapshot")
        kids0 = r0["buckets"]["other"]["children"]
        kids1 = r1["buckets"]["other"]["children"]
        self.assertEqual([c["actual"] for c in kids1],
                         [c["actual"] for c in kids0])
        self.assertEqual([c["name"] for c in kids1], ["Kids"])
        env1 = {e["payee"]: e for e in r1["bills"]["envelopes"]}
        self.assertAlmostEqual(env1["Kids Pool"]["used"],
                               env0["Kids Pool"]["used"], places=2)
        self.assertGreater(env1["Kids Pool"]["used"], 0)

        # a pure relabel: nothing but the name moved in the frozen month
        frozen_after = self.conn.execute(
            "SELECT config, bills FROM budget_snapshots "
            "WHERE year=2026 AND month=6").fetchone()
        b0 = frozen_before["config"]["custom_buckets"][0]
        b1 = frozen_after["config"]["custom_buckets"][0]
        self.assertEqual({**b0, "name": "Kids", "categories": ["Kids"]}, b1)
        self.assertEqual(
            {k: v for k, v in frozen_before["config"].items()
             if k != "custom_buckets"},
            {k: v for k, v in frozen_after["config"].items()
             if k != "custom_buckets"})
        rows0 = frozen_before["bills"]["rows"]
        rows1 = frozen_after["bills"]["rows"]
        self.assertEqual([{**r, "category": "Kids"} for r in rows0], rows1)
        self.assertEqual(
            frozen_before["bills"]["bills_monthly"],
            frozen_after["bills"]["bills_monthly"])

    def test_snapshot_without_a_frozen_schedule_keeps_it_absent(self):
        """A month frozen before bill rows were frozen keeps reading its
        schedule from the live table after a rename."""
        add_txn(self.conn, dt.date(2026, 6, 8), 40.0, "SWIM CLUB",
                override="Kids Activities")
        self.assertTrue(budget.snapshot_month(self.conn,
                                              dt.date(2026, 6, 15)))
        self.conn.execute("UPDATE budget_snapshots SET bills = NULL "
                          "WHERE year = 2026 AND month = 6")
        data.rename_category(self.conn, "Kids Activities", "Kids")
        row = self.conn.execute(
            "SELECT bills IS NULL AS absent, config FROM budget_snapshots "
            "WHERE year=2026 AND month=6").fetchone()
        self.assertTrue(row["absent"])
        self.assertEqual(row["config"]["custom_buckets"][0]["categories"],
                         ["Kids"])


if __name__ == "__main__":
    unittest.main()
