"""A file row's stand-in note keeps naming its feed account wherever the
account goes.

The note on a file-imported row names the bank-feed account whose own copy
of the charge was set aside. A re-link moves that account to a new id, and
a restore lands it in another household that has no record of the move.
Either way the note must still lead to an account that exists, or undoing
the import deletes the only copy of the charge.
"""
import csv
import datetime as dt
import io
import json
import unittest
import zipfile
from pathlib import Path

from oikonome.db import tenancy
from oikonome.sync import adopt, base, restore

from .util import TEST_DB, _admin_dsn, make_db

MIGRATION = (Path(base.__file__).resolve().parents[1] / "db" / "migrations"
             / "172_stand_in_notes_name_the_adopted_account.sql")
DAY = dt.date(2025, 5, 12)


def _zip(tables):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for tbl, rows in tables.items():
            cols = sorted({c for r in rows for c in r})
            s = io.StringIO()
            w = csv.DictWriter(s, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow(r)
            z.writestr(f"{tbl}.csv", s.getvalue())
    return buf.getvalue()


def _note(conn, tid="csv:one"):
    r = conn.execute("SELECT raw FROM transactions WHERE id = %s",
                     (tid,)).fetchone()
    return r["raw"][base.STOOD_IN_KEY]["accounts"]


def _relied(conn, tid="csv:one"):
    return tid in base.relied_on_by_feed(conn, [tid])


def _relink(conn, old_item, old_acct, new_acct):
    conn.execute(
        "INSERT INTO items (id, aggregator, institution_id,"
        " institution_name, status, access_token)"
        " VALUES ('new-item', 'plaid', 'ins_9', 'Sample Bank', 'ok',"
        " 'live-token')")
    out = adopt.adopt_accounts(
        conn, item_id="new-item", institution_id="ins_9",
        institution_name="Sample Bank",
        incoming=[{"account_id": new_acct, "mask": "4321",
                   "type": "depository"}])
    assert [o["old_account_id"] for o in out] == [old_acct], out


class AdoptionCarriesTheNoteTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_id,"
            " institution_name, status) VALUES ('old-item', 'plaid',"
            " 'ins_9', 'Sample Bank', 'restored')")
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, mask) VALUES"
            " ('feed-old', 'old-item', 'Checking', 'depository', '4321'),"
            " ('filechk', NULL, 'Checking (file)', 'depository', NULL)")
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name,"
            " pending, removed, raw) VALUES ('csv:one', 'filechk', %s, 18.0,"
            " 'MAPLE DINER', 0, 0, %s::jsonb)",
            (DAY, json.dumps({base.STOOD_IN_KEY: {
                "source": "plaid", "accounts": ["feed-old"]}})))

    def test_a_relink_renames_the_account_inside_the_note(self):
        _relink(self.conn, "old-item", "feed-old", "feed-new")
        self.assertEqual(_note(self.conn), ["feed-new"])
        # the adoption record is not part of an archive: the note alone
        # has to lead to the account
        self.conn.execute("DELETE FROM account_adoptions")
        self.assertTrue(_relied(self.conn))

    def test_the_upgrade_renames_notes_written_before_the_relink_did(self):
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type) VALUES"
            " ('feed-new', 'old-item', 'Checking', 'depository')")
        self.conn.execute(
            "INSERT INTO account_adoptions (account_id, old_account_id,"
            " item_id) VALUES ('feed-new', 'feed-old', 'old-item')")
        self.conn.execute("DELETE FROM accounts WHERE id = 'feed-old'")
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            admin.execute(MIGRATION.read_text())
        finally:
            admin.close()
        self.assertEqual(_note(self.conn), ["feed-new"])


class RestoreCarriesTheNoteTests(unittest.TestCase):
    """The archive holds the feed account under its current id and a note
    naming the id it had before a re-link; the adoption record that tied
    the two together is not in the archive."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def _restore(self, noted):
        restore.restore_zip(self.conn, _zip({
            "items": [{"id": "feed-item", "aggregator": "plaid",
                       "institution_id": "ins_9",
                       "institution_name": "Sample Bank",
                       "status": "ok"}],
            "accounts": [
                {"id": "feed-now", "item_id": "feed-item",
                 "name": "Checking", "type": "depository",
                 "subtype": "checking", "mask": "4321"},
                {"id": "filechk", "item_id": "", "name": "Checking (file)",
                 "type": "depository", "subtype": "checking", "mask": ""}],
            "transactions": [
                {"id": "csv:one", "account_id": "filechk",
                 "date": str(DAY), "amount": "18.00", "name": "MAPLE DINER",
                 "raw": json.dumps({base.STOOD_IN_KEY: {
                     "source": "plaid", "accounts": noted}})}]}))

    def test_a_note_naming_an_id_the_archive_lost_is_still_kept(self):
        self._restore(["feed-before"])
        self.assertEqual(_note(self.conn), [])
        self.assertTrue(_relied(self.conn))
        # and through the re-link of the restored connection
        _relink(self.conn, "feed-item", "feed-now", "feed-relinked")
        self.assertTrue(_relied(self.conn))

    def test_a_note_naming_a_restored_account_follows_its_relink(self):
        self._restore(["feed-now"])
        self.assertEqual(_note(self.conn), ["feed-now"])
        _relink(self.conn, "feed-item", "feed-now", "feed-relinked")
        self.assertEqual(_note(self.conn), ["feed-relinked"])
        self.conn.execute("DELETE FROM account_adoptions")
        self.assertTrue(_relied(self.conn))


if __name__ == "__main__":
    unittest.main()
