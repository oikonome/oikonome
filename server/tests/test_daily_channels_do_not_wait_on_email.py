"""The scheduled report's channels are independent of each other.

A household can ask for its daily verdict by SMS or push as well as, or
instead of, by email. The send used to try the email first and raise when
every address failed — before the SMS and push fan-out ran. So:

  * an instance with no mail relay at all (a self-host that never set one
    up) still tried to mail every recipient through the localhost
    fallback, failed, raised, and the household that turned on push got
    nothing, every hour, every day;
  * a relay outage on a configured instance held back the SMS and push as
    well, though neither depends on the relay;
  * the raise escaped the hourly due-loop, so a monthly or yearly report
    due the same hour — which only fires on the 1st — was never tried.

Pinned here: no relay means the email channel is off (and is not tried);
a failing relay still fails the email loudly, after the other channels
have gone out; a retry of the failed email does not send the SMS or push a
second time; and one cadence failing does not skip the others.
"""

import unittest
from unittest import mock

from oikonome.engine import budget
from oikonome.jobs import worker

from .util import accept_recipient_invite, make_db, write_config

NO_RELAY = {"host": "localhost", "port": 587, "user": None, "password": None,
            "sender": "oikonome@localhost", "starttls": True,
            "configured": False}
RELAY = dict(NO_RELAY, host="smtp.example.com", configured=True)


class DailyChannelsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        write_config(self.conn, email_recipients=["one@example.com"])
        accept_recipient_invite(self.tid, "one@example.com")

    def tearDown(self):
        self.conn.close()

    def _schedule(self, **daily):
        cfg = budget.load_config(self.conn)
        cfg["email_schedule"] = {"daily": dict({"on": True, "hour": 7},
                                               **daily)}
        cfg["notify_phone"] = {"number": "+12175551234", "verified": True}
        budget.save_config(self.conn, cfg)

    def _send(self, smtp, outcome):
        """One scheduled daily send. Returns (raised, send_each mock,
        sms mock, web push mock)."""
        from oikonome.web import report
        raised = None
        with mock.patch.object(report, "resolve_smtp", return_value=smtp), \
             mock.patch.object(report, "send_each",
                               return_value=outcome) as each, \
             mock.patch("oikonome.notify.sms.send",
                        return_value=True) as sms, \
             mock.patch("oikonome.notify.push.send_tenant") as web_push, \
             mock.patch("oikonome.notify.push_native.send_tenant"):
            try:
                worker.email_tenant(self.tid, send_it=True, cadence="daily")
            except RuntimeError as e:
                raised = e
        return raised, each, sms, web_push

    def _stamped(self):
        return self.conn.execute(
            "SELECT 1 FROM job_runs WHERE job='daily-email'").fetchone() \
            is not None

    def test_no_relay_means_no_email_and_the_push_still_goes(self):
        self._schedule(push=True)
        refused = [{"email": "one@example.com", "ok": False,
                    "error": "ConnectionRefusedError"}]
        raised, each, _, web_push = self._send(NO_RELAY, refused)
        self.assertIsNone(raised, "no relay is a decision, not a failure")
        each.assert_not_called()
        web_push.assert_called_once()
        self.assertTrue(self._stamped(),
                        "the day is done, or every hour would repeat it")

    def test_a_relay_outage_still_sends_the_sms_and_fails_the_email(self):
        self._schedule(sms=True)
        down = [{"email": "one@example.com", "ok": False,
                 "error": "SMTPServerDisconnected"}]
        raised, each, sms, _ = self._send(RELAY, down)
        each.assert_called_once()
        sms.assert_called_once()
        self.assertIsNotNone(raised, "a total email failure stays loud")
        self.assertFalse(self._stamped(),
                         "the failed email must come due again")
        # the next hourly retry of the email must not text the household
        # the same verdict again
        raised, each, sms, _ = self._send(RELAY, down)
        each.assert_called_once()
        sms.assert_not_called()
        self.assertIsNotNone(raised)


class CadencesAreIndependentTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])

    def tearDown(self):
        self.conn.close()

    def test_a_failing_daily_does_not_skip_the_monthly(self):
        tried = []

        def send(tenant_id, cadence="daily"):
            tried.append(cadence)
            if cadence == "daily":
                raise RuntimeError("email failed for every recipient")
            return "ok"
        with mock.patch.object(worker, "emails_due",
                               return_value=["daily", "monthly", "yearly"]), \
             mock.patch.object(worker, "email_tenant", side_effect=send):
            with self.assertRaises(RuntimeError):
                worker._email_if_due(self.tid)
        self.assertEqual(tried, ["daily", "monthly", "yearly"])


if __name__ == "__main__":
    unittest.main()
