"""A chain's fuel arm is its own merchant.

Costco's pump and Costco's warehouse are one payee to an aggregator and two
different things to a reader: different prices, different rhythm, different
answer to "what does a visit cost". The bank's statement line already tells
them apart ("COSTCO GAS #0007" vs "COSTCO WHSE #0007"), so the split is read
from the text.

What must NOT happen is a split derived from the category. Fuel rows are
routinely miscategorized, and a merchant name that follows the category
would let one bad row rename a payee across the whole ledger.
"""

import unittest
from pathlib import Path

from oikonome.sync.base import Transaction, fuel_arm, upsert_transactions

from .util import jsonb, make_db

_MIGRATIONS = Path(__file__).resolve().parents[1] / "oikonome/db/migrations"
_OUTLET = _MIGRATIONS / "092_merchant_outlet.sql"
_ANY_BRAND = _MIGRATIONS / "093_fuel_arm_any_brand.sql"
_REFILE = _MIGRATIONS / "094_fuel_arm_recategorize.sql"


class FuelArmParser(unittest.TestCase):
    def test_the_descriptor_names_the_brand_then_the_pump(self):
        self.assertEqual(fuel_arm("Costco", "COSTCO GAS #0007"), "Costco Gas")
        self.assertEqual(fuel_arm("Kroger", "KROGER FUEL CTR #2020"),
                         "Kroger Fuel")

    def test_the_store_and_the_website_are_not_the_pump(self):
        self.assertIsNone(fuel_arm("Costco", "COSTCO WHSE #0007"))
        self.assertIsNone(fuel_arm("Costco", "WWW COSTCO COM"))

    def test_a_merchant_already_named_for_fuel_is_left_alone(self):
        self.assertIsNone(fuel_arm("Costco Gas", "COSTCO GAS #0512"))
        self.assertIsNone(fuel_arm("Shell Fuel", "SHELL FUEL 123"))
        # the same guard is what stops a natural-gas utility becoming
        # "Wisconsin Public Gas Gas"
        self.assertIsNone(fuel_arm("Wisconsin Public Gas",
                                   "WISCONSIN PUBLIC GAS BILL PAYMENT"))

    def test_punctuation_in_the_brand_cannot_hide_the_pump(self):
        # the brand and the text are compared on alphanumerics alone, so a
        # hyphenated brand matches whether or not the bank keeps the hyphens
        self.assertEqual(fuel_arm("H-E-B", "H-E-B GAS/CARWASH #4SPRINGFIELD"),
                         "H-E-B Gas")
        self.assertEqual(fuel_arm("H-E-B", "HEB GAS STATION RIVERTON TX"),
                         "H-E-B Gas")
        self.assertEqual(fuel_arm("Lowe's", "LOWES FUEL CENTER"), "Lowe's Fuel")
        # and the store is still the store
        self.assertIsNone(fuel_arm("H-E-B", "H-E-B #512 000000000SPRINGFIELD"))

    def test_a_brand_cannot_carry_regex_syntax_into_the_pattern(self):
        # only the brand's alphanumerics reach the pattern, so this is a
        # non-event rather than a quoting bug waiting to happen
        self.assertIsNone(fuel_arm("A+ (Market)", "SOMETHING ELSE"))
        self.assertEqual(fuel_arm("A+ (Market)", "A MARKET GAS #1"),
                         "A+ (Market) Gas")

    def test_two_letter_brands_are_not_evidence(self):
        # "BP GAS" is a filling station naming itself, not an arm of a shop,
        # and a two-character brand matches far too much to act on
        self.assertIsNone(fuel_arm("BP", "BP GAS 123"))

    def test_the_fuel_word_must_belong_to_this_brand(self):
        # a different brand's pump on the same line names nobody here, and
        # "gas" inside another word is not the pump
        self.assertIsNone(fuel_arm("Safeway", "SHELL GAS #12 SAFEWAY PLAZA"))
        self.assertIsNone(fuel_arm("Vega", "VEGAS BUFFET"))
        # words in between are not "the next token" — and Shell is a filling
        # station already, so there is no arm to split off
        self.assertIsNone(fuel_arm(
            "Shell", "SHELL OIL 100200 SPRINGFIELD REF# 002 AUTO FUEL DISPEN"))

    def test_no_descriptor_means_no_split(self):
        # some feeds carry no statement line at all, which leaves a pump row
        # identifiable only by its category — exactly the signal this rule
        # refuses to use
        self.assertIsNone(fuel_arm("Kroger", None))
        self.assertIsNone(fuel_arm("Kroger", ""))
        self.assertIsNone(fuel_arm(None, "KROGER FUEL"))


