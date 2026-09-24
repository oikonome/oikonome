"""An apostrophe in a payee's name is read both ways, on both sides.

Banks drop the punctuation out of a name: the card line reads "JUNIPERS
MARKET 0442" for the payee an aggregator resolves, apostrophe and all, as
"Juniper's Market". Every question this resolver asks about a descriptor —
does it name this payee, may it overrule a stray guess — compares the two
strings word by word, so reading the apostrophe only as a break leaves the
possessive wanting a lone "s" that no bank descriptor can supply: the whole
descriptor machinery switches off for possessive brands, and each of them
collects a twin merchant.

Deleting the apostrophe instead only moves the hole. A line that prints an
elided prefix as a word of its own ("O DALEY HARDWARE"), or omits it, no
longer matches the joined form — and the string clean splits such names
that way too, so the ledger's own cleaned spellings would stop matching
their lines. Both readings are therefore offered on both sides, and either
one matching is enough.
"""

import unittest

from oikonome.engine import merchant_identity

from .util import add_txn, as_date, jsonb, make_db, write_config

LINE = "JUNIPERS MARKET 0442"
BRAND = "Juniper's Market"
ENTITY = "ENT-JUNIPERSMARKET"
STRAY = "Harbor Lights"


class ApostropheReadingTests(unittest.TestCase):
    """The two readings, as the comparison itself sees them — no ledger
    needed, and every shape a feed actually writes."""

    def names(self, line, merchant):
        return merchant_identity._descriptor_names(line, merchant)

    def test_a_line_without_the_apostrophe_names_the_possessive(self):
        self.assertTrue(self.names(LINE, BRAND))
        # and with the location tail banks glue on
        self.assertTrue(self.names("JUNIPERS MARKET SPRINGFIELD OR", BRAND))

    def test_every_apostrophe_a_feed_writes_counts(self):
        for mark in ("'", "’", "ʼ"):
            with self.subTest(mark=mark):
                self.assertTrue(self.names(LINE, f"Juniper{mark}s Market"))

    def test_an_elided_prefix_still_matches_its_line(self):
        """The string clean splits "O'Reilly" into two words, so the
        ledger's own cleaned name has to match the line it came from."""
        self.assertTrue(self.names("O'DALEY HARDWARE 17", "Daley Hardware"))
        self.assertTrue(self.names("L'AMANDIERE RIVERTON", "Amandiere"))

    def test_a_prefix_the_line_spaces_out_still_matches(self):
        self.assertTrue(self.names("O DALEY HARDWARE 17", "O'Daley Hardware"))

    def test_two_payees_are_still_two_payees(self):
        """The readings widen what counts as the same name, never what
        counts as a different one."""
        self.assertFalse(self.names(LINE, "Harbor Lights"))
        self.assertFalse(self.names("JUNIPERS MARKET 0442", "Juniper Dental"))


class PossessiveNameTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")

    def tearDown(self):
        self.conn.close()

    def _enriched(self, date, amount, txn_id, line=LINE):
        """A row the aggregator resolved to its own entity, which is what
        keeps the apostrophe in the merchant's name — the layer-1 string
        clean drops it, and then there is nothing to notice."""
        self.conn.execute(
            """INSERT INTO transactions (id,account_id,date,amount,name,
                   merchant_name,category_primary,pending,removed,raw)
               VALUES (%s,'card',%s,%s,%s,%s,'GENERAL_MERCHANDISE',0,0,%s)""",
            (txn_id, as_date(date), amount, line, BRAND,
             jsonb({"merchant_entity_id": ENTITY, "merchant_name": BRAND,
                    "counterparties": [{"name": BRAND, "type": "merchant",
                                        "entity_id": ENTITY,
                                        "confidence_level": "VERY_HIGH"}]})))
        return txn_id

    def _bare(self, date, amount, txn_id, line=LINE):
        self.conn.execute(
            """INSERT INTO transactions (id,account_id,date,amount,name,
                   merchant_name,category_primary,pending,removed,raw)
               VALUES (%s,'card',%s,%s,%s,NULL,'GENERAL_MERCHANDISE',0,0,%s)""",
            (txn_id, as_date(date), amount, line, jsonb({})))
        return txn_id

    def _merchant_of(self, txn_id):
        return self.conn.execute(
            "SELECT m.id, m.name FROM transactions t "
            "JOIN merchants m ON m.id = t.merchant_id WHERE t.id=%s",
            (txn_id,)).fetchone()

    def test_a_bare_row_joins_the_possessive_payee_its_line_names(self):
        charge = self._enriched("2026-09-09", 25.00, "pchg")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of(charge)["name"], BRAND)
        refund = self._bare("2026-09-10", -25.00, "pref")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of(refund)["id"],
                         self._merchant_of(charge)["id"],
                         "the apostrophe cost the payee its own bank line")

    def test_the_line_may_still_overrule_a_stray_guess_for_it(self):
        usual = [self._enriched(f"2026-08-{d + 1:02d}", 20.0 + d, f"ph{d}")
                 for d in range(merchant_identity.ESTABLISHED_ROWS)]
        merchant_identity.resolve(self.conn)
        add_txn(self.conn, "2026-04-14", 7.40, LINE, merchant=STRAY,
                txn_id="pstray")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of("pstray")["id"],
                         self._merchant_of(usual[0])["id"])


if __name__ == "__main__":
    unittest.main()
