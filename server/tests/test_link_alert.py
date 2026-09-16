"""Connection break/recovery email: only healthy<->link-issue TRANSITIONS
alert — transient blips and steady-state outages stay silent — and the
worker sweep wires it in."""

import unittest
from unittest import mock

from oikonome.jobs import link_alert, worker
from oikonome.sync import plaid

from .util import accept_recipient_invite, make_db, write_config


class BuildAlertTests(unittest.TestCase):
    def test_no_transitions_no_alert(self):
        self.assertIsNone(link_alert.build_alert([]))
        # steady-state outage: old == new == link issue → silent
        self.assertIsNone(link_alert.build_alert(
            [{"name": "Chase", "old": "error:ITEM_LOGIN_REQUIRED",
              "new": "error:ITEM_LOGIN_REQUIRED"}]))
        # healthy → healthy
        self.assertIsNone(link_alert.build_alert(
            [{"name": "Chase", "old": "ok", "new": "ok"}]))

    def test_transient_blips_do_not_alert(self):
        for code in ("error:RATE_LIMIT_EXCEEDED", "error:ConnectError",
                     "error:INSTITUTION_NOT_RESPONDING", "error:API_ERROR"):
            self.assertIsNone(
                link_alert.build_alert([{"name": "Acme Bank", "old": "ok",
                                         "new": code}]), code)
            # and recovery FROM a transient is equally silent
            self.assertIsNone(
                link_alert.build_alert([{"name": "Acme Bank", "old": code,
                                         "new": "ok"}]), code)

    def test_break_alert(self):
        built = link_alert.build_alert(
            [{"name": "Chase", "old": "ok",
              "new": "error:ITEM_LOGIN_REQUIRED"}])
        self.assertIsNotNone(built)
        subject, plain, html_body = built
        self.assertTrue(subject.startswith("⚠️"))
        self.assertIn("Chase", subject)
        self.assertIn("needs attention", subject)
        self.assertIn("re-authentication", plain)
        # no mailable base in the test env → the CTA names the page instead
        # of hyperlinking it: a LAN or relative URL in the body is what gets
        # a message spam-filtered
        self.assertIn("Re-link it on the Accounts page", plain)
        self.assertNotIn("http", plain.lower().replace("https", "http"))
        self.assertIn("Chase", html_body)

    def test_break_alert_links_cta_on_a_public_base(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ,
                             {"OIKONOME_BASE_URL": "https://app.example.test"}):
            built = link_alert.build_alert(
                [{"name": "Chase", "old": "ok",
                  "new": "error:ITEM_LOGIN_REQUIRED"}])
        _, plain, html_body = built
        self.assertIn("https://app.example.test/accounts", plain)
        self.assertIn('href="https://app.example.test/accounts"', html_body)

    def test_recovery_alert(self):
        built = link_alert.build_alert(
            [{"name": "Northwind Card", "old": "error:ITEM_NOT_SUPPORTED",
              "new": "ok"}])
        subject, plain, _ = built
        self.assertTrue(subject.startswith("✅"))
        self.assertIn("Northwind Card", subject)
        self.assertIn("reconnected", subject)
        self.assertIn("Back online", plain)

    def test_mixed_break_wins_subject(self):
        built = link_alert.build_alert([
            {"name": "A Bank", "old": "ok", "new": "error:ITEM_LOGIN_REQUIRED"},
            {"name": "B Bank", "old": "login_required", "new": "ok"}])
        subject, plain, _ = built
        self.assertTrue(subject.startswith("⚠️"))
        self.assertIn("A Bank", subject)
        self.assertIn("B Bank", plain)          # recovery still in the body

    def test_html_escapes_institution_name(self):
        _, _, html_body = link_alert.build_alert(
            [{"name": "<script>x</script>", "old": "ok",
              "new": "error:PENDING_EXPIRATION"}])
        self.assertNotIn("<script>", html_body)
        self.assertIn("&lt;script&gt;", html_body)

    def test_notify_sends_and_summarizes(self):
        transitions = [{"name": "Chase", "old": "ok",
                        "new": "error:ITEM_LOGIN_REQUIRED"}]
        with mock.patch("oikonome.web.report.send") as send:
            out = link_alert.notify(transitions, ["a@x.dev"])
        self.assertEqual(out, {"broke": ["Chase"], "fixed": []})
        send.assert_called_once()
        self.assertEqual(send.call_args.args[3], ["a@x.dev"])
        # nothing alert-worthy → no send, no recipients needed
        with mock.patch("oikonome.web.report.send") as send:
            self.assertIsNone(link_alert.notify(
                [{"name": "Chase", "old": "ok", "new": "ok"}], ["a@x.dev"]))
            self.assertIsNone(link_alert.notify(transitions, []))
        send.assert_not_called()


