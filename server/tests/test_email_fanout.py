"""Household mail goes to EVERY recipient, one message each.

A second address on the recipient list can silently never receive the daily
verdict while the first one does, with nothing logging a problem.

The shape that causes it is one message with `To: <sender>` and
`Bcc: <everyone>`. `smtplib.send_message` strips the Bcc header before
transmission, so what arrives at each recipient's provider is a message
addressed to somebody else — a strong "this is spam" signal, applied
per-recipient, which is why one address gets it and another does not.

The failure is also invisible: one blast has one outcome, so the sweep
records a clean send as long as the relay accepts the envelope.

Pinned here:
  * one message per recipient, each addressed to that recipient;
  * the sender is not silently copied on household mail;
  * one bad address does not cost the others their mail;
  * a total failure raises rather than stamping a good heartbeat;
  * the test-email button covers the WHOLE list, not just whoever clicked it.
"""

import unittest
import uuid
from unittest import mock

from oikonome.web import report

from .util import (TEST_DB, _admin_dsn, accept_recipient_invite,
                   make_db, write_config)


class SendEachTests(unittest.TestCase):
    RECIPS = ["first@example.com", "second@example.com", "third@example.com"]
    SMTP = {"host": "smtp.example.com", "port": 587, "user": "u",
            "password": "p", "sender": "no-reply@example.com",
            "starttls": True, "configured": True}

    def test_one_message_per_recipient_each_addressed_to_them(self):
        sent = []
        with mock.patch.object(report, "send",
                               side_effect=lambda s, p, h, r, **kw: sent.append(
                                   (r, kw.get("bcc")))):
            out = report.send_each("s", "p", "<p>h</p>", self.RECIPS,
                                   smtp=self.SMTP)
        self.assertEqual([r for r, _ in sent], [[a] for a in self.RECIPS],
                         "each recipient must get their OWN message")
        self.assertTrue(all(bcc is False for _, bcc in sent),
                        "household mail must be addressed TO the recipient — "
                        "a Bcc blast reads as spam to the provider")
        self.assertTrue(all(r["ok"] for r in out))
        self.assertEqual([r["email"] for r in out], self.RECIPS)

    def test_one_bad_address_does_not_cost_the_others_their_mail(self):
        def flaky(subject, plain, html, recips, **kw):
            if recips == ["second@example.com"]:
                raise OSError("mailbox unavailable")
        with mock.patch.object(report, "send", side_effect=flaky):
            out = report.send_each("s", "p", "h", self.RECIPS, smtp=self.SMTP)
        by = {r["email"]: r for r in out}
        self.assertTrue(by["first@example.com"]["ok"])
        self.assertFalse(by["second@example.com"]["ok"])
        self.assertIn("mailbox unavailable", by["second@example.com"]["error"])
        self.assertTrue(by["third@example.com"]["ok"],
                        "a failure must not abort the remaining recipients")

    def test_envelope_carries_only_that_recipient(self):
        """The real transport, faked at smtplib — proves the header the
        provider actually sees names the person receiving it."""
        seen = []

        class FakeSMTP:
            def __init__(self, host, port, timeout=None): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self, context=None): pass
            def login(self, u, p): pass
            def send_message(self, msg, **kw):
                seen.append({"To": msg["To"], "Bcc": msg["Bcc"]})

        with mock.patch("oikonome.web.report.smtplib.SMTP", FakeSMTP):
            report.send_each("s", "p", "<p>h</p>", self.RECIPS, smtp=self.SMTP)
        self.assertEqual([m["To"] for m in seen], self.RECIPS)
        self.assertTrue(all(m["Bcc"] is None for m in seen),
                        "no Bcc at all now — nobody is hidden behind another "
                        "address, and nobody sees a co-recipient")


class DailyEmailFanoutTests(unittest.TestCase):
    """The daily verdict, end to end through `email_tenant`."""

    def setUp(self):
        self.conn = make_db()
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        write_config(self.conn,
                     email_recipients=["one@example.com", "two@example.com"])
        # being on the list proposes an address; the person on the
        # other end enrols it. These two said yes.
        accept_recipient_invite(self.tid, "one@example.com", "two@example.com")

    def tearDown(self):
        self.conn.close()

    def _run(self, outcome):
        from oikonome.jobs import worker
        from oikonome.web import report
        with mock.patch.object(report, "send_each", return_value=outcome) as m:
            subject = worker.email_tenant(self.tid, send_it=True,
                                          force_email=True)
        return subject, m

    def test_every_configured_recipient_is_mailed(self):
        _, m = self._run([{"email": "one@example.com", "ok": True, "error": None},
                          {"email": "two@example.com", "ok": True, "error": None}])
        self.assertEqual(m.call_args[0][3],
                         ["one@example.com", "two@example.com"],
                         "the daily verdict must go to the WHOLE list — a "
                         "second address that never receives it is the bug "
                         "this pins")

    def test_a_send_where_nobody_received_it_is_an_error(self):
        err = "SMTPAuthenticationError: bad creds"
        with self.assertRaises(RuntimeError) as cm:
            self._run([{"email": "one@example.com", "ok": False, "error": err},
                       {"email": "two@example.com", "ok": False, "error": err}])
        self.assertIn("every recipient", str(cm.exception))

    def test_a_partial_failure_still_delivers_to_the_rest(self):
        subject, _ = self._run(
            [{"email": "one@example.com", "ok": True, "error": None},
             {"email": "two@example.com", "ok": False, "error": "boom"}])
        self.assertTrue(subject, "one bad address must not fail the run")



