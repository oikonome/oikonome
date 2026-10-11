"""On an investment statement, money moving between the household's own
holdings and accounts — buys, purchases, sales, exchanges, reinvestments,
distributions, transfers — is a transfer, never spending; fees stay
spending and dividend/interest lines keep the category they always had.
Without the transfer hint a fund purchase lands in the spending totals of
every lens and report email.

The same account's activity CSV is read by the same rule: imported both
ways, one purchase must not be a transfer from the PDF and spending from
the CSV."""

import unittest

from oikonome.sync import csvimport, flowmap, pdfimport

from .util import make_db, write_config

PERIOD = "Statement period 01/01/2026 - 01/31/2026\n"


def _rows(lines: str, hints=None):
    text = PERIOD + lines
    if hints is not None:
        hints = [None] + hints
    r = pdfimport.parse_statement(text, kind="investment", hints=hints)
    return {row["name"]: row for row in r["rows"]}


class InvestmentMovementsAreTransfersTests(unittest.TestCase):

    def test_purchases_are_transfers_with_their_sign_unchanged(self):
        rows = _rows(
            "01/15 Purchase Vanguard 500 Index Admiral 2,500.00 10,000.00\n"
            "01/21 Shares purchased VTSAX 500.00 8,500.00\n"
            "01/22 Exchange to Bond Index Fund 300.00 8,200.00\n"
            "01/23 Redemption Money Market 200.00 8,000.00\n")
        for name, amount in (
                ("Purchase Vanguard 500 Index Admiral", 2500.00),
                ("Shares purchased VTSAX", 500.00)):
            self.assertEqual(rows[name]["amount"], amount)
            self.assertEqual(rows[name]["flow"], "transfer", name)
            primary, _ = flowmap.flow_category(rows[name]["flow"], amount)
            self.assertEqual(primary, "TRANSFER_OUT")
        for name in ("Exchange to Bond Index Fund", "Redemption Money Market"):
            self.assertEqual(rows[name]["flow"], "transfer", name)

    def test_a_distribution_under_a_column_is_a_transfer(self):
        rows = _rows("01/20 IRA Distribution to checking 1,000.00\n",
                     hints=["out"])
        row = rows["IRA Distribution to checking"]
        self.assertEqual(row["amount"], 1000.00)        # the column's sign
        self.assertEqual(row["flow"], "transfer")
        # and with no column, by keyword
        rows = _rows("01/20 IRA Distribution to checking 1,000.00\n")
        self.assertEqual(rows["IRA Distribution to checking"]["flow"],
                         "transfer")

    def test_fees_stay_spending_and_income_keeps_its_category(self):
        rows = _rows(
            "01/05 Advisory fee 25.00\n"
            "01/06 Commission on purchase 4.95\n"
            "01/10 Dividend received VTSAX 12.34\n"
            "01/11 Interest earned 1.02\n"
            "01/12 Capital gain distribution 8.00\n")
        for name in ("Advisory fee", "Commission on purchase"):
            self.assertIsNone(rows[name]["flow"], name)
            self.assertGreater(rows[name]["amount"], 0)
        for name in ("Dividend received VTSAX", "Interest earned",
                     "Capital gain distribution"):
            self.assertIsNone(rows[name]["flow"], name)
            self.assertLess(rows[name]["amount"], 0)

    def test_a_bank_statement_purchase_is_still_spending(self):
        r = pdfimport.parse_statement(
            PERIOD + "01/15 POS PURCHASE GROCERY MART 42.50\n", kind="bank")
        self.assertIsNone(r["rows"][0]["flow"])


