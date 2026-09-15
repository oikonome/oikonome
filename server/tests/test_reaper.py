"""zombie-Item reaper + orphan reconcile.

A Plaid Item bills monthly for as long as it exists — a connection stuck
in an unrecoverable error for OIKONOME_PLAID_REAP_DAYS (default 30)
consecutive days is released (/item/remove), archived locally with a
reason, stamped in the slot ledger, and the user is told on the Accounts
page and in the daily email. The same daily job reconciles the DB→Plaid
direction: an active item that turns out to be ITEM_NOT_FOUND at Plaid
is archived locally (the reverse direction cannot be enumerated — see
jobs/reaper.py's module doc; the slot ledger is that audit surface).
"""

import datetime as dt
import json
import os
import unittest
import uuid

import httpx

from oikonome.db import tenancy
from oikonome.engine import alerts
from oikonome.jobs import reaper
from oikonome.sync import plaid

from .util import TEST_DB, _admin_dsn, make_db, write_config

NOW = dt.datetime.now(dt.timezone.utc)


def _plaid_transport(calls: list, not_found_tokens: set[str] = frozenset()):
    """Mock Plaid: records (path, access_token); /item/get 400s
    ITEM_NOT_FOUND for tokens in `not_found_tokens`."""
    def handler(request):
        body = json.loads(request.content.decode() or "{}")
        calls.append((request.url.path, body.get("access_token")))
        if request.url.path == "/item/get" \
                and body.get("access_token") in not_found_tokens:
            return httpx.Response(400, text=json.dumps(
                {"error_code": "ITEM_NOT_FOUND", "error_type": "ITEM_ERROR",
                 "error_message": "gone"}))
        return httpx.Response(200, text="{}")
    return httpx.MockTransport(handler)


def _client(calls, not_found=frozenset()):
    return plaid.Client("cid", "sec", plaid.ENV_URLS["sandbox"],
                        transport=_plaid_transport(calls, not_found))


class ReaperBase(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self._env = os.environ.get("OIKONOME_PLAID_REAP_DAYS")
        os.environ.pop("OIKONOME_PLAID_REAP_DAYS", None)

    def tearDown(self):
        if self._env is None:
            os.environ.pop("OIKONOME_PLAID_REAP_DAYS", None)
        else:
            os.environ["OIKONOME_PLAID_REAP_DAYS"] = self._env
        self.conn.close()

    def _item(self, status="error:ITEM_LOGIN_REQUIRED", name="Dead Bank",
              webhook_status=None, webhook_days_ago=None,
              error_days_ago=35, ok_days_ago=None, ledger=False):
        """A plaid item + its sync_log history. error_days_ago = start of
        the failure streak (hourly errors since); ok_days_ago = last
        successful sync (None = never succeeded)."""
        iid = f"reap-{uuid.uuid4().hex[:10]}"
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, "
            "access_token, status, webhook_status, webhook_status_at) "
            "VALUES (%s,'plaid',%s,%s,%s,%s,%s)",
            (iid, name, f"tok-{iid}", status, webhook_status,
             (NOW - dt.timedelta(days=webhook_days_ago))
             if webhook_days_ago is not None else None))
        if ok_days_ago is not None:
            self.conn.execute(
                "INSERT INTO sync_log (item_id, ran_at, added) "
                "VALUES (%s,%s,0)",
                (iid, NOW - dt.timedelta(days=ok_days_ago)))
        if error_days_ago is not None:
            for days in (error_days_ago, error_days_ago / 2, 0.04):
                self.conn.execute(
                    "INSERT INTO sync_log (item_id, ran_at, error) "
                    "VALUES (%s,%s,'ITEM_LOGIN_REQUIRED: boom')",
                    (iid, NOW - dt.timedelta(days=days)))
        if ledger:
            self.conn.execute(
                "INSERT INTO plaid_item_ledger (item_id) VALUES (%s) "
                "ON CONFLICT (item_id) DO NOTHING", (iid,))
        return iid

    def _status(self, iid):
        return self.conn.execute(
            "SELECT status, archived_reason, archived_at FROM items "
            "WHERE id=%s", (iid,)).fetchone()


