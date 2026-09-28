"""When the string clean renames a merchant already in the ledger, what
was attached to the old merchant follows its rows.

The nightly pass recomputes every machine-written canonical, so improving
the cleaning moves rows from the merchant the old string named to the one
the new string names. Two things hold the old merchant where the move
cannot see them: a bill's identity keeps its ID in the bill's own JSON, and
a category rule is stored under its NAME. Dropped without a trace, the old
merchant takes both with it — the bill stops matching its own charges and
the household's correction stops applying, neither with any sign.

So the emptied merchant is left pointing at the one its rows went to, the
way a person's merge leaves it; the pointer survives until the bill has
read it; and a rule under the old name moves to the new one.
"""

import unittest

from oikonome.engine import bills, budget, merchant_dedup, merchant_identity

from .util import add_txn, make_db

POSSESSIVE = "Juniper's Market"
STRIPPED = "JUNIPERS MARKET"
WAS = "Juniper Market"
NOW = "Junipers Market"
# a third spelling a person deliberately keeps under the old name
KEPT = "Juniper Market Annex"


class ReCleanedMerchantTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        add_txn(self.conn, "2026-08-01", 31.40, POSSESSIVE, txn_id="pos1")
        add_txn(self.conn, "2026-08-03", 44.10, STRIPPED, txn_id="str1")
        for raw, canon in ((POSSESSIVE, WAS), (STRIPPED, NOW)):
            self.conn.execute(
                "INSERT INTO merchant_canonical (raw_merchant, canonical, "
                "method) VALUES (%s,%s,'layer1')", (raw, canon))
        merchant_identity.resolve(self.conn)
        self.old = self._id(WAS)
        self.new = self._id(NOW)

    def tearDown(self):
        self.conn.close()

    def _id(self, name):
        return str(self.conn.execute(
            "SELECT id FROM merchants WHERE name = %s", (name,)).fetchone()["id"])

    def _merged_into(self, mid):
        row = self.conn.execute(
            "SELECT merged_into::text AS s FROM merchants WHERE id::text = %s",
            (mid,)).fetchone()
        return row and row["s"]

    def _bill(self):
        bills.save_bill(self.conn, payee="Juniper groceries", amount=30,
                        frequency="monthly", merchants=[WAS])
        row = self.conn.execute(
            "SELECT payee, raw FROM bills WHERE payee = 'Juniper groceries'"
        ).fetchone()
        self.assertEqual([r["id"] for r in budget.bill_refs(row["raw"])],
                         [self.old])
        return row

    def _rule(self, name):
        return self.conn.execute(
            "SELECT category_primary, source FROM merchant_categories "
            "WHERE merchant = %s", (name,)).fetchone()

    # ---- the pointer ------------------------------------------------------

    def test_the_emptied_merchant_points_at_the_one_its_rows_went_to(self):
        self._bill()
        merchant_dedup.apply(self.conn)
        self.assertEqual(self._merged_into(self.old), self.new)

    def test_a_bill_on_the_old_merchant_matches_the_survivor_at_once(self):
        row = self._bill()
        merchant_dedup.apply(self.conn)
        self.assertEqual(budget.bill_merchant_ids(self.conn, [row]),
                         {"Juniper groceries": frozenset({self.new})})

    def test_the_pointer_outlives_every_prune_until_the_bill_has_read_it(self):
        """The bill pass runs BEFORE the merchant pass each night, so the
        pointer written tonight is first read tomorrow — after tonight's
        prune, and after any number of syncs' worth of full passes."""
        self._bill()
        merchant_dedup.apply(self.conn)
        merchant_identity.prune_empty(self.conn)
        merchant_dedup.apply(self.conn)
        self.assertEqual(self._merged_into(self.old), self.new)
        # the bill pass re-points the bill at the survivor, by id and name…
        bills.reconcile_bill_merchants(self.conn)
        raw = self.conn.execute(
            "SELECT raw FROM bills WHERE payee = 'Juniper groceries'"
        ).fetchone()["raw"]
        self.assertEqual(budget.bill_refs(raw), [{"id": self.new, "name": NOW}])
        # …and only then is the emptied merchant free to go
        self.assertEqual(merchant_identity.prune_empty(self.conn), 1)
        self.assertIsNone(self._merged_into(self.old))

    def test_a_merchant_no_bill_names_is_pruned_the_same_night(self):
        merchant_dedup.apply(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM merchants").fetchone()["n"], 1)

    def test_a_live_empty_merchant_is_not_held_by_a_bill(self):
        """The hold is for a pointer a bill has yet to follow. A live
        merchant whose charges are all gone names nothing to follow to, and
        the bill's stored name finds the merchant again if they return."""
        self._bill()
        self.conn.execute("UPDATE transactions SET removed = 1 WHERE id = 'pos1'")
        self.conn.execute("DELETE FROM merchant_canonical WHERE raw_merchant = %s",
                          (POSSESSIVE,))
        self.assertEqual(merchant_identity.prune_empty(self.conn), 1)

    def test_the_survivor_gains_the_logo_it_lacked(self):
        self.conn.execute("UPDATE merchants SET logo_url = 'https://logo.example/j.png' "
                          "WHERE id::text = %s", (self.old,))
        self._bill()
        merchant_dedup.apply(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT logo_url FROM merchants WHERE id::text = %s",
            (self.new,)).fetchone()["logo_url"], "https://logo.example/j.png")

    # ---- the rule ---------------------------------------------------------

    def test_a_rule_under_the_old_name_moves_to_the_new_one(self):
        self.conn.execute(
            "INSERT INTO merchant_categories (merchant, category_primary, "
            "source) VALUES (%s,'FOOD_AND_DRINK','user')", (WAS,))
        merchant_dedup.apply(self.conn)
        self.assertIsNone(self._rule(WAS))
        self.assertEqual(merchant_dedup.detail(self.conn, NOW)["rule"],
                         {"category_primary": "FOOD_AND_DRINK", "source": "user"})

    def test_a_persons_rule_outranks_the_machines_under_the_new_name(self):
        self.conn.execute(
            "INSERT INTO merchant_categories (merchant, category_primary, "
            "source) VALUES (%s,'FOOD_AND_DRINK','user'), "
            "(%s,'GENERAL_MERCHANDISE','llm')", (WAS, NOW))
        merchant_dedup.apply(self.conn)
        self.assertIsNone(self._rule(WAS))
        self.assertEqual(self._rule(NOW), {"category_primary": "FOOD_AND_DRINK",
                                           "source": "user"})

    def test_the_survivors_own_rule_stays_when_neither_outranks(self):
        self.conn.execute(
            "INSERT INTO merchant_categories (merchant, category_primary, "
            "source) VALUES (%s,'FOOD_AND_DRINK','llm'), "
            "(%s,'GENERAL_MERCHANDISE','user')", (WAS, NOW))
        merchant_dedup.apply(self.conn)
        self.assertIsNone(self._rule(WAS))
        self.assertEqual(self._rule(NOW),
                         {"category_primary": "GENERAL_MERCHANDISE",
                          "source": "user"})

    def test_a_rule_whose_name_another_live_merchant_carries_is_left_alone(self):
        """A person kept one spelling under the old name: the rule still
        names a live merchant, and moving it would take it off their rows."""
        add_txn(self.conn, "2026-08-05", 9.00, KEPT, txn_id="kept")
        merchant_dedup.apply(self.conn)
        merchant_dedup.rename(
            self.conn, merchant_dedup.canonical_merchant(KEPT), WAS)
        self.conn.execute(
            "INSERT INTO merchant_categories (merchant, category_primary, "
            "source) VALUES (%s,'FOOD_AND_DRINK','user')", (WAS,))
        self.assertEqual(merchant_dedup._rekey_rules(self.conn, WAS, NOW), 0)
        self.assertIsNotNone(self._rule(WAS))


if __name__ == "__main__":
    unittest.main()
