"""Every Retirement, Debt and continuity door is called at least once and its
reply matches the shape it documents (web/schemas/retirement.py,
web/schemas/continuity.py).

The conformance hook (tests/api_conformance.py) does the validating; this
module makes sure each door is reached with a household that fills its
shape — a card and a loan so the debt rows and schedules are not empty, a
retirement account so the buckets carry money, the projection both with a
spend and with none to plan for — and that the bodies the released clients
send, older shapes included, are bodies the documented model accepts.
"""

from __future__ import annotations

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web.schemas.continuity import ContinuityEmail, ContinuitySave
from oikonome.web.schemas.retirement import DebtPlanSave

from . import api_conformance
from .util import _ensure_db, seed_accounts, write_config

PW = "correct-horse-battery"


class ShapesHoldOnEveryDoor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.email = f"shape-{uuid.uuid4().hex[:8]}@example.dev"
        r = cls.client.post("/api/signup", data={"email": cls.email,
                                                 "password": PW})
        assert r.status_code == 200, r.text
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn, birthdate="1980-05-04")
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,"
                "balance_current) VALUES "
                "('k401','it1','Work Plan','investment','401k',120000),"
                "('auto','it1','Car Loan','loan','auto',9000)")
        finally:
            conn.close()

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    def _ok(self, method, path, **kw):
        before = api_conformance.validated()
        r = self.client.request(method, "/api" + path, **kw)
        self.assertEqual(r.status_code, 200, r.text)
        after = api_conformance.validated()
        self.assertGreater(sum(after.values()), sum(before.values()),
                           f"{method} {path}: the reply was not validated")
        return r.json()

    def test_retirement_with_a_spend_and_with_none(self):
        planned = self._ok("GET", "/retirement?spend=40000&employer_mo=500")
        self.assertTrue(planned["rows"])
        self.assertIsNotNone(planned["hist"])
        self.assertEqual(planned["inputs"]["spend_source"], "what_if")
        nothing = self._ok("GET", "/retirement?ask_spend=1")
        self.assertEqual(nothing["inputs"]["spend_source"], "needed")
        self.assertEqual(nothing["rows"], [])
        older = self._ok("GET", "/retirement")       # an older phone's ask
        self.assertEqual(older["inputs"]["spend_source"], "needed")
        self.assertTrue(older["rows"])

    def test_debt_plan_read_and_save(self):
        plan = self._ok("GET", "/debt-plan?method=snowball&extra=100")
        self.assertEqual({d["id"] for d in plan["debts"]}, {"card", "auto"})
        self._ok("POST", "/debt-plan", json={
            "method": "avalanche", "extra_monthly": "50",
            "debts": {"card": {"apr": "24.9", "min_payment": None,
                               "skip": False}}})
        saved = self._ok("GET", "/debt-plan")
        self.assertIn("card", saved["overrides"])

    def test_continuity_read_save_and_email(self):
        self._ok("GET", "/continuity")
        self._ok("PUT", "/continuity", json={
            "note": "The papers are in the hall desk.",
            "prepared_for": "Robin"})
        self._ok("PUT", "/continuity", json={"note": ""})
        with mock.patch.dict(os.environ, {"OIKONOME_SMTP_HOST": "smtp.test",
                                          "OIKONOME_HOSTED": ""}), \
                mock.patch("oikonome.web.report.send"):
            self._ok("POST", "/continuity/email",
                     json={"to": self.email, "password": PW})


class ShippedBodiesAreDocumentedBodies(unittest.TestCase):
    """The bodies the released clients send (web and mobile api wrappers,
    older builds included) validate against the documented body models, so
    writing a model down never narrowed what an installed client may send."""

    def test_debt_plan_bodies(self):
        for body in (
                {"method": "snowball", "extra_monthly": 150,
                 "debts": {"card": {"apr": 19.99, "min_payment": "35",
                                    "skip": True}}},
                {"extra_monthly": ""}, {},
                {"debts": {"card": {"apr": "", "min_payment": None}}}):
            with self.subTest(body=body):
                DebtPlanSave.model_validate(body)
        # and the two the door refuses, the model refuses too
        for body in ({"extra_monthly": None}, {"method": None}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                DebtPlanSave.model_validate(body)

    def test_continuity_bodies(self):
        for body in ({"note": "x", "prepared_for": "Robin"},
                     {"note": "", "prepared_for": ""}, {"note": "x"}):
            ContinuitySave.model_validate(body)
        for body in ({"to": "a@example.dev"},
                     {"to": "a@example.dev", "password": PW,
                      "totp_code": "", "recovery_code": ""}):
            ContinuityEmail.model_validate(body)


if __name__ == "__main__":
    unittest.main()
