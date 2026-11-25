"""A card number whose groups a tab or a no-break space joins is cut from
stored text, as one joined by a space is.

A spreadsheet cell joins a card number's groups with a tab and a
statement with a no-break space. Migration 215 cuts such numbers from
every column the earlier card-number cuts cover (file rows, merchant
names, bills and what names them, offers and the activity log, bill keys
and log names), with the grammar sync/card_number.py defines, selecting
only text where such a separator joins the groups: clean text runs none
of the cut's statements, and a second run writes nothing. Every number
here is invented.
"""
import datetime as dt
import json
import re
import unittest
import uuid
from unittest import mock

from oikonome.db import migrate
from oikonome.engine import bills, budget
from oikonome.sync import card_number

from .card_number_fixture import TABBED
from .util import TODAY, add_bill, make_db, write_config

STEP = "215_card_numbers_joined_by_any_whitespace_are_cut.sql"
TAB = "ACME 4111\t1111\t1111\t1111"
NBSP = "VISA 4111\u00a01111\u00a01111\u00a01111"
CUT = "ACME \u20261111"
NBSP_CUT = "VISA \u20261111"
CLEAN = "Fabrikam \u20260002"


def _step(conn):
    migrate.PYTHON_STEPS[STEP](conn)


class _Db(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)

    def _file_row(self, rid, text):
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name,"
            " merchant_name, removed, raw) VALUES (%s, 'chk', '2026-03-10',"
            " -50, %s, %s, 0, %s::jsonb)",
            (rid, text, text, json.dumps({"memo": text})))

    def _offer(self, pid, kind, payee, evidence):
        self.conn.execute(
            "INSERT INTO bill_proposals (id, kind, payee, amount, evidence,"
            " status) VALUES (%s,%s,%s,12.99,%s::jsonb,'pending')",
            (pid, kind, payee, json.dumps(evidence)))

    def _log(self, label):
        self.conn.execute(
            "INSERT INTO activity_log (actor, kind, action, target, label,"
            " summary) VALUES ('sam@example.test', 'bill', 'edited', %s,"
            " %s, %s)", (label, label, f"edited the bill {label}"))

    def _stored_bill(self, payee, key=None):
        """A detected bill as an older version stored it, name and key
        whole: written past save_bill, which cuts what it writes now."""
        held = f"placeholder {uuid.uuid4().hex[:8]}"
        add_bill(self.conn, held, 12.99,
                 next_due=TODAY + dt.timedelta(days=10))
        self.conn.execute(
            "UPDATE bills SET id = %s, payee = %s, merchant = %s"
            " WHERE payee = %s", (bills._slug(payee), payee, key, held))
        return bills._slug(payee)

    def _seed(self, text, tag):
        self._file_row(f"csv:{tag}", text)
        self.conn.execute("INSERT INTO import_batches (id, source,"
                          " account_id, filename) VALUES (%s, 'csv', 'chk',"
                          " %s)", (f"b-{tag}", text + ".csv"))
        self.conn.execute("INSERT INTO merchants (name) VALUES (%s)",
                          (text + " merchant",))
        self._stored_bill(text + " bill", text.lower())
        self._offer(f"prop:add-{tag}", "add", text + " offer",
                    {"description": text})
        self._log(text + " label")

    def _texts(self):
        q = self.conn.execute
        out = []
        for r in q("SELECT name, merchant_name, raw->>'memo' AS memo"
                   " FROM transactions WHERE id LIKE 'csv:%'"):
            out += [r["name"], r["merchant_name"], r["memo"]]
        out += [r["filename"] for r in q("SELECT filename FROM import_batches")]
        out += [r["name"] for r in q("SELECT name FROM merchants")]
        out += [x for r in q("SELECT id, payee, merchant FROM bills")
                for x in (r["id"], r["payee"], r["merchant"])]
        out += [x for r in q("SELECT payee, evidence->>'description' AS d"
                              " FROM bill_proposals")
                for x in (r["payee"], r["d"])]
        out += [x for r in q("SELECT label, summary, target FROM activity_log")
                for x in (r["label"], r["summary"], r["target"])]
        return out


