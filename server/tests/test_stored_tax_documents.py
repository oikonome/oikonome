"""Every stored tax and income document is listed, and can be removed.

Each committed document is an upload with, per year, the columns it wrote
(income_uploads / income_upload_values); each stored yearly figure is the
value the newest live upload wrote (taxdocs.resolve_year). Removing one
hands each column it wrote to the next older document that wrote it, or
empties it, and gives a wage figure back to the W-2s; it can be put back
for 30 days. Figures stored before the list are given uploads (migration
231, or taxdocs.backfill_household on an older archive) that resolve to
exactly what is stored. All figures here are invented.
Spec: specs/tax-documents.md, "Stored documents and what each one wrote".
"""

import csv
import io
import json
import pathlib
import unittest
import uuid
import zipfile
from unittest import mock

from oikonome.db import staging
from oikonome.engine import taxdocs
from oikonome.sync import batches, export, restore

from .util import make_db, write_config

COLS = ("year", "total_income", "agi", "taxable_income", "tax_paid", "wages",
        "primary_wages", "primary_wages_w2", "spouse_wages", "spouse_wages_w2",
        "investment_income", "capital_gain", "ss_earnings",
        "medicare_earnings")
MIGRATION = (pathlib.Path(taxdocs.__file__).resolve().parent.parent / "db"
             / "migrations"
             / "231_a_stored_tax_document_remembers_what_it_wrote.sql")


def _annual(conn):
    return {r["year"]: {c: r[c] for c in COLS} for r in conn.execute(
        f"SELECT {', '.join(COLS)} FROM income_annual ORDER BY year")}


def _store(conn, kind, rows, filename="invented.pdf"):
    token = staging.put(conn, "taxdoc", meta={"kind": kind, "rows": rows,
                                              "filename": filename})
    return taxdocs.commit(conn, token, rows)


def _upload(conn, kind):
    return next(d for d in taxdocs.list_stored(conn) if d["kind"] == kind)


def _remove(conn, doc):
    return taxdocs.remove_upload(conn, doc["id"], doc["seen"])


def _legacy_household(conn):
    """A household as an older build stored it: figures merged into
    income_annual with a free-text source, no uploads."""
    x = conn.execute
    x("""INSERT INTO income_annual (year, ss_earnings, medicare_earnings,
                                    source) VALUES
         (2001, 1000, 1100, 'SSA earnings record'),
         (2002, 2000, 2100, 'SSA earnings record')""")
    x("""INSERT INTO income_annual (year, total_income, agi, wages,
            ss_earnings, medicare_earnings, source) VALUES
         (2019, 50000, 48000, 45000, 44000, 45000,
          'invented_2019.pdf.txt;SSA')""")
    # a wage figure the W-2s wrote, and one a return wrote
    x("""INSERT INTO income_annual (year, primary_wages, primary_wages_w2,
            source) VALUES (2020, 30000, 30000, 'w2')""")
    x("""INSERT INTO income_annual (year, wages, primary_wages,
            primary_wages_w2, source)
         VALUES (2021, 40000, 41000, 39000, 'transcript+w2')""")
    x("""INSERT INTO income_documents (id, year, form, owner, payer,
            primary_amount) VALUES
         (1, 2020, 'W-2', 'primary', 'Invented Works', 30000),
         (2, 2021, 'W-2', 'primary', 'Invented Works', 39000),
         (3, 2021, '1099-INT', 'primary', 'INVT', 12.5)""")


def _run_migration_backfill(conn):
    sql = MIGRATION.read_text()
    part = sql[sql.index("CREATE TEMP TABLE _bf_tenants"):]
    with conn.transaction():
        conn.execute(part)


