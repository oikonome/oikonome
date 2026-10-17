"""The signup confirmation mail goes out even when no tenant connection can
be had.

On hosted the relay is always the operator's, so the mail needs no database
at all and a starved pool must not stop it. Self-hosted reads the
household's relay first; when that read fails the operator's relay still
carries the mail, and the failure is logged at error level rather than
leaving a signup silently half-finished.
"""

import os
import unittest
from unittest import mock

from psycopg_pool import PoolTimeout

from oikonome.db import tenancy

from .util import _ensure_db

ENV = {"OIKONOME_SMTP_HOST": "relay.example.dev",
       "OIKONOME_BASE_URL": "https://app.example.dev"}


class VerificationMailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()
        import oikonome.web.app as appmod
        cls.appmod = appmod

    def setUp(self):
        p = mock.patch.object(self.appmod, "DEV_MODE", False)
        p.start()
        self.addCleanup(p.stop)
        from oikonome.web import report
        self.send = mock.patch.object(report, "send").start()
        self.addCleanup(mock.patch.stopall)
        self.starved = mock.patch.object(
            tenancy, "tenant_connect",
            side_effect=PoolTimeout("couldn't get a connection")).start()

    def test_hosted_sends_without_touching_the_pool(self):
        with mock.patch.dict(os.environ, {**ENV, "OIKONOME_HOSTED": "1"}):
            self.appmod._deliver_verification(
                "new@example.dev", "tok", "00000000-0000-0000-0000-000000000001")
        self.starved.assert_not_called()
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[3], ["new@example.dev"])
        self.assertEqual(self.send.call_args.kwargs["smtp"]["host"],
                         "relay.example.dev")

    def test_self_hosted_falls_back_to_the_operator_relay_loudly(self):
        with mock.patch.dict(os.environ, {**ENV, "OIKONOME_HOSTED": ""}), \
                self.assertLogs("oikonome.auth", level="ERROR") as logs:
            self.appmod._deliver_verification(
                "new@example.dev", "tok", "00000000-0000-0000-0000-000000000001")
        self.starved.assert_called_once()
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.kwargs["smtp"]["host"],
                         "relay.example.dev")
        self.assertTrue(any("mail settings" in m for m in logs.output))


if __name__ == "__main__":
    unittest.main()
