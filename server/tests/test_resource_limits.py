"""Behaviour that must hold once the data gets big.

Each of these works on a small ledger and fails quietly on a household with
years of history, so each is pinned at a size that exercises the limit.
"""

import io
import json
import unittest
import uuid
import zipfile
from unittest import mock


from oikonome.sync import restore
from oikonome.web import security

from .util import TODAY, make_db, write_config


def _zip_with_transactions(n: int, *, dupe_ids: int = 0) -> io.BytesIO:
    """A minimal backup ZIP carrying `n` transactions on one account, plus
    `dupe_ids` rows that repeat earlier ids (so ON CONFLICT DO NOTHING has
    something to skip)."""
    import csv
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        w = io.StringIO()
        c = csv.writer(w)
        c.writerow(["id", "aggregator", "institution_id", "institution_name",
                    "status", "raw"])
        c.writerow(["it1", "test", "", "Test Bank", "ok", "{}"])
        z.writestr("items.csv", w.getvalue())

        w = io.StringIO()
        c = csv.writer(w)
        c.writerow(["id", "item_id", "name", "display_name", "type", "subtype",
                    "mask", "balance_current", "balance_available", "currency",
                    "owner", "entity_id", "raw"])
        c.writerow(["acc1", "it1", "Checking", "", "depository", "checking",
                    "1234", "100", "100", "USD", "", "", "{}"])
        z.writestr("accounts.csv", w.getvalue())

        w = io.StringIO()
        c = csv.writer(w)
        c.writerow(["id", "account_id", "date", "amount", "name",
                    "merchant_name", "category_primary", "category_detailed",
                    "category_override", "pending", "removed", "entity_id",
                    "raw"])
        for i in range(n):
            c.writerow([f"t{i}", "acc1", TODAY.isoformat(), "10.00",
                        f"MERCHANT {i}", "", "GENERAL_MERCHANDISE", "", "",
                        "0", "0", "", "{}"])
        for i in range(dupe_ids):
            c.writerow([f"t{i}", "acc1", TODAY.isoformat(), "10.00",
                        f"MERCHANT {i}", "", "GENERAL_MERCHANDISE", "", "",
                        "0", "0", "", "{}"])
        z.writestr("transactions.csv", w.getvalue())
    buf.seek(0)
    return buf


class RestoreBatchingTests(unittest.TestCase):
    """Transactions restore in batches, and the counters still tell the truth.

    The ledger is the one member of a backup that scales with years of use,
    so it is not restored one client/server round trip per row. Batching is
    only correct if the counters still match what per-row `cur.rowcount`
    accounting would report — that is the part worth a test, not the speed.
    """

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_more_rows_than_one_chunk_all_land(self):
        n = 2500                                  # > CHUNK (1000): 3 flushes
        out = restore.restore_zip(self.conn, _zip_with_transactions(n).getvalue())
        self.assertEqual(out["transactions"], n)
        got = self.conn.execute(
            "SELECT count(*) AS n FROM transactions").fetchone()["n"]
        self.assertEqual(got, n, "every row must survive the batching")

    def test_duplicate_ids_are_counted_as_skipped_not_inserted(self):
        out = restore.restore_zip(
            self.conn, _zip_with_transactions(1200, dupe_ids=200).getvalue())
        self.assertEqual(out["transactions"], 1200)
        self.assertGreaterEqual(out.get("already_present", 0), 200,
                                "ON CONFLICT DO NOTHING rows are skips, and "
                                "a batched rowcount must not count them as "
                                "inserts")
        got = self.conn.execute(
            "SELECT count(*) AS n FROM transactions").fetchone()["n"]
        self.assertEqual(got, 1200)

    def test_a_restore_is_still_idempotent(self):
        restore.restore_zip(self.conn, _zip_with_transactions(1100).getvalue())
        out = restore.restore_zip(
            self.conn, _zip_with_transactions(1100).getvalue())
        self.assertEqual(out.get("transactions", 0), 0,
                         "the second restore inserts nothing")
        got = self.conn.execute(
            "SELECT count(*) AS n FROM transactions").fetchone()["n"]
        self.assertEqual(got, 1100)


