"""A paycheck is income on every surface, whichever bank it lands in.

The ledger's income definition decided which accounts are banks by the
institution's NAME: a checking account at a bank whose name reads like a
brokerage was not a bank, so its paycheck was left out of the ledger
header's "in", Cash Flow and the lenses while the budget's month plan
counted it. Which accounts hold the household's bank deposits is decided
by the account TYPE:

- every depository account is a bank account, whatever its institution
  is called;
- a brokerage's cash account (depository, in a login that also holds an
  investment account) counts only the deposits stamped INCOME — the cash
  leg of a sale or a sweep out of the portfolio beside it is the
  household's own money moving, not money earned;
- a deposit the aggregator tagged as wages is a paycheck even when the
  employer's name is a provider's;
- a transfer back from a provider that the bank stamped INCOME is still
  not income.
"""

import datetime as dt
import unittest

from oikonome.engine import budget, reporting

from .util import TODAY, add_bill, add_txn, make_db, write_config

CHECK = 2500.0
# institution names and descriptors that carry each provider token the
# descriptor test knows, so every one of them is exercised; the rest of
# each name is invented
TOKENS = reporting._INV_PROVIDERS


def _item(conn, item, inst):
    conn.execute(
        "INSERT INTO items (id, aggregator, institution_name, access_token)"
        " VALUES (%s,'test',%s,%s)", (item, inst, f"tok-{item}"))


def _account(conn, acct, item, typ, subtype):
    conn.execute(
        "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
        " VALUES (%s,%s,%s,%s,%s,1000)", (acct, item, acct, typ, subtype))


class PaycheckAtABrokerageNamedBank(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn, budgeted_income_monthly=5000.0)
        # the household's other bank has seen the month, so its income is
        # known
        add_txn(self.conn, TODAY.replace(day=2), -10.0, "INTEREST PAYMENT",
                primary="INCOME", account="chk")

    def _in(self):
        return reporting.month_flows(self.conn, TODAY.year, TODAY.month)["in"]

    def test_a_paycheck_into_a_bank_named_like_a_brokerage_is_in_the_ledger(self):
        for i, tok in enumerate(TOKENS):
            _item(self.conn, f"itp{i}", f"Fabrikam {tok.title()} Bank")
            _account(self.conn, f"fab{i}", f"itp{i}", "depository", "checking")
            add_txn(self.conn, TODAY.replace(day=1), -CHECK,
                    "NORTHWIND PAYROLL", primary="INCOME", account=f"fab{i}")
        want = len(TOKENS) * CHECK + 10.0
        self.assertAlmostEqual(self._in(), want, places=2)
        self.assertAlmostEqual(
            reporting.month_income(self.conn, TODAY.year, TODAY.month),
            want, places=2)

    def test_the_ledger_header_and_the_month_plan_agree_on_it(self):
        _item(self.conn, "it2", f"Fabrikam {TOKENS[0].title()} Bank")
        _account(self.conn, "fab", "it2", "depository", "checking")
        add_txn(self.conn, TODAY.replace(day=1), -CHECK, "NORTHWIND PAYROLL",
                primary="INCOME", account="fab")
        add_bill(self.conn, "Northwind Payroll", CHECK, income=True,
                 next_due=dt.date(2026, 8, 1), last_seen=TODAY.replace(day=1),
                 frequency="MONTHLY", interval=1)
        st = budget.month_status(self.conn, TODAY)
        self.assertAlmostEqual(st["income_received"], self._in(), places=2)

    def test_a_brokerage_cash_account_counts_its_paycheck_not_its_sweeps(self):
        _item(self.conn, "it3", "Contoso Invest")
        _account(self.conn, "cash", "it3", "depository", "cash management")
        _account(self.conn, "brk", "it3", "investment", "brokerage")
        add_txn(self.conn, TODAY.replace(day=1), -CHECK, "NORTHWIND PAYROLL",
                primary="INCOME", account="cash")
        # the cash leg of a sale out of the portfolio beside it
        add_txn(self.conn, TODAY.replace(day=3), -4000.0, "SALE PROCEEDS",
                primary="GENERAL_SERVICES", account="cash")
        add_txn(self.conn, TODAY.replace(day=4), -700.0, "SWEEP FROM BROKERAGE",
                primary="TRANSFER_IN", account="cash")
        self.assertAlmostEqual(self._in(), CHECK + 10.0, places=2)

    def test_a_cash_account_without_a_paycheck_does_not_make_years_known(self):
        _item(self.conn, "it3", "Contoso Invest")
        _account(self.conn, "cash", "it3", "depository", "cash management")
        _account(self.conn, "brk", "it3", "investment", "brokerage")
        add_txn(self.conn, dt.date(2019, 3, 5), -400.0, "SWEEP",
                primary="GENERAL_SERVICES", account="cash")
        years, start = reporting._income_coverage(self.conn)
        self.assertNotIn("2019", years)
        self.assertEqual(start, f"{TODAY.year:04d}-{TODAY.month:02d}")

    def test_wages_from_an_employer_named_like_a_provider_are_income(self):
        for tok in TOKENS:
            add_txn(self.conn, TODAY.replace(day=1), -CHECK,
                    f"{tok.upper()} PAYROLL DIRECT DEP", primary="INCOME",
                    detailed="INCOME_WAGES", account="chk")
        self.assertAlmostEqual(self._in(), len(TOKENS) * CHECK + 10.0,
                               places=2)

    def test_a_transfer_back_from_a_provider_stamped_income_stays_out(self):
        add_txn(self.conn, TODAY.replace(day=1), -CHECK,
                f"{TOKENS[0].upper()} SELL TRANSFER", primary="INCOME",
                detailed="INCOME_OTHER_INCOME", account="chk")
        self.assertAlmostEqual(self._in(), 10.0, places=2)


if __name__ == "__main__":
    unittest.main()
