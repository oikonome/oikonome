"""Retirement engine invariants (RMD conservation, historical machinery,
age math). Pure compute — no DB fixture needed except the buckets test."""

import datetime as dt
import unittest

from oikonome.engine import retirement as R

from .util import add_txn, make_db

BK = {"cash": 100_000.0, "taxable": 400_000.0, "td": 400_000.0,
      "roth": 300_000.0, "debt": 0.0, "total": 1_200_000.0}
KW = dict(rr=0.04, ss_annual=0.0, ss_age=67, rental_annual=0.0,
          td_annual=0.0, taxable_annual=0.0, resume_age=99,
          eff_ord=0.25, eff_cg=0.18, gain_frac=0.5)


class TestRetirement(unittest.TestCase):
    def test_age_from_birthdate(self):
        # A birthday just before and just after the as-of date, and the
        # as-of day itself, are the cases that matter.
        bd = "1970-01-15"
        self.assertEqual(R.age_from_birthdate(bd, dt.date(2026, 7, 6)), 56)
        self.assertEqual(R.age_from_birthdate(bd, dt.date(2026, 1, 15)), 56)
        self.assertEqual(R.age_from_birthdate(bd, dt.date(2026, 1, 14)), 55)

    def test_rmd_forces_distribution(self):
        """td-only portfolio, zero spend, zero growth: RMDs must move money
        td → taxable with exactly the ordinary-tax haircut, never lose it."""
        b = dict(BK, cash=0.0, taxable=0.0, roth=0.0, td=100_000.0)
        kw = dict(KW, rr=0.0)
        r = R.simulate(b, 74, 74, 76, 0.0, **kw)
        self.assertTrue(r["survived"])
        gross75 = 100_000 / R.RMD_DIVISOR[75]
        td76 = 100_000 - gross75
        gross76 = td76 / R.RMD_DIVISOR[76]
        expected = (td76 - gross76) + (gross75 + gross76) * 0.75
        self.assertAlmostEqual(r["end_balance"], expected, places=2)

    def test_rmd_is_forced_while_still_working(self):
        """An IRA's required distribution is set by age, not by whether the
        saver has retired: someone working past the start age still has the
        year's RMD moved td → taxable at the ordinary-tax haircut. Skipping
        it while working overstates tax-deferred growth and defers tax that
        is owed each year."""
        b = dict(BK, cash=0.0, taxable=0.0, roth=0.0, td=100_000.0)
        kw = dict(KW, rr=0.0)
        r = R.simulate(b, 75, 77, 77, 0.0, **kw)       # retires at 77
        gross75 = 100_000 / R.RMD_DIVISOR[75]
        td76 = 100_000 - gross75
        gross76 = td76 / R.RMD_DIVISOR[76]
        td77 = td76 - gross76
        gross77 = td77 / R.RMD_DIVISOR[77]
        expected = (td77 - gross77) + (gross75 + gross76 + gross77) * 0.75
        self.assertAlmostEqual(r["end_balance"], expected, places=2)
        # the growth curve applies the same distributions
        path = R._growth_path(b, 75, 77, 0.0, 0.0, 0.0, 99, eff_ord=0.25)
        self.assertAlmostEqual(path[2][1], td77 + (gross75 + gross76) * 0.75,
                               places=1)

    def test_rmd_start_age_follows_birth_year(self):
        """SECURE 2.0: 73 for those born 1951-1959, 75 from 1960; 72 before.
        A 1955-born saver's projection must start distributions at 73."""
        self.assertEqual(R.rmd_start_age(None), 75)
        self.assertEqual(R.rmd_start_age(1950), 72)
        self.assertEqual(R.rmd_start_age(1955), 73)
        self.assertEqual(R.rmd_start_age(1959), 73)
        self.assertEqual(R.rmd_start_age(1960), 75)
        b = dict(BK, cash=0.0, taxable=0.0, roth=0.0, td=100_000.0)
        kw = dict(KW, rr=0.0)
        late = R.simulate(b, 73, 73, 73, 0.0, **kw)
        self.assertAlmostEqual(late["end_balance"], 100_000.0, places=2)
        early = R.simulate(b, 73, 73, 73, 0.0, rmd_start=73, **kw)
        gross = 100_000 / R.RMD_DIVISOR[73]
        self.assertAlmostEqual(early["end_balance"],
                               100_000 - gross + gross * 0.75, places=2)

    def test_cash_loses_purchasing_power_at_its_real_return(self):
        """In a real-dollar model cash at 0% nominal decays by inflation:
        $100k over ten years of 3% is ~$74,409 of today's dollars. The
        default (cash keeps pace with inflation) leaves it at $100k — an
        explicit assumption, tested so it cannot drift."""
        b = dict(BK, taxable=0.0, td=0.0, roth=0.0, cash=100_000.0)
        kw = dict(KW, rr=0.0)
        kept = R.simulate(b, 60, 60, 69, 0.0, **kw)
        self.assertAlmostEqual(kept["end_balance"], 100_000.0, places=2)
        cash_rr = R.real_return(0.0, 0.03)
        decayed = R.simulate(b, 60, 60, 69, 0.0, cash_rr=cash_rr, **kw)
        self.assertAlmostEqual(decayed["end_balance"], 100_000 / 1.03 ** 10,
                               places=2)
        path = R._growth_path(b, 60, 70, 0.0, 0.0, 0.0, 99, cash_rr=cash_rr)
        self.assertAlmostEqual(path[10][1], round(100_000 / 1.03 ** 10, 2),
                               places=1)

    def test_growth_chart_and_retire_table_agree_on_each_age(self):
        """The working-years curve at age A must be the balance the table
        reports for retiring at A. Capturing at_retire after that year's
        growth would put the table one year of compounding ahead of the
        chart."""
        b = dict(BK, cash=0.0, taxable=10_000.0, td=0.0, roth=0.0)
        kw = dict(KW, rr=0.07, td_annual=1_000.0, taxable_annual=500.0,
                  resume_age=45)
        path = R._growth_path(b, 40, 80, 0.07, 1_000.0, 500.0, 45)
        for ra in (40, 45, 55, 70):
            sim = R.simulate(b, 40, ra, 95, 0.0, **kw)
            self.assertAlmostEqual(path[ra - 40][1], sim["at_retire"],
                                   places=1, msg=f"age {ra}")
        self.assertAlmostEqual(
            R.simulate(b, 40, 70, 95, 0.0, **dict(kw, td_annual=0.0,
                                                  taxable_annual=0.0))["at_retire"],
            10_000 * 1.07 ** 30, places=2)

    def test_hist_paths_shape(self):
        paths = R._hist_paths(50, 0.9)
        self.assertEqual(len(paths), 98)
        self.assertTrue(all(len(p) == 50 for p in paths))

    def test_spend_orderings(self):
        """spend100 <= spend90, and success% is monotone in spend."""
        s90 = R.hist_spend_at(BK, 60, 65, 90, 0.9, KW, 0.90)
        s100 = R.hist_spend_at(BK, 60, 65, 90, 0.9, KW, 1.00)
        self.assertLessEqual(s100, s90 + 1)
        lo = R.hist_success(BK, 60, 65, 90, s100 * 0.8, 0.9, KW)
        hi = R.hist_success(BK, 60, 65, 90, s90 * 1.5, 0.9, KW)
        self.assertGreaterEqual(lo["pct"], hi["pct"])

    def test_deterministic_sim_conservation_no_spend(self):
        r = R.simulate(BK, 60, 60, 70, 0.0, **dict(KW, rr=0.0))
        self.assertAlmostEqual(r["end_balance"], 1_200_000.0, places=1)