class ReapThresholdTests(ReaperBase):
    def test_reaps_after_30_days_and_releases_at_plaid(self):
        iid = self._item(error_days_ago=31, ledger=True)
        calls: list = []
        out = reaper.reap(self.conn, client=_client(calls), now=NOW)
        self.assertEqual(out["reaped"], ["Dead Bank"])
        row = self._status(iid)
        self.assertEqual(row["status"], "archived")
        self.assertEqual(row["archived_reason"], "auto-reap")
        self.assertIsNotNone(row["archived_at"])
        self.assertIn(("/item/remove", f"tok-{iid}"), calls)
        # slot ledger stamped (admin-side — append-only for the app role)
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            led = admin.execute(
                "SELECT removed_at, removed_reason FROM plaid_item_ledger "
                "WHERE item_id=%s", (iid,)).fetchone()
        finally:
            admin.close()
        self.assertIsNotNone(led["removed_at"])
        self.assertEqual(led["removed_reason"], "auto-reap")

    def test_under_threshold_not_reaped(self):
        iid = self._item(error_days_ago=29)
        calls: list = []
        out = reaper.reap(self.conn, client=_client(calls), now=NOW)
        self.assertEqual(out["reaped"], [])
        self.assertEqual(self._status(iid)["status"],
                         "error:ITEM_LOGIN_REQUIRED")
        self.assertNotIn("/item/remove", [p for p, _ in calls])

    def test_exactly_at_threshold_reaped(self):
        self._item(error_days_ago=30.01)
        out = reaper.reap(self.conn, client=_client([]), now=NOW)
        self.assertEqual(out["reaped"], ["Dead Bank"])

    def test_success_resets_the_streak(self):
        # errors for 40 days BUT a successful sync 10 days ago — the
        # current streak is only 5 days, not 40
        iid = self._item(error_days_ago=5, ok_days_ago=10)
        self.conn.execute(
            "INSERT INTO sync_log (item_id, ran_at, error) "
            "VALUES (%s,%s,'old error')",
            (iid, NOW - dt.timedelta(days=40)))
        out = reaper.reap(self.conn, client=_client([]), now=NOW)
        self.assertEqual(out["reaped"], [])

    def test_transient_errors_never_reap(self):
        # an institution outage is not the user's to fix — 40 days of
        # INSTITUTION_DOWN must not cost them a re-link
        iid = self._item(status="error:INSTITUTION_DOWN", error_days_ago=40)
        out = reaper.reap(self.conn, client=_client([]), now=NOW)
        self.assertEqual(out["reaped"], [])
        self.assertEqual(self._status(iid)["status"],
                         "error:INSTITUTION_DOWN")

    def test_transient_streak_then_link_issue_uses_link_issue_start(self):
        # 31 days of INSTITUTION_DOWN (transient), THEN a fresh
        # ITEM_LOGIN_REQUIRED today. The reap streak must anchor on the
        # login-required day (~0), NOT the outage start — the user just got
        # the re-auth prompt and is owed the full 30-day reconnect window.
        iid = self._item(status="error:ITEM_LOGIN_REQUIRED", error_days_ago=None)
        for d in (31, 20, 10, 2):
            self.conn.execute(
                "INSERT INTO sync_log (item_id, ran_at, error) "
                "VALUES (%s,%s,'INSTITUTION_DOWN: down for maintenance')",
                (iid, NOW - dt.timedelta(days=d)))
        self.conn.execute(
            "INSERT INTO sync_log (item_id, ran_at, error) "
            "VALUES (%s,%s,'ITEM_LOGIN_REQUIRED: reauth')",
            (iid, NOW - dt.timedelta(days=0.04)))
        out = reaper.reap(self.conn, client=_client([]), now=NOW)
        self.assertEqual(out["reaped"], [])       # link issue is fresh
        self.assertEqual(self._status(iid)["status"],
                         "error:ITEM_LOGIN_REQUIRED")

    def test_old_link_issue_under_transient_still_reaps(self):
        # the converse: a 31-day-old ITEM_LOGIN_REQUIRED streak (with some
        # transient noise mixed in) still reaps — the filter counts the
        # link-issue days, which are old enough.
        iid = self._item(status="error:ITEM_LOGIN_REQUIRED", error_days_ago=None)
        self.conn.execute(
            "INSERT INTO sync_log (item_id, ran_at, error) "
            "VALUES (%s,%s,'ITEM_LOGIN_REQUIRED: reauth')",
            (iid, NOW - dt.timedelta(days=31)))
        self.conn.execute(
            "INSERT INTO sync_log (item_id, ran_at, error) "
            "VALUES (%s,%s,'RATE_LIMIT_EXCEEDED: slow down')",
            (iid, NOW - dt.timedelta(days=5)))
        out = reaper.reap(self.conn, client=_client([]), now=NOW)
        self.assertEqual(out["reaped"], ["Dead Bank"])

    def test_webhook_only_error_uses_webhook_timestamp(self):
        # error arrived out of band (webhook) and polling never logged —
        # webhook_status_at anchors the streak
        iid = self._item(status="ok", webhook_status="revoked",
                         webhook_days_ago=31, error_days_ago=None)
        out = reaper.reap(self.conn, client=_client([]), now=NOW)
        self.assertEqual(out["reaped"], ["Dead Bank"])
        self.assertEqual(self._status(iid)["archived_reason"], "auto-reap")

    def test_item_healed_after_the_snapshot_is_not_reaped(self):
        """The reap loop reads all candidates in one upfront SELECT, then
        acts per item. A LOGIN_REPAIRED webhook (plaid_webhook writes on its
        own connection) can heal an Item between that snapshot and its turn
        in the loop — and reaping the stale copy would disconnect a bank the
        person only moments ago finished re-linking. The loop must re-read
        live status before releasing. Simulated by healing the row during
        _failing_since, which runs between the snapshot and the fresh check."""
        iid = self._item(error_days_ago=31, ledger=True)
        calls: list = []
        orig = reaper._failing_since

        def healing(conn, item_id, wsa):
            # the webhook lands now: the Item is healed on the DB
            conn.execute("UPDATE items SET status='ok', webhook_status=NULL, "
                         "webhook_status_at=NULL WHERE id=%s", (item_id,))
            return orig(conn, item_id, wsa)

        reaper._failing_since = healing
        try:
            out = reaper.reap(self.conn, client=_client(calls), now=NOW)
        finally:
            reaper._failing_since = orig
        # not reaped, not released at Plaid — the fresh re-read saw 'ok'
        self.assertEqual(out["reaped"], [])
        self.assertNotIn("/item/remove", [p for p, _ in calls])
        self.assertEqual(self._status(iid)["status"], "ok")

    def test_failed_release_leaves_item_for_next_run(self):
        iid = self._item(error_days_ago=35)

        def handler(request):
            if request.url.path == "/item/remove":
                return httpx.Response(400, text=json.dumps(
                    {"error_code": "INTERNAL_SERVER_ERROR",
                     "error_type": "API_ERROR", "error_message": "oops"}))
            return httpx.Response(200, text="{}")
        client = plaid.Client("cid", "sec", plaid.ENV_URLS["sandbox"],
                              transport=httpx.MockTransport(handler))
        out = reaper.reap(self.conn, client=client, now=NOW)
        # not archived: the Item still exists (and bills) at Plaid
        self.assertEqual(out["reaped"], [])
        self.assertEqual(self._status(iid)["status"],
                         "error:ITEM_LOGIN_REQUIRED")

    def test_archived_with_live_token_is_retried_and_released(self):
        # a user disconnect whose /item/remove failed archived the row
        # but left the token live (still billing at Plaid). The candidate
        # query skips archived, so without the straggler pass it bills
        # forever; the pass retries the release, sheds the token, stamps the
        # ledger.
        iid = self._item(error_days_ago=None, ledger=True)
        self.conn.execute(
            "UPDATE items SET status='archived', "
            "archived_reason='disconnect-release-failed' WHERE id=%s", (iid,))
        calls: list = []
        out = reaper.reap(self.conn, client=_client(calls), now=NOW)
        self.assertEqual(out["released_archived"], ["Dead Bank"])
        self.assertIn(("/item/remove", f"tok-{iid}"), calls)
        row = self.conn.execute(
            "SELECT access_token FROM items WHERE id=%s", (iid,)).fetchone()
        self.assertIsNone(row["access_token"])       # shed → not retried again
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            led = admin.execute(
                "SELECT removed_at, removed_reason FROM plaid_item_ledger "
                "WHERE item_id=%s", (iid,)).fetchone()
        finally:
            admin.close()
        self.assertIsNotNone(led["removed_at"])
        self.assertEqual(led["removed_reason"], "disconnect-retry")

    def test_archived_without_token_not_probed(self):
        # a cleanly-released archived item (token already NULL) is inert
        iid = self._item(error_days_ago=None)
        self.conn.execute(
            "UPDATE items SET status='archived', access_token=NULL "
            "WHERE id=%s", (iid,))
        calls: list = []
        out = reaper.reap(self.conn, client=_client(calls), now=NOW)
        self.assertEqual(out["released_archived"], [])
        self.assertEqual(calls, [])

    def test_disabled_mode(self):
        os.environ["OIKONOME_PLAID_REAP_DAYS"] = "0"
        iid = self._item(error_days_ago=100)
        calls: list = []
        out = reaper.reap(self.conn, client=_client(calls), now=NOW)
        self.assertTrue(out["disabled"])
        self.assertEqual(calls, [])
        self.assertEqual(self._status(iid)["status"],
                         "error:ITEM_LOGIN_REQUIRED")

    def test_custom_threshold_env(self):
        os.environ["OIKONOME_PLAID_REAP_DAYS"] = "7"
        self._item(error_days_ago=8)
        out = reaper.reap(self.conn, client=_client([]), now=NOW)
        self.assertEqual(out["reaped"], ["Dead Bank"])


