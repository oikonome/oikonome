"""The Jinja email-settings door carries the same policy as the API.

POST /api/settings enforces two policies on outbound-mail config: hosted
recipients must hold an account on the tenant, and demo instances refuse
recipient keys outright. The transitional Jinja form POST /settings/email
writes the SAME config key, so it must enforce both as well; a door that
accepted free-form recipients would exfiltrate the daily verdict to any
address a stolen session could type.

The worker's hourly sweep is the matching demo invariant: Doctor tells
demo tenants "email disabled — demo instance", and email_tenant must
agree rather than building and sending anyway.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget

from .util import (_ensure_db, accept_recipient_invite, add_bill,
                   add_txn, make_db, write_config)

PW = "correct-horse-battery"


def _signed_in_client():
    import oikonome.web.app as appmod
    from oikonome.web import security
    security._limiter._hits.clear()      # signup limit is 5/h per IP
    appmod.DEV_MODE = True
    client = TestClient(appmod.app)
    email = f"fauth-{uuid.uuid4().hex[:10]}@example.dev"
    r = client.post("/api/signup", data={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    tid = client.get("/api/me").json()["tenant_id"]
    from .util import give_totp_factor
    give_totp_factor(email)   # hosted writes require an enrolled factor
    return client, tid, email


def _set_demo(tid):
    from oikonome.web import demoguard
    conn = tenancy.tenant_connect(tid)
    try:
        cfg = budget.load_config(conn)
        cfg["demo_mode"] = True
        budget.save_config(conn, cfg)
    finally:
        conn.close()
    demoguard._cache.clear()


def _saved_recipients(tid):
    conn = tenancy.tenant_connect(tid)
    try:
        return budget.load_config(conn).get("email_recipients")
    finally:
        conn.close()


class JinjaEmailRecipientPolicyTests(unittest.TestCase):
    """The form path carries the hosted member-only rule."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()

    def setUp(self):
        self.client, self.tid, self.email = _signed_in_client()

    def _post(self, recipients, hour=14):
        return self.client.post(
            "/settings/email",
            data={"email_recipients": recipients,
                  "email_send_hour_utc": hour},
            follow_redirects=False)

    def test_hosted_non_member_rejected(self):
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            r = self._post(f"{self.email}, attacker@evil.test")
        self.assertEqual(r.status_code, 400)
        self.assertIn("attacker@evil.test", r.text)
        self.assertIsNone(_saved_recipients(self.tid))

    def test_hosted_member_saves(self):
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            r = self._post(self.email)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(_saved_recipients(self.tid), [self.email])

    def test_self_host_free_form_saves(self):
        r = self._post("spouse@family.example, me@work.example")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(_saved_recipients(self.tid),
                         ["spouse@family.example", "me@work.example"])

    def test_clearing_recipients_still_works(self):
        self._post("someone@family.example")
        r = self._post("")
        self.assertEqual(r.status_code, 303)
        self.assertIsNone(_saved_recipients(self.tid))


class JinjaEmailDemoGuardTests(unittest.TestCase):
    """Web half: a demo instance refuses the form outright."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()

    def test_demo_tenant_gets_403(self):
        client, tid, email = _signed_in_client()
        _set_demo(tid)
        r = client.post(
            "/settings/email",
            data={"email_recipients": email, "email_send_hour_utc": 9},
            follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        self.assertIsNone(_saved_recipients(tid))


class WorkerDemoEmailSkipTests(unittest.TestCase):
    """Worker half: the sweep never emails a demo tenant."""

    def setUp(self):
        self.conn = make_db()
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])

    def tearDown(self):
        self.conn.close()

    def _seed(self, **cfg):
        write_config(self.conn, **cfg)
        import datetime as dt
        from .util import TODAY
        add_bill(self.conn, "Rent", 2000.0, next_due=TODAY.replace(day=1),
                 last_seen=TODAY.replace(day=1))
        add_txn(self.conn, dt.date.today(), 42.5, "SAFEWAY",
                primary="FOOD_AND_DRINK")

    def test_email_tenant_skips_demo(self):
        from oikonome.jobs import worker
        from oikonome.web import report
        self._seed(demo_mode=True,
                   email_recipients=["someone@example.dev"])
        with mock.patch.object(report, "send") as send:
            subject = worker.email_tenant(self.tid, send_it=True)
        send.assert_not_called()
        self.assertEqual(subject, "")

    def test_email_tenant_still_sends_for_real_tenants(self):
        from oikonome.jobs import worker
        from oikonome.web import report
        self._seed(email_recipients=["someone@example.dev"])
        # configured proposes, accepted enrols
        accept_recipient_invite(self.tid, "someone@example.dev")
        with mock.patch.object(report, "send") as send:
            subject = worker.email_tenant(self.tid, send_it=True)
        send.assert_called_once()
        self.assertTrue(subject)


if __name__ == "__main__":
    unittest.main()
