"""A split file's source stays on the account that already holds its rows.

A file row's id is hashed over the account it was imported into, and an
import only matches the rows of the account it writes to. So a source sent
to a new account inserts its whole history a second time, however it got
there:

- an upgrade: a build before opaque ids gave two cards sharing their last
  four one account (an OFX statement was keyed on the last four, a QIF
  source on the slug of its cut name), so both cards' rows live there. The
  first card re-imported claims that account for its number; the second
  must stay on it too, since its rows are there, rather than be moved to
  a fresh account;
- a restore into another household: the source rows are keyed under the
  secret of the household that wrote them, which never travels, so the
  restored household cannot read them. The account the archive brought
  still holds the source's rows, and that is where the re-import goes.

A card that holds no rows on the shared account still gets an account of
its own. Every name and number here is invented."""
import tempfile
import unittest
from unittest import mock

from oikonome.sync import base, export, ofximport, qifimport, restore

from .test_account_sources_keep_each_source_on_its_own_account import (
    OFX_TWO_CARDS, _cc_stmt, _qif)
from .util import make_db, write_config

CARD_A = "Visa 4000111122220123"
CARD_B = "Visa 4000333344440123"          # same last four, another card
CARD_C = "Visa 4000555566660123"          # a third, never imported before
A_ROWS = [(4, -61.20, "HARBOR GROCER")]
B_ROWS = [(5, -5.00, "CONTOSO KIOSK"), (6, -7.50, "NORTHWIND TOLL")]
C_ROWS = [(7, -3.00, "FABRIKAM CAFE")]


def _pre_range_ensure(conn, prefix, name, *, key=None, type_=None,
                      subtype=None, **_kw):
    """The split account as the build before opaque ids made it: the slug
    of the source key with its number cut, so two cards sharing their last
    four shared one account."""
    conn.execute(
        "INSERT INTO items (id, aggregator, institution_name, access_token)"
        " VALUES ('manual','manual','Manual',NULL)"
        " ON CONFLICT (tenant_id, id) DO NOTHING")
    aid = f"manual:{prefix}-" + base._split_slug(
        base.mask_account_name((key or name).strip()))
    conn.execute(
        "INSERT INTO accounts (id, item_id, name, type, subtype)"
        " VALUES (%s,'manual',%s,%s,%s) ON CONFLICT (tenant_id, id)"
        " DO NOTHING",
        (aid, base.mask_account_name(name.strip()), type_ or "credit",
         subtype))
    return aid


def _ofx(*stmts) -> bytes:
    head, _, _ = OFX_TWO_CARDS.decode().partition("<CCSTMTTRNRS>")
    return (head + "".join(stmts)
            + "</CREDITCARDMSGSRSV1></OFX>\n").encode()


def _export(conn) -> bytes:
    with tempfile.TemporaryFile() as fh:
        export.build_zip(conn, out=fh)
        fh.seek(0)
        return fh.read()


