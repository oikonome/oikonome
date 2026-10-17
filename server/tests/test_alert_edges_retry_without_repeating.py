"""An alert whose mail the relay refused is retried, and the retry neither
repeats what already went out nor carries over into a later alert.

* The connection.changed webhook does not go through the mail relay: it is
  queued once per edge even while the link-alert mail for that edge is
  being retried, and a recovery is still posted.
* The link-alert retry window belongs to one pending alert. When the edge
  goes away before the mail goes out, the window closes, so a later alert's
  first transient refusal is retried rather than counted as retries used up.
* The stale-collector alert and the savings milestone are recorded only
  once a recipient accepted the mail (or there is no relay at all).
"""

import unittest
from unittest import mock

from oikonome.engine import budget
from oikonome.jobs import worker
from oikonome.notify import webhooks
from oikonome.sync import plaid

from .util import accept_recipient_invite, make_db, write_config

RELAY = {"configured": True, "host": "relay.example", "port": 587}


def _broken(conn, item_id):
    raise plaid.PlaidError(
        {"error_code": "ITEM_LOGIN_REQUIRED", "error_type": "ITEM_ERROR",
         "error_message": "x"}, 400)


def _healthy(conn, item_id):
    return {"added": 0}


class LinkAlertRetryTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, email_recipients=["me@x.dev"])
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        accept_recipient_invite(self.tid, "me@x.dev")
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, "
            "access_token, status, linked_at) VALUES "
            "('pl-1','plaid','Demo Bank','enc-tok','ok', "
            "now() - interval '30 days')")

    def tearDown(self):
        self.conn.close()

    def _sweep(self, pull, relay_up: bool):
        with mock.patch("oikonome.web.report.resolve_smtp",
                        return_value=RELAY), \
             mock.patch.object(worker.plaid, "sync", side_effect=pull), \
             mock.patch.object(worker.plaid, "sync_products",
                               return_value={}), \
             mock.patch("oikonome.web.report.send",
                        side_effect=None if relay_up
                        else OSError("relay down")) as send, \
             mock.patch("oikonome.notify.push.send_tenant"), \
             mock.patch("oikonome.notify.push_native.send_tenant"):
            worker.sync_tenant(self.tid)
        return send.call_count

    def _hook(self):
        with mock.patch.object(webhooks, "_check_url", side_effect=lambda u: u):
            hook, _ = webhooks.create(self.conn, url="https://hooks.example/x",
                                      events=["connection.changed"])
        return hook["id"]

    def _deliveries(self, hook_id):
        return [r["payload"]["connections"] for r in self.conn.execute(
            "SELECT payload FROM webhook_deliveries WHERE webhook_id=%s "
            "ORDER BY created_at", (hook_id,)).fetchall()]

    def test_connection_changed_is_queued_once_while_the_mail_is_retried(self):
        """A break whose mail the relay keeps refusing stays pending for the
        mail, but the receiver gets that break once — not once per retry
        pass — and the recovery after it is still posted."""
        hook = self._hook()
        mails = self._sweep(_broken, relay_up=False)
        mails += self._sweep(_broken, relay_up=False)
        mails += self._sweep(_broken, relay_up=False)
        self.assertEqual(mails, 3, "the mail is retried on every pass")
        got = self._deliveries(hook)
        self.assertEqual(len(got), 1, got)
        self.assertEqual(got[0][0]["to"], "error:ITEM_LOGIN_REQUIRED")
        self._sweep(_healthy, relay_up=True)
        got = self._deliveries(hook)
        self.assertEqual(len(got), 2, got)
        self.assertEqual((got[1][0]["from"], got[1][0]["to"]),
                         ("error:ITEM_LOGIN_REQUIRED", "ok"))

    def test_a_retry_window_ends_when_its_edge_goes_away_unsent(self):
        """The retry window of an alert whose edge disappeared before the
        relay came back must not be inherited by the next alert: that
        alert's first refusal is a first refusal, retried next pass."""
        self._sweep(_broken, relay_up=False)        # refused, pending
        self.conn.execute(
            "UPDATE job_runs SET ran_at = now() - interval '3 days' "
            "WHERE job = 'link-alert-mail-retry'")
        self._sweep(_healthy, relay_up=False)       # recovered: no edge left
        left = self.conn.execute(
            "SELECT 1 FROM job_runs WHERE job = 'link-alert-mail-retry'"
        ).fetchone()
        self.assertIsNone(left, "the old episode's retry window was kept")
        # a later break meets one transient refusal, then a working relay
        self._sweep(_broken, relay_up=False)
        self.assertEqual(self._sweep(_broken, relay_up=True), 1,
                         "the later alert was given up on its first refusal")


class StaleCollectorDeliveryTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.conn = make_db()
        cls.tid = cls.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def _run(self, smtp, results):
        overdue = [{"source": "coinbase", "label": "Coinbase", "alerts": True,
                    "alerted_at": None, "age_hours": 96.0,
                    "expected_hours": 24, "last_push": None}]
        with mock.patch("oikonome.sync.heartbeat.overdue",
                        return_value=overdue), \
             mock.patch("oikonome.web.report.resolve_smtp", return_value=smtp), \
             mock.patch("oikonome.jobs.worker._recipients",
                        return_value=["a@example.dev"]), \
             mock.patch("oikonome.web.report.send_each",
                        return_value=results), \
             mock.patch("oikonome.notify.push.send_tenant") as push, \
             mock.patch("oikonome.notify.push_native.send_tenant"):
            out = worker.script_alert_tenant(self.tid)
        return out, push

    def test_a_refused_quiet_collector_alert_is_not_recorded_as_sent(self):
        """Every recipient refused on a configured relay = nothing went
        out: the edge stays unstamped (a quiet collector never pushes, so a
        stamp would lose the alert for good) and the push waits for it."""
        out, push = self._run(RELAY, [{"email": "a@example.dev", "ok": False,
                                       "error": "relay down"}])
        self.assertEqual(out, {"alerted": 0})
        push.assert_not_called()

    def test_an_accepted_quiet_collector_alert_is_recorded_and_pushed(self):
        out, push = self._run(RELAY, [{"email": "a@example.dev", "ok": True,
                                       "error": None}])
        self.assertEqual(out, {"alerted": 1})
        push.assert_called_once()

    def test_no_relay_at_all_records_the_alert_and_pushes(self):
        """No relay is a setting, not a failure: retrying would never end,
        and a push-only household must still hear of it."""
        out, push = self._run({"configured": False}, [
            {"email": "a@example.dev", "ok": False, "error": "no relay"}])
        self.assertEqual(out, {"alerted": 1})
        push.assert_called_once()


class SavingsMilestoneDeliveryTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]

    def tearDown(self):
        self.conn.close()

    def _night(self, results):
        state = {"g1": {"milestone": 50}}
        with mock.patch("oikonome.engine.savings.goals", return_value=[1]), \
             mock.patch("oikonome.engine.savings.check_milestones",
                        return_value=(["Goal reached 50%"], state)), \
             mock.patch("oikonome.web.report.resolve_smtp",
                        return_value=RELAY), \
             mock.patch("oikonome.jobs.worker._recipients",
                        return_value=["a@example.dev"]), \
             mock.patch("oikonome.web.report.send_each",
                        return_value=results):
            worker.nightly_tenant(self.tid)
        return budget.load_config(self.conn).get("savings_goal_state")

    def test_a_milestone_whose_mail_every_recipient_refused_is_not_recorded(self):
        """Recorded before a refused send, the crossing is never mailed:
        the next night finds no edge. It is recorded once a mail goes out."""
        self.assertFalse(self._night(
            [{"email": "a@example.dev", "ok": False, "error": "relay down"}]))
        self.assertEqual(self._night(
            [{"email": "a@example.dev", "ok": True, "error": None}]),
            {"g1": {"milestone": 50}})


if __name__ == "__main__":
    unittest.main()
