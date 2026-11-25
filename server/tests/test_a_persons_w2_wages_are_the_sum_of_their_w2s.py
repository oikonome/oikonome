"""A person's wages for a year are the sum of their W-2s (specs/w2-wages.md).

A W-2 does not print whose it is, so the person says; each stored W-2
counts toward one person's wages, or nobody's while unassigned. Whether an
upload is a form already stored is asked, never inferred: two spouses with
the same employer and the same box 1 hold two W-2s. A figure the W-2s did
not set — a return, a transcript, an older build after a rollback — is
never moved by one.
"""

import io
import threading
import unittest
import zipfile
from unittest import mock

import psycopg

from oikonome.db import migrate, staging, tenancy
from oikonome.engine import taxdocs
from oikonome.sync import restore

from .util import TEST_DB, _admin_dsn, make_db


def _w2(year, box1, employer="ACME STAFFING", ein="•••••6789", **more):
    """A W-2 row as a current client sends it: whose it is always said
    (primary unless `owner` is given; owner=None leaves the key out, as an
    app older than the question does)."""
    more.setdefault("owner", "primary")
    if more["owner"] is None:
        del more["owner"]
    return {"year": year, "employer": employer, "ein": ein,
            "boxes": {} if box1 is None else {"1": box1}, **more}


