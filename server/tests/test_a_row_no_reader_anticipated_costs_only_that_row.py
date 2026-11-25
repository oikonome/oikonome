"""A row whose reading fails in a way no check of the restore anticipated
costs only that row, and the restore says which rows it skipped.

Each lander's own checks refuse the cells they expect to meet (a blank
required cell, text where a number belongs, an unreadable date). A cell
none of them anticipated used to raise a bare ValueError from Python's own
parser, which refused the whole archive: one odd cell in a note cost the
household its whole backup. The row is now skipped, counted under its
table, and named in the summary by its id when that is a uuid (an id
that says nothing) or by its line in the file, never by what its cells
hold; what hangs on it is left out as it is for any refused row, and what
its reading had already counted is told once, as the skip.

What still refuses the whole archive does so: the archive's own refusals
(a non-finite number among them), a database error, an error that says
the code is wrong rather than the cell (a TypeError, an AttributeError),
and a table most of whose rows fail the same way, which is no odd cell.
"""

import dataclasses
import unittest
import uuid

import psycopg

from oikonome.sync import landers, restore

from .test_restore_v2 import _zip
from .util import make_db, write_config

TXN_COLS = ["id", "account_id", "date", "amount", "name"]
OFFER_COLS = ["id", "kind", "payee", "amount", "status", "evidence"]


def _txn(tid, amount="-10"):
    return {"id": tid, "account_id": "chk", "date": "2026-03-02",
            "amount": amount, "name": f"INVENTED SHOP {tid.upper()}"}


def _offer(pid):
    return {"id": pid, "kind": "add", "payee": f"INVENTED {pid.upper()}",
            "amount": "10", "status": "pending", "evidence": "{}"}


def _archive(**over):
    tables = {
        "transactions": (TXN_COLS, [_txn("t-ok"), _txn("t-bad"),
                                    _txn("t-ok2")]),
        "transaction_notes": (["txn_id", "note"], [
            {"txn_id": "t-ok", "note": "kept"},
            {"txn_id": "t-bad", "note": "hangs on the skipped charge"},
            {"txn_id": "t-ok2", "note": "SECRET-NOTE-TEXT"}]),
        "bill_proposals": (OFFER_COLS, [
            _offer("p-ok"), _offer("p-bad"), _offer("p-ok2")]),
    }
    tables.update(over)
    return _zip(tables)


