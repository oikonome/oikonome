"""A correction must teach the classifier, not just write one rule.

The categorizer used to be a frozen deployment artifact: the household's
corrections accumulated as per-merchant rules while the model learned
nothing, so every NEW lookalike merchant still needed the LLM — or, on an
instance with no LLM, stayed wherever the aggregator left it. The nightly
overlay closes that loop. These tests protect its contract: it trains
from human/LLM answers only (never from its own output — that echo would
inflate confidence without information), it refuses to train a model too
small to generalize, it abstains below the confidence bar like every
machine source, and a retrain with nothing new is a cheap no-op.
"""
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import model_categorize, model_train

from .util import make_db

try:
    import sklearn                                       # noqa: F401
    HAVE_SKLEARN = True
except Exception:                                        # noqa: BLE001
    HAVE_SKLEARN = False


def _seed_rules(conn, n_per_class=30):
    """Distinctive vocabularies per class, many merchants each — enough
    for the trainer's floor and separable enough to generalize from."""
    rows = []
    for i in range(n_per_class):
        rows.append((f"Cafe Ristorante {i} Trattoria", "FOOD_AND_DRINK"))
        rows.append((f"Clinic Dermatology {i} Health", "MEDICAL"))
        rows.append((f"Garage Autoworks {i} Tires", "TRANSPORTATION"))
    for merchant, cat in rows:
        conn.execute(
            """INSERT INTO merchant_categories (merchant, category_primary,
                   source)
               VALUES (%s, %s, 'user')
               ON CONFLICT (tenant_id, merchant) DO NOTHING""",
            (merchant, cat))
    return len(rows)


@unittest.skipUnless(HAVE_SKLEARN, "optional [model] extra not installed")
class OverlayTrainingTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_corrections_generalize_to_unseen_lookalikes(self):
        _seed_rules(self.conn)
        r = model_train.train(self.conn)
        self.assertEqual("trained", r["status"], r)
        # merchants the tenant has NEVER categorized, shaped like ones it has
        got = model_categorize.classify(
            [{"merchant": "Trattoria Nuova Cafe", "sample_name": ""},
             {"merchant": "Lakeside Dermatology Clinic", "sample_name": ""}],
            conn=self.conn)
        self.assertEqual("FOOD_AND_DRINK", got.get(0, (None,))[0])
        self.assertEqual("MEDICAL", got.get(1, (None,))[0])

    def test_gibberish_is_an_abstention_not_a_guess(self):
        _seed_rules(self.conn)
        model_train.train(self.conn)
        got = model_categorize.classify(
            [{"merchant": "ZQXV 000 KJH", "sample_name": ""}],
            conn=self.conn)
        # absent = abstained; a low-information string must not clear the
        # same bar a real lookalike clears
        self.assertNotIn(0, got)

    def test_model_output_is_not_training_input(self):
        """source='model' rows alone must never produce a model."""
        for i in range(120):
            self.conn.execute(
                """INSERT INTO merchant_categories (merchant,
                       category_primary, source)
                   VALUES (%s, 'FOOD_AND_DRINK', 'model')
                   ON CONFLICT (tenant_id, merchant) DO NOTHING""",
                (f"Echo Merchant {i}",))
        r = model_train.maybe_train(self.conn)
        self.assertEqual("too-few", r["status"],
                         "the model trained on its own echoes")

    def test_unchanged_corrections_do_not_retrain(self):
        _seed_rules(self.conn)
        self.assertEqual("trained", model_train.maybe_train(self.conn)["status"])
        self.assertEqual("current", model_train.maybe_train(self.conn)["status"])
        # one more human answer moves the set → retrain fires
        self.conn.execute(
            """INSERT INTO merchant_categories (merchant, category_primary,
                   source) VALUES ('Brand New Bakery Cafe', 'FOOD_AND_DRINK',
                   'user')""")
        self.assertEqual("trained", model_train.maybe_train(self.conn)["status"])

    def test_overlay_stays_in_its_own_tenant(self):
        _seed_rules(self.conn)
        model_train.train(self.conn)
        admin = tenancy.admin_connect()
        try:
            tid = str(tenancy.create_tenant(
                admin, f"ov-{uuid.uuid4().hex[:8]}@x.dev"))
        finally:
            admin.close()
        other = tenancy.tenant_connect(tid)
        try:
            self.assertIsNone(model_categorize.tenant_model(other),
                              "a tenant read a neighbour's model")
        finally:
            other.close()

    def test_too_few_samples_refuse_to_train(self):
        self.conn.execute(
            """INSERT INTO merchant_categories (merchant, category_primary,
                   source) VALUES ('Lone Cafe', 'FOOD_AND_DRINK', 'user')""")
        self.assertEqual("too-few", model_train.train(self.conn)["status"])


if __name__ == "__main__":
    unittest.main()
