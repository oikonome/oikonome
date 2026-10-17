"""Pairing an archive's rows on a moved account with the live rows they
were carried onto costs memory bounded by what the household holds, not
by what the archive claims; and a free-text cell restores no longer than
the API would ever have stored it.

Before any row lands, a restore pairs each archived charge on an account
this household moved with the live twin the reconnect guard carried it
onto. A restore archive is accepted from any household owner, and one
member may declare millions of rows while the ZIP is a few hundred KB on
the wire — one repeated line deflates to almost nothing. Holding every
such row, its id and its name tokens before pairing ran to gigabytes in
the web process every household shares. The live rows of the moved
accounts are already in the database and bounded there, so they are what
is indexed; the archive is streamed past them. An archive claiming far
more rows on a moved account than any real one carries is refused like
any other implausible member.

A note is free text the API caps when it is written; a restore that
stored a longer one would hold a value no edit could have produced, and
the next edit would silently cut it. Restore cuts it at the same length.
"""

import io
import tracemalloc
import unittest
import zipfile

from oikonome.sync import adopt, csvimport, restore
from oikonome.web import data

from .util import make_db, write_config

CSV = """Date,Description,Amount
2026-03-10,SAFEWAY STORE,-42.50
2026-03-12,CORNER BAKERY,-7.25
"""
MAPPING = {"date": "Date", "amount": "Amount", "name": "Description"}
HEADER = "id,account_id,date,amount,name,merchant_name\n"


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


def _many_rows(n: int, day: str, amount) -> str:
    # distinct ids and names of several words, none held by the household;
    # half share the live charge's day and amount, so the planner has to
    # weigh each of them against it and none may pair (no word in common)
    return HEADER + "".join(
        (f"gone-{i:07d},chk,{day},{amount}," if i % 2 else
         f"gone-{i:07d},chk,2026-0{1 + i % 9}-{1 + i % 28:02d},"
         f"-{1 + i % 997}.{i % 100:02d},")
        + f"GROCERY OUTLET NUMBER {i} NORTH SIDE MARKET,Grocery Outlet {i}\n"
        for i in range(n))


class MovedRowPairingIsBounded(unittest.TestCase):

    def setUp(self):
        a = self.a = make_db()
        self.addCleanup(a.close)
        write_config(a)
        csvimport.import_csv(a, "chk", CSV, MAPPING)
        item = a.execute("SELECT item_id FROM accounts WHERE id = 'chk'"
                         ).fetchone()["item_id"]
        self.assertTrue(adopt._rewrite_account_id(a, "chk", "plaid-chk",
                                                  item))
        a.execute("INSERT INTO account_adoptions (account_id,"
                  " old_account_id, item_id, cutoff_date)"
                  " VALUES ('plaid-chk', 'chk', %s, '2026-03-12')", (item,))
        row = a.execute(
            "SELECT id, amount FROM transactions WHERE name = 'SAFEWAY STORE'"
        ).fetchone()
        self.safeway, self.amount = row["id"], row["amount"]
        self.assertTrue(adopt._rewrite_txn_id(a, self.safeway, "plaid-t1"))

    def _plan(self, archive: bytes):
        z = zipfile.ZipFile(io.BytesIO(archive))
        token = restore._MOVED_ACCOUNTS.set(
            restore._plan_moved_accounts(self.a))
        try:
            return restore._plan_moved_txns(self.a, z)
        finally:
            restore._MOVED_ACCOUNTS.reset(token)

    def test_many_archived_rows_on_a_moved_account_plan_in_bounded_memory(
            self):
        n = 90_000
        # the carried twin sits among them, so pairing still has to work
        body = _many_rows(n, "2026-03-10", self.amount) + (
            f"{self.safeway},chk,2026-03-10,{self.amount},SAFEWAY STORE,\n")
        archive = _zip({"transactions.csv": body})
        self.assertLess(len(archive), 4 * 1024 * 1024)
        tracemalloc.start()
        try:
            out = self._plan(archive)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(out, {self.safeway: "plaid-t1"})
        # holding every candidate took ~130 MB at this size and grew with
        # the member; the planner now holds a bounded batch of them
        self.assertLess(peak, 16 * 1024 * 1024, f"peak {peak:,} bytes")

    def test_an_archive_with_implausibly_many_rows_on_a_moved_account_is_refused(
            self):
        old = restore._MOVED_ROWS_FLOOR
        restore._MOVED_ROWS_FLOOR = 1_000
        self.addCleanup(setattr, restore, "_MOVED_ROWS_FLOOR", old)
        # one repeated line: kilobytes on the wire, any number of rows
        body = HEADER + "tx1,chk,2026-01-01,1,alpha bravo charlie,\n" * 5_000
        with self.assertRaises(ValueError) as e:
            self._plan(_zip({"transactions.csv": body}))
        self.assertIn("transactions.csv", str(e.exception))
        # and through the restore door, before any row lands
        before = self.a.execute("SELECT count(*) AS n FROM transactions"
                                ).fetchone()["n"]
        with self.assertRaises(ValueError):
            restore.restore_zip(self.a, _zip({"transactions.csv": body}))
        self.assertEqual(self.a.execute(
            "SELECT count(*) AS n FROM transactions").fetchone()["n"], before)

    def test_rows_on_an_account_nobody_moved_are_not_counted(self):
        old = restore._MOVED_ROWS_FLOOR
        restore._MOVED_ROWS_FLOOR = 1_000
        self.addCleanup(setattr, restore, "_MOVED_ROWS_FLOOR", old)
        body = HEADER + "".join(
            f"o{i},other,2026-01-01,1,alpha,\n" for i in range(5_000))
        self.assertEqual(self._plan(_zip({"transactions.csv": body})), {})


class RestoredNotesKeepTheApiLimit(unittest.TestCase):

    def setUp(self):
        a = self.a = make_db()
        self.addCleanup(a.close)
        write_config(a)
        csvimport.import_csv(a, "chk", CSV, MAPPING)
        self.tid = a.execute("SELECT id FROM transactions ORDER BY id"
                             ).fetchone()["id"]

    def test_an_overlong_note_is_cut_to_what_the_api_stores(self):
        long_note = "n" * (data.NOTE_MAX + 500)
        archive = _zip({"transaction_notes.csv":
                        f"txn_id,note\n{self.tid},{long_note}\n"})
        with self.assertLogs("oikonome.sync.restore", "WARNING") as logs:
            restore.restore_zip(self.a, archive)
        note = self.a.execute("SELECT note FROM transaction_notes"
                              " WHERE txn_id = %s", (self.tid,)
                              ).fetchone()["note"]
        self.assertEqual(note, long_note[:data.NOTE_MAX])
        self.assertTrue(any("note" in m for m in logs.output))

    def test_a_note_within_the_limit_restores_whole(self):
        archive = _zip({"transaction_notes.csv":
                        f"txn_id,note\n{self.tid},birthday cake\n"})
        restore.restore_zip(self.a, archive)
        self.assertEqual(self.a.execute(
            "SELECT note FROM transaction_notes WHERE txn_id = %s",
            (self.tid,)).fetchone()["note"], "birthday cake")


if __name__ == "__main__":
    unittest.main()
