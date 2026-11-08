"""A hand-made account an earlier release named by a full account number
moves off that id, and the rows it holds move with it so that importing
the same statement again adds nothing.

An account made from a file or a typed name before names were masked has
the whole number in its id (a run of eight digits, or a card number typed
in groups and slugged to "visa-4111-1111-1111-1111"), and every row a file
importer put on it has an id hashed over that id. Cutting the number out of
the account id alone breaks re-import: the statement never spells the
account id, so the importer can only hash its lines under the id the
account has now, and every line would land a second time. So each row is
re-hashed under the new id at the moment the old one is still in hand,
after proving from the row's own date, amount and name that its id is the
hash of exactly that line under the old id. A row that cannot be proved
(edited since) is keyed under the household's secret instead and counted.
"""
import datetime as dt
import io
import re
import tempfile
import unittest
import zipfile

from oikonome import tenant_export
from oikonome.db import migrate
from oikonome.sync import (adopt, base, csvimport, export, ofximport,
                           pdfimport, restore, ynabimport)

from .test_pdfimport import HEADER, PERIOD, make_layout_pdf
from .util import TEST_DB, _admin_dsn, make_db, write_config

NUMBER = "12345678901234"                 # invented
OLD = f"manual:everyday-checking-{NUMBER}"
GROUPED = "manual:visa-4111-1111-1111-1111"   # a test card number, typed
# what an id spelling a full number looks like: a run of eight digits, or
# twelve or more in groups split by single dashes or spaces
SPELLS_A_NUMBER = re.compile(r"[0-9]{8}|[0-9](?:[ -]?[0-9]){11}")

CSV = ("Date,Description,Amount\n"
       "2026-03-10,SPRINGFIELD GROCER,-42.50\n"
       "2026-03-10,SPRINGFIELD GROCER,-42.50\n"
       "2026-03-11,EMPLOYER PAYROLL,2000.00\n"
       "2026-03-12,CARD 4111111111111111 PAYMENT,-15.00\n")
CSV_MAP = {"date": "Date", "amount": "Amount", "name": "Description"}
YNAB = ('"Date","Payee","Memo","Outflow","Inflow"\n'
        '"2026-04-02","Harbor Bakery","","$8.00","$0.00"\n'
        '"2026-04-03","Northwind Fuel","","$31.00","$0.00"\n')


def _ofx_without_fitid(conn, aid):
    """Statement lines a bank sent without a FITID: the content hash."""
    rows = [{"fitid": None, "date": dt.date(2026, 5, d), "amount": a,
             "name": n, "memo": None, "type": "DEBIT", "acct": None}
            for d, a, n in ((1, -12.0, "CONTOSO STREAM"),
                            (2, -3.5, "FABRIKAM CAFE"))]
    return ofximport._import_into(conn, aid, rows, batch_id=None)


def _ofx_with_fitid():
    body = ("<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260503<TRNAMT>-9.00"
            "<FITID>F-77<NAME>TAILSPIN TOYS</STMTTRN>\n")
    return ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\n"
            "ENCODING:USASCII\nCHARSET:1252\nCOMPRESSION:NONE\n"
            "OLDFILEUID:NONE\nNEWFILEUID:NONE\n\n<OFX>\n"
            "<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>"
            "<DTSERVER>20260712120000<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1>\n"
            "<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO"
            "</STATUS>\n<STMTRS><CURDEF>USD\n<BANKACCTFROM><BANKID>1"
            "<ACCTID>5555<ACCTTYPE>CHECKING</BANKACCTFROM>\n"
            "<BANKTRANLIST><DTSTART>20260101<DTEND>20260601\n" + body +
            "</BANKTRANLIST>\n<LEDGERBAL><BALAMT>0<DTASOF>20260601"
            "</LEDGERBAL>\n</STMTRS></STMTTRNRS></BANKMSGSRSV1>\n</OFX>\n"
            ).encode()


def _pdf():
    return make_layout_pdf([[
        PERIOD, HEADER,
        (720, [(36, "06/03"), (100, "GROCERY MART"), (380, "42.50"),
               (540, "1,957.50")]),
        (696, [(36, "06/07"), (100, "CHECK 1024"), (380, "500.00"),
               (540, "1,457.50")]),
    ]])