class OwnerAlwaysReceivesTests(unittest.TestCase):
    """Filling in the recipients field must not cut the owner out.

    Read as a REPLACEMENT for the recipient list, adding one address
    silently stops the owner's own daily verdict — and nothing says so, in
    the UI or the logs. "Add a person" is what the field says and what
    everyone means by it.
    """

    def setUp(self):
        from oikonome.db import tenancy
        self.conn = make_db()
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        write_config(self.conn)
        u = uuid.uuid4().hex[:8]
        self.owner = f'owner-{u}@example.com'
        self.viewer = f'viewer-{u}@example.com'
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s,%s,'x','owner',now())",
                (self.tid, self.owner))
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s,%s,'x','viewer',now())",
                (self.tid, self.viewer))
        finally:
            admin.close()

    def tearDown(self):
        self.conn.close()

    def _recips(self):
        from oikonome.jobs.worker import _recipients
        return _recipients(self.conn, self.tid)

    def test_owner_is_added_to_the_configured_list(self):
        write_config(self.conn, email_recipients=["partner@example.com"])
        accept_recipient_invite(self.tid, "partner@example.com")
        got = self._recips()
        self.assertIn(self.owner, got,
                      "the owner must still receive their OWN daily verdict "
                      "after adding somebody else")
        self.assertIn("partner@example.com", got)

    def test_owner_listed_explicitly_is_not_mailed_twice(self):
        write_config(self.conn,
                     email_recipients=[self.owner.upper(), "p@example.com"])
        accept_recipient_invite(self.tid, self.owner, "p@example.com")
        got = self._recips()
        self.assertEqual(len([e for e in got if e.lower() == self.owner.lower()]),
                         1, "listing yourself must not mail you twice — the "
                            "owner is folded in case-insensitively")
        self.assertIn("p@example.com", got)

    def test_no_configured_list_still_means_every_member(self):
        got = self._recips()
        self.assertIn(self.owner, got)
        self.assertIn(self.viewer, got)


if __name__ == "__main__":
    unittest.main()


class MailableBaseTests(unittest.TestCase):
    """No LAN/IP links in recurring mail — the big providers filter the
    whole message.

    A daily verdict whose only link is a LAN address
    (http://192.168.1.50:8042/app/) is filtered, while the same message
    without the link arrives. Every LAN self-host has exactly that shape of base URL, so the daily verdict
    (the product's core artifact) would be filtered for the whole class of
    installs this app is built for.
    """

    def _base(self, value):
        with mock.patch.dict("os.environ", {"OIKONOME_BASE_URL": value}):
            return report.mailable_base()

    def test_lan_ip_http_is_not_mailable(self):
        self.assertEqual(self._base("http://192.168.1.50:8042"), "")

    def test_even_https_to_an_ip_is_not_mailable(self):
        self.assertEqual(self._base("https://192.168.1.50:8042"), "")
        self.assertEqual(self._base("https://203.0.113.7"), "")

    def test_http_to_a_domain_is_not_mailable(self):
        self.assertEqual(self._base("http://oikonome.example.com"), "")

    def test_lan_suffixes_are_not_mailable(self):
        self.assertEqual(self._base("https://nas.local"), "")
        self.assertEqual(self._base("https://box.lan"), "")

    def test_public_https_domain_is_mailable(self):
        self.assertEqual(self._base("https://money.example.org"),
                         "https://money.example.org")

    def test_unset_is_not_mailable(self):
        self.assertEqual(self._base(""), "")

    def test_daily_email_has_no_lan_link(self):
        """End to end: with a LAN base, the rendered daily email must not
        contain the base URL anywhere — html or plain."""
        import datetime as dt
        conn = make_db()
        try:
            write_config(conn)
            with mock.patch.dict("os.environ",
                                 {"OIKONOME_BASE_URL": "http://192.168.1.50:8042"}):
                d = report.gather(conn, dt.date.today())
                _, plain, html = report.build(d)
            self.assertNotIn("192.168.1.50", html,
                             "a LAN URL in the body is the exact string that "
                             "got the message spam-filtered")
            self.assertNotIn("192.168.1.50", plain)
        finally:
            conn.close()

    def test_daily_email_keeps_the_link_on_a_public_domain(self):
        import datetime as dt
        conn = make_db()
        try:
            write_config(conn)
            with mock.patch.dict("os.environ",
                                 {"OIKONOME_BASE_URL": "https://money.example.org"}):
                d = report.gather(conn, dt.date.today())
                _, plain, html = report.build(d)
            self.assertIn("https://money.example.org/app/", html)
            self.assertIn("https://money.example.org/app/", plain)
        finally:
            conn.close()
