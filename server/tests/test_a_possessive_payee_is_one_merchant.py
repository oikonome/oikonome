"""A household that already carries both spellings of a possessive payee
ends up with ONE merchant.

The string clean is recomputed every sync, so improving it renames
merchants that are already in the ledger — and the improvement that makes
"Juniper's Market" clean to the same string as the card line "JUNIPERS
MARKET" lands on a household that has been reading two names, with its
money split between them, for as long as both spellings have been arriving.
The nightly pass therefore has to finish the job: recompute the canonical,
move the rows to the merchant that string now names, and leave nothing
behind — no third merchant minted from the new spelling, no alias pointing
at a merchant that was pruned.

What it must NOT do is decide this for a person who already answered. A
manual name outranks every automatic layer and is never recomputed, so two
spellings a person deliberately keeps apart stay apart.
"""

import unittest

from oikonome.engine import merchant_dedup, merchant_identity

from .util import add_txn, make_db

# the two spellings of one payee: the name a feed resolves, apostrophe and
# all, and the same payee as a card line prints it
POSSESSIVE = "Juniper's Market"
STRIPPED = "JUNIPERS MARKET"
# what the OLD cleaning made of them — a whole word apart, because the
# apostrophe broke the name and the one-letter piece was dropped as noise
WAS = "Juniper Market"
NOW = "Junipers Market"


class PossessiveConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        add_txn(self.conn, "2026-08-01", 31.40, POSSESSIVE, txn_id="pos1")
        add_txn(self.conn, "2026-08-02", 12.75, POSSESSIVE, txn_id="pos2")
        add_txn(self.conn, "2026-08-03", 44.10, STRIPPED, txn_id="str1")
        # the state the old cleaning left: one canonical per spelling, and
        # rows resolved to a merchant carrying each
        for raw, canon in ((POSSESSIVE, WAS), (STRIPPED, NOW)):
            self.conn.execute(
                "INSERT INTO merchant_canonical (raw_merchant, canonical, "
                "method) VALUES (%s,%s,'layer1')", (raw, canon))
        merchant_identity.resolve(self.conn)

    def tearDown(self):
        self.conn.close()

    def _display(self, txn_id):
        return self.conn.execute(
            f"SELECT {merchant_dedup.DISPLAY_MERCHANT} AS d "
            f"  FROM transactions t {merchant_dedup.MC_JOIN} "
            f" WHERE t.id = %s", (txn_id,)).fetchone()["d"]

    def _live_names(self):
        return sorted(r["name"] for r in self.conn.execute(
            "SELECT name FROM merchants WHERE merged_into IS NULL").fetchall())

    def _dangling_aliases(self):
        """Aliases whose merchant_id names no merchant at all."""
        return [r["raw_merchant"] for r in self.conn.execute(
            "SELECT a.raw_merchant FROM merchant_canonical a "
            " WHERE a.merchant_id IS NOT NULL "
            "   AND NOT EXISTS (SELECT 1 FROM merchants m "
            "                    WHERE m.id = a.merchant_id)").fetchall()]

    def test_the_split_is_real_before_the_pass_runs(self):
        """The starting point, stated so the convergence below means
        something: two merchants, two names, the money divided."""
        self.assertEqual(self._live_names(), [WAS, NOW])
        self.assertEqual(self._display("pos1"), WAS)
        self.assertEqual(self._display("str1"), NOW)

    def test_the_nightly_pass_lands_both_spellings_on_one_merchant(self):
        merchant_dedup.apply(self.conn)
        self.assertEqual(self._display("pos1"), NOW)
        self.assertEqual(self._display("pos2"), NOW)
        self.assertEqual(self._display("str1"), NOW)
        # one merchant, not three: the new spelling must reuse the merchant
        # that already answers to it rather than mint its twin, and the
        # emptied one must not linger in name matching
        self.assertEqual(self._live_names(), [NOW])
        self.assertEqual(len({r["merchant_id"] for r in self.conn.execute(
            "SELECT merchant_id FROM transactions WHERE removed = 0"
        ).fetchall()}), 1)

    def test_nothing_is_left_pointing_at_a_pruned_merchant(self):
        merchant_dedup.apply(self.conn)
        self.assertEqual(self._dangling_aliases(), [])
        # and both aliases point at the survivor
        self.assertEqual(len({r["merchant_id"] for r in self.conn.execute(
            "SELECT merchant_id FROM merchant_canonical").fetchall()}), 1)

    def test_the_pass_is_idempotent_once_it_has_converged(self):
        merchant_dedup.apply(self.conn)
        self.assertEqual(merchant_dedup.apply(self.conn), 0)
        self.assertEqual(self._live_names(), [NOW])
        self.assertEqual(self._dangling_aliases(), [])

    def test_a_manual_name_on_one_spelling_is_never_recomputed(self):
        """A person who named this spelling something else has answered the
        question the cleaning is guessing at, and the answer outranks it."""
        merchant_dedup.rename(self.conn, WAS, "Harbor Lights")
        merchant_dedup.apply(self.conn)
        self.assertEqual(
            self.conn.execute(
                "SELECT canonical, method FROM merchant_canonical "
                " WHERE raw_merchant = %s", (POSSESSIVE,)).fetchone(),
            {"canonical": "Harbor Lights", "method": "manual"})
        self.assertEqual(self._display("pos1"), "Harbor Lights")
        self.assertEqual(self._display("str1"), NOW)
        self.assertEqual(self._live_names(), ["Harbor Lights", NOW])

    def test_a_manual_name_the_other_spelling_already_has_still_merges(self):
        """The same person's other answer: renaming one spelling ONTO the
        name the other carries is a merge, and the improved cleaning must
        not fork it back apart."""
        merchant_dedup.rename(self.conn, WAS, NOW)
        merchant_dedup.apply(self.conn)
        self.assertEqual(self._live_names(), [NOW])
        self.assertEqual(self._display("pos1"), NOW)
        self.assertEqual(self._display("str1"), NOW)

    def test_a_rule_stored_under_a_source_spelling_still_reaches_the_rows(self):
        """A category rule keyed on a raw spelling travels with it: the rule
        lookup resolves the string through the alias table, so a canonical
        that changed underneath it does not detach the rule from the rows it
        was written for."""
        self.conn.execute(
            "INSERT INTO merchant_categories (merchant, category_primary, "
            "source) VALUES (%s,'GENERAL_MERCHANDISE','user')", (POSSESSIVE,))
        merchant_dedup.apply(self.conn)
        rule = merchant_dedup.detail(self.conn, NOW)["rule"]
        self.assertEqual(rule, {"category_primary": "GENERAL_MERCHANDISE",
                                "source": "user"})


if __name__ == "__main__":
    unittest.main()
