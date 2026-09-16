"""The source-failover alert is household mail: one message per
recipient with the unsubscribe footer, dated on the household's day.

Each address must get its own delivery so a per-address failure is
recorded and the footer is present; otherwise one refusal is invisible.
The in-app alert must carry the household's date; otherwise the Alerts
page and the Today page name different days for the same event.
"""

import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import budget, links
from oikonome.jobs import worker
from oikonome.web import report

from .util import make_db, write_config


def _mk_source(conn, item_id, aggregator, acct_id):
    conn.execute(
        "INSERT INTO items (id, aggregator, institution_name) "
        "VALUES (%s,%s,%s) ON CONFLICT (tenant_id, id) DO NOTHING",
        (item_id, aggregator, item_id))
    conn.execute(
        """INSERT INTO accounts (id, item_id, name, type, subtype, mask,
                                 balance_current, updated_at)
           VALUES (%s,%s,%s,'credit','credit card','1234',100,now())""",
        (acct_id, item_id, acct_id))


class FailoverAlertMailTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        _mk_source(self.conn, "pl-1", "plaid", "pl-disc")
        _mk_source(self.conn, "sf-1", "simplefin-org", "sf-disc")
        links.create(self.conn, ["pl-disc", "sf-disc"])
        self.conn.execute(
            "UPDATE items SET status='error:ITEM_LOGIN_REQUIRED' "
            "WHERE id='pl-1'")
        self.assertEqual(len(links.failover_pending(self.conn)), 1)

    def tearDown(self):
        self.conn.close()

    def _run(self, results, recipients=("a@example.dev", "b@example.dev")):
        smtp = {"configured": True, "host": "h", "port": 25, "user": "",
                "password": "", "sender": "no-reply@example.dev"}
        with mock.patch.object(report, "resolve_smtp", return_value=smtp), \
             mock.patch.object(report, "send_each",
                               return_value=results) as each, \
             mock.patch.object(report, "send") as blast, \
             mock.patch.object(worker, "_recipients",
                               return_value=list(recipients)), \
             mock.patch.object(worker, "_push_alert") as push:
            worker._alert_failovers(self.conn, self.tid)
        return each, blast, push

    def test_one_message_per_recipient_with_the_footer(self):
        each, blast, push = self._run([
            {"email": "a@example.dev", "ok": True, "error": None},
            {"email": "b@example.dev", "ok": True, "error": None}])
        blast.assert_not_called()
        each.assert_called_once()
        self.assertEqual(each.call_args.args[3],
                         ["a@example.dev", "b@example.dev"])
        self.assertEqual(each.call_args.kwargs["unsubscribe"], self.tid)
        # the edge is stamped and the push rides it
        self.assertEqual(links.failover_pending(self.conn), [])
        push.assert_called_once()

    def test_every_recipient_refused_retries_next_sweep(self):
        each, _blast, push = self._run([
            {"email": "a@example.dev", "ok": False, "error": "x"},
            {"email": "b@example.dev", "ok": False, "error": "x"}])
        self.assertEqual(len(links.failover_pending(self.conn)), 1)
        push.assert_not_called()

    def test_nobody_deliverable_leaves_the_edge_unstamped(self):
        # the only address is held as bouncing: nothing was attempted, so
        # nothing is stamped and no push rides a send that never happened;
        # the mail goes out on the first sweep after the address is fixed
        each, _blast, push = self._run([], recipients=())
        each.assert_not_called()
        push.assert_not_called()
        self.assertEqual(len(links.failover_pending(self.conn)), 1)
        # the in-app alert still landed
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM alerts_log "
            "WHERE kind='source-failover'").fetchone()["n"], 1)

    def test_alert_is_dated_on_the_households_day(self):
        cfg = budget.load_config(self.conn)
        cfg["timezone"] = "Pacific/Kiritimati"       # UTC+14
        budget.save_config(self.conn, cfg)
        # 22:00 UTC — the server's date is today, the household's is
        # tomorrow
        import zoneinfo
        now = dt.datetime(2026, 7, 15, 22, tzinfo=dt.timezone.utc)
        local = now.astimezone(zoneinfo.ZoneInfo("Pacific/Kiritimati"))
        with mock.patch("oikonome.localtime.now_local", return_value=local):
            self._run([], recipients=())
        row = self.conn.execute(
            "SELECT first_seen FROM alerts_log WHERE kind='source-failover'"
        ).fetchone()
        self.assertEqual(str(row["first_seen"]), "2026-07-16")


if __name__ == "__main__":
    unittest.main()
