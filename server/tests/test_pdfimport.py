"""Statement-PDF importer: heuristic line parsing (bank + card signs,
two amount columns, year inference), confidence scoring, extract_text
errors, and end-to-end import with cross-source dedup."""

import datetime as dt
import io
import resource
import time
import tracemalloc
import unittest
import zlib
from unittest import mock

from oikonome.sync import batches, pdf_sandbox, pdfimport

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


def make_layout_pdf(pages: list[list[tuple[float, list[tuple[float, str]]]]]
                    ) -> bytes:
    """A PDF whose cells are drawn as separate runs: `pages` is a list of
    pages, each a list of (y, [(x, text), ...]) rows — the shape a bank's
    statement generator produces, one Tj per cell under a column header."""
    def esc(s):
        return (s.replace("\\", r"\\").replace("(", r"\(")
                 .replace(")", r"\)"))
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    kids = []
    font_obj = 3
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for page in pages:
        parts = []
        for y, cells in page:
            for x, text in cells:
                parts.append(f"BT /F1 10 Tf {x:.1f} {y:.1f} Td ({esc(text)}) Tj ET")
        stream = "\n".join(parts).encode("latin-1")
        contents_no = len(objs) + 2
        page_no = len(objs) + 1
        objs.append((b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                     b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                     % (font_obj, contents_no)))
        objs.append(b"<< /Length %d >>\nstream\n%s\nendstream"
                    % (len(stream), stream))
        kids.append(page_no)
    objs[1] = (b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids)
               + b"] /Count %d >>" % len(kids))
    return assemble_pdf(objs)


def assemble_pdf(objs: list[bytes]) -> bytes:
    """Wrap numbered object bodies (1-based, catalog first) in a PDF."""
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


def flate_stream(content: bytes, extra: bytes = b"") -> bytes:
    z = zlib.compress(content, 9)
    return (b"<< /Length %d /Filter /FlateDecode %s>>\nstream\n%s\nendstream"
            % (len(z), extra, z))


def flate_page_pdf(content: bytes) -> bytes:
    """One page whose content stream is `content`, flate-compressed."""
    return assemble_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
         b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        flate_stream(content),
    ])


def xobject_page_pdf(xobject: bytes, subtype: bytes | None) -> bytes:
    """One page with a tiny content stream that draws XObject /X, whose
    flate-compressed stream is `xobject` and whose /Subtype is `subtype`
    (None leaves the key out)."""
    extra = b"/Type /XObject /BBox [0 0 612 792] "
    if subtype is not None:
        extra += b"/Subtype " + subtype + b" "
    return assemble_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
         b"/Resources << /Font << /F1 4 0 R >> /XObject << /X 6 0 R >> >> "
         b"/Contents 5 0 R >>"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        flate_stream(b"/X Do"),
        flate_stream(xobject, extra),
    ])


def form_fan_out_pdf(levels: int = 4, fan: int = 10,
                     leaf_ops: int = 200) -> bytes:
    """A page drawing form 1, which draws form 2 `fan` times, and so on
    down to a small leaf: fan**levels leaf invocations from a tiny file."""
    leaf = b"BT /F1 1 Tf 0 0 Td (a) Tj ET\n" * (leaf_ops // 5)
    first_form = 6
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
         b"/Resources << /Font << /F1 4 0 R >> /XObject << /X %d 0 R >> >> "
         b"/Contents 5 0 R >>" % first_form),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        flate_stream(b"/X Do"),
    ]
    for level in range(levels):
        num = first_form + level
        if level < levels - 1:
            body = b"/X Do\n" * fan
            res = (b"/Resources << /Font << /F1 4 0 R >> "
                   b"/XObject << /X %d 0 R >> >> " % (num + 1))
        else:
            body, res = leaf, b"/Resources << /Font << /F1 4 0 R >> >> "
        objs.append(flate_stream(
            body, b"/Type /XObject /Subtype /Form /BBox [0 0 612 792] " + res))
    return assemble_pdf(objs)


def double_flate_stream(size: int, extra: bytes = b"") -> bytes:
    """`size` zero bytes compressed twice: a few KB on disk, and just under
    the decoder cap when `size` is 3.9 MB, so no single stream trips it."""
    z = zlib.compress(zlib.compress(b"\0" * size, 9), 9)
    return (b"<< /Length %d /Filter [/FlateDecode /FlateDecode] %s>>\n"
            b"stream\n%s\nendstream" % (len(z), extra, z))


def page_pdf(resources: bytes = b"", objs: tuple[bytes, ...] = (),
             page_extra: bytes = b"", fonts: bytes = b"/F1 4 0 R",
             content: bytes = b"BT /F1 12 Tf 20 700 Td (Total 12.00) Tj ET"
             ) -> bytes:
    """One page drawing `content`, with `resources` spliced into its
    resource dict beside the fonts and `page_extra` into the page dict.
    The objects in `objs` are numbered from 6."""
    return assemble_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
         b"/Resources << /Font << " + fonts + b" >> " + resources + b" >> "
         + page_extra + b" /Contents 5 0 R >>"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        flate_stream(content),
        *objs,
    ])


def many_inflating_forms_pdf(count: int = 200) -> bytes:
    """A 50 KB page whose resources list `count` distinct forms, each
    inflating to 3.9 MB: 780 MB if every one is decoded and kept."""
    names = b" ".join(b"/X%d %d 0 R" % (i, 6 + i) for i in range(count))
    return page_pdf(b"/XObject << " + names + b" >>", tuple(
        double_flate_stream(3_900_000, b"/Type /XObject /Subtype /Form "
                            b"/BBox [0 0 1 1] ") for _ in range(count)))


def multi_page_pdf(pages: int, fonts_per_page: int = 0,
                   map_size: int = 3_900_000, shared_map: int = 0) -> bytes:
    """`pages` pages, each drawing a statement line with font /F1. Each
    page also lists `fonts_per_page` fonts of its own, every one with a
    distinct ToUnicode map inflating to `map_size`; with `shared_map`, /F1
    carries one ToUnicode map of that size that every page shares."""
    font = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica %s>>" % (
        b"/ToUnicode 5 0 R " if shared_map else b"")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", font,
            flate_stream(b"BT /F1 12 Tf 20 700 Td "
                         b"(06/03 CHECK 1024 500.00) Tj ET"),
            double_flate_stream(shared_map or 1)]
    kids = []
    for _ in range(pages):
        num = len(objs) + 1
        own = b" ".join(
            b"/U%d << /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            b"/ToUnicode %d 0 R >>" % (i, num + 1 + i)
            for i in range(fonts_per_page))
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    b"/Resources << /Font << /F1 3 0 R " + own + b" >> >> "
                    b"/Contents 4 0 R >>")
        objs.extend(double_flate_stream(map_size)
                    for _ in range(fonts_per_page))
        kids.append(b"%d 0 R" % num)
    objs[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(kids), pages)
    return assemble_pdf(objs)


def image_page_pdf(width: int, height: int, stream: bytes) -> bytes:
    """A receipt-like page: a line of text and image /Im0 (object 6, the
    `stream` built by image_stream) drawn beneath it."""
    return page_pdf(b"/XObject << /Im0 6 0 R >>", (stream,), content=(
        b"BT /F1 12 Tf 20 700 Td (Total 12.00) Tj ET "
        b"q %d 0 0 %d 20 600 cm /Im0 Do Q" % (min(width, 500),
                                             min(height, 80))))