# A brokerage cash-management account carries card spending and paychecks
# beside its trades. Read as transfers, a month of card purchases drops out
# of every spending total and a paycheck stops counting as income.
CASH_ACCOUNT_LINES = (
    "01/07 DEBIT CARD PURCHASE EXAMPLE CAFE #12 5.75\n"
    "01/08 POS PURCHASE GROCERY MART 42.50\n"
    "01/09 POS SALE CORNER HARDWARE 18.20\n"
    "01/10 ATM WITHDRAWAL MAIN ST 100.00\n"
    "01/11 BILL PAYMENT CITY WATER 61.00\n"
    "01/12 PURCHASE GROCERY MART 12.00\n"
    "01/13 BEST BUY 199.99\n")
CASH_ACCOUNT_SPENDING = (
    "DEBIT CARD PURCHASE EXAMPLE CAFE #12", "POS PURCHASE GROCERY MART",
    "POS SALE CORNER HARDWARE", "ATM WITHDRAWAL MAIN ST",
    "BILL PAYMENT CITY WATER", "PURCHASE GROCERY MART", "BEST BUY")
PAYCHECK = "DIRECT DEPOSIT PAYROLL EXAMPLE CO"


class CashManagementSpendingStaysSpendingTests(unittest.TestCase):

    def test_card_and_register_lines_on_an_investment_pdf_stay_spending(self):
        rows = _rows(CASH_ACCOUNT_LINES)
        for name in CASH_ACCOUNT_SPENDING:
            self.assertIsNone(rows[name]["flow"], name)
            self.assertGreater(rows[name]["amount"], 0, name)
            self.assertEqual(flowmap.flow_category(
                rows[name]["flow"], rows[name]["amount"]), (None, None))

    def test_a_paycheck_on_an_investment_pdf_stays_income(self):
        rows = _rows(f"01/15 {PAYCHECK} 2,000.00\n")
        self.assertIsNone(rows[PAYCHECK]["flow"])
        self.assertLess(rows[PAYCHECK]["amount"], 0)

    def test_a_trade_is_still_a_transfer_beside_them(self):
        for desc in ("Purchase Vanguard 500 Index Admiral",
                     "Shares purchased VTSAX", "Sale of VTSAX",
                     "YOU BOUGHT 10 SHARES", "Purchase 10 @ 150.00",
                     "Contribution", "Transfer out to linked bank",
                     "Deposit from linked bank"):
            self.assertEqual(flowmap.investment_flow(desc), "transfer", desc)


class InvestmentCsvMatchesThePdfTests(unittest.TestCase):

    CSV = ("Date,Description,Amount,Category\n"
           "2026-01-15,PURCHASE VANGUARD 500,-2500.00,\n"
           "2026-01-16,Sale of VTSAX,800.00,Investments\n"
           "2026-01-17,Advisory fee,-25.00,\n"
           "2026-01-18,Dividend received VTSAX,12.34,Dividend\n")
    MAPPING = {"date": "Date", "amount": "Amount", "name": "Description",
               "category": "Category"}

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('brk','it1','Brokerage',"
            "'investment','brokerage',10000)")

    def _cats(self, amount_sign):
        csvimport.import_csv(self.conn, "brk", self.CSV, self.MAPPING,
                             amount_sign=amount_sign)
        return {r["name"]: (r["category_primary"], r["amount"])
                for r in self.conn.execute(
                    "SELECT name, category_primary, amount FROM transactions "
                    "WHERE account_id = 'brk'").fetchall()}

    def test_a_csv_purchase_is_a_transfer_like_the_pdf(self):
        rows = self._cats("investment")
        self.assertEqual(rows["PURCHASE VANGUARD 500"],
                         ("TRANSFER_OUT", 2500.00))
        self.assertEqual(rows["Sale of VTSAX"], ("TRANSFER_IN", -800.00))
        # a fee stays spending, a dividend keeps the file's own category
        self.assertIsNone(rows["Advisory fee"][0])
        self.assertEqual(rows["Dividend received VTSAX"][0], "Dividend")
        # and the PDF reads the same purchase the same way
        pdf = _rows("01/15 PURCHASE VANGUARD 500 2,500.00 7,500.00\n")
        self.assertEqual(flowmap.flow_category(
            pdf["PURCHASE VANGUARD 500"]["flow"], 2500.00)[0], "TRANSFER_OUT")

    def test_a_bank_csv_purchase_is_still_spending(self):
        rows = self._cats("bank")
        self.assertIsNone(rows["PURCHASE VANGUARD 500"][0])

    def test_cash_account_spending_and_pay_in_an_investment_csv(self):
        """The CSV of a cash-management account reads card spending and
        paychecks the way its PDF does: spending stays spending (its own
        category or none), a paycheck keeps the file's category."""
        csv = "Date,Description,Amount,Category\n" + "".join(
            f"2026-01-{7 + i:02d},{name},-{10 + i}.00,\n"
            for i, name in enumerate(CASH_ACCOUNT_SPENDING)) + (
            f"2026-01-20,{PAYCHECK},2000.00,Paycheck\n")
        csvimport.import_csv(self.conn, "brk", csv, self.MAPPING,
                             amount_sign="investment")
        rows = {r["name"]: (r["category_primary"], r["amount"])
                for r in self.conn.execute(
                    "SELECT name, category_primary, amount FROM transactions "
                    "WHERE account_id = 'brk'").fetchall()}
        for name in CASH_ACCOUNT_SPENDING:
            self.assertIsNone(rows[name][0], name)
            self.assertGreater(rows[name][1], 0, name)
        self.assertEqual(rows[PAYCHECK], ("Paycheck", -2000.00))


