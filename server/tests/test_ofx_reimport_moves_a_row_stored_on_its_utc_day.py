"""A re-import moves a row an earlier build stored on its UTC day onto the
day the bank printed, and never mistakes another charge for that row.

An OFX stamp with a clock time and a zone (`20260310000000[+1:CET]`) was
once dated by the UTC day of that instant, so a statement imported then
holds some rows a day off from the day the same file yields now. Repeated
same-amount charges (a coffee every morning) cannot be told apart by the
restatement rule, which needs a pairing unique both ways, so without help
each such line became a second row beside its first.

The help is narrow on purpose. A FITID's stored row takes its line across
a day only when the stored date is exactly the UTC day of that line's own
stamp — the one date the earlier build could have written for it — and the
amount and name agree. Banks reuse FITIDs, so a FITID one day off for any
other reason (a different charge, a per-download numbering, a shifted
sequence) is still treated as a different charge.
"""

import datetime as dt
import unittest
from unittest import mock

from oikonome.db import migrate
from oikonome.sync import batches, feed_dates, ofximport, restore

from .test_imported_dates_are_the_day_the_source_printed import _line, _ofx
from .util import add_txn, make_db, write_config

BACKFILL = "220_ofx_rows_dated_by_the_printed_day_say_so.sql"

# a stamp at local midnight of a bank one hour east of Greenwich: its UTC
# instant falls on the previous day
EAST_MIDNIGHT = "000000[+1:CET]"
# a stamp at local noon of a bank west of Greenwich: same UTC day
WEST_NOON = "120000[-5:EST]"


