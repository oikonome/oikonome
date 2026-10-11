"""An owner's credential rotation pauses the household's own outbound
settings until the owner confirms them.

The Settings relay, the extra daily-mail recipients and the AI endpoint
are the footholds a hijacked owner session could plant that no eviction
reached: a password reset kept mailing through the intruder's relay.
Now a rotation (password change here; the reset path goes through the
same `revoke_everything`) records what was set, the senders stand down
on exactly those channels, Settings shows the record, and the owner's
Resume lifts it. A member's rotation touches nothing."""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget, llm_categorize
from oikonome.jobs import worker
from oikonome.notify import outbound_pause
from oikonome.web import report

from .util import _ensure_db, accept_recipient_invite, write_config

PW = "correct-horse-battery"


class OutboundPauseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.app)
        self.email = f"outb-{uuid.uuid4().hex[:10]}@example.dev"
        r = self.client.post("/api/signup", data={"email": self.email,
                                                 "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        admin = tenancy.admin_connect()
        try:
            self.uid = admin.execute(
                "SELECT id FROM users WHERE lower(email)=lower(%s)",
                (self.email,)).fetchone()["id"]
        finally:
            admin.close()
        self.partner = f"partner-{uuid.uuid4().hex[:6]}@example.dev"
        conn = tenancy.tenant_connect(self.tid)
        try:
            write_config(conn, smtp_host="relay.example.dev", smtp_port=2525,
                         smtp_starttls=False,
                         email_recipients=[self.email, self.partner],
                         llm_url="http://10.0.0.9:11434", llm_model="x")
        finally:
            conn.close()
        accept_recipient_invite(self.tid, self.partner)

    def _conn(self):
        return tenancy.tenant_connect(self.tid)

    def _rotate(self):
        r = self.client.post("/api/password/change", json={
            "current_password": PW, "new_password": "another-long-phrase-9"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_a_password_change_records_what_was_set(self):
        self._rotate()
        conn = self._conn()
        try:
            p = outbound_pause.paused(budget.load_config(conn))
        finally:
            conn.close()
        self.assertIsNotNone(p)
        self.assertTrue(p["smtp"])
        self.assertTrue(p["llm"])
        # the owner's own address is never paused — only the extras
        self.assertEqual(p["recipients"], [self.partner])
        self.assertEqual(p["reason"], "rotation")
        self.assertTrue(p["at"])

    def test_the_senders_stand_down_on_the_paused_channels(self):
        self._rotate()
        conn = self._conn()
        try:
            with mock.patch.dict(os.environ, {"OIKONOME_SMTP_HOST": "",
                                              "OIKONOME_HOSTED": ""}):
                smtp = report.resolve_smtp(conn)
            self.assertFalse(smtp["configured"],
                             "the Settings relay is not used while paused")
            self.assertNotIn(self.partner,
                             worker._recipients_raw(conn, self.tid, "daily"))
            self.assertIn(self.email,
                          worker._recipients_raw(conn, self.tid, "daily"))
            self.assertEqual(
                llm_categorize.resolve_role(budget.load_config(conn)),
                ("env", None))
        finally:
            conn.close()

    def test_settings_shows_the_record_and_resume_lifts_it(self):
        self._rotate()
        got = self.client.get("/api/settings").json()["outbound_paused"]
        self.assertEqual(got["recipients"], [self.partner])
        self.assertTrue(got["smtp"] and got["llm"])
        # resuming is a step-up act (see
        # test_outbound_resume_is_guarded_like_a_new_destination)
        r = self.client.post("/api/auth/elevate",
                             json={"password": "another-long-phrase-9"})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post("/api/settings/outbound/resume")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["resumed"])
        self.assertIsNone(self.client.get("/api/settings").json()["outbound_paused"])
        conn = self._conn()
        try:
            with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": ""}):
                self.assertEqual(report.resolve_smtp(conn)["host"],
                                 "relay.example.dev")
            self.assertIn(self.partner,
                          worker._recipients_raw(conn, self.tid, "daily"))
            self.assertEqual(
                llm_categorize.resolve_role(budget.load_config(conn)),
                ("legacy", None))
        finally:
            conn.close()
        # nothing paused is not an error
        r = self.client.post("/api/settings/outbound/resume")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["resumed"])

    def test_recovery_pauses_the_same_channels_marked_as_recovery(self):
        from oikonome.auth import reset as reset_mod
        admin = tenancy.admin_connect()
        try:
            reset_mod.revoke_everything(admin, self.uid)
        finally:
            admin.close()
        conn = self._conn()
        try:
            p = outbound_pause.paused(budget.load_config(conn))
        finally:
            conn.close()
        self.assertIsNotNone(p)
        self.assertEqual(p["reason"], "recovery")
        self.assertEqual(p["recipients"], [self.partner])

    def test_a_second_rotation_widens_the_record_never_narrows_it(self):
        self._rotate()
        conn = self._conn()
        try:
            cfg = budget.load_config(conn)
            cfg.pop("smtp_host", None)
            budget.save_config(conn, cfg)
        finally:
            conn.close()
        admin = tenancy.admin_connect()
        try:
            outbound_pause.pause_for_rotation(admin, self.uid)
        finally:
            admin.close()
        conn = self._conn()
        try:
            p = outbound_pause.paused(budget.load_config(conn))
        finally:
            conn.close()
        self.assertTrue(p["smtp"], "a relay paused earlier stays paused")

    def test_nothing_set_records_nothing(self):
        conn = self._conn()
        try:
            cfg = budget.load_config(conn)
            for k in ("smtp_host", "smtp_port", "llm_url", "llm_model"):
                cfg.pop(k, None)
            cfg["email_recipients"] = [self.email]
            budget.save_config(conn, cfg)
        finally:
            conn.close()
        self._rotate()
        self.assertIsNone(self.client.get("/api/settings").json()["outbound_paused"])

    def test_an_unreadable_record_reads_as_not_paused(self):
        for bad in ("x", ["smtp"], {"smtp": "yes", "recipients": "a@b"},
                    {}, {"recipients": []}):
            p = outbound_pause.paused({"outbound_paused": bad})
            if p:
                p.pop("version")
            self.assertIn(p, (None, {"at": "", "reason": "rotation",
                                     "smtp": True, "recipients": [],
                                     "llm": False, "aggregator": False}), bad)

    def test_a_member_cannot_resume_and_a_members_rotation_pauses_nothing(self):
        admin = tenancy.admin_connect()
        try:
            self.assertIsNone(outbound_pause.pause_for_rotation(
                admin, "00000000-0000-4000-8000-000000000000"))
        finally:
            admin.close()


if __name__ == "__main__":
    unittest.main()
