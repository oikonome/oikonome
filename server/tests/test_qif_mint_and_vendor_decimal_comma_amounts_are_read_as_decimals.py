"""QIF, Mint and the vendor exports read a decimal-comma amount as a decimal.

A file written in a comma locale (a Quicken, GnuCash or HomeBank QIF dated
day-first; a re-saved Mint or Monarch export) spells twelve and a half as
"12,50" and a rent payment as "1.234,56". Dropping every comma stored the
first a hundred times too large and the second a thousand times too small,
with no error, and the wrong amounts then fed every total, budget and
forecast. Each importer reads the cell by the CSV rule instead
(csvimport.parse_money_text); the US spellings ("1,234.56") read as before.
"""
import unittest

from oikonome.sync import competitors, mintimport, qifimport

from .util import make_db, write_config

QIF = """!Type:Bank
D15/01/2024
T-12,50
PSPRINGFIELD BAKERY
^
D16/01/2024
T-1.234,56
PNORTHWIND PROPERTIES
^
D17/01/2024
U1,111.25
PFABRIKAM PAYROLL
^
"""

MINT = ("Date,Description,Original Description,Amount,Transaction Type,"
        "Category,Account Name,Labels,Notes\n"
        '1/15/2024,Bakery,SPRINGFIELD BAKERY,"12,50",debit,Groceries,'
        "Everyday,,\n"
        '1/16/2024,Rent,NORTHWIND PROPERTIES,"1.234,56",debit,Rent,'
        "Everyday,,\n")

MONARCH = ("Date,Merchant,Category,Account,Original Statement,Notes,"
           "Amount,Tags\n"
           '2024-01-15,Bakery,Groceries,Everyday,SPRINGFIELD BAKERY,,'
           '"-12,50",\n'
           '2024-01-16,Rent,Rent,Everyday,NORTHWIND PROPERTIES,,'
           '"-1.234,56",\n')


class DecimalCommaAmountTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def test_a_qif_amount_line_reads_either_separator_convention(self):
        rows = qifimport.parse_qif(QIF)
        self.assertEqual([r["amount"] for r in rows],
                         [-12.50, -1234.56, 1111.25])

    def test_a_mint_amount_cell_reads_a_decimal_comma(self):
        mintimport.import_mint(self.conn, "chk", MINT)
        got = sorted(r["amount"] for r in self.conn.execute(
            "SELECT amount FROM transactions WHERE id LIKE 'mint:%'"))
        self.assertEqual(got, [12.50, 1234.56])

    def test_a_vendor_export_amount_cell_reads_a_decimal_comma(self):
        competitors.import_monarch(self.conn, "chk", MONARCH)
        got = sorted(r["amount"] for r in self.conn.execute(
            "SELECT amount FROM transactions WHERE id LIKE 'monarch:%'"))
        # Monarch writes spending negative; the engine stores it positive
        self.assertEqual(got, [12.50, 1234.56])

    def test_the_vendor_money_reader_keeps_its_other_spellings(self):
        self.assertEqual(competitors._money("$1,234.56"), 1234.56)
        self.assertEqual(competitors._money("(12.50)"), -12.50)
        self.assertEqual(competitors._money("12,50"), 12.50)
        self.assertEqual(competitors._money("1.234,56"), 1234.56)
        self.assertEqual(competitors._money(""), 0.0)


if __name__ == "__main__":
    unittest.main()
