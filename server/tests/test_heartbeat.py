"""push-heartbeats: stamp/auto-watch semantics, staleness status,
door stamping (upsert_transactions per item, plan-CSV import), and the
edge-triggered stale-collector email sweep."""

import datetime as dt
import unittest
from unittest import mock

from oikonome.jobs import worker
from oikonome.sync import base as sync_base
from oikonome.sync import heartbeat, plan_csv

from .util import accept_recipient_invite, make_db, write_config


def _row(conn, source):
    return conn.execute("SELECT * FROM script_heartbeats WHERE source=%s",
                        (source,)).fetchone()


def _backdate(conn, source, hours):
    conn.execute(
        "UPDATE script_heartbeats SET last_push = now() - "
        "make_interval(hours => %s) WHERE source=%s", (hours, source))


class StampTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_first_stamp_inserts_unwatched(self):
        heartbeat.stamp(self.conn, "s1", rows=5, label="S One")
        r = _row(self.conn, "s1")
        self.assertEqual(r["last_rows"], 5)
        self.assertEqual(r["label"], "S One")
        self.assertIsNone(r["expected_hours"])   # undecided, not watched
        self.assertEqual(r["alerts"], 0)

    def test_same_day_restamp_stays_unwatched(self):
        heartbeat.stamp(self.conn, "s1")
        # clamp the backdate to today's midnight: a plain now()-2h crosses
        # the calendar-day boundary when the suite runs 00:00–02:00 UTC,
        # legitimately auto-watching and flaking the suite
        self.conn.execute(
            "UPDATE script_heartbeats SET last_push = GREATEST("
            "now() - interval '2 hours', date_trunc('day', now()))"
            " WHERE source='s1'")
        heartbeat.stamp(self.conn, "s1", rows=9)
        r = _row(self.conn, "s1")
        self.assertIsNone(r["expected_hours"])
        self.assertEqual(r["last_rows"], 9)

    def test_second_day_push_autowatches_at_24(self):
        heartbeat.stamp(self.conn, "s1")
        _backdate(self.conn, "s1", 26)
        heartbeat.stamp(self.conn, "s1")
        self.assertEqual(_row(self.conn, "s1")["expected_hours"], 24)

    def test_push_gap_over_3_days_never_autowatches(self):
        # a manual import repeated much later is not a recurring collector
        heartbeat.stamp(self.conn, "s1")
        _backdate(self.conn, "s1", 100)          # > 72h ago
        heartbeat.stamp(self.conn, "s1")
        self.assertIsNone(_row(self.conn, "s1")["expected_hours"])

    def test_user_zero_survives_autowatch(self):
        heartbeat.stamp(self.conn, "s1")
        self.conn.execute(
            "UPDATE script_heartbeats SET expected_hours=0 WHERE source='s1'")
        _backdate(self.conn, "s1", 26)
        heartbeat.stamp(self.conn, "s1")
        self.assertEqual(_row(self.conn, "s1")["expected_hours"], 0)

    def test_stamp_clears_alert_edge_and_keeps_label(self):
        heartbeat.stamp(self.conn, "s1", label="S One")
        self.conn.execute(
            "UPDATE script_heartbeats SET alerted_at=now() WHERE source='s1'")
        heartbeat.stamp(self.conn, "s1")         # no label this time
        r = _row(self.conn, "s1")
        self.assertIsNone(r["alerted_at"])
        self.assertEqual(r["label"], "S One")


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _status(self, source):
        return {p["source"]: p["status"]
                for p in heartbeat.panel(self.conn)}[source]

    def test_fresh_within_two_intervals(self):
        heartbeat.stamp(self.conn, "s1")
        self.conn.execute(
            "UPDATE script_heartbeats SET expected_hours=24 WHERE source='s1'")
        _backdate(self.conn, "s1", 40)           # < 48h
        self.assertEqual(self._status("s1"), "fresh")
        self.assertEqual(heartbeat.overdue(self.conn), [])

    def test_stale_after_two_missed_intervals(self):
        heartbeat.stamp(self.conn, "s1")
        self.conn.execute(
            "UPDATE script_heartbeats SET expected_hours=24 WHERE source='s1'")
        _backdate(self.conn, "s1", 50)           # > 48h
        self.assertEqual(self._status("s1"), "stale")
        self.assertEqual([r["source"] for r in heartbeat.overdue(self.conn)],
                         ["s1"])

    def test_unwatched_is_off_and_never_overdue(self):
        heartbeat.stamp(self.conn, "s1")         # expected_hours NULL
        heartbeat.stamp(self.conn, "s2")
        self.conn.execute(
            "UPDATE script_heartbeats SET expected_hours=0 WHERE source='s2'")
        _backdate(self.conn, "s1", 500)
        _backdate(self.conn, "s2", 500)
        self.assertEqual(self._status("s1"), "off")
        self.assertEqual(self._status("s2"), "off")
        self.assertEqual(heartbeat.overdue(self.conn), [])


