"""Who OIKONOME_SMTP_NO_VERIFY reaches.

On a self-hosted instance the operator who sets the env flag is the person
who saves a relay in Settings, and the setup wizard steers a mail-bridge
user (self-signed certificate on the LAN) to Settings. If the flag stopped
at the env relay, that relay could not send at all short of turning
STARTTLS off, which sends the relay password in the clear. So on self-host
the flag reaches a Settings relay on the operator's own network (loopback, a
private address, a bare LAN hostname), and the send path and the health probe
agree about it. A Settings relay on a public host keeps its certificate
check: the flag was set for the operator's bridge, and a relay another
household saved, or one later repointed at a public host, would otherwise
have its password sent to whoever answers. On hosted it never reaches a
household's relay: a tenant row is ignored there, and the flag cannot be
what turns certificate checks off for a relay somebody other than the
operator chose."""

import os
import ssl
import unittest
from unittest import mock

from oikonome.notify import mailhealth
from oikonome.web import report


def _tenant_relay(hosted=False, flag=True, host="192.168.50.7"):
    cfg = {"smtp_host": host, "smtp_port": 587,
           "smtp_from": "oiko@example.dev", "smtp_starttls": True}
    env = {"OIKONOME_SMTP_NO_VERIFY": "1" if flag else "",
           "OIKONOME_HOSTED": "1" if hosted else ""}
    with mock.patch.dict(os.environ, env), \
         mock.patch("oikonome.engine.budget.load_config", return_value=cfg), \
         mock.patch("oikonome.web.netguard.check_host", return_value=None):
        return report.resolve_smtp(object())


def _starttls_ctx(send):
    smtp_cls = mock.MagicMock()
    conn = smtp_cls.return_value.__enter__.return_value
    with mock.patch.dict(os.environ, {"OIKONOME_SMTP_NO_VERIFY": "1"}), \
         mock.patch("smtplib.SMTP", smtp_cls), \
         mock.patch("oikonome.web.netguard.pinned_smtp_host",
                    side_effect=lambda h: h):
        send()
    conn.starttls.assert_called_once()
    return conn.starttls.call_args.kwargs["context"]


class NoVerifyReachesTheSelfHostSettingsRelay(unittest.TestCase):
    def test_self_host_settings_relay_sends_unverified_with_the_flag(self):
        relay = _tenant_relay()
        self.assertEqual(relay["host"], "192.168.50.7")
        ctx = _starttls_ctx(lambda: report.send(
            "s", "p", "<p>h</p>", ["to@example.dev"], smtp=relay, bcc=False))
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)
        self.assertFalse(ctx.check_hostname)

    def test_health_probe_agrees_with_the_send_path(self):
        relay = _tenant_relay()
        ctx = _starttls_ctx(lambda: mailhealth.probe(relay))
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)

    def test_self_host_settings_relay_verifies_without_the_flag(self):
        relay = _tenant_relay(flag=False)
        self.assertFalse(report.tls_unverified(relay))

    def test_hosted_never_sends_a_household_relay_unverified(self):
        relay = _tenant_relay(hosted=True)
        # hosted mail goes through the operator's relay, never the row
        self.assertNotEqual(relay["host"], "192.168.50.7")

    def test_hosted_settings_branch_refuses_the_flag_on_its_own(self):
        """The early hosted return is one guard; the Settings branch must
        not depend on it, so a reorder cannot hand the flag to a tenant."""
        cfg = {"smtp_host": "192.168.50.7", "smtp_starttls": True}
        real = report.env_flag
        calls = {"n": 0}

        def flag(name):
            # skip ONLY the early hosted return, then report hosted
            if name == "OIKONOME_HOSTED":
                calls["n"] += 1
                return calls["n"] > 1
            return real(name)
        with mock.patch.dict(os.environ, {"OIKONOME_SMTP_NO_VERIFY": "1"}), \
             mock.patch.object(report, "env_flag", side_effect=flag), \
             mock.patch("oikonome.engine.budget.load_config",
                        return_value=cfg), \
             mock.patch("oikonome.web.netguard.check_host",
                        return_value=None):
            relay = report.resolve_smtp(object())
        self.assertEqual(relay["host"], "192.168.50.7")
        self.assertFalse(report.tls_unverified(relay))

    def test_lan_shaped_relay_hosts_take_the_flag(self):
        for host in ("127.0.0.1", "::1", "10.1.2.3", "172.16.0.9",
                     "fe80::1", "mailbridge", "localhost"):
            with self.subTest(host=host):
                self.assertTrue(report.tls_unverified(
                    _tenant_relay(host=host)))

    def test_public_host_settings_relay_still_verifies(self):
        """A relay saved in Settings on a public host is checked even with
        the flag on — otherwise repointing the bridge's entry at a public
        relay hands its password to whoever answers on the way."""
        for host in ("relay.example.dev", "203.0.113.9", "mail.example.lan",
                     "2001:db8::25"):
            with self.subTest(host=host):
                relay = _tenant_relay(host=host)
                self.assertEqual(relay["host"], host)
                self.assertFalse(report.tls_unverified(relay))
        ctx = _starttls_ctx(lambda: report.send(
            "s", "p", "<p>h</p>", ["to@example.dev"],
            smtp=_tenant_relay(host="relay.example.dev"), bcc=False))
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_another_tenants_relay_is_not_covered_by_the_flag(self):
        """Two households on one self-hosted instance: the operator's flag
        covers the operator's LAN bridge, and a second household's own
        relay on a public host is still verified — resolved per tenant,
        never inherited from the first."""
        mine = _tenant_relay(host="10.0.0.25")
        theirs = _tenant_relay(host="smtp.other-household.example")
        self.assertTrue(report.tls_unverified(mine))
        self.assertFalse(report.tls_unverified(theirs))
        ctx = _starttls_ctx(lambda: mailhealth.probe(theirs))
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)

    def test_env_relay_still_honours_the_flag(self):
        with mock.patch.dict(os.environ, {"OIKONOME_SMTP_NO_VERIFY": "1",
                                          "OIKONOME_SMTP_HOST":
                                              "bridge.example.dev"}):
            relay = report._env_smtp()
        ctx = _starttls_ctx(lambda: report.send(
            "s", "p", "<p>h</p>", ["to@example.dev"], smtp=relay, bcc=False))
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)


if __name__ == "__main__":
    unittest.main()
