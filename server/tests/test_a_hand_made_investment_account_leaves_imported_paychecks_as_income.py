"""A hand-made investment account does not turn imported checking into
brokerage cash.

A brokerage's cash account — a depository account in the same login as an
investment account — counts only the deposits stamped INCOME, because it
also receives the cash leg of every sale out of the portfolio beside it.
Hand-made and file-imported accounts carry no login at all: they all share
the one item 'manual'. Reading that item as a login made a hand-made IRA
beside an imported checking account turn the checking into brokerage cash,
so its uncategorized paychecks (a plain CSV or QIF import stamps nothing,
and a self-hosted instance with no model leaves them uncategorized) dropped
out of income everywhere, and the years they cover went from known to
unknown. Those paychecks are income on every surface, and their years are
measured.
"""

import datetime as dt
import unittest

from oikonome.engine import reporting
from oikonome.web import lenses

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 10, 9)
CHECK = 2500.0
CHK = "manual:northwind-checking"
IRA = "manual:contoso-roth-ira"


def _manual_account(conn, acct, typ, subtype):
    # the same shared item every hand-made and imported account lands in
    conn.execute(
        "INSERT INTO items (id, aggregator, institution_name, access_token)"
        " VALUES ('manual','manual','Manual',NULL)"
        " ON CONFLICT (tenant_id, id) DO NOTHING")
    conn.execute(
        "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
        " VALUES (%s,'manual',%s,%s,%s,1000)", (acct, acct, typ, subtype))


class HandMadeInvestmentBesideImportedChecking(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        _manual_account(self.conn, CHK, "depository", "checking")
        _manual_account(self.conn, IRA, "investment", "ira")
        for m in range(1, 13):
            add_txn(self.conn, dt.date(2022, m, 15), -CHECK,
                    "FABRIKAM PAYROLL", primary=None, account=CHK)
        self.conn.commit()

    def test_the_checking_is_a_bank_for_coverage(self):
        self.assertIn(
            CHK, reporting._bank_account_ids(self.conn, brokerage_cash=False))

    def test_the_paychecks_are_income_by_month_and_year(self):
        by_month = {r["p"]: float(r["amt"])
                    for r in reporting._income_by(self.conn, "month")}
        self.assertEqual(by_month.get("2022-03"), CHECK)
        by_year = {r["p"]: float(r["amt"])
                   for r in reporting._income_by(self.conn, "year")}
        self.assertEqual(by_year.get("2022"), 12 * CHECK)

    def test_their_years_are_known(self):
        self.assertEqual(reporting._income_coverage(self.conn),
                         ({"2022"}, "2022-01"))

    def test_the_year_lens_reads_them(self):
        y = lenses.year_summary(self.conn, 2022, today=TODAY)
        for c in y["cells"]:
            self.assertEqual(c["income"], CHECK, f"2022-{c['m']:02d} income")

    def test_the_income_window_counts_them(self):
        w = reporting.income_window(self.conn, TODAY, "all")
        self.assertEqual(w["total"], 12 * CHECK)
        self.assertEqual(w["from"], "2022-01-01")

    def test_a_real_brokerage_login_still_counts_only_stamped_income(self):
        # the rule the shared item was wrongly caught by still holds for a
        # login that truly holds both kinds of account
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name,"
            " access_token) VALUES ('itb','test','Contoso Invest','tok-b')")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('cash','itb','cash','depository',"
            "'cash management',0), ('brk','itb','brk','investment',"
            "'brokerage',0)")
        add_txn(self.conn, dt.date(2022, 3, 20), -4000.0, "SALE PROCEEDS",
                primary=None, account="cash")
        by_month = {r["p"]: float(r["amt"])
                    for r in reporting._income_by(self.conn, "month")}
        self.assertEqual(by_month.get("2022-03"), CHECK)


if __name__ == "__main__":
    unittest.main()
