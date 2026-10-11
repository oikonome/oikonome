"""Who receives which kind of household email.

Per address and per kind (each cadence, and the alert mail), stored as the
addresses that turned a kind off, so an absent or unreadable value can
only mean everyone gets everything. The whole-address mute still wins.

The door is a one-box delta applied to the stored document: two devices
changing two boxes must both land. The owner decides for anyone on the
mail; a member, a viewer and the shared demonstration login only for
their own address. Every scheduled cadence and every alert send filters
through it; a restore or export carries it.
"""

import io
import csv
import json
import os
import unittest
import uuid
import zipfile
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.jobs import worker
from oikonome.notify import email_prefs
from oikonome.sync import restore

from .util import (TEST_DB, _admin_dsn, _ensure_db, accept_recipient_invite,
                   make_db, write_config)

PW = "correct-horse-battery"
MEMBER_PW = "family-member-pass"


class StoredShapeTests(unittest.TestCase):
    def test_absent_means_every_kind_on(self):
        self.assertEqual(email_prefs.row({}, "a@x.dev"),
                         {k: True for k in email_prefs.KINDS})

    def test_off_then_on_round_trips_to_nothing_stored(self):
        cfg = {}
        email_prefs.set_pref(cfg, "A@X.dev", "monthly", False, cap=5)
        self.assertEqual(cfg["email_prefs"], {"monthly": ["a@x.dev"]})
        self.assertFalse(email_prefs.allows(cfg, "a@x.dev", "monthly"))
        self.assertTrue(email_prefs.allows(cfg, "a@x.dev", "daily"))
        email_prefs.set_pref(cfg, "a@x.dev", "monthly", True, cap=5)
        self.assertNotIn("email_prefs", cfg)

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            email_prefs.set_pref({}, "a@x.dev", "hourly", False, cap=5)

    def test_any_unreadable_shape_reads_as_everything_on(self):
        for bad in ("x", ["a@x.dev"], {"monthly": "a@x.dev"},
                    {"monthly": [1, None]}):
            self.assertTrue(
                email_prefs.allows({"email_prefs": bad}, "a@x.dev", "monthly"),
                bad)

    def test_a_changed_address_carries_its_choices(self):
        cfg = {"email_prefs": {"alerts": ["old@x.dev"]}}
        email_prefs.move(cfg, "old@x.dev", "new@x.dev")
        self.assertEqual(cfg["email_prefs"], {"alerts": ["new@x.dev"]})
        email_prefs.without(cfg, "new@x.dev")
        self.assertNotIn("email_prefs", cfg)


class WorkerFiltersTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.owner = f"owner-{uuid.uuid4().hex[:6]}@x.dev"
        self.member = f"member-{uuid.uuid4().hex[:6]}@x.dev"
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s,%s,'h','owner',now()), "
                "(%s,%s,'h','member',now())",
                (self.tid, self.owner, self.tid, self.member))
        finally:
            admin.close()

    def tearDown(self):
        self.conn.close()

    def test_a_kind_turned_off_leaves_only_that_kind(self):
        write_config(self.conn, email_prefs={"monthly": [self.member]})
        self.assertNotIn(self.member,
                         worker._recipients_raw(self.conn, self.tid, "monthly"))
        self.assertIn(self.member,
                      worker._recipients_raw(self.conn, self.tid, "daily"))
        self.assertIn(self.member, worker._recipients_raw(self.conn, self.tid))

    def test_the_mute_still_wins(self):
        write_config(self.conn, email_muted=[self.member])
        self.assertNotIn(self.member,
                         worker._recipients_raw(self.conn, self.tid, "daily"))

    def test_a_configured_list_is_filtered_too(self):
        guest = f"guest-{uuid.uuid4().hex[:6]}@x.dev"
        accept_recipient_invite(self.tid, guest)
        write_config(self.conn, email_recipients=[guest],
                     email_prefs={"monthly": [guest, self.owner]})
        self.assertEqual(
            worker._recipients_raw(self.conn, self.tid, "monthly"), [])
        self.assertEqual(
            {e.lower() for e in worker._recipients_raw(self.conn, self.tid,
                                                       "yearly")},
            {self.owner, guest})

    def test_the_cadence_send_skips_who_turned_it_off(self):
        from oikonome.web import report
        write_config(self.conn, email_prefs={"daily": [self.member]})
        with mock.patch.object(report, "send_each", return_value=[
                {"email": self.owner, "ok": True, "error": None}]) as m:
            worker.email_tenant(self.tid, send_it=True, force_email=True)
        self.assertEqual(m.call_args[0][3], [self.owner])

    def _everyone_off(self, kind="daily"):
        write_config(self.conn, email_prefs={kind: [self.owner, self.member]},
                     food_monthly=1000, other_monthly=1000)

    def _stamp(self, job="daily-email"):
        return self.conn.execute(
            "SELECT note FROM job_runs WHERE job=%s", (job,)).fetchone()

    def test_a_cadence_everyone_turned_off_is_stamped_and_not_rendered(self):
        """Nobody wants it: a decision made. The day is stamped done with a
        note saying so, and the report is never built for no one."""
        from oikonome.web import report
        self._everyone_off()
        with mock.patch.object(report, "gather") as gather, \
                mock.patch.object(report, "send_each") as send:
            worker.email_tenant(self.tid, send_it=True)
        gather.assert_not_called()
        send.assert_not_called()
        self.assertEqual(self._stamp()["note"], email_prefs.NOBODY_OPTED_IN)
        import datetime as dt
        self.assertNotIn("daily", worker.emails_due(
            self.conn, dt.datetime.now(dt.timezone.utc)))

    def test_send_now_says_everyone_turned_it_off(self):
        self._everyone_off()
        with self.assertRaises(worker.NoRecipients) as cm:
            worker.email_tenant(self.tid, send_it=True, force_email=True)
        self.assertIn("turned off the daily email", str(cm.exception))

    def test_doctor_says_nobody_opted_in_not_check_smtp(self):
        from oikonome.web import doctor
        self._everyone_off()
        worker.email_tenant(self.tid, send_it=True)
        rows = [r for r in doctor.checks(self.tid)
                if r.get("name") == "daily email (worker)"]
        self.assertEqual(len(rows), 1, rows)
        self.assertTrue(rows[0]["ok"])
        self.assertIn("turned the daily email off", rows[0]["detail"])
        self.assertNotIn("SMTP", rows[0]["detail"])

    def test_an_alert_skips_who_turned_alerts_off(self):
        from oikonome.engine import links
        from oikonome.web import report
        write_config(self.conn, email_prefs={"alerts": [self.owner]})
        for item, agg, acct in (("pl-1", "plaid", "pl-c"),
                                ("sf-1", "simplefin-org", "sf-c")):
            self.conn.execute(
                "INSERT INTO items (id, aggregator, institution_name) "
                "VALUES (%s,%s,%s)", (item, agg, item))
            self.conn.execute(
                "INSERT INTO accounts (id, item_id, name, type, subtype, "
                "mask, balance_current, updated_at) VALUES "
                "(%s,%s,%s,'credit','credit card','1234',100,now())",
                (acct, item, acct))
        links.create(self.conn, ["pl-c", "sf-c"])
        self.conn.execute("UPDATE items SET status='error:ITEM_LOGIN_REQUIRED'"
                          " WHERE id='pl-1'")
        smtp = {"configured": True, "host": "h", "port": 25, "user": "",
                "password": "", "sender": "no-reply@example.dev"}
        with mock.patch.object(report, "resolve_smtp", return_value=smtp), \
             mock.patch.object(report, "send_each", return_value=[
                 {"email": self.member, "ok": True, "error": None}]) as m, \
             mock.patch.object(worker, "_push_alert"):
            worker._alert_failovers(self.conn, self.tid)
        self.assertEqual(m.call_args.args[3], [self.member])


