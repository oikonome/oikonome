"""The plan's income is printed in one set of whole dollars on every page.

The plan line (budget.plan_line) rounds its legs to the printed excess, so
its income leg can sit a dollar from the income rounded alone. Budget and
the Bills page print the plan line's leg; the Month lens's "expected from
paychecks $X" rounded the income on its own, and the two pages named two
incomes for one month. The Month lens now prints the plan line's leg.
Figures are invented.
"""

import unittest

from oikonome.engine import budget
from oikonome.web import lenses

from .util import TODAY, make_db, write_config


class ThePlansIncomeIsOneFigure(unittest.TestCase):

    def test_the_month_lens_prints_the_plan_lines_income(self):
        conn = make_db()
        self.addCleanup(conn.close)
        # cents whose legs, rounded to the printed excess, move the income
        write_config(conn, budgeted_income_monthly=5000.40,
                     food_monthly=1000.90, other_monthly=1000.00)
        st = budget.month_status(conn, TODAY)
        pl = st["plan_line"]
        self.assertIsNotNone(pl)
        # the leg the line prints is a dollar off the income rounded alone
        self.assertEqual(pl["income"], 5001)
        m = lenses.month_summary(conn, TODAY.year, TODAY.month, today=TODAY)
        text = m["income"]["plan_text"]
        self.assertIsNotNone(text)
        self.assertTrue(text.endswith(f"${pl['income']:,}"), (text, pl))

    def test_the_words_print_the_figure_they_are_given(self):
        say = lenses.income_plan_text
        self.assertEqual(say({"actual": None, "budgeted": 2000.4,
                              "source": "typed"}, shown=2001),
                         "budgeted $2,001")
        # the same printed dollars as what came in: nothing to add
        self.assertIsNone(say({"actual": 2001.2, "budgeted": 2000.4,
                               "source": "typed"}, shown=2001))


if __name__ == "__main__":
    unittest.main()