class _Db(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def store(self, *rows, kind="w2"):
        token = staging.put(self.conn, "taxdoc",
                            meta={"kind": kind, "rows": []})
        return taxdocs.commit(self.conn, token, list(rows))

    def annual(self, year):
        return self.conn.execute(
            "SELECT * FROM income_annual WHERE year = %s", (year,)).fetchone()

    def wages(self, year):
        row = self.annual(year)
        return (row["primary_wages"], row["spouse_wages"]) if row else None

    def docs(self, year):
        return self.conn.execute(
            "SELECT * FROM income_documents WHERE year = %s ORDER BY id",
            (year,)).fetchall()

    def tid(self):
        return self.conn.execute(
            "SELECT current_setting('app.tenant_id', true) AS t"
        ).fetchone()["t"]

    def seen(self, doc_id):
        """What the stored list shows for a W-2: its year and box 1."""
        w = next(w for w in taxdocs.list_w2s(self.conn) if w["id"] == doc_id)
        return {k: w[k] for k in ("year", "box1", "employer", "ein", "owner")}

    def delete(self, doc_id, seen=None):
        return taxdocs.delete_w2(self.conn, doc_id, seen or self.seen(doc_id))

    def name(self, doc_id, owner):
        return taxdocs.set_w2_owner(self.conn, doc_id, owner,
                                    self.seen(doc_id))

    def second_conn(self):
        c = tenancy.tenant_connect(
            self.tid(),
            f"{self.conn.info.dsn} password={self.conn.info.password}")
        self.addCleanup(c.close)
        return c


class SumTests(_Db):
    def test_two_employers_in_a_year_add_up(self):
        self.store(_w2(2024, 50000.0))
        self.store(_w2(2024, 12000.5, "BIRCH DINER", "•••••1111"))
        self.assertEqual(self.wages(2024), (62000.5, None))
        self.assertEqual(self.annual(2024)["primary_wages_w2"], 62000.5)
        self.assertEqual(self.annual(2024)["source"], "w2")

    def test_a_spouses_w2s_count_for_the_spouse_alone(self):
        self.store(_w2(2024, 50000.0))
        self.store(_w2(2024, 30000.0, "BIRCH", "•••••1111", owner="spouse"))
        self.store(_w2(2024, 4000.0, "CEDAR", "•••••2222", owner="spouse"))
        self.assertEqual(self.wages(2024), (50000.0, 34000.0))

    def test_an_unread_box_1_is_kept_and_counts_for_nothing(self):
        self.store(_w2(2024, None))
        self.assertEqual(len(self.docs(2024)), 1)
        self.assertIsNone(self.annual(2024))

    def test_an_owner_that_is_no_person_is_refused_and_nothing_written(self):
        with self.assertRaises(ValueError):
            self.store(_w2(2024, 1.0, owner="dog"))
        self.assertEqual(self.docs(2024), [])


class SameFormTests(_Db):
    """Is it the form already stored? The person answers."""

    def test_two_spouses_same_employer_same_wages_are_two_w2s(self):
        self.store(_w2(2024, 48000.0))
        self.store(_w2(2024, 48000.0, owner="spouse", replaces=None))
        self.assertEqual(len(self.docs(2024)), 2)
        self.assertEqual(self.wages(2024), (48000.0, 48000.0))

    def test_a_lookalike_with_no_answer_is_refused_and_nothing_written(self):
        self.store(_w2(2024, 48000.0))
        token = staging.put(self.conn, "taxdoc",
                            meta={"kind": "w2", "rows": []})
        with self.assertRaises(taxdocs.W2Unanswered):
            taxdocs.commit(self.conn, token, [_w2(2024, 48000.0,
                                                  owner="spouse")])
        self.assertEqual(len(self.docs(2024)), 1)
        # the preview survives the refusal, so the person can answer
        self.assertIsNotNone(staging.pop(self.conn, "taxdoc", token))

    def test_the_same_form_again_replaces_it_and_never_doubles(self):
        self.store(_w2(2024, 48000.0))
        doc = self.docs(2024)[0]["id"]
        out = self.store(_w2(2024, 48000.0, replaces=doc))
        self.assertEqual(out["replaced"], 1)
        self.assertEqual(len(self.docs(2024)), 1)
        self.assertEqual(self.wages(2024), (48000.0, None))

    def test_the_same_form_under_the_other_person_is_a_move(self):
        self.store(_w2(2024, 48000.0))
        doc = self.docs(2024)[0]["id"]
        self.store(_w2(2024, 48000.0, owner="spouse", replaces=doc))
        self.assertEqual([d["owner"] for d in self.docs(2024)], ["spouse"])
        self.assertEqual(self.wages(2024), (None, 48000.0))

    def test_an_unread_box_1_is_never_a_lookalike(self):
        self.store(_w2(2024, None))
        self.store(_w2(2024, None))           # no answer needed
        self.assertEqual(len(self.docs(2024)), 2)
        self.assertEqual(taxdocs.w2_matches(self.conn, 2024, "ACME STAFFING",
                                            "•••••6789", None), [])

    def test_the_preview_lists_the_lookalikes(self):
        self.store(_w2(2024, 48000.0))
        out = taxdocs.stage(self.conn, "w2", [
            {"year": 2024, "employer": "ACME STAFFING", "ein": "•••••6789",
             "boxes": {"1": 48000.0}, "box1": 48000.0}])
        m = out["rows"][0]["matches"]
        self.assertEqual([(x["owner"], x["box1"]) for x in m],
                         [("primary", 48000.0)])

    def test_replacing_a_w2_of_another_year_is_refused(self):
        self.store(_w2(2023, 48000.0))
        doc = self.docs(2023)[0]["id"]
        with self.assertRaises(ValueError):
            self.store(_w2(2024, 48000.0, replaces=doc))
        self.assertEqual(self.docs(2024), [])

    def test_an_edit_that_makes_a_lookalike_is_asked_with_its_matches(self):
        """The preview read 47,000 and found nothing; the person corrects
        box 1 to 48,000, which is a stored W-2. The refusal carries the
        lookalike so the client can ask; analyzing again never would."""
        self.store(_w2(2024, 48000.0))
        token = staging.put(self.conn, "taxdoc",
                            meta={"kind": "w2", "rows": []})
        with self.assertRaises(taxdocs.W2Unanswered) as cm:
            taxdocs.commit(self.conn, token, [_w2(2024, 48000.0,
                                                  owner="spouse")])
        self.assertEqual([[m["box1"] for m in row] for row in cm.exception
                          .matches], [[48000.0]])
        out = taxdocs.commit(self.conn, token, [_w2(
            2024, 48000.0, owner="spouse", replaces=None)])
        self.assertEqual(out["documents"], 1)

    def test_a_replaces_that_no_longer_looks_like_this_form_is_asked_again(
            self):
        """The preview answered "the same form" for a W-2 that was removed
        since, and its id came back as another person's form: the commit
        must not overwrite it."""
        self.store(_w2(2024, 48000.0))
        old = self.docs(2024)[0]["id"]
        self.delete(old)
        self.store(_w2(2024, 9000.0, "BIRCH", "•••••1111", owner="spouse"))
        self.assertEqual(self.docs(2024)[0]["id"], old)      # the id is back
        with self.assertRaises(ValueError):
            self.store(_w2(2024, 48000.0, replaces=old))
        self.assertEqual(self.wages(2024), (None, 9000.0))

    def test_a_stale_replaces_beside_a_live_lookalike_is_asked_about_it(self):
        self.store(_w2(2024, 48000.0))
        self.store(_w2(2024, 48000.0, owner="spouse", replaces=None))
        a, b = (d["id"] for d in self.docs(2024))
        token = staging.put(self.conn, "taxdoc",
                            meta={"kind": "w2", "rows": []})
        with self.assertRaises(taxdocs.W2Unanswered) as cm:
            taxdocs.commit(self.conn, token,
                           [_w2(2024, 48000.0, replaces=b + 100)])
        self.assertEqual([m["id"] for m in cm.exception.matches[0]], [a, b])

    def test_two_rows_cannot_replace_the_same_stored_w2(self):
        self.store(_w2(2024, 48000.0))
        doc = self.docs(2024)[0]["id"]
        with self.assertRaises(ValueError):
            self.store(_w2(2024, 48000.0, replaces=doc),
                       _w2(2024, 48000.0, owner="spouse", replaces=doc))
        self.assertEqual(self.wages(2024), (48000.0, None))

    def test_an_ein_read_on_one_side_only_is_still_asked_about(self):
        self.store(_w2(2024, 48000.0, "ACME STAFFING", None))
        self.assertEqual(len(taxdocs.w2_matches(
            self.conn, 2024, "ACME STAFFING INC.", "12-3456789", 48000.0)), 1)
        # two EINs that were both read and differ are two employers
        self.store(_w2(2024, 7000.0, "BIRCH", "•••••1111"))
        self.assertEqual(taxdocs.w2_matches(
            self.conn, 2024, "BIRCH", "•••••2222", 7000.0), [])

    def test_a_box_1_too_large_for_the_database_matches_nothing(self):
        self.assertEqual(taxdocs.w2_matches(
            self.conn, 2024, "ACME", None, 10 ** 400), [])
        out = taxdocs.stage(self.conn, "w2", [
            {"year": 2024, "employer": "ACME", "boxes": {"1": 10 ** 400},
             "box1": 10 ** 400}])
        self.assertEqual(out["rows"][0]["matches"], [])

    def test_a_replaces_that_is_not_an_id_is_refused(self):
        for bad in (True, "3", 1.5, -1, 2 ** 64):
            with self.assertRaises(ValueError):
                self.store(_w2(2024, 1.0, replaces=bad))


class OlderAppTests(_Db):
    """An app older than the question sends no owner and is never asked."""

    def test_the_first_w2_of_a_year_is_the_primary_earners(self):
        self.store(_w2(2024, 50000.0, owner=None))
        self.assertEqual(self.docs(2024)[0]["owner"], "primary")
        self.assertEqual(self.wages(2024), (50000.0, None))

    def test_a_later_one_is_unassigned_and_counts_for_nobody(self):
        self.store(_w2(2024, 50000.0, owner=None))
        self.store(_w2(2024, 30000.0, "BIRCH", "•••••1111", owner=None))
        self.store(_w2(2024, 50000.0, owner=None))      # a re-upload
        self.assertEqual([d["owner"] for d in self.docs(2024)],
                         ["primary", "unassigned", "unassigned"])
        self.assertEqual(self.wages(2024), (50000.0, None))


class _Undo(Exception):
    pass


class FiledByAnOlderBuildTests(unittest.TestCase):
    """Migration 227 over W-2s a build that never asked stored under the
    primary earner (and 226 seeded as the W-2s' figure). One admin
    transaction, rolled back."""

    def test_only_the_w2_the_figure_stands_for_stays_primary(self):
        conn = make_db()
        self.addCleanup(conn.close)
        tid = conn.execute(
            "SELECT current_setting('app.tenant_id', true) AS t"
        ).fetchone()["t"]
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(admin.close)
        sql = (migrate.HERE / "migrations" /
               "227_w2s_an_older_build_filed_under_the_primary_earner.sql"
               ).read_text()
        try:
            with admin.transaction():
                for d in (
                        # 2021: his form set the figure; hers, filed under
                        # him; his uploaded twice
                        (1, 2021, "ACME", 60000.0), (2, 2021, "BIRCH", 40000.0),
                        (3, 2021, "ACME", 60000.0),
                        # 2022: one W-2, the common case
                        (4, 2022, "ACME", 52000.0),
                        # 2023: the figure is the SECOND stored
                        (5, 2023, "BIRCH", 1.0), (6, 2023, "ACME", 54000.0),
                        # 2024: two primary W-2s that ARE the figure's sum
                        (7, 2024, "ACME", 50000.0), (8, 2024, "CEDAR", 5000.0)):
                    admin.execute(
                        """INSERT INTO income_documents (tenant_id, id, year,
                               form, owner, payer, primary_amount)
                           VALUES (%s, %s, %s, 'W-2', 'primary', %s, %s)""",
                        (tid, *d))
                for year, wages in ((2021, 60000.0), (2022, 52000.0),
                                    (2023, 54000.0), (2024, 55000.0)):
                    admin.execute(
                        """INSERT INTO income_annual (tenant_id, year,
                               primary_wages, primary_wages_w2, source)
                           VALUES (%s, %s, %s, %s, 'w2')""",
                        (tid, year, wages, wages))
                admin.execute(sql)
                admin.execute(sql)       # running it again changes nothing
                owners = {r["id"]: r["owner"] for r in admin.execute(
                    "SELECT id, owner FROM income_documents "
                    "WHERE tenant_id = %s", (tid,)).fetchall()}
                self.assertEqual(owners, {
                    1: "primary", 2: "unassigned", 3: "unassigned",
                    4: "primary", 5: "unassigned", 6: "primary",
                    7: "primary", 8: "primary"})
                marks = {r["year"]: (r["primary_wages"],
                                     r["primary_wages_w2"])
                         for r in admin.execute(
                             "SELECT * FROM income_annual "
                             "WHERE tenant_id = %s", (tid,)).fetchall()}
                self.assertEqual(marks, {
                    2021: (60000.0, 60000.0), 2022: (52000.0, 52000.0),
                    2023: (54000.0, 54000.0), 2024: (55000.0, 55000.0)})
                raise _Undo
        except _Undo:
            pass

    def test_the_restore_applies_the_same_rule_to_an_older_archive(self):
        conn = make_db()
        self.addCleanup(conn.close)
        restore.restore_zip(conn, _zip({
            "income_annual.csv":
                "year,primary_wages,source\n2021,60000.0,w2\n",
            "income_documents.csv":
                "id,year,form,owner,payer,ein_last4,primary_amount,amounts,"
                "notes\n"
                '1,2021,W-2,primary,BIRCH,1111,40000.0,"{}",\n'
                '2,2021,W-2,primary,ACME,6789,60000.0,"{}",\n'}))
        owners = {r["payer"]: r["owner"] for r in conn.execute(
            "SELECT payer, owner FROM income_documents").fetchall()}
        self.assertEqual(owners, {"BIRCH": "unassigned", "ACME": "primary"})
        row = conn.execute("SELECT * FROM income_annual WHERE year = 2021"
                           ).fetchone()
        self.assertEqual((row["primary_wages"], row["primary_wages_w2"]),
                         (60000.0, 60000.0))
        # the W-2 controls work for it: a second job adds to the figure
        token = staging.put(conn, "taxdoc", meta={"kind": "w2", "rows": []})
        taxdocs.commit(conn, token, [_w2(2021, 5000.0, "CEDAR", "•••••2222")])
        self.assertEqual(conn.execute(
            "SELECT primary_wages FROM income_annual WHERE year = 2021"
        ).fetchone()["primary_wages"], 65000.0)


class OtherWritersTests(_Db):
    def test_a_figure_a_return_set_is_not_moved_by_a_w2(self):
        self.store({"year": 2024, "primary_wages": 70000.0}, kind="1040")
        self.store(_w2(2024, 50000.0))
        self.assertEqual(self.wages(2024), (70000.0, None))

    def test_a_return_writing_the_same_number_still_takes_the_figure(self):
        self.store(_w2(2024, 50000.0))
        self.store({"year": 2024, "primary_wages": 50000.0}, kind="1040")
        self.store(_w2(2024, 1000.0, "BIRCH", "•••••1111"))
        self.assertEqual(self.wages(2024), (50000.0, None))

    def test_a_writer_that_knows_nothing_of_the_mark_keeps_its_figure(self):
        """A build older than this one (a rollback) writes the figure and
        leaves the mark alone: the figure is its now, not the W-2s'."""
        self.store(_w2(2024, 50000.0))
        self.conn.execute("UPDATE income_annual SET primary_wages = 65000.0 "
                          "WHERE year = 2024")
        self.store(_w2(2024, 1000.0, "BIRCH", "•••••1111"))
        self.assertEqual(self.wages(2024), (65000.0, None))
        # and the old 221 flag, still set by a build that wrote it, is not
        # what decides
        self.conn.execute("UPDATE income_annual SET primary_wages_from_w2 "
                          "= true WHERE year = 2024")
        self.delete(self.docs(2024)[0]["id"])
        self.assertEqual(self.wages(2024), (65000.0, None))

    def test_another_document_making_the_year_leaves_the_wages_to_w2s(self):
        self.store({"year": 2024, "agi": 90000.0}, kind="transcript")
        self.store(_w2(2024, 50000.0))
        self.assertEqual(self.wages(2024), (50000.0, None))
        self.assertEqual(self.annual(2024)["source"], "transcript+w2")


class StoredListTests(_Db):
    def test_the_list_shows_each_w2_masked_and_whose(self):
        self.store(_w2(2024, 50000.0))
        self.store(_w2(2023, 10.0, owner=None))
        self.conn.execute("UPDATE income_documents SET owner = 'unassigned' "
                          "WHERE year = 2023")
        got = taxdocs.list_w2s(self.conn)
        self.assertEqual([(w["year"], w["owner"], w["ein"]) for w in got],
                         [(2024, "primary", "•••••6789"),
                          (2023, "unassigned", "•••••6789")])

    def test_removing_a_w2_sums_again_and_the_last_empties_the_figure(self):
        self.store(_w2(2024, 50000.0))
        self.store(_w2(2024, 1000.0, "BIRCH", "•••••1111"))
        a, b = (d["id"] for d in self.docs(2024))
        b_seen = self.seen(b)
        self.assertTrue(self.delete(a))
        self.assertEqual(self.wages(2024), (1000.0, None))
        self.assertTrue(self.delete(b))
        self.assertEqual(self.wages(2024), (None, None))
        self.assertFalse(self.delete(b, b_seen))

    def test_removing_a_w2_leaves_a_figure_it_did_not_set(self):
        self.store({"year": 2024, "primary_wages": 70000.0}, kind="1040")
        self.store(_w2(2024, 50000.0))
        self.delete(self.docs(2024)[0]["id"])
        self.assertEqual(self.wages(2024), (70000.0, None))

    def test_naming_the_person_moves_it_between_the_sums(self):
        self.store(_w2(2024, 50000.0))
        self.store(_w2(2024, 30000.0, "BIRCH", "•••••1111", owner=None))
        un = self.docs(2024)[1]["id"]
        self.assertTrue(self.name(un, "spouse"))
        self.assertEqual(self.wages(2024), (50000.0, 30000.0))
        self.assertTrue(self.name(un, "primary"))
        self.assertEqual(self.wages(2024), (80000.0, None))

    def test_an_identical_twin_under_the_other_person_is_allowed(self):
        self.store(_w2(2024, 48000.0))
        self.store(_w2(2024, 48000.0, owner=None))           # unassigned
        twin = self.docs(2024)[1]["id"]
        self.assertTrue(self.name(twin, "spouse"))
        self.assertEqual(self.wages(2024), (48000.0, 48000.0))

    def test_a_stale_list_never_acts_on_another_w2_under_a_reused_id(self):
        """The list showed a W-2 that was removed elsewhere since; its id
        now names a new form. Remove and an owner change both refuse."""
        self.store(_w2(2024, 48000.0))
        doc = self.docs(2024)[0]["id"]
        shown = self.seen(doc)
        self.delete(doc)
        self.store(_w2(2024, 9000.0, "BIRCH", "•••••1111", owner="spouse"))
        with self.assertRaises(taxdocs.W2Changed):
            taxdocs.delete_w2(self.conn, doc, shown)
        with self.assertRaises(taxdocs.W2Changed):
            taxdocs.set_w2_owner(self.conn, doc, "primary", shown)
        self.assertEqual(self.wages(2024), (None, 9000.0))

    def test_a_reused_id_holding_the_other_spouses_twin_is_refused(self):
        """Two spouses' identical W-2s share year and box 1: the list's
        employer, EIN and person still tell a reused id apart."""
        self.store(_w2(2024, 48000.0))
        doc = self.docs(2024)[0]["id"]
        shown = self.seen(doc)
        self.delete(doc)
        self.store(_w2(2024, 48000.0, owner="spouse"))
        self.assertEqual(self.docs(2024)[0]["id"], doc)
        with self.assertRaises(taxdocs.W2Changed):
            taxdocs.delete_w2(self.conn, doc, shown)
        self.assertEqual(self.wages(2024), (None, 48000.0))

    def test_a_twin_swapped_in_while_waiting_for_the_year_is_refused(self):
        """The id is checked against the list again once the year is
        held: a form removed and replaced by the other spouse's twin
        (same year and box 1) while the request waited is not acted on."""
        self.store(_w2(2024, 48000.0))
        doc = self.docs(2024)[0]["id"]
        shown = self.seen(doc)
        real_lock = taxdocs._lock_years

        def lock_after_a_swap(conn, years, bounded):
            # what a writer that held the year first leaves behind
            conn.execute("UPDATE income_documents SET owner = 'spouse', "
                         "payer = 'BIRCH' WHERE id = %s", (doc,))
            real_lock(conn, years, bounded)
        for act in (lambda: taxdocs.delete_w2(self.conn, doc, shown),
                    lambda: taxdocs.set_w2_owner(self.conn, doc, "primary",
                                                 shown)):
            with mock.patch.object(taxdocs, "_lock_years",
                                   lock_after_a_swap), \
                    self.assertRaises(taxdocs.W2Changed):
                act()
        self.assertEqual(len(self.docs(2024)), 1)

    def test_a_restored_box_1_past_the_bound_can_still_be_removed(self):
        self.conn.execute(
            "INSERT INTO income_documents (id, year, form, owner, payer, "
            "primary_amount, amounts) VALUES (1, 2024, 'W-2', 'primary', "
            "'ACME', 1e15, '{}')")
        self.assertTrue(self.delete(1))

    def test_a_removal_must_say_which_w2(self):
        self.store(_w2(2024, 1.0))
        doc = self.docs(2024)[0]["id"]
        for bad in (None, {}, {"year": 2024}, {"year": "x", "box1": 1.0},
                    {"year": 2024, "box1": float("inf")}):
            with self.assertRaises(ValueError):
                taxdocs.delete_w2(self.conn, doc, bad)
        self.assertEqual(len(self.docs(2024)), 1)

    def test_only_a_person_can_be_named(self):
        self.store(_w2(2024, 1.0))
        with self.assertRaises(ValueError):
            self.name(self.docs(2024)[0]["id"], "unassigned")


class ConcurrencyTests(_Db):
    def test_w2s_stored_at_the_same_moment_are_all_counted(self):
        self.conn.commit()
        tokens = [staging.put(self.conn, "taxdoc",
                              meta={"kind": "w2", "rows": []})
                  for _ in range(6)]
        self.conn.commit()
        errors = []
        start = threading.Barrier(len(tokens))

        def run(i, token):
            conn = tenancy.tenant_connect(
                self.tid(),
                f"{self.conn.info.dsn} password={self.conn.info.password}")
            try:
                start.wait()
                taxdocs.commit(conn, token, [
                    _w2(2024, 1000.0 + i, f"EMPLOYER {i}", f"•••••000{i}")])
                conn.commit()
            except Exception as e:                    # noqa: BLE001
                errors.append(e)
            finally:
                conn.close()

        threads = [threading.Thread(target=run, args=(i, t))
                   for i, t in enumerate(tokens)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.docs(2024)), 6)
        self.assertEqual(self.wages(2024)[0],
                         sum(1000.0 + i for i in range(6)))

    def test_a_w2_door_behind_a_restore_answers_busy(self):
        self.store(_w2(2024, 50000.0))
        doc = self.docs(2024)[0]["id"]
        seen = self.seen(doc)
        self.conn.commit()
        holder = self.second_conn()
        holder.execute("BEGIN")
        taxdocs._lock_years(holder, [2024], bounded=False)
        with mock.patch("oikonome.sync.reanchor.REQUEST_LOCK_WAIT_MS", 300):
            with self.assertRaises(psycopg.errors.LockNotAvailable):
                taxdocs.delete_w2(self.conn, doc, seen)
        self.conn.rollback()
        holder.rollback()
        self.assertEqual(len(self.docs(2024)), 1)


