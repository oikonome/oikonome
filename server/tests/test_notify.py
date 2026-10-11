"""Multi-channel budget summaries — annual cadence, per-cadence
channel flags, SMS phone verification, push subscriptions, and the
worker's channel fan-out gating. SMS/push transports are mocked; the
short-form renders are pure functions of the email's own payloads."""

import datetime as dt
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.notify import render, sms

from .util import _ensure_db


class ShortFormRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_daily_over(self):
        # keys as report.gather() ACTUALLY produces them (today,
        # days_in_month, buckets[k].month_budget/actual) — NOT a fabricated
        # 'allow'/'days_left', which would silently drop the allowance line
        d = {"verdict": "OVER BUDGET", "variance": 199.4,
             "reasons": [{"bucket": "Food", "variance": 493.0,
                          "pace": "", "detail": []}],
             "today": dt.date(2026, 7, 21), "days_in_month": 31,
             "buckets": {"food": {"month_budget": 1500.0, "actual": 1311.0,
                                  "rows": []},
                         "other": {"month_budget": 360.0, "actual": 833.0,
                                   "rows": []}}}
        t = render.short_daily(d)
        self.assertIn("OVER budget by $199", t)
        self.assertIn("Food", t)
        self.assertIn("Spend at most", t)
        self.assertIn("11 days left", t)         # 31 - 21 + 1
        self.assertIn("/day Food", t)
        self.assertIn("/day Everything else", t)
        self.assertLess(len(t), 320)             # SMS-sized
        # the push line is the same text on one line, verdict first,
        # without the "Oikonome:" prefix the notification title already
        # carries
        p = render.push_daily(d)
        self.assertNotIn("\n", p)
        self.assertTrue(p.startswith("OVER budget by $199"))
        self.assertIn(" · Food ", p)
        self.assertIn("Spend at most", p)
        # sentence stops read as stray dots beside the separators
        self.assertNotIn(". ·", p)
        self.assertFalse(p.endswith("."))

    def test_daily_allowance_matches_the_today_tiles_with_a_carve_out(self):
        """A carve-out has its own Today tile and the parent's tile uses the
        carved numbers; the text's per-day line must say the same numbers
        under the same names, never the raw parent bucket (which counts the
        carve-out's overspend twice) or an internal bucket key."""
        from oikonome.web import todayview
        d = {"verdict": "OVER BUDGET", "variance": 40.0, "reasons": [],
             "today": dt.date(2026, 10, 10), "days_in_month": 31,
             "buckets": {
                 "food": {"month_budget": 900.0, "actual": 550.0,
                          "remaining_budget": 750.0,
                          "display_actual": 350.0, "rows": [],
                          "children": [{"name": "Coffee",
                                        "month_budget": 150.0,
                                        "actual": 200.0, "rows": []}]},
                 "other": {"month_budget": 400.0, "actual": 100.0,
                           "rows": []}}}
        t = render.short_daily(d)
        _a, _k, tiles = todayview.allowances(d)
        for label, a in tiles:
            self.assertIn(f"${a['rate']:,.0f}/day {label}", t)
        # (750 - 350) / 22 days, not the raw (900 - 550) / 22
        self.assertIn("$18/day Food", t)
        self.assertIn("$0/day Coffee", t)
        self.assertIn("$14/day Everything else", t)
        self.assertNotIn("$16/day", t)
        self.assertNotIn(" else,", t.replace("Everything else", ""))
        self.assertNotIn("other", t)
        self.assertIn("22 days left", t)
        self.assertIn("$18/day Food", render.push_daily(d))

    def test_daily_allowance_uses_real_gather_dict(self):
        """short_daily must produce the allowance line from the exact dict
        report.gather returns — build a real one and assert the line
        appears."""
        from oikonome.web import report
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, f"r-{uuid.uuid4().hex[:8]}")
        finally:
            admin.close()
        conn = tenancy.tenant_connect(tid)
        try:
            d = report.gather(conn, dt.date.today())
        finally:
            conn.close()
        t = render.short_daily(d)
        # a fresh tenant has budgets → days-left line must render
        self.assertIn("days left", t)

    def test_daily_on_budget_minimal(self):
        t = render.short_daily({"verdict": "ON BUDGET", "variance": 3,
                                "reasons": [], "allow": {}, "days_left": 0})
        self.assertEqual(t, "Oikonome: ON budget.")

    def test_monthly_and_yearly(self):
        m = render.short_monthly({"verdict": "ON BUDGET", "variance": 2,
                                  "variable_actual": 5000,
                                  "variable_budget": 5500,
                                  "spend_total": 9000}, "June 2026")
        self.assertIn("June 2026", m)
        y = render.short_yearly({"cells": [
            {"verdict": "ON BUDGET"}, {"verdict": "OVER BUDGET"}],
            "totals": {"income": 100000, "spend": 80000, "saved": 20000,
                       "rate": 20.0}}, 2025)
        self.assertIn("2025", y)
        self.assertIn("1 of 2", y)
        self.assertIn("saved $20,000 (20%)", y)


