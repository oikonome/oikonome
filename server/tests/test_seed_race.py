"""load_config's seed-persist must not revert a concurrent settings save.

An unlocked whole-document write of a snapshot read BEFORE the (slow)
3-month seed aggregation silently reverts any save that lands during the
aggregation: the wizard step un-completes, the just-added recipient
vanishes. Every caller reads, locks and merges, and the persist is a
caller like the rest.

So the persist takes the row lock, RE-READS, and merges the seed into the
FRESH document — seed fills gaps, the concurrent write wins.
"""

import unittest
import uuid
from unittest import mock

from oikonome.db import tenancy
from oikonome.engine import budget

from .util import TODAY, _admin_dsn, _ensure_db, TEST_DB, add_txn


class SeedPersistRaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"seed-{uuid.uuid4().hex[:8]}"))
        conn = tenancy.tenant_connect(self.tid)
        try:
            # real spend so the seed is non-zero and the persist branch runs
            conn.execute(
                "INSERT INTO items (id, aggregator, institution_name, "
                "access_token) VALUES ('it1','test','Test Bank','tok')")
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,"
                "balance_current) VALUES "
                "('chk','it1','Chk','depository','checking',5000)")
            add_txn(conn, TODAY, 400.0, "SAFEWAY", primary="FOOD_AND_DRINK",
                    account="chk")
            # a config WITHOUT budgets — the exact onboarding window
            conn.execute(
                """INSERT INTO tenant_settings (config)
                   VALUES ('{"wizard_steps": {"connect": "done"}}')
                   ON CONFLICT (tenant_id) DO UPDATE
                     SET config = EXCLUDED.config""")
        finally:
            conn.close()

    def tearDown(self):
        self.admin.close()

    def test_concurrent_save_survives_the_seed_persist(self):
        conn = tenancy.tenant_connect(self.tid)
        self.addCleanup(conn.close)
        real_seed = budget._seed_config
        fired = []

        def slow_seed(c, excluded=None, **kw):
            out = real_seed(c, excluded=excluded)
            if fired:            # config_txn re-enters load_config → once
                return out
            fired.append(True)
            # a settings save lands WHILE the aggregation runs, on its own
            # connection — a pre-seed snapshot written back afterwards
            # would clobber it
            other = tenancy.tenant_connect(self.tid)
            try:
                with budget.config_txn(other) as cfg2:
                    cfg2.setdefault("wizard_steps", {})["sync"] = "done"
                    cfg2["email_recipients"] = ["fam@example.dev"]
            finally:
                other.close()
            return out

        with mock.patch.object(budget, "_seed_config", slow_seed):
            merged = budget.load_config(conn)
        # the seed landed…
        self.assertGreater(merged.get("food_monthly", 0), 0)
        # …and the concurrent save was NOT reverted
        stored = tenancy.tenant_connect(self.tid)
        try:
            cfg = budget.load_config(stored)
        finally:
            stored.close()
        self.assertEqual(cfg.get("email_recipients"), ["fam@example.dev"],
                         "the seed persist reverted a concurrent save")
        self.assertEqual(cfg.get("wizard_steps", {}).get("sync"), "done")
        self.assertEqual(cfg.get("wizard_steps", {}).get("connect"), "done")
        self.assertGreater(cfg.get("food_monthly", 0), 0)


if __name__ == "__main__":
    unittest.main()
