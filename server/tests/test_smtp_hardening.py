"""SMTP SSRF + recipient policy.

An SMTP host isn't a URL, so netguard's check_url never saw it: a tenant
could point smtp_host at the cloud metadata endpoint or internal services
and let the nightly email job probe them (an SMTP handshake against an
arbitrary port is a serviceable scanner). check_host applies the same
address policy at every door the value can enter: Settings save, .oikx
bundle import, and resolve_smtp at the moment of use (configs that
predate the guard). Self-host keeps LAN relays first-class — only the
metadata endpoint is blocked there.

Recipient policy (hosted only): the daily verdict carries balances and
spending, so free-form recipients let a compromised session exfiltrate
financial mail. Hosted recipients must hold an account on the tenant;
self-host stays free-form (the operator owns the SMTP).
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web.netguard import BlockedURL, check_host

from .util import (_ensure_db, accept_recipient_invite, make_db,
                   write_config)

METADATA_IP = "169.254.169.254"
INTERNAL_IP = "10.1.2.3"
PUBLIC_IP = "8.8.8.8"
PW = "correct-horse-battery"


class CheckHostTests(unittest.TestCase):
    def test_metadata_ip_blocked_in_both_modes(self):
        with self.assertRaises(BlockedURL):
            check_host(METADATA_IP)
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            with self.assertRaises(BlockedURL):
                check_host(METADATA_IP)

    def test_private_ip_blocked_only_when_hosted(self):
        check_host(INTERNAL_IP)                      # self-host: LAN relay ok
        check_host("127.0.0.1")
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            with self.assertRaises(BlockedURL):
                check_host(INTERNAL_IP)
            with self.assertRaises(BlockedURL):
                check_host("127.0.0.1")

    def test_public_ip_passes_everywhere(self):
        check_host(PUBLIC_IP)
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            check_host(PUBLIC_IP)

    def test_empty_host_is_refused(self):
        with self.assertRaises(BlockedURL):
            check_host("")


class SmtpSettingsGuardTests(unittest.TestCase):
    def setUp(self):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        security._limiter._hits.clear()
        from oikonome.web.app import app
        self.client = TestClient(app)
        self.email = f"smtp-guard-{uuid.uuid4().hex[:8]}@example.dev"
        self.client.post("/api/signup",
                         data={"email": self.email, "password": PW})
        from .util import give_totp_factor
        give_totp_factor(self.email)  # hosted writes require a factor

    def _save(self, host):
        return self.client.post("/api/settings",
                                json={"smtp_host": host, "smtp_port": 587})

    def test_metadata_smtp_host_rejected_even_self_hosted(self):
        r = self._save(METADATA_IP)
        self.assertEqual(r.status_code, 400)
        self.assertIn("metadata", r.text.lower())

    def test_lan_relay_allowed_self_hosted(self):
        self.assertEqual(self._save("192.168.1.10").status_code, 200)

    def test_internal_smtp_host_refused_when_hosted(self):
        """A hosted tenant cannot set ANY smtp_* value, internal or public,
        so the request never reaches the SSRF check behind it. 403 is the
        stronger answer — the host owns the mail server, because resolve_smtp
        prefers a tenant relay over the operator's env and the daily verdict
        would otherwise leave through a server the operator does not run.

        The SSRF guard itself is still covered where it is reachable: metadata
        IPs are refused even self-hosted
        (test_metadata_smtp_host_rejected_even_self_hosted), and the
        restore/bundle path has BundleSmtpGuardTests."""
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            internal = self._save(INTERNAL_IP)
            public = self._save(PUBLIC_IP)
        self.assertEqual(internal.status_code, 403)
        # …and not because the address looked internal — a perfectly
        # routable relay is refused identically on hosted
        self.assertEqual(public.status_code, 403)

    def test_hosted_recipients_must_hold_an_account(self):
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            r = self.client.post("/api/settings", json={
                "email_recipients": f"{self.email}, attacker@evil.test"})
            self.assertEqual(r.status_code, 400)
            self.assertIn("attacker@evil.test", r.text)
            r = self.client.post("/api/settings",
                                 json={"email_recipients": self.email})
            self.assertEqual(r.status_code, 200)

    def test_self_host_recipients_stay_free_form(self):
        r = self.client.post("/api/settings", json={
            "email_recipients": "spouse@family.example, me@work.example"})
        self.assertEqual(r.status_code, 200)


class BundleSmtpGuardTests(unittest.TestCase):
    def test_bundle_with_metadata_smtp_host_refused(self):
        from oikonome.sync import connections_bundle as cb
        with self.assertRaises(ValueError) as e:
            cb._check_urls({"smtp_host": METADATA_IP})
        self.assertIn("metadata", str(e.exception).lower())

    def test_bundle_with_public_smtp_host_passes(self):
        from oikonome.sync import connections_bundle as cb
        cb._check_urls({"smtp_host": PUBLIC_IP})     # no raise


class ResolveSmtpBeltTests(unittest.TestCase):
    """Configs written before the guard (or through any future door) are
    re-checked at the moment of use and demoted to the env fallback."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _set_host(self, host):
        from oikonome.engine import budget
        cfg = budget.load_config(self.conn)
        cfg["smtp_host"] = host                      # bypasses the API guard
        budget.save_config(self.conn, cfg)

    def test_blocked_config_host_falls_back_to_env(self):
        from oikonome.web import report
        self._set_host(METADATA_IP)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OIKONOME_SMTP_HOST", None)
            s = report.resolve_smtp(self.conn)
        self.assertNotEqual(s["host"], METADATA_IP)
        self.assertFalse(s["configured"])            # env unset → can't send

    def test_legit_config_host_still_wins(self):
        from oikonome.web import report
        self._set_host("smtp.mailhost.example")
        s = report.resolve_smtp(self.conn)
        self.assertEqual(s["host"], "smtp.mailhost.example")
        self.assertTrue(s["configured"])


