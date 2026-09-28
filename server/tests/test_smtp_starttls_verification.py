"""report.send's STARTTLS upgrade must verify the relay's certificate.

With smtplib's default (non-verifying) SSL context, a man-in-the-middle
between the app and the SMTP relay can present any certificate and read
the daily verdict plus the SMTP password. STARTTLS is therefore called
with ssl.create_default_context (chain + hostname verified).

Escape hatch: a local mail bridge or your own relay is a first-class
provider, and a local mail bridge presents a self-signed
certificate on localhost — OIKONOME_SMTP_NO_VERIFY=1 keeps those relays
working (encrypted, unverified), documented in the quickstart's SMTP
section."""

import os
import ssl
import unittest
from unittest import mock

from oikonome.web import report


def _send(**env):
    """Run report.send against a mocked SMTP; return the starttls call."""
    smtp_cls = mock.MagicMock()
    smtp_conn = smtp_cls.return_value.__enter__.return_value
    with mock.patch.dict(os.environ, env), \
         mock.patch.object(report.smtplib, "SMTP", smtp_cls):
        report.send("subj", "plain", "<p>html</p>", ["to@example.dev"],
                    smtp={"host": "mail.example.dev", "port": 587,
                          "user": None, "password": None,
                          "sender": "oiko@example.dev", "starttls": True,
                          "configured": True})
    smtp_conn.starttls.assert_called_once()
    return smtp_conn.starttls.call_args


class SmtpStartTlsVerificationTests(unittest.TestCase):
    def test_starttls_verifies_certificates_by_default(self):
        call = _send()
        ctx = call.kwargs.get("context")
        self.assertIsInstance(ctx, ssl.SSLContext)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_no_verify_escape_hatch_for_self_signed_relays(self):
        call = _send(OIKONOME_SMTP_NO_VERIFY="1")
        ctx = call.kwargs.get("context")
        self.assertIsInstance(ctx, ssl.SSLContext)
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)
        self.assertFalse(ctx.check_hostname)


if __name__ == "__main__":
    unittest.main()
