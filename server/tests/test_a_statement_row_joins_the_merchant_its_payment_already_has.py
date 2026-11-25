"""A payment imported from a bank statement is the payment the feed also
carries, and is filed under the same merchant (specs/merchant-identity.md,
"A statement's lead-in is not the payee's name"): the entry kind a
statement export writes in front of the payee is not part of the name,
and a merchant's totals count the imported months with the feed's.
"""
import datetime as dt
import unittest

from oikonome.engine import merchant_dedup, merchant_identity, reporting
from oikonome.engine.merchant_dedup import canonical_merchant, strip_lead_in

from .util import add_txn, make_db, write_config

FEED = "FABRIKAM TUTORING"
STATEMENT = "ACH Withdrawal FABRIKAM TUTORING"
CATEGORY = "GENERAL_SERVICES"
TODAY = dt.date(2026, 11, 17)


def _unnamed(conn, day, amount, line, txn_id, account="chk"):
    """A row the aggregator never named, as a file import writes it."""
    add_txn(conn, day, amount, line, txn_id=txn_id, account=account,
            primary=CATEGORY)
    conn.execute("UPDATE transactions SET merchant_name = NULL WHERE id = %s",
                 (txn_id,))


class LeadInIsNotTheName(unittest.TestCase):
    def test_the_entry_kind_is_cut_from_the_front(self):
        self.assertEqual(canonical_merchant(STATEMENT),
                         canonical_merchant(FEED))
        self.assertEqual(canonical_merchant("ACH Deposit ACME PAYROLL"),
                         "Acme Payroll")
        self.assertEqual(
            canonical_merchant("Direct Deposit — CONTOSO PAYROLL"),
            "Contoso Payroll")
        self.assertEqual(strip_lead_in("ACH Debit - KITE SHOP"), "KITE SHOP")

    def test_what_is_not_a_lead_in_is_left(self):
        for line in ("ATM Withdrawal - 12 MAIN ST", "Withdrawal at teller",
                     "Achilles Deposit Co", "ACH Withdrawal",
                     "Direct Deposit"):
            self.assertEqual(strip_lead_in(line), line)


