"""Two or more cards whose numbers share the last four stay apart.

A multi-account file named its split accounts by the plain slug of the
source name, so a card named by its full number kept that number in its
id. Migration 199 (and restore, for an older archive) moves each onto a
masked id and records the move from the old id with its number cut, so
two cards ending in the same four digits leave one record key naming two
accounts, and their masked source names are equal too.

A re-import of the same file must still put every card's rows back on that
card's own account: the earlier rows are found by recomputing their ids
under the full old id, which the file spells again. Following the shared
key to whichever account comes first would file one card's whole history
onto the other's. And every card moves, however many share the last four:
none may keep its number in its id.
"""
import io
import re
import tempfile
import unittest
import zipfile
from unittest import mock

from oikonome.sync import base, export, qifimport, restore

from .test_split_account_ids_follow_where_the_rows_came_from import (
    MIGRATION, _legacy_ensure, _qif)
from .util import make_db, write_config

LONG_DIGITS = re.compile(r"\d{8,}")

A = "Visa 1111111111110123"          # invented numbers, same last four
B = "Visa 2222222222220123"
C = "Visa 3333333333330123"
A_ROWS = [(4, -61.20, "SPRINGFIELD GROCER"), (5, -5.00, "SERVICE FEE")]
B_ROWS = [(6, -23.00, "NORTHWIND FUEL"), (7, -9.99, "CONTOSO STREAM"),
          (8, -1.00, "FABRIKAM PARKING")]
C_ROWS = [(9, -2.00, "CONTOSO KIOSK")]
NEW = {A: (20, -3.00, "SPRINGFIELD BAKERY"),
       B: (21, -4.00, "NORTHWIND TOLL"),
       C: (22, -6.00, "FABRIKAM BOOKS")}
ROWS = {A: A_ROWS, B: B_ROWS, C: C_ROWS}


def _legacy_import(conn, *cards):
    with mock.patch.object(qifimport, "ensure_split_account",
                           _legacy_ensure):
        qifimport.import_qif(conn, "chk", _qif(*((c, ROWS[c])
                                                 for c in cards)))


def _reimport_with_one_new_row_each(conn, *cards):
    qifimport.import_qif(conn, "chk", _qif(*((c, ROWS[c] + [NEW[c]])
                                             for c in cards)))


def _export(conn) -> bytes:
    with tempfile.TemporaryFile() as fh:
        export.build_zip(conn, out=fh)
        fh.seek(0)
        return fh.read()


class SameLastFourTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _fresh(self):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        return conn

    def _by_card(self, conn):
        """card -> (its account, row count, payees on it), the card being
        known by a payee only its rows carry."""
        out = {}
        for card, rows in ROWS.items():
            r = conn.execute(
                "SELECT account_id FROM transactions WHERE name = %s",
                (rows[0][2],)).fetchone()
            if r is None:
                continue
            aid = r["account_id"]
            names = {x["name"] for x in conn.execute(
                "SELECT name FROM transactions WHERE account_id = %s",
                (aid,)).fetchall()}
            out[card] = (aid, len(names), names)
        return out

    def _assert_apart(self, conn, cards, *, with_new):
        got = self._by_card(conn)
        accounts = {got[c][0] for c in cards}
        self.assertEqual(len(accounts), len(cards), got)
        for c in cards:
            want = {p for _d, _a, p in ROWS[c]}
            if with_new:
                want.add(NEW[c][2])
            self.assertEqual(got[c][2], want, (c, got))
        total = conn.execute(
            "SELECT count(*) AS n FROM transactions"
            " WHERE account_id LIKE 'manual:qif-%'").fetchone()["n"]
        self.assertEqual(total, sum(len(ROWS[c]) + with_new for c in cards))

    def _assert_no_number(self, conn):
        for sql in ("SELECT id AS v FROM accounts",
                    "SELECT account_id AS v FROM transactions",
                    "SELECT old_account_id AS v FROM account_adoptions",
                    "SELECT account_id AS v FROM account_adoptions"):
            for r in conn.execute(sql).fetchall():
                self.assertIsNone(LONG_DIGITS.search(r["v"] or ""),
                                  (sql, r["v"]))

    def test_two_cards_reimported_each_get_only_their_own_new_row(self):
        _legacy_import(self.conn, A, B)
        self.conn.execute(MIGRATION.read_text())
        self._assert_no_number(self.conn)
        self._assert_apart(self.conn, (A, B), with_new=False)
        _reimport_with_one_new_row_each(self.conn, A, B)
        self._assert_apart(self.conn, (A, B), with_new=True)
        # and the same file once more adds nothing
        _reimport_with_one_new_row_each(self.conn, A, B)
        self._assert_apart(self.conn, (A, B), with_new=True)
        self._assert_no_number(self.conn)

    def test_three_cards_all_move_and_each_keeps_its_rows(self):
        _legacy_import(self.conn, A, B, C)
        self.conn.execute(MIGRATION.read_text())
        self.conn.execute(MIGRATION.read_text())          # idempotent
        self._assert_no_number(self.conn)
        ids = sorted(r["id"] for r in self.conn.execute(
            "SELECT id FROM accounts WHERE id LIKE 'manual:qif-%'"))
        self.assertEqual(len(ids), 3, ids)
        for aid in ids:
            # a suffix spelled in letters, never a digit beyond the four
            self.assertRegex(aid, r"^manual:qif-visa-0123(-[g-pa-f]{8})?$")
        _reimport_with_one_new_row_each(self.conn, A, B, C)
        self._assert_apart(self.conn, (A, B, C), with_new=True)
        self._assert_no_number(self.conn)

    def test_restore_moves_an_older_archive_onto_the_ids_the_migration_names(
            self):
        _legacy_import(self.conn, A, B, C)
        data = _export(self.conn)                         # before the move
        dest = self._fresh()
        restore.restore_zip(dest, data)
        self.conn.execute(MIGRATION.read_text())
        q = ("SELECT id FROM accounts WHERE id LIKE 'manual:qif-%'"
             " ORDER BY id")
        self.assertEqual([r["id"] for r in dest.execute(q)],
                         [r["id"] for r in self.conn.execute(q)])
        self.assertEqual(self._by_card(dest), self._by_card(self.conn))
        self._assert_no_number(dest)
        _reimport_with_one_new_row_each(dest, A, B, C)
        self._assert_apart(dest, (A, B, C), with_new=True)

    def test_export_restore_reimport_keeps_two_cards_apart(self):
        _legacy_import(self.conn, A, B)
        self.conn.execute(MIGRATION.read_text())
        data = _export(self.conn)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for member in ("accounts.csv", "account_adoptions.csv",
                           "transactions.csv"):
                text = z.read(member).decode("utf-8")
                for card in (A, B):
                    self.assertNotIn(card.split()[1], text, member)
        dest = self._fresh()
        restore.restore_zip(dest, data)
        self._assert_no_number(dest)
        self._assert_apart(dest, (A, B), with_new=False)
        _reimport_with_one_new_row_each(dest, A, B)
        self._assert_apart(dest, (A, B), with_new=True)
        self._assert_no_number(dest)

    def test_an_older_archive_restored_after_the_move_lands_on_each_card(
            self):
        """The archive still names both cards by their full numbers, and
        this household already moved them: each archived account is filed
        onto the account holding its own rows, not onto a third one."""
        _legacy_import(self.conn, A, B)
        data = _export(self.conn)                         # before the move
        self.conn.execute(MIGRATION.read_text())
        before = self._by_card(self.conn)
        restore.restore_zip(self.conn, data)
        self._assert_no_number(self.conn)
        self.assertEqual(self._by_card(self.conn), before)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM accounts WHERE id LIKE 'manual:qif-%'"
        ).fetchone()["n"], 2)
        _reimport_with_one_new_row_each(self.conn, A, B)
        self._assert_apart(self.conn, (A, B), with_new=True)

    def test_an_unmatched_shared_key_is_not_followed(self):
        """A file whose lines match no stored row of either card says
        nothing about which card it is: the shared key is not followed,
        so neither moved card is picked on the record's word alone."""
        _legacy_import(self.conn, A, B)
        self.conn.execute(MIGRATION.read_text())
        me = base._split_source(A)
        legacy = base.split_legacy_id("qif", A)
        unrelated = qifimport._lines(qifimport.parse_qif(
            _qif((A, [(25, -7.00, "CONTOSO DINER")]))))
        self.assertIsNone(base._adopted_split_account(
            self.conn, [legacy], "qif", me, unrelated))
        own = qifimport._lines(qifimport.parse_qif(_qif((A, A_ROWS))))
        self.assertEqual(
            base._adopted_split_account(self.conn, [legacy], "qif", me, own),
            self._by_card(self.conn)[A][0])

if __name__ == "__main__":
    unittest.main()
