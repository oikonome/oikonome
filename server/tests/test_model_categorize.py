"""The local merchant classifier (categorization fallback when the
aggregator doesn't know): runs after the seed and before any LLM, writes
source='model' rules only above its confidence floor, abstains below it,
degrades to absent without a model file, and its rules obey the same
trust guard and user-rule supremacy as every machine source."""

import os
import sys
import types
import unittest
from unittest import mock

from oikonome.engine import llm_categorize, model_categorize

from .util import add_txn, make_db, write_config


class FakeModel:
    """predict_proba stand-in: maps feature text keywords → (class, conf)."""
    classes_ = ["FOOD_AND_DRINK", "MEDICAL", "TRANSPORTATION"]

    def __init__(self, answers):
        self.answers = answers          # substring → (class_idx, conf)

    def predict_proba(self, feats):
        rows = []
        for f in feats:
            row = [0.0] * len(self.classes_)
            for key, (idx, conf) in self.answers.items():
                if key.lower() in f.lower():
                    row[idx] = conf
                    break
            else:
                row[0] = 0.34            # nothing matched → low everywhere
            rows.append(row)
        return _Arr(rows)


class _Arr(list):
    """the two ndarray behaviours classify() uses: iteration and argmax."""
    def __iter__(self):
        for row in super().__iter__():
            yield _Row(row)


class _Row(list):
    def argmax(self):
        return self.index(max(self))

    def __getitem__(self, i):
        return super().__getitem__(i)


def _install(model):
    model_categorize._cache["path"] = "/fake/model.joblib"
    model_categorize._cache["model"] = model
    return mock.patch.dict(os.environ,
                           {"OIKONOME_CATEGORIZER_MODEL": "/fake/model.joblib"})


def _exists_fake(path):
    return path == "/fake/model.joblib" or os.path.exists(path)


class ModelPassTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()
        model_categorize._cache["path"] = None
        model_categorize._cache["model"] = None

    def _pend(self, name, amount=25.0, primary=None, raw=None):
        add_txn(self.conn, "2026-07-01", amount, name, primary=primary)

    def test_confident_prediction_becomes_a_model_rule(self):
        add_txn(self.conn, "2026-07-01", 25.0, "Cedar Creamery",
                primary="GENERAL_MERCHANDISE")
        fake = FakeModel({"cedar": (0, 0.92)})
        with _install(fake), \
             mock.patch.object(model_categorize.os.path, "exists",
                               _exists_fake):
            stats: dict = {}
            n = llm_categorize.model_pass(self.conn, stats)
        self.assertEqual(n, 1)
        row = self.conn.execute(
            "SELECT category_primary, source FROM merchant_categories "
            "WHERE merchant='Cedar Creamery'").fetchone()
        self.assertEqual(row["category_primary"], "FOOD_AND_DRINK")
        self.assertEqual(row["source"], "model")
        # and the rule applies to the generic row like any machine rule
        llm_categorize.apply(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT category_primary FROM transactions LIMIT 1"
        ).fetchone()["category_primary"], "FOOD_AND_DRINK")

    def test_below_threshold_abstains_and_stays_pending(self):
        add_txn(self.conn, "2026-07-01", 25.0, "Mystery Vendor",
                primary="GENERAL_MERCHANDISE")
        fake = FakeModel({"mystery": (1, 0.40)})
        with _install(fake), \
             mock.patch.object(model_categorize.os.path, "exists",
                               _exists_fake):
            n = llm_categorize.model_pass(self.conn, {})
        self.assertEqual(n, 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM merchant_categories").fetchone()["n"],
            0)
        pend = {m["merchant"]
                for m in llm_categorize.pending_merchants(self.conn)}
        self.assertIn("Mystery Vendor", pend,
                      "an abstention must leave the merchant for the LLM")

    def test_no_model_file_is_a_noop(self):
        add_txn(self.conn, "2026-07-01", 25.0, "Anything",
                primary="GENERAL_MERCHANDISE")
        self.assertEqual(llm_categorize.model_pass(self.conn, {}), 0)

    def test_model_rule_takes_the_trust_guard(self):
        """source='model' is a machine source: it may refine a generic
        bucket but never move a row out of a concrete Plaid family."""
        add_txn(self.conn, "2026-07-01", 40.0, "Concrete Cafe",
                primary="FOOD_AND_DRINK")
        self.conn.execute(
            "UPDATE transactions SET category_plaid='FOOD_AND_DRINK', "
            "category_plaid_confidence='LOW'")
        self.conn.execute(
            "INSERT INTO merchant_categories (merchant, category_primary, "
            "source) VALUES ('Concrete Cafe', 'MEDICAL', 'model')")
        self.assertEqual(llm_categorize.apply(self.conn), 0)
        self.assertEqual(self.conn.execute(
            "SELECT category_primary FROM transactions LIMIT 1"
        ).fetchone()["category_primary"], "FOOD_AND_DRINK")

    def test_user_rule_outranks_a_model_rule(self):
        add_txn(self.conn, "2026-07-01", 25.0, "Corner Shop",
                primary="GENERAL_MERCHANDISE")
        self.conn.execute(
            "INSERT INTO merchant_categories (merchant, category_primary, "
            "source) VALUES ('Corner Shop', 'FOOD_AND_DRINK', 'user')")
        fake = FakeModel({"corner": (1, 0.99)})
        with _install(fake), \
             mock.patch.object(model_categorize.os.path, "exists",
                               _exists_fake):
            llm_categorize.model_pass(self.conn, {})
        # the user's rule row is untouched (model INSERT is DO NOTHING)
        row = self.conn.execute(
            "SELECT category_primary, source FROM merchant_categories "
            "WHERE merchant='Corner Shop'").fetchone()
        self.assertEqual(row["source"], "user")
        self.assertEqual(row["category_primary"], "FOOD_AND_DRINK")

    def test_missing_sklearn_stack_disables_the_backend(self):
        """A box without the optional `oikonome[model]` extra: a configured
        model path degrades to "feature absent" — no crash, the reason
        logged, and available() stops claiming a classifier the process
        cannot run (the Settings card reads it)."""
        import sys
        add_txn(self.conn, "2026-07-01", 25.0, "Anything",
                primary="GENERAL_MERCHANDISE")
        env = {"OIKONOME_CATEGORIZER_MODEL": "/fake/model.joblib"}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(model_categorize.os.path, "exists",
                               _exists_fake), \
             mock.patch.dict(sys.modules, {"joblib": None}):
            with self.assertLogs(model_categorize.log, "WARNING") as cm:
                self.assertEqual(llm_categorize.model_pass(self.conn, {}), 0)
            self.assertIn("model backend unavailable", "\n".join(cm.output))
            self.assertFalse(model_categorize.available())

    def test_broken_artifact_degrades_to_absent(self):
        add_txn(self.conn, "2026-07-01", 25.0, "Anything",
                primary="GENERAL_MERCHANDISE")

        class Boom:
            classes_ = ["FOOD_AND_DRINK"]

            def predict_proba(self, feats):
                raise ValueError("version skew")

        with _install(Boom()), \
             mock.patch.object(model_categorize.os.path, "exists",
                               _exists_fake):
            self.assertEqual(llm_categorize.model_pass(self.conn, {}), 0)


