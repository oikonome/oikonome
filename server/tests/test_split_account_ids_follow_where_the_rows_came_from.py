"""A split import finds each source's account by where its rows came from.

A multi-account file (QIF, Mint, the vendor exports) makes one account per
source account. Earlier releases took the plain slug of the source name as
the id, which mapped two sources onto one id when the slug could not tell
them apart (a script it cannot spell, two names alike past its cut), and
kept a full account number whole in the id of an account named by it.

New sources now get ids that keep them apart and keep only the last four
digits. What must not change is where existing rows are found: a row's id
hashes the account id it was imported under, so an import that sent a
source to a new id would insert that source's whole history a second time.
So an account an earlier release made keeps every source whose rows it
holds, a re-keyed account is found through the adoption its move recorded,
and only a source no account holds yet gets a new id.

The numbered ids are moved once, by migration 199 for stored accounts and
by restore for an archive written before it; each leaves an adoption that
the re-import follows to the moved account. That record names the old id
with its number cut, because it leaves through /export like the rest; the
re-import recomputes its rows' ids under the full old id, which the file
it reads spells again.
"""
import hashlib
import io
import pathlib
import re
import tempfile
import unittest
import zipfile
from unittest import mock

from oikonome.engine import budget
from oikonome.sync import base, export, qifimport, restore

from .util import make_db, write_config

MIGRATION = (pathlib.Path(base.__file__).parent.parent / "db" / "migrations"
             / "199_split_account_ids_and_source_cells_lose_full_numbers.sql")

LONG_DIGITS = re.compile(r"\d{8,}")
NUMBER = "1234567890123"            # invented


def _legacy_ensure(conn, prefix, name, *, key=None, type_=None,
                   subtype=None, lines=None):
    """The split account as an earlier release made it: the plain slug of
    the source name as the id, its name as migration 189 left it."""
    conn.execute(
        "INSERT INTO items (id, aggregator, institution_name, access_token)"
        " VALUES ('manual','manual','Manual',NULL)"
        " ON CONFLICT (tenant_id, id) DO NOTHING")
    aid = f"manual:{prefix}-{base._split_slug(key or name)}"
    conn.execute(
        "INSERT INTO accounts (id, item_id, name, type, subtype)"
        " VALUES (%s,'manual',%s,%s,%s) ON CONFLICT (tenant_id, id)"
        " DO NOTHING",
        (aid, base.mask_long_digits(name.strip()), type_ or "depository",
         subtype))
    return aid


def _qif(*accounts):
    """A QIF file with one !Account section per (name, rows)."""
    out = []
    for name, rows in accounts:
        out += ["!Account", f"N{name}", "TBank", "^", "!Type:Bank"]
        for day, amount, payee in rows:
            out += [f"D03/{day:02d}/2026", f"T{amount:.2f}", f"P{payee}",
                    "^"]
    return "\n".join(out) + "\n"


SAVINGS = "Сбережения"              # neither name has a letter the slug
CURRENT = "Текущий счёт"            # can spell: both slugged to "x"
A_ROWS = [(4, -61.20, "SPRINGFIELD GROCER"), (5, -5.00, "SERVICE FEE")]
B_ROWS = [(6, -23.00, "NORTHWIND FUEL"), (7, -9.99, "CONTOSO STREAM")]
H_ROWS = [(8, -12.00, "FABRIKAM CAFE")]


class SplitIdTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _legacy_import(self, text):
        with mock.patch.object(qifimport, "ensure_split_account",
                               _legacy_ensure):
            return qifimport.import_qif(self.conn, "chk", text)

    def _split(self):
        return {r["account_id"]: r["n"] for r in self.conn.execute(
            "SELECT account_id, count(*) AS n FROM transactions"
            " WHERE account_id LIKE 'manual:qif-%' GROUP BY account_id")}

    def _accounts(self):
        return sorted(r["id"] for r in self.conn.execute(
            "SELECT id FROM accounts WHERE id LIKE 'manual:qif-%'"))

    def test_a_collapsed_account_keeps_both_sources_on_reimport(self):
        """The case a digest scheme got wrong: the legacy id holds two
        sources' rows and its name names only the first. Re-importing the
        same file must add nothing and make no account."""
        text = _qif((SAVINGS, A_ROWS), (CURRENT, B_ROWS))
        self._legacy_import(text)
        before = self._split()
        self.assertEqual(before, {"manual:qif-x": 4})
        qifimport.import_qif(self.conn, "chk", text)
        self.assertEqual(self._split(), before)
        self.assertEqual(self._accounts(), ["manual:qif-x"])

    def test_a_new_source_beside_a_collapsed_one_gets_its_own_account(self):
        self._legacy_import(_qif((SAVINGS, A_ROWS), ("Harbor Savings", H_ROWS)))
        qifimport.import_qif(self.conn, "chk", _qif(
            (SAVINGS, A_ROWS), (CURRENT, B_ROWS), ("Harbor Savings", H_ROWS)))
        got = self._split()
        self.assertEqual(len(got), 3, got)
        self.assertEqual(got["manual:qif-x"], len(A_ROWS))
        self.assertEqual(got["manual:qif-harbor-savings"], len(H_ROWS))
        (new,) = set(got) - {"manual:qif-x", "manual:qif-harbor-savings"}
        self.assertEqual(got[new], len(B_ROWS))
        qifimport.import_qif(self.conn, "chk", _qif(
            (SAVINGS, A_ROWS), (CURRENT, B_ROWS), ("Harbor Savings", H_ROWS)))
        self.assertEqual(self._split(), got)

    def test_a_renamed_account_is_still_found(self):
        text = _qif(("Harbor Savings", H_ROWS), ("Everyday", A_ROWS))
        qifimport.import_qif(self.conn, "chk", text)
        before = self._split()
        self.conn.execute(
            "UPDATE accounts SET display_name = 'Rainy Day'"
            " WHERE id = 'manual:qif-harbor-savings'")
        qifimport.import_qif(self.conn, "chk", text)
        self.assertEqual(self._split(), before)

    def test_a_source_named_by_its_number_never_spells_it(self):
        out = qifimport.import_qif(self.conn, "chk", _qif(
            (NUMBER, A_ROWS), ("Harbor Savings", H_ROWS)))
        for aid in self._accounts():
            self.assertIsNone(re.search(r"\d", aid), aid)
        (aid,) = [r["id"] for r in self.conn.execute(
            "SELECT id FROM accounts WHERE name = '…0123'"
            "   AND id LIKE 'manual:qif-%'").fetchall()]
        raw = self.conn.execute(
            "SELECT raw::text AS r FROM transactions"
            " WHERE account_id = %s", (aid,)).fetchall()
        self.assertTrue(raw)
        for r in raw:
            self.assertIsNone(LONG_DIGITS.search(r["r"]), r["r"])
        for k in out["split_accounts"]:
            self.assertIsNone(LONG_DIGITS.search(k), k)


