"""JSON API for the Retirement page + its settings keys.

GET /api/retirement wraps engine/retirement.project with the same parameter
clamping the page has always applied; /api/settings grows birthdate,
ss_estimates, retirement_saving and the two biweekly take-home keys.
"""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget, hist_returns, retirement

from .util import _ensure_db, seed_accounts, write_config


def _make_client() -> TestClient:
    import os
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    from oikonome.web.app import app
    return TestClient(app)


def _signup(client: TestClient, prefix: str) -> str:
    client.post("/api/signup", data={
        "email": f"{prefix}-{uuid.uuid4().hex[:8]}@example.dev",
        "password": "correct-horse-battery"})
    return client.get("/api/me").json()["tenant_id"]


class RetirementSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = _make_client()
        cls.tid = _signup(cls.client, "rst")
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def test_roundtrip_new_fields(self):
        r = self.client.post("/api/settings", json={
            "birthdate": "1975-03-04",
            "ss_estimates": {"67": 3000, "70": "4,100"},
            "retirement_saving": True,
            "income_biweekly_saving": 5200,
            "income_biweekly_not_saving": "$6,000.50"})
        self.assertEqual(r.status_code, 200)
        got = self.client.get("/api/settings").json()
        self.assertEqual(got["birthdate"], "1975-03-04")
        self.assertEqual(got["ss_estimates"], {"67": 3000.0, "70": 4100.0})
        self.assertTrue(got["retirement_saving"])
        self.assertEqual(got["income_biweekly_saving"], 5200.0)
        self.assertEqual(got["income_biweekly_not_saving"], 6000.5)
        # the biweekly scenario now drives budgeted income (toggle is on)
        self.assertEqual(budget.active_monthly_income(got), 10400.0)
        # empty values clear the keys (biweekly cleared → income falls back
        # to the static budgeted_income_monthly knob)
        r = self.client.post("/api/settings", json={
            "income_biweekly_saving": "", "ss_estimates": {},
            "birthdate": "", "retirement_saving": False})
        self.assertEqual(r.status_code, 200)
        got = self.client.get("/api/settings").json()
        self.assertIsNone(got["income_biweekly_saving"])
        self.assertIsNone(got["ss_estimates"])
        self.assertIsNone(got["birthdate"])
        self.assertFalse(got["retirement_saving"])

    def test_validation_rejects_junk_without_corrupting(self):
        for body in ({"birthdate": "not-a-date"},
                     {"birthdate": "2999-01-01"},          # future
                     {"ss_estimates": {"55": 100}},        # age out of range
                     {"ss_estimates": {"abc": 100}},
                     {"ss_estimates": ["not", "a", "dict"]},
                     {"ss_estimates": {"67": "lots"}},
                     {"income_biweekly_saving": "lots"},
                     {"income_biweekly_not_saving": "junk"}):
            r = self.client.post("/api/settings", json=body)
            self.assertEqual(r.status_code, 400, body)


class RetirementApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = _make_client()
        cls.tid = _signup(cls.client, "ret")
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)        # checking $5,000 / card $250
            write_config(conn)
        finally:
            conn.close()

    def test_age_override_param(self):
        # explicit no-birthdate state (order-independent), then a what-if age
        self.client.post("/api/settings", json={"birthdate": ""})
        r = self.client.get("/api/retirement?age=70&end=75")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["inputs"]["age"], 70)
        self.assertEqual(body["inputs"]["end"], 75)
        # rows run current age → retire_max (72)
        self.assertEqual([row["age"] for row in body["rows"]], [70, 71, 72])
        # without birthdate config, the default age is 45
        r = self.client.get("/api/retirement?end=50&spend=10000")
        self.assertEqual(r.json()["inputs"]["age"], 45)

    def test_param_clamping_never_500s(self):
        r = self.client.get("/api/retirement", params={
            "age": 200, "end": 300, "ssage": 99, "ret": 999, "infl": -999,
            "spend": -5, "stockpct": 150, "employer_mo": -10,
            "taxable_mo": -3, "resume": 95})
        self.assertEqual(r.status_code, 200)
        inp = r.json()["inputs"]
        self.assertEqual(inp["age"], 90)
        self.assertEqual(inp["end"], 110)
        self.assertEqual(inp["ssage"], 70)
        self.assertEqual(inp["ret"], 30.0)
        self.assertEqual(inp["infl"], -20.0)
        self.assertEqual(inp["spend"], 0)
        self.assertEqual(inp["stockpct"], 100)
        self.assertEqual(inp["employer_mo"], 0)
        self.assertEqual(inp["taxable_mo"], 0)
        self.assertEqual(inp["saving_yr"], 0)
        self.assertEqual(inp["resume"], 95)

    def test_projection_shape_and_birthdate_default(self):
        r = self.client.post("/api/settings", json={
            "birthdate": "1975-03-04", "ss_estimates": {"67": 3000}})
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/api/retirement")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for key in ("buckets", "rows", "earliest", "real_return", "spend",
                    "spend_now", "growth_path", "grow_to_67", "stress",
                    "stress_ref_age", "hist", "inputs"):
            self.assertIn(key, body)
        # $4,750 against the default target: no age works, so the page is
        # told what saving would make one work (a list, possibly empty)
        self.assertIsNone(body["earliest"])
        self.assertIsInstance(body["catch_up"], list)
        for c in body["catch_up"]:
            self.assertEqual(set(c), {"age", "extra_monthly"})
            self.assertGreater(c["extra_monthly"], 0)
        inp = body["inputs"]
        self.assertEqual(inp["age"],
                         retirement.age_from_birthdate("1975-03-04"))
        self.assertEqual(inp["your_ss"], 3000)
        self.assertTrue(inp["has_sched"])
        self.assertEqual(inp["spend"], 250000)          # the default
        # card debt nets out of cash: $5,000 − $250
        self.assertEqual(body["buckets"]["cash"], 4750.0)
        row = body["rows"][0]
        self.assertEqual(row["age"], inp["age"])
        self.assertEqual(set(row), {"age", "at_retire", "feasible",
                                    "fail_age", "max_spend", "hist_pct"})
        # historical replay covers every start year of the return series
        self.assertEqual(body["hist"]["n"], len(hist_returns.STOCK))
        for k in ("ok", "pct", "spend90", "spend100", "stock_frac"):
            self.assertIn(k, body["hist"])
        self.assertEqual(len(body["stress"]), 4)
        for s in body["stress"]:
            self.assertEqual(set(s), {"label", "earliest",
                                      "max_spend_at_ref"})
        self.assertTrue(body["growth_path"])            # [["YYYY-06", $], …]

    def test_cash_yield_and_birth_year_reach_the_projection(self):
        """`cash` is the cash bucket's nominal yield (default: keeps pace
        with inflation); the RMD start age follows the configured
        birthdate. Both are echoed so the page shows what was computed."""
        r = self.client.get("/api/retirement?age=60&end=70&infl=3")
        body = r.json()
        self.assertIsNone(body["inputs"]["cash"])
        self.assertEqual(body["cash_real_return"], 0.0)
        self.assertEqual(body["rmd_start"], 75)             # no birthdate
        r = self.client.get("/api/retirement?age=60&end=70&infl=3&cash=0")
        body = r.json()
        self.assertEqual(body["inputs"]["cash"], 0.0)
        self.assertAlmostEqual(body["cash_real_return"], 1 / 1.03 - 1, 6)
        self.client.post("/api/settings", json={"birthdate": "1955-06-01"})
        r = self.client.get("/api/retirement?end=80")
        self.assertEqual(r.json()["rmd_start"], 73)
        # clamped like every other what-if input
        r = self.client.get("/api/retirement?age=60&end=70&cash=999")
        self.assertEqual(r.json()["inputs"]["cash"], 30.0)

    def test_ssage_picks_schedule_row(self):
        self.client.post("/api/settings", json={
            "ss_estimates": {"67": 3000, "70": 4000}})
        r = self.client.get("/api/retirement?age=70&end=75&ssage=70")
        self.assertEqual(r.json()["inputs"]["your_ss"], 4000)
        # an ssage with no schedule entry means $0 SS in the sim
        r = self.client.get("/api/retirement?age=70&end=75&ssage=65")
        inp = r.json()["inputs"]
        self.assertEqual(inp["ssage"], 65)
        self.assertEqual(inp["your_ss"], 0)


if __name__ == "__main__":
    unittest.main()