def _seed(conn, aid, name):
    """The account as an earlier release made it: its id the slug of the
    name with the number whole."""
    conn.execute(
        "INSERT INTO items (id, aggregator, institution_name, access_token)"
        " VALUES ('manual','manual','Manual',NULL)"
        " ON CONFLICT (tenant_id, id) DO NOTHING")
    conn.execute(
        "INSERT INTO accounts (id, item_id, name, type, subtype,"
        " balance_current) VALUES (%s,'manual',%s,'depository','checking',0)",
        (aid, name))


def _import_all(conn, aid):
    """Every importer's count of rows added."""
    return {
        "csv": csvimport.import_csv(conn, aid, CSV, CSV_MAP)["imported"],
        "pdf": pdfimport.import_pdf(conn, aid, _pdf())["imported"],
        "ofx": _ofx_without_fitid(conn, aid)["imported"],
        "ofx_fitid": ofximport.import_ofx(conn, aid, _ofx_with_fitid())[
            "imported"],
        "ynab": ynabimport.import_ynab(conn, aid, YNAB)["imported"],
    }


def _count(conn):
    return conn.execute(
        "SELECT count(*) AS n FROM transactions WHERE account_id LIKE"
        " 'manual:%%'").fetchone()["n"]


def _manual_ids(conn):
    return [r["id"] for r in conn.execute(
        "SELECT id FROM accounts WHERE id LIKE 'manual:%%'").fetchall()]


def _spelled(ids):
    return [i for i in ids if SPELLS_A_NUMBER.search(i or "")]


def _rows(conn, aid):
    return {r["id"]: (r["date"], float(r["amount"]), r["name"])
            for r in conn.execute(
                "SELECT id, date, amount, name FROM transactions"
                " WHERE account_id = %s", (aid,)).fetchall()}


def _export(conn) -> bytes:
    with tempfile.TemporaryFile() as fh:
        export.build_zip(conn, out=fh)
        fh.seek(0)
        return fh.read()


def _tenant(conn) -> str:
    return conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]


