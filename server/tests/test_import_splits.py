"""Multi-account file imports must split, with sane account types.

* Mint: Monarch/Copilot/Simplifi split per vendor account, and Mint's
transactions.csv carries the same "Account Name" column, so it splits too
rather than merging whole households into the one hub-selected account.
* A split account infers its type from the vendor-side account name,
conservatively; otherwise a card account created as depository breaks
spend math (credit sign/exclusion never applies).
* A multi-statement OFX file (several <STMTRS> blocks, e.g.
checking + savings from one bank) splits per statement, keyed by ACCTID
suffix, rather than landing entirely in the selected account.
"""

import unittest

from oikonome.sync import base, competitors, mintimport, ofximport

from .util import make_db, write_config

MINT_HEADER = ("Date,Description,Original Description,Amount,"
               "Transaction Type,Category,Account Name,Labels,Notes\n")

MINT_MULTI = (MINT_HEADER +
              "7/10/2026,Safeway,SAFEWAY STORE 123,42.50,debit,Groceries,"
              "Household Checking,,\n"
              "7/11/2026,Chevron,CHEVRON 42,30.00,debit,Gas & Fuel,"
              "Rewards Credit Card,,\n"
              "7/12/2026,Paycheck,EMPLOYER PAYROLL,2000.00,credit,Paycheck,"
              "Household Checking,,\n"
              "7/13/2026,Interest,INTEREST PAID,1.25,credit,Interest Income,"
              "Emergency Savings,,\n")

MINT_SINGLE = (MINT_HEADER +
               "7/10/2026,Safeway,SAFEWAY STORE 123,42.50,debit,Groceries,"
               "Household Checking,,\n"
               "7/12/2026,Paycheck,EMPLOYER PAYROLL,2000.00,credit,Paycheck,"
               "Household Checking,,\n")

# checking + savings statements from one bank, plus the same file's card
# statement — the classic "one login, whole relationship" OFX export
OFX_MULTI = b"""OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>
<DTSERVER>20260712120000<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1>
<STMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO</STATUS>
<STMTRS><CURDEF>USD
<BANKACCTFROM><BANKID>123456789<ACCTID>987654321<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST><DTSTART>20260701<DTEND>20260712
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260710<TRNAMT>-42.50<FITID>F001<NAME>SAFEWAY STORE</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260711<TRNAMT>2000.00<FITID>F002<NAME>EMPLOYER PAYROLL</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL><BALAMT>1500.00<DTASOF>20260712</LEDGERBAL>
</STMTRS></STMTTRNRS>
<STMTTRNRS><TRNUID>2<STATUS><CODE>0<SEVERITY>INFO</STATUS>
<STMTRS><CURDEF>USD
<BANKACCTFROM><BANKID>123456789<ACCTID>555000007777<ACCTTYPE>SAVINGS</BANKACCTFROM>
<BANKTRANLIST><DTSTART>20260701<DTEND>20260712
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260709<TRNAMT>1.25<FITID>S001<NAME>INTEREST PAID</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL><BALAMT>9000.00<DTASOF>20260712</LEDGERBAL>
</STMTRS></STMTTRNRS>
</BANKMSGSRSV1>
<CREDITCARDMSGSRSV1>
<CCSTMTTRNRS><TRNUID>3<STATUS><CODE>0<SEVERITY>INFO</STATUS>
<CCSTMTRS><CURDEF>USD
<CCACCTFROM><ACCTID>4111222233334444</CCACCTFROM>
<BANKTRANLIST><DTSTART>20260701<DTEND>20260712
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260710<TRNAMT>-30.00<FITID>C001<NAME>CHEVRON 42</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL><BALAMT>-250.00<DTASOF>20260712</LEDGERBAL>
</CCSTMTRS></CCSTMTTRNRS>
</CREDITCARDMSGSRSV1>
</OFX>
"""


class TypeInferenceTests(unittest.TestCase):
    def test_infer_account_type(self):
        cases = {
            "Rewards Credit Card": ("credit", "credit card"),
            "Chase Card": ("credit", "credit card"),
            "Emergency Savings": ("depository", "savings"),
            "Household Checking": ("depository", "checking"),
            # a credit-union CHECKING account is not a credit card
            "First Credit Union Checking": ("depository", "checking"),
            "Brokerage": ("depository", "checking"),
        }
        for name, want in cases.items():
            self.assertEqual(base.infer_account_type(name), want, name)


class MintSplitTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _by_account(self):
        return {r["account_id"]: r["n"] for r in self.conn.execute(
            "SELECT account_id, COUNT(*) AS n FROM transactions "
            "WHERE id LIKE 'mint:%' GROUP BY account_id").fetchall()}

    def _acct(self, aid):
        return self.conn.execute(
            "SELECT name, type, subtype FROM accounts WHERE id=%s",
            (aid,)).fetchone()

    def test_multi_account_mint_splits_per_account(self):
        out = mintimport.import_mint(self.conn, "chk", MINT_MULTI)
        self.assertEqual(out["imported"], 4)
        self.assertEqual(out["split_accounts"], {
            "Household Checking": 2, "Rewards Credit Card": 1,
            "Emergency Savings": 1})
        self.assertIn("3 accounts", out["note"])
        self.assertEqual(self._by_account(), {
            "manual:mint-household-checking": 2,
            "manual:mint-rewards-credit-card": 1,
            "manual:mint-emergency-savings": 1})

    def test_split_accounts_get_inferred_types(self):
        mintimport.import_mint(self.conn, "chk", MINT_MULTI)
        card = self._acct("manual:mint-rewards-credit-card")
        self.assertEqual((card["type"], card["subtype"]),
                         ("credit", "credit card"))
        sav = self._acct("manual:mint-emergency-savings")
        self.assertEqual((sav["type"], sav["subtype"]),
                         ("depository", "savings"))
        chk = self._acct("manual:mint-household-checking")
        self.assertEqual((chk["type"], chk["subtype"]),
                         ("depository", "checking"))
        self.assertEqual(card["name"], "Rewards Credit Card")

    def test_single_account_mint_uses_selected_account(self):
        out = mintimport.import_mint(self.conn, "chk", MINT_SINGLE)
        self.assertNotIn("split_accounts", out)
        self.assertEqual(self._by_account(), {"chk": 2})

    def test_mint_split_reimport_is_idempotent(self):
        mintimport.import_mint(self.conn, "chk", MINT_MULTI)
        mintimport.import_mint(self.conn, "chk", MINT_MULTI)
        self.assertEqual(self._by_account(), {
            "manual:mint-household-checking": 2,
            "manual:mint-rewards-credit-card": 1,
            "manual:mint-emergency-savings": 1})
        n = self.conn.execute("SELECT COUNT(*) AS n FROM accounts "
                              "WHERE id LIKE 'manual:mint%'").fetchone()
        self.assertEqual(n["n"], 3)

    def test_reimport_never_clobbers_user_rename_or_type(self):
        mintimport.import_mint(self.conn, "chk", MINT_MULTI)
        self.conn.execute(
            "UPDATE accounts SET name='My Card', type='depository', "
            "subtype=NULL, type_user_set=TRUE "
            "WHERE id='manual:mint-rewards-credit-card'")
        mintimport.import_mint(self.conn, "chk", MINT_MULTI)
        r = self._acct("manual:mint-rewards-credit-card")
        self.assertEqual((r["name"], r["type"]), ("My Card", "depository"))


class CompetitorSplitTypeTests(unittest.TestCase):
    """Type inference applies to the competitor split too."""

    MULTI = ("Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags\n"
             "2026-07-10,Safeway,Groceries,Checking,SAFEWAY STORE,,-42.50,\n"
             "2026-07-11,Chevron,Gas,Chase Card,CHEVRON 42,,-30.00,\n"
             "2026-07-13,Acme Bank,Interest,Savings,INTEREST PAID,,1.25,\n")

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_monarch_split_accounts_get_inferred_types(self):
        competitors.import_monarch(self.conn, "chk", self.MULTI)
        types = {r["id"]: (r["type"], r["subtype"]) for r in self.conn.execute(
            "SELECT id, type, subtype FROM accounts "
            "WHERE id LIKE 'manual:monarch%'").fetchall()}
        self.assertEqual(types["manual:monarch-chase-card"],
                         ("credit", "credit card"))
        self.assertEqual(types["manual:monarch-savings"],
                         ("depository", "savings"))
        self.assertEqual(types["manual:monarch-checking"],
                         ("depository", "checking"))


class OfxMultiStatementTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _by_account(self):
        return {r["account_id"]: r["n"] for r in self.conn.execute(
            "SELECT account_id, COUNT(*) AS n FROM transactions "
            "WHERE id LIKE 'ofx:%' GROUP BY account_id").fetchall()}

    def test_multi_statement_ofx_splits_per_statement(self):
        out = ofximport.import_ofx(self.conn, "chk", OFX_MULTI)
        self.assertEqual(out["imported"], 4)
        self.assertIn("3 statements", out["note"])
        self.assertEqual(self._by_account(), {
            "manual:ofx-checking-4321": 2,
            "manual:ofx-savings-7777": 1,
            "manual:ofx-card-4444": 1})
        types = {r["id"]: (r["type"], r["subtype"], r["name"])
                 for r in self.conn.execute(
                     "SELECT id, type, subtype, name FROM accounts "
                     "WHERE id LIKE 'manual:ofx%'").fetchall()}
        self.assertEqual(types["manual:ofx-card-4444"][:2],
                         ("credit", "credit card"))
        self.assertEqual(types["manual:ofx-savings-7777"][:2],
                         ("depository", "savings"))
        self.assertEqual(types["manual:ofx-checking-4321"][:2],
                         ("depository", "checking"))
        # names carry the type + ACCTID suffix so the user can tell them apart
        self.assertIn("4444", types["manual:ofx-card-4444"][2])

    def test_ofx_split_reimport_is_idempotent(self):
        ofximport.import_ofx(self.conn, "chk", OFX_MULTI)
        ofximport.import_ofx(self.conn, "chk", OFX_MULTI)
        self.assertEqual(self._by_account(), {
            "manual:ofx-checking-4321": 2,
            "manual:ofx-savings-7777": 1,
            "manual:ofx-card-4444": 1})
        n = self.conn.execute("SELECT COUNT(*) AS n FROM accounts "
                              "WHERE id LIKE 'manual:ofx%'").fetchone()
        self.assertEqual(n["n"], 3)

    def test_split_keeps_fitid_ids_stable_per_account(self):
        ofximport.import_ofx(self.conn, "chk", OFX_MULTI)
        ids = {r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions WHERE id LIKE 'ofx:%'").fetchall()}
        self.assertIn("ofx:manual:ofx-checking-4321:F001", ids)
        self.assertIn("ofx:manual:ofx-card-4444:C001", ids)

    def test_single_statement_ofx_uses_selected_account(self):
        from .test_ofximport import OFX as OFX_SINGLE
        out = ofximport.import_ofx(self.conn, "chk", OFX_SINGLE)
        self.assertNotIn("split_accounts", out)
        self.assertEqual(self._by_account(), {"chk": 3})


if __name__ == "__main__":
    unittest.main()