class SmsHelperTests(unittest.TestCase):
    def test_configured_needs_all_three(self):
        env = {"OIKONOME_TWILIO_ACCOUNT_SID": "AC1",
               "OIKONOME_TWILIO_AUTH_TOKEN": "t",
               "OIKONOME_TWILIO_FROM": "+12175550000"}
        with mock.patch.dict(os.environ, env):
            self.assertTrue(sms.configured())
        with mock.patch.dict(os.environ, {**env,
                                          "OIKONOME_TWILIO_FROM": ""}):
            self.assertFalse(sms.configured())

    def test_e164_and_mask(self):
        self.assertTrue(sms.valid_e164("+12175551234"))
        self.assertFalse(sms.valid_e164("2175551234"))
        self.assertFalse(sms.valid_e164("+1 217 555"))
        self.assertEqual(sms.mask("+12175551234"), "+1••••••1234")

    def test_send_noop_unconfigured(self):
        for k in ("OIKONOME_TWILIO_ACCOUNT_SID",):
            os.environ.pop(k, None)
        self.assertFalse(sms.send("+12175551234", "x"))


class CadenceScheduleTests(unittest.TestCase):
    """emails_due: the yearly cadence + any-channel gating."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def _due(self, sched, when_local):
        from oikonome.jobs import worker
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, f"n-{uuid.uuid4().hex[:8]}")
        finally:
            admin.close()
        conn = tenancy.tenant_connect(tid)
        try:
            from oikonome.engine import budget
            cfg = budget.load_config(conn)
            cfg["email_schedule"] = sched
            budget.save_config(conn, cfg)
            # the schedule hours below are written for an instance running
            # on UTC; pin the instance zone rather than inherit whatever
            # zone the test host happens to be in
            from oikonome import localtime
            with mock.patch.object(worker, "_tz",
                                   return_value=dt.timezone.utc), \
                 mock.patch.object(localtime, "instance_tz",
                                   return_value=dt.timezone.utc):
                return worker.emails_due(
                    conn, when_local.replace(tzinfo=dt.timezone.utc))
        finally:
            conn.close()

    def test_yearly_fires_jan_1_and_catches_up_only_days_after(self):
        sched = {"yearly": {"on": True, "hour": 7}}
        self.assertIn("yearly", self._due(
            sched, dt.datetime(2026, 1, 1, 8)))
        # an unsent Jan 1 is still sent a day later, never weeks later
        self.assertEqual(["yearly"],
                         self._due(sched, dt.datetime(2026, 1, 2, 8)))
        self.assertEqual([], self._due(sched, dt.datetime(2026, 1, 10, 8)))
        self.assertEqual([], self._due(sched, dt.datetime(2026, 7, 1, 8)))

    def test_a_catch_up_day_waits_for_the_send_hour(self):
        """Days after the 1st still honour the household's hour: the first
        sweep after midnight must not send the card at 00:05."""
        sched = {"monthly": {"on": True, "sms": True, "hour": 7}}
        self.assertEqual([], self._due(sched, dt.datetime(2026, 10, 2, 0, 5)))
        self.assertEqual(["monthly"],
                         self._due(sched, dt.datetime(2026, 10, 2, 7, 5)))

    def test_monthly_sms_push_counts_as_sent_for_the_whole_period(self):
        """The SMS/push half stamped on the 1st still counts on a catch-up
        day, so a relay outage retries the mail without re-texting; last
        month's stamp does not count."""
        from oikonome.jobs import worker
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, f"n-{uuid.uuid4().hex[:8]}")
        finally:
            admin.close()
        conn = tenancy.tenant_connect(tid)
        try:
            from oikonome.engine import budget
            cfg = budget.load_config(conn)
            job = "monthly-email:sms-push"
            now = dt.datetime.now(dt.timezone.utc)
            day1 = now.replace(day=1, hour=12, minute=0)
            conn.execute("INSERT INTO job_runs (job, ran_at) VALUES (%s, %s)",
                         (job, day1))
            with mock.patch.object(worker, "_tz",
                                   return_value=dt.timezone.utc), \
                 mock.patch("oikonome.localtime.instance_tz",
                            return_value=dt.timezone.utc):
                self.assertTrue(worker._stamped_this_period(conn, cfg, job))
                conn.execute("UPDATE job_runs SET ran_at=%s WHERE job=%s",
                             (day1 - dt.timedelta(days=31), job))
                self.assertFalse(worker._stamped_this_period(conn, cfg, job))
        finally:
            conn.close()

    def test_sms_only_cadence_still_fires(self):
        # email off, sms on → the cadence is still scheduled (the worker
        # then routes to the channels the entry asks for)
        sched = {"daily": {"on": False, "sms": True, "hour": 7}}
        self.assertIn("daily", self._due(sched, dt.datetime(2026, 7, 21, 8)))

    def test_all_channels_off_never_fires(self):
        sched = {"daily": {"on": False, "hour": 7}}
        self.assertEqual([], self._due(sched, dt.datetime(2026, 7, 21, 8)))


class NotifyApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        email = f"n-{uuid.uuid4().hex[:8]}@x.dev"
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, email)
            self.tid = str(tid)     # consent-evidence assertions read config
            from oikonome.auth import passwords
            self.uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, "
                "verified_at) VALUES (%s, %s, %s, now()) RETURNING id",
                (tid, email, passwords.hash_password("correct-horse-battery"))
                ).fetchone()["id"]
        finally:
            admin.close()
        self.client = TestClient(self.appmod.app)
        r = self.client.post("/login", data={
            "email": email, "password": "correct-horse-battery"},
            follow_redirects=False)
        assert r.status_code == 303, r.text
        # setting a phone number is a step-up act; these tests exercise
        # what happens past that gate, so the session proves itself once
        r = self.client.post("/api/auth/elevate",
                             json={"password": "correct-horse-battery"})
        assert r.status_code == 200, r.text

    def test_status_shape(self):
        r = self.client.get("/api/notify")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        for k in ("sms_available", "phone", "phone_verified",
                  "phone_paused_at", "push_available", "vapid_public_key", "push_subscribed",
                  "native_push_available", "native_push_devices"):
            self.assertIn(k, j)
        self.assertFalse(j["phone_verified"])

    def test_native_push_availability_is_its_own_channel(self):
        """Phone push rides the relay, web push rides VAPID — the status has
        to report them separately, or a household whose only push is the
        mobile app is told push is unavailable and cannot choose it."""
        with mock.patch.dict(os.environ):     # restored on exit
            os.environ.pop("OIKONOME_PUSH_RELAY_URL", None)
            j = self.client.get("/api/notify").json()
            self.assertFalse(j["native_push_available"])
        with mock.patch.dict(
                os.environ, {"OIKONOME_PUSH_RELAY_URL": "https://relay.test"}):
            j = self.client.get("/api/notify").json()
            self.assertTrue(j["native_push_available"])


    def test_phone_verify_flow(self):
        env = {"OIKONOME_TWILIO_ACCOUNT_SID": "AC1",
               "OIKONOME_TWILIO_AUTH_TOKEN": "t",
               "OIKONOME_TWILIO_FROM": "+12175550000"}
        sent: list = []
        with mock.patch.dict(os.environ, env), \
             mock.patch("oikonome.notify.sms.send",
                        side_effect=lambda to, body: sent.append(body) or True):
            r = self.client.post("/api/notify/phone",
                                 json={"number": "+12175551234",
                                       "consent": True})
            self.assertEqual(r.status_code, 200, r.text)
            code = sent[0].split(":")[1].strip()
            r = self.client.post("/api/notify/phone/verify",
                                 json={"code": "000001"})
            self.assertEqual(r.status_code, 400)     # wrong code
            r = self.client.post("/api/notify/phone/verify",
                                 json={"code": code})
            self.assertEqual(r.status_code, 200, r.text)
        j = self.client.get("/api/notify").json()
        self.assertTrue(j["phone_verified"])
        self.assertTrue(j["phone"].endswith("1234"))
        # clear
        r = self.client.post("/api/notify/phone", json={"number": ""})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.client.get("/api/notify").json()["phone_verified"])

    def test_a_paused_number_is_reported_and_removable_without_a_code(self):
        """A credential change pauses the household's verified number. The
        status must say so (with when), and the owner must be able to drop
        it through the clear door with no verification code and no fresh
        step-up — clearing only takes a destination away."""
        from oikonome.auth import reset as reset_mod
        conn = tenancy.tenant_connect(self.tid)
        try:
            from oikonome.engine import budget
            cfg = budget.load_config(conn)
            cfg["notify_phone"] = {"number": "+12175551234", "verified": True}
            budget.save_config(conn, cfg)
        finally:
            conn.close()
        j = self.client.get("/api/notify").json()
        self.assertTrue(j["phone_verified"])
        self.assertIsNone(j["phone_paused_at"])
        cc = tenancy.control_connect()
        try:
            self.assertTrue(reset_mod._unverify_sms_number(cc, self.uid))
        finally:
            cc.close()
        j = self.client.get("/api/notify").json()
        self.assertFalse(j["phone_verified"])
        self.assertTrue(j["phone"].endswith("1234"))
        paused = dt.datetime.fromisoformat(j["phone_paused_at"])
        self.assertLess(abs((dt.datetime.now(dt.timezone.utc) - paused)
                            .total_seconds()), 300)
        # the clear asks for no proof at all (elevation would refuse here)
        from oikonome.web import security
        security._limiter._hits.clear()
        with mock.patch("oikonome.web.app._require_elevation",
                        side_effect=AssertionError("clear asked for proof")), \
             mock.patch("oikonome.notify.sms.send") as sms_send:
            r = self.client.post("/api/notify/phone", json={"number": ""})
        self.assertEqual(r.status_code, 200, r.text)
        sms_send.assert_not_called()
        j = self.client.get("/api/notify").json()
        self.assertIsNone(j["phone"])
        self.assertIsNone(j["phone_paused_at"])

    def test_phone_requires_explicit_sms_consent(self):
        """SMS consent must be its own affirmative action. The SPA gates the
        button on a checkbox, but a UI gate leaves nothing to show a
        carrier — so the server refuses without consent and records WHEN it
        was given, for WHICH number."""
        env = {"OIKONOME_TWILIO_ACCOUNT_SID": "AC1",
               "OIKONOME_TWILIO_AUTH_TOKEN": "t",
               "OIKONOME_TWILIO_FROM": "+12175550000"}
        sent: list = []
        with mock.patch.dict(os.environ, env), \
             mock.patch("oikonome.notify.sms.send",
                        side_effect=lambda to, body: sent.append(body) or True):
            # missing entirely, and explicitly false — both refused
            for payload in ({"number": "+12175551234"},
                            {"number": "+12175551234", "consent": False}):
                r = self.client.post("/api/notify/phone", json=payload)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertIn("consent", r.text.lower())
            self.assertEqual(sent, [], "no billed SMS may go out without consent")
            # with consent: accepted, and the timestamp is stored
            r = self.client.post("/api/notify/phone",
                                 json={"number": "+12175551234",
                                       "consent": True})
            self.assertEqual(r.status_code, 200, r.text)
            code = sent[0].split(":")[1].strip()
            from oikonome.engine import budget
            from oikonome.db import tenancy
            conn = tenancy.tenant_connect(self.tid)
            try:
                pend = budget.load_config(conn).get("notify_phone_pending")
                self.assertIsNotNone(pend.get("consent_at"))
            finally:
                conn.close()
            # ...and it survives verification, when the pending row is dropped
            r = self.client.post("/api/notify/phone/verify",
                                 json={"code": code})
            self.assertEqual(r.status_code, 200, r.text)
            conn = tenancy.tenant_connect(self.tid)
            try:
                rec = budget.load_config(conn)["notify_phone"]
                self.assertTrue(rec["verified"])
                self.assertIsNotNone(rec.get("consent_at"),
                                     "consent evidence must not be lost on verify")
                self.assertIsNotNone(rec.get("verified_at"))
            finally:
                conn.close()
        # clearing a number must NOT require consent — you can always leave
        r = self.client.post("/api/notify/phone", json={"number": ""})
        self.assertEqual(r.status_code, 200, r.text)

    def test_phone_consent_is_per_program(self):
        """One consent may not cover two message programs, so consent is
        stored as a LIST of programs rather than a bare yes. Ticking a
        program must really mean only those texts go out, not just a tidier
        form.

        The 'alerts' program is retired: nothing in the server sends an
        alerts message, so offering the box would promise silence. The
        per-program MECHANISM is what this test pins."""
        from oikonome.notify import sms as _sms
        env = {"OIKONOME_TWILIO_ACCOUNT_SID": "AC1",
               "OIKONOME_TWILIO_AUTH_TOKEN": "t",
               "OIKONOME_TWILIO_FROM": "+12175550000"}
        sent: list = []
        with mock.patch.dict(os.environ, env), \
             mock.patch("oikonome.notify.sms.send",
                        side_effect=lambda to, body: sent.append(body) or True):
            # an empty list, an unknown program, or a RETIRED one is not
            # consent — 'alerts' must not sneak back in through the API just
            # because the checkbox is gone from the SPA
            for bad in ([], ["marketing"], ["alerts"]):
                r = self.client.post("/api/notify/phone",
                                     json={"number": "+12175551234",
                                           "consent": bad})
                self.assertEqual(r.status_code, 400, r.text)
            self.assertEqual(sent, [])
            # the one real program — accepted, and stored as exactly that
            r = self.client.post("/api/notify/phone",
                                 json={"number": "+12175551234",
                                       "consent": ["summary"]})
            self.assertEqual(r.status_code, 200, r.text)
            code = sent[0].split(":")[1].strip()
            r = self.client.post("/api/notify/phone/verify",
                                 json={"code": code})
            self.assertEqual(r.status_code, 200, r.text)
            from oikonome.engine import budget
            from oikonome.db import tenancy
            conn = tenancy.tenant_connect(self.tid)
            try:
                rec = budget.load_config(conn)["notify_phone"]
                self.assertEqual(rec.get("consent_programs"), ["summary"])
            finally:
                conn.close()
        # a record with no list opted in under the combined box, so it
        # must not go dark
        self.assertTrue(_sms.consented({"number": "+12175551234"}, "summary"))
        # a record that only ever consented to the retired program is NOT
        # consent for the summary text
        self.assertFalse(_sms.consented({"consent_programs": ["alerts"]},
                                        "summary"))

    def test_phone_rejects_bad_number(self):
        env = {"OIKONOME_TWILIO_ACCOUNT_SID": "AC1",
               "OIKONOME_TWILIO_AUTH_TOKEN": "t",
               "OIKONOME_TWILIO_FROM": "+12175550000"}
        with mock.patch.dict(os.environ, env):
            r = self.client.post("/api/notify/phone",
                                 json={"number": "555-1234"})
            self.assertEqual(r.status_code, 400)

    def test_push_subscribe_roundtrip(self):
        sub = {"endpoint": f"https://fcm.googleapis.com/fcm/send/{uuid.uuid4().hex}",
               "keys": {"p256dh": "k1", "auth": "a1"}}
        r = self.client.post("/api/notify/push/subscribe", json=sub)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(self.client.get("/api/notify").json()["push_subscribed"])
        # re-subscribe same endpoint upserts, not duplicates
        r = self.client.post("/api/notify/push/subscribe", json=sub)
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/api/notify/push/unsubscribe",
                             json={"endpoint": sub["endpoint"]})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.client.get("/api/notify").json()["push_subscribed"])

    def test_push_endpoint_cannot_be_stolen_by_another_account(self):
        """ON CONFLICT (endpoint) DO UPDATE reassigned
        user_id/tenant_id, so knowing an endpoint URL was enough to take the
        row over. push_subscriptions is control-plane and has no RLS, so
        nothing else stopped it — and because the thief's keys cannot
        decrypt on the victim's browser, every send then fails and the
        victim's alerts die silently. Takeover now needs the SAME keys,
        which only the real browser has."""
        endpoint = f"https://fcm.googleapis.com/fcm/send/{uuid.uuid4().hex}"
        victim = {"endpoint": endpoint,
                  "keys": {"p256dh": "victim-key", "auth": "victim-auth"}}
        r = self.client.post("/api/notify/push/subscribe", json=victim)
        self.assertEqual(r.status_code, 200, r.text)

        # a SECOND account learns the endpoint and tries to claim it
        other = self._second_account_client()
        r = other.post("/api/notify/push/subscribe", json={
            "endpoint": endpoint,
            "keys": {"p256dh": "thief-key", "auth": "thief-auth"}})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertFalse(other.get("/api/notify").json()["push_subscribed"])
        # the victim still owns it
        self.assertTrue(self.client.get("/api/notify").json()["push_subscribed"])

    def test_same_browser_resubscribe_still_works_across_accounts(self):
        """The legitimate case the guard must NOT break: a shared browser
        profile presents the same keys, so a second login may claim it."""
        endpoint = f"https://fcm.googleapis.com/fcm/send/{uuid.uuid4().hex}"
        keys = {"p256dh": "same-browser", "auth": "same-auth"}
        r = self.client.post("/api/notify/push/subscribe",
                             json={"endpoint": endpoint, "keys": keys})
        self.assertEqual(r.status_code, 200, r.text)
        other = self._second_account_client()
        r = other.post("/api/notify/push/subscribe",
                       json={"endpoint": endpoint, "keys": keys})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(other.get("/api/notify").json()["push_subscribed"])

    def _second_account_client(self):
        from oikonome.auth import passwords
        from oikonome.web import security
        security._limiter._hits.clear()
        email = f"n2-{uuid.uuid4().hex[:8]}@x.dev"
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, email)
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, "
                "verified_at) VALUES (%s, %s, %s, now())",
                (tid, email, passwords.hash_password("correct-horse-battery")))
        finally:
            admin.close()
        c = TestClient(self.appmod.app)
        r = c.post("/login", data={"email": email,
                                   "password": "correct-horse-battery"},
                   follow_redirects=False)
        assert r.status_code == 303, r.text
        # adding a push destination is a step-up act, like the first
        # account's in setUp
        r = c.post("/api/auth/elevate",
                   json={"password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        return c

    def test_push_subscribe_rejects_junk(self):
        r = self.client.post("/api/notify/push/subscribe",
                             json={"endpoint": "http://not-https", "keys": {}})
        self.assertEqual(r.status_code, 400)

    def test_push_subscribe_rejects_non_push_host_ssrf(self):
        """The endpoint is server-POSTed by the
        fan-out, so a non-push-service host (esp. an internal address) must
        be refused before storage."""
        for bad in ("https://10.0.0.5:8080/x",
                    "https://evil.example.com/x",
                    "https://169.254.169.254/latest"):
            r = self.client.post("/api/notify/push/subscribe",
                                 json={"endpoint": bad,
                                       "keys": {"p256dh": "k", "auth": "a"}})
            self.assertEqual(r.status_code, 400, bad)

    def test_push_subscribe_accepts_real_service(self):
        r = self.client.post("/api/notify/push/subscribe", json={
            "endpoint": f"https://fcm.googleapis.com/fcm/send/{uuid.uuid4().hex}",
            "keys": {"p256dh": "k", "auth": "a"}})
        self.assertEqual(r.status_code, 200, r.text)

    def test_sms_account_throttle(self):
        """IP rotation can't amplify billable sends: the 4th verification
        SMS from one account is refused even across source IPs."""
        env = {"OIKONOME_TWILIO_ACCOUNT_SID": "AC1",
               "OIKONOME_TWILIO_AUTH_TOKEN": "t",
               "OIKONOME_TWILIO_FROM": "+12175550000"}
        with mock.patch.dict(os.environ, env), \
             mock.patch("oikonome.notify.sms.send", return_value=True):
            codes = 0
            for i in range(4):
                r = self.client.post("/api/notify/phone",
                                     json={"number": f"+1217555{1000+i:04d}",
                                           "consent": ["summary"]})
                if r.status_code == 200:
                    codes += 1
                elif r.status_code == 429:
                    break
            self.assertLessEqual(codes, 3)

    def test_schedule_accepts_yearly_and_channels(self):
        r = self.client.post("/api/settings", json={"email_schedule": {
            "daily": {"on": True, "hour": 7, "sms": True},
            "yearly": {"on": True, "hour": 9, "push": True}}})
        self.assertEqual(r.status_code, 200, r.text)
        j = self.client.get("/api/settings").json()
        self.assertTrue(j["email_schedule"]["daily"]["sms"])
        self.assertTrue(j["email_schedule"]["yearly"]["push"])
        self.assertEqual(j["email_schedule"]["yearly"]["hour"], 9)


class WorkerFanoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_email_off_sms_on_sends_sms_not_email(self):
        from oikonome.jobs import worker
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, f"f-{uuid.uuid4().hex[:8]}")
        finally:
            admin.close()
        conn = tenancy.tenant_connect(tid)
        try:
            from oikonome.engine import budget
            cfg = budget.load_config(conn)
            cfg["email_schedule"] = {"daily": {"on": False, "sms": True,
                                               "hour": 7}}
            cfg["notify_phone"] = {"number": "+12175551234", "verified": True}
            # a household with a PLAN: the scheduled daily verdict is
            # skipped entirely while no budget exists (there is no verdict
            # to report), and this test is about channel routing, not that
            # gate
            cfg["food_monthly"] = 800
            budget.save_config(conn, cfg)
        finally:
            conn.close()
        sent_sms: list = []
        with mock.patch("oikonome.web.report.send") as email_send, \
             mock.patch("oikonome.notify.sms.send",
                        side_effect=lambda to, b: sent_sms.append((to, b))
                        or True):
            worker.email_tenant(tid, send_it=True, cadence="daily")
        email_send.assert_not_called()
        self.assertEqual(len(sent_sms), 1)
        self.assertEqual(sent_sms[0][0], "+12175551234")
        self.assertIn("Oikonome", sent_sms[0][1])

    def test_unverified_phone_never_gets_sms(self):
        from oikonome.jobs import worker
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, f"f-{uuid.uuid4().hex[:8]}")
        finally:
            admin.close()
        conn = tenancy.tenant_connect(tid)
        try:
            from oikonome.engine import budget
            cfg = budget.load_config(conn)
            cfg["email_schedule"] = {"daily": {"on": False, "sms": True,
                                               "hour": 7}}
            cfg["notify_phone"] = {"number": "+12175551234",
                                   "verified": False}
            cfg["food_monthly"] = 800      # see above: a plan must exist
            budget.save_config(conn, cfg)
        finally:
            conn.close()
        with mock.patch("oikonome.notify.sms.send") as sms_send:
            worker.email_tenant(tid, send_it=True, cadence="daily")
        sms_send.assert_not_called()


    def test_a_number_paused_during_the_send_gets_no_text(self):
        """The sweep reads the number, then gathers the month and mails
        every recipient — minutes, on a slow relay. An owner who changes
        their password in that window pauses the number; the text that
        follows must go nowhere, not to the number read before."""
        from oikonome.auth import reset as reset_mod
        from oikonome.jobs import worker
        from oikonome.web import report
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, f"f-{uuid.uuid4().hex[:8]}")
            owner = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s, %s, 'x', 'owner') RETURNING id",
                (tid, f"o-{uuid.uuid4().hex[:8]}@example.dev")
            ).fetchone()["id"]
            admin.commit()
        finally:
            admin.close()
        conn = tenancy.tenant_connect(tid)
        try:
            from oikonome.engine import budget
            cfg = budget.load_config(conn)
            cfg["email_schedule"] = {"daily": {"on": False, "sms": True,
                                               "hour": 7}}
            cfg["notify_phone"] = {"number": "+12175551234", "verified": True}
            cfg["food_monthly"] = 800
            budget.save_config(conn, cfg)
        finally:
            conn.close()
        real_gather = report.gather

        def gather_then_owner_rotates(*a, **kw):
            d = real_gather(*a, **kw)
            cc = tenancy.control_connect()
            try:
                self.assertTrue(reset_mod._unverify_sms_number(cc, owner))
            finally:
                cc.close()
            return d

        with mock.patch("oikonome.web.report.gather",
                        side_effect=gather_then_owner_rotates), \
             mock.patch("oikonome.notify.sms.send") as sms_send:
            worker.email_tenant(tid, send_it=True, cadence="daily")
        sms_send.assert_not_called()

    def test_a_pause_still_committing_when_the_text_goes_gets_no_text(self):
        """The rotation marks the number unverified inside its own
        transaction and commits after the rest of the rotation. A send
        reaching the number in that window must wait for the outcome, not
        read the verified number the rotation is about to pause."""
        import threading
        import time
        from oikonome.jobs import worker
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, f"f-{uuid.uuid4().hex[:8]}")
        finally:
            admin.close()
        conn = tenancy.tenant_connect(tid)
        try:
            from oikonome.engine import budget
            cfg = budget.load_config(conn)
            cfg["notify_phone"] = {"number": "+12175551234", "verified": True}
            budget.save_config(conn, cfg)
        finally:
            conn.close()
        self.assertEqual(worker._live_sms_number(tid), "+12175551234")
        rot = tenancy.admin_connect()
        got = []
        try:
            with rot.transaction():
                rot.execute(
                    "UPDATE tenant_settings SET config = jsonb_set(config, "
                    "'{notify_phone,verified}', 'false') WHERE tenant_id=%s",
                    (tid,))
                t = threading.Thread(
                    target=lambda: got.append(worker._live_sms_number(tid)))
                t.start()
                time.sleep(0.5)        # the rotation is still finishing
        finally:
            rot.close()
        t.join(10)
        self.assertEqual(got, [None])


if __name__ == "__main__":
    unittest.main()
