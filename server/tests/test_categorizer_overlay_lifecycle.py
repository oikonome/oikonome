"""The learned overlay must only ever serve an artifact the CURRENT
labeled set and the CURRENT runtime stand behind.

Invariants protected here:

- Training data is invisible without tenant context. The training queries
  deliberately carry no tenant predicate — the ambient RLS setting is the
  design — so a connection with no tenant scope must yield an empty
  training set, never a cross-tenant one.
- A labeled set that shrinks below the training floor retires the stored
  artifact; a model trained on corrections the household walked back must
  not keep serving predictions.
- An artifact pickled under a different sklearn than the running process
  is refused and cleared at load — a version-skewed pickle can
  deserialize fine and still predict garbage, so silent degradation is
  not an option. A same-version artifact that merely fails to load is NOT
  cleared (the data is fine; the runtime is what's broken).
- Renaming a category counts as movement of the labeled set: the nightly
  staleness check must retrain, not report "current" while the overlay
  keeps answering with the old name.
- The training set is capped to the most recently answered merchants, so
  one bulk import cannot buy unbounded nightly fit CPU or artifact
  growth — and a capped tenant with nothing new is still a cheap no-op.
- Feature text uses each merchant's newest bank descriptor, whatever
  shape the query that gathers it takes.
"""
import unittest

import psycopg
from psycopg.rows import dict_row

from oikonome.db import tenancy
from oikonome.engine import model_categorize, model_train
from oikonome.web import data

from .util import TODAY, add_txn, make_db

try:
    import sklearn                                       # noqa: F401
    HAVE_SKLEARN = True
except Exception:                                        # noqa: BLE001
    HAVE_SKLEARN = False


def _seed_rules(conn, n_per_class=30):
    """Distinctive vocabularies per class, enough for the trainer's floor."""
    for i in range(n_per_class):
        for merchant, cat in (
                (f"Cafe Ristorante {i} Trattoria", "FOOD_AND_DRINK"),
                (f"Clinic Dermatology {i} Health", "MEDICAL"),
                (f"Garage Autoworks {i} Tires", "TRANSPORTATION")):
            conn.execute(
                """INSERT INTO merchant_categories (merchant,
                       category_primary, source)
                   VALUES (%s, %s, 'user')
                   ON CONFLICT (tenant_id, merchant) DO NOTHING""",
                (merchant, cat))


class OverlayLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_training_data_invisible_without_tenant_context(self):
        """No tenant scope ⇒ empty training set, never a cross-tenant one.

        training_pairs/maybe_train carry no tenant_id predicate on
        purpose — ambient RLS owns tenancy. This pins the other half of
        that bargain: run them on a connection whose app.tenant_id was
        never set and they must see nothing, even though the data exists.
        """
        _seed_rules(self.conn, n_per_class=2)
        self.assertTrue(model_train.training_pairs(self.conn),
                        "scoped connection should see its own rules")
        raw = psycopg.connect(tenancy.APP_DSN, row_factory=dict_row,
                              autocommit=True)
        try:
            self.assertEqual([], model_train.training_pairs(raw),
                             "unscoped connection saw tenant training data")
            # and the nightly entry must refuse, not train on leakage
            self.assertIn(model_train.maybe_train(raw)["status"],
                          ("too-few", "unavailable"))
        finally:
            raw.close()

    @unittest.skipUnless(HAVE_SKLEARN, "optional [model] extra not installed")
    def test_shrunken_labeled_set_retires_stored_overlay(self):
        """Falling below the training floor deletes the stale artifact."""
        _seed_rules(self.conn)
        self.assertEqual("trained", model_train.train(self.conn)["status"])
        # the household walks back most of its corrections
        self.conn.execute(
            "DELETE FROM merchant_categories WHERE merchant NOT LIKE 'Cafe%'")
        r = model_train.maybe_train(self.conn)
        self.assertEqual("too-few", r["status"], r)
        self.assertTrue(r.get("retired"), r)
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM categorizer_model").fetchone(),
            "stale artifact left serving after the labeled set shrank")
        self.assertIsNone(model_categorize.tenant_model(self.conn))

    @unittest.skipUnless(HAVE_SKLEARN, "optional [model] extra not installed")
    def test_version_skewed_overlay_refused_and_cleared(self):
        """A pickle from a different sklearn is refused at load and the
        row cleared so the nightly retrain rebuilds it."""
        self.conn.execute(
            """INSERT INTO categorizer_model (artifact, samples, classes,
                   sklearn_version)
               VALUES (%s, 60, 3, %s)""",
            (b"opaque-bytes", sklearn.__version__ + "-skew"))
        self.assertIsNone(model_categorize.tenant_model(self.conn))
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM categorizer_model").fetchone(),
            "version-skewed artifact left in place")
        # contrast: a SAME-version, properly sealed artifact that fails to
        # load is a runtime problem, not bad data — it must NOT be cleared
        self.conn.execute(
            """INSERT INTO categorizer_model (artifact, trained_at, samples,
                   classes, sklearn_version)
               VALUES (%s, now() - interval '1 hour', 60, 3, %s)""",
            (model_categorize.seal_artifact(self.conn, b"opaque-bytes"),
             sklearn.__version__))
        self.assertIsNone(model_categorize.tenant_model(self.conn))
        self.assertIsNotNone(
            self.conn.execute("SELECT 1 FROM categorizer_model").fetchone(),
            "same-version artifact wrongly cleared on load failure")

    def test_category_rename_counts_as_labeled_set_movement(self):
        """rename_category must trip the nightly staleness check — it
        changes neither the rule count nor (without the classified_at
        refresh) the newest timestamp, and the overlay would keep serving
        the old name forever."""
        self.conn.execute(
            """INSERT INTO merchant_categories (merchant, category_primary,
                   source, classified_at)
               VALUES ('Hobby Hut', 'Hobby Supplies', 'user',
                       now() - interval '7 days')""")
        # a stored model that matches the labeled set exactly: "current"
        self.conn.execute(
            """INSERT INTO categorizer_model (artifact, samples, classes,
                   sklearn_version)
               VALUES (%s, 1, 1, '')""", (b"x",))
        self.assertEqual("current",
                         model_train.maybe_train(self.conn)["status"])
        data.rename_category(self.conn, "Hobby Supplies", "ENTERTAINMENT")
        self.assertNotEqual(
            "current", model_train.maybe_train(self.conn)["status"],
            "rename left the overlay staleness check reporting current")

    def test_training_set_capped_to_most_recent_answers(self):
        """One bulk import must not buy an unbounded nightly fit: only the
        most recently answered merchants train."""
        prev = model_train.MAX_SAMPLES
        model_train.MAX_SAMPLES = 5
        try:
            for i in range(3):
                self.conn.execute(
                    """INSERT INTO merchant_categories (merchant,
                           category_primary, source, classified_at)
                       VALUES (%s, 'FOOD_AND_DRINK', 'user',
                               now() - interval '30 days')""",
                    (f"Stale Merchant {i}",))
            for i in range(5):
                self.conn.execute(
                    """INSERT INTO merchant_categories (merchant,
                           category_primary, source, classified_at)
                       VALUES (%s, 'MEDICAL', 'user', now())""",
                    (f"Fresh Merchant {i}",))
            pairs = model_train.training_pairs(self.conn)
            self.assertEqual(5, len(pairs))
            feats = " ".join(f for f, _ in pairs)
            self.assertNotIn("Stale Merchant", feats,
                             "cap kept old answers over recent ones")
            # a capped tenant with nothing new is still a cheap no-op:
            # the staleness check compares against the capped count
            self.conn.execute(
                """INSERT INTO categorizer_model (artifact, samples,
                       classes, sklearn_version)
                   VALUES (%s, 5, 2, '')""", (b"x",))
            self.assertEqual("current",
                             model_train.maybe_train(self.conn)["status"],
                             "capped tenant retrains every night")
        finally:
            model_train.MAX_SAMPLES = prev

    def test_features_use_each_merchants_newest_descriptor(self):
        """The sample bank string joined into the feature text is the
        merchant's most recent transaction name."""
        self.conn.execute(
            """INSERT INTO merchant_categories (merchant, category_primary,
                   source)
               VALUES ('Coffee Palace', 'FOOD_AND_DRINK', 'user')""")
        add_txn(self.conn, TODAY.replace(day=1), 4.50,
                "CP OLD DESCRIPTOR 001", merchant="Coffee Palace")
        add_txn(self.conn, TODAY, 5.25,
                "COFFEE PALACE #42 NYC", merchant="Coffee Palace")
        pairs = model_train.training_pairs(self.conn)
        feats = [f for f, _ in pairs if "Coffee Palace" in f]
        self.assertEqual(1, len(feats))
        self.assertIn("COFFEE PALACE #42 NYC", feats[0])
        self.assertNotIn("CP OLD DESCRIPTOR", feats[0])


if __name__ == "__main__":
    unittest.main()