def image_stream(width: int, height: int, data: bytes, double=False,
                 colorspace: bytes = b"/DeviceRGB") -> bytes:
    extra = (b"/Type /XObject /Subtype /Image /Width %d /Height %d "
             b"/ColorSpace %s /BitsPerComponent 8 " % (width, height,
                                                       colorspace))
    if double:
        z = zlib.compress(zlib.compress(data, 9), 9)
        filt = b"[/FlateDecode /FlateDecode]"
    else:
        z, filt = zlib.compress(data, 6), b"/FlateDecode"
    return (b"<< /Length %d /Filter %s %s>>\nstream\n%s\nendstream"
            % (len(z), filt, extra, z))


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

    def test_card_unsigned_cash_back_rewards_are_credits(self):
        """A card statement's unsigned cash-back reward is a credit in the
        singular, the plural and the redemption wording alike."""
        text = ("Statement Period: 06/01/2026 - 06/30/2026\n"
                "06/05 CASH BACK REWARD 10.00\n"
                "06/06 CASH BACK REWARDS 11.00\n"
                "06/07 CASHBACK REWARDS REDEMPTION 12.00\n"
                "06/08 COFFEE ROASTERS 4.25\n")
        r = pdfimport.parse_statement(text, kind="card")
        self.assertEqual({row["name"]: row["amount"] for row in r["rows"]}, {
            "CASH BACK REWARD": -10.00,
            "CASH BACK REWARDS": -11.00,
            "CASHBACK REWARDS REDEMPTION": -12.00,
            "COFFEE ROASTERS": 4.25,
        })

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

    def test_bank_money_out_lines_with_inbound_words_stay_money_out(self):
        """Cash back taken with a purchase, a deposit the bank reversed or
        returned, and any line printed with a minus all take money out of
        the account, even though their wording contains a money-in keyword.
        A cash-back reward (singular or plural, redeemed or not), a refunded
        security deposit, a deposit returned to the household by someone
        else, and a reversed purchase are still money in."""
        text = (
            "Statement Period: 09/01/2025 - 09/30/2025\n"
            "09/12 Purchase With Cash Back $ 20.00 Authorized On 09/12 "
            "Grocer 80.00 1,120.00\n"
            "09/15 Card Purchase With Pin 09/15 Grocer Cash Back "
            "-60.00 560.00\n"
            "09/14 Deposit Reversal -500.00 620.00\n"
            "09/16 Deposit Reversal 400.00 220.00\n"
            "09/17 Returned Deposit Item 250.00 370.00\n"
            "09/17 Deposited Item Returned 25.00 345.00\n"
            "09/18 Interest Paid -1.00 369.00\n"
            "09/19 Cash Back Reward 5.00 374.00\n"
            "09/19 Cash Back Rewards 6.00 380.00\n"
            "09/19 Cashback Rewards Redemption 7.00 387.00\n"
            "09/20 Purchase Reversal 20.00 394.00\n"
            "09/21 Deposit 100.00 494.00\n"
            "09/22 Security Deposit Return Acme Property Mgmt 300.00 794.00\n"
            "09/23 Returned deposit from Oak Apts 150.00 944.00\n"
        )
        r = pdfimport.parse_statement(text, kind="bank")
        rows = [(row["name"], row["amount"]) for row in r["rows"]]
        self.assertEqual(rows, [
            ("Purchase With Cash Back $ 20.00 Authorized On 09/12 Grocer",
             80.00),
            ("Card Purchase With Pin 09/15 Grocer Cash Back", 60.00),
            ("Deposit Reversal", 500.00),
            ("Deposit Reversal", 400.00),
            ("Returned Deposit Item", 250.00),
            ("Deposited Item Returned", 25.00),
            ("Interest Paid", 1.00),
            ("Cash Back Reward", -5.00),
            ("Cash Back Rewards", -6.00),
            ("Cashback Rewards Redemption", -7.00),
            ("Purchase Reversal", -20.00),
            ("Deposit", -100.00),
            ("Security Deposit Return Acme Property Mgmt", -300.00),
            ("Returned deposit from Oak Apts", -150.00),
        ])

    def test_investment_unsigned_lines_follow_their_income_words(self):
        """On a brokerage statement an unsigned line is read by its activity
        words, and income words win: a dividend, a reinvested dividend's
        income line, a capital gain or fund distribution "paid" to the
        holder, and a cash-back reward are money in. A printed minus or
        parentheses is money out whatever the wording, so the buy half of a
        reinvestment (printed negative) and a signed contribution leave the
        account. A retirement account's own distribution and a
        "distribution out" line are money paid out of the account."""
        text = (
            "Statement Period: 03/01/2026 through 03/31/2026\n"
            "03/05 IRA DISTRIBUTION 1,000.00\n"
            "03/06 DISTRIBUTION OUT 900.00\n"
            "03/07 DIVIDEND REINVESTMENT XMPLA 50.00\n"
            "03/08 DIVIDEND REINVESTMENT XMPLB -40.00\n"
            "03/09 CONTRIBUTION -300.00\n"
            "03/10 CAPITAL GAIN DISTRIBUTION 30.00\n"
            "03/11 DIVIDEND XMPLA 12.40\n"
            "03/15 REINVEST DIVIDEND XMPLA 50.00\n"
            "03/15 REINVEST SHARES XMPLA (50.00)\n"
            "03/16 DIVIDEND REINVESTED XMPLB 20.00\n"
            "03/17 CAP GAIN DISTRIBUTION PAID 30.00\n"
            "03/18 LT CAP GAIN DISTRIBUTION PAID 100.00\n"
            "03/19 DIVIDEND/DISTRIBUTION PAID 9.00\n"
            "03/20 CASH BACK REWARDS 4.00\n"
        )
        r = pdfimport.parse_statement(text, kind="investment")
        rows = {row["name"]: row["amount"] for row in r["rows"]}
        self.assertEqual(rows, {
            "IRA DISTRIBUTION": 1000.00,
            "DISTRIBUTION OUT": 900.00,
            "DIVIDEND REINVESTMENT XMPLA": -50.00,
            "DIVIDEND REINVESTMENT XMPLB": 40.00,
            "CONTRIBUTION": 300.00,
            "CAPITAL GAIN DISTRIBUTION": -30.00,
            "DIVIDEND XMPLA": -12.40,
            "REINVEST DIVIDEND XMPLA": -50.00,
            "REINVEST SHARES XMPLA": 50.00,
            "DIVIDEND REINVESTED XMPLB": -20.00,
            "CAP GAIN DISTRIBUTION PAID": -30.00,
            "LT CAP GAIN DISTRIBUTION PAID": -100.00,
            "DIVIDEND/DISTRIBUTION PAID": -9.00,
            "CASH BACK REWARDS": -4.00,
        })

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
            "03/05 DIVIDEND XMPLA 12.40\n"
            "03/10 BUY XMPLA 500.00\n"
            "03/15 CONTRIBUTION ACH DEPOSIT 1000.00\n"
            "03/20 SELL XMPLB 250.00\n"
            "03/22 MANAGEMENT FEE 5.00\n"
        )
        r = pdfimport.parse_statement(text, kind="investment")
        rows = {row["name"]: row for row in r["rows"]}
        self.assertEqual(rows["DIVIDEND XMPLA"]["amount"], -12.40)
        self.assertEqual(rows["BUY XMPLA"]["amount"], 500.00)
        self.assertEqual(rows["BUY XMPLA"]["flow"], "transfer")
        self.assertEqual(rows["CONTRIBUTION ACH DEPOSIT"]["amount"], -1000.00)
        self.assertEqual(rows["CONTRIBUTION ACH DEPOSIT"]["flow"], "transfer")
        self.assertEqual(rows["SELL XMPLB"]["amount"], -250.00)
        self.assertEqual(rows["MANAGEMENT FEE"]["amount"], 5.00)

    def test_investment_printed_minus_means_money_out(self):
        """A printed minus or parentheses on a brokerage line means cash
        left the account, as on a bank statement — so a signed buy, fee or
        withdrawal carries the same sign as its unsigned twin."""
        text = (
            "Statement Period: 03/01/2026 through 03/31/2026\n"
            "03/10 YOU BOUGHT XMPLA -500.00\n"
            "03/11 BUY XMPLB 300.00\n"
            "03/12 WITHDRAWAL TO BANK (1,000.00)\n"
            "03/22 ADVISORY FEE -5.00\n"
            "03/23 ACCOUNT ADJUSTMENT -7.00\n"
            "03/24 ACCOUNT ADJUSTMENT 8.00 CR\n"
            "03/25 DIVIDEND XMPLA 12.40\n"
        )
        r = pdfimport.parse_statement(text, kind="investment")
        rows = {row["name"]: row["amount"] for row in r["rows"]}
        self.assertEqual(rows["YOU BOUGHT XMPLA"], 500.00)
        self.assertEqual(rows["BUY XMPLB"], 300.00)
        self.assertEqual(rows["WITHDRAWAL TO BANK"], 1000.00)
        self.assertEqual(rows["ADVISORY FEE"], 5.00)
        self.assertEqual(rows["DIVIDEND XMPLA"], -12.40)
        adjustments = sorted(row["amount"] for row in r["rows"]
                             if row["name"] == "ACCOUNT ADJUSTMENT")
        self.assertEqual(adjustments, [-8.00, 7.00])   # CR in, minus out

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

    def test_separate_opening_date_line_is_the_period_start(self):
        """An Opening Date printed on its own line starts the cycle; read
        as the end it would put the January rows a year early."""
        text = ("Opening Date 12/05/25\n"
                "Closing Date 01/04/26\n"
                "12/20 BOOKSHOP DOWNTOWN 18.40\n"
                "01/02 COFFEE PLACE 5.25\n")
        r = pdfimport.parse_statement(text, kind="card")
        by_name = {row["name"]: row["date"] for row in r["rows"]}
        self.assertEqual(by_name["BOOKSHOP DOWNTOWN"], dt.date(2025, 12, 20))
        self.assertEqual(by_name["COFFEE PLACE"], dt.date(2026, 1, 2))
        self.assertEqual(r["period"],
                         (dt.date(2025, 12, 5), dt.date(2026, 1, 4)))

    def test_account_opening_date_does_not_anchor_the_year(self):
        """An account-opening date years back is not a cycle start, and
        with no closing date it is no anchor at all."""
        text = ("Account opening date 03/15/19\n"
                "Closing Date 01/04/26\n"
                "12/20 BOOKSHOP DOWNTOWN 18.40\n"
                "01/02 COFFEE PLACE 5.25\n")
        r = pdfimport.parse_statement(text, kind="card")
        by_name = {row["name"]: row["date"] for row in r["rows"]}
        self.assertEqual(by_name["BOOKSHOP DOWNTOWN"], dt.date(2025, 12, 20))
        self.assertEqual(by_name["COFFEE PLACE"], dt.date(2026, 1, 2))
        alone = pdfimport.parse_statement(
            "Account opening date 03/15/19\n01/02 COFFEE PLACE 5.25\n",
            kind="card")
        self.assertIsNone(alone["period"])
        self.assertNotEqual(alone["rows"][0]["date"].year, 2019)

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            pdfimport.parse_statement("x", kind="plaid")


