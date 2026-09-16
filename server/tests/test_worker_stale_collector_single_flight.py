"""The stale-collector alert is single-flight per tenant.

`script_alert_tenant` reads the overdue collectors whose `alerted_at` is
still NULL, sends one email, and only THEN stamps the flag that suppresses
the next one. Two doors reach the sweep — the nightly cron and the admin
console's run-job enqueue — so two runs can both read "due" before either
stamps, and the household is told twice that the same script went quiet.
The conditional stamp dedupes the flag, not a mail that already left; the
per-tenant TRY lock is what dedupes the send.
"""

import unittest
from unittest import mock

from oikonome.db import tenancy
from oikonome.jobs import worker

from .util import make_db


class StaleCollectorSingleFlightTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.conn = make_db()
        cls.tid = cls.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def _key(self, tid=None) -> str:
        return f"oikonome:script-alert:{tid or self.tid}"

    def _hold(self, key: str):
        c = tenancy.tenant_connect(self.tid)
        got = c.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                        (key,)).fetchone()["ok"]
        self.assertTrue(got, "the test could not take the lock itself")
        return c

    def _release(self, c, key: str):
        c.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
        c.close()

    def _overdue(self, _conn=None):
        return [{"source": "coinbase", "label": "Coinbase", "alerts": True,
                 "alerted_at": None, "age_hours": 96.0, "expected_hours": 24,
                 "last_push": None}]

    def test_a_second_entrant_sends_no_second_email(self):
        key = self._key()
        held = self._hold(key)
        try:
            with mock.patch("oikonome.sync.heartbeat.overdue",
                            side_effect=lambda conn: self._overdue()), \
                 mock.patch("oikonome.web.report.resolve_smtp",
                            return_value=None), \
                 mock.patch("oikonome.jobs.worker._recipients",
                            return_value=["a@example.dev"]), \
                 mock.patch("oikonome.web.report.send_each") as send:
                out = worker.script_alert_tenant(self.tid)
            send.assert_not_called()
            self.assertEqual({"alerted": 0}, out)
        finally:
            self._release(held, key)

    def test_the_alert_email_also_tickles_the_push_channels(self):
        """The stale-collector alert fans out to web push and the native
        `alert` tickle alongside the email, on the same alerted_at edge —
        so it fires once per outage, never per sweep."""
        with mock.patch("oikonome.sync.heartbeat.overdue",
                        side_effect=lambda conn: self._overdue()), \
             mock.patch("oikonome.web.report.resolve_smtp",
                        return_value=None), \
             mock.patch("oikonome.jobs.worker._recipients",
                        return_value=["a@example.dev"]), \
             mock.patch("oikonome.web.report.send_each"), \
             mock.patch("oikonome.notify.push.send_tenant") as web_push, \
             mock.patch("oikonome.notify.push_native.send_tenant") as native:
            worker.script_alert_tenant(self.tid)
        web_push.assert_called_once()
        self.assertIn("gone quiet", web_push.call_args.args[2])
        native.assert_called_once()
        self.assertEqual(native.call_args.args[2], "alert")

    def test_another_tenants_lock_does_not_block(self):
        """The key carries the tenant id — an instance-wide key would mean
        one household's alert suppressed everybody else's."""
        other = self._key("00000000-0000-0000-0000-000000000000")
        held = self._hold(other)
        try:
            with mock.patch("oikonome.sync.heartbeat.overdue",
                            side_effect=lambda conn: self._overdue()), \
                 mock.patch("oikonome.web.report.resolve_smtp",
                            return_value=None), \
                 mock.patch("oikonome.jobs.worker._recipients",
                            return_value=["a@example.dev"]), \
                 mock.patch("oikonome.web.report.send_each") as send:
                worker.script_alert_tenant(self.tid)
            send.assert_called_once()
        finally:
            self._release(held, other)

    def test_the_lock_is_released_for_the_next_run(self):
        """tenant_connect is POOLED: close() hands the session back with any
        advisory lock still on it, so an unreleased lock would silence this
        tenant's collector alerts until that backend died."""
        with mock.patch("oikonome.sync.heartbeat.overdue", return_value=[]):
            worker.script_alert_tenant(self.tid)
        probe = tenancy.tenant_connect(self.tid)
        key = self._key()
        try:
            free = probe.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                (key,)).fetchone()["ok"]
            self.assertTrue(free, "the lock was not released")
        finally:
            probe.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
            probe.close()


if __name__ == "__main__":
    unittest.main()