class RowStoredOnItsUtcDay(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _rows(self):
        return {r["id"]: (r["date"], float(r["amount"]), r["name"])
                for r in self.conn.execute(
                    "SELECT id, date, amount, name FROM transactions "
                    "WHERE id LIKE 'ofx:%%' AND removed = 0").fetchall()}

    def _coffee_file(self):
        return _ofx(
            _line("T1", "20260310" + EAST_MIDNIGHT, "-5.00", "COFFEE CART")
            + _line("T2", "20260311" + EAST_MIDNIGHT, "-5.00", "COFFEE CART")
            + "".join(_line(f"U{i}", f"202603{19 + i}" + EAST_MIDNIGHT,
                            "-5.00", "COFFEE CART") for i in range(1, 5)))

    def _store_as_the_earlier_build_did(self):
        # each line's UTC day: the printed day minus one
        for fid, day in (("T1", "2026-03-09"), ("T2", "2026-03-10"),
                         ("U1", "2026-03-19"), ("U2", "2026-03-20"),
                         ("U3", "2026-03-21"), ("U4", "2026-03-22")):
            add_txn(self.conn, day, 5.00, "COFFEE CART", account="chk",
                    txn_id=f"ofx:chk:{fid}")

    def test_repeated_same_amount_charges_are_moved_to_their_printed_day(self):
        self._store_as_the_earlier_build_did()
        ofximport.import_ofx(self.conn, "chk", self._coffee_file())
        want = {f"ofx:chk:{fid}": (day, 5.0, "COFFEE CART") for fid, day in (
            ("T1", dt.date(2026, 3, 10)), ("T2", dt.date(2026, 3, 11)),
            ("U1", dt.date(2026, 3, 20)), ("U2", dt.date(2026, 3, 21)),
            ("U3", dt.date(2026, 3, 22)), ("U4", dt.date(2026, 3, 23)))}
        self.assertEqual(self._rows(), want)

    def test_importing_the_file_again_after_the_move_changes_nothing(self):
        self._store_as_the_earlier_build_did()
        ofximport.import_ofx(self.conn, "chk", self._coffee_file())
        before = self._rows()
        ofximport.import_ofx(self.conn, "chk", self._coffee_file())
        self.assertEqual(len(before), 6)
        self.assertEqual(self._rows(), before)

    def test_a_reused_fitid_for_another_charge_a_utc_day_off_is_not_taken(self):
        # the fare stored on the very day the parking stamp's UTC instant
        # falls on: same FITID, same amount, a different charge
        add_txn(self.conn, "2026-03-09", 3.00, "TRANSIT FARE",
                account="chk", txn_id="ofx:chk:F1")
        ofximport.import_ofx(self.conn, "chk", _ofx(
            _line("F1", "20260310" + EAST_MIDNIGHT, "-3.00", "PARKING")))
        self.assertEqual(sorted(self._rows().values()), [
            (dt.date(2026, 3, 9), 3.0, "TRANSIT FARE"),
            (dt.date(2026, 3, 10), 3.0, "PARKING")])

    def test_daily_fares_numbered_per_download_each_keep_their_row(self):
        ofximport.import_ofx(self.conn, "chk", _ofx(
            _line("1", "20260309", "-3.00", "TRANSIT FARE")
            + _line("2", "20260311", "-3.00", "TRANSIT FARE")))
        ofximport.import_ofx(self.conn, "chk", _ofx(
            _line("1", "20260310" + WEST_NOON, "-3.00", "TRANSIT FARE")
            + _line("2", "20260312" + WEST_NOON, "-3.00", "TRANSIT FARE")))
        self.assertEqual(sorted(d for d, _, _ in self._rows().values()), [
            dt.date(2026, 3, 9), dt.date(2026, 3, 10),
            dt.date(2026, 3, 11), dt.date(2026, 3, 12)])

    def test_a_shifted_sequence_does_not_move_a_row_onto_another_charge(self):
        ofximport.import_ofx(self.conn, "chk", _ofx(
            _line("1", "20260309", "-3.00", "TRANSIT FARE")
            + _line("2", "20260310", "-3.00", "TRANSIT FARE")))
        # the Mar 10 fare now on FITID 1, a new fare on FITID 2
        ofximport.import_ofx(self.conn, "chk", _ofx(
            _line("1", "20260310" + WEST_NOON, "-3.00", "TRANSIT FARE")
            + _line("2", "20260311" + WEST_NOON, "-3.00", "TRANSIT FARE")))
        self.assertEqual(sorted(d for d, _, _ in self._rows().values()), [
            dt.date(2026, 3, 9), dt.date(2026, 3, 10), dt.date(2026, 3, 11)])

    def test_a_stamp_without_a_zone_gets_no_drift(self):
        # rows a day before each line: with no zone the earlier build
        # dated these lines on their printed day too, so the stored rows
        # are other charges and stay where they are
        add_txn(self.conn, "2026-03-09", 5.00, "COFFEE CART", account="chk",
                txn_id="ofx:chk:T1")
        add_txn(self.conn, "2026-03-10", 5.00, "COFFEE CART", account="chk",
                txn_id="ofx:chk:T2")
        ofximport.import_ofx(self.conn, "chk", _ofx(
            _line("T1", "20260310", "-5.00", "COFFEE CART")
            + _line("T2", "20260311000000", "-5.00", "COFFEE CART")))
        rows = self._rows()
        self.assertEqual(rows["ofx:chk:T1"][0], dt.date(2026, 3, 9))
        self.assertEqual(sorted(d for d, _, _ in rows.values()), [
            dt.date(2026, 3, 9), dt.date(2026, 3, 10), dt.date(2026, 3, 11)])


class RowThisBuildDated(unittest.TestCase):
    """A row this build wrote carries the printed-day note in raw, and a
    re-import never moves it across a day by the UTC-day rule: daily fares
    of a bank whose stamps cross midnight UTC, numbered per download, look
    exactly like the earlier build's artifact one FITID over, and moving
    them would carry a charge's note, category and receipt onto its
    neighbour."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _rows(self):
        return {r["id"]: r["date"] for r in self.conn.execute(
            "SELECT id, date FROM transactions "
            "WHERE id LIKE 'ofx:%%' AND removed = 0").fetchall()}

    def _note(self, txn_id, text):
        self.conn.execute("INSERT INTO transaction_notes (txn_id, note) "
                          "VALUES (%s, %s)", (txn_id, text))

    def _noted_day(self, text):
        return self.conn.execute(
            "SELECT t.date FROM transaction_notes n JOIN transactions t "
            "ON t.id = n.txn_id WHERE n.note = %s", (text,)).fetchone()["date"]

    def _marked(self):
        return {r["id"]: (r["raw"] or {}).get(feed_dates.PRINTED_DAY_KEY)
                == feed_dates.PRINTED_DAY_VALUE for r in self.conn.execute(
                    "SELECT id, raw FROM transactions "
                    "WHERE id LIKE 'ofx:%%'").fetchall()}

    def _fares(self, *days):
        return _ofx("".join(
            _line(str(i), f"202603{d:02d}" + EAST_MIDNIGHT, "-3.00",
                  "TRANSIT FARE") for i, d in enumerate(days, 1)))

    def test_a_shifted_download_of_fares_this_build_stored_moves_no_row(self):
        ofximport.import_ofx(self.conn, "chk", self._fares(9, 10))
        self._note("ofx:chk:1", "monday fare")
        # next download: the Mar 10 fare is now FITID 1, a new one FITID 2
        ofximport.import_ofx(self.conn, "chk", self._fares(10, 11))
        rows = self._rows()
        self.assertEqual(rows["ofx:chk:1"], dt.date(2026, 3, 9))
        self.assertEqual(rows["ofx:chk:2"], dt.date(2026, 3, 10))
        self.assertEqual(sorted(rows.values()), [
            dt.date(2026, 3, 9), dt.date(2026, 3, 10), dt.date(2026, 3, 11)])
        self.assertEqual(self._noted_day("monday fare"), dt.date(2026, 3, 9))

    def test_an_earlier_builds_utc_day_row_is_moved_once_then_stays_put(self):
        # rows the build BEFORE printed-day dating stored on UTC days: no
        # printed-day note, and no import batch at or after the dating
        # change to earn one (see PrintedDayNoteBackfill for the rows the
        # builds after it wrote without the note)
        add_txn(self.conn, "2026-03-09", 3.00, "TRANSIT FARE", account="chk",
                txn_id="ofx:chk:1")
        add_txn(self.conn, "2026-03-10", 3.00, "TRANSIT FARE", account="chk",
                txn_id="ofx:chk:2")
        self._note("ofx:chk:1", "monday fare")
        self.assertEqual(self._marked(), {"ofx:chk:1": False,
                                          "ofx:chk:2": False})
        ofximport.import_ofx(self.conn, "chk", self._fares(10, 11))
        self.assertEqual(self._rows(), {"ofx:chk:1": dt.date(2026, 3, 10),
                                        "ofx:chk:2": dt.date(2026, 3, 11)})
        self.assertEqual(self._marked(), {"ofx:chk:1": True,
                                          "ofx:chk:2": True})
        # a later download shifted by one: the moved rows are this
        # build's now, and neither moves again
        ofximport.import_ofx(self.conn, "chk", self._fares(11, 12))
        rows = self._rows()
        self.assertEqual(rows["ofx:chk:1"], dt.date(2026, 3, 10))
        self.assertEqual(rows["ofx:chk:2"], dt.date(2026, 3, 11))
        self.assertEqual(sorted(rows.values()), [
            dt.date(2026, 3, 10), dt.date(2026, 3, 11), dt.date(2026, 3, 12)])
        self.assertEqual(self._noted_day("monday fare"), dt.date(2026, 3, 10))

    def test_the_note_survives_a_restore(self):
        ofximport.import_ofx(self.conn, "chk", self._fares(9))
        raw = self.conn.execute("SELECT raw FROM transactions "
                                "WHERE id = 'ofx:chk:1'").fetchone()["raw"]
        self.assertEqual(
            restore.scrub_transaction_raw(dict(raw)).get(
                feed_dates.PRINTED_DAY_KEY), feed_dates.PRINTED_DAY_VALUE)


class PrintedDayNoteBackfill(unittest.TestCase):
    """Rows the builds between the printed-day dating change and the note
    were written on their printed days but carry no note, so step 1b would
    take them for the earlier build's artifact. The backfill gives the note
    to every OFX row whose latest import batch began at or after the
    dating change; a row with an older batch, or none, is left as it is."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.cutoff = dt.datetime(2026, 3, 1, 12, tzinfo=dt.timezone.utc)

    def tearDown(self):
        self.conn.close()

    def _import(self, data, created_at):
        bid = batches.create(self.conn, "ofx", "chk", "statement.ofx")
        self.conn.execute("UPDATE import_batches SET created_at = %s "
                          "WHERE id = %s", (created_at, bid))
        ofximport.import_ofx(self.conn, "chk", data, batch_id=bid)
        return bid

    def _strip_notes(self):
        # what the builds between the dating change and the note wrote
        self.conn.execute("UPDATE transactions SET raw = raw - %s "
                          "WHERE id LIKE 'ofx:%%'",
                          (feed_dates.PRINTED_DAY_KEY,))

    _rows = RowThisBuildDated._rows
    _note = RowThisBuildDated._note
    _noted_day = RowThisBuildDated._noted_day
    _marked = RowThisBuildDated._marked
    _fares = RowThisBuildDated._fares

    def test_rows_written_after_the_change_get_the_note_and_never_move(self):
        self._import(self._fares(9, 10), self.cutoff + dt.timedelta(hours=1))
        self._strip_notes()
        self._note("ofx:chk:1", "monday fare")
        self.assertEqual(
            ofximport.mark_printed_day_rows(self.conn, self.cutoff), 2)
        self.assertEqual(self._marked(), {"ofx:chk:1": True,
                                          "ofx:chk:2": True})
        # next download shifted by one: nothing moves, nothing is lost,
        # the new fare is added
        self._import(self._fares(10, 11), self.cutoff + dt.timedelta(days=1))
        rows = self._rows()
        self.assertEqual(rows["ofx:chk:1"], dt.date(2026, 3, 9))
        self.assertEqual(rows["ofx:chk:2"], dt.date(2026, 3, 10))
        self.assertEqual(sorted(rows.values()), [
            dt.date(2026, 3, 9), dt.date(2026, 3, 10), dt.date(2026, 3, 11)])
        self.assertEqual(self._noted_day("monday fare"), dt.date(2026, 3, 9))

    def test_rows_written_before_the_change_stay_eligible_to_move(self):
        add_txn(self.conn, "2026-03-09", 3.00, "TRANSIT FARE", account="chk",
                txn_id="ofx:chk:1")
        add_txn(self.conn, "2026-03-10", 3.00, "TRANSIT FARE", account="chk",
                txn_id="ofx:chk:2")
        old = batches.create(self.conn, "ofx", "chk", "statement.ofx")
        self.conn.execute("UPDATE import_batches SET created_at = %s "
                          "WHERE id = %s",
                          (self.cutoff - dt.timedelta(days=30), old))
        self.conn.execute("UPDATE transactions SET raw = jsonb_build_object("
                          "'_batch', %s::text, '_batches', jsonb_build_array("
                          "%s::text)) WHERE id = 'ofx:chk:1'", (old, old))
        # ofx:chk:2 has no batch at all: no time to read, left unmarked
        self.assertEqual(
            ofximport.mark_printed_day_rows(self.conn, self.cutoff), 0)
        self.assertEqual(self._marked(), {"ofx:chk:1": False,
                                          "ofx:chk:2": False})
        self._import(self._fares(10, 11), self.cutoff + dt.timedelta(days=1))
        self.assertEqual(self._rows(), {"ofx:chk:1": dt.date(2026, 3, 10),
                                        "ofx:chk:2": dt.date(2026, 3, 11)})

    def test_a_second_run_writes_nothing(self):
        self._import(self._fares(9, 10), self.cutoff + dt.timedelta(hours=1))
        self._strip_notes()
        self.assertEqual(
            ofximport.mark_printed_day_rows(self.conn, self.cutoff), 2)
        self.assertEqual(
            ofximport.mark_printed_day_rows(self.conn, self.cutoff), 0)

    def test_the_migration_step_reads_the_dating_change_from_the_ledger(self):
        # this test database was built fresh, so the dating change's
        # migration ran before any of its rows: every batched row is marked
        ran = migrate.printed_day_dating_began(self.conn)
        self.assertIsNotNone(ran)
        self._import(self._fares(9, 10), ran + dt.timedelta(seconds=1))
        self._strip_notes()
        migrate.PYTHON_STEPS[BACKFILL](self.conn)
        self.assertEqual(self._marked(), {"ofx:chk:1": True,
                                          "ofx:chk:2": True})
        self.assertEqual(
            ofximport.mark_printed_day_rows(self.conn, ran), 0)


class EachImportNotesItsHouseholdFirst(unittest.TestCase):
    """The released build kept serving while migration 220 ran (the old
    app and worker are replaced after the one-shot migrate), so it can
    commit printed-day rows with a recent batch and no note after the
    backfill's snapshot. Every OFX import therefore notes its own
    household's such rows before it matches: the matcher never rests on
    the backfill having seen every row."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.began = feed_dates.printed_day_dating_began(self.conn)
        self.assertIsNotNone(self.began)
        self.noted: list[int] = []
        real = ofximport.mark_printed_day_rows

        def spy(conn, since):
            n = real(conn, since)
            self.noted.append(n)
            return n
        patcher = mock.patch.object(ofximport, "mark_printed_day_rows", spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.conn.close()

    _import = PrintedDayNoteBackfill._import
    _strip_notes = PrintedDayNoteBackfill._strip_notes
    _rows = RowThisBuildDated._rows
    _note = RowThisBuildDated._note
    _noted_day = RowThisBuildDated._noted_day
    _marked = RowThisBuildDated._marked
    _fares = RowThisBuildDated._fares

    def test_rows_the_old_app_wrote_after_the_backfill_are_noted_first(self):
        self._import(self._fares(9, 10), self.began + dt.timedelta(hours=1))
        self._strip_notes()              # the released build wrote these
        self._note("ofx:chk:1", "monday fare")
        self.noted.clear()
        self._import(self._fares(10, 11), self.began + dt.timedelta(days=1))
        self.assertEqual(self.noted, [2])
        rows = self._rows()
        self.assertEqual(rows["ofx:chk:1"], dt.date(2026, 3, 9))
        self.assertEqual(rows["ofx:chk:2"], dt.date(2026, 3, 10))
        self.assertEqual(sorted(rows.values()), [
            dt.date(2026, 3, 9), dt.date(2026, 3, 10), dt.date(2026, 3, 11)])
        self.assertEqual(self._noted_day("monday fare"), dt.date(2026, 3, 9))

    def test_a_household_with_nothing_to_note_has_nothing_written(self):
        self._import(self._fares(9, 10), self.began + dt.timedelta(hours=1))
        self.noted.clear()
        self._import(self._fares(9, 10), self.began + dt.timedelta(days=1))
        self.assertEqual(self.noted, [0])

    def test_rows_from_before_the_change_are_not_noted_and_still_move(self):
        add_txn(self.conn, "2026-03-09", 3.00, "TRANSIT FARE", account="chk",
                txn_id="ofx:chk:1")
        add_txn(self.conn, "2026-03-10", 3.00, "TRANSIT FARE", account="chk",
                txn_id="ofx:chk:2")
        old = batches.create(self.conn, "ofx", "chk", "statement.ofx")
        self.conn.execute("UPDATE import_batches SET created_at = %s "
                          "WHERE id = %s",
                          (self.began - dt.timedelta(days=30), old))
        self.conn.execute("UPDATE transactions SET raw = jsonb_build_object("
                          "'_batch', %s::text) WHERE id LIKE 'ofx:%%'", (old,))
        self.noted.clear()
        self._import(self._fares(10, 11), self.began + dt.timedelta(days=1))
        self.assertEqual(self.noted, [0])
        self.assertEqual(self._rows(), {"ofx:chk:1": dt.date(2026, 3, 10),
                                        "ofx:chk:2": dt.date(2026, 3, 11)})


class RestoredUtcDayRows(unittest.TestCase):
    """A restore of an archive from before printed-day dating notes its OFX
    rows "utc": a positive fact that they sit on their UTC day. Their
    restored batches keep the source's time (or the restore's), which says
    nothing about which build wrote them, so neither the backfill nor an
    import's note pass may overwrite that note, and step 1b still moves
    such a row onto its printed day — noting it "printed" as it does."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    _rows = RowThisBuildDated._rows

    def _notes(self):
        return {r["id"]: r["n"] for r in self.conn.execute(
            "SELECT id, raw ->> %s AS n FROM transactions "
            "WHERE id LIKE 'ofx:%%'", (feed_dates.PRINTED_DAY_KEY,))}

    def _restored(self, fid, day):
        # as a restore of a pre-change archive leaves the row: on its UTC
        # day, batch created now, noted "utc"
        bid = batches.create(self.conn, "ofx", "chk", "restored.ofx")
        add_txn(self.conn, day, 5.00, "COFFEE CART", account="chk",
                txn_id=f"ofx:chk:{fid}")
        self.conn.execute(
            "UPDATE transactions SET raw = jsonb_build_object("
            "'_batch', %s::text, '_batches', jsonb_build_array(%s::text), "
            "%s::text, %s::text) WHERE id = %s",
            (bid, bid, feed_dates.PRINTED_DAY_KEY, feed_dates.UTC_DAY_VALUE,
             f"ofx:chk:{fid}"))

    def test_restored_utc_rows_are_moved_not_doubled_by_the_next_import(self):
        days = (("T1", "2026-03-09"), ("T2", "2026-03-10"),
                ("U1", "2026-03-19"), ("U2", "2026-03-20"),
                ("U3", "2026-03-21"), ("U4", "2026-03-22"))
        for fid, day in days:
            self._restored(fid, day)
        bid = batches.create(self.conn, "ofx", "chk", "statement.ofx")
        ofximport.import_ofx(self.conn, "chk",
                             RowStoredOnItsUtcDay._coffee_file(None),
                             batch_id=bid)
        rows = self._rows()
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows, {f"ofx:chk:{fid}": day for fid, day in (
            ("T1", dt.date(2026, 3, 10)), ("T2", dt.date(2026, 3, 11)),
            ("U1", dt.date(2026, 3, 20)), ("U2", dt.date(2026, 3, 21)),
            ("U3", dt.date(2026, 3, 22)), ("U4", dt.date(2026, 3, 23)))})
        self.assertEqual(set(self._notes().values()),
                         {feed_dates.PRINTED_DAY_VALUE})

    def test_a_utc_note_is_never_overwritten_by_the_note_pass(self):
        self._restored("T1", "2026-03-09")
        since = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
        self.assertEqual(ofximport.mark_printed_day_rows(self.conn, since), 0)
        self.assertEqual(self._notes(),
                         {"ofx:chk:T1": feed_dates.UTC_DAY_VALUE})

    def test_an_un_noted_recent_row_is_still_noted_printed(self):
        self._restored("T1", "2026-03-09")
        bid = batches.create(self.conn, "ofx", "chk", "statement.ofx")
        add_txn(self.conn, "2026-03-12", 5.00, "COFFEE CART", account="chk",
                txn_id="ofx:chk:T9")
        self.conn.execute("UPDATE transactions SET raw = jsonb_build_object("
                          "'_batch', %s::text) WHERE id = 'ofx:chk:T9'",
                          (bid,))
        since = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
        self.assertEqual(ofximport.mark_printed_day_rows(self.conn, since), 1)
        self.assertEqual(self._notes(), {
            "ofx:chk:T1": feed_dates.UTC_DAY_VALUE,
            "ofx:chk:T9": feed_dates.PRINTED_DAY_VALUE})


class UtcDayOfEachLine(unittest.TestCase):
    def _utc_days(self, *stamps):
        rows = ofximport.parse_ofx(_ofx("".join(
            _line(f"S{i}", s) for i, s in enumerate(stamps))))
        return [r["utc_day"] for r in rows]

    def test_only_a_zoned_stamp_whose_utc_day_differs_carries_one(self):
        self.assertEqual(self._utc_days(
            "20260310" + EAST_MIDNIGHT, "20260331210000.000[-5:EST]",
            "20260310" + WEST_NOON, "20260310", "20260310000000"), [
            dt.date(2026, 3, 9), dt.date(2026, 4, 1), None, None, None])

    def test_the_utc_day_survives_the_parse_child(self):
        self.assertFalse(ofximport.IN_PROCESS)
        self.assertEqual(self._utc_days("20260310" + EAST_MIDNIGHT),
                         [dt.date(2026, 3, 9)])


if __name__ == "__main__":
    unittest.main()
