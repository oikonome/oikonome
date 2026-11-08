"""Each connection's "last successful pull" is its newest sync that did
not fail, read per item from the partial index rather than by joining
the whole sync history: the log keeps years of rows, and the Accounts page
polls this list every few seconds per open tab."""

import datetime as dt
import unittest

from oikonome.db import tenancy
from oikonome.web import doctor, pages

from .util import TEST_DB, _admin_dsn, make_db

T0 = dt.datetime(2031, 4, 2, 12, 0, tzinfo=dt.timezone.utc)


class NewestSuccessfulSyncTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        # The plans below must not depend on whether autovacuum has reached
        # this table yet: a database made a moment ago has no statistics for
        # it, and the planner then picks the other index.
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            admin.execute("ANALYZE sync_log")
        finally:
            admin.close()
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, "
            "access_token) VALUES ('it-a','plaid','Alder Savings','t'), "
            "('it-b','plaid','Birch Credit Union','t'), "
            "('it-c','simplefin-org','Cedar Bank','t')")
        rows = [
            ("it-a", T0 - dt.timedelta(hours=5), None),
            ("it-a", T0 - dt.timedelta(hours=2), None),
            # a failure after the newest success must not count as a pull
            ("it-a", T0 - dt.timedelta(hours=1), "ITEM_LOGIN_REQUIRED"),
            ("it-b", T0 - dt.timedelta(days=3), None),
            # it-c has only ever failed
            ("it-c", T0 - dt.timedelta(minutes=10), "connection reset"),
        ]
        for item, at, err in rows:
            self.conn.execute(
                "INSERT INTO sync_log (item_id, added, error, ran_at) "
                "VALUES (%s, 0, %s, %s)", (item, err, at))

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    def test_each_connection_reports_its_newest_successful_sync(self):
        by_id = {r["id"]: r for r in pages._connections(self.conn)}
        self.assertEqual(by_id["it-a"]["last_ok"],
                         T0 - dt.timedelta(hours=2))
        self.assertEqual(by_id["it-b"]["last_ok"], T0 - dt.timedelta(days=3))
        self.assertIsNone(by_id["it-c"]["last_ok"])

    def test_one_row_per_connection_however_long_the_history(self):
        self.conn.execute(
            "INSERT INTO sync_log (item_id, added, error, ran_at) "
            "SELECT 'it-b', 0, NULL, %s - g * interval '1 hour' "
            "FROM generate_series(4 * 24, 4 * 24 + 500) g", (T0,))
        ids = [r["id"] for r in pages._connections(self.conn)]
        self.assertEqual(sorted(ids), ["it-a", "it-b", "it-c"])

    def _plan(self, sql: str) -> str:
        # tiny tables tempt the planner into a sequential scan; ask it what
        # it would do with the index available and scans discouraged
        self.conn.execute("SET LOCAL enable_seqscan = off")
        self.conn.execute("SET LOCAL enable_bitmapscan = off")
        try:
            return "\n".join(list(r.values())[0] for r in self.conn.execute(
                "EXPLAIN (COSTS OFF) " + sql).fetchall())
        finally:
            self.conn.execute("RESET enable_seqscan")
            self.conn.execute("RESET enable_bitmapscan")

    def _sql_of(self, fn) -> str:
        """The statement a reader actually sends, captured on the way."""
        seen = []
        conn = self.conn

        class Recorder:
            def execute(self, sql, *a, **kw):
                seen.append(sql)
                return conn.execute(sql, *a, **kw)
        fn(Recorder())
        return next(q for q in seen if "sync_log" in q)

    def test_the_connections_list_uses_the_per_item_index(self):
        plan = self._plan(self._sql_of(pages._connections))
        # either per-connection index reads one connection, never the log
        self.assertRegex(plan, r"idx_sync_log_item_(last_ok|ran)")
        self.assertNotIn("Join", plan)

    def test_the_diagnostics_page_uses_the_per_item_index(self):
        now = dt.datetime.now(dt.timezone.utc)
        plan = self._plan(self._sql_of(
            lambda c: doctor._connection_rows(c, now)))
        # either per-connection index reads one connection, never the log
        self.assertRegex(plan, r"idx_sync_log_item_(last_ok|ran)")
        self.assertNotIn("Join", plan)

    def test_the_header_chip_reads_one_entry_of_the_time_index(self):
        plan = self._plan(
            "SELECT ran_at AS t FROM sync_log WHERE error IS NULL "
            "ORDER BY ran_at DESC LIMIT 1")
        self.assertIn("idx_sync_log_last_ok", plan)
        self.assertNotIn("Sort", plan)


if __name__ == "__main__":
    unittest.main()
