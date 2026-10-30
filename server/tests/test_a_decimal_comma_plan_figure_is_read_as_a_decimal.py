"""A decimal-comma figure is read as a decimal, not as a bigger number.

Two doors read a typed or exported amount by deleting every comma:

- the debt planner's extra monthly payment, APR and minimum payment,
  which people type into a form — "12,50" was saved as an extra 1,250 a
  month, inside the range check, so nothing said so;
- a workplace plan's activity CSV, which may have passed through a
  spreadsheet in a decimal-comma locale — "1.234,56" was stored as
  1.23456 and a "12,5" share count as 125.

Both read either separator convention the way the CSV importer does. A
US-format figure ("1,234.56", "$40") reads exactly as before.
"""

import unittest

from oikonome.engine import debt
from oikonome.sync import plan_csv


class DebtPlanFigures(unittest.TestCase):
    def test_a_decimal_comma_extra_payment_is_a_decimal(self):
        self.assertEqual(debt.normalize_plan(
            {"extra_monthly": "12,50"})["extra_monthly"], 12.5)

    def test_a_dot_thousands_extra_payment_is_read_whole(self):
        self.assertEqual(debt.normalize_plan(
            {"extra_monthly": "1.234,56"})["extra_monthly"], 1234.56)

    def test_a_decimal_comma_apr_and_minimum_are_decimals(self):
        p = debt.normalize_plan({"debts": {"c1": {"apr": "19,99 %",
                                                  "min_payment": "40,50"}}})
        self.assertEqual(p["debts"]["c1"], {"apr": 19.99, "min_payment": 40.5})

    def test_us_format_figures_read_as_before(self):
        p = debt.normalize_plan({"extra_monthly": "$1,250.50",
                                 "debts": {"c1": {"apr": "19.99%",
                                                  "min_payment": "$1,040"}}})
        self.assertEqual(p["extra_monthly"], 1250.5)
        self.assertEqual(p["debts"]["c1"], {"apr": 19.99,
                                            "min_payment": 1040.0})

    def test_an_unreadable_figure_is_refused(self):
        for bad in ({"extra_monthly": "1,2,3"},
                    {"debts": {"c1": {"apr": "19,9,9"}}},
                    {"debts": {"c1": {"min_payment": "forty"}}}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    debt.normalize_plan(bad)


class PlanActivityFigures(unittest.TestCase):
    def _row(self, amount, price, shares):
        text = (f'07/01/2026,Northwind Index Fund,NWX,Contribution,'
                f'"{amount}","{price}","{shares}"\n')
        return plan_csv._parse(text)[0]

    def test_decimal_comma_cells_are_decimals(self):
        r = self._row("1.234,56", "12,50", "98,75")
        self.assertEqual(r["amount"], 1234.56)
        self.assertEqual(r["price"], 12.5)
        self.assertEqual(r["shares"], 98.75)

    def test_us_format_cells_read_as_before(self):
        r = self._row("1,234.56", "12.50", "98.7654")
        self.assertEqual(r["amount"], 1234.56)
        self.assertEqual(r["price"], 12.5)
        self.assertEqual(r["shares"], 98.7654)

    def test_a_currency_sign_or_accounting_negative_is_read(self):
        r = self._row("$1,234.56", "(12.50)", "")
        self.assertEqual(r["amount"], 1234.56)
        self.assertEqual(r["price"], -12.5)
        self.assertIsNone(r["shares"])

    def test_a_cell_that_is_no_number_is_empty(self):
        r = self._row("n/a", "1,2,3", "")
        self.assertIsNone(r["amount"])
        self.assertIsNone(r["price"])


if __name__ == "__main__":
    unittest.main()
