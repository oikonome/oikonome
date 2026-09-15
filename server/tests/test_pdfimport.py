"""Statement-PDF importer: heuristic line parsing (bank + card signs,
two amount columns, year inference), confidence scoring, extract_text
errors, and end-to-end import with cross-source dedup."""

import datetime as dt
import io
import unittest

from oikonome.sync import batches, pdfimport

from .util import add_txn, make_db, write_config

# ---- fixtures ---------------------------------------------------------------

CHECKING_TEXT = """FIRST EXAMPLE BANK
Statement Period: 06/01/2026 through 06/30/2026
Account Number: XXXX1234
Page 1 of 2

Date  Description                              Amount       Balance
Beginning Balance                                          4,000.00
06/03 CHECK #1024                              500.00      3,500.00
06/05 DIRECT DEPOSIT EMPLOYER PAYROLL        2,000.00      5,500.00
06/08 POS PURCHASE SAFEWAY #12                  42.50      5,457.50
06/09 ONLINE TRANSFER TO SAVINGS               500.00      4,957.50
06/12 ATM WITHDRAWAL MAIN ST                   100.00      4,857.50
Total Withdrawals                            1,142.50
Ending Balance                                             4,857.50
"""

CARD_TEXT = """EXAMPLE REWARDS CARD
Statement Period: 12/10/2025 - 01/09/2026
Minimum Payment Due: $35.00
APR 24.99%

Trans Date  Description                              Amount
12/15 AMAZON MKTPLACE PMTS                          $84.99
12/28 GROCERY OUTLET 042                            $23.11
01/02 PAYMENT THANK YOU - WEB                     -$250.00
01/05 REFUND NIKE.COM                              (19.99)
01/07 COFFEE ROASTERS                                $4.25
Total Fees Charged This Period                       $0.00
"""


def make_pdf(text: str) -> bytes:
    """Minimal deterministic one-page text-layer PDF (hand-built xref;
    pypdf round-trips it, Td line moves come back as newlines)."""
    def esc(s):
        return (s.replace("\\", r"\\").replace("(", r"\(")
                 .replace(")", r"\)"))
    parts = ["BT /F1 10 Tf 36 756 Td"]
    for i, ln in enumerate(text.splitlines()):
        if i:
            parts.append("0 -12 Td")
        parts.append(f"({esc(ln)}) Tj")
    parts.append("ET")
    stream = "\n".join(parts).encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
         b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, xref))
    return bytes(out)


# ---- pure parser tests (no DB) ----------------------------------------------