class _Patched(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def fail_on(self, table, row_is, exc):
        L = landers.LANDERS[table]
        parse = L.parse

        def picky(r, ctx, out):
            if row_is(r):
                raise exc
            return parse(r, ctx, out)
        landers.LANDERS[table] = dataclasses.replace(L, parse=picky)
        self.addCleanup(landers.LANDERS.__setitem__, table, L)

    def ids(self, sql):
        return sorted(r["id"] for r in self.conn.execute(sql).fetchall())

    def count(self, sql):
        return self.conn.execute(sql).fetchone()["n"]


class AnUnanticipatedCellCostsItsRow(_Patched):
    def test_the_rest_of_every_table_lands_and_each_skip_is_named(self):
        self.fail_on("transactions", lambda r: r["id"] == "t-bad",
                     ValueError("could not convert string to float"))
        self.fail_on("bill_proposals", lambda r: r["id"] == "p-bad",
                     ValueError("an odd cell"))
        self.fail_on("transaction_notes",
                     lambda r: r["note"] == "SECRET-NOTE-TEXT",
                     ValueError("an odd cell"))
        result, out = restore.restore_outcomes(self.conn, _archive())

        self.assertEqual(self.ids(
            "SELECT id FROM transactions WHERE id LIKE 't-%'"),
            ["t-ok", "t-ok2"])
        self.assertEqual(self.ids("SELECT id FROM bill_proposals"),
                         ["p-ok", "p-ok2"])
        # the note on the skipped charge is left out with it, as for any
        # row whose parent did not land; the note that failed is skipped
        self.assertEqual(self.ids(
            "SELECT txn_id AS id FROM transaction_notes"), ["t-ok"])
        self.assertEqual(out.refused.get(("transaction_notes", "orphaned")),
                         1)
        self.assertEqual(
            {t: n for t, (n, _) in out.skipped.items()},
            {"transactions": 1, "bill_proposals": 1,
             "transaction_notes": 1})

        notes = result["_notes"]
        said = " ".join(notes)
        self.assertIn("1 row of transactions could not be read and was "
                      "skipped (row 2)", said)
        self.assertIn("1 row of bill_proposals could not be read and was "
                      "skipped (row 2)", said)
        # a row with no id is named by its place, never by its cells
        self.assertIn("1 row of transaction_notes could not be read and "
                      "was skipped (row 3)", said)
        self.assertNotIn("SECRET-NOTE-TEXT", said)
        self.assertNotIn("could not convert", said)
        # said before anything else the restore reports
        self.assertTrue(all("skipped" in n for n in notes[:3]), notes)

    def test_the_result_carries_the_skips_apart_and_says_them_first(self):
        from oikonome.web.pages import restore_result
        self.fail_on("bill_proposals", lambda r: r["id"] == "p-bad",
                     ValueError("odd"))
        counts, _out = restore.restore_outcomes(self.conn, _archive())
        got = restore_result(counts)
        sentence = ("1 row of bill_proposals could not be read and was "
                    "skipped (row 2)")
        self.assertEqual(got["skipped"], [
            {"table": "bill_proposals", "count": 1, "sample": ["row 2"],
             "sentence": sentence}])
        self.assertEqual(got["warnings"][0], sentence)

    def test_a_clean_restore_sends_no_skips(self):
        from oikonome.web.pages import restore_result
        counts, _out = restore.restore_outcomes(self.conn, _archive())
        self.assertEqual(restore_result(counts)["skipped"], [])

    def test_the_sample_of_skipped_rows_is_bounded(self):
        many = [_offer(f"p-bad-{i:02d}") for i in range(30)]
        kept = [_offer(f"p-ok-{i:02d}") for i in range(40)]
        self.fail_on("bill_proposals", lambda r: r["id"].startswith("p-bad"),
                     ValueError("odd"))
        result, out = restore.restore_outcomes(self.conn, _archive(
            bill_proposals=(OFFER_COLS, [*kept, *many])))
        n, sample = out.skipped["bill_proposals"]
        self.assertEqual(n, 30)
        self.assertEqual(len(sample), restore._SKIP_SAMPLE)
        line = next(x for x in result["_notes"] if "bill_proposals" in x)
        self.assertIn("30 rows of bill_proposals could not be read and were "
                      "skipped", line)
        self.assertIn("and 25 more", line)
        self.assertEqual(len(self.ids("SELECT id FROM bill_proposals")), 40)

    def test_only_a_uuid_id_names_a_row(self):
        plain = str(uuid.uuid4())
        bad = {"manual:visa-0000-0000-0000-1234": False,
               "rec:dr-invented-name-therapy": False,
               "x" * 64: False, plain: True}
        self.fail_on("bill_proposals", lambda r: r["id"] in bad,
                     ValueError("odd"))
        rows = [_offer(f"p-ok-{i}") for i in range(6)] + [
            _offer(i) for i in bad]
        result, out = restore.restore_outcomes(self.conn, _archive(
            bill_proposals=(OFFER_COLS, rows)))
        line = next(x for x in result["_notes"] if "bill_proposals" in x)
        self.assertIn(f"id {plain}", line)
        for spelled in ("visa", "0000", "therapy", "x" * 64):
            self.assertNotIn(spelled, line)
        self.assertIn("row 7, row 8, row 9", line)

    def test_a_split_group_counts_its_parts_and_is_named_by_its_line(self):
        parts = ["txn_id", "line", "category", "amount", "source"]
        data = _archive(transaction_splits=(parts, [
            {"txn_id": "t-ok2", "line": 0, "category": "Food",
             "amount": "-4", "source": "user"},
            {"txn_id": "t-ok2", "line": 1, "category": "Home",
             "amount": "-6", "source": "user"},
            {"txn_id": "t-ok", "line": 0, "category": "Food",
             "amount": "-3", "source": "user"},
            {"txn_id": "t-ok", "line": 1, "category": "Home",
             "amount": "-7", "source": "user"}]))
        clean = make_db()
        self.addCleanup(clean.close)
        write_config(clean)
        _r, before = restore.restore_outcomes(clean, data)
        self.fail_on("transaction_splits", lambda g: g["txn_id"] == "t-ok",
                     ValueError("odd"))
        _r, out = restore.restore_outcomes(self.conn, data)
        self.assertEqual(out.skipped["transaction_splits"], [2, ["row 3"]])
        # the group's two parts, counted as the two rows they are, in place
        # of whatever the group's own reading would have said of them
        self.assertEqual(
            sum(n for (t, _), n in out.refused.items()
                if t == "transaction_splits")
            + out.written.get("transaction_splits", 0),
            sum(n for (t, _), n in before.refused.items()
                if t == "transaction_splits")
            + before.written.get("transaction_splits", 0))

    def test_a_stored_document_is_named_by_its_line_in_the_file(self):
        # the first row is refused while the file is read, so the failing
        # one is the reader's first but the file's second
        self.fail_on("income_uploads", lambda u: u["old"] == 2,
                     ValueError("odd"))
        cols = ["id", "uid", "kind", "filename", "stored_at"]
        _r, out = restore.restore_outcomes(self.conn, _archive(
            income_uploads=(cols, [
                {"id": "1", "uid": str(uuid.uuid4()), "kind": "no-such-kind",
                 "filename": "a.pdf", "stored_at": "2026-01-05 10:00:00"},
                {"id": "2", "uid": str(uuid.uuid4()), "kind": "document",
                 "filename": "b.pdf", "stored_at": "2026-01-05 10:00:00"}])))
        self.assertEqual(out.skipped["income_uploads"], [1, ["row 2"]])

    def test_a_value_left_empty_before_the_row_failed_is_told_once(self):
        L = landers.LANDERS["business_entity"]
        parse = L.parse

        def then_fails(r, ctx, out):
            parse(r, ctx, out)
            raise ValueError("odd")
        landers.LANDERS["business_entity"] = dataclasses.replace(
            L, parse=then_fails)
        self.addCleanup(landers.LANDERS.__setitem__, "business_entity", L)
        _r, out = restore.restore_outcomes(self.conn, _archive(
            business_entity=(["id", "name", "structure",
                              "income_tax_rate"], [
                {"id": str(uuid.uuid4()), "name": "Invented Works",
                 "structure": "llc", "income_tax_rate": "999"}])))
        self.assertEqual(out.skipped["business_entity"][0], 1)
        self.assertEqual(out.repaired, {})
        self.assertEqual(out.emptied_bound, {})
        self.assertEqual(out.refused.get(("business_entity", "malformed")),
                         1)


class AnUnreadBusinessesAccountsLandAsPersonal(_Patched):
    def archive(self, ent_row):
        return _archive(
            business_entity=(["id", "name", "structure"], [ent_row]),
            accounts=(["id", "name", "type", "entity_id"], [
                {"id": "acct-biz", "name": "Invented Biz Checking",
                 "type": "depository", "entity_id": ent_row["id"]},
                {"id": "acct-own", "name": "Invented Own",
                 "type": "depository", "entity_id": ""}]))

    def check(self, result):
        got = self.conn.execute("SELECT entity_id FROM accounts"
                                " WHERE id = 'acct-biz'").fetchone()
        self.assertIsNone(got["entity_id"])
        self.assertIn("1 account of a business that could not be read was "
                      "restored as personal", result["_notes"])

    def test_after_a_skipped_business(self):
        ent = str(uuid.uuid4())
        self.fail_on("business_entity", lambda r: r["id"] == ent,
                     ValueError("odd"))
        result, _out = restore.restore_outcomes(self.conn, self.archive(
            {"id": ent, "name": "Invented Works", "structure": "llc"}))
        self.check(result)

    def test_after_a_malformed_business(self):
        result, _out = restore.restore_outcomes(self.conn, self.archive(
            {"id": str(uuid.uuid4()), "name": "", "structure": "llc"}))
        self.check(result)

    def test_a_business_that_lands_keeps_its_accounts(self):
        ent = str(uuid.uuid4())
        result, _out = restore.restore_outcomes(self.conn, self.archive(
            {"id": ent, "name": "Invented Works", "structure": "llc"}))
        got = self.conn.execute("SELECT entity_id FROM accounts"
                                " WHERE id = 'acct-biz'").fetchone()
        self.assertEqual(str(got["entity_id"]), ent)
        self.assertFalse(any("as personal" in n
                             for n in result.get("_notes", [])))


class WhatStillRefusesTheArchive(_Patched):
    def nothing_landed(self):
        self.assertEqual(self.count(
            "SELECT count(*) AS n FROM transactions WHERE id LIKE 't-%'"), 0)
        self.assertEqual(self.count(
            "SELECT count(*) AS n FROM bill_proposals"), 0)

    def test_a_non_finite_number_refuses_the_archive(self):
        with self.assertRaises(restore._ArchiveRefused):
            restore.restore_outcomes(self.conn, _archive(transactions=(
                TXN_COLS, [_txn("t-ok"), _txn("t-nan", amount="NaN")])))
        self.nothing_landed()

    def test_an_archive_refusal_raised_from_a_row_refuses_the_archive(self):
        self.fail_on("bill_proposals", lambda r: r["id"] == "p-bad",
                     restore._ArchiveRefused("not a genuine export"))
        with self.assertRaises(restore._ArchiveRefused):
            restore.restore_outcomes(self.conn, _archive())
        self.nothing_landed()

    def test_an_erased_household_still_stops_the_restore(self):
        self.fail_on("bill_proposals", lambda r: True,
                     restore.HouseholdBeingDeleted(restore.ERASED_MSG))
        with self.assertRaises(restore.HouseholdBeingDeleted):
            restore.restore_outcomes(self.conn, _archive())
        self.nothing_landed()

    def test_a_database_error_aborts_the_restore(self):
        L = landers.LANDERS["bill_proposals"]

        def broken(r, ctx, out):
            ctx.conn.execute("SELECT no_such_column FROM bill_proposals")
        landers.LANDERS["bill_proposals"] = dataclasses.replace(L,
                                                                parse=broken)
        self.addCleanup(landers.LANDERS.__setitem__, "bill_proposals", L)
        with self.assertRaises(psycopg.Error):
            restore.restore_outcomes(self.conn, _archive())
        self.nothing_landed()

    def test_a_type_error_is_the_codes_and_aborts_the_restore(self):
        self.fail_on("bill_proposals", lambda r: r["id"] == "p-bad",
                     TypeError("unsupported operand"))
        with self.assertRaises(TypeError):
            restore.restore_outcomes(self.conn, _archive())
        self.nothing_landed()

    def test_most_of_a_table_failing_refuses_the_archive(self):
        rows = [_offer(f"p-{i}") for i in range(6)]
        self.fail_on("bill_proposals", lambda r: r["id"] in
                     {"p-0", "p-1", "p-2", "p-3"}, ValueError("odd"))
        with self.assertRaises(restore._ArchiveRefused) as e:
            restore.restore_outcomes(self.conn, _archive(
                bill_proposals=(OFFER_COLS, rows)))
        self.assertIn("the backup's bill_proposals could not be read",
                      str(e.exception))
        self.nothing_landed()

    def test_a_few_failing_rows_of_a_small_table_do_not(self):
        rows = [_offer(f"p-{i}") for i in range(5)]
        self.fail_on("bill_proposals", lambda r: r["id"] in {"p-0", "p-1"},
                     ValueError("odd"))
        restore.restore_outcomes(self.conn, _archive(
            bill_proposals=(OFFER_COLS, rows)))
        self.assertEqual(len(self.ids("SELECT id FROM bill_proposals")), 3)

    def test_a_programming_error_aborts_the_restore(self):
        self.fail_on("bill_proposals", lambda r: r["id"] == "p-bad",
                     AttributeError("'NoneType' object has no attribute"))
        with self.assertRaises(AttributeError):
            restore.restore_outcomes(self.conn, _archive())
        self.nothing_landed()

    def test_a_missing_cell_key_is_not_taken_for_an_odd_cell(self):
        # a KeyError cannot be told apart from a lookup the code got
        # wrong, so it is not read as one row's cell
        self.fail_on("bill_proposals", lambda r: r["id"] == "p-bad",
                     KeyError("no_such_column"))
        with self.assertRaises(KeyError):
            restore.restore_outcomes(self.conn, _archive())
        self.nothing_landed()

    def test_a_lander_naming_no_parent_kind_is_a_defect_not_a_cell(self):
        with self.assertRaises(LookupError) as e:
            landers.Ctx().holds(self.conn, "no-such-kind", "x")
        self.assertNotIsInstance(e.exception, ValueError)
        with self.assertRaises(LookupError) as e:
            restore._Outcomes().refuse("bills", "no-such-outcome")
        self.assertNotIsInstance(e.exception, ValueError)


if __name__ == "__main__":
    unittest.main()