# a checking statement drawn cell by cell under a column header: the
# column, not the wording, says which way the money went
HEADER = (740, [(36, "Date"), (100, "Description"), (380, "Withdrawals"),
                (460, "Deposits"), (540, "Balance")])
PERIOD = (760, [(36, "Statement Period: 06/01/2026 - 06/30/2026")])


def _layout_statement():
    return make_layout_pdf([[
        PERIOD, HEADER,
        (720, [(36, "06/03"), (100, "POS PURCHASE W/CASH BACK SAFEWAY"),
               (380, "50.00"), (540, "1,950.00")]),
        (708, [(36, "06/05"), (100, "IRA DISTRIBUTION"), (460, "1,000.00"),
               (540, "2,950.00")]),
        (696, [(36, "06/07"), (100, "CHECK 1024"), (380, "500.00"),
               (540, "2,450.00")]),
        (684, [(36, "06/09"), (100, "DEPOSIT REVERSAL"), (380, "-25.00"),
               (540, "2,425.00")]),
        (672, [(36, "06/11"), (100, "DIRECT DEPOSIT PAYROLL"), (460, "2,000.00"),
               (540, "4,425.00")]),
    ]])


class ColumnLayoutSignTests(unittest.TestCase):
    """The sign of an unsigned line comes from the column it is drawn
    under, before any keyword: the statement itself says so."""

    def test_the_column_decides_the_sign_whatever_the_words_say(self):
        text, hints = pdfimport.extract_statement(_layout_statement())
        r = pdfimport.parse_statement(text, kind="bank", hints=hints)
        rows = {row["name"]: row for row in r["rows"]}
        self.assertEqual(len(rows), 5, text)
        # 'cash back' reads as money in by keyword; the Withdrawals column
        # says money out
        self.assertEqual(rows["POS PURCHASE W/CASH BACK SAFEWAY"]["amount"],
                         50.00)
        # 'distribution' has no bank keyword; the Deposits column says in
        self.assertEqual(rows["IRA DISTRIBUTION"]["amount"], -1000.00)
        self.assertEqual(rows["CHECK 1024"]["amount"], 500.00)
        self.assertEqual(rows["DIRECT DEPOSIT PAYROLL"]["amount"], -2000.00)
        # a printed minus still wins over the column
        self.assertEqual(rows["DEPOSIT REVERSAL"]["amount"], 25.00)
        self.assertEqual(r["warnings"], [])

    def test_a_page_without_its_own_header_gets_no_hints(self):
        """The layout rendering scales each page on its own, so page 1's
        character columns say nothing about page 2: a page that prints
        no header falls back to the keywords rather than guess."""
        pdf = make_layout_pdf([
            [PERIOD, HEADER,
             (720, [(36, "06/03"), (100, "CHECK 1024"), (380, "500.00"),
                    (540, "1,500.00")])],
            [(740, [(36, "06/20"), (100, "RMD DISTRIBUTION"), (460, "900.00"),
                    (540, "2,400.00")])],
        ])
        text, hints = pdfimport.extract_statement(pdf)
        lines = text.splitlines()
        self.assertEqual([h for ln, h in zip(lines, hints) if "CHECK" in ln],
                         ["out"])
        self.assertEqual([h for ln, h in zip(lines, hints) if "RMD" in ln],
                         [None])
        # and a header printed again on page 2 is read afresh
        pdf = make_layout_pdf([
            [PERIOD, HEADER,
             (720, [(36, "06/03"), (100, "CHECK 1024"), (380, "500.00"),
                    (540, "1,500.00")])],
            [HEADER,
             (720, [(36, "06/20"), (100, "RMD DISTRIBUTION"), (460, "900.00"),
                    (540, "2,400.00")])],
        ])
        text, hints = pdfimport.extract_statement(pdf)
        r = pdfimport.parse_statement(text, kind="bank", hints=hints)
        rows = {row["name"]: row for row in r["rows"]}
        self.assertEqual(rows["RMD DISTRIBUTION"]["amount"], -900.00)
        self.assertEqual(rows["CHECK 1024"]["amount"], 500.00)

    def test_without_a_header_the_keywords_still_decide(self):
        pdf = make_layout_pdf([[
            PERIOD,
            (720, [(36, "06/03"), (100, "ATM WITHDRAWAL MAIN ST"),
                   (380, "50.00")]),
            (708, [(36, "06/05"), (100, "DIRECT DEPOSIT PAYROLL"),
                   (460, "2,000.00")]),
        ]])
        text, hints = pdfimport.extract_statement(pdf)
        self.assertTrue(hints is None or not any(hints))
        r = pdfimport.parse_statement(text, kind="bank", hints=hints)
        rows = {row["name"]: row for row in r["rows"]}
        self.assertEqual(rows["ATM WITHDRAWAL MAIN ST"]["amount"], 50.00)
        self.assertEqual(rows["DIRECT DEPOSIT PAYROLL"]["amount"], -2000.00)

    def test_an_amount_under_the_balance_column_alone_gives_no_sign(self):
        pdf = make_layout_pdf([[
            PERIOD, HEADER,
            (720, [(36, "06/03"), (100, "SERVICE FEE"), (540, "1,950.00")]),
        ]])
        text, hints = pdfimport.extract_statement(pdf)
        self.assertEqual([h for h in hints if h], [])

    def test_one_amount_column_names_no_direction(self):
        text, hints = pdfimport.extract_statement(make_pdf(CHECKING_TEXT))
        self.assertTrue(hints is None or not any(hints))
        r = pdfimport.parse_statement(text, kind="bank", hints=hints)
        self.assertEqual(len(r["rows"]), 5)

    def test_import_reads_the_columns_end_to_end(self):
        conn = make_db()
        try:
            write_config(conn)
            res = pdfimport.import_pdf(conn, "chk", _layout_statement(),
                                       amount_sign="bank")
            self.assertEqual(res.get("imported"), 5, res)
            got = {r["name"]: r["amount"] for r in conn.execute(
                "SELECT name, amount FROM transactions "
                "WHERE account_id='chk'").fetchall()}
        finally:
            conn.close()
        # the distribution landed as money in: the opposite sign of the
        # purchase, the same sign as the paycheck
        self.assertEqual(abs(got["IRA DISTRIBUTION"]), 1000.00)
        self.assertEqual(abs(got["POS PURCHASE W/CASH BACK SAFEWAY"]), 50.00)
        self.assertLess(got["IRA DISTRIBUTION"] * got["POS PURCHASE W/CASH BACK SAFEWAY"], 0)
        self.assertGreater(got["IRA DISTRIBUTION"] * got["DIRECT DEPOSIT PAYROLL"], 0)
        self.assertGreater(got["CHECK 1024"] * got["POS PURCHASE W/CASH BACK SAFEWAY"], 0)


class ParseTimeTests(unittest.TestCase):
    """The parser runs regexes inside the request and they hold the GIL
    for the whole server, so a line crafted to make them backtrack must
    still cost time linear in its length."""

    def test_amount_token_search_is_linear_on_a_long_digit_run(self):
        t0 = time.perf_counter()
        list(pdfimport._AMOUNT_RE.finditer("7" * 200_000))
        list(pdfimport._AMOUNT_RE.finditer("1" + ",234" * 60_000))
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_a_huge_statement_line_is_skipped_quickly(self):
        text = ("Statement Period: 01/01/2026 - 01/31/2026\n"
                "01/05 x " + "7" * 200_000 + "\n"
                "01/06 payment" + " " * 200_000 + "thank you 1.00\n"
                "01/07 GROCERY MART 42.50\n")
        t0 = time.perf_counter()
        r = pdfimport.parse_statement(text, kind="card")
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual([row["name"] for row in r["rows"]], ["GROCERY MART"])
        self.assertTrue(any("too long" in w for w in r["warnings"]))

    def test_amount_tokens_still_read_after_separators(self):
        for line, want in (("01/05 STORE $1,234.56", 1234.56),
                           ("01/05 STORE (12.00)", 12.00),
                           ("01/05 STORE -1,000.00", 1000.00),
                           ("01/05 STORE 4.25 CR", 4.25)):
            with self.subTest(line=line):
                m = pdfimport._trailing_amounts(line)[0]
                self.assertEqual(pdfimport._amount_parts(m)[0], want)


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


