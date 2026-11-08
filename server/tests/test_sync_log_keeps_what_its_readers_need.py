"""The sync log is bounded, and what it keeps is exactly what its readers
ask for.

Every pull writes a row, so a long-lived household's log grows by
thousands of rows a month. The per-item probes that do not filter on the
error column (has this connection ever been pulled, was it tried in the
last few seconds, what did its last pull say, the reaper's failure
streak) must read one connection's rows from an index, not every row the
household ever logged. And the retention pass may drop a row only when no
reader can still see it: each connection's newest success, every failure
after it, and every row of a connection that never succeeded stay."""

import datetime as dt
import unittest

import psycopg

from oikonome.db import tenancy
from oikonome.jobs import reaper, retention, worker
from oikonome.web import data

from .util import TEST_DB, _admin_dsn, make_db

NOW = dt.datetime.now(dt.timezone.utc)
OLD = NOW - dt.timedelta(days=retention.SYNC_LOG_KEEP_DAYS + 40)


def _log(conn, item, at, err=None):
    conn.execute("INSERT INTO sync_log (item_id, added, error, ran_at) "
                 "VALUES (%s, 0, %s, %s)", (item, err, at))


class ProbePlanTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            admin.execute("ANALYZE sync_log")
        finally:
            admin.close()
        _log(self.conn, "it1", NOW - dt.timedelta(hours=1))

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    def _plan(self, sql: str, params=()) -> str:
        # tiny tables tempt the planner into a sequential scan; ask it what
        # it would do with the indexes available and scans discouraged (a
        # session SET: the pooled connection runs in autocommit, where SET
        # LOCAL is a no-op)
        self.conn.execute("SET enable_seqscan = off")
        self.conn.execute("SET enable_bitmapscan = off")
        try:
            return "\n".join(list(r.values())[0] for r in self.conn.execute(
                "EXPLAIN (COSTS OFF) " + sql, params).fetchall())
        finally:
            self.conn.execute("RESET enable_seqscan")
            self.conn.execute("RESET enable_bitmapscan")

    def _sent(self, fn, needle: str):
        """The statement (and parameters) a reader actually sends."""
        seen = []
        conn = self.conn

        class Recorder:
            def execute(self, sql, params=(), *a, **kw):
                seen.append((sql, params))
                return conn.execute(sql, params, *a, **kw)
        fn(Recorder())
        return next(s for s in seen if needle in s[0])

    def test_never_pulled_reads_one_connection_from_the_index(self):
        sql, params = self._sent(lambda c: worker.never_pulled(c, "it1"),
                                 "sync_log")
        self.assertIn("idx_sync_log_item_ran", self._plan(sql, params))

    def test_the_reapers_failure_streak_reads_the_index(self):
        sql, params = self._sent(
            lambda c: reaper._failing_since(c, "it1", None),
            "error IS NOT NULL")
        self.assertIn("idx_sync_log_item_ran", self._plan(sql, params))

    def test_the_health_list_reads_each_last_error_from_the_index(self):
        sql, params = self._sent(data.items_health, "last_error")
        plan = self._plan(sql, params)
        self.assertIn("idx_sync_log_item_ran", plan)
        # newest first straight off the index, no sort of the log
        self.assertNotIn("Sort Key: s", plan)

    def test_the_backfill_probes_read_the_index(self):
        # the two per-item sync_log subqueries of the onboarding backfill
        # status (web/api.py), as that query spells them
        plan = self._plan(
            """SELECT (SELECT MAX(ran_at) FROM sync_log s
                       WHERE s.item_id = i.id
                         AND s.ran_at > now() - interval '45 seconds'),
                      EXISTS (SELECT 1 FROM sync_log s WHERE s.item_id = i.id)
               FROM items i""")
        self.assertNotIn("sync_log_pkey", plan)
        self.assertEqual(plan.count("idx_sync_log_item_ran"), 2, plan)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, "
            "access_token, linked_at) VALUES "
            "('ok', 'plaid', 'Alder Savings', 't', %s), "
            "('streak', 'plaid', 'Birch Credit Union', 't', %s), "
            "('never', 'plaid', 'Cedar Bank', 't', %s)",
            (OLD, OLD, OLD))

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    def _rows(self, item):
        return [(r["ran_at"], r["error"]) for r in self.conn.execute(
            "SELECT ran_at, error FROM sync_log WHERE item_id=%s "
            "ORDER BY ran_at", (item,)).fetchall()]

    def test_old_rows_go_and_what_readers_ask_for_stays(self):
        # 'ok': a long history, newest success recent
        for d in range(300, 0, -10):
            _log(self.conn, "ok", NOW - dt.timedelta(days=d))
        _log(self.conn, "ok", NOW - dt.timedelta(days=250), "RATE_LIMIT")
        # 'streak': last success long ago, failing ever since
        _log(self.conn, "streak", OLD - dt.timedelta(days=30))
        _log(self.conn, "streak", OLD - dt.timedelta(days=20))
        _log(self.conn, "streak", OLD - dt.timedelta(days=10),
             "ITEM_LOGIN_REQUIRED: login expired")
        _log(self.conn, "streak", NOW - dt.timedelta(days=1),
             "ITEM_LOGIN_REQUIRED: login expired")
        # 'never': only ever failed, long ago
        _log(self.conn, "never", OLD, "INSTITUTION_DOWN")
        _log(self.conn, "never", OLD + dt.timedelta(days=1),
             "ITEM_LOGIN_REQUIRED: login expired")
        # bookkeeping rows (an import undone) carry their note as an error
        _log(self.conn, "import-undo", OLD, "batch 7: 3 rows kept")

        before = {
            "failing": reaper._failing_since(self.conn, "streak", None),
            "failing_never": reaper._failing_since(self.conn, "never", None),
            "health": {r["id"]: (r["last_ok"], r["last_error"])
                       for r in data.items_health(self.conn)},
        }
        deleted = retention.prune_sync_log(self.conn, batch=7)
        self.assertGreater(deleted, 0)

        cutoff = NOW - dt.timedelta(days=retention.SYNC_LOG_KEEP_DAYS)
        ok = self._rows("ok")
        self.assertTrue(all(at > cutoff for at, _ in ok), ok)
        self.assertEqual(len(ok), len([d for d in range(300, 0, -10)
                                       if d < retention.SYNC_LOG_KEEP_DAYS]))
        # the newest success survives the window, and the streak after it
        self.assertEqual(self._rows("streak"), [
            (OLD - dt.timedelta(days=20), None),
            (OLD - dt.timedelta(days=10), "ITEM_LOGIN_REQUIRED: login expired"),
            (NOW - dt.timedelta(days=1), "ITEM_LOGIN_REQUIRED: login expired"),
        ])
        self.assertEqual(len(self._rows("never")), 2)
        self.assertEqual(len(self._rows("import-undo")), 1)

        self.assertEqual(
            reaper._failing_since(self.conn, "streak", None),
            before["failing"])
        self.assertEqual(
            reaper._failing_since(self.conn, "never", None),
            before["failing_never"])
        self.assertEqual(
            {r["id"]: (r["last_ok"], r["last_error"])
             for r in data.items_health(self.conn)}, before["health"])
        # a second pass finds nothing left to drop
        self.assertEqual(retention.prune_sync_log(self.conn), 0)

    def test_a_connection_with_any_row_still_counts_as_pulled(self):
        self.conn.execute("UPDATE items SET linked_at = now()")
        _log(self.conn, "ok", OLD - dt.timedelta(days=1))
        _log(self.conn, "ok", OLD)
        _log(self.conn, "never", OLD, "INSTITUTION_DOWN")
        retention.prune_sync_log(self.conn)
        self.assertFalse(worker.never_pulled(self.conn, "ok"))
        self.assertFalse(worker.never_pulled(self.conn, "never"))
        self.assertTrue(worker.never_pulled(self.conn, "streak"))

    def test_one_households_success_never_licenses_anothers_delete(self):
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            a = tenancy.create_tenant(admin, "retention-a")
            b = tenancy.create_tenant(admin, "retention-b")
            with admin.transaction():
                ins = ("INSERT INTO sync_log (tenant_id, item_id, added, "
                       "error, ran_at) VALUES (%s, 'shared', 0, %s, %s)")
                admin.execute(ins, (a, None, OLD))
                admin.execute(ins, (a, None, NOW))
                # b: the same item id, only one old success
                admin.execute(ins, (b, None, OLD - dt.timedelta(days=1)))
                retention.prune_sync_log(admin)
                left = {(str(r["tenant_id"]), r["ran_at"]) for r in
                        admin.execute(
                            "SELECT tenant_id, ran_at FROM sync_log "
                            "WHERE tenant_id = ANY(%s)",
                            ([a, b],)).fetchall()}
                self.assertEqual(left, {
                    (str(a), NOW), (str(b), OLD - dt.timedelta(days=1))})
                raise psycopg.Rollback()
        finally:
            admin.execute("DELETE FROM tenants WHERE id = ANY(%s)",
                          ([a, b],))
            admin.close()


