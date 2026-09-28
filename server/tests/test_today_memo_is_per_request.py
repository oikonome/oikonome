"""The request-scoped memo the Today path threads through the engine passes
is created per call and dies with it: a second gather() sees what the ledger
holds NOW. And the shortcuts that make one request cheaper — bill matchers
taking the row's precomputed tokens, one bill's health computed alone, the
slim year-to-date scan — must give exactly the answers the full passes do.
"""

import datetime as dt
import unittest

from oikonome.engine import bills, budget

from .util import add_bill, add_txn, make_db, write_config

TODAY = dt.date.today()
MONTH_START = TODAY.replace(day=1)


class TodayMemoIsPerRequestTests(unittest.TestCase):
    def test_gather_reflects_a_row_added_between_two_calls(self):
        from oikonome.web import report
        conn = make_db()
        try:
            write_config(conn, food_monthly=600, other_monthly=900)
            first = report.gather(conn, TODAY)
            add_txn(conn, TODAY, 123.0, "NEW SPEND AFTER FIRST GATHER",
                    primary="GENERAL_MERCHANDISE")
            second = report.gather(conn, TODAY)
        finally:
            conn.close()
        self.assertAlmostEqual(
            second["variable_actual"] - first["variable_actual"], 123.0,
            places=2)

    def test_memo_answers_repeat_windows_from_one_scan(self):
        conn = make_db()
        try:
            add_txn(conn, MONTH_START, 40.0, "COFFEE")
            cfg = budget.load_config(conn)
            memo: dict = {}
            since = MONTH_START - dt.timedelta(days=budget.PREPAY_MATCH_DAYS)
            until = TODAY + dt.timedelta(days=1)
            a = budget._spend_rows(conn, since, until,
                                   excluded=cfg.get("excluded_accounts"),
                                   memo=memo)
            # the window is memoized; a row that lands after the first read
            # is invisible to THIS memo — and visible to a fresh one, which is
            # what every new request gets
            add_txn(conn, MONTH_START, 41.0, "TEA")
            b = budget._spend_rows(conn, since, until,
                                   excluded=cfg.get("excluded_accounts"),
                                   memo=memo)
            self.assertIs(a, b)
            fresh = budget._spend_rows(conn, since, until,
                                       excluded=cfg.get("excluded_accounts"),
                                       memo={})
            self.assertEqual(len(fresh), len(a) + 1)
            # a different window, or the slim projection, is its own entry
            slim = budget._spend_rows(conn, since, until,
                                      excluded=cfg.get("excluded_accounts"),
                                      slim=True, memo=memo)
            self.assertIsNot(slim, a)
            self.assertEqual({r["payee"] for r in slim} - {"TEA"},
                             {r["payee"] for r in a})
        finally:
            conn.close()

    def test_month_status_equal_with_and_without_a_memo(self):
        conn = make_db()
        try:
            write_config(conn, food_monthly=600, other_monthly=900)
            add_bill(conn, "Streamflix", 15.99, next_due=MONTH_START)
            add_txn(conn, MONTH_START, 15.99, "STREAMFLIX")
            add_txn(conn, MONTH_START, 60.0, "GROCER",
                    primary="FOOD_AND_DRINK")
            plain = budget.month_status(conn, TODAY)
            memo: dict = {}
            once = budget.month_status(conn, TODAY, memo=memo)
            again = budget.month_status(conn, TODAY, memo=memo)
        finally:
            conn.close()
        for key in ("verdict", "variance", "variable_actual",
                    "variable_expected", "buckets", "overdue_unpaid"):
            self.assertEqual(plain[key], once[key], key)
            self.assertEqual(plain[key], again[key], key)


class MatcherTokensAndSingleBillHealthTests(unittest.TestCase):
    def test_precomputed_tokens_give_the_same_verdict(self):
        cases = [("Harbor Brothers Coffee", None),
                 ("nova mobile", None),
                 ("pixelforge|pxf labs", None),
                 (None, "cedar ridge"),
                 (None, None)]
        texts = ["HARBOR BROTHERS COFFEE ROASTERS",
                 "NOVA MOBILE *AUTOPAY", "PLUS MOBILE", "T-Mobile",
                 "PXF LABS INC", "Pixelforge.ai subscription",
                 "CEDAR RIDGE LOAN SERVICES", "roaster refresh"]
        for merchant, key in cases:
            m = budget.merchant_matcher(merchant, key)
            for text in texts:
                self.assertEqual(m(text), m(text, budget._tokens(text)),
                                 (merchant, key, text))

    def test_one_bill_health_equals_its_entry_in_the_full_pass(self):
        conn = make_db()
        try:
            add_bill(conn, "Streamflix", 15.99, next_due=MONTH_START)
            add_bill(conn, "Acme Payroll", 2500, frequency="WEEKLY",
                     interval=2, income=True, next_due=MONTH_START)
            for k in range(1, 4):
                d = MONTH_START - dt.timedelta(days=30 * k)
                add_txn(conn, d, 15.99, "STREAMFLIX")
                add_txn(conn, d, -2500.0, "ACME PAYROLL DIRECT DEP",
                        account="chk", primary="INCOME")
            full = bills.analyze_bills(conn)
            one = bills.analyze_bills(conn, payee="Streamflix")
            pay = bills.analyze_bills(conn, payee="Acme Payroll")
        finally:
            conn.close()
        self.assertEqual(set(one), {"Streamflix"})
        self.assertEqual(one["Streamflix"], full["Streamflix"])
        self.assertEqual(set(pay), {"Acme Payroll"})
        self.assertEqual(pay["Acme Payroll"], full["Acme Payroll"])


if __name__ == "__main__":
    unittest.main()
