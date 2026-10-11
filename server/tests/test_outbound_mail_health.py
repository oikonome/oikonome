"""Outbound mail must fail LOUDLY. A dead relay must never leave the console
saying "invite emailed" while every transactional send dies in a daemon
thread: the instance-wide health record, the half-hourly probe, and the
three places that read them (console banner, mint door, Doctor) make it
something an operator can see."""
import os
import smtplib
import unittest
import uuid
from unittest.mock import patch

from oikonome.db import tenancy
from oikonome.notify import mailhealth

from .util import _ensure_db

KEYS = (mailhealth.K_OK, mailhealth.K_FAIL, mailhealth.K_PROBE)


class MailHealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect()
        self.admin.execute("DELETE FROM ops_state WHERE key = ANY(%s)",
                           (list(KEYS),))

    def tearDown(self):
        self.admin.execute("DELETE FROM ops_state WHERE key = ANY(%s)",
                           (list(KEYS),))
        self.admin.close()

    def test_quiet_instance_is_not_failing(self):
        st = mailhealth.status(self.admin)
        self.assertFalse(st["failing"])
        self.assertEqual(st["reason"], "")

    def test_a_failed_send_after_the_last_success_reads_as_failing(self):
        mailhealth.record(True, kind="verdict", to=["a@x"])
        self.assertFalse(mailhealth.status(self.admin)["failing"])
        mailhealth.record(False, kind="Your Oikonome invite",
                          to=["b@x"], error="OSError: timed out")
        st = mailhealth.status(self.admin)
        self.assertTrue(st["failing"])
        self.assertIn("timed out", st["reason"])
        # addresses are never persisted — bookkeeping, not a log
        self.assertNotIn("b@x", str(st))
        # a later success clears it
        mailhealth.record(True, kind="verdict", to=["a@x"])
        self.assertFalse(mailhealth.status(self.admin)["failing"])

    def test_failed_probe_is_failing_and_keeps_its_since(self):
        with patch.object(mailhealth, "probe",
                          return_value={"ok": False, "at": "2026-08-13T17:00:00+00:00",
                                        "host": "r", "port": 587,
                                        "error": "OSError: blocked"}):
            self.assertTrue(mailhealth.run_probe().startswith("FAIL"))
        with patch.object(mailhealth, "probe",
                          return_value={"ok": False, "at": "2026-08-13T17:30:00+00:00",
                                        "host": "r", "port": 587,
                                        "error": "OSError: blocked"}):
            mailhealth.run_probe()
        st = mailhealth.status(self.admin)
        self.assertTrue(st["failing"])
        self.assertEqual(st["probe"]["since"], "2026-08-13T17:00:00+00:00")
        self.assertIn("since 2026-08-13T17:00:00", st["reason"])

    def test_recovery_mails_the_operator_once(self):
        sent = []
        bad = {"ok": False, "at": "t1", "host": "r", "port": 2525,
               "error": "OSError: blocked"}
        good = {"ok": True, "at": "t2", "host": "r", "port": 2525}
        with patch.object(mailhealth, "probe", return_value=bad):
            mailhealth.run_probe(mail_operator=lambda *a: sent.append(a))
        self.assertEqual(sent, [])             # can't mail about a dead path
        with patch.object(mailhealth, "probe", return_value=good):
            self.assertEqual(mailhealth.run_probe(
                mail_operator=lambda *a: sent.append(a)), "restored (mailed)")
            self.assertEqual(mailhealth.run_probe(
                mail_operator=lambda *a: sent.append(a)), "ok")
        self.assertEqual(len(sent), 1)
        self.assertIn("restored", sent[0][0])
        self.assertFalse(mailhealth.status(self.admin)["failing"])

    def test_subject_line_is_never_persisted_only_its_class(self):
        # an invite subject carries the inviter's address, and ops_state is
        # read by every tenant's Doctor — only a coarse label may survive
        mailhealth.record(False, kind="owner@household-a.example invited you "
                          "to their daily Oikonome summary",
                          to=["b@x"], error="OSError: timed out")
        st = mailhealth.status(self.admin)
        self.assertTrue(st["failing"])
        self.assertNotIn("household-a", str(st))
        self.assertNotIn("@", str(st))
        self.assertEqual(st["last_fail"]["kind"], "invite")
        self.assertIn("(invite)", st["reason"])

    def test_error_text_is_scrubbed_of_addresses_and_relay_payloads(self):
        exc = smtplib.SMTPRecipientsRefused(
            {"someone@example.com": (550, b"Mailbox unavailable")})
        mailhealth.record(False, kind="Your Oikonome invite",
                          to=["someone@example.com"], error=exc)
        st = mailhealth.status(self.admin)
        self.assertNotIn("someone", str(st))
        self.assertNotIn("example.com", str(st))
        self.assertNotIn("550", str(st))
        self.assertEqual(st["last_fail"]["error"], "SMTPRecipientsRefused")
        # the plain-string form (older callers) is scrubbed the same way
        self.assertEqual(
            mailhealth.sanitize_error("SMTPAuthenticationError: (535, "
                                      "b'5.7.8 bad login for ops@relay.x')"),
            "SMTPAuthenticationError")
        self.assertEqual(
            mailhealth.sanitize_error(OSError(111, "Connection refused")),
            "ConnectionRefusedError: Connection refused")
        # multi-line relay chatter keeps only the first line
        self.assertNotIn("secret", mailhealth.sanitize_error(
            "OSError: first line\nuser=secret host=relay.internal"))

    def test_probe_failure_is_recorded_scrubbed(self):
        smtp = {"host": "relay.invalid", "port": 2525, "user": "ops@r.x",
                "password": "p", "sender": "x@y", "starttls": False,
                "configured": True}
        err = smtplib.SMTPAuthenticationError(
            535, b"5.7.8 Authentication failed for ops@r.x")
        with patch.object(mailhealth.smtplib, "SMTP", side_effect=err):
            res = mailhealth.probe(smtp)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "SMTPAuthenticationError")
        self.assertNotIn("ops@", res["error"])

    def test_newest_signal_wins_between_sends_and_probes(self):
        def at(t):
            return patch.object(mailhealth, "_now", return_value=t)
        # one bounced send, then the half-hourly probe passes: not failing
        with at("2026-08-13T10:00:00+00:00"):
            mailhealth.record(False, kind="invite", to=["a@x"],
                              error="SMTPRecipientsRefused: {}")
        self.assertTrue(mailhealth.status(self.admin)["failing"])
        with patch.object(mailhealth, "probe", return_value={
                "ok": True, "at": "2026-08-13T10:30:00+00:00",
                "host": "r", "port": 2525}):
            mailhealth.run_probe()
        st = mailhealth.status(self.admin)
        self.assertFalse(st["failing"], st)
        # a good send, then a LATER failed probe: failing, reason = probe
        with at("2026-08-13T11:00:00+00:00"):
            mailhealth.record(True, kind="verdict", to=["a@x"])
        with patch.object(mailhealth, "probe", return_value={
                "ok": False, "at": "2026-08-13T11:30:00+00:00",
                "host": "r", "port": 2525, "error": "OSError: blocked"}):
            mailhealth.run_probe()
        st = mailhealth.status(self.admin)
        self.assertTrue(st["failing"])
        self.assertIn("relay probe failing", st["reason"])
        # a good send newer than that failed probe clears it again
        with at("2026-08-13T12:00:00+00:00"):
            mailhealth.record(True, kind="verdict", to=["a@x"])
        self.assertFalse(mailhealth.status(self.admin)["failing"])
        # and a failed send newer than everything trips it, reason = send
        with at("2026-08-13T12:30:00+00:00"):
            mailhealth.record(False, kind="reset", to=["a@x"],
                              error="OSError: timed out")
        st = mailhealth.status(self.admin)
        self.assertTrue(st["failing"])
        self.assertIn("last send failed", st["reason"])

    def test_probe_without_smtp_is_a_skip(self):
        # no relay anywhere: neither the env nor any household's Settings
        with patch.dict(os.environ, {"OIKONOME_SMTP_HOST": ""}), \
                patch.object(mailhealth, "_settings_relays", return_value=[]):
            self.assertIn("skipped", mailhealth.run_probe())

    def test_self_host_probes_the_settings_relay_that_sends_use(self):
        """On a self-hosted instance a relay saved in Settings wins over the
        env one for every real send, so the probe must knock on THAT door:
        probing the unused env relay reads green while every send fails,
        and with only a Settings relay it would probe nothing at all."""
        from oikonome.web import netguard
        from .util import make_db, write_config
        conn = make_db()
        self.addCleanup(conn.close)
        tid = str(conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        write_config(conn, smtp_host="relay.settings-side.example",
                     smtp_port=2526, smtp_starttls=False)
        # oldest household on the instance, so the pick is this one
        self.admin.execute("UPDATE tenants SET created_at='1990-01-01' "
                           "WHERE id=%s", (tid,))

        def _retire():
            admin = tenancy.admin_connect()
            try:
                admin.execute("UPDATE tenants SET status='deleting' "
                              "WHERE id=%s", (tid,))
            finally:
                admin.close()
        self.addCleanup(_retire)
        dialled = []

        class _SMTP:
            def __init__(self, host, port, timeout=None):
                dialled.append(port)
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def ehlo(self):
                pass
            def starttls(self, context=None):
                pass
            def login(self, user, password):
                pass

        env = {"OIKONOME_SMTP_HOST": "relay.env-side.example",
               "OIKONOME_SMTP_PORT": "2525", "OIKONOME_HOSTED": ""}
        with patch.dict(os.environ, env), \
                patch.object(netguard, "check_host", lambda *a, **k: None), \
                patch.object(netguard, "pinned_smtp_host", lambda h: h), \
                patch.object(mailhealth.smtplib, "SMTP", _SMTP):
            self.assertTrue(mailhealth.configured())
            self.assertEqual(mailhealth.run_probe(), "ok")
        # the Settings relay is dialled (the env one too, when another
        # household on the instance still sends through it)
        self.assertIn(2526, dialled)
        st = mailhealth.status(self.admin)
        self.assertEqual(st["probe"]["host"], "relay.settings-side.example")

    def test_every_households_relay_is_probed_not_only_the_oldest(self):
        """Two households, two relays: a dead second relay must turn the
        probe red — probing only the oldest household's relay reads green
        while every send from the other household fails."""
        from oikonome.web import netguard
        from .util import make_db, write_config
        tids = []
        for host, port, created in (
                ("relay.first.example", 2531, "1990-01-01"),
                ("relay.second.example", 2532, "1991-01-01")):
            conn = make_db()
            self.addCleanup(conn.close)
            tid = str(conn.execute(
                "SELECT current_setting('app.tenant_id') AS t"
            ).fetchone()["t"])
            write_config(conn, smtp_host=host, smtp_port=port,
                         smtp_starttls=False)
            self.admin.execute("UPDATE tenants SET created_at=%s "
                               "WHERE id=%s", (created, tid))
            tids.append(tid)

        def _retire():
            admin = tenancy.admin_connect()
            try:
                admin.execute("UPDATE tenants SET status='deleting' "
                              "WHERE id = ANY(%s)", (tids,))
            finally:
                admin.close()
        self.addCleanup(_retire)
        dialled = []

        class _SMTP:
            def __init__(self, host, port, timeout=None):
                dialled.append(port)
                if port == 2532:
                    raise OSError("connection refused")
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def ehlo(self):
                pass
            def starttls(self, context=None):
                pass
            def login(self, user, password):
                pass

        env = {"OIKONOME_SMTP_HOST": "", "OIKONOME_HOSTED": ""}
        with patch.dict(os.environ, env), \
                patch.object(netguard, "check_host", lambda *a, **k: None), \
                patch.object(netguard, "pinned_smtp_host", lambda h: h), \
                patch.object(mailhealth.smtplib, "SMTP", _SMTP):
            self.assertTrue(mailhealth.run_probe().startswith("FAIL"))
        self.assertIn(2531, dialled)
        self.assertIn(2532, dialled)
        st = mailhealth.status(self.admin)
        self.assertTrue(st["failing"])
        self.assertEqual(st["probe"]["host"], "relay.second.example")
        # other households on the shared test database may carry relays
        # of their own, so the count is a floor
        self.assertGreaterEqual(st["probe"]["relays"], 2)
        self.assertEqual(st["probe"]["relays_failing"], 1)

    def test_one_relay_shared_by_two_households_is_dialled_once(self):
        from .util import make_db, write_config
        tids = []
        for _ in range(2):
            conn = make_db()
            self.addCleanup(conn.close)
            tids.append(str(conn.execute(
                "SELECT current_setting('app.tenant_id') AS t"
            ).fetchone()["t"]))
            write_config(conn, smtp_host="relay.shared.example",
                         smtp_port=2533, smtp_starttls=False)

        def _retire():
            admin = tenancy.admin_connect()
            try:
                admin.execute("UPDATE tenants SET status='deleting' "
                              "WHERE id = ANY(%s)", (tids,))
            finally:
                admin.close()
        self.addCleanup(_retire)
        with patch.dict(os.environ, {"OIKONOME_HOSTED": ""}):
            hosts = [(s["host"], s["port"]) for s in mailhealth.relays()]
        self.assertEqual(hosts.count(("relay.shared.example", 2533)), 1)

    def test_env_relay_is_probed_even_when_every_household_has_its_own(self):
        """Recovery mail (reset, unlock, factor-cleared) always goes through
        the env relay when one is set, whatever the households saved in
        Settings. So a dead env relay must turn the probe red even when
        every household sends its reports through a working Settings relay
        — otherwise every recovery link is lost while health reads green.
        (No household on the instance sends reports through the env relay:
        the test database is shared, so that is stated rather than built.)"""
        from oikonome.web import netguard
        settings = {"configured": True, "host": "relay.settings-only.example",
                    "port": 2534, "user": None, "password": None,
                    "starttls": False, "no_verify": False}
        dialled = []

        class _SMTP:
            def __init__(self, host, port, timeout=None):
                dialled.append(port)
                if port == 2535:
                    raise OSError("connection refused")
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def ehlo(self):
                pass
            def starttls(self, context=None):
                pass
            def login(self, user, password):
                pass

        env = {"OIKONOME_SMTP_HOST": "relay.env-dead.example",
               "OIKONOME_SMTP_PORT": "2535", "OIKONOME_SMTP_STARTTLS": "0",
               "OIKONOME_HOSTED": ""}
        with patch.dict(os.environ, env), \
                patch.object(mailhealth, "_settings_relays",
                             return_value=[settings]), \
                patch.object(mailhealth, "_env_relay_in_use", create=True,
                             return_value=False), \
                patch.object(netguard, "check_host", lambda *a, **k: None), \
                patch.object(netguard, "pinned_smtp_host", lambda h: h), \
                patch.object(mailhealth.smtplib, "SMTP", _SMTP):
            hosts = [s["host"] for s in mailhealth.relays()]
            self.assertIn("relay.env-dead.example", hosts)
            self.assertTrue(mailhealth.run_probe().startswith("FAIL"))
        self.assertIn(2534, dialled)
        self.assertIn(2535, dialled)
        st = mailhealth.status(self.admin)
        self.assertTrue(st["failing"])
        self.assertEqual(st["probe"]["host"], "relay.env-dead.example")

    def test_send_failure_is_recorded_by_report(self):
        from oikonome.web import report
        smtp = {"host": "relay.invalid", "port": 2525, "user": None,
                "password": None, "sender": "x@y", "starttls": False,
                "configured": True}
        with patch.object(report, "_smtp_deliver",
                          side_effect=OSError("connection refused")):
            with self.assertRaises(OSError):
                report.send("subj", "p", "<p>p</p>", ["a@x"], smtp=smtp,
                            bcc=False)
        st = mailhealth.status(self.admin)
        self.assertTrue(st["failing"])
        self.assertIn("connection refused", st["reason"])
        with patch.object(report, "_smtp_deliver", return_value=None):
            report.send("subj", "p", "<p>p</p>", ["a@x"], smtp=smtp,
                        bcc=False)
        self.assertFalse(mailhealth.status(self.admin)["failing"])


class ConsoleAndDoctorSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect()
        self.admin.execute("DELETE FROM ops_state WHERE key = ANY(%s)",
                           (list(KEYS),))

    def tearDown(self):
        self.admin.execute("DELETE FROM ops_state WHERE key = ANY(%s)",
                           (list(KEYS),))
        self.admin.close()

    def test_console_health_helper_follows_status(self):
        from oikonome.web import adminconsole
        self.assertFalse(adminconsole._mail_health()["failing"])
        mailhealth.record(False, kind="reset", to=["a@x"], error="X: boom")
        mh = adminconsole._mail_health()
        self.assertTrue(mh["failing"])
        self.assertIn("boom", mh["reason"])

    def test_doctor_names_failing_mail_without_the_host(self):
        from oikonome.web import doctor

        from .util import make_db
        conn = make_db()
        try:
            tid = conn.execute("SELECT current_setting('app.tenant_id') AS t"
                               ).fetchone()["t"]
            with patch.object(mailhealth, "_now",
                              return_value="2026-08-13T17:00:00+00:00"):
                mailhealth.record(False, kind="invite", to=["a@x"],
                                  error=OSError("relay.example.com refused "
                                                "ops@relay.example.com"))
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith("OIKONOME_LLM")}
            env.update({"OIKONOME_SMTP_HOST": "relay.example.com",
                        "OIKONOME_HOSTED": "1"})
            with patch.dict(os.environ, env, clear=True):
                rows = [r for r in doctor.checks(tid)
                        if r["name"] == "outbound mail"]
            self.assertEqual(len(rows), 1)
            self.assertFalse(rows[0]["ok"])
            self.assertIn("FAILING", rows[0]["detail"])
            # any tenant's viewer reads this row: failure class + since,
            # never the message, the relay, or an address
            self.assertIn("OSError", rows[0]["detail"])
            self.assertIn("since 2026-08-13T17:00:00", rows[0]["detail"])
            self.assertNotIn("relay.example.com", rows[0]["detail"])
            self.assertNotIn("refused", rows[0]["detail"])
            self.assertNotIn("@", rows[0]["detail"])
        finally:
            conn.close()

    def test_hosted_unsent_link_log_line_is_not_a_delivery_receipt(self):
        from oikonome.web import app as appmod
        with patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}), \
                self.assertLogs("oikonome.auth", level="WARNING") as cm:
            appmod._log_link("password reset link", "u@x", "/reset?t=abc")
        line = "\n".join(cm.output)
        self.assertNotIn("sent by email", line)
        self.assertNotIn("/reset?t=abc", line)
        self.assertIn("NOT emailed", line)