class WorkerRecipientFilterTests(unittest.TestCase):
    def setUp(self):
        _ensure_db()
        admin = tenancy.admin_connect()
        self.addCleanup(admin.close)
        self.tid = tenancy.create_tenant(admin, f"recip-filter-{uuid.uuid4().hex[:8]}")
        self.member = f"member-{uuid.uuid4().hex[:6]}@example.dev"
        admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash) "
            "VALUES (%s,%s,'x')", (self.tid, self.member))
        self.conn = tenancy.tenant_connect(self.tid)
        self.addCleanup(self.conn.close)
        write_config(self.conn)

    def _config_recipients(self, recips):
        from oikonome.engine import budget
        cfg = budget.load_config(self.conn)
        cfg["email_recipients"] = recips
        budget.save_config(self.conn, cfg)

    def test_hosted_filters_outsiders(self):
        from oikonome.jobs.worker import _recipients
        self._config_recipients([self.member, "attacker@evil.test"])
        # accept for the member so this test still exercises the
        # membership filter rather than passing by falling through
        # to the every-member fallback
        accept_recipient_invite(self.tid, self.member)
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            got = _recipients(self.conn, self.tid)
        self.assertEqual(got, [self.member])

    def test_hosted_all_outsiders_falls_back_to_members(self):
        from oikonome.jobs.worker import _recipients
        self._config_recipients(["attacker@evil.test"])
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            # an UNVERIFIED owner is not mailed on hosted even from the
            # configured-list branch — both branches demand the same
            # proof of mailbox control
            self.assertEqual(_recipients(self.conn, self.tid), [])
            admin = tenancy.admin_connect()
            try:
                admin.execute("UPDATE users SET verified_at = now() WHERE email=%s",
                              (self.member,))
            finally:
                admin.close()
            got = _recipients(self.conn, self.tid)
        self.assertEqual(got, [self.member])

    def test_self_host_keeps_free_form_recipients_and_adds_the_owner(self):
        """Self-host accepts any address (the operator owns the SMTP) — and
        the owner is on the list too.

        The configured field ADDS to the recipient list, it does not REPLACE it:
        replacing means adding one address silently stops the account owner's own
        daily verdict. Both halves are pinned here."""
        from oikonome.jobs.worker import _recipients
        self._config_recipients(["anyone@anywhere.example"])
        accept_recipient_invite(self.tid, "anyone@anywhere.example")
        got = _recipients(self.conn, self.tid)
        self.assertIn("anyone@anywhere.example", got,
                      "self-host must not filter free-form recipients")
        self.assertIn(self.member, got,
                      "the owner keeps receiving their own verdict")


if __name__ == "__main__":
    unittest.main()