class DoorTests(unittest.TestCase):
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
        self.owner = TestClient(self.app)
        self.owner_email = f"own-{uuid.uuid4().hex[:8]}@example.dev"
        self.owner.post("/api/signup", data={"email": self.owner_email,
                                             "password": PW})
        self.owner.post("/api/auth/elevate", json={"password": PW})
        self.tid = self.owner.get("/api/me").json()["tenant_id"]

    def _claim(self, role):
        r = self.owner.post("/api/invites", json={
            "label": "partner", "role": role, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        token = r.json()["url"].rsplit("token=", 1)[1]
        c = TestClient(self.app)
        email = f"{role}-{uuid.uuid4().hex[:8]}@example.dev"
        got = c.post("/api/invite/claim", json={
            "token": token, "email": email, "password": MEMBER_PW})
        self.assertEqual(got.status_code, 200, got.text)
        return c, email

    def _stored(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return budget.load_config(conn).get("email_prefs")
        finally:
            conn.close()

    def _set(self, c, address, kind, on):
        return c.post("/api/settings", json={"email_prefs_set": {
            "address": address, "kind": kind, "on": on}})

    def test_the_owner_sets_anyone_and_the_view_shows_the_grid(self):
        _, member = self._claim("member")
        r = self._set(self.owner, member.upper(), "monthly", False)
        self.assertEqual(r.status_code, 200, r.text)
        r = self._set(self.owner, self.owner_email, "alerts", False)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._stored(), {"monthly": [member],
                                          "alerts": [self.owner_email]})
        rows = {s["email"].lower(): s["prefs"] for s in
                self.owner.get("/api/settings").json()[
                    "email_recipient_status"]}
        # no list configured: the owner and every verified member are on
        # the grid, as they are on the mail
        self.assertFalse(rows[self.owner_email]["alerts"])
        self.assertTrue(rows[self.owner_email]["monthly"])
        if member in rows:
            self.assertFalse(rows[member]["monthly"])
            self.assertTrue(rows[member]["daily"])
        # and the save reply carries the same grid
        self.assertFalse({s["email"].lower(): s["prefs"] for s in
                          r.json()["email_recipient_status"]}
                         [self.owner_email]["alerts"])

    def test_two_boxes_changed_from_two_copies_both_land(self):
        self._set(self.owner, "a@example.dev", "daily", False)
        self._set(self.owner, "b@example.dev", "daily", False)
        self.assertEqual(self._stored(),
                         {"daily": ["a@example.dev", "b@example.dev"]})

    def test_a_member_sets_only_their_own(self):
        c, member = self._claim("member")
        r = self._set(c, member, "monthly", False)
        self.assertEqual(r.status_code, 200, r.text)
        r = self._set(c, self.owner_email, "daily", False)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self._stored(), {"monthly": [member]})

    def test_a_viewer_sets_their_own_through_the_self_door(self):
        c, viewer = self._claim("viewer")
        r = c.post("/api/me/email", json={"kind": "alerts", "on": False})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["prefs"]["alerts"])
        self.assertEqual(self._stored(), {"alerts": [viewer]})
        me = c.get("/api/me").json()["daily_email"]
        self.assertFalse(me["prefs"]["alerts"])
        self.assertTrue(me["cadences"]["daily"])
        self.assertFalse(me["cadences"]["monthly"])

    def test_the_shared_demonstration_login_sets_only_its_own(self):
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE users SET second_factor_waived=TRUE "
                          "WHERE email=%s", (self.owner_email,))
        finally:
            admin.close()
        r = self._set(self.owner, "someone@example.dev", "daily", False)
        self.assertEqual(r.status_code, 403, r.text)
        r = self._set(self.owner, self.owner_email, "daily", False)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._stored(), {"daily": [self.owner_email]})

    def test_a_malformed_change_is_a_400(self):
        for bad in ({"address": "a@example.dev", "kind": "hourly", "on": False},
                    {"address": "a@example.dev", "kind": "daily", "on": "no"},
                    {"address": "a@x.dev, b@x.dev", "kind": "daily",
                     "on": False},
                    {"kind": "daily", "on": False}, ["daily"]):
            r = self.owner.post("/api/settings",
                                json={"email_prefs_set": bad})
            self.assertEqual(r.status_code, 400, (bad, r.text))
        self.assertIsNone(self._stored())


def _zip(config: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=["config"])
        w.writeheader()
        w.writerow({"config": json.dumps(config)})
        z.writestr("tenant_settings.csv", s.getvalue())
    return buf.getvalue()


class TravelsTests(unittest.TestCase):
    def test_the_export_scrub_keeps_every_address(self):
        # an address that happens to contain a credential-shaped word is a
        # value here, never a key, so the scrub cannot drop it
        prefs = {"monthly": ["token.holder@example.dev"],
                 "alerts": ["secret-santa@example.dev"]}
        self.assertEqual(restore.scrub_config({"email_prefs": prefs})
                         ["email_prefs"], prefs)

    def test_a_restore_keeps_the_choices(self):
        conn = make_db()
        try:
            write_config(conn)
            restore.restore_zip(conn, _zip(
                {"email_prefs": {"yearly": ["p@example.dev"]}}))
            self.assertEqual(budget.load_config(conn).get("email_prefs"),
                             {"yearly": ["p@example.dev"]})
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
