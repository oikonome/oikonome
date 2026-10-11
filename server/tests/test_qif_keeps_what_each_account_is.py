"""A Quicken file's accounts keep the type they declare, and investment
records are signed by what they did.

A whole-file Quicken export names each account's type in its `!Account`
block (Bank, CCard, Invst). Thrown away, every split account was typed by
its name alone, so 'Lakeside Rewards' became a checking account and lost the
card's sign and exclusion rules, and a brokerage became checking too.

Investment records write the amount unsigned and say the direction in the
action: a $10,000 Buy read as a bank amount landed as $10,000 coming IN, and
a reinvested dividend counted as a second inflow beside its dividend.
"""
import unittest

from oikonome.sync import qifimport

from .util import make_db, write_config

QIF = """!Option:AutoSwitch
!Account
NEveryday Checking
TBank
^
!Account
NLakeside Rewards
TCCard
^
!Account
NHarbor Brokerage
TInvst
^
!Clear:AutoSwitch
!Account
NEveryday Checking
TBank
^
!Type:Bank
D03/01/2026
T-2,000.00
PTransfer to brokerage
L[Harbor Brokerage]
^
D03/04/2026
T-61.20
PCORNER GROCER
^
!Account
NLakeside Rewards
TCCard
^
!Type:CCard
D03/05/2026
T-45.00
PRIVERSIDE CAFE
^
!Account
NHarbor Brokerage
TInvst
^
!Type:Invst
D03/01/2026
NXIn
T2,000.00
L[Everyday Checking]
^
D03/02/2026
NBuy
YHARBOR TOTAL MARKET
I80
Q25
T2,000.00
^
D03/20/2026
NDiv
YHARBOR TOTAL MARKET
T14.50
^
D03/20/2026
NReinvDiv
YHARBOR TOTAL MARKET
I80.50
Q0.18
T14.50
^
"""


class QifAccountTypesAndInvestmentSigns(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        self.out = qifimport.import_qif(self.conn, "chk", QIF)

    def _account(self, name):
        return self.conn.execute(
            "SELECT id, type, subtype FROM accounts WHERE name=%s",
            (name,)).fetchone()

    def _rows(self, name):
        aid = self._account(name)["id"]
        return [(r["date"].isoformat(), r["amount"], r["category_primary"])
                for r in self.conn.execute(
                    "SELECT date, amount, category_primary FROM transactions "
                    "WHERE account_id=%s ORDER BY date, amount", (aid,))]

    def test_each_account_takes_its_declared_type(self):
        self.assertEqual(self._account("Lakeside Rewards")["type"], "credit")
        self.assertEqual(self._account("Harbor Brokerage")["type"],
                         "investment")
        self.assertEqual(self._account("Everyday Checking")["type"],
                         "depository")

    def test_investment_records_are_signed_by_their_action(self):
        self.assertEqual(self._rows("Harbor Brokerage"), [
            ("2026-03-01", -2000.0, "TRANSFER_IN"),     # cash in from checking
            ("2026-03-02", 2000.0, "TRANSFER_OUT"),     # cash out into shares
            ("2026-03-20", -14.5, "INCOME"),            # the dividend, once
        ])

    def test_bank_and_card_records_keep_the_bank_sign(self):
        self.assertEqual(self._rows("Everyday Checking"), [
            ("2026-03-01", 2000.0, "TRANSFER_OUT"),
            ("2026-03-04", 61.2, None)])
        self.assertEqual([a for _, a, _ in self._rows("Lakeside Rewards")],
                         [45.0])


class QifReimportCorrectsTheOldInvestmentRows(unittest.TestCase):
    """A file imported before investment records were signed by their
    action left every record signed as written and named by payee alone
    ('?' here), a reinvested dividend included. Re-importing the same file
    corrects those rows in place: no second copy of each, and the
    share-only record that moved no cash is retired."""

    def setUp(self):
        import datetime as dt

        from oikonome.sync import base
        from oikonome.sync.base import FileImportIds
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        aid = base.ensure_split_account(self.conn, "qif", "Harbor Brokerage",
                                        type_="investment",
                                        subtype="brokerage")
        self.aid = aid
        old = FileImportIds("qif", aid)
        for day, t, action in (("2026-03-01", 2000.0, "XIn"),
                               ("2026-03-02", 2000.0, "Buy"),
                               ("2026-03-20", 14.5, "Div"),
                               ("2026-03-20", 14.5, "ReinvDiv")):
            d = dt.date.fromisoformat(day)
            tid = old.id_for(d, -t, "?")
            self.conn.execute(
                "INSERT INTO transactions (id, account_id, date, amount, name,"
                " pending, removed, raw) VALUES (%s,%s,%s,%s,'?',0,0,"
                " jsonb_build_object('section','Invst','action',%s::text))",
                (tid, aid, d, -t, action))

    def test_reimport_rewrites_the_old_rows_instead_of_adding_copies(self):
        qifimport.import_qif(self.conn, "chk", QIF)
        rows = [(r["date"].isoformat(), r["amount"], r["category_primary"])
                for r in self.conn.execute(
                    "SELECT date, amount, category_primary FROM transactions "
                    "WHERE account_id=%s AND removed=0 ORDER BY date, amount",
                    (self.aid,))]
        self.assertEqual(rows, [
            ("2026-03-01", -2000.0, "TRANSFER_IN"),
            ("2026-03-02", 2000.0, "TRANSFER_OUT"),
            ("2026-03-20", -14.5, "INCOME"),
        ])

    def test_a_second_reimport_changes_nothing(self):
        qifimport.import_qif(self.conn, "chk", QIF)
        n = self.conn.execute("SELECT count(*) AS n FROM transactions"
                              " WHERE account_id=%s", (self.aid,)
                              ).fetchone()["n"]
        qifimport.import_qif(self.conn, "chk", QIF)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM transactions WHERE account_id=%s",
            (self.aid,)).fetchone()["n"], n)


REINVEST_FIRST = """!Account
NHarbor Brokerage
TInvst
^
!Type:Invst
D03/20/2026
NReinvDiv
PHarbor 500
YHARBOR 500 INDEX
I80.50
Q0.62
T50.00
^
D03/20/2026
NDiv
PHarbor 500
YHARBOR 500 INDEX
T50.00
^
"""


class QifShareOnlyRecordNeverRetiresACashRow(unittest.TestCase):
    """A share-only record's old id is counted over every record, a cash
    record's new id over cash records only, so a reinvested dividend
    followed by its same-day dividend gives the dividend a new id equal to
    the reinvestment's old one. Retiring 'the reinvestment's old row' must
    not take the dividend just written under that id."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _income(self):
        return [(r["amount"], r["category_primary"]) for r in self.conn.execute(
            "SELECT amount, category_primary FROM transactions"
            " WHERE removed = 0")]

    def test_the_dividend_survives_a_fresh_import_and_a_reimport(self):
        qifimport.import_qif(self.conn, "chk", REINVEST_FIRST)
        self.assertEqual(self._income(), [(-50.0, "INCOME")])
        qifimport.import_qif(self.conn, "chk", REINVEST_FIRST)
        self.assertEqual(self._income(), [(-50.0, "INCOME")])


if __name__ == "__main__":
    unittest.main()