class _Ledger(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _fresh(self):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        return conn

    def _ledger(self, conn, like):
        r = conn.execute(
            "SELECT count(*) AS n, COALESCE(sum(amount), 0) AS s,"
            "       count(DISTINCT account_id) AS a"
            "  FROM transactions WHERE account_id LIKE %s", (like,)
        ).fetchone()
        return r["n"], round(float(r["s"]), 2), r["a"]

    def _accounts(self, conn, like):
        return conn.execute("SELECT count(*) AS n FROM accounts"
                            " WHERE id LIKE %s", (like,)).fetchone()["n"]


class AnUpgradedSharedAccountKeepsBothCards(_Ledger):
    def test_an_ofx_reimport_adds_no_row_and_no_account(self):
        with mock.patch.object(ofximport, "ensure_split_account",
                               _pre_range_ensure):
            ofximport.import_ofx(self.conn, "chk", OFX_TWO_CARDS)
        before = self._ledger(self.conn, "manual:ofx-%")
        self.assertEqual(before, (2, 42.0, 1))
        for _ in range(2):
            ofximport.import_ofx(self.conn, "chk", OFX_TWO_CARDS)
            self.assertEqual(self._ledger(self.conn, "manual:ofx-%"), before)
        self.assertEqual(self._accounts(self.conn, "manual:ofx-%"), 1)

    def test_a_qif_reimport_adds_no_row_and_no_account(self):
        text = _qif((CARD_A, A_ROWS), (CARD_B, B_ROWS))
        with mock.patch.object(qifimport, "ensure_split_account",
                               _pre_range_ensure):
            qifimport.import_qif(self.conn, "chk", text)
        before = self._ledger(self.conn, "manual:qif-%")
        self.assertEqual(before[0], 3)
        self.assertEqual(before[2], 1)
        for _ in range(2):
            qifimport.import_qif(self.conn, "chk", text)
            self.assertEqual(self._ledger(self.conn, "manual:qif-%"), before)
        self.assertEqual(self._accounts(self.conn, "manual:qif-%"), 1)

    def test_a_card_with_no_rows_there_still_gets_its_own_account(self):
        with mock.patch.object(qifimport, "ensure_split_account",
                               _pre_range_ensure):
            qifimport.import_qif(self.conn, "chk",
                                 _qif((CARD_A, A_ROWS), (CARD_B, B_ROWS)))
        qifimport.import_qif(self.conn, "chk", _qif(
            (CARD_A, A_ROWS), (CARD_B, B_ROWS), (CARD_C, C_ROWS)))
        self.assertEqual(self._accounts(self.conn, "manual:qif-%"), 2)
        self.assertEqual(self._ledger(self.conn, "manual:qif-%")[0], 4)
        fresh = self.conn.execute(
            "SELECT account_id FROM transactions WHERE name = %s",
            (C_ROWS[0][2],)).fetchone()["account_id"]
        self.assertRegex(fresh, r"^manual:qif-[a-z]{16}$")

    def test_an_ofx_card_with_no_rows_there_still_gets_its_own_account(self):
        with mock.patch.object(ofximport, "ensure_split_account",
                               _pre_range_ensure):
            ofximport.import_ofx(self.conn, "chk", OFX_TWO_CARDS)
        three = _ofx(_cc_stmt("4000111122220123", "A001", "-30.00"),
                     _cc_stmt("4000333344440123", "B001", "-12.00"),
                     _cc_stmt("4000555566660123", "C001", "-4.00"))
        ofximport.import_ofx(self.conn, "chk", three)
        self.assertEqual(self._ledger(self.conn, "manual:ofx-%"),
                         (3, 46.0, 2))


class ARestoredHouseholdReimportsOntoWhatItRestored(_Ledger):
    def test_a_qif_reimport_after_a_restore_adds_nothing(self):
        text = _qif(("Invented Checking", [(4, -20.00, "HARBOR GROCER"),
                                           (5, -1.00, "SERVICE FEE")]),
                    ("Invented Savings", [(6, 50.00, "TRANSFER IN")]))
        qifimport.import_qif(self.conn, "chk", text)
        before = self._ledger(self.conn, "manual:qif-%")
        dest = self._fresh()
        out = restore.restore_zip(dest, _export(self.conn))
        self.assertFalse(out.get("error"), out)
        self.assertEqual(self._ledger(dest, "manual:qif-%"), before)
        for _ in range(2):
            qifimport.import_qif(dest, "chk", text)
            self.assertEqual(self._ledger(dest, "manual:qif-%"), before)
        self.assertEqual(self._accounts(dest, "manual:qif-%"), 2)

    def test_two_cards_sharing_their_last_four_each_find_their_own(self):
        text = _qif((CARD_A, A_ROWS), (CARD_B, B_ROWS))
        qifimport.import_qif(self.conn, "chk", text)
        before = self._ledger(self.conn, "manual:qif-%")
        self.assertEqual(before[2], 2)
        dest = self._fresh()
        restore.restore_zip(dest, _export(self.conn))
        qifimport.import_qif(dest, "chk", text)
        self.assertEqual(self._ledger(dest, "manual:qif-%"), before)
        self.assertEqual(self._accounts(dest, "manual:qif-%"), 2)

    def test_an_ofx_reimport_after_a_restore_adds_nothing(self):
        ofximport.import_ofx(self.conn, "chk", OFX_TWO_CARDS)
        before = self._ledger(self.conn, "manual:ofx-%")
        dest = self._fresh()
        restore.restore_zip(dest, _export(self.conn))
        ofximport.import_ofx(dest, "chk", OFX_TWO_CARDS)
        self.assertEqual(self._ledger(dest, "manual:ofx-%"), before)
        self.assertEqual(self._accounts(dest, "manual:ofx-%"), 2)


if __name__ == "__main__":
    unittest.main()