class ReconcileTests(ReaperBase):
    def test_gone_at_plaid_archives_locally(self):
        # healthy-looking item, quiet for days; Plaid says ITEM_NOT_FOUND
        iid = self._item(status="ok", error_days_ago=None, ok_days_ago=3)
        calls: list = []
        out = reaper.reap(self.conn,
                          client=_client(calls, {f"tok-{iid}"}), now=NOW)
        self.assertEqual(out["orphaned"], ["Dead Bank"])
        row = self._status(iid)
        self.assertEqual(row["status"], "archived")
        self.assertEqual(row["archived_reason"], "plaid-gone")
        self.assertIn(("/item/get", f"tok-{iid}"), calls)
        # no /item/remove for an Item that is already gone
        self.assertNotIn("/item/remove", [p for p, _ in calls])

    def test_recently_synced_items_skip_the_probe(self):
        # a success within the last day proves existence — zero API calls
        self._item(status="ok", error_days_ago=None, ok_days_ago=0.02)
        calls: list = []
        out = reaper.reap(self.conn, client=_client(calls), now=NOW)
        self.assertEqual(out["orphaned"], [])
        self.assertEqual(calls, [])

    def test_other_probe_errors_do_nothing(self):
        iid = self._item(status="ok", error_days_ago=None, ok_days_ago=3)

        def handler(request):
            return httpx.Response(400, text=json.dumps(
                {"error_code": "RATE_LIMIT", "error_type": "API_ERROR",
                 "error_message": "later"}))
        client = plaid.Client("cid", "sec", plaid.ENV_URLS["sandbox"],
                              transport=httpx.MockTransport(handler))
        out = reaper.reap(self.conn, client=client, now=NOW)
        self.assertEqual(out["orphaned"], [])
        self.assertEqual(self._status(iid)["status"], "ok")