class TestCatchUpSaving(unittest.TestCase):
    """When no retirement age works, the page says what extra monthly
    saving would make one work. The figure must be one that really works,
    and the smallest round one that does."""
    SMALL = {"cash": 20_000.0, "taxable": 80_000.0, "td": 150_000.0,
             "roth": 50_000.0, "debt": 0.0, "total": 300_000.0}

    def _survives(self, monthly, retire_age=67):
        return R.simulate(self.SMALL, 45, retire_age, 95, 70_000.0,
                          extra_annual=monthly * 12.0, **KW)["survived"]

    def test_the_figure_works_and_one_step_less_does_not(self):
        m = R.catch_up_monthly(self.SMALL, 45, 67, 95, 70_000.0, KW)
        self.assertGreater(m, 0)
        self.assertEqual(m % R.CATCH_UP_STEP, 0)
        self.assertTrue(self._survives(m))
        self.assertFalse(self._survives(m - R.CATCH_UP_STEP))

    def test_more_working_years_need_less_a_month(self):
        self.assertLess(R.catch_up_monthly(self.SMALL, 45, 72, 95, 70_000.0, KW),
                        R.catch_up_monthly(self.SMALL, 45, 67, 95, 70_000.0, KW))

    def test_a_plan_that_already_works_needs_nothing(self):
        self.assertEqual(R.catch_up_monthly(BK, 45, 67, 95, 40_000.0, KW), 0)

    def test_no_working_years_left_or_an_absurd_target_is_no_answer(self):
        self.assertIsNone(R.catch_up_monthly(self.SMALL, 67, 67, 95, 70_000.0, KW))
        self.assertIsNone(R.catch_up_monthly(self.SMALL, 66, 67, 95, 5_000_000.0, KW))

    def test_the_extra_is_saved_from_now_not_from_the_resume_age(self):
        # KW resumes the plan's own saving at 99 — never — yet extra lands
        with_extra = R.simulate(self.SMALL, 45, 67, 95, 0.0,
                                extra_annual=12_000.0, **KW)["at_retire"]
        without = R.simulate(self.SMALL, 45, 67, 95, 0.0, **KW)["at_retire"]
        self.assertGreater(with_extra, without + 22 * 12_000.0)

    def test_no_extra_leaves_the_projection_exactly_as_it_was(self):
        self.assertEqual(R.simulate(BK, 45, 60, 95, 60_000.0, **KW),
                         R.simulate(BK, 45, 60, 95, 60_000.0, extra_annual=0.0, **KW))


