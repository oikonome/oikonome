"""A restored OFX row gets the printed-day note only when the build that
wrote the archive dated rows by the printed day.

The note tells a later re-import that a row's date is already the day its
line printed, so the matcher leaves it where it is. Builds from the
printed-day dating change up to the note dated rows that way without
saying so, and a restore of their archive gives the note to the rows their
recent batches wrote. A build older than the dating change dated every
row by its UTC day, so its rows must stay without the note: noted, the next
overlapping re-import could not move them onto their printed day and would
double every evening charge instead. So the restore stamps them with the
UTC note, a positive fact a later import reads: their batch times are the
source's and cannot say which build wrote them. An archive with no
manifest and no column naming its age is read as the oldest. A row that
already carries a note keeps it, and the destination's own rows are never
touched."""

import io
import json
import unittest
import zipfile

from oikonome.sync import archive_upgrades, feed_dates, restore

from .util import make_db, write_config


def _cell(v) -> str:
    v = v if isinstance(v, str) else json.dumps(v)
    return '"' + v.replace('"', '""') + '"'


def _csv(header, rows) -> str:
    lines = [",".join(header)]
    for r in rows:
        lines.append(",".join("" if v is None else _cell(v) for v in r))
    return "\n".join(lines) + "\n"


_TXN = ["id", "account_id", "date", "amount", "name", "merchant_name",
        "category_primary", "category_detailed", "category_override",
        "pending", "removed", "raw"]


def _archive(schema: str | None, first_note: str | None = None) -> bytes:
    after = (restore._PRINTED_DAY_DATING_SINCE.replace(day=8)).isoformat()
    files = {
        "import_batches.csv": _csv(
            ["id", "source", "account_id", "filename", "row_count",
             "created_at"],
            [["b-late", "ofx", "chk", "card.ofx", "2", after]]),
        "transactions.csv": _csv(_TXN, [
            ["ofx:aa11", "chk", "2026-11-07", "-3.5", "TRANSIT FARE",
             "Transit", "TRANSPORTATION", None, None, 0, 0,
             {"_batch": "b-late", "_batches": ["b-late"],
              **({feed_dates.PRINTED_DAY_KEY: first_note}
                 if first_note else {})}],
            ["ofx:bb22", "chk", "2026-11-08", "-3.5", "TRANSIT FARE",
             "Transit", "TRANSPORTATION", None, None, 0, 0,
             {"_batch": "b-late", "_batches": ["b-late"]}]])}
    if schema:
        files[archive_upgrades.MANIFEST] = archive_upgrades.manifest(
            schema, "test", [])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


class PrintedDayNoteOnRestore(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _notes(self) -> dict:
        return {r["id"]: (r["raw"] or {}).get(feed_dates.PRINTED_DAY_KEY)
                for r in self.conn.execute(
                    "SELECT id, raw FROM transactions"
                    " WHERE id LIKE 'ofx:%'").fetchall()}

    def _restore(self, schema, **kw):
        restore.restore_zip(self.conn, _archive(schema, **kw))
        got = self._notes()
        self.assertEqual(len(got), 2, got)
        return got

    UTC, PRINTED = feed_dates.UTC_DAY_VALUE, feed_dates.PRINTED_DAY_VALUE

    def test_an_archive_from_before_printed_day_dating_says_utc(self):
        got = self._restore("209_card_numbers_in_import_file_names_are_cut.sql")
        self.assertEqual(set(got.values()), {self.UTC}, got)

    def test_an_archive_without_a_manifest_says_utc(self):
        got = self._restore(None)
        self.assertEqual(set(got.values()), {self.UTC}, got)

    def test_a_pre_dating_row_that_already_says_printed_keeps_it(self):
        got = self._restore(None, first_note=feed_dates.PRINTED_DAY_VALUE)
        self.assertEqual(got, {"ofx:aa11": self.PRINTED,
                               "ofx:bb22": self.UTC})

    def test_an_archive_that_dated_by_the_printed_day_gets_the_note(self):
        got = self._restore(
            "214_plaid_raw_keeps_no_account_or_person_identifiers.sql")
        self.assertEqual(set(got.values()), {self.PRINTED}, got)

    def test_the_first_build_that_dated_by_the_printed_day_counts(self):
        got = self._restore(restore._PRINTED_DAY_DATING)
        self.assertEqual(set(got.values()), {self.PRINTED}, got)

    def test_the_destinations_own_rows_are_not_stamped(self):
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name,"
            " raw) VALUES ('ofx:own1', 'chk', '2026-11-01', -2, 'OWN',"
            " '{}'::jsonb)")
        restore.restore_zip(self.conn, _archive(None))
        raw = self.conn.execute("SELECT raw FROM transactions"
                                " WHERE id = 'ofx:own1'").fetchone()["raw"]
        self.assertNotIn(feed_dates.PRINTED_DAY_KEY, raw)

    def test_a_printed_day_archive_leaves_the_households_own_rows_alone(self):
        """The instant the pass counts from is a fact about the archive's
        source. A row the household already had, on a batch after that
        instant, may have been dated by a build that upgraded later; it is
        not this restore's to note."""
        after = restore._PRINTED_DAY_DATING_SINCE.replace(day=8)
        self.conn.execute(
            "INSERT INTO import_batches (id, source, account_id, filename,"
            " row_count, created_at) VALUES ('b-own', 'ofx', 'chk',"
            " 'own.ofx', 1, %s)", (after,))
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name,"
            " raw) VALUES ('ofx:own1', 'chk', '2026-11-08', -2, 'OWN',"
            " '{\"_batch\": \"b-own\"}'::jsonb)")
        restore.restore_zip(self.conn, _archive(
            "214_plaid_raw_keeps_no_account_or_person_identifiers.sql"))
        got = self._notes()
        self.assertIsNone(got["ofx:own1"], got)
        self.assertEqual({got["ofx:aa11"], got["ofx:bb22"]}, {self.PRINTED})


if __name__ == "__main__":
    unittest.main()
