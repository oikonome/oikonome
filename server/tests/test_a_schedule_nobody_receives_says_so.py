"""A cadence that is switched on is not a cadence that arrives. When the only
address is muted and push is ticked with no browser or phone registered,
nothing goes anywhere, yet every surface used to say "daily 9am". The settings
read must carry the senders' own answer — who receives each cadence — with
the list line the clients print, and System health must warn in the same
words."""
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.notify import reach

from .util import _ensure_db, accept_recipient_invite


class NobodyReceivingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.email = f"reach-{uuid.uuid4().hex[:8]}@example.dev"
        cls.client.post("/api/signup", data={
            "email": cls.email, "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]

    def setUp(self):
        # a relay is configured unless a test says otherwise: an address
        # receives mail only when there is a relay to send it
        p = mock.patch.dict(os.environ,
                            {"OIKONOME_SMTP_HOST": "relay.example.dev"})
        p.start()
        self.addCleanup(p.stop)

    def _cfg(self, **kw):
        conn = tenancy.tenant_connect(self.tid)
        try:
            cfg = budget.load_config(conn)
            for k in ("email_muted", "email_recipients", "email_schedule"):
                cfg.pop(k, None)
            cfg.update(kw)
            budget.save_config(conn, cfg)
        finally:
            conn.close()

    def _reach(self):
        r = self.client.get("/api/settings")
        self.assertEqual(r.status_code, 200)
        return r.json()["email_reach"]

    def test_a_muted_owner_and_no_devices_is_nobody(self):
        self._cfg(email_muted=[self.email],
                  email_schedule={"daily": {"on": True, "hour": 9,
                                            "push": True}})
        d = self._reach()["daily"]
        self.assertTrue(d["on"])
        self.assertFalse(d["receiving"])
        self.assertTrue(d["warn"])
        self.assertEqual(d["email"], 0)
        self.assertEqual(d["push"], 0)
        self.assertEqual(d["text"], "daily 9am · nobody receiving it")

    def test_an_unmuted_owner_receives_it(self):
        self._cfg(email_schedule={"daily": {"on": True, "hour": 21}})
        d = self._reach()["daily"]
        self.assertTrue(d["receiving"])
        self.assertFalse(d["warn"])
        self.assertEqual(d["email"], 1)
        self.assertEqual(d["text"], "daily 9pm")

    def test_an_accepted_recipient_is_counted_beside_a_muted_owner(self):
        partner = f"partner-{uuid.uuid4().hex[:6]}@example.dev"
        accept_recipient_invite(self.tid, partner)
        self._cfg(email_muted=[self.email], email_recipients=[partner],
                  email_schedule={"daily": {"on": True, "hour": 7}})
        d = self._reach()["daily"]
        self.assertTrue(d["receiving"])
        self.assertEqual(d["text"], "daily 7am · +1 recipient")

    def test_a_daily_switched_off_says_off_and_does_not_warn(self):
        self._cfg(email_schedule={"daily": {"on": False, "hour": 7}})
        d = self._reach()["daily"]
        self.assertFalse(d["on"])
        self.assertFalse(d["warn"])
        self.assertEqual(d["text"], "daily email off")

    def test_the_save_reply_carries_the_same_answer(self):
        self._cfg(email_muted=[self.email])
        r = self.client.post("/api/settings", json={
            "email_schedule": {"daily": {"on": True, "hour": 9}}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["email_reach"]["daily"]["text"],
                         "daily 9am · nobody receiving it")

    def test_system_health_warns_when_nobody_receives_the_daily(self):
        from oikonome.web import doctor
        self._cfg(email_muted=[self.email],
                  email_schedule={"daily": {"on": True, "hour": 9}})
        rows = [r for r in doctor.checks(self.tid)
                if r["name"].startswith("daily email")]
        self.assertTrue(rows, "no daily email row")
        self.assertFalse(rows[0]["ok"])
        self.assertIn(reach.NOBODY, str(rows[0].get("detail")))

    def test_no_mail_relay_is_nobody_however_many_addresses(self):
        self._cfg(email_schedule={"daily": {"on": True, "hour": 9}})
        with mock.patch.dict(os.environ, {"OIKONOME_SMTP_HOST": ""}):
            budget_conn = tenancy.tenant_connect(self.tid)
            try:
                cfg = budget.load_config(budget_conn)
                cfg.pop("smtp_host", None)
                budget.save_config(budget_conn, cfg)
            finally:
                budget_conn.close()
            d = self._reach()["daily"]
        self.assertEqual(d["email"], 0)
        self.assertFalse(d["receiving"])
        self.assertTrue(d["warn"])
        self.assertIn(reach.NOBODY, d["text"])
        self.assertIn(reach.NO_RELAY, d["text"])

    def test_a_saved_relay_held_by_a_rotation_is_not_called_missing(self):
        # the owner's relay is filled in under Settings → Email; telling
        # them there is none sends them to a card that looks right — the
        # step that sends mail again is confirming the held relay
        from oikonome.notify import outbound_pause
        from oikonome.web import doctor
        self._cfg(email_schedule={"daily": {"on": True, "hour": 9}},
                  smtp_host="relay.household.example",
                  **{outbound_pause.KEY: {"at": "2026-01-01T00:00:00",
                                          "reason": outbound_pause.ROTATION,
                                          "smtp": True}})
        with mock.patch.dict(os.environ, {"OIKONOME_SMTP_HOST": ""}):
            d = self._reach()["daily"]
            rows = [r for r in doctor.checks(self.tid)
                    if r["name"].startswith("daily email")]
        self.assertTrue(d["warn"])
        self.assertNotIn(reach.NO_RELAY, d["text"])
        self.assertIn(reach.RELAY_HELD, d["text"])
        self.assertNotIn("no mail relay", str(rows[0].get("detail")))
        self.assertIn(reach.RELAY_HELD, str(rows[0].get("detail")))
        self._cfg(**{outbound_pause.KEY: None, "smtp_host": None})


if __name__ == "__main__":
    unittest.main()
