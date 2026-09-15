"""A merchant's history is the merchant the ledger displayed.

The ledger, the Merchants catalog and both clients name a payee with one
string — the display key (merchant_dedup.DISPLAY_MERCHANT over MC_JOIN:
a reviewed/manual canonical, else the outlet a chain's fuel arm was
identified as, else what the feed called it). Every link into the merchant
history page hands back exactly that string, so the page must resolve on it
too. When it resolved on the bank's raw descriptor instead, the app showed a
name it could not then look up: a merged fuel arm came back empty, and a
chain's own page swept in the pump charges the ledger had split off — the
history and the catalog disagreeing about the same merchant in both
directions.

Bill matching is the other question — what did the bank literally send —
and stays on the raw descriptor's tokens. The last test here is what says so.
"""

import datetime as dt
import unittest

from oikonome.engine import bills, merchant_dedup

from .util import add_bill, add_txn, make_db, write_config

TODAY = dt.date(2026, 8, 14)


def _catalog(conn, display: str) -> dict:
    """What the Merchants page reports for one display name."""
    for row in merchant_dedup.listing(conn, limit=500):
        if row["display"] == display:
            return row
    return {"display": display, "rows": 0, "total": 0.0}


def _displays(conn) -> set:
    return {r["display"] for r in merchant_dedup.listing(conn, limit=500)}


class FuelArmHistoryIsTheFuelArm(unittest.TestCase):
    """A chain that also sells fuel is two merchants wearing one name, and
    the ledger already splits them: the pump rows carry an outlet, so they
    display as 'Costco Gas' while the warehouse rows display as 'Costco'.
    The history page has to honour that split in both directions — the map
    cannot help it, because the map is keyed by merchant STRING and 'Costco'
    is the pump on one row and the warehouse on the next."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        # Plaid's real shape: the brand in merchant_name, the statement line
        # in name, and the pump identified beside them as the outlet.
        for day, amount in ((3, 32.00), (10, 41.50)):
            tid = add_txn(self.conn, dt.date(2026, 8, day), amount,
                          "COSTCO GAS #0007", merchant="Costco", account="chk")
            self.conn.execute(
                "UPDATE transactions SET merchant_outlet=%s WHERE id=%s",
                ("Costco Gas", tid))
        for day, amount in ((5, 255.00), (12, 118.25)):
            add_txn(self.conn, dt.date(2026, 8, day), amount,
                    "COSTCO WHSE #0007", merchant="Costco", account="chk")
        merchant_dedup.apply(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_the_pump_page_shows_the_pump_charges_only(self):
        self.assertIn("Costco Gas", _displays(self.conn))
        h = bills.merchant_history(self.conn, "Costco Gas", today=TODAY)
        self.assertEqual(sorted(t["amount"] for t in h["txns"]),
                         [32.00, 41.50])
        cat = _catalog(self.conn, "Costco Gas")
        self.assertEqual(h["lifetime"]["count"], cat["rows"])
        self.assertEqual(h["lifetime"]["total"], float(cat["total"]))

    def test_the_warehouse_page_does_not_swallow_the_pump(self):
        h = bills.merchant_history(self.conn, "Costco", today=TODAY)
        self.assertEqual(sorted(t["amount"] for t in h["txns"]),
                         [118.25, 255.00])
        cat = _catalog(self.conn, "Costco")
        self.assertEqual(h["lifetime"]["count"], cat["rows"])
        self.assertEqual(h["lifetime"]["total"], float(cat["total"]))

    def test_a_merged_fuel_arm_is_found_under_its_new_name(self):
        # merging a chain's fuel arm is the worked example in the user guide
        merchant_dedup.rename(self.conn, "Costco Gas", "Costco Fuel")
        self.assertIn("Costco Fuel", _displays(self.conn))
        h = bills.merchant_history(self.conn, "Costco Fuel", today=TODAY)
        self.assertEqual([t["payee"] for t in h["txns"]],
                         ["Costco Fuel", "Costco Fuel"])
        cat = _catalog(self.conn, "Costco Fuel")
        self.assertEqual(len(h["txns"]), cat["rows"])
        self.assertEqual(h["lifetime"]["total"], float(cat["total"]))


class RenamedMerchantHistoryFollowsTheName(unittest.TestCase):
    """Two spellings of one business, merged by hand under a name the bank
    never sent. Nothing in the raw text contains the words the user chose,
    so a token-subset match cannot find these rows — the page came back
    empty for a merchant the ledger had just rendered."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.raws = ["SQ *BLUE BOTTLE 88", "BLUEBOTTLE #4 OAKLAND"]
        add_txn(self.conn, dt.date(2026, 7, 9), 6.75, self.raws[0],
                account="chk")
        add_txn(self.conn, dt.date(2026, 8, 2), 8.25, self.raws[1],
                account="chk")
        merchant_dedup.apply(self.conn)
        for raw in self.raws:
            display = merchant_dedup.canonical_merchant(
                raw, merchant_dedup.cities_for(self.conn))
            merchant_dedup.rename(self.conn, display,
                                  "The Blue Bottle Co.")

    def tearDown(self):
        self.conn.close()

    def test_history_of_the_displayed_name_is_the_whole_merchant(self):
        name = "The Blue Bottle Co."
        self.assertIn(name, _displays(self.conn))
        h = bills.merchant_history(self.conn, name, today=TODAY)
        self.assertEqual(sorted(t["amount"] for t in h["txns"]), [6.75, 8.25])
        self.assertEqual([t["payee"] for t in h["txns"]], [name, name])
        cat = _catalog(self.conn, name)
        self.assertEqual(h["lifetime"]["count"], cat["rows"])
        self.assertEqual(h["lifetime"]["total"], float(cat["total"]))
        self.assertEqual(h["lifetime"]["total"], 15.00)

    def test_monthly_totals_cover_both_spellings(self):
        h = bills.merchant_history(self.conn, "The Blue Bottle Co.",
                                   today=TODAY)
        months = {m["month"]: m["total"] for m in h["monthly"]}
        self.assertEqual(months.get("2026-07"), 6.75)
        self.assertEqual(months.get("2026-08"), 8.25)


class BillMatchingStaysOnTheBankText(unittest.TestCase):
    """A bill matches the tokens the BANK sends, which is a different
    question from what the user calls the merchant. Renaming the merchant
    must not move the bill's charges off its history page, and the page must
    still reach every raw spelling the bill's token match covers."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Alderway Fiber", 79.99, next_due=dt.date(2026, 9, 5))
        for day, name in ((5, "ALDERWAY FIBER TELECOM"),
                          (5, "ALDERWAY FIBER")):
            add_txn(self.conn, dt.date(2026, 7 if day == 5 else 8, day), 79.99,
                    name, account="chk")
        add_txn(self.conn, dt.date(2026, 8, 5), 79.99, "ALDERWAY FIBER TELECOM",
                account="chk")
        merchant_dedup.apply(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_bill_history_matches_raw_tokens_across_spellings(self):
        h = bills.merchant_history(self.conn, "Alderway Fiber", today=TODAY)
        self.assertEqual(len(h["txns"]), 3)
        self.assertEqual(h["lifetime"]["total"], 239.97)

    def test_renaming_the_merchant_does_not_move_the_bill_history(self):
        merchant_dedup.rename(self.conn, "Alderway Fiber Telecom",
                              "Fibre Internet Co")
        h = bills.merchant_history(self.conn, "Alderway Fiber", today=TODAY)
        self.assertEqual(len(h["txns"]), 3)
        self.assertEqual(h["lifetime"]["total"], 239.97)


if __name__ == "__main__":
    unittest.main()
