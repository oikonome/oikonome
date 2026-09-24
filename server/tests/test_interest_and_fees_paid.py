"""Interest and fees paid: the cost of holding cash and carrying a balance,
by year, netted against what the bank refunded — and the one-day notice
when a new charge posts.

A fee reimbursed is not a fee paid, interest EARNED is income and never a
cost, an investment custodian's fee is fee drag (another report's subject),
and a card's first interest in months is told as "first since", because the
gap is what the person wants to know."""
import datetime as dt
import unittest

from oikonome.engine import anomalies, reporting, splits

from .util import TODAY, add_txn, make_db, write_config


def _fee(conn, date, amount, name, *, account="chk",
         detailed="BANK_FEES_OTHER_BANK_FEES", primary="BANK_FEES"):
    return add_txn(conn, date, amount, name, primary=primary,
                   detailed=detailed, account=account)


class InterestAndFeesPaidTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def report(self):
        return reporting.compute_cash_costs(self.conn, today=TODAY)

    def test_fees_net_of_refunds_by_year_with_the_biggest_named(self):
        _fee(self.conn, dt.date(2026, 3, 26), 20, "WIRE FEE")
        _fee(self.conn, dt.date(2026, 5, 20), -10, "ATM FEE REIMBURSEMENT",
             detailed="BANK_FEES_ATM_FEES")
        _fee(self.conn, dt.date(2025, 11, 14), 12.50, "FOREIGN TRANSACTION FEE",
             account="card", detailed="BANK_FEES_FOREIGN_TRANSACTION_FEES")
        r = self.report()
        self.assertEqual([b["year"] for b in r["by_year"]], ["2026", "2025"])
        y26, y25 = r["by_year"]
        self.assertEqual((y26["fees"], y26["refunds"], y26["net"]), (20, 10, 10))
        self.assertEqual(y26["biggest"][:2], ["WIRE FEE", 20])
        self.assertEqual((y25["fees"], y25["net"]), (12.50, 12.50))
        self.assertEqual(r["ytd"]["year"], "2026")
        # the latest CHARGE, not the latest row (the refund came later)
        self.assertEqual(r["latest"][0], "WIRE FEE")

    def test_interest_charged_is_its_own_column_and_never_the_earned_kind(self):
        _fee(self.conn, dt.date(2026, 2, 20), 30.00, "PURCHASE INTEREST CHARGE",
             account="card", detailed="BANK_FEES_INTEREST_CHARGE")
        # no tag at all — the statement's words are enough
        add_txn(self.conn, dt.date(2026, 4, 20), 12.10, "INTEREST CHARGE ON PURCHASES",
                primary=None, detailed=None, account="card")
        # interest EARNED on checking is income, whatever its words say
        add_txn(self.conn, dt.date(2026, 3, 20), -2.00, "INTEREST PAID",
                primary="INCOME", detailed="INCOME_INTEREST_EARNED", account="chk")
        r = self.report()
        y = r["by_year"][0]
        self.assertEqual((y["interest"], y["fees"]), (42.10, 0))
        self.assertEqual(r["interest_ever"], 42.10)
        self.assertEqual(r["earned_ytd"], 2.00)

    def test_an_investment_custodian_fee_and_an_excluded_account_stay_out(self):
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
            " VALUES ('ira','it1','Test IRA','investment','ira',10000)")
        add_txn(self.conn, dt.date(2026, 1, 7), 5.00, "CUSTODY FEE",
                primary="BANK_FEES", detailed="BANK_FEES_OTHER_BANK_FEES",
                account="ira")
        _fee(self.conn, dt.date(2026, 1, 8), 95, "ANNUAL MEMBERSHIP FEE",
             account="card")
        write_config(self.conn, excluded_accounts=["card"])
        self.assertEqual(reporting.compute_cash_costs(self.conn, today=TODAY)["by_year"], [])

    def test_a_split_counts_only_its_fee_part(self):
        """A split names what each part of a charge was, and every rollup by
        category reads the parts. A fee row split into fee and goods is
        only its fee part; a purchase with a fee part folded in counts
        that part. Reading the row's own category counted the goods as a
        fee and missed the fee inside the purchase."""
        fee = _fee(self.conn, dt.date(2026, 3, 26), 30, "WIRE FEE")
        splits.set_split(self.conn, fee, [
            {"category": "BANK_FEES", "amount": 5},
            {"category": "GENERAL_MERCHANDISE", "amount": 25}])
        buy = add_txn(self.conn, dt.date(2026, 4, 2), 40, "CONVENIENCE STORE",
                      primary="FOOD_AND_DRINK", account="chk")
        splits.set_split(self.conn, buy, [
            {"category": "FOOD_AND_DRINK", "amount": 32},
            {"category": "BANK_FEES", "amount": 8}])
        # a row the bank tagged as a fee but the person split into goods
        # alone is not a fee at all
        tagged = _fee(self.conn, dt.date(2026, 4, 3), 20, "SERVICE CHARGE")
        splits.set_split(self.conn, tagged, [
            {"category": "GENERAL_MERCHANDISE", "amount": 12},
            {"category": "FOOD_AND_DRINK", "amount": 8}])
        y = self.report()["by_year"][0]
        self.assertEqual((y["fees"], y["net"]), (13, 13))
        self.assertEqual(y["biggest"][:2], ["CONVENIENCE STORE", 8])
        rows = reporting.cash_cost_rows(self.conn)
        self.assertEqual(sorted(r["amount"] for r in rows), [5, 8])

    def test_nothing_paid_means_nothing_to_show(self):
        add_txn(self.conn, TODAY, 42, "GROCER")
        r = self.report()
        self.assertEqual(r["by_year"], [])
        self.assertIsNone(r["ytd"])
        self.assertIsNone(r["latest"])


class NewChargeNoticeTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def notices(self):
        return [m for m in anomalies.detect(self.conn, TODAY)
                if m.startswith(("Fee charged", "Interest charged"))]

    def test_a_fee_that_posted_since_yesterday_is_told_once_a_refund_never(self):
        _fee(self.conn, TODAY - dt.timedelta(days=1), 35, "OVERDRAFT FEE",
             detailed="BANK_FEES_OVERDRAFT_FEES")
        _fee(self.conn, TODAY, -3.50, "ATM FEE REIMBURSEMENT",
             detailed="BANK_FEES_ATM_FEES")
        _fee(self.conn, TODAY - dt.timedelta(days=9), 20, "WIRE FEE")
        self.assertEqual(self.notices(),
                         ["Fee charged: OVERDRAFT FEE $35 on "
                          f"{TODAY - dt.timedelta(days=1):%m/%d} (Test Checking)"])

    def test_interest_names_the_gap_since_the_card_last_charged_it(self):
        _fee(self.conn, dt.date(2026, 3, 20), 18, "PURCHASE INTEREST CHARGE",
             account="card", detailed="BANK_FEES_INTEREST_CHARGE")
        _fee(self.conn, TODAY, 25.00, "PURCHASE INTEREST CHARGE",
             account="card", detailed="BANK_FEES_INTEREST_CHARGE")
        self.assertEqual(self.notices(),
                         [f"Interest charged: Test Card $25 on {TODAY:%m/%d}"
                          " — first since Mar 2026"])

    def test_a_first_ever_interest_charge_says_so(self):
        _fee(self.conn, TODAY, 9.99, "INTEREST CHARGE ON PURCHASES",
             account="card", detailed="BANK_FEES_INTEREST_CHARGE")
        self.assertEqual(self.notices(),
                         [f"Interest charged: Test Card $9.99 on {TODAY:%m/%d}"
                          " — first ever on this account"])

    def test_pennies_are_not_a_notice(self):
        _fee(self.conn, TODAY, 0.52, "FOREIGN TRANSACTION FEE",
             account="card", detailed="BANK_FEES_FOREIGN_TRANSACTION_FEES")
        self.assertEqual(self.notices(), [])


if __name__ == "__main__":
    unittest.main()