class _OverlayConn:
    """Fake tenant connection: serves the categorizer_model row that
    tenant_model() reads (probe + full fetch use the same row)."""

    def __init__(self, tenant_id, trained_at):
        self._row = {"tenant_id": tenant_id, "trained_at": trained_at,
                     "artifact": b"\x00", "sklearn_version": ""}

    def execute(self, sql):
        row = dict(self._row)

        class Res:
            def fetchone(self):
                return row
        return Res()


class TenantOverlayCacheTests(unittest.TestCase):
    """The per-tenant overlay cache must stay bounded in a long-lived
    worker serving arbitrarily many tenants: least-recently-used entries
    fall out at the cap, recently-used tenants survive, and a retrain
    still replaces (never duplicates) a tenant's entry."""

    def setUp(self):
        model_categorize._tenant_cache.clear()
        joblib_stub = types.ModuleType("joblib")
        joblib_stub.load = lambda f: object()      # fresh model per load
        self._mods = mock.patch.dict(sys.modules, {"joblib": joblib_stub})
        self._mods.start()

    def tearDown(self):
        self._mods.stop()
        model_categorize._tenant_cache.clear()

    def _load(self, tenant, trained_at="t1"):
        return model_categorize.tenant_model(_OverlayConn(tenant, trained_at))

    def test_cache_is_bounded_and_evicts_least_recently_used(self):
        cap = 8
        with mock.patch.object(model_categorize, "_TENANT_CACHE_MAX", cap):
            for i in range(cap):
                self.assertIsNotNone(self._load(f"tenant-{i:03d}"))
            self.assertEqual(len(model_categorize._tenant_cache), cap)
            # touch the oldest tenant: a cache HIT must refresh its recency
            first = self._load("tenant-000")
            self.assertIs(first,
                          model_categorize._tenant_cache[("tenant-000", "t1")])
            # one past the cap: tenant-001 is now least recently used
            self._load("tenant-new")
            self.assertEqual(len(model_categorize._tenant_cache), cap)
            keys = {k[0] for k in model_categorize._tenant_cache}
            self.assertNotIn("tenant-001", keys,
                             "the least-recently-used tenant must be evicted")
            self.assertIn("tenant-000", keys,
                          "a recently re-used tenant must NOT be evicted")
            self.assertIn("tenant-new", keys)

    def test_cache_hit_returns_the_cached_model(self):
        m1 = self._load("tenant-a")
        self.assertIs(self._load("tenant-a"), m1,
                      "same (tenant, trained_at) must be served from cache")

    def test_retrain_replaces_the_tenants_entry(self):
        m1 = self._load("tenant-a", trained_at="t1")
        m2 = self._load("tenant-a", trained_at="t2")
        self.assertIsNot(m2, m1, "a new trained_at must reload the artifact")
        self.assertEqual(len(model_categorize._tenant_cache), 1,
                         "retrain replaces the entry, never duplicates it")
        self.assertIn(("tenant-a", "t2"), model_categorize._tenant_cache)
        self.assertNotIn(("tenant-a", "t1"), model_categorize._tenant_cache)

    def test_default_cap_is_small(self):
        self.assertEqual(model_categorize._TENANT_CACHE_MAX, 256)


if __name__ == "__main__":
    unittest.main()
