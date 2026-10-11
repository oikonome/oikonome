"""A file row the bank feed set its copy aside for, before the feed noted
that on the row, is still kept by an undo.

The import-overlap guard used to record the fact only as an activity-log
line naming each set-aside feed row's id and date. The upgrade reads those
lines back and notes the file rows the guard could have matched, so an
undo of that import keeps them: the feed will not send those charges again.
"""
import datetime as dt
import re
import unittest
from pathlib import Path

from oikonome.db import tenancy
from oikonome.sync import base, batches

from .util import TEST_DB, _admin_dsn, make_db, write_config

MIGRATIONS = Path(base.__file__).resolve().parents[1] / "db" / "migrations"
SQL = MIGRATIONS / "169_imported_rows_the_overlap_log_names_are_kept.sql"
PARTIAL_SQL = (MIGRATIONS
               / "171_a_partial_overlap_line_keeps_every_earlier_import.sql")
DAY = dt.date(2025, 3, 10)
LOGGED = dt.datetime(2025, 4, 1, tzinfo=dt.timezone.utc)


class OverlapLogBackfill(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name)"
            " VALUES ('files', 'manual', 'Files')")
        # the bank feed whose pulls wrote the log lines, still syncing
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name)"
            " VALUES ('feed', 'plaid', 'Sample Bank')")
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype)"
            " VALUES ('filechk', 'files', 'Checking (file)', 'depository',"
            " 'checking')")
        self.batch = self._import(
            [("csv:near", DAY + dt.timedelta(days=2)),
             ("csv:far", DAY + dt.timedelta(days=40)),
             ("csv:span", DAY + dt.timedelta(days=70))],
            before_the_log=True)

    def _import(self, rows, *, before_the_log):
        batch = batches.create(self.conn, "csv", "filechk", "a.csv")
        tagged = batches.tag(self.conn, [
            base.Transaction(id=tid, account_id="filechk", date=day,
                             amount=12.0, name="CORNER GROCER")
            for tid, day in rows], batch)
        base.upsert_transactions(self.conn, tagged)
        batches.finish(self.conn, batch, len(rows))
        if before_the_log:
            self.conn.execute(
                "UPDATE import_batches SET created_at = %s WHERE id = %s",
                (LOGGED - dt.timedelta(days=10), batch))
        return batch

    def _log(self, detail):
        self.conn.execute(
            "INSERT INTO sync_log (item_id, added, error, ran_at)"
            " VALUES ('import-overlap', 0, %s, %s)",
            ("suppressed as covered by file-imported rows: " + detail,
             LOGGED))

    def _upgrade(self):
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            admin.execute(SQL.read_text())
            admin.execute(PARTIAL_SQL.read_text())
        finally:
            admin.close()

    def _marked(self, tid):
        r = self.conn.execute("SELECT raw FROM transactions WHERE id = %s",
                              (tid,)).fetchone()
        return r is not None and base.STOOD_IN_KEY in (r["raw"] or {})

    def test_a_row_within_reach_of_a_logged_date_survives_an_undo(self):
        self._log(f"pf-1 {DAY}; pf-2 not-a-date")
        self._upgrade()
        self.assertTrue(self._marked("csv:near"))
        self.assertFalse(self._marked("csv:far"))
        res = batches.rollback(self.conn, self.batch)
        self.assertEqual(res["kept"], 1)
        self.assertTrue(self._marked("csv:near"),
                        "the only copy of the charge was deleted")
        self.assertFalse(self._marked("csv:far"))

    def test_a_line_that_names_only_the_first_rows_covers_every_earlier_import(self):
        # the unlisted rows can fall anywhere: the listed dates bound nothing
        self._log(f"pf-1 {DAY}; pf-2 {DAY + dt.timedelta(days=60)}; +40 more")
        self._upgrade()
        self.assertTrue(self._marked("csv:near"))
        self.assertTrue(self._marked("csv:far"))
        self.assertTrue(self._marked("csv:span"))
        res = batches.rollback(self.conn, self.batch)
        self.assertEqual(res["kept"], 3)

    def test_a_partial_line_reaches_other_accounts_and_dates(self):
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype)"
            " VALUES ('filecard', 'files', 'Card (file)', 'credit',"
            " 'credit card')")
        batch = batches.create(self.conn, "csv", "filecard", "b.csv")
        base.upsert_transactions(self.conn, batches.tag(self.conn, [
            base.Transaction(id="csv:card-old", account_id="filecard",
                             date=DAY - dt.timedelta(days=400), amount=7.0,
                             name="HARBOR BOOKS")], batch))
        batches.finish(self.conn, batch, 1)
        self.conn.execute(
            "UPDATE import_batches SET created_at = %s WHERE id = %s",
            (LOGGED - dt.timedelta(days=30), batch))
        self._log(f"pf-1 {DAY}; +200 more")
        self._upgrade()
        self.assertTrue(self._marked("csv:card-old"))
        self.assertTrue(self._marked("csv:span"))

    def test_a_partial_line_leaves_rows_imported_after_it(self):
        self._import([("csv:later", DAY - dt.timedelta(days=90))],
                     before_the_log=False)
        self._log(f"pf-1 {DAY}; +5 more")
        self._upgrade()
        self.assertFalse(self._marked("csv:later"))
        self.assertTrue(self._marked("csv:span"))

    def test_the_partial_note_names_no_account(self):
        self._log(f"pf-1 {DAY}; +5 more")
        self._upgrade()
        raw = self.conn.execute(
            "SELECT raw FROM transactions WHERE id = 'csv:span'").fetchone()
        self.assertEqual(raw["raw"][base.STOOD_IN_KEY],
                         {"source": "feed", "accounts": []})

    def test_with_no_feed_left_an_undo_lets_the_rows_go(self):
        # nothing can rely on a note that names no account once the
        # household has no bank feed that syncs
        self.conn.execute("UPDATE items SET status = 'archived'"
                          " WHERE id = 'feed'")
        self._log(f"pf-1 {DAY}; +5 more")
        self._upgrade()
        self.assertTrue(self._marked("csv:near"))
        res = batches.rollback(self.conn, self.batch)
        self.assertEqual(res["kept"], 0)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM transactions WHERE id = 'csv:near'").fetchone())

    def _archive_the_feed_keeping_its_account(self):
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype)"
            " VALUES ('feedchk', 'feed', 'Checking', 'depository',"
            " 'checking')")
        self.conn.execute("UPDATE items SET status = 'archived'"
                          " WHERE id = 'feed'")

    def _exists(self, tid):
        return self.conn.execute(
            "SELECT 1 FROM transactions WHERE id = %s",
            (tid,)).fetchone() is not None

    def test_an_archived_feed_whose_account_remains_still_holds_the_rows(self):
        # the archived account keeps its history, and a re-link adopts it
        # and skips what it is taken to hold: the set-aside charges are in
        # no copy but these
        self._archive_the_feed_keeping_its_account()
        self._log(f"pf-1 {DAY}; +5 more")
        self._upgrade()
        res = batches.rollback(self.conn, self.batch)
        self.assertEqual(res["kept"], 3)
        self.assertEqual(res["kept_without_account"], 3)
        for tid in ("csv:near", "csv:far", "csv:span"):
            self.assertTrue(self._exists(tid), tid)

    def test_rows_kept_without_an_account_stay_in_the_listed_import(self):
        self._log(f"pf-1 {DAY}; +5 more")
        self._upgrade()
        res = batches.rollback(self.conn, self.batch)
        self.assertEqual(res["kept_without_account"], 3)
        self.assertIn("3 rows were kept: an older bank feed may rely on them",
                      res["warning"])
        listed = {b["id"]: b for b in batches.recent(self.conn)}
        self.assertEqual(listed[self.batch]["row_count"], 3)
        # the owner can still choose to remove them later
        res = batches.rollback(self.conn, self.batch, force_kept=True)
        self.assertEqual(int(res), 3)
        self.assertFalse(self._exists("csv:near"))
        self.assertNotIn(self.batch,
                         {b["id"] for b in batches.recent(self.conn)})

    def test_asking_first_changes_nothing(self):
        self._log(f"pf-1 {DAY}; +5 more")
        self._upgrade()
        with self.assertRaises(batches.KeptRowsUnconfirmed) as cm:
            batches.rollback(self.conn, self.batch, ask_first=True)
        self.assertEqual(cm.exception.kept, 3)
        self.assertIn("3 imported rows", str(cm.exception))
        self.assertTrue(self._exists("csv:near"))
        self.assertIn(self.batch,
                      {b["id"] for b in batches.recent(self.conn)})

    def test_only_a_literal_true_forces(self):
        self._log(f"pf-1 {DAY}; +5 more")
        self._upgrade()
        res = batches.rollback(self.conn, self.batch, force_kept="yes")
        self.assertEqual(res["kept"], 3)

    def test_forcing_never_releases_a_row_a_named_feed_account_holds(self):
        self._archive_the_feed_keeping_its_account()
        self._log(f"pf-1 {DAY}; +5 more")
        self._upgrade()
        base.mark_stood_in(self.conn, {"csv:near": ["feedchk"]}, "plaid")
        res = batches.rollback(self.conn, self.batch, force_kept=True)
        self.assertEqual(res["kept"], 1)
        self.assertEqual(res["kept_without_account"], 0)
        self.assertTrue(self._exists("csv:near"))
        self.assertFalse(self._exists("csv:far"))

    def test_a_row_imported_after_the_line_is_left_alone(self):
        self._import([("csv:later", DAY + dt.timedelta(days=1))],
                     before_the_log=False)
        self._log(f"pf-1 {DAY}")
        self._upgrade()
        self.assertFalse(self._marked("csv:later"))
        self.assertTrue(self._marked("csv:near"))

    def test_a_line_written_after_the_feed_noted_rows_itself_is_not_read(self):
        self.conn.execute(
            "INSERT INTO sync_log (item_id, added, error)"
            " VALUES ('import-overlap', 0, %s)",
            (f"suppressed as covered by file-imported rows: pf-1 {DAY}",))
        self._upgrade()
        self.assertFalse(self._marked("csv:near"))

    def test_an_existing_note_is_left_as_it_is(self):
        base.mark_stood_in(self.conn, {"csv:near": ["chk"]}, "test")
        self._log(f"pf-1 {DAY}")
        self._upgrade()
        raw = self.conn.execute(
            "SELECT raw FROM transactions WHERE id = 'csv:near'").fetchone()
        self.assertEqual(raw["raw"][base.STOOD_IN_KEY],
                         {"source": "test", "accounts": ["chk"]})

    def test_the_upgrade_names_the_same_file_prefixes_as_the_sync(self):
        for path in (SQL, PARTIAL_SQL):
            named = re.findall(r"'([a-z]+:)%'", path.read_text())
            self.assertEqual(sorted(named),
                             sorted(base.FILE_IMPORT_ID_PREFIXES), path.name)


if __name__ == "__main__":
    unittest.main()
