"""A restored ledger never holds a transaction without an amount.

Every live door writes a number into `transactions.amount` (the upsert
refuses a row without one, the plan and wallet importers write 0), and every
reader of the ledger — the duplicate matchers above all — turns it into
cents. An archive row with an empty amount cell would plant the one shape the
app never stores: every later statement import onto that account, and the
reconnect guard on it, would then fail on the row. Leaving the row out is no
answer either: its receipt, note and split would then be dropped as orphans.
So the restore brings it back at 0, marks the row as repaired, and says how
many it repaired. A row an older restore already stored without an amount is
passed over by every matcher, so no import or reconnect fails on it.
"""
import csv
import datetime as dt
import io
import pathlib
import tempfile
import unittest
import zipfile

from oikonome.db import tenancy
from oikonome.sync import adopt, export, restore
from oikonome.sync.dedup import Deduper

from .util import TEST_DB, _admin_dsn, add_txn, make_db, write_config

MIGRATION = (pathlib.Path(__file__).resolve().parents[1] / "oikonome" / "db"
             / "migrations" / "205_a_transaction_always_has_an_amount.sql")


def _export(conn):
    with tempfile.TemporaryFile() as fh:
        export.build_zip(conn, out=fh)
        fh.seek(0)
        return fh.read()


def _blank_amount(data: bytes, txn_id: str) -> bytes:
    """An archive written before the column refused NULL can carry a
    transaction with no amount at all; today's column cannot, so the
    archive is edited rather than the source ledger."""
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            body = src.read(info.filename)
            if info.filename.endswith("transactions.csv"):
                rows = list(csv.DictReader(io.StringIO(body.decode())))
                assert rows and "amount" in rows[0], rows[:1]
                hit = [r for r in rows if r["id"] == txn_id]
                assert hit, txn_id
                for r in hit:
                    r["amount"] = ""
                buf = io.StringIO()
                w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
                body = buf.getvalue().encode()
            z.writestr(info, body)
    return out.getvalue()


def _allow_null(case) -> None:
    """Rows stored by a restore older than migration 205 had no amount; the
    column refuses that now, so a test of the readers that meet such a row
    loosens the column for its own run and puts it back."""
    admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
    admin.execute("ALTER TABLE transactions ALTER COLUMN amount DROP NOT NULL")

    def restore_constraint():
        admin.execute("DELETE FROM transactions WHERE amount IS NULL")
        admin.execute(
            "ALTER TABLE transactions ALTER COLUMN amount SET NOT NULL")
        admin.close()
    case.addCleanup(restore_constraint)


class _Txn:
    def __init__(self, account_id, date, amount, name, id=None):
        self.account_id, self.date = account_id, date
        self.amount, self.name = amount, name
        self.merchant_name = None
        self.id = id


