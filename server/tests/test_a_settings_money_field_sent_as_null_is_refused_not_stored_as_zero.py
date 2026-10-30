"""A money field the client could not read must never be stored as 0.

A web or phone form that cannot parse a typed amount (a stray trailing
point, a comma in the wrong place) holds NaN, and JSON carries NaN as
null. The settings door used to read that null as 0, so the save
succeeded and the person's figure was silently replaced by zero. A blank
box is still an answer of 0; a null for a field that was sent is refused
with a 400 and nothing is written.
"""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config


def _make_client() -> TestClient:
    import os
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    from oikonome.web.app import app
    return TestClient(app)


class NullMoneyFieldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = _make_client()
        cls.client.post("/api/signup", data={
            "email": f"nullmoney-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def _post(self, body):
        return self.client.post("/api/settings", json=body)

    def test_a_null_money_field_is_refused_and_nothing_is_written(self):
        r = self._post({
            "food_monthly": 640,
            "custom_buckets": [{"name": "Coffee", "parent": "food",
                                "monthly": 55, "categories": [],
                                "merchants": []}],
            "savings_goals": [{"name": "Trip", "target": 2400,
                               "monthly_plan": 200, "tokens": [],
                               "start_balance": 100}],
            "income_scenarios": [{"name": "Base", "take_home": 2100,
                                  "cadence": "biweekly"}]})
        self.assertEqual(r.status_code, 200, r.text)
        bodies = (
            {"food_monthly": None},
            {"other_monthly": None},
            {"budgeted_income_monthly": None},
            {"income_biweekly_saving": None},
            {"custom_buckets": [{"name": "Coffee", "parent": "food",
                                 "monthly": None, "categories": [],
                                 "merchants": []}]},
            {"savings_goals": [{"name": "Trip", "target": None,
                                "monthly_plan": 200, "tokens": []}]},
            {"savings_goals": [{"name": "Trip", "target": 2400,
                                "monthly_plan": None, "tokens": []}]},
            {"savings_goals": [{"name": "Trip", "target": 2400,
                                "monthly_plan": 200, "tokens": [],
                                "start_balance": None}]},
            {"income_scenarios": [{"name": "Base", "take_home": None,
                                   "cadence": "biweekly"}]},
            {"ss_estimates": {"67": None}},
        )
        for body in bodies:
            self.assertEqual(self._post(body).status_code, 400, body)
        got = self.client.get("/api/settings").json()
        self.assertEqual(got["food_monthly"], 640.0)
        coffee = next(b for b in got["custom_buckets"]
                      if b["name"] == "Coffee")
        self.assertEqual(coffee["monthly"], 55.0)
        trip = next(g for g in got["savings_goals"] if g["name"] == "Trip")
        self.assertEqual((trip["target"], trip["monthly_plan"],
                          trip["start_balance"]), (2400.0, 200.0, 100.0))
        self.assertEqual(got["income_scenarios"][0]["take_home"], 2100.0)

    def test_a_blank_or_absent_money_field_still_reads_as_zero(self):
        r = self._post({
            "other_monthly": "",
            "custom_buckets": [{"name": "Pets", "parent": "other",
                                "categories": [], "merchants": []}],
            "savings_goals": [{"name": "Rainy day", "target": "",
                               "monthly_plan": 50, "tokens": []}]})
        self.assertEqual(r.status_code, 200, r.text)
        got = self.client.get("/api/settings").json()
        self.assertEqual(got["other_monthly"], 0.0)
        pets = next(b for b in got["custom_buckets"] if b["name"] == "Pets")
        self.assertEqual(pets["monthly"], 0.0)
        goal = next(g for g in got["savings_goals"]
                    if g["name"] == "Rainy day")
        self.assertEqual((goal["target"], goal["start_balance"]), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
