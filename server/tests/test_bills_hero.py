"""The Bills page states what the schedule costs.

Without a headline the monthly bill load is a sum of a dozen cadence groups
done by eye. The numbers deliberately come from `budget.recurring_load`,
which reuses the SAME evened-out year_total/12 the month plan uses, because
a Bills page saying one thing and a Budget page saying another about the
identical schedule makes both untrustworthy.
"""

import datetime as dt
import unittest

from oikonome.engine import budget

from .util import TODAY, _ensure_db, add_bill, make_db, write_config


class RecurringLoadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_monthly_bill_and_income_load(self):
        add_bill(self.conn, "Rent", 2000.0, next_due=TODAY.replace(day=1),
                 last_seen=TODAY.replace(day=1))
        # biweekly: 26 payments a year, so 26/12 a month — NOT 2, which is
        # what a naive "twice monthly" reading gives, and what would make the
        # hero disagree with the plan by ~$400/mo on a $5k paycheck
        add_bill(self.conn, "Paycheck Co", 5000.0, frequency="WEEKLY",
                 interval=2, next_due=TODAY + dt.timedelta(days=2),
                 income=True, last_seen=TODAY - dt.timedelta(days=12))
        t = budget.recurring_load(self.conn, TODAY)
        self.assertAlmostEqual(t["bills_monthly"], 2000.0, places=2)
        self.assertAlmostEqual(t["income_monthly"], 5000 * 26 / 12, places=0)

    def test_it_agrees_with_the_budget_plan_on_the_same_bills(self):
        """The whole reason for reusing the plan's helper."""
        add_bill(self.conn, "Rent", 2000.0, next_due=TODAY.replace(day=1),
                 last_seen=TODAY.replace(day=1))
        add_bill(self.conn, "Insurance", 600.0, frequency="YEARLY",
                 next_due=TODAY + dt.timedelta(days=40),
                 last_seen=TODAY - dt.timedelta(days=325))
        hero = budget.recurring_load(self.conn, TODAY)["bills_monthly"]
        plan = budget.month_status(self.conn, TODAY)["avg_bills"]
        self.assertAlmostEqual(hero, plan, places=2,
                               msg="the hero and the plan must not quote "
                                   "different monthly loads for one schedule")

    def test_the_week_ahead_counts_only_what_is_actually_due(self):
        add_bill(self.conn, "Soon", 40.0, next_due=TODAY + dt.timedelta(days=3),
                 last_seen=TODAY - dt.timedelta(days=28))
        add_bill(self.conn, "Later", 900.0,
                 next_due=TODAY + dt.timedelta(days=25),
                 last_seen=TODAY - dt.timedelta(days=6))
        t = budget.recurring_load(self.conn, TODAY)
        self.assertEqual(t["week_count"], 1)
        self.assertAlmostEqual(t["week_total"], 40.0, places=2)

    def test_a_month_boundary_inside_the_window_does_not_double_count(self):
        """The window is expanded from two months when it straddles one; the
        same occurrence appearing in both expansions must be counted once."""
        near = TODAY + dt.timedelta(days=3)
        add_bill(self.conn, "Straddle", 75.0, next_due=near,
                 last_seen=near - dt.timedelta(days=30))
        t = budget.recurring_load(self.conn, TODAY)
        self.assertEqual(t["week_count"], 1)
        self.assertAlmostEqual(t["week_total"], 75.0, places=2)


class BillsPageShapeTests(unittest.TestCase):
    """Structural guarantees, pinned in source: one table carries every
    cadence group, a healthy bill says nothing, and the row offers no
    disable button of its own (the ✎ expander carries that one)."""

    def setUp(self):
        import pathlib
        import oikonome
        p = (pathlib.Path(oikonome.__file__).parent.parent.parent
             / "webapp" / "src" / "pages" / "Bills.tsx")
        if not p.exists():
            self.skipTest("webapp/ not present")
        self.src = p.read_text()

    def test_one_table_renders_every_cadence_group(self):
        self.assertIn("<BillTable groups={groupsOf(ordered, false)}", self.src)
        self.assertIn('<tr className="grp">', self.src,
                      "group separators replace per-group cards")
        self.assertNotIn("<BillGroup", self.src,
                         "a card per cadence group repeats twelve theads")

    def test_a_healthy_bill_prints_no_status_pill(self):
        """hpill() renders a ✓ for health 'ok'; the row must only call it for
        the states that are actually wrong — and never for a flag the owner
        has dismissed."""
        self.assertIn('{!disabled && !b.health_dismissed\n'
                      '            && ["drifting", "stale", "mismatch", "misdated"]\n'
                      '              .includes(b.health ?? "") && (',
                      self.src)
        self.assertIn('{" "}{hpill(b.health, b.last_seen)}', self.src)

    def test_a_dismissed_flag_can_be_brought_back(self):
        """Dismissing a health flag hides a real problem; without a restore
        control on the same row it is a one-way door."""
        self.assertIn("{!disabled && b.health_dismissed && mayEdit && (",
                      self.src)
        # the row's ↺ asks for a restore of the health flag, and that request
        # reaches the hint endpoint (through the page's one hint mutation)
        self.assertIn('onHint(b.payee, "restore", "health")', self.src)
        self.assertRegex(self.src, r"api\.billsHint\(payee, action, target\)")

    def test_linklike_buttons_shed_the_base_button_chrome(self):
        """.linklike has no stylesheet rule — the border/background come off
        inline, or the chip renders as a boxed button."""
        self.assertIn('const LINKLIKE = { padding: 0, background: "none",',
                      self.src)
        self.assertNotIn('className="linklike" style={{ padding: 0 }}',
                         self.src)

    def test_the_attention_jump_list_skips_disabled_rows(self):
        """A disabled row shows neither pill nor dismiss control, so it must
        not be offered as somewhere to go and fix something."""
        self.assertIn("&& !b.health_dismissed && !b.disabled);", self.src)

    def test_the_row_offers_one_control_and_it_is_the_expander(self):
        self.assertIn('className="row-menu" aria-expanded={editing}', self.src)
        self.assertNotIn('{disabled ? "enable" : "disable"}</button>', self.src)

    def test_the_envelope_pool_is_a_bar_not_a_third_restatement(self):
        self.assertIn('<span className="envbar"', self.src)
        self.assertNotIn("of {money(b.amount)}</td>", self.src,
                         "the pool is already in the Amount column")

    def test_archived_is_not_named_after_a_sort_key(self):
        self.assertNotIn("zArchived", self.src)
