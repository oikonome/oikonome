"""An explicit $0 savings plan is a remembered choice.

The planner writes the `Savings` goal row on every save, $0 included: the
presence of the row is what tells the next visit "the user has spoken", so
the leftover is never silently re-prefilled into the field. That only works
if the save path ACCEPTS a zero-plan, zero-target row — the marker.

A rule demanding "a target or a monthly plan" rejects exactly that row, so
the $0 save 400s and the choice is never stored.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from .util import _ensure_db


def _goal(**kw):
    g = {"name": "Savings", "target": 0, "monthly_plan": 0,
         "tokens": [], "start_balance": 0}
    g.update(kw)
    return g


class ExcessCashMarkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"excess-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]

    def _goals(self):
        return self.client.get("/api/settings").json()["savings_goals"]

    def _save(self, *goals):
        return self.client.post("/api/settings",
                                json={"savings_goals": list(goals)})

    def test_zero_plan_savings_marker_persists(self):
        # the case: "I'm not planning savings" must survive the round
        # trip, or the choice is lost on every return.
        self.assertEqual(self._save(_goal()).status_code, 200)
        rows = [g for g in self._goals() if g["name"] == "Savings"]
        self.assertEqual(len(rows), 1, "the $0 marker row must be stored")
        self.assertEqual(rows[0]["monthly_plan"], 0)
        self.assertEqual(rows[0]["target"], 0)

    def test_marker_survives_alongside_real_goals(self):
        # a real goal must not be what keeps the marker alive — and the
        # marker must not swallow the real goal's numbers
        self.assertEqual(self._save(
            _goal(), _goal(name="Trip", target=4000, monthly_plan=250),
        ).status_code, 200)
        by = {g["name"]: g for g in self._goals()}
        self.assertEqual(by["Savings"]["monthly_plan"], 0)
        self.assertEqual(by["Trip"]["monthly_plan"], 250)
        self.assertEqual(by["Trip"]["target"], 4000)

    def test_zero_plan_goal_is_plan_only_with_no_progress(self):
        # the plan-only rule still holds for the marker: no account_id and no tokens means
        # it must never claim credit for ledger-wide transfers
        self._save(_goal())
        row = next(g for g in self.client.get("/api/today/full").json()
                   ["savings_goals"] if g["name"] == "Savings")
        self.assertTrue(row["plan_only"])
        self.assertIsNone(row["saved"])

    def test_unnamed_goal_still_rejected(self):
        # loosening the target/plan rule must not open the door to junk
        self.assertEqual(self._save(_goal(name="")).status_code, 400)

    def test_blank_plan_and_no_target_still_rejected(self):
        # the Settings form sends "" for an untouched field and has no
        # client-side guard — that 400 IS its error message. An explicit
        # 0 is an answer; a blank one is not.
        self.assertEqual(self._save(
            _goal(name="Trip", monthly_plan="")).status_code, 400)
        self.assertEqual(self._save(
            _goal(name="Trip", monthly_plan="0")).status_code, 200)

    def test_goal_with_plan_omitted_entirely_still_rejected(self):
        g = _goal(name="Trip")
        del g["monthly_plan"]
        self.assertEqual(self._save(g).status_code, 400)

    def test_zero_plan_marker_adds_nothing_to_excess_cash(self):
        # the marker records a choice; it is not a $0 line item. Excess cash
        # = income - typical bills - budgets - savings plan, so a $0 plan
        # must leave it whole and a real plan must move it dollar-for-dollar.
        self.client.post("/api/settings", json={
            "budgeted_income_monthly": 5000,
            "food_monthly": 1000, "other_monthly": 500})
        self._save(_goal())
        with_marker = self.client.get("/api/today/full").json()["plan_surplus"]
        self.assertAlmostEqual(with_marker, 3500, places=2)
        self._save(_goal(monthly_plan=300))
        with_plan = self.client.get("/api/today/full").json()["plan_surplus"]
        self.assertAlmostEqual(with_plan, 3200, places=2)


if __name__ == "__main__":
    unittest.main()
