"""First-run budget seeding from observed spend.

The seed must respect accounts the owner already excluded, and a
zero seed taken before any spend has synced must not be locked in —
the next load with real data re-seeds."""

import unittest

from .util import add_txn, make_db


class BudgetSeedTests(unittest.TestCase):
    def test_seed_honors_already_excluded_accounts(self):
        from oikonome.engine import budget
        conn = make_db()
        try:
            # a side account with heavy spend, flagged excluded BEFORE budgets
            # seed (only excluded_accounts in config, no food/other yet)
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,"
                "balance_current) VALUES "
                "('side','it1','Side','depository','checking',0)")
            for d in ("2026-04-05", "2026-05-05", "2026-06-05"):
                add_txn(conn, d, 900.0, "BIG THING", account="side",
                        primary="GENERAL_MERCHANDISE")
            budget.save_config(conn, {"excluded_accounts": ["side"]})
            cfg = budget.load_config(conn)   # triggers seed
            seeded_other = cfg["other_monthly"]
            # now compare to a seed that did NOT exclude
            conn2 = make_db()
            try:
                conn2.execute(
                    "INSERT INTO accounts (id,item_id,name,type,subtype,"
                    "balance_current) VALUES "
                    "('side','it1','Side','depository','checking',0)")
                for d in ("2026-04-05", "2026-05-05", "2026-06-05"):
                    add_txn(conn2, d, 900.0, "BIG THING", account="side",
                            primary="GENERAL_MERCHANDISE")
                cfg2 = budget.load_config(conn2)
                self.assertLess(seeded_other, cfg2["other_monthly"],
                                "excluded account still inflated the seed")
            finally:
                conn2.close()
        finally:
            conn.close()

    def test_seed_not_persisted_before_spend_then_reseeds(self):
        from oikonome.engine import budget
        conn = make_db()   # accounts only, no transactions yet
        try:
            budget.load_config(conn)   # must not persist food/other=0
            row = conn.execute(
                "SELECT config FROM tenant_settings").fetchone()
            persisted = (row["config"] if row else {}) or {}
            self.assertLessEqual(persisted.get("food_monthly", 0), 0,
                                 "zero seed must NOT be locked in before data")
            # spend arrives (as a later sync would) → real seed, persisted
            for d in ("2026-04-05", "2026-05-05", "2026-06-05"):
                add_txn(conn, d, 600.0, "GROCERY", account="card",
                        primary="FOOD_AND_DRINK")
                add_txn(conn, d, 300.0, "STUFF", account="card",
                        primary="GENERAL_MERCHANDISE")
            cfg2 = budget.load_config(conn)
            self.assertGreater(cfg2["food_monthly"], 0)
            self.assertGreater(cfg2["other_monthly"], 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
