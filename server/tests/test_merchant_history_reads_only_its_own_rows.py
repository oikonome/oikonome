"""A merchant history is answered from that merchant's rows, not the ledger.

The page's matching is deliberately fuzzy — a payee reaches the ledger under
several strings, so the answer is a token/canonical/display decision made per
row, and that decision is the product behaviour. What must NOT be part of it
is where the rows come from: asking the question in Python alone means pulling
every non-removed transaction in the tenant into the worker (twice — the row
list is date-bounded, the lifetime aggregate cannot be) and running the
tokenizer over each one. On a large ledger that is tens of
thousands of rows and hundreds of milliseconds of CPU for a page both clients
link to from every merchant name they render.

So the candidate set is narrowed in SQL first. The narrowing is only allowed
to be a SUPERSET of the row predicate, and these tests are what says so: every
probe is answered twice — once narrowed, once with the narrowing neutralized,
which is exactly the pre-narrowing whole-ledger read — and the two answers
must be identical, down to the monthly rows and the lifetime aggregate.

The fixture is built out of near misses on purpose: a rename to words the bank
never sent, an outlet split off from its own brand, an abbreviation sharing no
token with the name people use, a hyphenated brand one word away from another,
a merchant name that is a substring of three unrelated ones, and non-ASCII
descriptors whose token runs break in unobvious places.
"""

import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import bills, budget, merchant_dedup

from .util import add_bill, add_txn, make_db, write_config

TODAY = dt.date(2026, 8, 14)


def _shape(h: dict) -> dict:
    """Everything the endpoint hands the clients, in a comparable form."""
    return {"txns": [dict(r) for r in h["txns"]],
            "monthly": h["monthly"], "lifetime": h["lifetime"],
            "inflow": h["inflow"], "fit": h["fit"], "logo": h["logo"]}


def _unnarrowed(conn, payee: str) -> dict:
    """The answer the page gave before the SQL narrowing existed: every
    non-removed row fetched, every row put to the Python predicate."""
    with mock.patch.object(bills, "_history_match_sql",
                           return_value=("TRUE", [])):
        return bills.merchant_history(conn, payee, today=TODAY)