class ExpansionLimitTests(unittest.TestCase):
    """A small upload must not be able to expand into enough parsed content
    to exhaust the app's memory or pin a worker: the limits apply BEFORE
    pypdf inflates and parses, not to the text that comes out."""

    BOMB_SEGMENT = b"BT /F1 1 Tf 0 0 Td (a) Tj ET\n"

    def _peak_rss_mb(self) -> float:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

    def test_flate_bomb_page_is_refused_quickly_with_bounded_memory(self):
        bomb = flate_page_pdf(self.BOMB_SEGMENT * (15_000_000 // 29))
        self.assertLess(len(bomb), 60_000)       # a tiny upload ...
        before = self._peak_rss_mb()
        t0 = time.perf_counter()
        with self.assertRaises(ValueError) as cm:
            pdfimport.extract_text(bomb)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertLess(self._peak_rss_mb() - before, 150)
        self.assertIn("size limit", str(cm.exception))

    def test_page_over_the_content_budget_is_refused_before_parsing(self):
        # under every pypdf decoder limit, over the per-page budget
        page = flate_page_pdf(
            self.BOMB_SEGMENT * (pdfimport.PAGE_CONTENT_LIMIT // 29 + 100))
        t0 = time.perf_counter()
        with self.assertRaises(ValueError) as cm:
            pdfimport.extract_text(page)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertIn("size limit", str(cm.exception))

    def test_xobject_of_any_non_image_subtype_counts_toward_the_page_budget(self):
        # pypdf draws an XObject as a form unless it is an image, so a
        # stream labelled with no subtype or an unexpected one would be
        # parsed in full; it must be measured like a /Form.
        body = self.BOMB_SEGMENT * (pdfimport.PAGE_CONTENT_LIMIT // 29 + 100)
        for subtype in (None, b"/PS", b"/Bogus", b"/Form"):
            with self.subTest(subtype=subtype):
                pdf = xobject_page_pdf(body, subtype)
                page = pdfimport.PdfReader(io.BytesIO(pdf)).pages[0]
                self.assertGreater(pdfimport._page_content_bytes(page),
                                   pdfimport.PAGE_CONTENT_LIMIT)
                with self.assertRaises(ValueError) as cm:
                    pdfimport.extract_text(pdf)
                self.assertIn("size limit", str(cm.exception))

    def test_image_xobject_is_not_counted_as_page_content(self):
        # an image is decoded by nobody during text extraction, so a large
        # one must not push an ordinary scanned-logo page over the budget
        body = b"\x00" * (pdfimport.PAGE_CONTENT_LIMIT + 100)
        pdf = xobject_page_pdf(body, b"/Image")
        page = pdfimport.PdfReader(io.BytesIO(pdf)).pages[0]
        self.assertLess(pdfimport._page_content_bytes(page), 100)

    def test_repeated_form_fan_out_is_bounded_by_the_operation_budget(self):
        pdf = form_fan_out_pdf()
        self.assertLess(len(pdf), 10_000)
        # the substitution must reach the extraction, so it runs inline
        with mock.patch.object(pdf_sandbox, "IN_PROCESS", True), \
                mock.patch.object(pdf_sandbox, "MAX_OPERATIONS", 50_000):
            t0 = time.perf_counter()
            with self.assertRaises(ValueError) as cm:
                pdfimport.extract_text(pdf)
            self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertIn("size limit", str(cm.exception))

    def test_repeated_form_fan_out_stops_at_the_deadline(self):
        with mock.patch.object(pdf_sandbox, "IN_PROCESS", True), \
                mock.patch.object(pdf_sandbox, "EXTRACT_SECONDS", 0.5):
            t0 = time.perf_counter()
            with self.assertRaises(ValueError) as cm:
                pdfimport.extract_text(form_fan_out_pdf(leaf_ops=2_000))
            self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertIn("size limit", str(cm.exception))

    def test_compressed_statement_still_parses(self):
        lines = CHECKING_TEXT.splitlines()
        body = [b"BT /F1 10 Tf 36 756 Td"]
        for i, ln in enumerate(lines):
            if i:
                body.append(b"0 -12 Td")
            body.append(b"(%s) Tj" % ln.replace("(", r"\(").replace(
                ")", r"\)").encode("latin-1"))
        body.append(b"ET")
        text = pdfimport.extract_text(flate_page_pdf(b"\n".join(body)))
        r = pdfimport.parse_statement(text, kind="bank")
        self.assertEqual(len(r["rows"]), 5)

    def test_pypdf_decoders_are_capped_at_import(self):
        import pypdf.filters
        self.assertLessEqual(pypdf.filters.ZLIB_MAX_OUTPUT_LENGTH,
                             pdfimport.STREAM_LIMIT)
        self.assertLessEqual(pypdf.filters.MAX_DECLARED_STREAM_LENGTH,
                             pdfimport.STREAM_LIMIT)

    def test_every_decoder_limit_exists_and_is_lowered(self):
        import pypdf.filters
        for name in pdfimport.DECODER_LIMITS:
            with self.subTest(name=name):
                self.assertTrue(hasattr(pypdf.filters, name))
                self.assertLessEqual(getattr(pypdf.filters, name),
                                     pdfimport.STREAM_LIMIT)

    def test_declared_pypdf_floor_has_the_limits_the_importer_sets(self):
        # pdfimport imports LimitReachedError and lowers the decoder
        # limits above; both first ship together in pypdf 6.11, so a lower
        # declared floor lets an install keep a pypdf the server cannot
        # import (or one that leaves decoders uncapped).
        import pathlib
        import re as re_
        pyproject = (pathlib.Path(__file__).resolve().parents[1]
                     / "pyproject.toml").read_text()
        m = re_.search(r'"pypdf>=([0-9.]+)"', pyproject)
        self.assertIsNotNone(m)
        floor = tuple(int(x) for x in m.group(1).split("."))
        self.assertGreaterEqual(floor + (0,) * (3 - len(floor)), (6, 11, 0))


# ---- end-to-end import (DB) -------------------------------------------------


class PageBudgetTests(unittest.TestCase):
    """Measuring a page must cost no more than the budget it enforces, and
    each consumer is measured for everything IT decodes: text extraction
    for content, forms and font maps, the rasterizer for all of that plus
    images, appearance streams, patterns, shadings, soft masks, glyph
    procedures and embedded font programs."""

    def _refused_within(self, fn, pdf, peak_mb=12.0, seconds=3.0):
        # inline: what is measured is the budgets' own cost, which a
        # child process would hide from tracemalloc
        with mock.patch.object(pdf_sandbox, "IN_PROCESS", True):
            self._refused_within_inline(fn, pdf, peak_mb, seconds)

    def _refused_within_inline(self, fn, pdf, peak_mb, seconds):
        tracemalloc.start()
        try:
            t0 = time.perf_counter()
            with self.assertRaises(ValueError) as cm:
                fn(pdf)
            elapsed = time.perf_counter() - t0
            peak = tracemalloc.get_traced_memory()[1] / 1e6
        finally:
            tracemalloc.stop()
        self.assertLess(elapsed, seconds)
        self.assertLess(peak, peak_mb)
        self.assertIn("size limit", str(cm.exception))

    def test_measuring_many_inflating_forms_holds_one_at_a_time(self):
        pdf = many_inflating_forms_pdf(200)
        self.assertLess(len(pdf), 100_000)
        for fn in (pdfimport.extract_text, pdfimport.first_page_within_budget):
            with self.subTest(fn=fn.__name__):
                self._refused_within(fn, pdf)

    def test_tounicode_maps_count_toward_the_text_budget(self):
        fonts = b" ".join(
            b"/U%d << /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            b"/ToUnicode %d 0 R >>" % (i, 6 + i) for i in range(10))
        pdf = page_pdf(fonts=b"/F1 4 0 R " + fonts, objs=tuple(
            double_flate_stream(3_900_000) for _ in range(10)))
        self._refused_within(pdfimport.extract_text, pdf, peak_mb=20)

    def test_font_maps_are_capped_across_the_document(self):
        # Each page's six maps fit its own allowance, but pypdf keeps what
        # it decodes for the whole document: forty such pages would hold
        # most of a gigabyte. The document total refuses it early.
        pdf = multi_page_pdf(40, fonts_per_page=6)
        self.assertLess(len(pdf), 100_000)
        extracted = []
        real = pdf_sandbox.PdfReader

        def reader(*a, **kw):
            r = real(*a, **kw)
            for page in r.pages:
                orig = page.extract_text

                def counted(*a, _orig=orig, **kw):
                    extracted.append(1)
                    return _orig(*a, **kw)
                page.extract_text = counted
            return r
        with mock.patch.object(pdf_sandbox, "IN_PROCESS", True), \
                mock.patch.object(pdf_sandbox, "PdfReader", reader):
            self._refused_within(pdfimport.extract_text, pdf, peak_mb=100)
        self.assertLess(len(extracted), 40)

    def test_decoded_font_maps_do_not_stay_resident_between_pages(self):
        # Under the document cap, what one page decoded must not still be
        # held while the next is read.
        pdf = multi_page_pdf(10, fonts_per_page=6)
        tracemalloc.start()
        try:
            with mock.patch.object(pdf_sandbox, "IN_PROCESS", True), \
                    mock.patch.object(pdf_sandbox, "DOC_DATA_LIMIT", 10**12):
                text = pdfimport.extract_text(pdf)
            peak = tracemalloc.get_traced_memory()[1] / 1e6
        finally:
            tracemalloc.stop()
        self.assertEqual(text.count("CHECK 1024"), 10)
        self.assertLess(peak, 60)

    def test_a_long_statement_sharing_one_font_map_still_parses(self):
        # A map every page shares is decoded once and counted once.
        text = pdfimport.extract_text(
            multi_page_pdf(60, shared_map=2_000_000))
        self.assertEqual(text.count("CHECK 1024"), 60)

    def test_fonts_per_page_are_capped_for_text_extraction(self):
        fonts = b" ".join(
            b"/G%d << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
            % i for i in range(pdfimport.MAX_FONTS_PER_PAGE + 1))
        with self.assertRaises(ValueError) as cm:
            pdfimport.extract_text(page_pdf(fonts=b"/F1 4 0 R " + fonts))
        self.assertIn("size limit", str(cm.exception))
        self.assertIn("Total 12.00", pdfimport.extract_text(page_pdf()))

    def test_an_image_declaring_a_huge_canvas_is_refused_for_rasterizing(self):
        pdf = image_page_pdf(30_000, 30_000, image_stream(
            30_000, 30_000, b"\x80" * 3))
        self.assertLess(len(pdf), 2_000)
        self._refused_within(pdfimport.first_page_within_budget, pdf)
        # text extraction never decodes an image, so it reads the page
        self.assertIn("Total 12.00", pdfimport.extract_text(pdf))

    def test_image_data_inflating_past_its_dimensions_is_refused(self):
        pdf = image_page_pdf(10, 10, image_stream(
            10, 10, b"\0" * 3_900_000, double=True))
        self._refused_within(pdfimport.first_page_within_budget, pdf)

    def test_a_large_honest_scan_is_not_refused(self):
        # 2500x1500 RGB inflates past the per-stream decoder cap, but a
        # single filter is decoded row by row into the declared image
        w, h = 2_500, 1_500
        pdf = image_page_pdf(w, h, image_stream(w, h, b"\xf0" * (w * h * 3)))
        pdfimport.first_page_within_budget(pdf)

    def test_every_stream_the_rasterizer_draws_counts(self):
        big = 2_000_000          # over the content budget, under the cap
        form = b"/Type /XObject /Subtype /Form /BBox [0 0 1 1] "
        placements = {
            "annotation appearance": dict(
                page_extra=b"/Annots [7 0 R]",
                objs=(double_flate_stream(big, form),
                      b"<< /Type /Annot /Subtype /Stamp /Rect [0 0 9 9] "
                      b"/AP << /N 6 0 R >> >>")),
            "tiling pattern": dict(
                resources=b"/Pattern << /P0 6 0 R >>",
                objs=(double_flate_stream(big, (
                    b"/PatternType 1 /PaintType 1 /TilingType 1 "
                    b"/BBox [0 0 1 1] /XStep 1 /YStep 1 ")),)),
            "type 3 glyph procedure": dict(
                fonts=(b"/F1 4 0 R /T3 << /Type /Font /Subtype /Type3 "
                       b"/FontBBox [0 0 1 1] /FontMatrix [1 0 0 1 0 0] "
                       b"/CharProcs << /a 6 0 R >> /Encoding << "
                       b"/Differences [97 /a] >> /FirstChar 97 "
                       b"/LastChar 97 /Widths [1] >>"),
                objs=(double_flate_stream(big),)),
            "soft mask group": dict(
                resources=(b"/ExtGState << /GS0 << /SMask << /S /Luminosity "
                           b"/G 6 0 R >> >> >>"),
                objs=(double_flate_stream(big, form),)),
            "shadings": dict(
                resources=b"/Shading << " + b" ".join(
                    b"/Sh%d %d 0 R" % (i, 6 + i) for i in range(7)) + b" >>",
                objs=tuple(double_flate_stream(3_900_000, (
                    b"/ShadingType 4 /ColorSpace /DeviceRGB "
                    b"/BitsPerCoordinate 8 /BitsPerComponent 8 "
                    b"/BitsPerFlag 8 /Decode [0 1 0 1 0 1 0 1 0 1] "))
                    for _ in range(7))),
            "embedded font programs": dict(
                fonts=b"/F1 4 0 R " + b" ".join(
                    b"/G%d << /Type /Font /Subtype /TrueType /BaseFont /X "
                    b"/FontDescriptor << /Type /FontDescriptor /FontName /X "
                    b"/Flags 32 /FontFile2 %d 0 R >> >>" % (i, 6 + i)
                    for i in range(7)),
                objs=tuple(double_flate_stream(3_900_000)
                           for _ in range(7))),
        }
        for name, parts in placements.items():
            with self.subTest(placement=name):
                pdf = page_pdf(**parts)
                self._refused_within(pdfimport.first_page_within_budget, pdf)
                # none of these is parsed by text extraction
                self.assertIn("Total 12.00", pdfimport.extract_text(pdf))


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

    def test_reimport_corrects_an_investment_row_signed_the_old_way(self):
        """A buy printed with parentheses was once stored as money in. The
        row id hashes the signed amount, so re-importing that statement must
        correct the stored row, not add the money-out twin beside it."""
        from oikonome.sync.base import FileImportIds
        self.conn.execute(
            "INSERT INTO accounts (id, name, type, subtype) "
            "VALUES ('brk', 'Brokerage', 'investment', 'brokerage')")
        pdf = make_pdf("Statement Period: 03/01/2026 through 03/31/2026\n"
                       "03/10 YOU BOUGHT XMPLA (1,000.00)\n")
        pdfimport.import_pdf(self.conn, "brk", pdf, amount_sign="investment")
        # rewrite it as the earlier sign rule stored it: money in, keyed on
        # the negative amount
        old_id = FileImportIds("pdf", "brk").id_for(
            dt.date(2026, 3, 10), -1000.00, "YOU BOUGHT XMPLA")
        self.conn.execute(
            "UPDATE transactions SET id=%s, amount=-1000.00 "
            "WHERE account_id='brk' AND id LIKE 'pdf:%%'", (old_id,))
        pdfimport.import_pdf(self.conn, "brk", pdf, amount_sign="investment")
        rows = self.conn.execute(
            "SELECT amount FROM transactions WHERE account_id='brk'"
        ).fetchall()
        self.assertEqual([float(r["amount"]) for r in rows], [1000.00])

    def test_same_january_line_in_consecutive_years_stays_two_rows(self):
        """Two Dec->Jan statements a year apart can print the very same
        January line. They are two real purchases; importing the second
        must add its own row, never re-date or replace the first year's."""
        for year in (25, 26):
            pdf = make_pdf(f"Opening Date 12/05/{year - 1}\n"
                           f"Closing Date 01/04/{year}\n"
                           "01/02 COFFEE PLACE 5.25\n")
            pdfimport.import_pdf(self.conn, "card", pdf, amount_sign="card")
        rows = sorted((r["date"], float(r["amount"])) for r in
                      self.conn.execute(
                          "SELECT date, amount FROM transactions "
                          "WHERE account_id='card'"))
        self.assertEqual(rows, [(dt.date(2025, 1, 2), 5.25),
                                (dt.date(2026, 1, 2), 5.25)])

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
    """Statement lines whose money sign is easy to get wrong — each one a
    silent corruption of the ledger's direction if it is."""

    def test_charge_at_payment_named_merchant_stays_a_charge(self):
        r = pdfimport.parse_statement(
            "Statement Period 01/01/2026 - 01/31/2026\n"
            "01/06 EXAMPLE PAYMENT CORP UTILITY 120.00\n"
            "01/07 CREDIT CHECK CO 9.99\n"
            "01/08 ONLINE PAYMENT - THANK YOU 250.00\n",
            kind="card")
        rows = {row["name"]: row for row in r["rows"]}
        self.assertEqual(rows["EXAMPLE PAYMENT CORP UTILITY"]["amount"], 120.0)
        self.assertIsNone(rows["EXAMPLE PAYMENT CORP UTILITY"]["flow"])
        self.assertEqual(rows["CREDIT CHECK CO"]["amount"], 9.99)
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


# ---- signs from the statement's own arithmetic and headings ----------------


def _signs(text, kind="bank", hints=None):
    r = pdfimport.parse_statement(text, kind=kind, hints=hints)
    return [(row["name"], row["amount"]) for row in r["rows"]], r


def _wording_warned(r):
    return any("read from the wording" in w for w in r["warnings"])


class RunningBalanceSignTests(unittest.TestCase):
    """A statement that prints a running balance says which way every
    amount went: only one assignment of in/out makes the balances add up.
    That arithmetic decides an unsigned line before any keyword does."""

    def test_running_balance_signs_an_unsigned_cash_back_debit(self):
        rows, r = _signs(
            "Statement Period: 09/01/2026 - 09/30/2026\n"
            "Beginning Balance 1,000.00\n"
            "09/02 POS PURCHASE W/CASH BACK CORNER MARKET 60.00 940.00\n"
            "09/03 DEBIT CARD CASHBACK HILLTOP GROCER 40.00 900.00\n"
            "09/04 Cash Back Rewards 6.00 906.00\n")
        self.assertEqual(rows, [
            ("POS PURCHASE W/CASH BACK CORNER MARKET", 60.00),
            ("DEBIT CARD CASHBACK HILLTOP GROCER", 40.00),
            ("Cash Back Rewards", -6.00)])
        self.assertFalse(_wording_warned(r))

    def test_a_daily_balance_printed_once_signs_every_line_of_that_day(self):
        rows, _ = _signs(
            "Statement Period: 09/01/2026 - 09/30/2026\n"
            "Beginning balance on 9/1 $1,000.00\n"
            "09/02 POS PURCHASE W/CASH BACK CORNER MARKET 60.00\n"
            "09/02 Online Transfer From Savings 100.00 1,040.00\n")
        self.assertEqual(rows, [
            ("POS PURCHASE W/CASH BACK CORNER MARKET", 60.00),
            ("Online Transfer From Savings", -100.00)])

    def test_newest_first_statement_reads_its_balances_backwards(self):
        rows, _ = _signs(
            "Statement Period: 09/01/2026 - 09/30/2026\n"
            "09/04 Cash Back Rewards 6.00 906.00\n"
            "09/03 DEBIT CARD CASHBACK HILLTOP GROCER 40.00 900.00\n"
            "09/02 POS PURCHASE W/CASH BACK CORNER MARKET 60.00 940.00\n"
            "Beginning Balance 1,000.00\n")
        self.assertEqual(rows, [
            ("Cash Back Rewards", -6.00),
            ("DEBIT CARD CASHBACK HILLTOP GROCER", 40.00),
            ("POS PURCHASE W/CASH BACK CORNER MARKET", 60.00)])

    def test_an_ambiguous_balance_change_gives_no_sign(self):
        """In 50 and out 50, or out 50 and in 50, both end where they
        started: the balance says nothing, and the wording decides."""
        rows, r = _signs(
            "Statement Period: 09/01/2026 - 09/30/2026\n"
            "Beginning Balance 1,000.00\n"
            "09/05 ZELLE FROM A FRIEND 50.00\n"
            "09/05 CORNER MARKET 50.00 1,000.00\n")
        self.assertEqual(rows, [("ZELLE FROM A FRIEND", -50.00),
                                ("CORNER MARKET", 50.00)])
        self.assertTrue(_wording_warned(r))

    def test_a_printed_sign_beats_the_running_balance(self):
        rows, _ = _signs(
            "Statement Period: 09/01/2026 - 09/30/2026\n"
            "Beginning Balance 1,000.00\n"
            "09/02 CORNER MARKET -60.00 1,060.00\n")
        self.assertEqual(rows, [("CORNER MARKET", 60.00)])

    def test_quantity_and_price_columns_never_solve_a_balance(self):
        rows, r = _signs(
            "Statement Period: 03/01/2026 through 03/31/2026\n"
            "Beginning Balance 1,000.00\n"
            "03/10 BUY XMPLA 10.000 50.00 500.00\n", kind="investment")
        self.assertEqual(rows, [("BUY XMPLA 10.000", 50.00)])
        self.assertTrue(_wording_warned(r))

    def test_the_running_balance_outranks_a_column_that_disagrees(self):
        text = ("Statement Period: 09/01/2026 - 09/30/2026\n"
                "Beginning Balance 1,000.00\n"
                "09/02 CORNER MARKET 60.00 940.00\n")
        rows, r = _signs(text, hints=[None, None, "in"])
        self.assertEqual(rows, [("CORNER MARKET", 60.00)])
        self.assertTrue(any("running balance and the column disagreed" in w
                            for w in r["warnings"]), r["warnings"])

    def test_a_line_on_a_page_the_layout_budget_skipped_takes_the_balance(self):
        text = ("Statement Period: 09/01/2026 - 09/30/2026\n"
                "Beginning Balance 1,000.00\n"
                "09/02 REFUND PROCESSING CO 60.00 940.00\n")
        r = pdfimport.parse_statement(
            text, kind="bank", hints=[pdfimport.UNKNOWN_SIGN] * 3)
        [row] = r["rows"]
        self.assertEqual(row["amount"], 60.00)
        self.assertFalse(row["sign_unknown"])

    def test_a_card_balance_is_never_read_as_a_sign(self):
        """On a card the balance is what is owed, so it rises with a
        charge: read the bank way it would turn every charge into a
        credit."""
        rows, _ = _signs(
            "Statement Period: 12/01/2026 - 12/31/2026\n"
            "Previous Balance 500.00\n"
            "12/15 COFFEE ROASTERS 10.00 510.00\n", kind="card")
        self.assertEqual(rows, [("COFFEE ROASTERS", 10.00)])

    def test_a_line_read_by_its_wording_alone_is_flagged(self):
        rows, r = _signs("Statement Period: 06/01/2026 - 06/30/2026\n"
                         "06/04 HARDWARE STORE 30.00\n")
        self.assertEqual(rows, [("HARDWARE STORE", 30.00)])
        self.assertTrue(_wording_warned(r))


class SectionHeadingSignTests(unittest.TestCase):
    """A statement that groups its lines under a Withdrawals or Deposits
    heading says which way each line under it went."""

    def test_section_heading_signs_lines_under_it(self):
        rows, _ = _signs(
            "Statement Period: 09/01/2026 - 09/30/2026\n"
            "ATM & DEBIT CARD WITHDRAWALS\n"
            "09/04 CASH BACK VALLEY MART #1234 20.00\n"
            "09/04 REFUND PROCESSING CO 25.00\n"
            "DEPOSITS AND ADDITIONS (continued)\n"
            "09/05 CASH BACK REWARD CREDIT 7.00\n"
            "09/06 CORNER MARKET 9.00\n")
        self.assertEqual(rows, [("CASH BACK VALLEY MART #1234", 20.00),
                                ("REFUND PROCESSING CO", 25.00),
                                ("CASH BACK REWARD CREDIT", -7.00),
                                ("CORNER MARKET", -9.00)])

    def test_a_summary_line_naming_a_section_is_not_a_heading(self):
        rows, _ = _signs(
            "Statement Period: 09/01/2026 - 09/30/2026\n"
            "Deposits and Additions 3 $5,000.00\n"
            "09/06 CORNER MARKET 9.00\n")
        self.assertEqual(rows, [("CORNER MARKET", 9.00)])

    def test_a_total_line_ends_the_section(self):
        rows, _ = _signs(
            "Statement Period: 09/01/2026 - 09/30/2026\n"
            "DEPOSITS AND ADDITIONS\n"
            "09/05 CORNER MARKET 7.00\n"
            "Total Deposits and Additions $7.00\n"
            "09/06 HILLTOP GROCER 9.00\n")
        self.assertEqual(rows, [("CORNER MARKET", -7.00),
                                ("HILLTOP GROCER", 9.00)])

    def test_a_heading_naming_both_directions_ends_the_section(self):
        rows, _ = _signs(
            "Statement Period: 09/01/2026 - 09/30/2026\n"
            "Deposits\n"
            "09/05 CORNER MARKET 7.00\n"
            "Deposits and Withdrawals\n"
            "09/06 HILLTOP GROCER 9.00\n")
        self.assertEqual(rows, [("CORNER MARKET", -7.00),
                                ("HILLTOP GROCER", 9.00)])

    def test_distributions_heading_never_signs_an_income_line(self):
        rows, _ = _signs(
            "Statement Period: 03/01/2026 through 03/31/2026\n"
            "Withdrawals and Distributions\n"
            "03/05 QUALIFIED DIVIDEND XMPLA 9.00\n"
            "03/06 ANNUAL DISTRIBUTION 200.00\n", kind="investment")
        self.assertEqual(rows, [("QUALIFIED DIVIDEND XMPLA", -9.00),
                                ("ANNUAL DISTRIBUTION", 200.00)])


class WordingLastResortSignTests(unittest.TestCase):
    """With no sign, column, balance or heading, the wording decides — and
    the wording of cash taken at a register, or of a retirement account
    paying out, is money out."""

    def test_unsigned_cash_back_without_reward_wording_is_money_out(self):
        rows, _ = _signs(
            "Statement Period: 09/01/2026 - 09/30/2026\n"
            "09/04 CASH BACK VALLEY MART #1234 20.00\n"
            "09/05 POS DEBIT W/ CASHBACK QUICK STOP 30.00\n"
            "09/06 POS PURCHASE W/CASH BACK CORNER MARKET 60.00\n"
            "09/07 CASH BACK REWARD 5.00\n")
        self.assertEqual(rows, [
            ("CASH BACK VALLEY MART #1234", 20.00),
            ("POS DEBIT W/ CASHBACK QUICK STOP", 30.00),
            ("POS PURCHASE W/CASH BACK CORNER MARKET", 60.00),
            ("CASH BACK REWARD", -5.00)])

    def test_retirement_distributions_without_a_column_are_money_out(self):
        rows, _ = _signs(
            "Statement Period: 03/01/2026 through 03/31/2026\n"
            "03/07 RMD DISTRIBUTION FEDERAL W/H 2,000.00\n"
            "03/08 NORMAL DISTRIBUTION 500.00\n"
            "03/09 ROTH IRA DISTRIBUTION 250.00\n"
            "03/10 SEP IRA DISTRIBUTION 3,000.00\n",
            kind="investment")
        self.assertEqual([a for _, a in rows],
                         [2000.00, 500.00, 250.00, 3000.00])

    def test_income_distributions_stay_money_in(self):
        rows, _ = _signs(
            "Statement Period: 03/01/2026 through 03/31/2026\n"
            "03/07 QUALIFIED DIVIDEND DISTRIBUTION 9.00\n"
            "03/08 LT CAP GAIN DISTRIBUTION PAID 100.00\n"
            "03/09 REINVEST DIVIDEND XMPLA 50.00\n", kind="investment")
        self.assertEqual([a for _, a in rows], [-9.00, -100.00, -50.00])

    def test_cash_balance_decides_a_distribution_whatever_its_words(self):
        rows, _ = _signs(
            "Statement Period: 03/01/2026 through 03/31/2026\n"
            "Beginning Balance 5,000.00\n"
            "03/05 IRA DISTRIBUTION 1,000.00 4,000.00\n"
            "03/06 IRA DISTRIBUTION 1,000.00 5,000.00\n", kind="investment")
        self.assertEqual([a for _, a in rows], [1000.00, -1000.00])

    def test_the_distribution_out_words_stay_linear(self):
        line = "03/05 " + "IRA " * 240 + "DISTRIBUTION 10.00"
        start = time.perf_counter()
        pdfimport.parse_statement(
            "Statement Period: 03/01/2026 through 03/31/2026\n" + line,
            kind="investment")
        self.assertLess(time.perf_counter() - start, 0.5)


# ---- re-import after a reading changed --------------------------------------


def _pdf_id(acct, date, amount, name):
    from oikonome.sync.base import FileImportIds
    return FileImportIds("pdf", acct).id_for(date, amount, name)


class _ReimportDb(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _restore_old(self, acct, new_id, old_id, amount, date=None):
        """Rewrite a row as an earlier reading stored it."""
        self.conn.execute(
            "UPDATE transactions SET id=%s, amount=%s,"
            " date=COALESCE(%s, date) WHERE id=%s",
            (old_id, amount, date, new_id))

    def _copy_old(self, acct, new_id, old_id, amount, date=None):
        """An earlier reading's row standing beside the corrected one."""
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name,"
            " merchant_name, removed, raw)"
            " SELECT %s, account_id, COALESCE(%s, date), %s, name,"
            "        merchant_name, 0, raw FROM transactions WHERE id=%s",
            (old_id, date, amount, new_id))

    def _note(self, txn_id, note):
        self.conn.execute(
            "INSERT INTO transaction_notes (txn_id, note) VALUES (%s,%s)",
            (txn_id, note))

    def _notes(self):
        return {r["txn_id"]: r["note"] for r in self.conn.execute(
            "SELECT txn_id, note FROM transaction_notes")}

    def _live(self, acct):
        return sorted((r["name"], r["date"], float(r["amount"]), r["id"])
                      for r in self.conn.execute(
                          "SELECT id, name, date, amount FROM transactions"
                          " WHERE account_id=%s AND removed=0", (acct,)))


class ReimportAfterSignRuleTests(_ReimportDb):
    """A statement imported while a line's sign was read another way
    holds that row under an id hashed from the other sign. Importing it
    again corrects the row in place — one row, the current sign, what a
    person put on it carried — and frees the old id."""

    BALANCED = ("Statement Period: 09/01/2026 - 09/30/2026\n"
                "Beginning Balance 1,000.00\n"
                "09/02 POS PURCHASE W/CASH BACK CORNER MARKET 60.00 940.00\n"
                "09/03 DEBIT CARD CASHBACK HILLTOP GROCER 40.00 900.00\n"
                "09/04 Cash Back Rewards 6.00 906.00\n")

    def test_reimport_after_balance_signs_corrects_rows_in_place(self):
        pdf = make_pdf(self.BALANCED)
        pdfimport.import_pdf(self.conn, "chk", pdf)
        lines = (("POS PURCHASE W/CASH BACK CORNER MARKET", 2, 60.00),
                 ("DEBIT CARD CASHBACK HILLTOP GROCER", 3, 40.00))
        for name, day, amt in lines:
            d = dt.date(2026, 9, day)
            old = _pdf_id("chk", d, -amt, name)
            self._restore_old("chk", _pdf_id("chk", d, amt, name), old, -amt)
            self._note(old, name.lower())
        r = pdfimport.import_pdf(self.conn, "chk", pdf)
        self.assertEqual(r["corrected"], 2)
        live = self._live("chk")
        self.assertEqual([(n, a) for n, _, a, _ in live], [
            ("Cash Back Rewards", -6.00),
            ("DEBIT CARD CASHBACK HILLTOP GROCER", 40.00),
            ("POS PURCHASE W/CASH BACK CORNER MARKET", 60.00)])
        notes = self._notes()
        for name, day, amt in lines:
            new = _pdf_id("chk", dt.date(2026, 9, day), amt, name)
            self.assertEqual(notes.get(new), name.lower())
        # and a third import changes nothing
        pdfimport.import_pdf(self.conn, "chk", pdf)
        self.assertEqual(self._live("chk"), live)

    def test_reimport_corrects_a_printed_minus_line_stored_as_money_in(self):
        """A bank line printed with a minus was once read by its wording,
        and a deposit reversal's wording says money in."""
        pdf = make_pdf("Statement Period: 09/01/2026 - 09/30/2026\n"
                       "09/14 DEPOSIT REVERSAL -500.00\n")
        pdfimport.import_pdf(self.conn, "chk", pdf)
        d = dt.date(2026, 9, 14)
        self._restore_old("chk", _pdf_id("chk", d, 500.00, "DEPOSIT REVERSAL"),
                          _pdf_id("chk", d, -500.00, "DEPOSIT REVERSAL"),
                          -500.00)
        pdfimport.import_pdf(self.conn, "chk", pdf)
        self.assertEqual([(n, a) for n, _, a, _ in self._live("chk")],
                         [("DEPOSIT REVERSAL", 500.00)])

    def test_reimport_after_the_cash_back_wording_change_corrects_in_place(self):
        pdf = make_pdf("Statement Period: 06/01/2026 - 06/30/2026\n"
                       "06/05 POS PURCHASE W/CASH BACK CORNER MARKET 50.00\n")
        name = "POS PURCHASE W/CASH BACK CORNER MARKET"
        d = dt.date(2026, 6, 5)
        pdfimport.import_pdf(self.conn, "chk", pdf)
        self._restore_old("chk", _pdf_id("chk", d, 50.00, name),
                          _pdf_id("chk", d, -50.00, name), -50.00)
        pdfimport.import_pdf(self.conn, "chk", pdf)
        self.assertEqual([(n, a, i) for n, _, a, i in self._live("chk")],
                         [(name, 50.00, _pdf_id("chk", d, 50.00, name))])

    def test_reimport_corrects_an_ira_distribution_stored_as_money_in(self):
        self.conn.execute(
            "INSERT INTO accounts (id, name, type, subtype) "
            "VALUES ('ira', 'Retirement', 'investment', 'ira')")
        pdf = make_pdf("Statement Period: 03/01/2026 through 03/31/2026\n"
                       "03/05 IRA DISTRIBUTION 1,000.00\n")
        d = dt.date(2026, 3, 5)
        pdfimport.import_pdf(self.conn, "ira", pdf, amount_sign="investment")
        self._restore_old("ira", _pdf_id("ira", d, 1000.00, "IRA DISTRIBUTION"),
                          _pdf_id("ira", d, -1000.00, "IRA DISTRIBUTION"),
                          -1000.00)
        pdfimport.import_pdf(self.conn, "ira", pdf, amount_sign="investment")
        self.assertEqual([(n, a) for n, _, a, _ in self._live("ira")],
                         [("IRA DISTRIBUTION", 1000.00)])

    def test_reimport_of_a_partly_column_signed_file_never_twins(self):
        """Page one prints a column header and was signed by it; page two
        prints none, was signed by its wording, and its running balance
        now says otherwise. One row per line either way."""
        pdf = make_layout_pdf([
            [PERIOD, HEADER,
             (720, [(36, "06/03"), (100, "CHECK 1024"), (380, "500.00"),
                    (540, "1,500.00")])],
            [(740, [(36, "06/20"),
                    (100, "POS PURCHASE W/CASH BACK CORNER MARKET"),
                    (380, "50.00"), (540, "1,450.00")])],
        ])
        name = "POS PURCHASE W/CASH BACK CORNER MARKET"
        d = dt.date(2026, 6, 20)
        pdfimport.import_pdf(self.conn, "chk", pdf)
        self._restore_old("chk", _pdf_id("chk", d, 50.00, name),
                          _pdf_id("chk", d, -50.00, name), -50.00)
        pdfimport.import_pdf(self.conn, "chk", pdf)
        self.assertEqual(sorted((n, a) for n, _, a, _ in self._live("chk")),
                         [("CHECK 1024", 500.00), (name, 50.00)])

    def test_corrected_row_frees_its_old_id_for_a_real_line(self):
        """Kept on the corrected row, the old id would be the id of a real
        credit of the same amount the same day, and a later file holding
        that credit would overwrite the corrected row."""
        name = "POS PURCHASE W/CASH BACK CORNER MARKET"
        d = dt.date(2026, 6, 5)
        pdf = make_pdf("Statement Period: 06/01/2026 - 06/30/2026\n"
                       f"06/05 {name} 50.00\n")
        pdfimport.import_pdf(self.conn, "chk", pdf)
        self._restore_old("chk", _pdf_id("chk", d, 50.00, name),
                          _pdf_id("chk", d, -50.00, name), -50.00)
        pdfimport.import_pdf(self.conn, "chk", pdf)
        pdfimport.import_pdf(self.conn, "chk", make_pdf(
            "Statement Period: 06/01/2026 - 06/30/2026\n"
            f"06/05 {name} 50.00 CR\n"))
        self.assertEqual(sorted(a for _, _, a, _ in self._live("chk")),
                         [-50.00, 50.00])

    def test_reimport_retires_a_twin_an_earlier_reimport_added(self):
        pdf = make_pdf("Statement Period: 09/01/2026 - 09/30/2026\n"
                       "09/14 DEPOSIT REVERSAL -500.00\n")
        pdfimport.import_pdf(self.conn, "chk", pdf)
        d = dt.date(2026, 9, 14)
        new = _pdf_id("chk", d, 500.00, "DEPOSIT REVERSAL")
        old = _pdf_id("chk", d, -500.00, "DEPOSIT REVERSAL")
        self._copy_old("chk", new, old, -500.00)
        self._note(old, "asked the bank")
        pdfimport.import_pdf(self.conn, "chk", pdf)
        self.assertEqual([(a, i) for _, _, a, i in self._live("chk")],
                         [(500.00, new)])
        gone = self.conn.execute(
            "SELECT removed, retired_at FROM transactions WHERE id=%s",
            (old,)).fetchone()
        self.assertEqual(gone["removed"], 1)
        self.assertIsNotNone(gone["retired_at"])
        self.assertEqual(self._notes().get(new), "asked the bank")


def _dec_jan(year: int, january_only: bool = False) -> bytes:
    """A card statement for the cycle from December of `year - 1` to
    January of `year`, which prints its opening date on its own line."""
    lines = [f"Opening Date 12/05/{(year - 1) % 100:02d}",
             f"Closing Date 01/04/{year % 100:02d}"]
    if not january_only:
        lines.append("12/20 BOOKSHOP DOWNTOWN 20.00")
    lines.append("01/02 COFFEE PLACE 6.00")
    return make_pdf("\n".join(lines) + "\n")


class RedateReimportTests(_ReimportDb):
    """A Dec->Jan statement read while a lone opening date was taken for
    the period end put its January rows a year early. Importing it again
    moves the year-early row to its real date — but only a row that only
    earlier imports of this same statement own, because last year's
    statement prints the very same January line."""

    def _import(self, pdf, name="stmt.pdf"):
        bid = batches.create(self.conn, "pdf", "card", name)
        pdfimport.import_pdf(self.conn, "card", pdf, amount_sign="card",
                             batch_id=bid)
        return bid

    def _year_early(self, *, keep_new_too=False, note=None):
        """This year's statement as the earlier reading stored it."""
        self._import(_dec_jan(2026))
        new = _pdf_id("card", dt.date(2026, 1, 2), 6.00, "COFFEE PLACE")
        old = _pdf_id("card", dt.date(2025, 1, 2), 6.00, "COFFEE PLACE")
        move = self._copy_old if keep_new_too else self._restore_old
        move("card", new, old, 6.00, dt.date(2025, 1, 2))
        if note:
            self._note(old, note)
        return new, old

    def _coffee(self):
        return [(d, a, i) for n, d, a, i in self._live("card")
                if n == "COFFEE PLACE"]

    def test_reimport_redates_a_year_early_january_row_its_own_statement_made(self):
        new, old = self._year_early(note="split with a friend")
        self._import(_dec_jan(2026))
        self.assertEqual(self._coffee(), [(dt.date(2026, 1, 2), 6.00, new)])
        self.assertEqual(self._notes(), {new: "split with a friend"})
        self.assertEqual(len(self._live("card")), 2)

    def test_existing_corrected_twin_retires_the_year_early_copy(self):
        new, old = self._year_early(keep_new_too=True, note="kept")
        self._import(_dec_jan(2026))
        self.assertEqual(self._coffee(), [(dt.date(2026, 1, 2), 6.00, new)])
        gone = self.conn.execute(
            "SELECT removed, retired_at FROM transactions WHERE id=%s",
            (old,)).fetchone()
        self.assertEqual(gone["removed"], 1)
        self.assertIsNotNone(gone["retired_at"])
        self.assertEqual(self._notes().get(new), "kept")

    def test_last_years_january_row_is_never_redated(self):
        self._import(_dec_jan(2025), "last-year.pdf")
        self._import(_dec_jan(2026))
        self._import(_dec_jan(2026))
        self.assertEqual([(d, a) for d, a, _ in self._coffee()],
                         [(dt.date(2025, 1, 2), 6.00),
                          (dt.date(2026, 1, 2), 6.00)])
        self.assertEqual(len(self._live("card")), 4)

    def test_a_year_early_row_shared_with_last_years_statement_stays(self):
        """The earlier reading of this statement merged its January line
        into last year's real row: that row is last year's, so it keeps
        its date, amount and id, and this year's row is added."""
        self._import(_dec_jan(2025), "last-year.pdf")
        shared = _pdf_id("card", dt.date(2025, 1, 2), 6.00, "COFFEE PLACE")
        bid = self._import(_dec_jan(2026))
        new = _pdf_id("card", dt.date(2026, 1, 2), 6.00, "COFFEE PLACE")
        self.conn.execute("DELETE FROM transactions WHERE id=%s", (new,))
        self.conn.execute(
            "UPDATE transactions SET raw = jsonb_set(raw, '{_batches}',"
            " (raw->'_batches') || to_jsonb(%s::text)) WHERE id=%s",
            (bid, shared))
        self._import(_dec_jan(2026))
        self.assertEqual(self._coffee(),
                         [(dt.date(2025, 1, 2), 6.00, shared),
                          (dt.date(2026, 1, 2), 6.00, new)])

    def test_redated_row_frees_its_old_id_for_last_years_statement(self):
        new, old = self._year_early()
        self._import(_dec_jan(2026))
        self._import(_dec_jan(2025), "last-year.pdf")
        self.assertEqual(self._coffee(),
                         [(dt.date(2025, 1, 2), 6.00, old),
                          (dt.date(2026, 1, 2), 6.00, new)])

    def test_statement_with_only_january_lines_adds_rather_than_guesses(self):
        """With no line that kept its id there is no proof the year-early
        row is this statement's and not last year's: it is left alone (an
        Undo of its import still removes it) and the right row is added."""
        self._import(_dec_jan(2026, january_only=True))
        new = _pdf_id("card", dt.date(2026, 1, 2), 6.00, "COFFEE PLACE")
        old = _pdf_id("card", dt.date(2025, 1, 2), 6.00, "COFFEE PLACE")
        self._restore_old("card", new, old, 6.00, dt.date(2025, 1, 2))
        self._import(_dec_jan(2026, january_only=True))
        self.assertEqual(self._coffee(),
                         [(dt.date(2025, 1, 2), 6.00, old),
                          (dt.date(2026, 1, 2), 6.00, new)])