class RestoredTransactionAmountTests(unittest.TestCase):
    def test_a_transaction_without_an_amount_is_restored_at_zero_with_its_note(self):
        src = make_db()
        self.addCleanup(src.close)
        write_config(src)
        add_txn(src, dt.date(2026, 7, 10), 40.0, "CORNER GROCER",
                account="chk", txn_id="kept")
        add_txn(src, dt.date(2026, 7, 11), 25.0, "HAND EDITED",
                account="chk", txn_id="blank")
        src.execute("INSERT INTO transaction_notes (txn_id, note) "
                    "VALUES ('blank', 'paint for the shed')")
        data = _blank_amount(_export(src), "blank")

        dest = make_db()
        self.addCleanup(dest.close)
        counts = restore.restore_zip(dest, data)
        rows = dest.execute(
            "SELECT id, amount, raw->>'_amount_missing' AS flag "
            "FROM transactions ORDER BY id").fetchall()
        self.assertEqual([(r["id"], r["amount"], r["flag"]) for r in rows],
                         [("blank", 0.0, "true"), ("kept", 40.0, None)])
        # its child keeps its parent rather than being dropped as an orphan
        note = dest.execute("SELECT note FROM transaction_notes "
                            "WHERE txn_id = 'blank'").fetchone()
        self.assertEqual(note["note"], "paint for the shed")
        self.assertNotIn("orphaned", counts)
        self.assertEqual(counts.get("amount_repaired"), 1)
        self.assertTrue(any("amount" in n and "0" in n
                            for n in counts.get("_notes", [])),
                        counts.get("_notes"))
        # a statement import onto the account builds its matcher cleanly
        Deduper(dest, "chk", "csv", [dt.date(2026, 7, 11)])

    def test_a_blank_amount_row_already_present_is_not_reported_as_repaired(self):
        # the destination already holds the row with its amount: the insert
        # is a no-op, so the row counts as skipped only, and the note that
        # tells the person to edit an amount restored at 0 is not shown
        src = make_db()
        self.addCleanup(src.close)
        write_config(src)
        add_txn(src, dt.date(2026, 7, 11), 25.0, "HAND EDITED",
                account="chk", txn_id="blank")
        data = _blank_amount(_export(src), "blank")

        dest = make_db()
        self.addCleanup(dest.close)
        write_config(dest)
        add_txn(dest, dt.date(2026, 7, 11), 25.0, "HAND EDITED",
                account="chk", txn_id="blank")
        counts = restore.restore_zip(dest, data)
        self.assertNotIn("amount_repaired", counts)
        self.assertFalse(any("no amount" in n
                             for n in counts.get("_notes", [])),
                         counts.get("_notes"))
        row = dest.execute("SELECT amount, raw->>'_amount_missing' AS flag "
                           "FROM transactions WHERE id = 'blank'").fetchone()
        self.assertEqual((row["amount"], row["flag"]), (25.0, None))

    def test_restoring_the_same_archive_twice_reports_the_repair_once(self):
        src = make_db()
        self.addCleanup(src.close)
        write_config(src)
        add_txn(src, dt.date(2026, 7, 11), 25.0, "HAND EDITED",
                account="chk", txn_id="blank")
        data = _blank_amount(_export(src), "blank")

        dest = make_db()
        self.addCleanup(dest.close)
        self.assertEqual(restore.restore_zip(dest, data).get("amount_repaired"), 1)
        self.assertNotIn("amount_repaired", restore.restore_zip(dest, data))

    def test_a_row_already_stored_without_an_amount_never_breaks_an_import(self):
        # a restore from before the door above closed can have left one
        # behind; it can never be a same-amount twin, so the matcher passes
        # over it and the statement still imports
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        add_txn(conn, dt.date(2026, 7, 10), 40.0, "CORNER GROCER",
                account="chk", txn_id="kept")
        add_txn(conn, dt.date(2026, 7, 11), 25.0, "HAND EDITED",
                account="chk", txn_id="blank")
        _allow_null(self)
        conn.execute("UPDATE transactions SET amount = NULL WHERE id = 'blank'")
        d = Deduper(conn, "chk", "csv", [dt.date(2026, 7, 10)])
        self.assertTrue(d.claim(dt.date(2026, 7, 10), 40.0, "CORNER GROCER"))
        # the operator's twin sweep on that account passes over it too
        self.assertEqual(adopt.dedupe_twins(conn, "chk", apply=False), [])

    def test_a_row_already_stored_without_an_amount_never_breaks_a_reconnect(self):
        conn = make_db()
        self.addCleanup(conn.close)
        conn.execute(
            "INSERT INTO items (id, aggregator, institution_id, "
            "institution_name, status, access_token) VALUES "
            "('old-item','plaid','ins_1','Acme','restored',NULL),"
            "('new-item','plaid','ins_1','Acme','ok','live-token')")
        conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, mask) "
            "VALUES ('old-acct','old-item','Checking','depository','1234')")
        add_txn(conn, dt.date(2026, 5, 1), 10.0, "COFFEE",
                account="old-acct", txn_id="old-1")
        add_txn(conn, dt.date(2026, 6, 1), 20.0, "MARKET STORE",
                account="old-acct", txn_id="old-2")
        add_txn(conn, dt.date(2026, 5, 30), 25.0, "HAND EDITED",
                account="old-acct", txn_id="blank")
        _allow_null(self)
        conn.execute("UPDATE transactions SET amount = NULL WHERE id = 'blank'")
        adopt.adopt_accounts(
            conn, item_id="new-item", institution_id="ins_1",
            institution_name="Acme",
            incoming=[{"account_id": "new-acct", "mask": "1234",
                       "type": "depository"}])
        # a backfill page at the seam: the restored copy of the market charge
        # (dropped) and a charge the restore never held (kept)
        kept, _stats = adopt.filter_incoming(conn, "new-acct", [
            _Txn("new-acct", dt.date(2026, 6, 1), 20.0, "MARKET STORE",
                 id="p-1"),
            _Txn("new-acct", dt.date(2026, 5, 31), 7.0, "BAKERY", id="p-2")])
        self.assertEqual([t.id for t in kept], ["p-2"])
        # the id carry and the restored/live merge read the same cells
        adopt._Absorbed(conn, "new-acct", dt.date(2026, 5, 29),
                        dt.date(2026, 6, 1), dt.date(2026, 6, 1))
        conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, mask) "
            "VALUES ('live-acct','new-item','Checking','depository','9876')")
        add_txn(conn, dt.date(2026, 5, 30), 25.0, "HAND EDITED",
                account="live-acct", txn_id="live-1")
        self.assertEqual(adopt._pair_overlap(conn, "new-acct", "live-acct"),
                         [])


if __name__ == "__main__":
    unittest.main()

    def test_the_migration_backfills_a_stored_blank_amount(self):
        """A row an older restore stored with no amount is set to 0 with the
        same marker the restore writes today, so its note and receipt keep
        their parent, and the column refuses NULL from then on."""
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        add_txn(conn, dt.date(2026, 7, 11), 25.0, "HAND EDITED",
                account="chk", txn_id="blank")
        _allow_null(self)
        conn.execute("UPDATE transactions SET amount = NULL WHERE id = 'blank'")
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(admin.close)
        admin.execute(MIGRATION.read_text())
        row = conn.execute(
            "SELECT amount, raw->>'_amount_missing' AS flag FROM transactions "
            "WHERE id = 'blank'").fetchone()
        self.assertEqual((row["amount"], row["flag"]), (0.0, "true"))
        self.assertEqual(admin.execute(
            "SELECT is_nullable FROM information_schema.columns WHERE "
            "table_name = 'transactions' AND column_name = 'amount'"
        ).fetchone()[0], "NO")