class NoticeTests(ReaperBase):
    def _reap_one(self, **kw):
        iid = self._item(**kw)
        reaper.reap(self.conn, client=_client([]), now=NOW)
        return iid

    def test_alert_until_reconnected(self):
        self._reap_one(error_days_ago=35)
        notes = alerts.reaped_connections(self.conn)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["kind"], "connection-removed")
        self.assertEqual(notes[0]["severity"], "warn")
        self.assertIn("Dead Bank", notes[0]["message"])
        self.assertIn("30 days", notes[0]["message"])
        self.assertIn("reconnect", notes[0]["message"])
        # reconnecting the same institution clears the notice
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, "
            "access_token, status) VALUES ('reap-new','plaid','Dead Bank',"
            "'tok-new','ok')")
        self.assertEqual(alerts.reaped_connections(self.conn), [])

    def test_plaid_gone_message(self):
        # reconcile path end-to-end: quiet 'ok' item that ITEM_NOT_FOUNDs
        iid = self._item(status="ok", error_days_ago=None, ok_days_ago=3)
        reaper.reap(self.conn, client=_client([], {f"tok-{iid}"}), now=NOW)
        notes = alerts.reaped_connections(self.conn)
        self.assertEqual(len(notes), 1)
        self.assertIn("no longer exists at Plaid", notes[0]["message"])

    def test_daily_email_carries_the_one_liner(self):
        """The strip renders in the email and Today page alike — the
        gathered status carries the notice and report.build writes it
        into both bodies."""
        self._reap_one(error_days_ago=35)
        from oikonome.web import report
        d = report.gather(self.conn, dt.date.today())
        msgs = [a["message"] for a in d.get("alerts") or []]
        self.assertTrue(any("Dead Bank" in m and "reconnect" in m
                            for m in msgs), msgs)
        _subject, plain, html = report.build(d)
        self.assertIn("Dead Bank", plain)
        self.assertIn("reconnect", plain)
        self.assertIn("Dead Bank", html)

    def test_user_disconnect_gets_no_notice(self):
        # a user-initiated archive (no reason) is not the reaper's business
        iid = self._item(error_days_ago=None)
        self.conn.execute("UPDATE items SET status='archived' WHERE id=%s",
                          (iid,))
        self.assertEqual(alerts.reaped_connections(self.conn), [])

    def test_accounts_payload_shows_reaped_state(self):
        from oikonome.web.pages import _connections
        iid = self._reap_one(error_days_ago=35)
        rows = {r["id"]: r for r in _connections(self.conn)}
        self.assertIn(iid, rows)
        self.assertEqual(rows[iid]["status"], "reaped")
        # a user-initiated archive stays hidden
        iid2 = self._item(error_days_ago=None)
        self.conn.execute("UPDATE items SET status='archived' WHERE id=%s",
                          (iid2,))
        rows = {r["id"]: r for r in _connections(self.conn)}
        self.assertNotIn(iid2, rows)


if __name__ == "__main__":
    unittest.main()
