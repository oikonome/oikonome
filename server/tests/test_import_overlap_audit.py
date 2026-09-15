"""A heuristic import-overlap drop must leave a diagnosable record.

filter_import_duplicates suppresses an incoming aggregator transaction
when a file-imported row appears to already cover it — a heuristic match
(same amount, ±3 days, name evidence). A false positive permanently loses
a real transaction: the aggregator's cursor advances past it and never
offers it again. When only an aggregate count was logged, such a drop was
undiagnosable and unrecoverable; each suppressed row's identity (id, date,
amount, name) must land in sync_log so an operator can find a wrong drop
and re-enter the row.
"""

import datetime as dt
import unittest

from oikonome.sync import base

from .util import add_txn, make_db, write_config


class SuppressedRowAuditTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _overlap_log_rows(self):
        return self.conn.execute(
            "SELECT error FROM sync_log WHERE item_id='import-overlap'"
        ).fetchall()

    def test_each_suppressed_row_is_recorded_in_sync_log(self):
        add_txn(self.conn, dt.date(2026, 7, 10), 42.50, "SAFEWAY #123",
                account="chk", txn_id="csv:1")
        incoming = base.Transaction(
            id="sfin:abc", account_id="acc-chk",
            date=dt.date(2026, 7, 11), amount=42.50, name="SAFEWAY STORE")
        kept, skipped = base.filter_import_duplicates(self.conn, [incoming])
        self.assertEqual((len(kept), skipped), (0, 1))
        rows = self._overlap_log_rows()
        self.assertEqual(len(rows), 1)
        detail = rows[0]["error"]
        # the opaque id + date locate the row in the tenant's own ledger
        for needle in ("sfin:abc", "2026-07-11"):
            self.assertIn(needle, detail)
        # but the amount and merchant name are third-party PII and must
        # NOT reach the operator-visible sync_log
        self.assertNotIn("42.50", detail)
        self.assertNotIn("SAFEWAY", detail)

    def test_a_clean_sync_writes_no_audit_row(self):
        add_txn(self.conn, dt.date(2026, 7, 10), 42.50, "SAFEWAY #123",
                account="chk", txn_id="csv:1")
        incoming = base.Transaction(
            id="sfin:xyz", account_id="acc-chk",
            date=dt.date(2026, 7, 11), amount=99.99, name="TARGET")
        kept, skipped = base.filter_import_duplicates(self.conn, [incoming])
        self.assertEqual((len(kept), skipped), (1, 0))
        self.assertEqual(self._overlap_log_rows(), [])

    def test_the_audit_row_never_counts_as_a_successful_sync(self):
        """Freshness checks key on `error IS NULL` — the audit record must
        carry error text so it can't masquerade as a good sync run."""
        add_txn(self.conn, dt.date(2026, 7, 10), 42.50, "SAFEWAY #123",
                account="chk", txn_id="csv:1")
        incoming = base.Transaction(
            id="sfin:abc", account_id="acc-chk",
            date=dt.date(2026, 7, 11), amount=42.50, name="SAFEWAY STORE")
        base.filter_import_duplicates(self.conn, [incoming])
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM sync_log "
            "WHERE item_id='import-overlap' AND error IS NULL"
        ).fetchone()["n"]
        self.assertEqual(n, 0)

    def test_import_overlap_is_not_counted_as_an_operator_sync_error(self):
        """The console's per-tenant sync_errors(24h) FILTER excludes the
        import-overlap sentinel, so a benign suppression never shows the
        operator a phantom failure on a healthy tenant."""
        add_txn(self.conn, dt.date(2026, 7, 10), 42.50, "SAFEWAY #123",
                account="chk", txn_id="csv:1")
        base.filter_import_duplicates(self.conn, [base.Transaction(
            id="sfin:abc", account_id="acc-chk", date=dt.date(2026, 7, 11),
            amount=42.50, name="SAFEWAY STORE")])
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        n = self.conn.execute(
            "SELECT count(*) FILTER (WHERE error IS NOT NULL AND "
            "item_id <> 'import-overlap' AND "
            "ran_at > now() - interval '24 hours') AS sync_errors "
            "FROM sync_log WHERE tenant_id = %s", (tid,)).fetchone()["sync_errors"]
        self.assertEqual(n, 0, "the overlap sentinel must not read as a "
                              "sync failure in the operator console")


if __name__ == "__main__":
    unittest.main()