class NumberedIdMoveTests(unittest.TestCase):
    """An account an earlier release named by its full number."""

    OLD = f"manual:qif-{NUMBER}"
    NEW = "manual:qif-0123"

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, excluded_accounts=[self.OLD, "card"])
        self.addCleanup(self.conn.close)
        self.text = _qif((NUMBER, A_ROWS), ("Harbor Savings", H_ROWS))
        with mock.patch.object(qifimport, "ensure_split_account",
                               _legacy_ensure):
            qifimport.import_qif(self.conn, "chk", self.text)
        # rows stored before the source cell was masked
        self.conn.execute(
            "UPDATE transactions SET raw = jsonb_set(raw, '{account}',"
            " to_jsonb(%s::text)) WHERE account_id = %s", (NUMBER, self.OLD))

    def _assert_no_number(self, conn):
        for sql in ("SELECT id AS v FROM accounts",
                    "SELECT account_id AS v FROM transactions",
                    "SELECT raw::text AS v FROM transactions",
                    "SELECT config::text AS v FROM tenant_settings",
                    "SELECT account_id AS v FROM account_adoptions",
                    "SELECT old_account_id AS v FROM account_adoptions"):
            for r in conn.execute(sql).fetchall():
                self.assertIsNone(LONG_DIGITS.search(r["v"] or ""),
                                  (sql, r["v"]))
        data = self._export(conn)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for member in ("accounts.csv", "account_adoptions.csv"):
                text = z.read(member).decode("utf-8")
                self.assertNotIn(NUMBER, text, member)
                self.assertIsNone(LONG_DIGITS.search(text), member)
        return data

    @staticmethod
    def _export(conn):
        with tempfile.TemporaryFile() as fh:
            export.build_zip(conn, out=fh)
            fh.seek(0)
            return fh.read()

    def _rows(self, conn):
        return conn.execute(
            "SELECT count(*) AS n FROM transactions"
            " WHERE account_id LIKE 'manual:qif-%'").fetchone()["n"]

    def test_the_migration_moves_it_and_a_reimport_adds_nothing(self):
        before = self._rows(self.conn)
        self.conn.execute(MIGRATION.read_text())
        self.conn.execute(MIGRATION.read_text())          # idempotent
        self._assert_no_number(self.conn)
        self.assertEqual(budget.load_config(self.conn)["excluded_accounts"],
                         [self.NEW, "card"])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM transactions WHERE account_id = %s",
            (self.NEW,)).fetchone()["n"], len(A_ROWS))
        self.assertEqual(self.conn.execute(
            "SELECT old_account_id AS v FROM account_adoptions"
            " WHERE account_id = %s", (self.NEW,)).fetchone()["v"],
            "manual:qif-…0123")
        qifimport.import_qif(self.conn, "chk", self.text)
        self.assertEqual(self._rows(self.conn), before)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM accounts WHERE id LIKE 'manual:qif-%'"
        ).fetchone()["n"], 2)
        data = self._assert_no_number(self.conn)
        # and after the export carrying that cut record is restored
        dest = make_db()
        self.addCleanup(dest.close)
        write_config(dest)
        restore.restore_zip(dest, data)
        self._assert_no_number(dest)
        self.assertEqual(self._rows(dest), before)
        qifimport.import_qif(dest, "chk", self.text)
        self.assertEqual(self._rows(dest), before)
        self.assertEqual(dest.execute(
            "SELECT count(*) AS n FROM accounts WHERE id LIKE 'manual:qif-%'"
        ).fetchone()["n"], 2)

    def test_a_taken_masked_id_gets_a_suffix_that_is_no_number(self):
        """The masked id is already an account: the moved one carries a
        digest of the cut old id spelled in letters, and a re-import of
        both files adds nothing. (The source named "0123" is made as an
        earlier release made it, under the readable id the move wants.)"""
        with mock.patch.object(qifimport, "ensure_split_account",
                               _legacy_ensure):
            qifimport.import_qif(self.conn, "chk", _qif(("0123", B_ROWS),
                                                          ("Harbor Savings",
                                                           H_ROWS)))
        before = self._rows(self.conn)
        self.conn.execute(MIGRATION.read_text())
        self.conn.execute(MIGRATION.read_text())          # idempotent
        self._assert_no_number(self.conn)
        moved = self.conn.execute(
            "SELECT account_id AS v FROM account_adoptions"
            " WHERE old_account_id = 'manual:qif-…0123'").fetchone()["v"]
        self.assertEqual(moved, f"{self.NEW}-" + base.letter_digest(
            hashlib.md5("manual:qif-…0123#1".encode()).hexdigest()))
        self.assertEqual(moved, f"{self.NEW}-" + base.renumbered_split_suffix(
            "manual:qif-…0123", 1))
        self.assertIsNone(re.search(r"\d", moved.rsplit("-", 1)[1]))
        qifimport.import_qif(self.conn, "chk", self.text)
        qifimport.import_qif(self.conn, "chk", _qif(("0123", B_ROWS),
                                                      ("Harbor Savings",
                                                       H_ROWS)))
        self.assertEqual(self._rows(self.conn), before)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM transactions WHERE account_id = %s",
            (moved,)).fetchone()["n"], len(A_ROWS))

    def test_the_python_twin_names_the_same_id_as_the_migration(self):
        for old, new in (
                (self.OLD, self.NEW),
                ("manual:mint-acct1234567890123x", "manual:mint-acct-0123x"),
                ("manual:copilot-card-4000123456789010",
                 "manual:copilot-card-9010")):
            self.assertEqual(base.renumbered_split_id(old), new)
            got = self.conn.execute(
                "SELECT split_part(%(o)s, '-', 1) || '-' || COALESCE(NULLIF("
                "left(btrim(regexp_replace(regexp_replace(substr(%(o)s,"
                " strpos(%(o)s, '-') + 1), '\\d{4,}(\\d{4})', '-\\1', 'g'),"
                " '-+', '-', 'g'), '-'), 40), ''), 'x') AS v",
                {"o": old}).fetchone()["v"]
            self.assertEqual(got, new)
            # the lineage the move records, and the suffix a taken id gets
            got = self.conn.execute(
                "SELECT regexp_replace(%(o)s, '\\d{4,}(\\d{4})', '…\\1',"
                " 'g') AS cut", {"o": old}).fetchone()["cut"]
            self.assertEqual(got, base.split_lineage_id(old))
            self.assertIsNone(LONG_DIGITS.search(got))
        sql = MIGRATION.read_text()
        self.assertIn("'\\d{4,}(\\d{4})', '-\\1'", sql)
        self.assertIn("'\\d{4,}(\\d{4})', '…\\1'", sql)
        self.assertIn("left(md5(cut_id || '#' || n), 8),\n", sql)
        self.assertIn("'0123456789', 'ghijklmnop')", sql)
        self.assertIn("IF n > 32 THEN", sql)
        self.assertEqual(base.RENUMBER_MAX_SUFFIXES, 32)
        self.assertIn("ORDER BY tenant_id, md5(id) COLLATE \"C\"", sql)
        # the suffix of each ordinal, in SQL and in Python alike
        for n in (1, 2, 3, 17):
            got = self.conn.execute(
                "SELECT translate(left(md5(%s || '#' || %s), 8),"
                " '0123456789', 'ghijklmnop') AS v",
                ("manual:qif-visa-…0123", n)).fetchone()["v"]
            self.assertEqual(
                got, base.renumbered_split_suffix("manual:qif-visa-…0123", n))
        # an all-decimal digest, in SQL and in Python alike
        for hexed in ("12345678abcdef", "90817263", "a0b1c2d3"):
            got = self.conn.execute(
                "SELECT translate(left(%s, 8), '0123456789', 'ghijklmnop')"
                " AS v", (hexed,)).fetchone()["v"]
            self.assertEqual(got, base.letter_digest(hexed))
            self.assertIsNone(re.search(r"\d", got))
        cut = "manual:qif-…0123"
        self.assertEqual(self.conn.execute(
            "SELECT md5(%s) AS v", (cut,)).fetchone()["v"],
            hashlib.md5(cut.encode("utf-8")).hexdigest())
        digest_id = "manual:qif-pargne-1999-" + base.letter_digest("65078653")
        self.assertIsNone(base.NUMBERED_SPLIT_ID_RE.search(digest_id))

    def test_the_migration_cuts_the_cells_the_live_rule_cuts(self):
        sql = MIGRATION.read_text()
        prefixes = re.search(r"split_part\(t\.id, ':', 1\) IN \((.*?)\)",
                             sql, re.S).group(1)
        self.assertEqual(
            tuple(p + ":" for p in re.findall(r"'([^']*)'", prefixes)),
            base.FILE_IMPORT_ID_PREFIXES)
        key_lists = re.findall(r"IN \(('account'.*?)\)", sql, re.S)
        self.assertEqual(len(key_lists), 2)
        for keys in key_lists:
            self.assertEqual(tuple(re.findall(r"'([^']*)'", keys)),
                             base._SPLIT_SOURCE_KEYS)
        self.assertEqual(
            base.mask_file_raw({" Account Name ": f"Card {NUMBER}",
                                "Batch": NUMBER}),
            {" Account Name ": "Card …0123", "Batch": NUMBER})

    def test_a_restore_of_an_older_archive_does_not_bring_it_back(self):
        with tempfile.TemporaryFile() as fh:
            export.build_zip(self.conn, out=fh)
            fh.seek(0)
            data = fh.read()
        dest = make_db()
        self.addCleanup(dest.close)
        write_config(dest)
        restore.restore_zip(dest, data)
        self._assert_no_number(dest)
        before = self._rows(dest)
        self.assertEqual(before, len(A_ROWS) + len(H_ROWS))
        self.assertIn(self.NEW, budget.load_config(dest)["excluded_accounts"])
        qifimport.import_qif(dest, "chk", self.text)
        self.assertEqual(self._rows(dest), before)
        self.assertEqual(dest.execute(
            "SELECT count(*) AS n FROM accounts WHERE id LIKE 'manual:qif-%'"
        ).fetchone()["n"], 2)
        # the same archive again lands on the moved account
        restore.restore_zip(dest, data)
        self._assert_no_number(dest)
        self.assertEqual(self._rows(dest), before)


if __name__ == "__main__":
    unittest.main()