class ParseStatementTests(unittest.TestCase):
    def test_checking_statement(self):
        """Headers/totals skipped; two trailing amount columns use the
        FIRST (amount, not running balance); bank keyword signs."""
        r = pdfimport.parse_statement(CHECKING_TEXT, kind="bank")
        rows = {row["name"]: row for row in r["rows"]}
        self.assertEqual(len(r["rows"]), 5)          # no header/total rows
        self.assertEqual(rows["CHECK #1024"]["amount"], 500.00)
        self.assertEqual(rows["DIRECT DEPOSIT EMPLOYER PAYROLL"]["amount"],
                         -2000.00)                   # deposit = money in
        self.assertEqual(rows["POS PURCHASE SAFEWAY #12"]["amount"], 42.50)
        self.assertEqual(rows["ATM WITHDRAWAL MAIN ST"]["amount"], 100.00)
        self.assertEqual(rows["ONLINE TRANSFER TO SAVINGS"]["flow"],
                         "transfer")
        self.assertEqual(r["period"],
                         (dt.date(2026, 6, 1), dt.date(2026, 6, 30)))
        self.assertEqual({row["date"].month for row in r["rows"]}, {6})
        self.assertGreaterEqual(r["confidence"], 0.9)
        self.assertEqual(r["warnings"], [])

    def test_card_statement_signs_and_parens(self):
        """Card heuristic: charges money out, payment/refund money in;
        parenthesized negatives; payment rows get the cc-payment flow."""
        r = pdfimport.parse_statement(CARD_TEXT, kind="card")
        rows = {row["name"]: row for row in r["rows"]}
        self.assertEqual(len(r["rows"]), 5)          # Minimum/APR/Total skipped
        self.assertEqual(rows["AMAZON MKTPLACE PMTS"]["amount"], 84.99)
        self.assertEqual(rows["PAYMENT THANK YOU - WEB"]["amount"], -250.00)
        self.assertEqual(rows["PAYMENT THANK YOU - WEB"]["flow"],
                         "credit card payment")
        self.assertEqual(rows["REFUND NIKE.COM"]["amount"], -19.99)
        self.assertIsNone(rows["REFUND NIKE.COM"]["flow"])  # refund != payment
        self.assertEqual(rows["COFFEE ROASTERS"]["amount"], 4.25)
        self.assertGreaterEqual(r["confidence"], 0.9)

    def test_year_inference_dec_jan_boundary(self):
        """MM/DD dates on a Dec→Jan statement land in the right years."""
        r = pdfimport.parse_statement(CARD_TEXT, kind="card")
        by_name = {row["name"]: row["date"] for row in r["rows"]}
        self.assertEqual(by_name["AMAZON MKTPLACE PMTS"],
                         dt.date(2025, 12, 15))
        self.assertEqual(by_name["GROCERY OUTLET 042"], dt.date(2025, 12, 28))
        self.assertEqual(by_name["PAYMENT THANK YOU - WEB"],
                         dt.date(2026, 1, 2))
        self.assertEqual(r["period"],
                         (dt.date(2025, 12, 10), dt.date(2026, 1, 9)))

    def test_month_name_dates_and_cr_marker(self):
        text = ("ANYTOWN CREDIT UNION\n"
                "Statement Period: Jan 1, 2026 - Jan 31, 2026\n"
                "Jan 05 COFFEE SHOP DOWNTOWN 4.25\n"
                "Jan 07 RETURNED ITEM CREDIT 19.99 CR\n"
                "Jan 09 Check 2101 250.00\n")
        r = pdfimport.parse_statement(text, kind="bank")
        rows = {row["name"]: row for row in r["rows"]}
        self.assertEqual(rows["COFFEE SHOP DOWNTOWN"]["date"],
                         dt.date(2026, 1, 5))
        self.assertEqual(rows["COFFEE SHOP DOWNTOWN"]["amount"], 4.25)
        self.assertEqual(rows["RETURNED ITEM CREDIT"]["amount"], -19.99)
        self.assertEqual(rows["Check 2101"]["amount"], 250.00)

    def test_signed_bank_export_minus_means_money_out(self):
        """A printed minus on a bank statement = balance went down."""
        text = ("Statement Period: 06/01/2026 to 06/30/2026\n"
                "06/04 SQ *LOCAL BAKERY -12.75\n")
        r = pdfimport.parse_statement(text, kind="bank")
        self.assertEqual(r["rows"][0]["amount"], 12.75)   # engine: money out

    def test_statement_alias_matches_bank(self):
        """UI 'bank statement (PDF)' amount_sign maps to bank heuristic."""
        r = pdfimport.parse_statement(CHECKING_TEXT, kind="statement")
        self.assertEqual(len(r["rows"]), 5)
        self.assertEqual(
            {row["name"]: row["amount"] for row in r["rows"]}
            ["DIRECT DEPOSIT EMPLOYER PAYROLL"], -2000.00)

    def test_investment_statement_dividends_and_buys(self):
        """Brokerage activity: dividend/sell = money in; buy = money out;
        contribution/buy get transfer flow for spend exclusion."""
        text = (
            "EXAMPLE BROKERAGE\n"
            "Statement Period: 03/01/2026 through 03/31/2026\n"
            "03/05 DIVIDEND VTI 12.40\n"
            "03/10 BUY VTI 500.00\n"
            "03/15 CONTRIBUTION ACH DEPOSIT 1000.00\n"
            "03/20 SELL VXUS 250.00\n"
            "03/22 MANAGEMENT FEE 5.00\n"
        )
        r = pdfimport.parse_statement(text, kind="investment")
        rows = {row["name"]: row for row in r["rows"]}
        self.assertEqual(rows["DIVIDEND VTI"]["amount"], -12.40)
        self.assertEqual(rows["BUY VTI"]["amount"], 500.00)
        self.assertEqual(rows["BUY VTI"]["flow"], "transfer")
        self.assertEqual(rows["CONTRIBUTION ACH DEPOSIT"]["amount"], -1000.00)
        self.assertEqual(rows["CONTRIBUTION ACH DEPOSIT"]["flow"], "transfer")
        self.assertEqual(rows["SELL VXUS"]["amount"], -250.00)
        self.assertEqual(rows["MANAGEMENT FEE"]["amount"], 5.00)

    def test_posting_date_column_skipped(self):
        text = ("Statement Period: 06/01/2026 to 06/30/2026\n"
                "06/04 06/05 HARDWARE STORE 33.10\n")
        r = pdfimport.parse_statement(text, kind="bank")
        self.assertEqual(r["rows"][0]["date"], dt.date(2026, 6, 4))
        self.assertEqual(r["rows"][0]["name"], "HARDWARE STORE")

    def test_confidence_drops_on_garbage(self):
        good = pdfimport.parse_statement(CHECKING_TEXT, kind="bank")
        garbage = pdfimport.parse_statement(
            "quarterly investor letter\ncall 1-800-555-0134 anytime\n"
            "thanks for banking with us\n")
        partial = pdfimport.parse_statement(
            "Statement Period: 06/01/2026 to 06/30/2026\n"
            "06/02 MYSTERY LINE WITH NO AMOUNT\n"
            "06/03 STORE PURCHASE 12.00\n")
        self.assertEqual(garbage["rows"], [])
        self.assertEqual(garbage["confidence"], 0.0)
        self.assertTrue(garbage["warnings"])
        self.assertLess(partial["confidence"], good["confidence"])
        self.assertTrue(partial["warnings"])

    def test_yearless_without_period_warns(self):
        r = pdfimport.parse_statement("06/03 STORE PURCHASE 12.00\n")
        self.assertEqual(len(r["rows"]), 1)
        self.assertTrue(any("guessed" in w for w in r["warnings"]))

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            pdfimport.parse_statement("x", kind="plaid")


