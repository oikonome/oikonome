"""One bank descriptor is one payee, however the aggregator enriched it.

The resolver keys on COALESCE(merchant_outlet, merchant_name, name), so the
same purchase keys differently depending on whether a given row carried
enrichment: an enriched charge keys on the tidy merchant string, its
unenriched refund keys on the raw descriptor. That minted a second merchant
for one payee, split its history, and the alias memory could not bridge the
two because it is stored under the key that differs. Refunds and
late-arriving rows are where enrichment is missing most often."""

import unittest

from oikonome.engine import merchant_identity

from .util import add_txn, make_db, write_config

DESCRIPTOR = "EXAMPLE TELECOM NEW YORK USA"


class SameDescriptorOneMerchantTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")

    def tearDown(self):
        self.conn.close()

    def _unenriched(self, date, amount, name, txn_id):
        """A row the aggregator gave no merchant_name — what a refund
        usually looks like."""
        from .util import as_date, jsonb
        self.conn.execute(
            """INSERT INTO transactions (id,account_id,date,amount,name,
                   merchant_name,category_primary,pending,removed,raw)
               VALUES (%s,'card',%s,%s,%s,NULL,'GENERAL_MERCHANDISE',0,0,%s)""",
            (txn_id, as_date(date), amount, name, jsonb({})))
        return txn_id

    def _merchant_of(self, txn_id):
        return self.conn.execute(
            "SELECT m.id, m.name FROM transactions t "
            "JOIN merchants m ON m.id = t.merchant_id WHERE t.id=%s",
            (txn_id,)).fetchone()

    def test_unenriched_refund_joins_the_enriched_charge(self):
        charge = add_txn(self.conn, "2026-09-09", 24.63, DESCRIPTOR,
                         merchant="example telecom", txn_id="chg1")
        merchant_identity.resolve(self.conn)
        refund = self._unenriched("2026-09-10", -24.63, DESCRIPTOR, "ref1")
        merchant_identity.resolve(self.conn)

        c, r = self._merchant_of(charge), self._merchant_of(refund)
        self.assertIsNotNone(c)
        self.assertIsNotNone(r)
        self.assertEqual(r["id"], c["id"],
                         f"refund got {r['name']!r}, charge got {c['name']!r}")

    def test_one_merchant_row_for_the_descriptor(self):
        add_txn(self.conn, "2026-09-09", 24.63, DESCRIPTOR,
                merchant="example telecom", txn_id="chg2")
        merchant_identity.resolve(self.conn)
        self._unenriched("2026-09-10", -24.63, DESCRIPTOR, "ref2")
        merchant_identity.resolve(self.conn)
        n = self.conn.execute(
            "SELECT count(*) AS n FROM merchants WHERE lower(name) LIKE 'example telecom%'"
        ).fetchone()["n"]
        self.assertEqual(n, 1)

    def test_a_different_descriptor_still_gets_its_own_merchant(self):
        """The adoption is keyed on the WHOLE descriptor, so it cannot
        collapse two payees that merely share a prefix."""
        add_txn(self.conn, "2026-09-09", 24.63, DESCRIPTOR,
                merchant="example telecom", txn_id="chg3")
        merchant_identity.resolve(self.conn)
        other = self._unenriched("2026-09-10", 8.00,
                                 "EXAMPLE TELECOM PAYMENTS DENVER CO", "oth1")
        merchant_identity.resolve(self.conn)
        self.assertNotEqual(self._merchant_of(other)["id"],
                            self._merchant_of("chg3")["id"])

    def test_plaid_authority_is_not_overruled_by_the_descriptor(self):
        """A row with its own Plaid entity keeps that identity even when an
        earlier unenriched row taught the descriptor a layer-1 merchant."""
        self._unenriched("2026-09-01", 10.0, "SQ *EXAMPLE ROASTERS OAKLAND", "u1")
        merchant_identity.resolve(self.conn)
        first = self._merchant_of("u1")["id"]
        from .util import as_date, jsonb
        self.conn.execute(
            """INSERT INTO transactions (id,account_id,date,amount,name,
                   merchant_name,category_primary,pending,removed,raw)
               VALUES ('p1','card',%s,12.0,'SQ *EXAMPLE ROASTERS OAKLAND',
                       'Example Roasters','FOOD_AND_DRINK',0,0,%s)""",
            (as_date("2026-09-02"),
             jsonb({"merchant_entity_id": "ENT-EXAMPLEROASTERS",
                    "merchant_name": "Example Roasters",
                    "counterparties": [
                        {"name": "Example Roasters", "type": "merchant",
                         "entity_id": "ENT-EXAMPLEROASTERS",
                         "confidence_level": "VERY_HIGH"}]})))
        merchant_identity.resolve(self.conn)
        got = self._merchant_of("p1")
        self.assertNotEqual(got["id"], first)
        self.assertEqual(got["name"], "Example Roasters")

    def test_a_generic_descriptor_does_not_gather_unrelated_payees(self):
        """Some banks write a constant where the payee should be.

        When the descriptor names no payee — "POS DEBIT PURCHASE" and its
        kin — the rows sharing it are unrelated purchases, and adopting the
        merchant one of them happens to have would fold the whole account
        into it. A descriptor already pointing at two merchants has proved
        it does not name a payee."""
        generic = "POS DEBIT PURCHASE"
        add_txn(self.conn, "2026-09-01", 12.0, generic,
                merchant="corner market", txn_id="g1")
        add_txn(self.conn, "2026-09-02", 30.0, generic,
                merchant="example hardware", txn_id="g2")
        merchant_identity.resolve(self.conn)
        first = self._merchant_of("g1")["id"]
        second = self._merchant_of("g2")["id"]
        self.assertNotEqual(first, second, "fixture should give two merchants")

        # a third, unenriched row carrying the same meaningless descriptor
        self._unenriched("2026-09-03", 7.5, generic, "g3")
        merchant_identity.resolve(self.conn)
        got = self._merchant_of("g3")["id"]
        self.assertNotIn(got, {first, second},
                         "an unenriched row adopted a payee the descriptor "
                         "does not name")

    def test_the_first_pairing_under_a_generic_descriptor_is_refused_too(self):
        """Waiting for a second payee to appear is waiting too long.

        A descriptor pointing at two merchants has proved it names none —
        but that evidence only exists once the damage is done. Under a
        bank's constant the FIRST unenriched row would adopt whichever
        payee happened to be there, and adoption writes an alias, which
        every later pass reads before anything else: the wrong answer
        becomes the authoritative one. So the descriptor has to name the
        payee, not merely have been seen beside it."""
        generic = "PURCHASE AUTHORIZED ON 09/01"
        add_txn(self.conn, "2026-09-01", 12.0, generic,
                merchant="corner market", txn_id="s1")
        merchant_identity.resolve(self.conn)
        first = self._merchant_of("s1")["id"]

        # a second, unrelated payee under the same meaningless descriptor,
        # arriving unenriched — the only payee the descriptor has ever
        # pointed at is the wrong one
        self._unenriched("2026-09-02", 7.5, generic, "s2")
        merchant_identity.resolve(self.conn)

        got = self._merchant_of("s2")
        self.assertIsNotNone(got)
        self.assertNotEqual(got["id"], first,
                            "an unenriched row adopted the one payee the "
                            "descriptor had been seen beside")
