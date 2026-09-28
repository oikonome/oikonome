"""A ledger row has ONE text that bills are matched against.

The month plan reads a fuel-arm row by its outlet ("Northwind Club Fuel");
a page that read the same row by the aggregator's name ("Northwind Club")
would list no charges for a bill the plan shows as paid, and a SQL
pre-filter built on the narrower reading would drop rows the Python matcher
would have kept — a pre-filter is only correct while it admits everything
the matcher accepts. specs/bill-match-text.md has the rule.
"""

import datetime as dt
import pathlib
import re
import unittest

from oikonome.engine import bills, budget
from oikonome.web import data

from .util import add_bill, add_txn, make_db

TODAY = dt.date(2026, 8, 20)
BRAND = "Northwind Club"
PUMP = "Northwind Club Fuel"
BILL = "Northwind Club Fuel"


def _outlet_row(conn, date, amount, line, txn_id):
    add_txn(conn, date, amount, line, merchant=BRAND, txn_id=txn_id,
            primary="TRANSPORTATION")
    conn.execute("UPDATE transactions SET merchant_outlet = %s WHERE id = %s",
                 (PUMP, txn_id))


class OutletRowsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        _outlet_row(self.conn, "2026-08-02", 50.00, "NORTHWIND FUEL", "pump1")
        _outlet_row(self.conn, "2026-07-03", 50.00, "NORTHWIND FUEL", "pump2")
        add_txn(self.conn, "2026-08-04", 200.00, "NORTHWIND CLUB STORE",
                merchant=BRAND, txn_id="whse")
        # a text-matched bill: no merchants attached, nothing pinned
        add_bill(self.conn, BILL, 50, merchant=PUMP.lower(), merchants=[],
                 bill_type="envelope", txn_category="TRANSPORTATION")
        self.conn.execute(
            "UPDATE bills SET raw = raw - 'merchant_refs' - 'merchant_names' "
            "- 'merchant_pinned' - 'merchant_offered'")

    def tearDown(self):
        self.conn.close()

    def _python_matches(self):
        row = self.conn.execute(
            "SELECT merchant FROM bills WHERE payee = %s", (BILL,)).fetchone()
        match = budget.merchant_matcher(row["merchant"], budget._key_token(BILL))
        return {r["id"] for r in self.conn.execute(
            f"SELECT t.id, {budget.MATCH_PAYEE_SQL} AS payee, t.name "
            "FROM transactions t").fetchall()
            if match(budget.match_text(r["payee"], r["name"]))}

    def test_the_bill_is_a_text_bill(self):
        row = self.conn.execute(
            "SELECT payee, raw FROM bills WHERE payee = %s", (BILL,)).fetchone()
        self.assertIsNone(budget.bill_merchant_ids(self.conn, [row])[BILL])

    def test_the_sql_text_and_the_python_text_are_one_string(self):
        for r in self.conn.execute(
                f"SELECT {budget.MATCH_TEXT_SQL} AS sql_text, "
                f"       {budget.MATCH_PAYEE_SQL} AS payee, t.name "
                "FROM transactions t").fetchall():
            self.assertEqual(r["sql_text"],
                             budget.match_text(r["payee"], r["name"]))

    def test_the_pre_filter_admits_every_row_the_matcher_accepts(self):
        row = self.conn.execute(
            "SELECT merchant FROM bills WHERE payee = %s", (BILL,)).fetchone()
        frag, params = budget.merchant_match_sql(row["merchant"],
                                                 budget._key_token(BILL))
        admitted = {r["id"] for r in self.conn.execute(
            f"SELECT t.id FROM transactions t WHERE {frag}", params).fetchall()}
        self.assertEqual(self._python_matches(), {"pump1", "pump2"})
        self.assertLessEqual(self._python_matches(), admitted)

    def test_the_history_page_lists_the_pumps_charges_and_not_the_warehouse(self):
        hist = bills.merchant_history(self.conn, BILL, today=TODAY)
        self.assertEqual({t["txn_id"] for t in hist["txns"]}, {"pump1", "pump2"})
        self.assertEqual(hist["lifetime"]["count"], 2)

    def test_the_bills_category_reaches_the_rows_it_pays(self):
        bills.apply_txn_categories(self.conn)
        got = {r["id"]: r["category_override"] for r in self.conn.execute(
            "SELECT id, category_override FROM transactions").fetchall()}
        self.assertEqual(got["pump1"], "TRANSPORTATION")
        self.assertIsNone(got["whse"])

    def test_detection_reads_the_pump_as_its_own_payee(self):
        rows = bills._ledger_rows(self.conn, TODAY)
        self.assertEqual({r["payee"] for r in rows}, {PUMP, BRAND})


class ExactRawStringsTests(unittest.TestCase):
    """A live display bucket is exactly its own identity keys."""

    def setUp(self):
        self.conn = make_db()
        add_txn(self.conn, "2026-08-02", 12.0, "HELIO CAFE 22", merchant="Helio",
                txn_id="a")
        add_txn(self.conn, "2026-08-03", 9.0, "HELIOTROPE FLORIST",
                merchant="Heliotrope", txn_id="b")

    def tearDown(self):
        self.conn.close()

    def test_the_predicate_selects_the_buckets_rows_and_only_those(self):
        frag, params = data._page_match_sql(self.conn, "Helio")
        self.assertEqual({r["id"] for r in self.conn.execute(
            f"SELECT t.id FROM transactions t WHERE {frag}", params).fetchall()},
            {"a"})


class OneDefinitionTests(unittest.TestCase):
    def test_no_module_spells_the_match_text_itself(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "oikonome"
        pat = re.compile(
            r"COALESCE\(\s*t\.merchant_name\s*,\s*t\.name\s*\)\s+AS\s+(raw_)?payee"
            r"|lower\(\s*COALESCE\(\s*t\.merchant_name\s*,\s*t\.name", re.I)
        offenders = [str(f.relative_to(root)) for f in root.rglob("*.py")
                     if pat.search(f.read_text(encoding="utf-8"))]
        self.assertEqual(offenders, [],
                         f"{offenders} spell a bill's match text themselves — "
                         "select budget.MATCH_PAYEE_SQL / read MATCH_TEXT_SQL")


if __name__ == "__main__":
    unittest.main()
