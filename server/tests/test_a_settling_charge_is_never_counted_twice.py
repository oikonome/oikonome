"""A charge settling from pending to posted is never live twice.

The posted row arrives under a new id and its pending hold is retired in
the same sync. Spend reads every row with `removed = 0`, so if another
connection can ever see the posted row committed while its hold is still
live, a Today load, a widget poll or an email rendered in that moment
counts the charge twice. The invariant: from any other connection, the
posted row and the retirement of its hold become visible together.
"""
import os
import unittest

from oikonome.db import tenancy
from oikonome.sync import base as sync_base

from .test_a_receipt_snapped_early_meets_its_charge import DAY, _txn
from .util import TEST_DB, make_db


def _other_conn(conn):
    tid = conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
    dsn = os.environ.get(
        "OIKONOME_TEST_DSN",
        f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}")
    return tenancy.tenant_connect(tid, dsn)


class _Watched:
    """The sync's connection, with a look from a second connection taken
    just before any statement that retires rows — the last moment the
    settlement has not yet finished."""

    def __init__(self, conn, look):
        self._conn, self._look = conn, look

    def execute(self, sql, *a, **kw):
        if isinstance(sql, str) and "SET removed=1" in sql:
            self._look()
        return self._conn.execute(sql, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class SettlingChargeIsNeverLiveTwiceTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.other = _other_conn(self.conn)

    def tearDown(self):
        self.other.close()
        self.conn.close()

    def _live(self, ids):
        return sorted(r["id"] for r in self.other.execute(
            "SELECT id FROM transactions WHERE id = ANY(%s) AND removed = 0",
            (ids,)).fetchall())

    def test_another_reader_never_sees_the_hold_and_its_posted_row_live(self):
        sync_base.upsert_transactions(self.conn, [
            _txn("hold-s1", 18.40, "CORNER CAFE", pending=True, date=DAY),
            _txn("hold-s2", 61.05, "HARDWARE BARN", pending=True, date=DAY)])
        ids = ["hold-s1", "post-s1", "hold-s2", "post-s2"]
        seen = []
        watched = _Watched(self.conn, lambda: seen.append(self._live(ids)))
        sync_base.upsert_transactions(watched, [
            _txn("post-s1", 18.40, "CORNER CAFE",
                 raw={"pending_transaction_id": "hold-s1"}),
            _txn("post-s2", 63.05, "HARDWARE BARN",
                 raw={"pending_transaction_id": "hold-s2"})])
        self.assertTrue(seen, "the settlement never retired a hold")
        for live in seen:
            # mid-settlement the other reader still sees the old world whole
            self.assertEqual(live, ["hold-s1", "hold-s2"])
        self.assertEqual(self._live(ids), ["post-s1", "post-s2"])

    def test_a_page_with_nothing_to_settle_still_lands(self):
        sync_base.upsert_transactions(self.conn, [
            _txn("plain-1", 9.99, "BOOKSHOP")])
        self.assertEqual(self._live(["plain-1"]), ["plain-1"])


if __name__ == "__main__":
    unittest.main()