class WhitespaceJoinedNumbersAreCut(_Db):
    def test_every_covered_column_is_cut_for_a_tab_and_a_no_break_space(self):
        self._seed(TAB, "tab")
        self._seed(NBSP, "nbsp")
        before = self._texts()
        self.assertTrue(any(card_number.holds_number(t) for t in before))
        _step(self.conn)
        after = self._texts()
        self.assertEqual([t for t in after if card_number.holds_number(t)],
                         [])
        self.assertIn(CUT, after)
        self.assertIn(NBSP_CUT, after)
        # the bill moved onto its cut name's id
        self.assertIsNotNone(bills.get_bill(self.conn, NBSP_CUT + " bill"))

    def test_a_bill_moves_with_the_settings_that_name_it(self):
        self._stored_bill(TAB)
        with budget.config_txn(self.conn) as cfg:
            cfg["disabled_bills"] = [TAB]
        _step(self.conn)
        self.assertEqual([r["payee"] for r in self.conn.execute(
            "SELECT payee FROM bills")], [CUT])
        self.assertEqual(budget.load_config(self.conn)["disabled_bills"],
                         [CUT])

    def test_clean_text_is_left_and_a_second_run_writes_nothing(self):
        self._seed(CLEAN, "clean")
        clean = self._texts()
        self.assertEqual(migrate.cut_whitespace_joined_numbers(self.conn), 0)
        self.assertEqual(self._texts(), clean)
        self._seed(TAB, "tab")
        _step(self.conn)
        once = sorted(map(str, self._texts()))
        self.assertEqual(migrate.cut_whitespace_joined_numbers(self.conn), 0)
        self.assertEqual(sorted(map(str, self._texts())), once)


class CleanTextCostsOneScanPerColumn(_Db):
    """206's statements join merchants to their rules; without planner
    statistics (a database restored a moment ago) that join is walked row
    by row. Text holding no such number must never reach them."""

    def test_clean_text_runs_none_of_the_moved_statements(self):
        self._seed(CLEAN, "clean")

        def never(_sql):
            raise AssertionError("206's statements ran over clean text")

        with mock.patch.object(migrate, "_sql_statements", never):
            self.assertEqual(migrate.cut_whitespace_joined_numbers(self.conn), 0)

    def test_every_text_the_moved_statements_select_by_is_probed(self):
        """The probe may skip the statements only if it reads every column
        they pick rows by."""
        sql = (migrate.HERE / "migrations" / migrate._MOVES_206).read_text()
        picked = set()
        for arg in re.findall(
                r"pg_temp\.(?:may_hold_number|is_cut)\(([^()]*)\)", sql):
            arg = arg.strip()
            if arg in ("v", "v text"):       # the helpers' own parameter
                continue
            picked.add(re.sub(r"^[a-z0-9_]+\.", "", arg))
        self.assertTrue(picked)
        probed = {expr for _, expr in migrate._SELECTED_BY_206}
        self.assertEqual(picked - probed, set())


class TheSelectorReadsEveryWhitespaceJoin(_Db):
    def test_it_picks_every_tabbed_case_plain_and_inside_a_raw_document(self):
        pat = migrate._odd_whitespace_selector()
        for text in TABBED:
            r = self.conn.execute(
                "SELECT %s::text ~ %s AS plain,"
                "       jsonb_build_object('memo', %s::text)::text ~ %s"
                "       AS raw", (text, pat, text, pat)).fetchone()
            self.assertEqual((r["plain"], r["raw"]), (True, True), text)

    def test_it_leaves_what_the_frozen_cut_read(self):
        pat = migrate._odd_whitespace_selector()
        for text in ("Visa 4000 1234 5678 9010", "Amex 3400-000000-00009",
                     "4000123456789010", CLEAN):
            self.assertFalse(self.conn.execute(
                "SELECT %s::text ~ %s AS hit", (text, pat)
            ).fetchone()["hit"], text)


if __name__ == "__main__":
    unittest.main()