PLAN_CSV = """Trade Date,Investments,Ticker,Transaction,Transaction Amount,Share Price,Total shares
07/01/2025,Vanguard 500 Idx,VFIAX,Contribution,"1,000.00",100.00,10.000
"""


class DoorTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _txn(self, i, account="chk"):
        return sync_base.Transaction(
            id=f"hb-test:{i}", account_id=account,
            date=dt.date(2026, 7, 1), amount=1.0, name="x")

    def test_upsert_transactions_stamps_per_item(self):
        # fixture accounts chk/card belong to item it1 ('Test Bank')
        sync_base.upsert_transactions(
            self.conn, [self._txn(1), self._txn(2, "card")])
        r = _row(self.conn, "it1")
        self.assertEqual(r["label"], "Test Bank")
        self.assertEqual(r["last_rows"], 2)

    def test_aggregator_items_not_stamped(self):
        sync_base.upsert_item(self.conn, "sfin-1", "simplefin", "Demo", "tok")
        sync_base.upsert_accounts(self.conn, "sfin-1", [sync_base.Account(
            id="sfin-acct", name="Demo Checking", type="depository")])
        sync_base.upsert_transactions(
            self.conn, [self._txn(3, "sfin-acct")])
        self.assertIsNone(_row(self.conn, "sfin-1"))

    def test_plan_import_stamps(self):
        plan_csv.import_csv(self.conn, "401k", PLAN_CSV)
        r = _row(self.conn, "plan")
        self.assertEqual(r["label"], "Workplace plan")
        self.assertEqual(r["last_rows"], 1)


class AccountsFreshnessTests(unittest.TestCase):
    """/api/accounts carries per-script freshness so the
    Accounts page can show a live dot — ok = a successful push within 26h
    (the same threshold Doctor applies to aggregator connections)."""

    def setUp(self):
        self.conn = make_db()
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])

    def tearDown(self):
        self.conn.close()

    def _script(self):
        from oikonome.web import api as web_api
        accts = web_api.accounts(user={"tenant_id": self.tid})["accounts"]
        return next(a["script"] for a in accts if a["id"] == "chk")

    def test_fresh_push_reads_ok(self):
        heartbeat.stamp(self.conn, "it1", rows=3, label="Test Bank")
        s = self._script()
        self.assertTrue(s["ok"])
        self.assertEqual(s["source"], "it1")
        self.assertTrue(s["last_push"])          # ISO timestamp for tooltip

    def test_quiet_over_26h_reads_not_ok(self):
        heartbeat.stamp(self.conn, "it1")
        _backdate(self.conn, "it1", 30)
        self.assertFalse(self._script()["ok"])
        self.assertIsNone(self._script()["token_revoked_at"])

    def _mint(self, name):
        from oikonome.auth import api_tokens
        uid = self.conn.execute(
            "SELECT id FROM users WHERE tenant_id=%s LIMIT 1",
            (self.tid,)).fetchone()
        uid = uid["id"] if uid else None
        if uid is None:
            # users is a global control-plane table (no RLS) that outlives
            # the per-test tenant, so the address must never repeat
            import uuid
            uid = self.conn.execute(
                "INSERT INTO users (tenant_id, email, password_hash) "
                "VALUES (%s, %s, 'x') RETURNING id",
                (self.tid, f"hb-{uuid.uuid4().hex[:10]}@example.test")
            ).fetchone()["id"]
        return api_tokens.mint(self.conn, self.tid, uid, name)[1]["id"]

    def test_token_revoked_after_last_push_is_named(self):
        """A password change or reset revokes every script token while
        the collector keeps running on its host — the red dot must say
        the token is gone, not send the person to check a healthy
        collector. Only a token named for THIS source counts, and only
        when no live replacement exists."""
        from oikonome.auth import api_tokens
        heartbeat.stamp(self.conn, "it1")
        _backdate(self.conn, "it1", 30)
        other = self._mint("plan-mirror")      # a different source
        mine = self._mint("it1-mirror")
        api_tokens.revoke(self.conn, self.tid, other)
        self.assertIsNone(self._script()["token_revoked_at"])
        api_tokens.revoke(self.conn, self.tid, mine)
        self.assertIsNotNone(self._script()["token_revoked_at"])
        # a fresh token for the same SOURCE means the person already
        # re-minted — the old revocation no longer explains anything. Names
        # are free text, so the replacement need not reuse the old one.
        fresh = self._mint("it1-replacement")
        self.assertIsNone(self._script()["token_revoked_at"])
        api_tokens.revoke(self.conn, self.tid, fresh)
        self.assertIsNotNone(self._script()["token_revoked_at"])
        # the same predicate, for Doctor and the daily email's alert strip
        # — a household that never opens Accounts must still hear it.
        # Watched sources only: an unwatched one-time import never warns.
        from oikonome.engine import alerts
        self.conn.execute("UPDATE script_heartbeats SET expected_hours=24 "
                          "WHERE source='it1'")
        self.assertIn("it1", heartbeat.revoked_tokens(self.conn))
        kinds = [a["kind"] for a in alerts.freshness(self.conn)]
        self.assertIn("collector-token-revoked", kinds)
        self._mint("it1-mirror")
        self.assertIsNone(self._script()["token_revoked_at"])
        self.assertNotIn("it1", heartbeat.revoked_tokens(self.conn))


class AlertSweepTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, email_recipients=["a@x.dev"])
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        # a configured recipient is only mailed once they
        # have accepted their invitation
        accept_recipient_invite(self.tid, "a@x.dev")

    def tearDown(self):
        self.conn.close()

    def _stale_watched(self, source="s1", alerts=1):
        heartbeat.stamp(self.conn, source, label="S One")
        self.conn.execute(
            "UPDATE script_heartbeats SET expected_hours=24, alerts=%s "
            "WHERE source=%s", (alerts, source))
        _backdate(self.conn, source, 60)

    def test_edge_triggered_email_once_then_rearms_on_recovery(self):
        self._stale_watched()
        with mock.patch("oikonome.web.report.send") as send:
            out = worker.script_alert_tenant(self.tid)
        self.assertEqual(out, {"alerted": 1})
        send.assert_called_once()
        subject, plain = send.call_args.args[0], send.call_args.args[1]
        self.assertIn("gone quiet", subject)
        self.assertIn("S One", plain)
        self.assertEqual(send.call_args.args[3], ["a@x.dev"])
        self.assertIsNotNone(_row(self.conn, "s1")["alerted_at"])

        # steady-state stale: no second email
        with mock.patch("oikonome.web.report.send") as send:
            self.assertEqual(worker.script_alert_tenant(self.tid),
                             {"alerted": 0})
        send.assert_not_called()

        # recovery push clears the edge; a relapse alerts again
        heartbeat.stamp(self.conn, "s1")
        _backdate(self.conn, "s1", 60)
        with mock.patch("oikonome.web.report.send") as send:
            self.assertEqual(worker.script_alert_tenant(self.tid),
                             {"alerted": 1})
        send.assert_called_once()

    def test_a_push_during_the_send_is_not_re_flagged(self):
        """A collector that recovers WHILE the alert mail is going out must
        not be stamped as alerted.

        `stamp` clears alerted_at on every push, and nothing else clears it.
        If the alert wrote it back onto a source that was healthy again, the
        next time that collector went quiet — precisely when there are no
        more pushes — it would be skipped for good."""
        self._stale_watched()

        def push_mid_send(*a, **k):
            # the recovery lands between "who is overdue" and the stamp
            heartbeat.stamp(self.conn, "s1")

        with mock.patch("oikonome.web.report.send", side_effect=push_mid_send):
            worker.script_alert_tenant(self.tid)

        self.assertIsNone(_row(self.conn, "s1")["alerted_at"],
                          "a source that recovered mid-send is not 'alerted'")

        # and the relapse still alerts
        _backdate(self.conn, "s1", 60)
        with mock.patch("oikonome.web.report.send") as send:
            self.assertEqual(worker.script_alert_tenant(self.tid),
                             {"alerted": 1})
        send.assert_called_once()

    def test_opt_out_never_emails(self):
        self._stale_watched(alerts=0)
        with mock.patch("oikonome.web.report.send") as send:
            self.assertEqual(worker.script_alert_tenant(self.tid),
                             {"alerted": 0})
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
