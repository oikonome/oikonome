"""Card debt larger than every bucket still counts in the retirement plan.

Card debt is netted out of the buckets in draw order, and whatever is left
once they are all empty is carried as `debt_residual`. A plan that reads
only the four buckets treats that residual as if it were never owed: the
growth chart starts at zero instead of below it, and every feasibility,
max-spend and catch-up answer is too kind by the debt and its growth. The
residual is a starting liability: savings pay it down before they build
anything, and a retiree still owing it has to pay it before spending.

The debt grows at a card's own rate, never the market's: a crash or a
negative historical year shrinks the portfolio, not what is owed, so a
stress scenario can never read as safer than average markets because of
the debt.
"""
import unittest

from oikonome.engine import retirement as R

KW = dict(rr=0.03, ss_annual=0.0, ss_age=67, rental_annual=0.0,
          td_annual=0.0, taxable_annual=6_000.0, resume_age=0,
          eff_ord=0.25, eff_cg=0.18, gain_frac=0.5)
EMPTY = {"cash": 0.0, "taxable": 0.0, "td": 0.0, "roth": 0.0}


class DebtResidualTests(unittest.TestCase):
    def test_savings_pay_the_debt_before_they_build_anything(self):
        owing = dict(EMPTY, debt_residual=15_000.0)
        clear = dict(EMPTY, debt_residual=0.0)
        a = R.simulate(owing, 30, 40, 90, 0.0, **KW)["at_retire"]
        b = R.simulate(clear, 30, 40, 90, 0.0, **KW)["at_retire"]
        # the debt and the growth it displaced: at least the debt itself
        self.assertLess(a, b - 15_000.0)

    def test_the_growth_path_starts_below_zero(self):
        owing = dict(EMPTY, debt_residual=15_000.0)
        path = R._growth_path(owing, 30, 35, 0.03, 0.0, 6_000.0, 0)
        self.assertEqual(path[0][1], -15_000.0)
        self.assertLess(path[1][1], 0.0)        # one year's saving short

    def test_a_retiree_still_owing_it_can_spend_less(self):
        kw = dict(KW, taxable_annual=0.0, ss_annual=20_000.0, ss_age=60)
        owing = dict(EMPTY, cash=100_000.0, debt_residual=50_000.0)
        clear = dict(EMPTY, cash=100_000.0, debt_residual=0.0)
        self.assertLess(R._max_spend(owing, 60, 60, 90, **kw),
                        R._max_spend(clear, 60, 60, 90, **kw))

    def test_no_residual_leaves_the_numbers_exactly_as_they_were(self):
        b = {"cash": 10_000.0, "taxable": 20_000.0, "td": 30_000.0,
             "roth": 5_000.0}
        self.assertEqual(R.simulate(b, 40, 60, 90, 30_000.0, **KW),
                         R.simulate(dict(b, debt_residual=0.0), 40, 60, 90,
                                    30_000.0, **KW))


class DebtGrowsAtItsOwnRateTests(unittest.TestCase):
    KW = dict(rr=R.real_return(0.08, 0.03), ss_annual=36_000.0, ss_age=67,
              rental_annual=0.0, td_annual=0.0, taxable_annual=0.0,
              resume_age=0, eff_ord=0.25, eff_cg=0.18, gain_frac=0.5)
    OWING = dict(EMPTY, debt_residual=12_000.0)

    def test_a_crash_never_makes_the_plan_look_safer(self):
        avg = R._max_spend(self.OWING, 67, 67, 95, **self.KW)
        for _label, shock in R.SCENARIOS:
            with self.subTest(scenario=_label):
                self.assertLessEqual(
                    R._max_spend(self.OWING, 67, 67, 95, shock=shock,
                                 **self.KW), avg)

    def test_a_down_market_year_does_not_shrink_the_debt(self):
        up = R.simulate(dict(EMPTY, cash=1.0, debt_residual=12_000.0),
                        40, 60, 41, 0.0, **dict(self.KW, rr=0.05))
        down = R.simulate(dict(EMPTY, cash=1.0, debt_residual=12_000.0),
                          40, 60, 41, 0.0, **dict(self.KW, rr=-0.35))
        self.assertEqual(up["end_balance"], down["end_balance"])
        self.assertLess(up["end_balance"], -12_000.0)

    def test_the_growth_path_grows_it_at_the_same_rate(self):
        path = R._growth_path(self.OWING, 40, 41, -0.35, 0.0, 0.0, 0)
        self.assertAlmostEqual(path[1][1], -12_000.0 * (1 + R.CARD_DEBT_REAL),
                               places=2)


if __name__ == "__main__":
    unittest.main()
