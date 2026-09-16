"""The overlay's training pairs must see the same shape of string the
classifier sees at predict time.

A pair is (merchant + one sample bank descriptor, category). The sample
descriptor is found by matching the rule's merchant back to a transaction,
and a transaction says who its merchant is in two ways: the merchant ROW
it resolved to, and the canonical string of its raw descriptor. Matching
only on strings leaves every Plaid-resolved merchant — the ones the
aggregator names best, and the ones a household sees most — with no
descriptor at all, so those merchants would train on a bare name while
prediction feeds the model a name plus a descriptor.
"""

import unittest

from oikonome.engine import model_train

from .util import add_txn, make_db


class TrainingPairsSeeResolvedMerchants(unittest.TestCase):

    def test_a_plaid_resolved_row_contributes_its_bank_descriptor(self):
        conn = make_db()
        self.addCleanup(conn.close)
        mid = conn.execute(
            "INSERT INTO merchants (name, name_source, plaid_entity_id) "
            "VALUES ('DoorDash','plaid','ENT-DD') RETURNING id").fetchone()["id"]
        tid = add_txn(conn, "2026-07-01", 20.00, "DD *DOORDASH CEDAR DINER",
                      merchant="Doordash Cedar")
        conn.execute("UPDATE transactions SET merchant_id=%s WHERE id=%s",
                     (mid, tid))
        conn.execute(
            "INSERT INTO merchant_categories (merchant, category_primary, source) "
            "VALUES ('DoorDash','FOOD_AND_DRINK','user')")

        pairs = model_train.training_pairs(conn)

        self.assertEqual(pairs, [("DoorDash DD *DOORDASH CEDAR DINER",
                                  "FOOD_AND_DRINK")])


if __name__ == "__main__":
    unittest.main()
