"""Every script-token, support-access, reimbursement, assistant and
notification door is called and its reply matches the shape it documents
(web/schemas/access.py, reimburse.py, assistant.py, notify.py).

The conformance hook (tests/api_conformance.py) validates each reply; this
module reaches every door itself, so the shapes stay checked whatever the
rest of the suite happens to call. Outside services are stood in for: the
SMS and mail senders, the push sender, and the assistant's model.
"""

from __future__ import annotations

import datetime as dt
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from . import api_conformance
from .push_keys import browser_keys
from .util import _ensure_db, add_txn, seed_accounts, write_config

PW = "correct-horse-battery"
TODAY = dt.date.today()
SMS_ENV = {"OIKONOME_TWILIO_ACCOUNT_SID": "AC1",
           "OIKONOME_TWILIO_AUTH_TOKEN": "t",
           "OIKONOME_TWILIO_FROM": "+12175550000"}
ENDPOINT = "https://fcm.googleapis.com/fcm/send/shape-"


def _canned_answer(conn, question, context=None):
    return {"answer": "About forty dollars.",
            "blocks": [{"kind": "text", "text": "About forty dollars."},
                       {"kind": "bar", "title": "Coffee by month",
                        "labels": ["Jan", "Feb"],
                        "series": [{"name": "Coffee",
                                    "values": [41.5, 38]}]},
                       {"kind": "table", "title": "Top merchants",
                        "columns": ["Merchant", "Spent"],
                        "rows": [["Copper Kettle", 41.5]]}],
            "tools_used": ["spending_by"]}


class TokenReimburseAssistantNotifyDoorShapes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.email = f"s3shape-{uuid.uuid4().hex[:8]}@example.dev"
        r = cls.client.post("/api/signup", data={"email": cls.email,
                                                 "password": PW})
        assert r.status_code == 200, r.text
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            cls.charge = add_txn(conn, TODAY - dt.timedelta(days=3), 80.0,
                                 "CITY CLINIC", merchant="City Clinic",
                                 account="card")
            cls.deposit = add_txn(conn, TODAY, -80.0, "INSURER REFUND",
                                  merchant="Insurer", account="chk")
        finally:
            conn.close()

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    def _ok(self, method, path, **kw):
        before = sum(api_conformance.validated().values())
        r = self.client.request(method, "/api" + path, **kw)
        self.assertEqual(r.status_code, 200, f"{method} {path}: {r.text}")
        self.assertGreater(sum(api_conformance.validated().values()), before,
                           f"{method} {path}: the reply was not validated")
        return r.json()

    def _elevate(self):
        r = self.client.post("/api/auth/elevate", json={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)

    def test_tokens_and_support_access(self):
        self._elevate()
        minted = self._ok("POST", "/tokens", json={"name": "collector",
                                                   "scope": "push"})
        self._ok("GET", "/tokens")
        self._ok("POST", "/tokens/revoke", json={"id": minted["id"]})
        self._ok("GET", "/support-access")
        self._ok("POST", "/support-access", json={"hours": 2,
                                                  "reason": "a question"})
        granted = self._ok("GET", "/support-access")
        self.assertTrue(granted["granted"])
        self._ok("POST", "/support-access/revoke", json={})

    def test_reimbursement_pairing(self):
        cid, did = self.charge, self.deposit
        self._ok("POST", f"/reimburse/{cid}/flag", json={"partial": False})
        pending = self._ok("GET", "/reimburse/pending")
        self.assertTrue(pending["pending"])
        cands = self._ok("GET", f"/reimburse/{cid}/candidates")
        self.assertTrue(cands["candidates"])
        self._ok("POST", "/reimburse/link",
                 json={"txn_id": cid, "other_ids": [did]})
        self.assertTrue(self._ok("GET", f"/reimburse/{cid}/candidates")
                        ["pairs"])
        self.assertTrue(self._ok("GET", "/reimburse/pairs")["pairs"])
        self._ok("POST", "/reimburse/unlink",
                 json={"expense_id": cid, "reimburse_id": did})
        self._ok("POST", f"/reimburse/{cid}/unflag", json={})

    def test_the_assistant_conversation(self):
        from oikonome.engine import assistant
        self._ok("GET", "/assistant")
        with mock.patch.object(assistant, "ask", _canned_answer), \
                mock.patch.object(assistant, "available", return_value=True):
            first = self._ok("POST", "/assistant",
                             json={"question": "What did coffee cost?"})
            self._ok("POST", "/assistant", json={
                "question": "And last month?",
                "thread_id": first["thread_id"]})
        hist = self._ok("GET", "/assistant/history?limit=1")
        self.assertTrue(hist["more"])
        self.assertIsNotNone(hist["next"])
        # the opening turn stays while its follow-up remains, and says why
        kept = self._ok("DELETE", f"/assistant/history/{first['id']}")
        self.assertTrue(kept["reason"])
        self._ok("DELETE", f"/assistant/threads/{first['thread_id']}")
        self._ok("DELETE", "/assistant/history")

    def test_notification_channels_and_the_send_now_email(self):
        self._ok("GET", "/notify")
        sent = []
        with mock.patch.dict(os.environ, SMS_ENV), \
                mock.patch("oikonome.notify.sms.send",
                           side_effect=lambda to, text:
                           sent.append(text) or True):
            self._elevate()
            self._ok("POST", "/notify/phone", json={
                "number": "+12175551234", "consent": ["summary"]})
            code = sent[-1].rsplit(" ", 1)[-1]
            self._ok("POST", "/notify/phone/verify", json={"code": code})
            self._ok("POST", "/notify/test", json={"channel": "sms"})
            self._ok("POST", "/notify/phone",
                     json={"number": "", "consent": []})
        endpoint = ENDPOINT + uuid.uuid4().hex
        self._ok("POST", "/notify/push/subscribe", json={
            "endpoint": endpoint, "keys": browser_keys(),
            "user_agent": "test"})
        self._ok("POST", "/notify/push/unsubscribe",
                 json={"endpoint": endpoint})
        from oikonome.web import report
        with mock.patch.dict(os.environ, {"OIKONOME_SMTP_HOST": "smtp.test",
                                          "OIKONOME_HOSTED": ""}), \
                mock.patch.object(report, "send"):
            self._ok("POST", "/jobs/email", json={})


if __name__ == "__main__":
    unittest.main()