class ConsoleDoorTests(unittest.TestCase):
    """The console's invite and decline doors say mail is failing when it
    is — an invite never reads "emailed" over a dead relay."""
    TOKEN = "test-admin-token-" + "x" * 32

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        from fastapi.testclient import TestClient

        from oikonome.web import security
        os.environ["OIKONOME_ADMIN_TOKEN"] = self.TOKEN
        security._limiter._hits.clear()
        self.client = TestClient(self.appmod.app)
        self.client.post("/admin/console/login", data={"token": self.TOKEN},
                         follow_redirects=False)
        self.admin = tenancy.admin_connect()
        self.admin.execute("DELETE FROM ops_state WHERE key = ANY(%s)",
                           (list(KEYS),))

    def tearDown(self):
        self.admin.execute("DELETE FROM ops_state WHERE key = ANY(%s)",
                           (list(KEYS),))
        self.admin.close()
        os.environ.pop("OIKONOME_ADMIN_TOKEN", None)

    def test_invite_door_and_banner_say_mail_is_failing(self):
        email = f"door-{uuid.uuid4().hex[:8]}@x.dev"
        r = self.client.post("/admin/console/invite", data={"email": email})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Invite minted and emailed", r.text)
        self.assertNotIn("Outbound mail is failing", r.text)
        mailhealth.record(False, kind="Your Oikonome invite",
                          to=["a@x"], error="OSError: timed out")
        with patch("oikonome.web.app._deliver_invite"), \
                patch.object(mailhealth, "status",
                             wraps=mailhealth.status) as spy:
            r = self.client.post("/admin/console/invite",
                                 data={"email": email + "2"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("OUTBOUND MAIL IS FAILING", r.text)
        self.assertNotIn("minted and emailed", r.text)
        self.assertIn("/signup?invite=", r.text)     # the link to hand over
        self.assertIn("Outbound mail is failing", r.text)   # the red banner
        # one request, one answer — the notice and the banner share it
        self.assertEqual(spy.call_count, 1)