class _Household(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _moved(self, conn=None):
        """The one manual account that is not a fixture."""
        conn = conn or self.conn
        ids = [i for i in _manual_ids(conn)]
        self.assertEqual(len(ids), 1, ids)
        return ids[0]

    def _note_and_receipt(self, tid):
        self.conn.execute(
            "INSERT INTO transaction_notes (txn_id, note)"
            " VALUES (%s, 'mine')", (tid,))
        self.conn.execute(
            "INSERT INTO receipts (txn_id, image, mime)"
            " VALUES (%s, '\\x00', 'image/png')", (tid,))

    def _attached(self, conn=None):
        conn = conn or self.conn
        return (conn.execute(
            "SELECT t.id FROM transaction_notes n JOIN transactions t"
            "  ON t.id = n.txn_id WHERE n.note = 'mine'").fetchall(),
                conn.execute(
            "SELECT t.id FROM receipts r JOIN transactions t"
            "  ON t.id = r.txn_id").fetchall())


class ReimportIntoARekeyedNumberedAccount(_Household):
    def setUp(self):
        super().setUp()
        _seed(self.conn, OLD, "Everyday Checking …1234")
        first = _import_all(self.conn, OLD)
        self.assertTrue(all(first.values()), first)
        self.before = _count(self.conn)
        grocer = self.conn.execute(
            "SELECT id FROM transactions WHERE account_id = %s"
            "   AND name = 'SPRINGFIELD GROCER' ORDER BY id LIMIT 1",
            (OLD,)).fetchone()["id"]
        self._note_and_receipt(grocer)

    def test_reimport_into_a_rekeyed_numbered_account_adds_nothing(self):
        older = _export(self.conn)
        migrate.run(_admin_dsn(TEST_DB))
        self.assertEqual(_spelled(_manual_ids(self.conn)), [])
        moved = self._moved()
        self.assertEqual(moved, "manual:everyday-checking-1234")
        self.assertEqual(_count(self.conn), self.before)
        # each row now carries the id an import mints on the moved account
        held = _rows(self.conn, moved)
        _import_all(self.conn, moved)
        self.assertEqual(_rows(self.conn, moved), held)
        self.assertEqual(_count(self.conn), self.before)
        notes, receipts = self._attached()
        self.assertEqual(len(notes), 1)
        self.assertEqual(len(receipts), 1)
        # the adoption record names the old id with its number cut
        olds = [r["old_account_id"] for r in self.conn.execute(
            "SELECT old_account_id FROM account_adoptions").fetchall()]
        self.assertEqual(olds, ["manual:everyday-checking-…1234"])
        # the full id still leads to the account it moved to
        self.assertEqual(base.cut_lineage_targets(self.conn, OLD), [moved])
        # and an older archive restored here again adds nothing
        restore.restore_zip(self.conn, older)
        self.assertEqual(_count(self.conn), self.before)
        self.assertEqual(_spelled(_manual_ids(self.conn)), [])
        self.assertEqual(len(self._attached()[0]), 1)

    def test_a_second_pass_changes_nothing(self):
        migrate.run(_admin_dsn(TEST_DB))
        moved = self._moved()
        first = _rows(self.conn, moved)
        self.assertEqual(base.rekey_numbered_accounts(self.conn), {})
        self.assertEqual(base.key_file_import_ids(self.conn), 0)
        migrate.run(_admin_dsn(TEST_DB))
        self.assertEqual(_rows(self.conn, moved), first)

    def test_an_older_archive_restores_elsewhere_onto_one_moved_account(self):
        older = _export(self.conn)
        dest = make_db()
        self.addCleanup(dest.close)
        write_config(dest)
        restore.restore_zip(dest, older)
        self.assertEqual(_spelled(_manual_ids(dest)), [])
        moved = self._moved(dest)
        n = _count(dest)
        self.assertEqual(n, self.before)
        restore.restore_zip(dest, older)
        self.assertEqual(_count(dest), n)
        held = _rows(dest, moved)
        _import_all(dest, moved)
        self.assertEqual(_rows(dest, moved), held)
        self.assertEqual(_count(dest), n)

    def test_no_id_in_an_export_spells_the_number_after_the_pass(self):
        migrate.run(_admin_dsn(TEST_DB))
        archive = tenant_export.build_archive(
            _tenant(self.conn), operator="t", consented=True)
        for blob in (_export(self.conn), archive):
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                text = b"".join(z.read(n) for n in z.namelist())
            self.assertFalse(NUMBER.encode() in text,
                             "the full number rides an export")


class AGroupedCardNumberSlug(_Household):
    def test_grouped_card_number_slug_is_rekeyed(self):
        _seed(self.conn, GROUPED, "Visa …1111")
        csvimport.import_csv(self.conn, GROUPED, CSV, CSV_MAP)
        before = _count(self.conn)
        migrate.run(_admin_dsn(TEST_DB))
        moved = self._moved()
        self.assertEqual(moved, "manual:visa-1111")
        self.assertEqual(_spelled(_manual_ids(self.conn)), [])
        self.assertEqual(_spelled([r["old_account_id"] for r in
                                   self.conn.execute(
                                       "SELECT old_account_id FROM"
                                       " account_adoptions").fetchall()]),
                         [])
        held = _rows(self.conn, moved)
        csvimport.import_csv(self.conn, moved, CSV, CSV_MAP)
        self.assertEqual(_rows(self.conn, moved), held)
        self.assertEqual(_count(self.conn), before)


    def test_a_split_source_named_by_a_grouped_number_is_rekeyed(self):
        """A QIF account typed with its card number in groups, as a build
        before the split importers cut grouped numbers minted it: the id
        holds no run of eight, so migration 199 never moved it. It moves,
        its rows are keyed, and the file still lands on them."""
        from oikonome.sync import qifimport
        _seed(self.conn, "manual:qif-visa-4111-1111-1111-1111",
              "Visa 4111 1111 1111 1111")
        text = ("!Account\nNVisa 4111 1111 1111 1111\nTBank\n^\n"
                "!Type:Bank\nD03/04/2026\nT-61.20\nPSPRINGFIELD GROCER\n^\n"
                "D03/05/2026\nT-5.00\nPSERVICE FEE\n^\n"
                "!Account\nNHarbor Savings\nTBank\n^\n"
                "!Type:Bank\nD03/08/2026\nT-12.00\nPFABRIKAM CAFE\n^\n")
        qifimport.import_qif(self.conn, "chk", text)
        self.assertEqual(_spelled(_manual_ids(self.conn)),
                         ["manual:qif-visa-4111-1111-1111-1111"])
        before = _count(self.conn)
        migrate.run(_admin_dsn(TEST_DB))
        self.assertEqual(_spelled(_manual_ids(self.conn)), [])
        self.assertIn("manual:qif-visa-1111", _manual_ids(self.conn))
        held = _rows(self.conn, "manual:qif-visa-1111")
        self.assertEqual(len(held), 2)
        qifimport.import_qif(self.conn, "chk", text)
        moved = "manual:qif-visa-1111"
        self.assertEqual(_rows(self.conn, moved), held)
        self.assertEqual(_count(self.conn), before)


class AFullAdoptionRecord(_Household):
    LINKED = "plaid-everyday-checking"

    def setUp(self):
        super().setUp()
        _seed(self.conn, OLD, "Everyday Checking …1234")
        csvimport.import_csv(self.conn, OLD, CSV, CSV_MAP)
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name,"
            " access_token) VALUES ('item-1','plaid','Bank',NULL)")
        # a reconnect that adopted the account onto the link's id, recorded
        # before re-keys cut the number from the record
        self.assertTrue(adopt._rewrite_account_id(
            self.conn, OLD, self.LINKED, "item-1"))
        self.conn.execute(
            "INSERT INTO account_adoptions (account_id, old_account_id,"
            " item_id, reconciled_at) VALUES (%s, %s, 'item-1', now())",
            (self.LINKED, OLD))
        self.n = len(_rows(self.conn, self.LINKED))

    def test_full_adoption_record_is_cut_and_reimport_still_matches(self):
        older = _export(self.conn)
        migrate.run(_admin_dsn(TEST_DB))
        olds = [r["old_account_id"] for r in self.conn.execute(
            "SELECT old_account_id FROM account_adoptions").fetchall()]
        self.assertEqual(olds, ["manual:everyday-checking-…1234"])
        rows = _rows(self.conn, self.LINKED)
        self.assertEqual(len(rows), self.n)
        # every row carries the id an import mints on the linked account
        for tid, (day, amount, name) in rows.items():
            self.assertIn(tid, {base.file_row_id("csv", self.LINKED, day,
                                                 amount, name, n)
                                for n in (1, 2)}, name)
        csvimport.import_csv(self.conn, self.LINKED, CSV, CSV_MAP)
        self.assertEqual(_rows(self.conn, self.LINKED), rows)
        restore.restore_zip(self.conn, older)
        self.assertEqual(len(_rows(self.conn, self.LINKED)), self.n)
        self.assertEqual(_spelled([r["old_account_id"] for r in
                                   self.conn.execute(
                                       "SELECT old_account_id FROM"
                                       " account_adoptions").fetchall()]),
                         [])