class AMerchantHistoryIsNarrowedWithoutChangingTheAnswer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = make_db()
        write_config(cls.conn)
        conn = cls.conn
        d = dt.date

        # --- a brand and its fuel arm: one name, two merchants -------------
        for day, amt in ((2, 34.10), (16, 47.80)):
            tid = add_txn(conn, d(2026, 7, day), amt, "COSTCO GAS #0007",
                          merchant="Costco", account="chk")
            conn.execute("UPDATE transactions SET merchant_outlet=%s "
                         "WHERE id=%s", ("Costco Gas", tid))
        for day, amt in ((4, 210.55), (19, 88.20)):
            add_txn(conn, d(2026, 7, day), amt, "COSTCO WHSE #0007",
                    merchant="Costco", account="chk")

        # --- two spellings the user later merges under a name the bank
        #     never sent (no token of it appears in either descriptor) -----
        add_txn(conn, d(2026, 6, 9), 6.75, "SQ *BLUE BOTTLE 88", account="chk")
        add_txn(conn, d(2026, 8, 2), 8.25, "BLUEBOTTLE #4 OAKLAND",
                account="chk")

        # --- an abbreviation and the name people use ----------------------
        add_txn(conn, d(2026, 7, 7), 24.99, "AMZN MKTP US*2H4K9", account="chk")
        add_txn(conn, d(2026, 8, 1), 15.00, "AMAZON.COM*RT4B1", account="chk")

        # --- hyphenated brands one word apart -----------------------------
        add_txn(conn, d(2026, 7, 3), 45.00, "NOVA MOBILE PREPAID", account="chk")
        add_txn(conn, d(2026, 8, 3), 45.00, "NOVA MOBILE PREPAID", account="chk")
        add_txn(conn, d(2026, 7, 3), 71.15, "T-MOBILE PCS SVC", account="chk")
        add_txn(conn, d(2026, 8, 3), 71.15, "T-MOBILE PCS SVC", account="chk")

        # --- a rebrand written as alternatives on the bill ----------------
        add_txn(conn, d(2026, 6, 20), 20.00, "HELIOSOFT INC", account="chk")
        add_txn(conn, d(2026, 8, 8), 20.00, "HELIO.APP SUBSCRIPTION",
                account="chk")

        # --- a four-letter merchant name that is a substring of three
        #     unrelated descriptors, and must not sweep them in -----------
        add_txn(conn, d(2026, 7, 11), 60.00, "CARE PHARMACY", account="chk")
        add_txn(conn, d(2026, 7, 12), 900.00, "BOREALIS HEALTHCARE PAYROLL",
                account="chk")
        add_txn(conn, d(2026, 7, 13), 130.00, "CAREVANE SYSTEMS",
                account="chk")
        add_txn(conn, d(2026, 7, 14), 75.00, "ELDER DAYCARE CENTER",
                account="chk")

        # --- non-ASCII, where the letter runs break in odd places ---------
        add_txn(conn, d(2026, 7, 21), 12.40, "CAFÉ MÜLLER BERLIN",
                account="chk")
        add_txn(conn, d(2026, 8, 5), 9.90, "Café Müller Berlin", account="chk")
        add_txn(conn, d(2026, 7, 22), 31.00, "MULLER GROCERY", account="chk")

        # --- a bill whose charges arrive under two raw spellings ----------
        add_bill(conn, "Alderway Fiber", 79.99, next_due=d(2026, 9, 5))
        add_txn(conn, d(2026, 6, 5), 79.99, "ALDERWAY FIBER TELECOM",
                account="chk")
        add_txn(conn, d(2026, 7, 5), 79.99, "ALDERWAY FIBER", account="chk")
        add_txn(conn, d(2026, 8, 5), 79.99, "ALDERWAY FIBER TELECOM",
                account="chk")

        # --- a phrase bill: its lone long token must not take T-Mobile ----
        add_bill(conn, "Nova Mobile", 45.00, merchant="Nova Mobile",
                 next_due=d(2026, 9, 3))
        # --- an alternatives bill -----------------------------------------
        add_bill(conn, "Helio", 20.00, merchant="helio|heliosoft",
                 next_due=d(2026, 9, 8))
        # --- a category-pool envelope: rows it owns share no merchant -----
        add_bill(conn, "Sitters", 400, bill_type="envelope",
                 category="CHILD CARE", match_category=True)
        add_txn(conn, d(2026, 7, 6), 120.0, "VENMO JANE DOE",
                primary="CHILD_CARE", account="chk")
        add_txn(conn, d(2026, 7, 17), 80.0, "ATM WITHDRAWAL",
                override="CHILD CARE", account="chk")

        # --- income, so the inflow branch is exercised too -----------------
        for day in (15, 31):
            add_txn(conn, d(2026, 7, day), -2400.00, "NORTHWIND PAYROLL DEP",
                    primary="INCOME", account="chk")

        # --- plain noise, so "the whole ledger" is meaningfully bigger ----
        for i in range(20):
            add_txn(conn, d(2026, 7, 1 + (i % 28)), 10.0 + i,
                    f"UNRELATED VENDOR {i:02d} LLC", account="card")

        merchant_dedup.apply(conn)
        for raw in ("SQ *BLUE BOTTLE 88", "BLUEBOTTLE #4 OAKLAND"):
            display = merchant_dedup.canonical_merchant(
                raw, merchant_dedup.cities_for(conn))
            merchant_dedup.rename(conn, display, "The Blue Bottle Co.")

        cls.ledger_rows = conn.execute(
            "SELECT count(*) AS n FROM transactions").fetchone()["n"]

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    # every entry point a client can link to, plus the ones that used to be
    # answered by the token fallback alone
    PROBES = ["Costco", "Costco Gas", "The Blue Bottle Co.", "Amazon",
              "AMZN MKTP US*2H4K9", "Nova Mobile", "T-Mobile", "Helio",
              "Care", "Borealis Healthcare", "Café Müller Berlin",
              "Muller Grocery", "Alderway Fiber", "Sitters",
              "Northwind Payroll Dep", "", "ZZ NOTHING LIKE THIS EXISTS"]

    def test_narrowing_returns_exactly_the_unnarrowed_answer(self):
        for payee in self.PROBES:
            with self.subTest(payee=payee):
                narrowed = bills.merchant_history(self.conn, payee,
                                                  today=TODAY)
                self.assertEqual(_shape(narrowed),
                                 _shape(_unnarrowed(self.conn, payee)))

    def test_the_near_misses_are_still_told_apart(self):
        """The comparison above is only worth anything if the probes match
        DIFFERENT rows — a narrowing that returned nothing would pass it."""
        got = {p: sorted(t["txn_id"] for t in
                         bills.merchant_history(self.conn, p,
                                                today=TODAY)["txns"])
               for p in self.PROBES}
        # the fuel arm and its brand are disjoint, and neither is empty
        self.assertTrue(got["Costco"] and got["Costco Gas"])
        self.assertFalse(set(got["Costco"]) & set(got["Costco Gas"]))
        # a phrase bill takes its own carrier and not the other one, while
        # the arbitrary-merchant page for "T-Mobile" still falls back to its
        # one long token and sweeps both — the fuzzy behaviour this narrowing
        # is not allowed to tighten
        self.assertEqual(len(got["Nova Mobile"]), 2)
        self.assertFalse(set(got["Nova Mobile"]) & {"T-MOBILE PCS SVC"})
        self.assertEqual(len(got["T-Mobile"]), 4)
        # a rebrand written as alternatives takes both names
        self.assertEqual(len(got["Helio"]), 2)
        # a four-letter name is a whole token, never a substring
        self.assertEqual(len(got["Care"]), 1)
        # a merged pair answers under the name the bank never sent
        self.assertEqual(len(got["The Blue Bottle Co."]), 2)
        # and a name nothing displays under finds nothing
        self.assertEqual(got["ZZ NOTHING LIKE THIS EXISTS"], [])

    def test_the_tokenizer_never_sees_the_whole_ledger(self):
        """The cost this narrowing exists to remove: unnarrowed, both scans
        fetch every non-removed row and tokenize each one — twice over, per
        request, on a page any signed-in caller can loop."""
        self.assertGreater(self.ledger_rows, 40)
        real = budget._tokens
        seen = []

        def counting(text):
            seen.append(text)
            return real(text)

        with mock.patch.object(budget, "_tokens", counting):
            bills.merchant_history(self.conn, "Alderway Fiber", today=TODAY)
            bills.merchant_history(self.conn, "Costco Gas", today=TODAY)
        self.assertLess(len(seen), self.ledger_rows)


if __name__ == "__main__":
    unittest.main()