class StatementRowsJoinTheFeedsMerchant(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        # the feed's rows: named by the aggregator, the amount in the line
        for n, month in enumerate((7, 8, 9, 10)):
            add_txn(self.conn, dt.date(2026, month, 5), 400.0 + n,
                    FEED, merchant="Fabrikam Tutoring",
                    txn_id=f"feed{n}", account="chk", primary=CATEGORY)
        merchant_dedup.apply(self.conn)
        merchant_identity.resolve(self.conn)

    def tearDown(self):
        self.conn.close()

    def _merchant(self, txn_id):
        return self.conn.execute(
            "SELECT m.name, m.name_source FROM transactions t "
            "JOIN merchants m ON m.id = t.merchant_id WHERE t.id = %s",
            (txn_id,)).fetchone()

    def _import(self):
        for n, month in enumerate((4, 5, 6)):
            _unnamed(self.conn, dt.date(2026, month, 5), 410.0 + n,
                     STATEMENT, f"stmt{n}")

    def _night(self):
        merchant_dedup.apply(self.conn)
        merchant_identity.resolve(self.conn)

    def _state(self):
        return [(r["id"], r["name"]) for r in self.conn.execute(
            "SELECT t.id, m.name FROM transactions t "
            "JOIN merchants m ON m.id = t.merchant_id ORDER BY t.id")]

    def test_an_imported_row_adopts_the_feeds_merchant(self):
        self._import()
        self._night()
        for n in range(3):
            self.assertEqual(self._merchant(f"stmt{n}")["name"], "Fabrikam Tutoring")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM merchants WHERE merged_into IS NULL"
        ).fetchone()["n"], 1)

    def test_rows_filed_before_the_rule_are_brought_along_by_the_recompute(self):
        """The ledger as an older clean left it: the statement's rows under
        a merchant named with the lead-in."""
        self._import()
        ids = [f"stmt{n}" for n in range(3)]
        old = self.conn.execute(
            "INSERT INTO merchants (name, name_source) VALUES "
            "('Withdrawal Fabrikam Tutoring','layer1') RETURNING id"
        ).fetchone()["id"]
        self.conn.execute(
            "UPDATE transactions SET merchant_id = %s WHERE id = ANY(%s)",
            (old, ids))
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method,"
            " merchant_id) SELECT DISTINCT name, 'Withdrawal Fabrikam Tutoring',"
            " 'layer1', %s FROM transactions WHERE id = ANY(%s) "
            "ON CONFLICT (tenant_id, raw_merchant) DO UPDATE SET "
            "canonical = EXCLUDED.canonical, merchant_id = EXCLUDED.merchant_id",
            (old, ids))
        self._night()
        for txn_id in ids:
            self.assertEqual(self._merchant(txn_id)["name"], "Fabrikam Tutoring")
        self.assertEqual(
            [r["name"] for r in self.conn.execute(
                "SELECT name FROM merchants WHERE merged_into IS NULL")],
            ["Fabrikam Tutoring"])

    def test_the_recompute_twice_is_the_recompute_once(self):
        self._import()
        self._night()
        once = self._state()
        merchants = self.conn.execute(
            "SELECT count(*) AS n FROM merchants").fetchone()["n"]
        self._night()
        self.assertEqual(self._state(), once)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM merchants").fetchone()["n"], merchants)

    def test_a_name_a_person_gave_the_statement_rows_survives(self):
        self._import()
        self._night()
        # the person splits the statement's months off under their own name
        self.conn.execute(
            "UPDATE transactions SET merchant_id = NULL WHERE id LIKE 'stmt%'")
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, "
            "method) VALUES (%s,'Spring Lessons','manual') "
            "ON CONFLICT (tenant_id, raw_merchant) DO UPDATE SET "
            "canonical = EXCLUDED.canonical, method = 'manual', "
            "merchant_id = NULL",
            (STATEMENT,))
        self._night()
        self._night()
        for n in range(3):
            got = self._merchant(f"stmt{n}")
            self.assertEqual((got["name"], got["name_source"]),
                             ("Spring Lessons", "manual"))
        # a later statement row of the family they named follows their name
        _unnamed(self.conn, dt.date(2026, 3, 5), 409.0,
                 STATEMENT, "stmt-later")
        self._night()
        self.assertEqual(self._merchant("stmt-later")["name"],
                         "Spring Lessons")
        self.assertEqual(self._merchant("feed0")["name"], "Fabrikam Tutoring")

    def test_two_payees_behind_one_lead_in_stay_apart(self):
        self._import()
        _unnamed(self.conn, dt.date(2026, 6, 9), 60.0,
                 "ACH Withdrawal QUILLO WATER", "water")
        _unnamed(self.conn, dt.date(2026, 6, 11), 45.0,
                 "ACH Withdrawal BRINDLE POWER", "power")
        self._night()
        names = {t: self._merchant(t)["name"]
                 for t in ("stmt0", "water", "power")}
        self.assertEqual(len(set(names.values())), 3, names)
        self.assertEqual(names["stmt0"], "Fabrikam Tutoring")
        for name in (names["water"], names["power"]):
            self.assertNotIn("Withdrawal", name)

    def test_a_cleaned_line_two_merchants_answer_to_adopts_neither(self):
        # the aggregator has called this line two things: it names no payee
        add_txn(self.conn, dt.date(2026, 8, 20), 30.0, "NORTHGATE SERVICES",
                merchant="Northgate Plumbing", txn_id="n1", account="chk")
        add_txn(self.conn, dt.date(2026, 9, 20), 30.0, "NORTHGATE SERVICES",
                merchant="Northgate Tutoring", txn_id="n2", account="chk")
        _unnamed(self.conn, dt.date(2026, 6, 20), 30.0,
                 "ACH Withdrawal NORTHGATE SERVICES", "n-stmt")
        self._night()
        self.assertNotIn(self._merchant("n-stmt")["name"],
                         ("Northgate Plumbing", "Northgate Tutoring"))

    def test_the_merchants_window_total_counts_its_imported_months(self):
        self._import()
        self._night()
        w = reporting.spending_window(self.conn, TODAY, "1y")
        rows = {r[0]: (r[1], r[2]) for r in w["merchants"]}
        # four feed payments and three from the statement, one merchant
        self.assertEqual(rows, {"Fabrikam Tutoring": (
            sum(400.0 + n for n in range(4)) + sum(410.0 + n for n in range(3)),
            7)})


if __name__ == "__main__":
    unittest.main()