class StoredDocuments(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    # ---- the backfill ----------------------------------------------------

    def test_backfill_resolves_to_every_stored_figure(self):
        _legacy_household(self.conn)
        before = _annual(self.conn)
        _run_migration_backfill(self.conn)
        for year in before:
            taxdocs.resolve_year(self.conn, year)
        self.assertEqual(_annual(self.conn), before)
        kinds = sorted(d["kind"] for d in taxdocs.list_stored(self.conn))
        self.assertEqual(kinds, ["document", "legacy_return",
                                 "legacy_return", "legacy_ssa", "w2", "w2"])
        ret = next(d for d in taxdocs.list_stored(self.conn)
                   if d["kind"] == "legacy_return" and d["years"] == [2019])
        # the old source text names no file; the row says what it is
        self.assertIsNone(ret["filename"])
        self.assertEqual(ret["stored_label"], "Stored earlier, date not kept")

    def test_the_restore_backfill_gives_the_same_uploads_as_the_migration(self):
        other = make_db()
        try:
            for c in (self.conn, other):
                _legacy_household(c)
            _run_migration_backfill(self.conn)
            taxdocs.backfill_household(other)

            def shape(c):
                return (
                    [tuple(r.values()) for r in c.execute(
                        "SELECT id, kind, filename, stored_at "
                        "FROM income_uploads ORDER BY id")],
                    [tuple(r.values()) for r in c.execute(
                        "SELECT upload_id, year, fields FROM "
                        "income_upload_values ORDER BY upload_id, year")],
                    [tuple(r.values()) for r in c.execute(
                        "SELECT id, upload_id FROM income_documents "
                        "ORDER BY id")])
            self.assertEqual(shape(other), shape(self.conn))
        finally:
            other.close()

    def _shape(self, c):
        return (
            [tuple(r.values()) for r in c.execute(
                "SELECT id, uid, kind, filename, stored_at, removed_at "
                "FROM income_uploads ORDER BY id")],
            [tuple(r.values()) for r in c.execute(
                "SELECT upload_id, year, fields FROM "
                "income_upload_values ORDER BY upload_id, year")],
            [tuple(r.values()) for r in c.execute(
                "SELECT id, upload_id FROM income_documents ORDER BY id")],
            _annual(c))

    def test_the_backfill_run_again_changes_nothing(self):
        """Migration 231's backfill, run a second time on the same
        database, and the restore's twin of it run after the migration,
        add no upload and move no figure."""
        _legacy_household(self.conn)
        _run_migration_backfill(self.conn)
        once = self._shape(self.conn)
        _run_migration_backfill(self.conn)
        self.assertEqual(self._shape(self.conn), once)
        taxdocs.backfill_household(self.conn)
        self.assertEqual(self._shape(self.conn), once)
        # and the nightly adoption over every year finds nothing new
        self.assertEqual(taxdocs.adopt_household(self.conn), 0)
        self.assertEqual(self._shape(self.conn), once)

    def test_a_half_backfilled_household_gets_each_loose_form_once(self):
        """A household that already holds uploads (a newer build stored
        one) is left alone by the migration run again; a form stored
        without an upload is given exactly one, however often the twin
        runs."""
        _store(self.conn, "1040", [{"year": 2022, "agi": 10.0}])
        self.conn.execute(
            "INSERT INTO income_documents (id, year, form, owner, payer, "
            "primary_amount) VALUES (70, 2022, '1099-INT', 'primary', "
            "'Invented Bank', 5)")
        before = self._shape(self.conn)
        _run_migration_backfill(self.conn)
        self.assertEqual(self._shape(self.conn), before)
        taxdocs.backfill_household(self.conn)
        once = self._shape(self.conn)
        loose = self.conn.execute(
            "SELECT upload_id FROM income_documents WHERE id = 70"
            ).fetchone()["upload_id"]
        self.assertIsNotNone(loose)
        self.assertEqual(len(once[0]), len(before[0]) + 1)
        taxdocs.backfill_household(self.conn)
        _run_migration_backfill(self.conn)
        self.assertEqual(self._shape(self.conn), once)

    # ---- removal -----------------------------------------------------------

    def test_removing_transcript_keeps_ssa_columns_of_merged_year(self):
        _store(self.conn, "ssa", [{"year": 2019, "ss_earnings": 44000.0,
                                   "medicare_earnings": 45000.0}])
        _store(self.conn, "transcript", [{"year": 2019, "agi": 48000.0,
                                          "wages": 45000.0}])
        _remove(self.conn, _upload(self.conn, "transcript"))
        row = _annual(self.conn)[2019]
        self.assertEqual((row["ss_earnings"], row["medicare_earnings"]),
                         (44000.0, 45000.0))
        self.assertIsNone(row["agi"])
        self.assertIsNone(row["wages"])

    def test_removing_ssa_record_empties_career_years_it_alone_gave(self):
        _store(self.conn, "ssa", [
            {"year": y, "ss_earnings": 1000.0 * (y - 1999),
             "medicare_earnings": 1000.0 * (y - 1999)}
            for y in (2000, 2001, 2002, 2019)])
        _store(self.conn, "transcript", [{"year": 2019, "wages": 45000.0}])
        ssa = _upload(self.conn, "ssa")
        self.assertIn("Career earnings loses 2000–2002",
                      taxdocs.removal_effect(self.conn, ssa["id"]))
        _remove(self.conn, ssa)
        self.assertEqual(sorted(_annual(self.conn)), [2019])
        self.assertEqual(_annual(self.conn)[2019]["wages"], 45000.0)

    def test_removing_newer_return_reverts_column_to_older_return(self):
        _store(self.conn, "transcript", [{"year": 2019, "agi": 100.0}])
        _store(self.conn, "1040", [{"year": 2019, "agi": 200.0}])
        self.assertEqual(_annual(self.conn)[2019]["agi"], 200.0)
        _remove(self.conn, _upload(self.conn, "1040"))
        self.assertEqual(_annual(self.conn)[2019]["agi"], 100.0)

    def test_wage_figure_returns_to_w2_sum_when_return_removed(self):
        _store(self.conn, "w2", [{"year": 2022, "owner": "primary",
                                  "employer": "Invented Works",
                                  "boxes": {"1": 50000.0}}])
        self.assertEqual(_annual(self.conn)[2022]["primary_wages"], 50000.0)
        _store(self.conn, "1040", [{"year": 2022, "primary_wages": 60000.0}])
        self.assertEqual(_annual(self.conn)[2022]["primary_wages"], 60000.0)
        _remove(self.conn, _upload(self.conn, "1040"))
        self.assertEqual(_annual(self.conn)[2022]["primary_wages"], 50000.0)

    def test_removed_document_undo_restores_figures_exactly(self):
        _store(self.conn, "ssa", [{"year": 2019, "ss_earnings": 44000.0}])
        _store(self.conn, "w2", [{"year": 2019, "owner": "primary",
                                  "employer": "Invented Works",
                                  "boxes": {"1": 45000.0}}])
        before = _annual(self.conn)
        docs_before = self.conn.execute(
            "SELECT year, form, owner, payer, primary_amount "
            "FROM income_documents").fetchall()
        for kind in ("w2", "ssa"):
            doc = _upload(self.conn, kind)
            _remove(self.conn, doc)
            gone = _upload(self.conn, kind)
            self.assertTrue(gone["restorable"])
            self.assertIn("can be put back until", gone["removed_label"])
            self.assertEqual(taxdocs.restore_upload(
                self.conn, gone["id"], gone["seen"]), [2019])
        self.assertEqual(_annual(self.conn), before)
        self.assertEqual(self.conn.execute(
            "SELECT year, form, owner, payer, primary_amount "
            "FROM income_documents").fetchall(), docs_before)

    def test_stale_list_removal_is_refused(self):
        _store(self.conn, "transcript", [{"year": 2019, "agi": 100.0}])
        doc = _upload(self.conn, "transcript")
        stale = dict(doc["seen"], stored_at="2001-01-01T00:00:00+00:00")
        with self.assertRaises(taxdocs.UploadChanged):
            taxdocs.remove_upload(self.conn, doc["id"], stale)
        self.assertEqual(_annual(self.conn)[2019]["agi"], 100.0)
        _remove(self.conn, doc)
        # removed already: a second removal from the same list finds none
        self.assertIsNone(_remove(self.conn, doc))

    def test_a_removed_document_is_purged_after_the_undo_window(self):
        _store(self.conn, "transcript", [{"year": 2019, "agi": 100.0}])
        _remove(self.conn, _upload(self.conn, "transcript"))
        self.assertEqual(taxdocs.purge_removed(self.conn), 0)
        self.conn.execute("UPDATE income_uploads SET removed_at = now() - "
                          "interval '31 days'")
        self.assertEqual(taxdocs.purge_removed(self.conn), 1)
        self.assertEqual(taxdocs.list_stored(self.conn), [])

    def _years_move_while_locks_are_waited_for(self, upload_id):
        """The upload reads as covering 2018 alone until the year locks
        are asked for; by the time they are held it covers 2019 too."""
        self.conn.execute("UPDATE income_upload_values SET fields = '{}' "
                          "WHERE upload_id = %s AND year = 2019",
                          (upload_id,))
        real = taxdocs._lock_years

        def moved(conn, years, bounded):
            conn.execute("UPDATE income_upload_values SET fields = %s "
                         "WHERE upload_id = %s AND year = 2019",
                         ('{"agi": 2000.0}', upload_id))
            return real(conn, years, bounded)
        return mock.patch.object(taxdocs, "_lock_years", moved)

    def test_an_effect_whose_years_moved_before_the_locks_says_it_changed(self):
        _store(self.conn, "1040", [{"year": 2018, "agi": 1000.0},
                                   {"year": 2019, "agi": 2000.0}])
        doc = _upload(self.conn, "1040")
        with self._years_move_while_locks_are_waited_for(doc["id"]):
            with self.assertRaises(taxdocs.UploadChanged):
                taxdocs.removal_effect(self.conn, doc["id"])

    def test_a_purge_whose_years_moved_before_the_locks_waits_a_night(self):
        _store(self.conn, "1040", [{"year": 2018, "agi": 1000.0},
                                   {"year": 2019, "agi": 2000.0}])
        _remove(self.conn, _upload(self.conn, "1040"))
        self.conn.execute("UPDATE income_uploads SET removed_at = now() - "
                          "interval '31 days'")
        uid = self.conn.execute("SELECT id FROM income_uploads"
                                ).fetchone()["id"]
        with self._years_move_while_locks_are_waited_for(uid):
            self.assertEqual(taxdocs.purge_removed(self.conn), 0)
        self.assertEqual(taxdocs.purge_removed(self.conn), 1)

    # ---- the list's words ------------------------------------------------

    def test_transcript_stub_shows_name_from_same_ein(self):
        x = self.conn.execute
        x("""INSERT INTO income_documents (id, year, form, payer, ein_last4,
                 primary_amount) VALUES
             (10, 2021, '1099-INT', 'INVT BK', '1234', 10),
             (11, 2022, '1099-INT', 'INVT (Invented Bank)', '1234', 12),
             (12, 2022, '1099-DIV', 'ZZQ', '9876', 3)""")
        taxdocs.backfill_household(self.conn)
        by = {d["years"][0] if d["kind"] == "document" else None: d
              for d in taxdocs.list_stored(self.conn)
              if d["kind_label"] == "1099-INT"}
        self.assertEqual(by[2021]["employer_shown"], "Invented Bank")
        self.assertFalse(by[2021]["abbreviated"])
        div = next(d for d in taxdocs.list_stored(self.conn)
                   if d["kind_label"] == "1099-DIV")
        self.assertEqual((div["employer_shown"], div["abbreviated"]),
                         ("ZZQ", True))
        groups = taxdocs.stored_groups(taxdocs.list_stored(self.conn))
        self.assertEqual(groups, [{"key": "other", "label": "Other forms (3)",
                                   "count": 3, "collapsed": True}])

    def test_effect_line_names_no_unread_figure(self):
        self.conn.execute("""INSERT INTO income_documents
            (id, year, form, payer, primary_amount)
            VALUES (20, 2021, '5498', 'Invented Trust', 100)""")
        taxdocs.backfill_household(self.conn)
        form = _upload(self.conn, "document")
        line = taxdocs.removal_effect(self.conn, form["id"])
        self.assertEqual(
            line, "No figure changes: nothing reads this document's numbers.")
        _store(self.conn, "ssa", [{"year": 2019, "ss_earnings": 1.0}])
        line = taxdocs.removal_effect(self.conn,
                                      _upload(self.conn, "ssa")["id"])
        self.assertNotIn("etirement", line)
        # the effect is worked out in a savepoint: nothing moved
        self.assertEqual(_annual(self.conn)[2019]["ss_earnings"], 1.0)

    def test_the_effect_is_this_removals_alone(self):
        """A store that lands while the effect waits on the year locks is
        not reported as what removing this document does: the figures are
        read after the locks, and only over the years this document holds."""
        import os
        from unittest import mock

        from oikonome.db import tenancy

        from .util import TEST_DB
        _store(self.conn, "ssa", [{"year": 2016, "ss_earnings": 3000.0,
                                   "medicare_earnings": 3000.0}])
        _store(self.conn, "transcript", [
            {"year": 2017, "agi": 6000.0, "wages": 6000.0}])
        ssa = _upload(self.conn, "ssa")
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        other = tenancy.tenant_connect(tid, os.environ.get(
            "OIKONOME_TEST_DSN",
            f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}"))
        self.addCleanup(other.close)
        real = taxdocs._lock_years
        landed = []

        def lock_after_another_store(conn, years, bounded):
            # another member's store commits while this one waits
            if conn is self.conn and not landed:
                landed.append(True)
                _store(other, "transcript", [
                    {"year": 2017, "agi": 7000.0, "wages": 7000.0}])
            return real(conn, years, bounded)
        with mock.patch.object(taxdocs, "_lock_years",
                               lock_after_another_store):
            line = taxdocs.removal_effect(self.conn, ssa["id"])
        self.assertTrue(landed)
        self.assertIn("2016", line)
        self.assertNotIn("2017", line)

    def test_an_effect_of_a_document_removed_meanwhile_says_it_changed(self):
        import os
        from unittest import mock

        from oikonome.db import tenancy

        from .util import TEST_DB
        _store(self.conn, "ssa", [{"year": 2016, "ss_earnings": 3000.0}])
        ssa = _upload(self.conn, "ssa")
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        other = tenancy.tenant_connect(tid, os.environ.get(
            "OIKONOME_TEST_DSN",
            f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}"))
        self.addCleanup(other.close)
        real = taxdocs._lock_years

        def lock_after_another_removal(conn, years, bounded):
            if conn is self.conn:
                _remove(other, ssa)
            return real(conn, years, bounded)
        with mock.patch.object(taxdocs, "_lock_years",
                               lock_after_another_removal):
            with self.assertRaises(taxdocs.UploadChanged):
                taxdocs.removal_effect(self.conn, ssa["id"])

    def test_removing_a_w2_a_return_stands_in_for_says_so(self):
        """A W-2 whose wages a return's figure owns changes nothing when
        it goes — but it is what backs the wages once the return goes,
        so "nothing reads this document's numbers" would be untrue."""
        _store(self.conn, "w2", [{"year": 2022, "owner": "primary",
                                  "employer": "Invented Works",
                                  "boxes": {"1": 50000.0}}])
        _store(self.conn, "1040", [{"year": 2022, "primary_wages": 60000.0}])
        line = taxdocs.removal_effect(self.conn, _upload(self.conn, "w2")["id"])
        self.assertEqual(
            line, "No figure changes now: the 2022 tax return figures stand "
                  "in for this W-2's wages. It backs them if that return is "
                  "removed.")
        self.assertNotIn("nothing reads", line)

    def test_removing_a_w2_other_figures_cover_says_it_backs_the_wages(self):
        """A primary's W-2 in a year whose shown figures come from other
        numbers (SSA earnings, a return's household wages): removing it
        changes the stored wages but no figure shown, so "nothing reads
        this document's numbers" would be untrue."""
        _store(self.conn, "w2", [{"year": 2022, "owner": "primary",
                                  "employer": "Invented Works",
                                  "boxes": {"1": 50000.0}}])
        _store(self.conn, "ssa", [{"year": 2022, "ss_earnings": 52000.0,
                                   "medicare_earnings": 52000.0}])
        _store(self.conn, "1040", [{"year": 2022, "total_income": 80000.0,
                                    "wages": 70000.0}])
        self.assertEqual(_annual(self.conn)[2022]["primary_wages"], 50000.0)
        line = taxdocs.removal_effect(self.conn, _upload(self.conn, "w2")["id"])
        self.assertEqual(
            line, "No figure shown changes: other numbers give the 2022 "
                  "figures shown. This W-2 backs the wages if they are "
                  "removed.")
        self.assertEqual(_annual(self.conn)[2022]["primary_wages"], 50000.0)

    def test_removing_a_w2_nobody_has_claimed_says_it_counts_for_nobody(self):
        _store(self.conn, "w2", [{"year": 2022, "owner": "primary",
                                  "employer": "Invented Works",
                                  "boxes": {"1": 50000.0}}])
        _store(self.conn, "w2", [{"year": 2022, "owner": "spouse",
                                  "employer": "Invented Labs",
                                  "boxes": {"1": 20000.0}}])
        # an older app stored the second W-2 of the year without asking
        self.conn.execute("UPDATE income_documents SET owner = 'unassigned' "
                          "WHERE payer = 'Invented Labs'")
        taxdocs.resum(self.conn, 2022)
        w2 = next(d for d in taxdocs.list_stored(self.conn)
                  if d["kind"] == "w2" and d["w2s"][0]["owner"] == "unassigned")
        self.assertEqual(w2["whose_label"],
                         "Whose? not yet said — counts toward nobody's wages")
        self.assertEqual(
            taxdocs.removal_effect(self.conn, w2["id"]),
            "No figure changes: nobody has said whose this W-2 is, so it "
            "counts toward nobody's wages.")

    def test_removing_a_return_keeps_a_year_its_w2s_still_report(self):
        """Reported income keeps a year the W-2s still give wages for, so
        the effect line must not say the year is lost."""
        _store(self.conn, "w2", [{"year": 2022, "owner": "primary",
                                  "employer": "Invented Works",
                                  "boxes": {"1": 50000.0}}])
        _store(self.conn, "1040", [{"year": 2022, "total_income": 70000.0,
                                    "wages": 60000.0}])
        line = taxdocs.removal_effect(self.conn,
                                      _upload(self.conn, "1040")["id"])
        self.assertNotIn("Reported income loses", line)
        self.assertIn("Reported income changes for 2022", line)

    def test_one_figure_is_named_once_in_the_effect_line(self):
        _store(self.conn, "ssa", [{"year": y, "ss_earnings": 1000.0,
                                   "medicare_earnings": 1000.0}
                                  for y in (2000, 2001, 2019)])
        _store(self.conn, "transcript", [{"year": 2019, "wages": 45000.0}])
        line = taxdocs.removal_effect(self.conn,
                                      _upload(self.conn, "ssa")["id"])
        self.assertEqual(line, "Career earnings loses 2000–2001 and changes "
                               "for 2019.")

    def test_a_long_irs_name_control_is_marked_abbreviated(self):
        for payer in ("CEDA RIDG HARB GRIL SERV",
                      "DEPT O REVE / STATE OF INVENTED REVENUE",
                      "INVT BANK", "ZQX LL"):
            self.assertEqual(taxdocs.payer_shown(self.conn, payer, None),
                             (payer, True), payer)
        for payer in ("Invented Works", "Invented Bank Of The North"):
            self.assertEqual(taxdocs.payer_shown(self.conn, payer, None),
                             (payer, False), payer)

    def test_member_removal_gate(self):
        _store(self.conn, "transcript", [{"year": 2019, "agi": 1.0}])
        _store(self.conn, "1040", [{"year": 2018, "agi": 1.0},
                                   {"year": 2019, "agi": 2.0}])
        _store(self.conn, "ssa", [{"year": 2019, "ss_earnings": 1.0}])
        self.assertFalse(taxdocs.needs_step_up(
            self.conn, _upload(self.conn, "transcript")["id"]))
        self.assertTrue(taxdocs.needs_step_up(
            self.conn, _upload(self.conn, "1040")["id"]))
        self.assertTrue(taxdocs.needs_step_up(
            self.conn, _upload(self.conn, "ssa")["id"]))

    def test_older_client_w2_doors_unchanged(self):
        _store(self.conn, "w2", [{"year": 2022, "owner": "primary",
                                  "employer": "Invented Works",
                                  "boxes": {"1": 50000.0}}])
        w2s = taxdocs.list_w2s(self.conn)
        self.assertEqual(len(w2s), 1)
        self.assertEqual(set(w2s[0]), {"id", "year", "employer", "ein",
                                       "owner", "box1"})
        self.assertTrue(taxdocs.delete_w2(self.conn, w2s[0]["id"], {
            "year": 2022, "box1": 50000.0}))
        # the upload that held it holds nothing now, and is not listed
        self.assertEqual(taxdocs.list_stored(self.conn), [])

    def test_the_file_name_is_kept_with_card_numbers_cut(self):
        _store(self.conn, "transcript", [{"year": 2019, "agi": 1.0}],
               filename="C:\\docs\\statement 4111111111111111.pdf")
        name = _upload(self.conn, "transcript")["filename"]
        self.assertNotIn("4111111111111111", name)
        self.assertNotIn("docs", name)

    # ---- restore -----------------------------------------------------------

    def test_restore_does_not_resurrect_removed_upload(self):
        _store(self.conn, "transcript", [{"year": 2019, "agi": 100.0}])
        data = export.build_zip(self.conn)
        _remove(self.conn, _upload(self.conn, "transcript"))
        restore.restore_outcomes(self.conn, data)
        doc = _upload(self.conn, "transcript")
        self.assertTrue(doc["removed"])
        self.assertIsNone(_annual(self.conn).get(2019, {}).get("agi"))

    def test_a_restore_lands_the_uploads_and_their_figures(self):
        _store(self.conn, "transcript", [{"year": 2019, "agi": 100.0}])
        _store(self.conn, "1040", [{"year": 2019, "agi": 200.0}])
        data = export.build_zip(self.conn)
        dest = make_db()
        try:
            restore.restore_outcomes(dest, data)
            self.assertEqual(_annual(dest)[2019]["agi"], 200.0)
            _remove(dest, _upload(dest, "1040"))
            self.assertEqual(_annual(dest)[2019]["agi"], 100.0)
        finally:
            dest.close()

    def test_old_archive_restore_backfills(self):
        _legacy_household(self.conn)
        data = export.build_zip(self.conn)
        src = zipfile.ZipFile(io.BytesIO(data))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n in src.namelist():
                if n not in ("income_uploads.csv",
                             "income_upload_values.csv"):
                    z.writestr(n, src.read(n))
        dest = make_db()
        try:
            restore.restore_outcomes(dest, buf.getvalue())
            self.assertEqual(_annual(dest), _annual(self.conn))
            kinds = sorted(d["kind"] for d in taxdocs.list_stored(dest))
            self.assertEqual(kinds, ["document", "legacy_return",
                                     "legacy_return", "legacy_ssa", "w2",
                                     "w2"])
        finally:
            dest.close()

    # ---- the import history ---------------------------------------------

    def test_imported_accounts_with_no_batch_are_said_under_the_history(self):
        self.assertIsNone(batches.older_imports_line(self.conn))
        self.conn.execute("INSERT INTO items (id, aggregator, "
                          "institution_name) VALUES ('manual', 'manual', "
                          "'Manual')")
        self.conn.execute("INSERT INTO accounts (id, item_id, name, type) "
                          "VALUES ('imp1', 'manual', 'Invented Card', "
                          "'credit')")
        self.assertEqual(
            batches.older_imports_line(self.conn),
            "1 account came from files before undo history existed (a "
            "restore or an earlier import); remove it from Accounts.")


if __name__ == "__main__":
    unittest.main()


class StoredDocumentsKeepWhatTheyHold(unittest.TestCase):
    """What a restore, a rollback or a put-back must never lose or move."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _without_uploads(self, data):
        src = zipfile.ZipFile(io.BytesIO(data))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n in src.namelist():
                if n not in ("income_uploads.csv",
                             "income_upload_values.csv"):
                    z.writestr(n, src.read(n))
        return buf.getvalue()

    def _every_pointer_has_its_upload(self, conn):
        for table in ("income_documents", "income_upload_values"):
            loose = conn.execute(
                f"SELECT count(*) AS n FROM {table} x WHERE x.upload_id "
                "IS NOT NULL AND NOT EXISTS (SELECT 1 FROM income_uploads u "
                "WHERE u.id = x.upload_id)").fetchone()["n"]
            self.assertEqual(loose, 0, table)

    def test_a_restore_keeps_every_backfilled_upload_apart(self):
        # the migration's uploads share kind, an empty name and a moment
        _legacy_household(self.conn)
        _run_migration_backfill(self.conn)
        self.conn.execute("UPDATE income_uploads SET stored_at = "
                          "'2001-01-01T00:00:00+00'")
        before = _annual(self.conn)
        held = sorted((d["kind"], d["years_label"])
                      for d in taxdocs.list_stored(self.conn))
        data = export.build_zip(self.conn)
        dest = make_db()
        try:
            restore.restore_outcomes(dest, data)
            self._every_pointer_has_its_upload(dest)
            self.assertEqual(sorted((d["kind"], d["years_label"])
                                    for d in taxdocs.list_stored(dest)),
                             held)
            self.assertEqual(_annual(dest), before)
            # the next upload takes a fresh id, never a landed one's
            _store(dest, "transcript", [{"year": 2030, "agi": 1.0}])
            self._every_pointer_has_its_upload(dest)
            self.assertEqual(len(taxdocs.list_stored(dest)), len(held) + 1)
            # restored twice: the same uploads, by their identity
            restore.restore_outcomes(dest, data)
            self.assertEqual(len(taxdocs.list_stored(dest)), len(held) + 1)
        finally:
            dest.close()

    def test_figures_an_older_build_wrote_survive_the_next_upload(self):
        _store(self.conn, "ssa", [{"year": 2023, "ss_earnings": 6.0,
                                   "medicare_earnings": 7.0}])
        _store(self.conn, "1040", [{"year": 2023, "total_income": 70.0,
                                    "agi": 68.0}])
        # the release before this one, after a rollback: an amended return
        # merged straight into the year, and a W-2 with no upload
        x = self.conn.execute
        x("UPDATE income_annual SET total_income = 72.5, agi = 70.1 "
          "WHERE year = 2023")
        x("INSERT INTO income_documents (id, year, form, owner, payer, "
          "primary_amount) VALUES (99, 2023, 'W-2', 'primary', "
          "'Invented Works', 61.0)")
        x("UPDATE income_annual SET primary_wages = 61, primary_wages_w2 = 61 "
          "WHERE year = 2023")
        _store(self.conn, "ssa", [{"year": 2023, "ss_earnings": 6.5,
                                   "medicare_earnings": 7.5}])
        row = _annual(self.conn)[2023]
        self.assertEqual((row["total_income"], row["agi"]), (72.5, 70.1))
        self.assertEqual(row["ss_earnings"], 6.5)
        self.assertEqual(row["primary_wages"], 61.0)
        found = [d for d in taxdocs.list_stored(self.conn)
                 if d["kind"] == "legacy_return"]
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["stored_label"].startswith(
            "Written outside this list"))
        self.assertTrue(any(d["kind"] == "w2"
                            for d in taxdocs.list_stored(self.conn)))

    def test_the_nightly_sweep_adopts_what_an_outside_writer_left(self):
        _store(self.conn, "1040", [{"year": 2023, "agi": 68.0}])
        self.conn.execute("UPDATE income_annual SET agi = 70 WHERE year = 2023")
        self.assertEqual(taxdocs.adopt_household(self.conn), 1)
        _remove(self.conn, _upload(self.conn, "1040"))
        self.assertEqual(_annual(self.conn)[2023]["agi"], 70.0)

    def test_an_archived_legacy_figure_never_replaces_a_local_one(self):
        self.conn.execute("INSERT INTO income_annual (year, total_income, "
                          "agi) VALUES (2019, 50, 49)")
        taxdocs.backfill_household(self.conn)
        other = make_db()
        try:
            other.execute("INSERT INTO income_annual (year, total_income, "
                          "agi) VALUES (2019, 48, 47), (2020, 30, 29)")
            taxdocs.backfill_household(other)
            restore.restore_outcomes(self.conn, export.build_zip(other))
        finally:
            other.close()
        a = _annual(self.conn)
        self.assertEqual((a[2019]["total_income"], a[2019]["agi"]),
                         (50.0, 49.0))
        self.assertEqual(a[2020]["agi"], 29.0)
        self._every_pointer_has_its_upload(self.conn)

    def test_an_older_archive_merged_into_a_household_with_uploads_is_listed(self):
        _store(self.conn, "1040", [{"year": 2020, "agi": 1000.0}])
        other = make_db()
        try:
            other.execute("INSERT INTO income_annual (year, agi, "
                          "total_income) VALUES (2021, 777, 800)")
            data = self._without_uploads(export.build_zip(other))
        finally:
            other.close()
        restore.restore_outcomes(self.conn, data)
        self.assertEqual(_annual(self.conn)[2021]["agi"], 777.0)
        doc = next(d for d in taxdocs.list_stored(self.conn)
                   if d["years"] == [2021])
        _remove(self.conn, doc)
        self.assertNotIn(2021, _annual(self.conn))

    def test_restoring_a_pre_removal_export_keeps_a_removed_w2_out(self):
        _store(self.conn, "w2", [{"year": 2022, "owner": "primary",
                                  "employer": "Invented Works",
                                  "boxes": {"1": 500.0}}])
        data = export.build_zip(self.conn)
        _remove(self.conn, _upload(self.conn, "w2"))
        restore.restore_outcomes(self.conn, data)
        self.assertIsNone(_annual(self.conn).get(2022, {}).get(
            "primary_wages"))
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM income_documents").fetchone()["n"], 0)
        self.assertTrue(_upload(self.conn, "w2")["removed"])

    def test_put_back_keeps_each_twin_with_its_person(self):
        twins = [{"year": 2024, "owner": o, "employer": "Invented Co",
                  "ein": "11-1111234", "boxes": {"1": 520.0}}
                 for o in ("primary", "spouse")]
        _store(self.conn, "w2", twins)
        before = _annual(self.conn)
        d = _upload(self.conn, "w2")
        _remove(self.conn, d)
        g = _upload(self.conn, "w2")
        taxdocs.restore_upload(self.conn, g["id"], g["seen"])
        self.assertEqual(_annual(self.conn), before)
        # one spouse's own file, removed and put back beside the other's
        other = make_db()
        try:
            write_config(other)
            _store(other, "w2", [dict(twins[0], ein=None)], filename="a.pdf")
            _store(other, "w2", [dict(twins[1], ein=None, replaces=None)],
                   filename="b.pdf")
            before = _annual(other)
            b = next(x for x in taxdocs.list_stored(other)
                     if x["filename"] == "b.pdf")
            _remove(other, b)
            b = next(x for x in taxdocs.list_stored(other)
                     if x["filename"] == "b.pdf")
            taxdocs.restore_upload(other, b["id"], b["seen"])
            self.assertEqual(_annual(other), before)
        finally:
            other.close()

    def test_a_w2_stored_since_the_removal_takes_the_put_back_ones_place(self):
        row = {"year": 2024, "owner": "primary", "employer": "Invented Co",
               "boxes": {"1": 520.0}}
        _store(self.conn, "w2", [row], filename="a.pdf")
        a = _upload(self.conn, "w2")
        _remove(self.conn, a)
        _store(self.conn, "w2", [dict(row, replaces=None)], filename="b.pdf")
        a = next(x for x in taxdocs.list_stored(self.conn)
                 if x["filename"] == "a.pdf")
        taxdocs.restore_upload(self.conn, a["id"], a["seen"])
        self.assertEqual(_annual(self.conn)[2024]["primary_wages"], 520.0)
        a = next(x for x in taxdocs.list_stored(self.conn)
                 if x["filename"] == "a.pdf")
        self.assertEqual(a["w2s"][0]["owner"], "unassigned")

    def test_removing_a_joint_return_keeps_its_filing_status(self):
        self.conn.execute(
            "INSERT INTO income_annual (year, total_income, wages, joint, "
            "filing_status) VALUES (2019, 90, 85, 1, 'MFJ')")
        taxdocs.backfill_household(self.conn)
        _remove(self.conn, _upload(self.conn, "legacy_return"))
        g = _upload(self.conn, "legacy_return")
        taxdocs.restore_upload(self.conn, g["id"], g["seen"])
        r = self.conn.execute("SELECT total_income, joint, filing_status "
                              "FROM income_annual WHERE year = 2019"
                              ).fetchone()
        self.assertEqual(tuple(r.values()), (90.0, 1, "MFJ"))

    def test_a_restored_upload_is_kept_to_what_the_live_door_keeps(self):
        _store(self.conn, "w2", [{"year": 2022, "owner": "primary",
                                  "employer": "Invented Works",
                                  "boxes": {"1": 5.0}}])
        _remove(self.conn, _upload(self.conn, "w2"))
        data = export.build_zip(self.conn)
        z = zipfile.ZipFile(io.BytesIO(data))
        rows = list(csv.DictReader(io.StringIO(
            z.read("income_uploads.csv").decode())))
        held = json.loads(rows[0]["held_documents"])
        held[0]["ein_last4"] = "123456789"
        rows[0]["held_documents"] = json.dumps(held + [{"year": "x"}, 7])
        rows[0]["filename"] = "C:\\scans\\w2 4111111111111111.pdf"
        bad = dict(rows[0], uid=str(uuid.uuid4()), id="77",
                   removed_at="not a time")
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows([rows[0], bad])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as dz:
            for n in z.namelist():
                dz.writestr(n, out.getvalue() if n == "income_uploads.csv"
                            else z.read(n))
        dest = make_db()
        try:
            _, outcome = restore.restore_outcomes(dest, buf.getvalue())
            self.assertEqual(outcome.refused.get(
                ("income_uploads", "malformed")), 1)
            doc = _upload(dest, "w2")
            self.assertNotIn("4111111111111111", doc["filename"])
            self.assertNotIn("scans", doc["filename"])
            taxdocs.restore_upload(dest, doc["id"], doc["seen"])
            ein = dest.execute("SELECT ein_last4 FROM income_documents"
                               ).fetchone()["ein_last4"]
            self.assertEqual(ein, "6789")
            # a held cell no build writes costs its form, never the list
            dest.execute("UPDATE income_uploads SET removed_at = now(), "
                         "held_documents = '[1, {\"year\": \"x\"}]'")
            taxdocs.list_stored(dest)
        finally:
            dest.close()

    def test_the_nightly_settles_beside_an_upload_dated_ahead(self):
        _store(self.conn, "1040", [{"year": 2021, "agi": 500.0}])
        self.conn.execute("UPDATE income_uploads SET stored_at = now() + "
                          "interval '2 days'")
        self.conn.execute("UPDATE income_annual SET agi = 650 "
                          "WHERE year = 2021")
        self.assertEqual(taxdocs.adopt_household(self.conn), 1)
        self.assertEqual(taxdocs.adopt_household(self.conn), 0)
        _store(self.conn, "ssa", [{"year": 2021, "ss_earnings": 1.0}])
        self.assertEqual(_annual(self.conn)[2021]["agi"], 650.0)
        self.assertEqual(taxdocs.adopt_household(self.conn), 0)

    def test_a_restore_reads_an_archived_moment_ahead_of_now_as_now(self):
        _store(self.conn, "1040", [{"year": 2021, "agi": 500.0}])
        data = export.build_zip(self.conn)
        src = zipfile.ZipFile(io.BytesIO(data))
        rows = list(csv.DictReader(io.StringIO(
            src.read("income_uploads.csv").decode())))
        rows[0]["stored_at"] = "2999-01-01T00:00:00+00:00"
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n in src.namelist():
                z.writestr(n, out.getvalue() if n == "income_uploads.csv"
                           else src.read(n))
        dest = make_db()
        try:
            restore.restore_outcomes(dest, buf.getvalue())
            self.assertTrue(dest.execute(
                "SELECT bool_and(stored_at <= now()) AS ok "
                "FROM income_uploads").fetchone()["ok"])
        finally:
            dest.close()

    def test_a_merge_adopts_no_year_the_household_held(self):
        self.conn.execute("INSERT INTO income_annual (year, total_income, "
                          "agi) VALUES (2019, 50000, 49000)")
        taxdocs.backfill_household(self.conn)
        other = make_db()
        try:
            _store(other, "1040", [{"year": 2019, "agi": 47000.0}])
            restore.restore_outcomes(self.conn, export.build_zip(other))
        finally:
            other.close()
        row = _annual(self.conn)[2019]
        # the archive's real document is newer than the household's own
        # figure from before the list, column by column
        self.assertEqual((row["total_income"], row["agi"]),
                         (50000.0, 47000.0))
        self.assertFalse(any(d["stored_label"].startswith("Written outside")
                             for d in taxdocs.list_stored(self.conn)))

    def test_a_figure_no_number_can_be_stops_no_door(self):
        _store(self.conn, "1040", [{"year": 2018, "agi": 1.0}])
        _remove(self.conn, _upload(self.conn, "1040"))
        self.conn.execute("UPDATE income_uploads SET removed_at = now() - "
                          "interval '40 days'")
        self.conn.execute("INSERT INTO income_annual (year, agi) "
                          "VALUES (2017, 'NaN')")
        taxdocs.adopt_household(self.conn)
        self.assertEqual(taxdocs.purge_removed(self.conn), 1)
        _store(self.conn, "transcript", [{"year": 2017, "agi": 5.0}])
        self.assertEqual(_annual(self.conn)[2017]["agi"], 5.0)

    def test_the_document_written_last_ranks_last_in_one_transaction(self):
        with self.conn.transaction():
            _store(self.conn, "transcript", [{"year": 2019, "agi": 1.0}])
            _store(self.conn, "1040", [{"year": 2019, "agi": 2.0}])
        self.assertEqual(_annual(self.conn)[2019]["agi"], 2.0)
        _remove(self.conn, _upload(self.conn, "1040"))
        self.assertEqual(_annual(self.conn)[2019]["agi"], 1.0)


class StoredDocumentDoors(unittest.TestCase):
    """The doors a person reaches: the list, the confirm's words, the
    removal (step-up for a multi-year one) and the put-back."""

    PW = "invented-horse-battery"

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        from .util import _ensure_db
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        import uuid
        from fastapi.testclient import TestClient
        from oikonome.db import tenancy
        from oikonome.web import security
        security._limiter._hits.clear()
        self.c = TestClient(self.app)
        r = self.c.post("/api/signup", data={
            "email": f"sd-{uuid.uuid4().hex[:8]}@example.dev",
            "password": self.PW})
        assert r.status_code in (200, 303), r.text
        self.tid = self.c.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(self.tid)
        try:
            _store(conn, "transcript", [{"year": 2019, "agi": 100.0}])
            _store(conn, "1040", [{"year": 2018, "agi": 1.0},
                                  {"year": 2019, "agi": 200.0}])
        finally:
            conn.close()

    def _doc(self, kind):
        docs = self.c.get("/api/import/taxdoc/stored").json()["documents"]
        return next(d for d in docs if d["kind"] == kind)

    def test_a_one_year_document_is_removed_and_put_back_through_its_doors(self):
        d = self._doc("transcript")
        fx = self.c.get(f"/api/import/taxdoc/stored/{d['id']}/effect").json()
        self.assertFalse(fx["step_up"])
        self.assertIn("No figure changes", fx["effect_line"])
        r = self.c.request("DELETE", f"/api/import/taxdoc/stored/{d['id']}",
                           json={"seen": d["seen"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["years"], [2019])
        # the same list a second time: removed already
        r = self.c.request("DELETE", f"/api/import/taxdoc/stored/{d['id']}",
                           json={"seen": d["seen"]})
        self.assertEqual(r.status_code, 404)
        # and a removed document has no removal to work out
        self.assertEqual(self.c.get(
            f"/api/import/taxdoc/stored/{d['id']}/effect").status_code, 404)
        gone = self._doc("transcript")
        r = self.c.post(f"/api/import/taxdoc/stored/{d['id']}/restore",
                        json={"seen": gone["seen"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(self._doc("transcript")["removed"])

    def test_a_multi_year_document_needs_the_step_up_window(self):
        d = self._doc("1040")
        r = self.c.request("DELETE", f"/api/import/taxdoc/stored/{d['id']}",
                           json={"seen": d["seen"]})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["detail"]["error"], "elevation_required")
        self.assertFalse(self._doc("1040")["removed"])
        r = self.c.post("/api/auth/elevate", json={"password": self.PW})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.c.request("DELETE", f"/api/import/taxdoc/stored/{d['id']}",
                           json={"seen": d["seen"]})
        self.assertEqual(r.status_code, 200, r.text)

    def test_a_stale_list_is_refused_at_the_door(self):
        d = self._doc("transcript")
        r = self.c.request("DELETE", f"/api/import/taxdoc/stored/{d['id']}",
                           json={"seen": dict(d["seen"], years=[2020])})
        self.assertEqual(r.status_code, 409, r.text)


def _rewrite(data: bytes, name: str, edit) -> bytes:
    """The archive `data` with `name`'s rows passed through `edit` (a
    function of the list of row dicts, returning the rows to write)."""
    src = zipfile.ZipFile(io.BytesIO(data))
    rows = list(csv.DictReader(io.StringIO(src.read(name).decode())))
    fields = list(rows[0])
    rows = edit(rows)
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=fields)
    w.writeheader()
    w.writerows(rows)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in src.namelist():
            z.writestr(n, out.getvalue() if n == name else src.read(n))
    return buf.getvalue()


class AStoredDocumentStaysReachable(unittest.TestCase):
    """Every listed document can be removed and put back, whatever wrote
    it, and nothing a restore lands can take the list down."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _second_connection(self):
        import os
        from oikonome.db import tenancy
        from .util import TEST_DB
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        other = tenancy.tenant_connect(tid, os.environ.get(
            "OIKONOME_TEST_DSN",
            f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}"))
        self.addCleanup(other.close)
        return other

    def test_a_restored_record_partly_given_by_local_documents_is_removed_and_put_back(self):
        _store(self.conn, "ssa", [{"year": y, "ss_earnings": 100.0 + y}
                                  for y in (2020, 2021, 2022)])
        other = make_db()
        try:
            other.execute(
                "INSERT INTO income_annual (year, ss_earnings) VALUES "
                "(2018, 1.0), (2019, 2.0), (2020, 3.0), (2021, 4.0)")
            taxdocs.backfill_household(other)
            restore.restore_outcomes(self.conn, export.build_zip(other))
        finally:
            other.close()
        # the archive's figures the household's own record gives are not
        # landed at all, not landed empty
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM income_upload_values "
            "WHERE fields = '{}'::jsonb").fetchone()["n"], 0)
        doc = _upload(self.conn, "legacy_ssa")
        self.assertEqual(doc["years"], [2018, 2019])
        self.assertEqual(_remove(self.conn, doc), [2018, 2019])
        gone = _upload(self.conn, "legacy_ssa")
        self.assertEqual(taxdocs.restore_upload(
            self.conn, gone["id"], gone["seen"]), [2018, 2019])
        self.assertEqual(_annual(self.conn)[2021]["ss_earnings"], 2121.0)

    def test_an_empty_values_row_left_by_an_older_restore_counts_no_year(self):
        _store(self.conn, "1040", [{"year": 2020, "agi": 10.0}])
        doc = _upload(self.conn, "1040")
        taxdocs.write_upload_values(self.conn, doc["id"], 2016, {})
        doc = _upload(self.conn, "1040")
        self.assertEqual(doc["years"], [2020])
        self.assertEqual(_remove(self.conn, doc), [2020])

    def test_a_restore_takes_a_taken_upload_id_under_the_id_lock(self):
        """A restored upload whose archived id is taken here is given the
        next id by the live door's allocator, under the household's
        document-ids lock: a store of a year the restore does not hold
        allocates from the same MAX+1 and must wait for it."""
        _store(self.conn, "1040", [{"year": 2023, "agi": 10.0}])
        data = export.build_zip(self.conn)
        dest = make_db()
        self.addCleanup(dest.close)
        # the archived upload's id is taken by another document here
        _store(dest, "transcript", [{"year": 2030, "agi": 1.0}])
        tid = dest.execute("SELECT current_setting('app.tenant_id') AS t"
                           ).fetchone()["t"]
        probe = self._second_connection()
        held = []
        real = taxdocs._new_upload_id

        def allocate(conn):
            got = real(conn)
            if conn is dest:
                free = probe.execute(
                    "SELECT pg_try_advisory_lock(hashtext("
                    "'income-document-ids'), hashtext(%s)) AS ok",
                    (tid,)).fetchone()["ok"]
                if free:
                    probe.execute("SELECT pg_advisory_unlock(hashtext("
                                  "'income-document-ids'), hashtext(%s))",
                                  (tid,))
                held.append(not free)
            return got
        with mock.patch.object(taxdocs, "_new_upload_id", allocate):
            restore.restore_outcomes(dest, data)
        self.assertTrue(held)
        self.assertTrue(all(held), held)
        self.assertEqual(len(taxdocs.list_stored(dest)), 2)

    def test_a_request_does_not_wait_on_the_id_lock_behind_a_restore(self):
        _store(self.conn, "1040", [{"year": 2023, "agi": 10.0}])
        holder = self._second_connection()
        with holder.transaction():
            # what a restore holds for its whole transaction
            taxdocs._lock_years(holder, [2019], bounded=False)
            taxdocs.lock_document_ids(holder)
            self.conn.execute("SET statement_timeout = '3s'")
            try:
                doc = _upload(self.conn, "1040")
                self.assertIsInstance(
                    taxdocs.removal_effect(self.conn, doc["id"]), str)
                self.assertEqual(_remove(self.conn, doc), [2023])
            finally:
                self.conn.execute("SET statement_timeout = 0")

    def test_a_request_that_must_adopt_answers_busy_behind_a_restore(self):
        import psycopg
        _store(self.conn, "1040", [{"year": 2024, "agi": 10.0}])
        # a form an older build stored without an upload: the removal
        # gives it one first, which allocates an id
        self.conn.execute(
            "INSERT INTO income_documents (id, year, form, owner, payer, "
            "primary_amount) VALUES (40, 2024, '1099-INT', 'primary', "
            "'Invented Bank', 5)")
        doc = _upload(self.conn, "1040")
        holder = self._second_connection()
        with holder.transaction():
            taxdocs.lock_document_ids(holder)
            self.conn.execute("SET statement_timeout = '15s'")
            try:
                with self.assertRaises(psycopg.errors.LockNotAvailable):
                    _remove(self.conn, doc)
            finally:
                self.conn.execute("SET statement_timeout = 0")

    def test_a_held_form_no_number_can_date_is_refused_alone(self):
        for year in (float("inf"), float("nan"), 2024.5, True, 10 ** 400):
            self.assertIsNone(taxdocs._held_form(
                {"year": year, "form": "W-2"}))
        kept = taxdocs._held_form(
            {"year": 2024, "form": "W-2",
             "amounts": {"1": 5.0, "2": "text", "3": {"deep": 1},
                         "4": True}})
        self.assertEqual(kept["amounts"], {"1": 5.0})
        _store(self.conn, "w2", [{"year": 2022, "owner": "primary",
                                  "employer": "Invented Works",
                                  "boxes": {"1": 5.0}}])
        _remove(self.conn, _upload(self.conn, "w2"))

        def edit(rows):
            held = rows[0]["held_documents"]
            rows[0]["held_documents"] = (
                held[:-1] + ', {"year": 1e999, "form": "W-2"}, '
                '{"year": NaN, "form": "W-2"}, '
                '{"year": 1' + "0" * 400 + ', "form": "W-2"}]')
            return rows
        data = _rewrite(export.build_zip(self.conn), "income_uploads.csv",
                        edit)
        dest = make_db()
        try:
            restore.restore_outcomes(dest, data)
            doc = _upload(dest, "w2")
            self.assertEqual(doc["years"], [2022])
            self.assertEqual(taxdocs.restore_upload(
                dest, doc["id"], doc["seen"]), [2022])
            # jsonb prints 1e999 back as a long integer: one stored that
            # way costs its form, never the list
            dest.execute(
                "UPDATE income_uploads SET removed_at = now(), "
                "held_documents = %s::jsonb",
                ('[{"year": 1' + "0" * 400 + ', "form": "W-2"}, '
                 '{"year": 2022, "form": "W-2"}]',))
            self.assertEqual(_upload(dest, "w2")["years"], [2022])
        finally:
            dest.close()

    def test_a_document_dated_before_any_calendar_leaves_the_list_working(self):
        write_config(self.conn, timezone="America/Denver")
        _store(self.conn, "1040", [{"year": 2021, "agi": 500.0}])

        def edit(rows):
            rows[0]["stored_at"] = "0001-01-01T00:00:00+00:00"
            return rows
        data = _rewrite(export.build_zip(self.conn), "income_uploads.csv",
                        edit)
        dest = make_db()
        try:
            write_config(dest, timezone="America/Denver")
            _, outcome = restore.restore_outcomes(dest, data)
            self.assertEqual(outcome.refused.get(
                ("income_uploads", "malformed")), 1)
            taxdocs.list_stored(dest)
        finally:
            dest.close()
        # one already stored that way (an older build's restore) is listed
        self.conn.execute("UPDATE income_uploads SET stored_at = "
                          "'0001-01-01T00:00:00+00:00'")
        self.assertEqual(len(taxdocs.list_stored(self.conn)), 1)