class WorkerTransitionTests(unittest.TestCase):
    """sync_tenant observes item-status transitions and emails exactly on
    break and on recovery — never on the steady state in between."""

    def setUp(self):
        self.conn = make_db()
        # config recipients keep _recipients() off the control-plane users
        write_config(self.conn, email_recipients=["me@x.dev"])
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        # a configured recipient is only mailed once they
        # have accepted their invitation
        accept_recipient_invite(self.tid, "me@x.dev")
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, "
            "access_token, status) VALUES "
            "('pl-1','plaid','Demo Bank','enc-tok','ok')")

    def tearDown(self):
        self.conn.close()

    def _run(self, sync_side_effect):
        with mock.patch.object(worker.plaid, "sync",
                               side_effect=sync_side_effect), \
             mock.patch.object(worker.plaid, "sync_products",
                               return_value={"liabilities": 0, "holdings": 0}), \
             mock.patch("oikonome.web.report.send") as send, \
             mock.patch("oikonome.notify.push.send_tenant") as web_push, \
             mock.patch("oikonome.notify.push_native.send_tenant") as native:
            worker.sync_tenant(self.tid)
        return send, web_push, native

    def test_break_then_steady_then_recovery(self):
        def fail(conn, item_id):
            raise plaid.PlaidError(
                {"error_code": "ITEM_LOGIN_REQUIRED",
                 "error_type": "ITEM_ERROR",
                 "error_message": "the login is no longer valid"}, 400)

        send, web_push, native = self._run(fail)        # ok → login-required
        send.assert_called_once()
        self.assertTrue(send.call_args.args[0].startswith("⚠️"))
        self.assertIn("Demo Bank", send.call_args.args[0])
        self.assertEqual(send.call_args.args[3], ["me@x.dev"])
        # the push channels ride the same transition edge as the email:
        # web push carries the subject, phones get the content-free
        # `alert` tickle — the event the docs promise, actually sent
        web_push.assert_called_once()
        self.assertEqual(web_push.call_args.args[2], send.call_args.args[0])
        native.assert_called_once()
        self.assertEqual(native.call_args.args[2], "alert")
        st = self.conn.execute(
            "SELECT status FROM items WHERE id='pl-1'").fetchone()["status"]
        self.assertEqual(st, "error:ITEM_LOGIN_REQUIRED")

        send, web_push, native = self._run(fail)        # steady state: silent
        send.assert_not_called()
        web_push.assert_not_called()
        native.assert_not_called()

        send, web_push, native = self._run(
            lambda conn, item_id: {"added": 0})         # recovery
        send.assert_called_once()
        self.assertTrue(send.call_args.args[0].startswith("✅"))
        native.assert_called_once()

    def test_transient_failure_never_emails(self):
        def blip(conn, item_id):
            raise plaid.PlaidError(
                {"error_code": "INSTITUTION_NOT_RESPONDING",
                 "error_type": "INSTITUTION_ERROR",
                 "error_message": "overnight outage"}, 400)
        send, _, native = self._run(blip)
        send.assert_not_called()
        native.assert_not_called()
        send, _, native = self._run(lambda conn, item_id: {"added": 0})
        send.assert_not_called()
        native.assert_not_called()

    def test_push_failure_never_breaks_the_sweep(self):
        def fail(conn, item_id):
            raise plaid.PlaidError(
                {"error_code": "ITEM_LOGIN_REQUIRED",
                 "error_type": "ITEM_ERROR", "error_message": "x"}, 400)
        with mock.patch.object(worker.plaid, "sync", side_effect=fail), \
             mock.patch("oikonome.web.report.send"), \
             mock.patch("oikonome.db.tenancy.control_connect",
                        side_effect=OSError("db down")):
            out = worker.sync_tenant(self.tid)          # must not raise
        self.assertEqual(out["pl-1"], "error:PlaidError")

    def test_mail_failure_never_breaks_the_sweep(self):
        def fail(conn, item_id):
            raise plaid.PlaidError(
                {"error_code": "ITEM_LOGIN_REQUIRED",
                 "error_type": "ITEM_ERROR", "error_message": "x"}, 400)
        with mock.patch.object(worker.plaid, "sync", side_effect=fail), \
             mock.patch("oikonome.web.report.send",
                        side_effect=OSError("smtp down")):
            out = worker.sync_tenant(self.tid)          # must not raise
        self.assertEqual(out["pl-1"], "error:PlaidError")


if __name__ == "__main__":
    unittest.main()
