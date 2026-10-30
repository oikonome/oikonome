"""A settings amount in a restored archive is read the way the settings
door reads a typed one.

The door reads either separator convention ("1,234.56", "1.234,56",
"12,50"). Restore deleted every comma instead, so a decimal-comma budget
of "12,50" came back as 1250 — a plausible, silently wrong figure in every
total that reads it.
"""

import unittest

from oikonome.sync import restore


class RestoredSettingsAmounts(unittest.TestCase):
    def test_a_decimal_comma_amount_is_a_decimal(self):
        self.assertEqual(restore._amount_or_none("12,50"), 12.5)
        self.assertEqual(restore._amount_or_none("1.234,56"), 1234.56)

    def test_a_thousands_comma_and_a_currency_sign_still_read(self):
        self.assertEqual(restore._amount_or_none("$1,234.56"), 1234.56)
        self.assertEqual(restore._amount_or_none(" 800 "), 800.0)

    def test_what_is_not_an_amount_is_dropped(self):
        for v in ("", "  ", "1,2,3", "lots", "nan", "1e999", True, None):
            self.assertIsNone(restore._amount_or_none(v), v)

    def test_a_restored_budget_keeps_its_decimal_comma_value(self):
        cfg = {"food_monthly": "612,50", "other_monthly": 400}
        restore.clean_budget_amounts(cfg)
        self.assertEqual(cfg["food_monthly"], 612.5)
        self.assertEqual(cfg["other_monthly"], 400.0)


if __name__ == "__main__":
    unittest.main()
