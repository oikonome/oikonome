"""annual-pool envelopes.

An envelope's cap is a pool over a PERIOD. At `envelope_months=1` that period
is the month (the classic behavior, frozen by test_budget.py). At 12 it's the
calendar year — for lumpy annual costs like domain renewals, where a monthly
cap calls every renewal month overspending.

Fixture reality: TODAY is 2026-07-15, so Jan–Jun are prior months of the same
pool year.
"""

import datetime as dt
import unittest

from oikonome.engine import bills, budget
from oikonome.engine.compat import as_dict

from .util import TODAY, add_bill, add_txn, make_db, write_config

POOL = 120.0          # $120/yr — a domain-renewal-sized annual pool
SHARE = POOL / 12     # $10/mo in the plan


class AnnualEnvelopeBase(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Namehost", POOL, bill_type="envelope",
                 frequency=None, interval=12)

    def tearDown(self):
        self.conn.close()

    def status(self, **kw):
        return budget.month_status(self.conn, TODAY, **kw)

    def env(self, st):
        return st["buckets"]["fixed"]["envelopes"][0]


class TestStorage(AnnualEnvelopeBase):
    def test_pool_is_the_period_not_the_month(self):
        """`amount` is the ANNUAL pool; the plan carries its monthly share."""
        r = self.conn.execute(
            "SELECT amount, monthly_amount, frequency, raw FROM bills "
            "WHERE payee='Namehost'").fetchone()
        self.assertEqual(r["amount"], -POOL)
        self.assertAlmostEqual(r["monthly_amount"], -SHARE, places=2)
        self.assertEqual(r["frequency"], "ENVELOPE")
        self.assertEqual(as_dict(r["raw"])["envelope_months"], 12)

    def test_monthly_envelope_unchanged(self):
        """interval=1 is the old shape, byte for byte: pool == monthly."""
        add_bill(self.conn, "Bean Haus", 100.0, bill_type="envelope",
                 frequency=None, interval=1)
        r = self.conn.execute(
            "SELECT amount, monthly_amount, raw FROM bills "
            "WHERE payee='Bean Haus'").fetchone()
        self.assertEqual(r["amount"], -100.0)
        self.assertEqual(r["monthly_amount"], -100.0)
        self.assertEqual(as_dict(r["raw"])["envelope_months"], 1)


class TestAnnualCap(AnnualEnvelopeBase):
    def test_lumpy_renewal_does_not_overflow(self):
        """THE POINT: a $29.00 renewal is 3x the $10/mo share, but the cap is
        the YEAR's pool — so it's fixed spend, not a variable-verdict 'over'.
        A monthly envelope at the same $10 would leak $19.00 to variable."""
        add_txn(self.conn, TODAY, 29.00, "NAMEHOST", primary="GENERAL_SERVICES")
        st = self.status()
        e = self.env(st)
        self.assertAlmostEqual(e["used"], 29.00, places=2)
        self.assertEqual(e["overflow"], 0.0)
        self.assertAlmostEqual(st["buckets"]["fixed"]["actual"], 29.00, places=2)
        self.assertEqual(st["buckets"]["other"]["actual"], 0.0)

    def test_plan_carries_the_monthly_share(self):
        """The plan reserves pool/12 every month — that's what spreads an
        annual cost across the forecast and the surplus."""
        st = self.status()
        self.assertAlmostEqual(st["buckets"]["fixed"]["month_budget"],
                               SHARE, places=2)

    def test_earlier_months_draw_down_the_pool(self):
        """Charges earlier in the SAME calendar year are already spent."""
        add_txn(self.conn, dt.date(2026, 3, 18), 100.0, "NAMEHOST",
                primary="GENERAL_SERVICES")
        add_txn(self.conn, TODAY, 8.0, "NAMEHOST", primary="GENERAL_SERVICES")
        e = self.env(self.status())
        self.assertAlmostEqual(e["period_used"], 108.0, places=2)
        self.assertAlmostEqual(e["period_left"], 12.0, places=2)
        self.assertEqual(e["overflow"], 0.0)

    def test_overflow_once_the_year_is_spent(self):
        """$100 in March leaves $20; a $30 July charge spends $20 of pool and
        overflows $10 to variable — the cap still means something."""
        add_txn(self.conn, dt.date(2026, 3, 18), 100.0, "NAMEHOST",
                primary="GENERAL_SERVICES")
        add_txn(self.conn, TODAY, 30.0, "NAMEHOST", primary="GENERAL_SERVICES")
        st = self.status()
        e = self.env(st)
        self.assertAlmostEqual(e["used"], 20.0, places=2)
        self.assertAlmostEqual(e["overflow"], 10.0, places=2)
        self.assertEqual(e["period_left"], 0.0)
        self.assertAlmostEqual(st["buckets"]["other"]["actual"], 10.0, places=2)

    def test_last_years_charges_do_not_count(self):
        """The pool resets Jan 1 — December's renewal is a different year."""
        add_txn(self.conn, dt.date(2025, 12, 18), 115.0, "NAMEHOST",
                primary="GENERAL_SERVICES")
        add_txn(self.conn, TODAY, 29.00, "NAMEHOST", primary="GENERAL_SERVICES")
        e = self.env(self.status())
        self.assertAlmostEqual(e["period_used"], 29.00, places=2)
        self.assertEqual(e["overflow"], 0.0)

    def test_january_has_no_lookback(self):
        """First month of the pool year: nothing prior, full pool available."""
        add_txn(self.conn, dt.date(2026, 1, 20), 90.0, "NAMEHOST",
                primary="GENERAL_SERVICES")
        st = budget.month_status(self.conn, dt.date(2026, 1, 25))
        e = st["buckets"]["fixed"]["envelopes"][0]
        self.assertAlmostEqual(e["used"], 90.0, places=2)
        self.assertEqual(e["overflow"], 0.0)
        self.assertAlmostEqual(e["period_left"], 30.0, places=2)

    def test_conservation_with_overflow(self):
        """Every counted dollar lands in exactly one bucket."""
        add_txn(self.conn, dt.date(2026, 2, 10), 110.0, "NAMEHOST",
                primary="GENERAL_SERVICES")
        add_txn(self.conn, TODAY, 40.0, "NAMEHOST", primary="GENERAL_SERVICES")
        add_txn(self.conn, TODAY, 25.0, "TARGET")
        st = self.status()
        total = (st["buckets"]["fixed"]["actual"]
                 + st["buckets"]["food"]["actual"]
                 + st["buckets"]["other"]["actual"])
        self.assertAlmostEqual(total, 65.0, places=2)      # only July's rows
        e = self.env(st)
        self.assertAlmostEqual(e["used"], 10.0, places=2)   # $120 - $110 prior
        self.assertAlmostEqual(e["overflow"], 30.0, places=2)