# an empty income_annual member whose header dates an archive as written
# by a build with migration 226
_CURRENT = {"income_annual.csv": "year,primary_wages,primary_wages_w2\n"}


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


class RestoreTests(_Db):
    def owners(self):
        return {r["payer"] + str(r["year"]): r["owner"]
                for r in self.conn.execute(
                    "SELECT * FROM income_documents").fetchall()}

    def test_an_archive_from_before_the_question_lands_by_the_unsaid_rule(
            self):
        restore.restore_zip(self.conn, _zip({
            "income_annual.csv":
                "year,primary_wages,source\n2021,50000.0,w2\n",
            "income_documents.csv":
                "id,year,form,payer,ein_last4,primary_amount,amounts,notes\n"
                '1,2021,W-2,ACME,6789,50000.0,"{}",\n'
                '2,2021,W-2,BIRCH,1111,30000.0,"{}",\n'}))
        self.assertEqual(self.owners(), {"ACME2021": "primary",
                                         "BIRCH2021": "unassigned"})
        # a pre-226 figure is nobody's sum: it stays as archived
        self.assertEqual(self.wages(2021), (50000.0, None))

    def test_a_household_restored_from_its_own_export_is_unchanged(self):
        restore.restore_zip(self.conn, _zip({
            "income_annual.csv":
                "year,primary_wages,primary_wages_w2,spouse_wages,"
                "spouse_wages_w2,source\n2021,80000.0,80000.0,48000.0,"
                "48000.0,w2\n",
            "income_documents.csv":
                "id,year,form,owner,payer,ein_last4,primary_amount,amounts,"
                "notes\n"
                '1,2021,W-2,primary,ACME,6789,48000.0,"{}",\n'
                '2,2021,W-2,primary,BIRCH,1111,32000.0,"{}",\n'
                '3,2021,W-2,spouse,ACME,6789,48000.0,"{""2"": 1.0}",\n'}))
        self.assertEqual(len(self.docs(2021)), 3)
        self.assertEqual(self.wages(2021), (80000.0, 48000.0))

    def test_identical_twins_survive_a_round_trip(self):
        """Two spouses at one employer with every box the same, and one
        person's two identical forms kept as a second W-2: an export of the
        household restores every copy and every figure."""
        restore.restore_zip(self.conn, _zip({
            "income_annual.csv":
                "year,primary_wages,primary_wages_w2,spouse_wages,"
                "spouse_wages_w2,source\n2021,96000.0,96000.0,48000.0,"
                "48000.0,w2\n",
            "income_documents.csv":
                "id,year,form,owner,payer,ein_last4,primary_amount,amounts,"
                "notes\n"
                '1,2021,W-2,primary,ACME,6789,48000.0,"{}",\n'
                '2,2021,W-2,primary,ACME,6789,48000.0,"{}",\n'
                '3,2021,W-2,spouse,ACME,6789,48000.0,"{}",\n'}))
        self.assertEqual(sorted(d["owner"] for d in self.docs(2021)),
                         ["primary", "primary", "spouse"])
        self.assertEqual(self.wages(2021), (96000.0, 48000.0))

    def test_a_w2_for_an_impossible_year_is_refused(self):
        restore.restore_zip(self.conn, _zip({
            "income_documents.csv":
                "id,year,form,owner,payer,ein_last4,primary_amount,amounts,"
                "notes\n"
                '1,123456,W-2,primary,ACME,6789,1.0,"{}",\n'}))
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM income_documents").fetchone()["n"], 0)

    def test_a_document_named_since_is_present_not_a_second_copy(self):
        self.store(_w2(2021, 50000.0, "ACME", None, owner="spouse"))
        doc = self.docs(2021)[0]
        restore.restore_zip(self.conn, _zip({
            "income_documents.csv":
                "id,year,form,owner,payer,ein_last4,primary_amount,amounts,"
                "notes\n"
                '9,2021,W-2,primary,ACME,,50000.0,"{""1"": 50000.0}",\n'}))
        self.assertEqual([d["id"] for d in self.docs(2021)], [doc["id"]])
        self.assertEqual(self.wages(2021), (None, 50000.0))

    def test_w2s_merged_into_a_populated_year_are_summed(self):
        self.store(_w2(2021, 50000.0))
        restore.restore_zip(self.conn, _zip({**_CURRENT,
            "income_documents.csv":
                "id,year,form,owner,payer,ein_last4,primary_amount,amounts,"
                "notes\n"
                '1,2021,W-2,primary,BIRCH,1111,30000.0,"{}",\n'}))
        self.assertEqual(self.wages(2021), (80000.0, None))

    def test_an_older_copy_of_a_replaced_w2_is_never_summed_twice(self):
        """Exported, then the same form uploaded again with box 2 read and
        answered "the same form": a merge of the old export must not add
        the old copy as a second W-2."""
        self.store(_w2(2021, 50000.0, "ACME", "•••••6789"))
        doc = self.docs(2021)[0]["id"]
        self.store({**_w2(2021, 50000.0, "ACME", "•••••6789", replaces=doc),
                    "boxes": {"1": 50000.0, "2": 6000.0}})
        restore.restore_zip(self.conn, _zip({**_CURRENT,
            "income_documents.csv":
                "id,year,form,owner,payer,ein_last4,primary_amount,amounts,"
                "notes\n"
                '1,2021,W-2,primary,ACME,6789,50000.0,'
                '"{""1"": 50000.0}",\n'}))
        self.assertEqual(self.wages(2021), (50000.0, None))
        self.assertEqual(sorted(d["owner"] for d in self.docs(2021)),
                         ["primary", "unassigned"])

    def test_a_replaced_form_beside_its_twin_is_never_summed_twice(self):
        """Two spouses' identical W-2s; the spouse's is replaced since the
        export with box 2 read. A merge of the export must not add the
        spouse's old copy back beside the new one."""
        self.store(_w2(2021, 48000.0, "ACME", "•••••6789"))
        self.store(_w2(2021, 48000.0, "ACME", "•••••6789", owner="spouse",
                       replaces=None))
        s_id = self.docs(2021)[1]["id"]
        self.store({**_w2(2021, 48000.0, "ACME", "•••••6789", owner="spouse",
                          replaces=s_id),
                    "boxes": {"1": 48000.0, "2": 6000.0}})
        restore.restore_zip(self.conn, _zip({**_CURRENT,
            "income_documents.csv":
                "id,year,form,owner,payer,ein_last4,primary_amount,amounts,"
                "notes\n"
                '1,2021,W-2,primary,ACME,6789,48000.0,"{""1"": 48000.0}",\n'
                '2,2021,W-2,spouse,ACME,6789,48000.0,"{""1"": 48000.0}",\n'}))
        self.assertEqual(self.wages(2021), (48000.0, 48000.0))

    def test_a_year_written_as_a_decimal_is_locked_too(self):
        """A "2021.0" cell lands in 2021, so 2021's lock is taken."""
        with mock.patch.object(taxdocs, "_lock_years",
                               wraps=taxdocs._lock_years) as locked:
            restore.restore_zip(self.conn, _zip({**_CURRENT,
                "income_documents.csv":
                    "id,year,form,owner,payer,ein_last4,primary_amount,"
                    "amounts,notes\n"
                    '1,2021.0,W-2,primary,ACME,6789,1.0,"{}",\n'}))
        self.assertIn(2021, set(locked.call_args.args[1]))
        self.assertEqual(self.wages(2021), (1.0, None))

    def test_twins_present_under_one_person_land_under_the_other(self):
        """The household holds the primary earner's copy of two spouses'
        identical W-2s; the archive holds both. The spouse's lands."""
        self.store(_w2(2021, 48000.0, "ACME", None))
        restore.restore_zip(self.conn, _zip({**_CURRENT,
            "income_documents.csv":
                "id,year,form,owner,payer,ein_last4,primary_amount,amounts,"
                "notes\n"
                '1,2021,W-2,spouse,ACME,,48000.0,"{""1"": 48000.0}",\n'
                '2,2021,W-2,primary,ACME,,48000.0,"{""1"": 48000.0}",\n'}))
        self.assertEqual(sorted(d["owner"] for d in self.docs(2021)),
                         ["primary", "spouse"])
        self.assertEqual(self.wages(2021), (48000.0, 48000.0))

    def test_a_restore_locks_only_its_own_years(self):
        """A person's W-2 edit of a year the archive does not hold is never
        held up by the restore, and the restore holds a handful of locks."""
        self.conn.commit()
        holder = self.second_conn()
        holder.execute("BEGIN")
        taxdocs._lock_years(holder, [2030], bounded=False)
        restore.restore_zip(self.conn, _zip({**_CURRENT,
            "income_documents.csv":
                "id,year,form,owner,payer,ein_last4,primary_amount,amounts,"
                "notes\n"
                '1,2021,W-2,primary,ACME,6789,1.0,"{}",\n'}))
        self.assertEqual(self.wages(2021), (1.0, None))
        holder.rollback()

    def test_an_unknown_person_lands_unassigned(self):
        restore.restore_zip(self.conn, _zip({**_CURRENT,
            "income_documents.csv":
                "id,year,form,owner,payer,ein_last4,primary_amount,amounts,"
                "notes\n"
                '1,2021,W-2,dog,ACME,6789,50000.0,"{}",\n'}))
        self.assertEqual(self.owners(), {"ACME2021": "unassigned"})
        self.assertIsNone(self.annual(2021))


if __name__ == "__main__":
    unittest.main()
