"""A household suspended or put up for erasure while a send is under way
is sent nothing more.

The hourly mail sweep and the minute's webhook drain list the live
households once, then work through them for up to a minute or more:
building a report over a whole ledger, or posting a batch of queued
deliveries one receiver at a time. A suspension, or the owner's own
erasure (which flips the status first and only then waits for the
household's writers), can land anywhere in that stretch. The household's
status is therefore read again right before each thing leaves the box —
every mail fan-out, text, push and webhook post — and a household that is
no longer active gets none of them. Queued deliveries are left pending,
untried, so a reactivation resumes them.
"""
import datetime as dt
import unittest
from unittest import mock

import httpx

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.jobs import worker
from oikonome.notify import webhooks

from .util import accept_recipient_invite, make_db, write_config

RELAY = {"host": "smtp.example.com", "port": 587, "user": None,
         "password": None, "sender": "oikonome@example.com",
         "starttls": True, "configured": True}
HOOK_URL = "http://127.0.0.1:9/hook"


def _set_status(tid, status):
    admin = tenancy.admin_connect()
    try:
        admin.execute("UPDATE tenants SET status=%s WHERE id=%s",
                      (status, tid))
        admin.commit()
    finally:
        admin.close()


class ScheduledSendStopsForAFrozenHousehold(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        write_config(self.conn, email_recipients=["one@example.com"])
        accept_recipient_invite(self.tid, "one@example.com")
        cfg = budget.load_config(self.conn)
        cfg["email_schedule"] = {"daily": {"on": True, "hour": 7,
                                           "sms": True, "push": True}}
        cfg["notify_phone"] = {"number": "+12175550100", "verified": True}
        budget.save_config(self.conn, cfg)

    def _send_with_status_flipped_mid_build(self, status):
        """One scheduled daily send whose household is frozen after the
        sweep read its recipients, while the report is being built."""
        from oikonome.web import report
        real = worker._recipients_detail

        def read_then_freeze(conn, tid, kind=None):
            out = real(conn, tid, kind)
            _set_status(self.tid, status)
            return out

        with mock.patch.object(worker, "_recipients_detail",
                               side_effect=read_then_freeze), \
             mock.patch.object(report, "resolve_smtp", return_value=RELAY), \
             mock.patch.object(report, "send_each", return_value=[
                 {"email": "one@example.com", "ok": True}]) as each, \
             mock.patch("oikonome.notify.sms.send",
                        return_value=True) as sms, \
             mock.patch("oikonome.notify.push.send_tenant") as web_push, \
             mock.patch("oikonome.notify.push_native.send_tenant") as native:
            worker.email_tenant(self.tid, send_it=True, cadence="daily")
        return each, sms, web_push, native

    def test_a_household_suspended_mid_build_gets_no_mail_text_or_push(self):
        each, sms, web_push, native = \
            self._send_with_status_flipped_mid_build("suspended")
        each.assert_not_called()
        sms.assert_not_called()
        web_push.assert_not_called()
        native.assert_not_called()

    def test_a_household_being_erased_mid_build_gets_nothing(self):
        each, sms, web_push, native = \
            self._send_with_status_flipped_mid_build("pending_delete")
        each.assert_not_called()
        sms.assert_not_called()
        web_push.assert_not_called()
        native.assert_not_called()

    def test_an_active_household_still_gets_every_channel(self):
        from oikonome.web import report
        with mock.patch.object(report, "resolve_smtp", return_value=RELAY), \
             mock.patch.object(report, "send_each", return_value=[
                 {"email": "one@example.com", "ok": True}]) as each, \
             mock.patch("oikonome.notify.sms.send",
                        return_value=True) as sms, \
             mock.patch("oikonome.notify.push.send_tenant") as web_push:
            with mock.patch("oikonome.notify.push_native.send_tenant"):
                worker.email_tenant(self.tid, send_it=True, cadence="daily")
        each.assert_called_once()
        sms.assert_called_once()
        web_push.assert_called_once()


class AlertMailStopsForAFrozenHousehold(unittest.TestCase):
    """The alert mails a sync or nightly pass sends (a broken bank link, a
    collector gone quiet, a savings milestone, a source failover) resolve
    their recipients right before sending; a frozen household has none."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        write_config(self.conn, email_recipients=["one@example.com"])
        accept_recipient_invite(self.tid, "one@example.com")

    def test_an_active_household_has_its_alert_recipients(self):
        self.assertIn("one@example.com",
                      worker._recipients(self.conn, self.tid, "alerts"))

    def test_a_suspended_household_has_no_alert_recipients(self):
        _set_status(self.tid, "suspended")
        self.assertEqual(worker._recipients(self.conn, self.tid, "alerts"),
                         [])

    def test_an_alert_push_to_a_household_being_erased_is_not_sent(self):
        _set_status(self.tid, "pending_delete")
        with mock.patch("oikonome.notify.push.send_tenant") as web_push, \
             mock.patch("oikonome.notify.push_native.send_tenant") as native:
            worker._push_alert(self.tid, "A bank link broke", "Reconnect it")
        web_push.assert_not_called()
        native.assert_not_called()


class WebhookDrainStopsForAFrozenHousehold(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        webhooks._transport_override = None
        self.addCleanup(setattr, webhooks, "_transport_override", None)
        self.hook, _ = webhooks.create(self.conn, url=HOOK_URL,
                                       events=["test.ping"], name="ha")
        for i in range(3):
            webhooks.emit(self.conn, "test.ping", {"n": i})
        self.posted = []

    def _receiver(self, on_first=None):
        def handle(request: httpx.Request) -> httpx.Response:
            self.posted.append(request.headers["x-oikonome-delivery"])
            if len(self.posted) == 1 and on_first:
                on_first()
            return httpx.Response(200)
        webhooks._transport_override = httpx.MockTransport(handle)

    def _rows(self):
        return self.conn.execute(
            "SELECT state, attempts FROM webhook_deliveries "
            "WHERE webhook_id=%s", (self.hook["id"],)).fetchall()

    def test_a_household_suspended_after_the_listing_has_nothing_posted(self):
        self.assertIn(self.tid, worker._webhook_tenants())
        _set_status(self.tid, "suspended")
        self._receiver()
        worker.webhooks_drain_tenant(self.tid)
        self.assertEqual(self.posted, [])
        rows = self._rows()
        self.assertEqual({r["state"] for r in rows}, {"pending"})
        self.assertEqual({r["attempts"] for r in rows}, {0})

    def test_an_erasure_landing_mid_batch_stops_the_rest_of_it(self):
        self._receiver(on_first=lambda: _set_status(self.tid,
                                                    "pending_delete"))
        worker.webhooks_drain_tenant(self.tid)
        self.assertEqual(len(self.posted), 1)
        rows = self._rows()
        self.assertEqual(sorted(r["state"] for r in rows),
                         ["delivered", "pending", "pending"])
        due = self.conn.execute(
            "SELECT count(*) AS n FROM webhook_deliveries WHERE "
            "webhook_id=%s AND state='pending' AND next_attempt_at <= %s",
            (self.hook["id"], dt.datetime.now(dt.timezone.utc))).fetchone()
        self.assertEqual(due["n"], 2, "the untried rows are handed back")

    def test_an_active_household_has_every_delivery_posted(self):
        self._receiver()
        worker.webhooks_drain_tenant(self.tid)
        self.assertEqual(len(self.posted), 3)


if __name__ == "__main__":
    unittest.main()