class AnUnverifiableRow(_Household):
    def test_unverifiable_row_is_keyed_and_counted(self):
        _seed(self.conn, OLD, "Everyday Checking …1234")
        csvimport.import_csv(self.conn, OLD, CSV, CSV_MAP)
        edited = self.conn.execute(
            "SELECT id FROM transactions WHERE account_id = %s"
            "   AND name = 'EMPLOYER PAYROLL'", (OLD,)).fetchone()["id"]
        # the amount was corrected by hand after the import
        self.conn.execute(
            "UPDATE transactions SET amount = amount + 1 WHERE id = %s",
            (edited,))
        new = base.renumbered_manual_id(OLD)
        with self.assertLogs("oikonome.sync", level="WARNING"):
            moved, keyed, left = base.rehash_file_rows(self.conn, OLD, new)
        self.assertEqual((moved, keyed, left), (3, 1, 0))
        ids = set(_rows(self.conn, OLD))
        self.assertNotIn(edited, ids)
        self.assertEqual(
            [i for i in ids if re.match(base.KEYED_FILE_ID_PATTERN, i)],
            [base.keyed_file_id(base.txn_id_secret(self.conn), edited)])
        # nothing left to move: the moved rows hash the new id, the keyed
        # one is keyed already
        self.assertEqual(base.rehash_file_rows(self.conn, OLD, new),
                         (0, 0, 0))

    def test_the_migrate_pass_is_idempotent_over_a_keyed_row(self):
        _seed(self.conn, OLD, "Everyday Checking …1234")
        csvimport.import_csv(self.conn, OLD, CSV, CSV_MAP)
        self.conn.execute(
            "UPDATE transactions SET amount = amount + 1"
            " WHERE account_id = %s AND name = 'EMPLOYER PAYROLL'", (OLD,))
        migrate.run(_admin_dsn(TEST_DB))
        moved = self._moved()
        first = _rows(self.conn, moved)
        self.assertEqual(sum(1 for i in first
                             if re.match(base.KEYED_FILE_ID_PATTERN, i)), 1)
        migrate.run(_admin_dsn(TEST_DB))
        self.assertEqual(_rows(self.conn, moved), first)


if __name__ == "__main__":
    unittest.main()
