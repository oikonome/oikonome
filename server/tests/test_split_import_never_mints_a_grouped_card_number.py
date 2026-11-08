"""A split import whose source account is named by a card number typed in
groups ("Visa 4111 1111 1111 1111") mints the account, its name, the
source cell of every row and the result it answers with the number cut
to its last four — at import time, not only at the next migrate.

Cutting only unbroken runs left the grouped number in the id
("manual:qif-visa-4111-1111-1111-1111"), the stored name and every row's
raw cell until a later migrate re-keyed it, so each export in between
carried it. An account an earlier build minted the grouped way is still
found: the re-key moves it and records where (its cut lineage), and the
file names the source again, so a re-import after the migrate lands on
the moved account and adds nothing.
"""
import re
import unittest

from oikonome.db import migrate
from oikonome.sync import base, qifimport

from .util import TEST_DB, _admin_dsn, make_db, write_config

SRC = "Visa 4111 1111 1111 1111"          # a test card number
LONG = "Platinum Rewards Visa Signature 4111 1111 1111 1111"
GROUPED_RUN = re.compile(r"[0-9](?:[ -]?[0-9]){11}")


def _qif(name):
    return (f"!Account\nN{name}\nTBank\n^\n"
            "!Type:Bank\nD03/04/2026\nT-61.20\nPSPRINGFIELD GROCER\n^\n"
            "D03/05/2026\nT-5.00\nPSERVICE FEE\n^\n"
            "!Account\nNHarbor Savings\nTBank\n^\n"
            "!Type:Bank\nD03/08/2026\nT-12.00\nPFABRIKAM CAFE\n^\n")


class _Base(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _manual(self):
        return {r["id"]: r["name"] for r in self.conn.execute(
            "SELECT id, name FROM accounts WHERE id LIKE 'manual:%%'"
        ).fetchall()}

    def _rows(self, aid):
        return {r["id"]: r["raw"] for r in self.conn.execute(
            "SELECT id, raw FROM transactions WHERE account_id = %s",
            (aid,)).fetchall()}

    def _count(self):
        return self.conn.execute(
            "SELECT count(*) AS n FROM transactions WHERE account_id LIKE"
            " 'manual:%%'").fetchone()["n"]


class AFreshSplitImport(_Base):
    def test_fresh_split_import_mints_the_cut_number(self):
        out = qifimport.import_qif(self.conn, "chk", _qif(SRC))
        accounts = self._manual()
        (card,) = [a for a, n in accounts.items() if n == "Visa …1111"]
        for aid, name in accounts.items():
            self.assertFalse(GROUPED_RUN.search(aid), aid)
            self.assertFalse(GROUPED_RUN.search(name), name)
        for raw in self._rows(card).values():
            self.assertEqual(raw.get("account"), "Visa …1111")
        self.assertFalse(any(GROUPED_RUN.search(k)
                             for k in out["split_accounts"]),
                         out["split_accounts"])
        # and a second import of the same file adds nothing
        n = self._count()
        qifimport.import_qif(self.conn, "chk", _qif(SRC))
        self.assertEqual(self._count(), n)

    def test_mask_file_raw_cuts_a_grouped_source_cell(self):
        self.assertEqual(
            base.mask_file_raw({"account": SRC, "payee": "X"})["account"],
            "Visa …1111")


class AnAccountMintedTheOldGroupedWay(_Base):
    def _as_old_build(self, name, aid):
        """The account and rows as a build before the grouped cut left
        them: the id, the name and the rows' source cells all whole."""
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name,"
            " access_token) VALUES ('manual','manual','Manual',NULL)"
            " ON CONFLICT (tenant_id, id) DO NOTHING")
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype)"
            " VALUES (%s,'manual',%s,'depository','checking')", (aid, name))
        qifimport.import_qif(self.conn, "chk", _qif(name))
        rows = self._rows(aid)
        self.assertEqual(len(rows), 2, self._manual())
        self.conn.execute(
            "UPDATE transactions SET raw = jsonb_set(raw, '{account}',"
            " to_jsonb(%s::text)) WHERE account_id = %s", (name, aid))
        return len(rows)

    def test_reimport_after_migrate_lands_on_the_moved_account(self):
        old = base.split_legacy_id("qif", SRC)
        self.assertEqual(old, "manual:qif-visa-4111-1111-1111-1111")
        held = self._as_old_build(SRC, old)
        n = self._count()
        migrate.run(_admin_dsn(TEST_DB))
        accounts = self._manual()
        self.assertNotIn(old, accounts)
        self.assertIn("manual:qif-visa-1111", accounts)
        self.assertEqual(len(self._rows("manual:qif-visa-1111")), held)
        qifimport.import_qif(self.conn, "chk", _qif(SRC))
        self.assertEqual(self._count(), n)
        self.assertEqual(len(self._rows("manual:qif-visa-1111")), held)

    def test_a_long_name_minted_with_a_digest_is_still_found(self):
        old = base.split_account_id("qif", LONG, grouped_whole=True)
        self.assertNotEqual(old, base.split_account_id("qif", LONG))
        held = self._as_old_build(LONG, old)
        n = self._count()
        migrate.run(_admin_dsn(TEST_DB))
        qifimport.import_qif(self.conn, "chk", _qif(LONG))
        self.assertEqual(self._count(), n)
        self.assertEqual(sum(len(self._rows(a)) for a in self._manual()
                             if a.startswith("manual:qif-platinum")), held)


if __name__ == "__main__":
    unittest.main()


class ADateNamedSource(_Base):
    """Sixteen digits across two slash-written dates are not a card
    number: cut, two instruments named by their terms would read as one
    account and the second's ledger would land in the first."""

    def test_two_sources_named_by_their_terms_stay_two_accounts(self):
        first = "CD 03/15/2025 - 09/15/2025"
        second = "CD 01/15/2025 - 07/15/2025"
        self.assertEqual(base.mask_account_name(first), first)
        self.assertNotEqual(base.split_account_id("csv", first),
                            base.split_account_id("csv", second))
        qifimport.import_qif(self.conn, "chk", _qif(first))
        qifimport.import_qif(self.conn, "chk", _qif(second))
        names = set(self._manual().values())
        self.assertIn(first, names)
        self.assertIn(second, names)
        # while a card typed in its usual layout is still cut, whatever
        # separates its groups
        self.assertEqual(base.mask_account_name("Visa 4111 1111 1111 1111"),
                         "Visa \u20261111")
        self.assertEqual(base.mask_account_name("Visa 4111.1111.1111.1111"),
                         "Visa \u20261111")