class UnlockRetentionTests(unittest.TestCase):
    def test_spent_unlock_links_go_and_live_ones_stay(self):
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            tid = tenancy.create_tenant(admin, "retention-unlocks")
            with admin.transaction():
                uid = admin.execute(
                    "INSERT INTO users (tenant_id, email, password_hash) "
                    "VALUES (%s, 'unlocks@example.test', 'x') RETURNING id",
                    (tid,)).fetchone()["id"]
                ins = ("INSERT INTO login_unlocks (user_id, token_hash, "
                       "expires_at, unlocked_until, created_at) "
                       "VALUES (%s, %s, %s, %s, %s)")
                long_ago = NOW - dt.timedelta(days=90)
                admin.execute(ins, (uid, "dead", long_ago, long_ago,
                                    long_ago))
                admin.execute(ins, (uid, "never-followed", long_ago, None,
                                    long_ago))
                admin.execute(ins, (uid, "live", NOW + dt.timedelta(hours=1),
                                    None, NOW))
                retention.prune_login_unlocks(admin)
                left = {r["token_hash"] for r in admin.execute(
                    "SELECT token_hash FROM login_unlocks WHERE user_id=%s",
                    (uid,)).fetchall()}
                self.assertEqual(left, {"live"})
                raise psycopg.Rollback()
        finally:
            admin.execute("DELETE FROM tenants WHERE id = %s", (tid,))
            admin.close()


if __name__ == "__main__":
    unittest.main()