class StockTradesAndShopNamesTests(unittest.TestCase):
    """A stock trade names shares, a quantity, a price, a share class or
    a ticker beside the company; read as spending, a $15,000 share purchase
    lands in every spending total. A company's legal suffix alone is not
    enough: a brokerage cash account prints a card swipe as "PURCHASE
    TARGET CORP", and a swipe read as a trade vanishes from spending. A
    shop whose name holds "buy" or "index" is still spending, however the
    PDF spaced it."""

    def test_a_stock_purchase_or_sale_is_a_transfer(self):
        for desc in ("PURCHASE 10 SH AAPL", "Purchase Alphabet Class A",
                     "PURCHASE APPLE (AAPL)", "SALE 50 SHS MICROSOFT CORP",
                     "PURCHASE 12.5 SHARES EXAMPLE CO"):
            self.assertEqual(flowmap.investment_flow(desc), "transfer", desc)

    def test_a_company_suffix_alone_is_not_a_trade(self):
        for desc in ("PURCHASE APPLE INC", "SALE MICROSOFT CORP",
                     "Sale Example Holdings PLC", "PURCHASE EXAMPLE CO",
                     "PURCHASE TARGET CORP", "PURCHASE HOME DEPOT INC"):
            self.assertIsNone(flowmap.investment_flow(desc), desc)

    def test_a_stock_sale_on_the_statement_is_money_in(self):
        rows = _rows("01/15 PURCHASE 100 SH APPLE INC 15,000.00\n"
                     "01/16 SALE 50 SHS MICROSOFT CORP 9,000.00\n"
                     "01/17 POS SALE CORNER HARDWARE 18.20\n")
        self.assertEqual(rows["PURCHASE 100 SH APPLE INC"]["amount"], 15000.00)
        self.assertEqual(rows["PURCHASE 100 SH APPLE INC"]["flow"], "transfer")
        self.assertEqual(rows["SALE 50 SHS MICROSOFT CORP"]["amount"],
                         -9000.00)
        self.assertEqual(rows["SALE 50 SHS MICROSOFT CORP"]["flow"],
                         "transfer")
        # a register "sale" is still money out, and spending
        self.assertEqual(rows["POS SALE CORNER HARDWARE"]["amount"], 18.20)
        self.assertIsNone(rows["POS SALE CORNER HARDWARE"]["flow"])

    def test_index_alone_is_not_a_security(self):
        self.assertIsNone(flowmap.investment_flow(
            "PURCHASE INDEX CARDS OFFICE DEPOT"))
        self.assertEqual(flowmap.investment_flow(
            "Purchase Vanguard 500 Index Admiral"), "transfer")

    def test_a_web_address_is_not_a_company_suffix(self):
        self.assertIsNone(flowmap.investment_flow("PURCHASE AMAZON.COM"))

    def test_shops_named_buy_stay_spending_however_spaced(self):
        for desc in ("BEST BUY 00123", "BEST  BUY 00123", "Best\tBuy",
                     "Buy Buy Baby", "BUY  BUY BABY #44"):
            self.assertIsNone(flowmap.investment_flow(desc), desc)
        self.assertEqual(flowmap.investment_flow("BUY 10 SH AAPL"),
                         "transfer")