class TestPriorUsedIsAnnualOnly(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_monthly_envelope_ignores_earlier_months(self):
        """A monthly envelope's period IS the month being matched — last
        month's spend must never eat this month's pool."""
        add_bill(self.conn, "Bean Haus", 100.0, bill_type="envelope",
                 frequency=None, interval=1)
        add_txn(self.conn, dt.date(2026, 6, 10), 100.0, "BEAN HAUS",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, TODAY, 40.0, "BEAN HAUS", primary="FOOD_AND_DRINK")
        st = budget.month_status(self.conn, TODAY)
        e = st["buckets"]["fixed"]["envelopes"][0]
        self.assertEqual(e["used"], 40.0)
        self.assertEqual(e["overflow"], 0.0)
        self.assertEqual(e["period_months"], 1)

    def test_prior_used_query_skipped_without_annual_envelopes(self):
        add_bill(self.conn, "Bean Haus", 100.0, bill_type="envelope",
                 frequency=None, interval=1)
        envs = budget._envelope_bills(self.conn)
        self.assertEqual(
            budget._envelope_prior_used(self.conn, envs, TODAY), {})


class TestPriorUsedSkipsBillOccurrences(unittest.TestCase):
    """The current month hands envelope matching only the rows no bill
    occurrence claimed. The year-to-date lookback must apply the same
    rule, or a category-matched annual envelope silently absorbs every
    earlier month's bill charges in that category and reports its pool
    spent by bills that were never its."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Services", 1200.0, bill_type="envelope",
                 frequency=None, interval=12, category="GENERAL_SERVICES",
                 match_category=True)
        add_bill(self.conn, "City Power", 100.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 8, 1), last_seen=dt.date(2026, 1, 1))
        for m in range(1, 7):
            add_txn(self.conn, dt.date(2026, m, 1), 100.0, "CITY POWER",
                    primary="GENERAL_SERVICES")
        add_txn(self.conn, dt.date(2026, 7, 1), 100.0, "CITY POWER",
                primary="GENERAL_SERVICES")
        add_txn(self.conn, TODAY, 50.0, "HANDYMAN", primary="GENERAL_SERVICES")

    def tearDown(self):
        self.conn.close()

    def test_bill_charges_matched_to_occurrences_do_not_draw_the_pool(self):
        st = budget.month_status(self.conn, TODAY)
        e = st["buckets"]["fixed"]["envelopes"][0]
        self.assertEqual(e["payee"], "Services")
        self.assertAlmostEqual(e["used"], 50.0, places=2)
        # January–June City Power was the bill's, not the envelope's
        self.assertAlmostEqual(e["period_used"], 50.0, places=2)
        self.assertAlmostEqual(e["period_left"], 1150.0, places=2)
        # and July's bill charge is still the bill's
        self.assertEqual(100.0, round(st["buckets"]["fixed"]["actual"] - 50.0, 2))


class TestCadenceSurface(AnnualEnvelopeBase):
    def test_cadence_round_trips_through_the_edit_form(self):
        from oikonome.web.api import CADENCES, _bill_cadence
        raw = as_dict(self.conn.execute(
            "SELECT raw FROM bills WHERE payee='Namehost'").fetchone()["raw"])
        self.assertEqual(_bill_cadence(raw), "ENVELOPE:12")
        self.assertIn("ENVELOPE:12", [v for v, _ in CADENCES])

    def test_label_distinguishes_the_two_pools(self):
        from oikonome.web.pages import _cadence_label
        self.assertEqual(_cadence_label("ENVELOPE", 12), "annual envelope")
        self.assertEqual(_cadence_label("ENVELOPE", 1), "envelope")
        self.assertEqual(_cadence_label("ENVELOPE", None), "envelope")


if __name__ == "__main__":
    unittest.main()


class AnnualEnvelopeNightlyPassTests(unittest.TestCase):
    """The nightly detect pass treats every envelope
    as a MONTHLY one, so an annual pool is wrong in both directions.

    The branch reads `bill_type == "envelope"` and never looks at
    `envelope_months`, so for a $120/YEAR pool:

      * drift: `new_amt` is the median of the trailing THREE monthly totals.
        A lumpy annual cost that happens to land twice in that window drifts
        the whole year pool down to one charge — $120/yr becomes ~$29.
      * removal: two consecutive $0 months proposes deleting the bill. Ten of
        twelve months are $0 for an annual cost by definition, so the bill is
        proposed for removal nearly every month of its life.
    """

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        # every pooled connection MUST go back: a leak here starves the
        # shared pool for the whole run (test_control_pool asks for
        # pool.max_size connections at once), and the cascade reads as
        # hundreds of unrelated errors.
        self.conn.close()

    def _run(self):
        return bills.run(self.conn, today=TODAY)

    def _bill(self):
        r = self.conn.execute(
            "SELECT payee, amount, raw FROM bills WHERE active=1").fetchone()
        return r

    def _proposals(self, kind=None):
        rows = self.conn.execute(
            "SELECT kind, payee, amount FROM bill_proposals "
            "WHERE status='pending'").fetchall()
        return [r for r in rows if kind is None or r["kind"] == kind]

    def test_annual_pool_is_not_drifted_to_a_single_charge(self):
        add_bill(self.conn, "Namehost", POOL, bill_type="envelope",
                 frequency=None, interval=12)
        self.conn.execute(
            "UPDATE bills SET raw = raw || %s::jsonb WHERE payee='Namehost'",
            ('{"envelope_months": 12}',))
        # two renewals inside the trailing 3-month window (lumpy, as annual
        # costs are) — enough for the 3-month median to be a real number
        add_txn(self.conn, TODAY - dt.timedelta(days=35), 29.00, "NAMEHOST")
        add_txn(self.conn, TODAY - dt.timedelta(days=65), 29.00, "NAMEHOST")
        self.conn.commit()
        self._run()
        amt = abs(self._bill()["amount"])
        self.assertEqual(
            amt, POOL,
            f"the $120/yr pool was auto-drifted to ${amt} — the 3-month "
            "median is a MONTHLY statistic and collapses an annual pool")

    def test_annual_envelope_is_not_proposed_for_removal_when_quiet(self):
        add_bill(self.conn, "Namehost", POOL, bill_type="envelope",
                 frequency=None, interval=12)
        self.conn.execute(
            "UPDATE bills SET raw = raw || %s::jsonb WHERE payee='Namehost'",
            ('{"envelope_months": 12}',))
        # last renewal 5 months ago: totally normal for a yearly cost
        add_txn(self.conn, TODAY - dt.timedelta(days=150), 120.0, "NAMEHOST")
        self.conn.commit()
        self._run()
        removes = self._proposals("remove")
        self.assertEqual(
            [], removes,
            "an annual envelope was proposed for removal after two quiet "
            "months — 10 of 12 months are quiet by definition")

    def test_monthly_envelope_behaviour_is_unchanged(self):
        """The guard must be scoped to annual pools only."""
        add_bill(self.conn, "Groceries", 400.0, bill_type="envelope",
                 frequency=None, interval=1)
        for d in (10, 40, 70):
            add_txn(self.conn, TODAY - dt.timedelta(days=d), 250.0, "GROCERIES")
        self.conn.commit()
        self._run()
        self.assertEqual(abs(self._bill()["amount"]), 250.0,
                         "monthly envelope should still track its median")
