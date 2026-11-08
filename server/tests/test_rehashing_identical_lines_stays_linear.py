"""Proving a file row's id before it is moved off a numbered account
costs a bounded number of hashes per row, however many identical lines
the account or an archive holds.

The proof hashes each possible hash input of the row's line, one per
occurrence up to the number of identical lines beside it. Done row by
row, N identical lines cost N times N hashes: an archive of a few
thousand crafted rows of one line kept the web process hashing for
seconds before a row landed, and a hundred thousand for hours. The
inputs are the same for every row of one line, so they are hashed once
per line, and the occurrence a proof walks is clamped to what a real
statement repeats; rows past the clamp are keyed instead and counted.
"""
import datetime as dt
import hashlib
import secrets
import tempfile
import unittest
import zipfile
from unittest import mock

from oikonome.sync import base, csvimport, export, restore

from .test_reimport_into_a_rekeyed_numbered_account_adds_nothing import (
    OLD, _seed)
from .util import make_db, write_config

N = 1200


def _counted(fn):
    calls = [0]
    real = hashlib.sha1

    def sha1(*a, **k):
        calls[0] += 1
        return real(*a, **k)
    with mock.patch.object(hashlib, "sha1", sha1):
        fn()
    return calls[0]


class IdenticalLines(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        _seed(self.conn, OLD, "Everyday Checking …1234")
        # one line, N times, under ids no import minted
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name)"
            " SELECT 'csv:' || x, %s, %s, -1.00, 'SAME LINE'"
            "   FROM unnest(%s::text[]) AS x",
            (OLD, dt.date(2026, 3, 10),
             [secrets.token_hex(20) for _ in range(N)]))

    def test_rehash_of_identical_lines_is_linear(self):
        new = base.renumbered_manual_id(OLD)
        got = {}
        with self.assertLogs("oikonome.sync", level="WARNING"):
            n = _counted(lambda: got.setdefault(
                "r", base.rehash_file_rows(self.conn, OLD, new)))
        self.assertEqual(got["r"], (0, N, 0))
        self.assertLess(n, 20 * N)

    def test_restore_planning_of_identical_lines_is_linear(self):
        with tempfile.TemporaryFile() as fh:
            export.build_zip(self.conn, out=fh)
            fh.seek(0)
            z = zipfile.ZipFile(fh)
            dest = make_db()
            self.addCleanup(dest.close)
            n = _counted(lambda: restore._plan_rehashed_txns(dest, z))
        self.assertLess(n, 20 * N)

    def test_key_pass_over_identical_lines_is_linear(self):
        # rows whose names carry a cut number are what the key pass proves
        self.conn.execute(
            "UPDATE transactions SET name = 'CARD …1111 PAYMENT'"
            " WHERE account_id = %s", (OLD,))
        self.conn.execute(
            "UPDATE accounts SET name = 'x' WHERE id = %s", (OLD,))
        n = _counted(lambda: base.key_file_import_ids(self.conn))
        self.assertLess(n, 20 * N)


class ManyRealIdenticalLines(unittest.TestCase):
    """Real identical lines past the clamp: those the proof reaches move,
    the rest are keyed and counted, and nothing is lost."""

    def test_lines_past_the_clamp_are_keyed_and_counted(self):
        conn = make_db()
        write_config(conn)
        self.addCleanup(conn.close)
        _seed(conn, OLD, "Everyday Checking …1234")
        lines = base.FILE_ROW_MAX_OCCURRENCES + 10
        csv = "Date,Description,Amount\n" + (
            "2026-03-10,PARKING METER,-0.25\n" * lines)
        csvimport.import_csv(conn, OLD, csv, {
            "date": "Date", "amount": "Amount", "name": "Description"})
        new = base.renumbered_manual_id(OLD)
        with self.assertLogs("oikonome.sync", level="WARNING"):
            moved, keyed, left = base.rehash_file_rows(conn, OLD, new)
        self.assertEqual(left, 0)
        self.assertEqual(moved + keyed, lines)
        self.assertEqual(moved, base.FILE_ROW_MAX_OCCURRENCES + 2)


if __name__ == "__main__":
    unittest.main()