# ---- extract_text -----------------------------------------------------------


class ExtractTextTests(unittest.TestCase):
    def test_roundtrip(self):
        text = pdfimport.extract_text(make_pdf(CHECKING_TEXT))
        self.assertIn("06/08 POS PURCHASE SAFEWAY #12", text)
        self.assertIn("Statement Period: 06/01/2026 through 06/30/2026", text)

    def test_encrypted_pdf_rejected(self):
        from pypdf import PdfWriter
        w = PdfWriter()
        w.add_blank_page(width=200, height=200)
        w.encrypt("owner-secret")
        buf = io.BytesIO()
        w.write(buf)
        with self.assertRaises(ValueError) as cm:
            pdfimport.extract_text(buf.getvalue())
        self.assertIn("password", str(cm.exception))

    def test_no_text_layer_rejected(self):
        from pypdf import PdfWriter
        w = PdfWriter()
        w.add_blank_page(width=200, height=200)   # image-only/blank: no text
        buf = io.BytesIO()
        w.write(buf)
        with self.assertRaises(ValueError) as cm:
            pdfimport.extract_text(buf.getvalue())
        self.assertIn("no text layer", str(cm.exception))

    def test_not_a_pdf_rejected(self):
        with self.assertRaises(ValueError):
            pdfimport.extract_text(b"Date,Amount\n06/03,12.00\n")


# ---- end-to-end import (DB) -------------------------------------------------


class ImportPdfTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_stored_raw_line_masks_long_digit_runs(self):
        """Statement lines routinely embed full account numbers in running
        text, and raw rides /export verbatim — any 8+ digit run is masked
        to its last 4 (amounts, dates and short check numbers all break
        under 8 consecutive digits, so ledger content survives)."""
        pdf = make_pdf(
            "Statement Period 01/01/2026 - 01/31/2026\n"
            "01/05 TRANSFER TO ACCT 123456789012 250.00\n")
        pdfimport.import_pdf(self.conn, "chk", pdf)
        raw = self.conn.execute(
            "SELECT raw FROM transactions WHERE id LIKE 'pdf:%'"
        ).fetchone()["raw"]
        self.assertNotIn("123456789012", raw["line"])
        self.assertIn("…9012", raw["line"])

    def test_import_with_cross_source_dedup(self):
        """An aggregator row already covering the Safeway charge absorbs
        exactly one PDF row; the rest import engine-signed."""
        add_txn(self.conn, dt.date(2026, 6, 8), 42.50, "SAFEWAY",
                account="chk")
        r = pdfimport.import_pdf(self.conn, "chk", make_pdf(CHECKING_TEXT))
        self.assertEqual(r["skipped_duplicates"], 1)
        self.assertEqual(r["imported"], 4)
        self.assertGreaterEqual(r["confidence"], 0.9)
        self.assertEqual(r["warnings"], [])
        rows = {row["name"]: row for row in self.conn.execute(
            "SELECT name, amount, category_primary FROM transactions "
            "WHERE id LIKE 'pdf:%'").fetchall()}
        self.assertNotIn("POS PURCHASE SAFEWAY #12", rows)   # deduped
        self.assertEqual(rows["DIRECT DEPOSIT EMPLOYER PAYROLL"]["amount"],
                         -2000.00)
        self.assertEqual(rows["CHECK #1024"]["amount"], 500.00)
        # bank transfer routed through flowmap → spend-excluded
        self.assertEqual(rows["ONLINE TRANSFER TO SAVINGS"]["category_primary"],
                         "TRANSFER_OUT")

    def test_reimport_is_idempotent(self):
        pdf = make_pdf(CHECKING_TEXT)
        pdfimport.import_pdf(self.conn, "chk", pdf)
        r2 = pdfimport.import_pdf(self.conn, "chk", pdf)
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE id LIKE 'pdf:%'"
        ).fetchone()["n"]
        self.assertEqual(n, 5)
        self.assertEqual(r2["imported"], 5)      # upsert, not duplicates

    def test_card_import_flow_categories(self):
        r = pdfimport.import_pdf(self.conn, "card", make_pdf(CARD_TEXT),
                                 amount_sign="card")
        self.assertEqual(r["imported"], 5)
        rows = {row["name"]: row for row in self.conn.execute(
            "SELECT name, amount, category_primary, category_detailed "
            "FROM transactions WHERE id LIKE 'pdf:%'").fetchall()}
        pay = rows["PAYMENT THANK YOU - WEB"]
        self.assertEqual(pay["amount"], -250.00)
        self.assertEqual(pay["category_detailed"],
                         "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        self.assertEqual(pay["category_primary"], "LOAN_PAYMENTS")
        self.assertEqual(rows["REFUND NIKE.COM"]["amount"], -19.99)
        self.assertIsNone(rows["REFUND NIKE.COM"]["category_primary"])

    def test_batch_tagging(self):
        bid = batches.create(self.conn, "pdf", "chk", "statement.pdf")
        r = pdfimport.import_pdf(self.conn, "chk", make_pdf(CHECKING_TEXT),
                                 batch_id=bid)
        batches.finish(self.conn, bid, r["rows"])
        tagged = self.conn.execute(
            "SELECT COUNT(*) AS n FROM transactions "
            "WHERE id LIKE 'pdf:%%' AND raw->>'_batch' = %s", (bid,)
        ).fetchone()["n"]
        self.assertEqual(tagged, 5)

    def test_bad_amount_sign_rejected(self):
        with self.assertRaises(ValueError):
            pdfimport.import_pdf(self.conn, "chk", make_pdf(CHECKING_TEXT),
                                 amount_sign="plaid")


if __name__ == "__main__":
    unittest.main()


class SignCorruptionRegressionTests(unittest.TestCase):
    """Statement lines that used to be parsed with the wrong money sign —
    each one a silent corruption of the ledger's direction."""

    def test_charge_at_payment_named_merchant_stays_a_charge(self):
        r = pdfimport.parse_statement(
            "Statement Period 01/01/2026 - 01/31/2026\n"
            "01/06 UNIVERSAL PAYMENT CORP UTILITY 120.00\n"
            "01/07 CREDIT KARMA 9.99\n"
            "01/08 ONLINE PAYMENT - THANK YOU 250.00\n",
            kind="card")
        rows = {row["name"]: row for row in r["rows"]}
        self.assertEqual(rows["UNIVERSAL PAYMENT CORP UTILITY"]["amount"], 120.0)
        self.assertIsNone(rows["UNIVERSAL PAYMENT CORP UTILITY"]["flow"])
        self.assertEqual(rows["CREDIT KARMA"]["amount"], 9.99)
        # the real settlement still maps
        self.assertEqual(rows["ONLINE PAYMENT - THANK YOU"]["amount"], -250.0)
        self.assertEqual(rows["ONLINE PAYMENT - THANK YOU"]["flow"],
                         "credit card payment")

    def test_balance_transfer_is_parsed_not_swallowed(self):
        r = pdfimport.parse_statement(
            "Statement Period 01/01/2026 - 01/31/2026\n"
            "01/05 BALANCE TRANSFER FROM CHASE 3,000.00\n",
            kind="card")
        self.assertEqual(len(r["rows"]), 1)
        self.assertEqual(r["rows"][0]["flow"], "transfer")

    def test_dated_summary_lines_warn(self):
        r = pdfimport.parse_statement(
            "Statement Period 01/01/2026 - 01/31/2026\n"
            "01/01 BEGINNING BALANCE 500.00\n"
            "01/05 GROCERY MART 42.50\n",
            kind="bank")
        self.assertEqual(len(r["rows"]), 1)
        self.assertTrue(any("summary words" in w for w in r["warnings"]))

    def test_incoming_zelle_is_money_in(self):
        r = pdfimport.parse_statement(
            "Statement Period 01/01/2026 - 01/31/2026\n"
            "01/05 Zelle payment from JOHN DOE 500.00\n"
            "01/06 Zelle payment to JANE ROE 75.00\n",
            kind="bank")
        rows = {row["name"]: row for row in r["rows"]}
        self.assertEqual(rows["Zelle payment from JOHN DOE"]["amount"], -500.0)
        self.assertEqual(rows["Zelle payment to JANE ROE"]["amount"], 75.0)

    def test_zero_cell_three_column_layout_picks_nonzero(self):
        r = pdfimport.parse_statement(
            "Statement Period 01/01/2026 - 01/31/2026\n"
            "01/05 PAYROLL DIRECT DEP 0.00 2,000.00 5,432.10\n",
            kind="bank")
        self.assertEqual(r["rows"][0]["amount"], -2000.0)