class FuelArmOnIngest(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _sync(self, txn_id, merchant, orig):
        upsert_transactions(self.conn, [Transaction(
            id=txn_id, account_id="card", date="2026-08-01", amount=42.00,
            name=merchant, merchant_name=merchant,
            category_primary="GENERAL_MERCHANDISE",
            raw={"original_description": orig})])
        row = self.conn.execute(
            "SELECT merchant_name, merchant_outlet FROM transactions "
            "WHERE id=%s", (txn_id,)).fetchone()
        return row["merchant_outlet"], row["merchant_name"]

    def test_the_pump_and_the_warehouse_become_two_merchants(self):
        # the outlet is recorded BESIDE the source's word, never over it —
        # what Plaid said stays recoverable without a re-sync
        self.assertEqual(self._sync("f1", "Costco", "COSTCO GAS #0007"),
                         ("Costco Gas", "Costco"))
        self.assertEqual(self._sync("f2", "Costco", "COSTCO WHSE #0007"),
                         (None, "Costco"))

    def test_a_resync_re_derives_the_same_outlet(self):
        # pending → posted runs the same row through again; a rule that
        # appended each time would produce "Costco Gas Gas"
        self.assertEqual(self._sync("f3", "Costco", "COSTCO GAS #0007"),
                         ("Costco Gas", "Costco"))
        self.assertEqual(self._sync("f3", "Costco", "COSTCO GAS #0007"),
                         ("Costco Gas", "Costco"))


class FuelArmRefilesOnIngest(unittest.TestCase):
    """A row the bank itself labels as this brand's pump ingests as fuel,
    whatever the aggregator guessed — and category_override, the user's own
    word, is the one column this path may never touch."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _sync(self, txn_id, merchant, orig, category="GENERAL_MERCHANDISE",
              detailed=None):
        upsert_transactions(self.conn, [Transaction(
            id=txn_id, account_id="card", date="2026-08-01", amount=42.00,
            name=merchant, merchant_name=merchant,
            category_primary=category, category_detailed=detailed,
            raw={"original_description": orig})])

    def _row(self, txn_id):
        r = self.conn.execute(
            "SELECT category_primary, category_detailed, category_override, "
            "merchant_outlet FROM transactions WHERE id=%s",
            (txn_id,)).fetchone()
        return (r["category_primary"], r["category_detailed"],
                r["category_override"], r["merchant_outlet"])

    def test_a_proven_pump_ingests_as_fuel_with_its_outlet(self):
        # Plaid files H-E-B's pump under groceries; the statement line is
        # the evidence that refiles it
        self._sync("g1", "H-E-B", "H-E-B GAS/CARWASH #4SPRINGFIELD",
                   category="FOOD_AND_DRINK",
                   detailed="FOOD_AND_DRINK_GROCERIES")
        self.assertEqual(self._row("g1"),
                         ("TRANSPORTATION", "TRANSPORTATION_GAS", None,
                          "H-E-B Gas"))

    def test_the_user_s_category_survives_a_resync(self):
        # pending → posted runs the same row through again; the user's
        # override must come out the other side, and the derived categories
        # must land exactly where they did the first time
        self._sync("g2", "Costco", "COSTCO GAS #0007",
                   category="FOOD_AND_DRINK")
        self.conn.execute("UPDATE transactions SET category_override=%s "
                          "WHERE id='g2'", ("ENTERTAINMENT",))
        self._sync("g2", "Costco", "COSTCO GAS #0007",
                   category="FOOD_AND_DRINK")
        self.assertEqual(self._row("g2"),
                         ("TRANSPORTATION", "TRANSPORTATION_GAS",
                          "ENTERTAINMENT", "Costco Gas"))

    def test_a_store_row_from_the_same_brand_keeps_its_category(self):
        # the warehouse is not the pump — the aggregator's word stands
        self._sync("g3", "Costco", "COSTCO WHSE #0007",
                   category="GENERAL_MERCHANDISE",
                   detailed="GENERAL_MERCHANDISE_SUPERSTORES")
        self.assertEqual(self._row("g3"),
                         ("GENERAL_MERCHANDISE",
                          "GENERAL_MERCHANDISE_SUPERSTORES", None, None))

    def test_a_merchant_already_named_for_fuel_is_not_refiled(self):
        # a natural-gas utility says "gas" in its own name; it gets no
        # outlet and its utility category stays put
        self._sync("g4", "Wisconsin Public Gas",
                   "WISCONSIN PUBLIC GAS BILL PAYMENT",
                   category="RENT_AND_UTILITIES",
                   detailed="RENT_AND_UTILITIES_GAS_AND_ELECTRICITY")
        self.assertEqual(self._row("g4"),
                         ("RENT_AND_UTILITIES",
                          "RENT_AND_UTILITIES_GAS_AND_ELECTRICITY",
                          None, None))


class FuelArmStoredHistory(unittest.TestCase):
    """The migration must reach the same verdict as the Python parser."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _row(self, txn_id, merchant, orig, category="GENERAL_MERCHANDISE"):
        self.conn.execute(
            """INSERT INTO transactions (id, account_id, date, amount, name,
                   merchant_name, category_primary, pending, removed, raw)
               VALUES (%s,'card','2026-08-01',42.00,%s,%s,%s,0,0,%s)""",
            (txn_id, merchant, merchant, category,
             jsonb({"original_description": orig} if orig else {})))

    def _migrate(self):
        # the test database is already migrated, so the column exists and the
        # connection (app role) cannot re-run DDL it does not own — what is
        # under test here is the backfill agreeing with the Python parser
        for f in (_OUTLET, _ANY_BRAND, _REFILE):
            sql = f.read_text()
            self.conn.execute(sql[sql.index("UPDATE"):])

    def _outlet(self, txn_id):
        row = self.conn.execute(
            "SELECT merchant_name, merchant_outlet FROM transactions "
            "WHERE id=%s", (txn_id,)).fetchone()
        return row["merchant_outlet"], row["merchant_name"]

    def test_stored_pump_rows_get_an_outlet_and_nothing_else_moves(self):
        self._row("h1", "Costco", "COSTCO GAS #0007")
        self._row("h2", "Costco", "COSTCO WHSE #0007")
        self._row("h3", "Costco", None)
        # already named for its own pump
        self._row("h4", "Costco Gas", "COSTCO GAS #0512")
        # categorized as fuel, but nothing in the text says so — the case
        # the rule deliberately declines
        self._row("h5", "Kroger", None, category="TRANSPORTATION")
        # a punctuated brand: the pattern compares alphanumerics only
        self._row("h8", "H-E-B", "H-E-B GAS/CARWASH #4SPRINGFIELD")
        self._migrate()
        self.assertEqual(self._outlet("h1"), ("Costco Gas", "Costco"))
        self.assertEqual(self._outlet("h2"), (None, "Costco"))
        self.assertEqual(self._outlet("h3"), (None, "Costco"))
        # already named for its own pump: nothing to add
        self.assertEqual(self._outlet("h4"), (None, "Costco Gas"))
        self.assertEqual(self._outlet("h5"), (None, "Kroger"))
        self.assertEqual(self._outlet("h8"), ("H-E-B Gas", "H-E-B"))

    def test_running_it_twice_changes_nothing_the_second_time(self):
        self._row("h6", "Costco", "COSTCO GAS #0007")
        self._migrate()
        self._migrate()
        self.assertEqual(self._outlet("h6"), ("Costco Gas", "Costco"))

    def _cats(self, txn_id):
        row = self.conn.execute(
            "SELECT category_primary, category_detailed, category_override "
            "FROM transactions WHERE id=%s", (txn_id,)).fetchone()
        return (row["category_primary"], row["category_detailed"],
                row["category_override"])

    def test_a_proven_pump_is_refiled_as_fuel(self):
        # the bank says pump, the aggregator said groceries — a tank of
        # petrol was sitting inside the food budget
        self._row("h9", "H-E-B", "H-E-B GAS/CARWASH #4SPRINGFIELD",
                  category="FOOD_AND_DRINK")
        self._migrate()
        self.assertEqual(self._cats("h9"),
                         ("TRANSPORTATION", "TRANSPORTATION_GAS", None))

    def test_a_row_the_rule_did_not_prove_keeps_its_category(self):
        self._row("h10", "Costco", "COSTCO WHSE #0007")
        self._migrate()
        self.assertEqual(self._cats("h10")[0], "GENERAL_MERCHANDISE")

    def test_the_user_s_own_category_is_never_overwritten(self):
        self._row("h11", "H-E-B", "H-E-B GAS/CARWASH #512",
                  category="FOOD_AND_DRINK")
        self.conn.execute("UPDATE transactions SET category_override=%s "
                          "WHERE id='h11'", ("ENTERTAINMENT",))
        self._migrate()
        cats = self._cats("h11")
        self.assertEqual(cats[2], "ENTERTAINMENT")      # untouched
        self.assertEqual(cats[0], "FOOD_AND_DRINK")     # and not refiled


if __name__ == "__main__":
    unittest.main()