class DualStoreFallbackTests(unittest.TestCase):
    """One helper for "Redis, else in-process", shared by every limiter:
    getting the fallback backwards means a limit silently stops applying
    exactly when Redis is struggling."""

    def test_falls_back_when_there_is_no_shared_store(self):
        with mock.patch.object(security, "_shared_store", return_value=None):
            self.assertTrue(security._dual("check", ("t", uuid.uuid4().hex),
                                           2, 60))

    def test_falls_back_when_redis_answers_none(self):
        dead = mock.Mock()
        dead.check.return_value = None            # Redis down
        key = ("t", uuid.uuid4().hex)
        with mock.patch.object(security, "_shared_store", return_value=dead):
            self.assertTrue(security._dual("check", key, 1, 60))
            # the in-process limiter really took the hit, so the SECOND call
            # is refused — proving the fallback counted, not just returned
            self.assertFalse(security._dual("check", key, 1, 60))

    def test_a_real_answer_from_redis_wins(self):
        live = mock.Mock()
        live.check.return_value = False           # over the limit per Redis
        with mock.patch.object(security, "_shared_store", return_value=live):
            self.assertFalse(security._dual("check", ("t", "x"), 99, 60))

    def test_zero_and_false_are_answers_not_absences(self):
        """count()==0 and check()==False must NOT be treated as 'Redis is
        down, ask the local store' — that is the subtle way a hand-rolled
        `or` gets this wrong."""
        live = mock.Mock()
        live.count.return_value = 0
        with mock.patch.object(security, "_shared_store", return_value=live):
            self.assertEqual(security._dual("count", ("t", "y"), 60), 0)
        live.count.assert_called_once()

    def test_clear_reaches_both_stores(self):
        live = mock.Mock()
        key = ("t", uuid.uuid4().hex)
        security._limiter.add(key, 60)
        with mock.patch.object(security, "_shared_store", return_value=live):
            security._dual_clear("clear", key)
        live.clear.assert_called_once_with(key)
        self.assertEqual(security._limiter.count(key, 60), 0,
                         "a key left in the in-process store would keep a "
                         "cleared count alive on that one worker")


class TenantExportStreamTests(unittest.TestCase):
    """The export streams rather than materialising each table three times."""

    def setUp(self):
        from .util import add_txn
        self.conn = make_db()
        write_config(self.conn)
        for i in range(50):
            add_txn(self.conn, TODAY, 10.0 + i, f"PAYEE {i}")
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]

    def tearDown(self):
        self.conn.close()

    def test_archive_is_valid_json_and_counts_match(self):
        from oikonome import tenant_export
        blob = tenant_export.build_archive(self.tid, operator="test",
                                           consented=True)
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            manifest = json.loads(z.read("manifest.json"))
            for table, n in manifest["tables"].items():
                rows = json.loads(z.read(f"data/{table}.json"))
                self.assertIsInstance(rows, list, f"{table} must be a list")
                self.assertEqual(len(rows), n,
                                 f"{table}: manifest count must match the "
                                 f"streamed rows")
        self.assertGreaterEqual(manifest["tables"].get("transactions", 0), 50)

    def test_an_empty_table_is_an_empty_array(self):
        from oikonome import tenant_export
        blob = tenant_export.build_archive(self.tid, operator="test",
                                           consented=True)
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            manifest = json.loads(z.read("manifest.json"))
            empty = [t for t, n in manifest["tables"].items() if n == 0]
            self.assertTrue(empty, "fixture should leave some table empty")
            for t in empty:
                self.assertEqual(json.loads(z.read(f"data/{t}.json")), [])


if __name__ == "__main__":
    unittest.main()