class TestBuckets(unittest.TestCase):
    """Tax-treatment classification is subtype/name-driven — no institution
    hardcodes."""

    def test_subtype_classification(self):
        conn = make_db()
        try:
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
                " VALUES ('r1','it1','Roth IRA','investment','roth',300)")
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
                " VALUES ('t1','it1','Workplace 403b','investment','403b',400)")
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
                " VALUES ('i1','it1','Traditional IRA','investment','ira',200)")
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
                " VALUES ('c1','it1','BTC Wallet','investment','crypto',100)")
            b = R.current_buckets(conn)
            self.assertEqual(b["roth"], 300.0)
            self.assertEqual(b["td"], 600.0)     # 403b + traditional IRA
            # crypto → taxable; card debt (250) netted out of cash (5000)
            self.assertEqual(b["taxable"], 100.0)
            self.assertEqual(b["cash"], 4750.0)
        finally:
            conn.close()

    def test_generic_retirement_subtypes_are_pre_tax(self):
        """Aggregators that do not know the plan wrapper stamp "retirement";
        Plaid also ships "pension", "sep ira", "simple ira" and "thrift
        savings plan". All are pre-tax money: simulated as a brokerage they
        take a capital-gains haircut where ordinary income tax is due, so
        every withdrawal is grossed up too little and the earliest feasible
        retirement age comes back too early."""
        conn = make_db()
        try:
            for aid, name, sub in (
                    ("g1", "Fidelity 401(k)", "retirement"),
                    ("g2", "Company Pension", "pension"),
                    ("g3", "SEP", "sep ira"),
                    ("g4", "SIMPLE", "simple ira"),
                    ("g5", "TSP", "thrift savings plan"),
                    ("b1", "Vanguard Brokerage", "brokerage")):
                conn.execute(
                    "INSERT INTO accounts (id,item_id,name,type,subtype,"
                    "balance_current) VALUES (%s,'it1',%s,'investment',%s,100)",
                    (aid, name, sub))
            b = R.current_buckets(conn)
            self.assertEqual(b["td"], 500.0)
            self.assertEqual(b["taxable"], 100.0)
            self.assertEqual(b["roth"], 0.0)
        finally:
            conn.close()

    def test_health_savings_is_pre_tax_and_education_savings_is_not_spent(self):
        """An HSA is pre-tax money: as a brokerage its withdrawals would take
        a capital-gains haircut they never owe. A 529 pays for tuition, so
        the simulation must not spend it as part of the nest egg."""
        conn = make_db()
        try:
            for aid, name, sub, bal in (
                    ("h1", "Health Savings", "hsa", 400),
                    ("e1", "Education Savings", "529", 300),
                    ("b1", "Brokerage", "brokerage", 100)):
                conn.execute(
                    "INSERT INTO accounts (id,item_id,name,type,subtype,"
                    "balance_current) VALUES (%s,'it1',%s,'investment',%s,%s)",
                    (aid, name, sub, bal))
            b = R.current_buckets(conn)
            self.assertEqual(b["td"], 400.0)
            self.assertEqual(b["taxable"], 100.0)
        finally:
            conn.close()

    def test_default_spend_is_net_of_partial_reimbursements(self):
        """A $5,000 charge with $2,000 reimbursed is $3,000 the household
        actually lives on — the same net every other spend surface uses.
        Starting the page at the gross figure asks the plan to fund money
        the household never spent."""
        conn = make_db()
        try:
            add_txn(conn, dt.date.today() - dt.timedelta(days=30), 5000.0,
                    "CONFERENCE HOTEL", txn_id="exp1")
            add_txn(conn, dt.date.today() - dt.timedelta(days=20), -2000.0,
                    "EMPLOYER REIMB", account="chk", primary="INCOME",
                    txn_id="reimb1")
            conn.execute(
                "INSERT INTO reimbursements (expense_id, reimburse_id, "
                "partial, amount) VALUES ('exp1', 'reimb1', 1, 2000)")
            self.assertEqual(R.default_spend(conn, floor=0), 3000)
        finally:
            conn.close()

    def test_a_roth_employer_plan_is_roth_not_pre_tax(self):
        """Roth wins over the plan wrapper.

        Money in a Roth 401(k) comes out tax-free; simulated as pre-tax it
        takes an ordinary-income haircut, and simulated as a brokerage
        (where Plaid's "roth 401k" subtype landed, matching neither the
        pre-tax tuple nor "roth") a capital-gains one. Either understates
        every withdrawal from it, so max spend and the earliest feasible
        retirement age come back worse than the plan really is."""
        conn = make_db()
        try:
            for aid, name, sub in (
                    ("r401", "Fidelity NetBenefits", "roth 401k"),
                    ("r403", "Workplace Plan", "roth 403b"),
                    ("rname", "Employer Roth 401(k)", "401k")):
                conn.execute(
                    "INSERT INTO accounts (id,item_id,name,type,subtype,"
                    "balance_current) VALUES (%s,'it1',%s,'investment',%s,100)",
                    (aid, name, sub))
            b = R.current_buckets(conn)
            self.assertEqual(b["roth"], 300.0)
            self.assertEqual(b["td"], 0.0)
            self.assertEqual(b["taxable"], 0.0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
