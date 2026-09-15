"""Seams between rules that are each correct in their own file.

- the windows' `/mo` divisor is the calendar span of the RANGE — but never
  longer than the ledger itself: a household three weeks old on the
  default 1y range must not divide its spend by twelve;
- the flow picture applies excluded_accounts to money IN exactly as it does
  to money OUT, or "stayed" is the excluded account's whole spend;
- a restored bill's companions carry numbers the ledger and the daily
  report cast in SQL — free text there aborts both.
"""

import datetime as dt
import unittest

from oikonome.engine import reporting
from oikonome.sync import restore

from .test_reporting_windows_and_exclusions import _txn
from .util import make_db, seed_accounts, write_config


class ShortLedgerDivisorTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        seed_accounts(self.conn)
        self.today = dt.date(2026, 8, 31)

    def tearDown(self):
        self.conn.close()

    def test_a_young_ledger_divides_by_the_months_it_has(self):
        """Three weeks of data on the 1y range: $2,400 is $2,400/mo, not
        $200/mo — the span the label promises is capped at the ledger."""
        _txn(self.conn, "chk", dt.date(2026, 8, 12), 2400.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        w = reporting.spending_window(self.conn, self.today, "1y")
        self.assertEqual(w["total"], 2400.0)
        self.assertEqual(w["months"], 1)
        _txn(self.conn, "chk", dt.date(2026, 8, 15), -500.0, "ROYALTY",
             category_primary="INCOME",
             category_detailed="INCOME_OTHER_INCOME")
        self.assertEqual(reporting.income_window(
            self.conn, self.today, "1y")["months"], 1)

    def test_a_full_ledger_keeps_the_range_span(self):
        _txn(self.conn, "chk", dt.date(2025, 9, 3), 10.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        _txn(self.conn, "chk", dt.date(2026, 8, 3), 10.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        self.assertEqual(reporting.spending_window(
            self.conn, self.today, "1y")["months"], 12)


class FlowPictureIncomeExclusionTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        seed_accounts(self.conn)
        self.today = dt.date(2026, 8, 31)
        reporting._FIXED_MONTH_CACHE.clear()

    def tearDown(self):
        reporting._FIXED_MONTH_CACHE.clear()
        self.conn.close()

    def test_excluded_account_is_out_of_both_sides_of_stayed(self):
        """An excluded account that takes $6,000 in and pays $5,500 out
        must not read as "+$5,500 stayed": in and out are judged under
        the same exclusion, so the picture nets to the counted accounts."""
        # a second DEPOSITORY account (income only counts bank accounts)
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current,balance_available) VALUES "
            "('biz','it1','Biz Checking','depository','checking',900,900)")
        write_config(self.conn, excluded_accounts=["biz"])
        _txn(self.conn, "biz", dt.date(2026, 8, 1), -6000.0, "BIZ CLIENT",
             category_primary="INCOME",
             category_detailed="INCOME_OTHER_INCOME")
        _txn(self.conn, "biz", dt.date(2026, 8, 5), 5500.0, "BIZ SUPPLIER",
             category_primary="GENERAL_SERVICES")
        _txn(self.conn, "chk", dt.date(2026, 8, 2), -3000.0, "EMPLOYER PAYROLL",
             category_primary="INCOME", category_detailed="INCOME_WAGES")
        _txn(self.conn, "chk", dt.date(2026, 8, 12), 700.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        m = reporting.flow_breakdown(self.conn, self.today, "3m")
        self.assertEqual(m["out"]["total"], 700.0)
        self.assertEqual(m["in"]["total"], 3000.0)
        self.assertEqual(m["saved"], 2300.0)


class BillCompanionRawTests(unittest.TestCase):
    def test_companions_with_unreadable_amounts_are_dropped(self):
        raw = restore.clean_bill_raw({
            "companions": [{"payee": "fee", "amount": "N/A"},
                           {"payee": "tax", "amount": "1.50"},
                           {"payee": "tip", "amount": 2.0},
                           "junk", {"payee": "nan", "amount": "nan"}]})
        self.assertEqual(
            [c["payee"] for c in raw["companions"]], ["tax", "tip"])
        self.assertEqual(raw["companions"][0]["amount"], 1.5)


if __name__ == "__main__":
    unittest.main()