class CashAccountShoppingIsNotATradeTests(unittest.TestCase):
    """A brokerage cash account's card and register print shopping as
    "PURCHASE <shop>" and "SALE <shop>". A short capitalised shop name
    looks like a ticker and a shop is a company too, so neither may make
    the line a trade: read as one, the purchase leaves spending as a
    transfer and the sale is booked as money IN — spending turned into
    income. Only words a trade alone prints let an unsigned SALE be money
    in: a fund, a share count, a price per share, a fund family, a CUSIP,
    a ticker in parentheses or a mutual fund's."""

    SHOPS = ("PURCHASE CVS", "PURCHASE IKEA", "PURCHASE BJS",
             "SALE EXAMPLE OUTFITTERS", "SALE ABC", "SALE SAMPLE GOODS CO", "SALE BEST BUY",
             "PURCHASE AAPL")

    def test_a_short_shop_name_is_not_a_ticker(self):
        for desc in ("PURCHASE CVS", "PURCHASE IKEA", "PURCHASE BJS",
                     "SALE EXAMPLE OUTFITTERS", "SALE ABC", "PURCHASE AAPL"):
            self.assertIsNone(flowmap.investment_flow(desc), desc)

    def test_an_unsigned_shop_sale_is_money_out(self):
        rows = _rows("".join(f"01/{10 + i} {d} 18.20\n"
                             for i, d in enumerate(self.SHOPS)))
        for desc in self.SHOPS:
            self.assertEqual(rows[desc]["amount"], 18.20, desc)
        for desc in ("SALE EXAMPLE OUTFITTERS", "SALE ABC", "SALE BEST BUY",
                     "PURCHASE CVS"):
            self.assertIsNone(rows[desc]["flow"], desc)

    def test_a_company_suffix_alone_does_not_make_a_sale_money_in(self):
        for desc in ("SALE SAMPLE GOODS CO", "SALE MICROSOFT CORP",
                     "SALE EXAMPLE HOLDINGS PLC", "SALE EXAMPLE OUTFITTERS",
                     "SALE BEST BUY", "SALE BEST BUY CO"):
            self.assertFalse(flowmap.sale_of_security(desc), desc)

    def test_a_sale_naming_what_only_a_trade_prints_is_money_in(self):
        sales = ("SALE VANGUARD 500 INDEX FUND", "SALE 25 SHARES EXAMPLE CORP",
                 "SALE 12.5 SH EXAMPLE CO", "SALE @ 41.20 EXAMPLE CORP",
                 "SALE EXAMPLE CORP CUSIP 123456789", "SALE APPLE (AAPL)",
                 "SALE VTSAX", "SALE FIDELITY CONTRAFUND",
                 "SALE US TREASURY NOTE", "SALE COMMON STOCK")
        for desc in sales:
            self.assertTrue(flowmap.sale_of_security(desc), desc)
        rows = _rows("".join(f"01/{10 + i} {d} 900.00\n"
                             for i, d in enumerate(sales)))
        for desc in sales:
            self.assertEqual(rows[desc]["amount"], -900.00, desc)
            self.assertEqual(rows[desc]["flow"], "transfer", desc)


if __name__ == "__main__":
    unittest.main()
