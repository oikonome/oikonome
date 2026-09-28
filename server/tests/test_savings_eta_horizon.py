"""A savings goal's ETA is bounded, and one pathological goal never takes
the others down with it.

A big target on a cents-a-month rate is a century-scale ETA; unbounded,
timedelta overflows and the exception — caught only at the caller — would
wipe EVERY goal from the Today page and the daily email.
"""

import datetime as dt
import unittest

from oikonome.engine import budget, savings

from .util import make_db, write_config


class SavingsEtaHorizonTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_huge_target_on_tiny_rate_yields_a_capped_eta_not_a_crash(self):
        # a healthy goal beside the pathological one must still be reported
        cfg = budget.load_config(self.conn)
        cfg["savings_goals"] = [
            {"name": "House", "target": 500_000, "account_id": "chk"},
            {"name": "Trip", "target": 1_000, "account_id": "chk"},
        ]
        budget.save_config(self.conn, cfg)
        today = dt.date(2026, 8, 30)
        with __import__("unittest.mock").mock.patch.object(
                savings, "matched_net", return_value=0.03):
            out = savings.progress(self.conn, budget.load_config(self.conn),
                                   today)
        names = {g["name"] for g in out}
        self.assertEqual(names, {"House", "Trip"})
        house = next(g for g in out if g["name"] == "House")
        eta = dt.date.fromisoformat(house["eta"])
        self.assertLessEqual((eta - today).days, 365 * 100 + 1)


if __name__ == "__main__":
    unittest.main()
