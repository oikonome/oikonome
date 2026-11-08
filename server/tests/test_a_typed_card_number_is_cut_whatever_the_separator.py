"""A new account's stored name keeps only the last four of a number.

Every door that makes a hand-made account (the bulk import, the add-account
form, the API) takes its stored name from bulk_import.stored_name; the id
is opaque and holds no part of the name. A card typed with dots, slashes,
doubled spaces or mixed separators must be cut like any other, or the
page, every export and the categorizer's prompt show the full number; and
a name whose slug an earlier build would have read as a number is cut the
way that slug read it, so it keeps no more digits than the ids those
builds derived from it."""
import unittest

from oikonome.db import migrate
from oikonome.sync import base, card_number, qifimport
from oikonome.web import bulk_import

from .card_number_fixture import CASES
from .util import TEST_DB, _admin_dsn, make_db, write_config


class TheStoredNameIsCut(unittest.TestCase):
    def test_dots_slashes_doubled_and_mixed_separators(self):
        for typed in ("Visa 4000.1234.5678.9010", "Visa 4000/1234/5678/9010",
                      "Visa 4000  1234  5678  9010",
                      "Visa 4000-1234 5678-9010"):
            self.assertEqual(bulk_import.stored_name(typed), "Visa …9010",
                             typed)

    def test_every_shared_case(self):
        for typed, cut in CASES:
            if len(typed) > bulk_import.NEW_ACCOUNT_NAME_MAX:
                continue
            shown = bulk_import.stored_name(typed)
            self.assertEqual(shown, cut, typed)
            self.assertFalse(card_number.holds_number(shown), typed)

    def test_a_separator_only_the_slug_reads(self):
        self.assertEqual(
            bulk_import.stored_name("Visa_4000_0000_0000_0002"), "Visa_…0002")


class AnAccountStoredWholeIsTheSameAccount(unittest.TestCase):
    """A build whose cut missed the separator stored the full name under
    the cut slug. The same file imported again lands on that account, not
    beside it, and the migrate's name scrub cuts the stored name."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name)"
            " VALUES ('manual','manual','Manual') ON CONFLICT DO NOTHING")
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype)"
            " VALUES ('manual:visa-9010','manual',%s,'credit','credit card')",
            ("Visa 4000.1234.5678.9010",))

    def test_reimport_reuses_it(self):
        aid = bulk_import._ensure_account(self.conn,
                                          "Visa 4000.1234.5678.9010", "card")
        self.assertEqual(aid, "manual:visa-9010")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM accounts WHERE id LIKE 'manual:visa%%'"
        ).fetchone()["n"], 1)

    def test_the_name_scrub_cuts_it(self):
        self.assertEqual(base.mask_manual_account_names(self.conn), 1)
        self.assertEqual(self.conn.execute(
            "SELECT name FROM accounts WHERE id = 'manual:visa-9010'"
        ).fetchone()["name"], "Visa …9010")


DOTTED = "Visa 4000.0000.0000.0002"        # an invented card number


def _qif(name):
    return (f"!Account\nN{name}\nTBank\n^\n"
            "!Type:Bank\nD03/04/2026\nT-61.20\nPFABRIKAM GROCER\n^\n"
            "D03/05/2026\nT-5.00\nPSERVICE FEE\n^\n"
            "!Account\nNHarbor Savings\nTBank\n^\n"
            "!Type:Bank\nD03/08/2026\nT-12.00\nPCONTOSO CAFE\n^\n")


class ASplitSourceTheEarlierCutMissed(unittest.TestCase):
    """A split file whose source is a card typed with dots: an earlier
    build kept the number whole in the id and the rows. The same file
    imported again, before and after the migrate moves the account, adds
    nothing."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _count(self):
        return self.conn.execute(
            "SELECT count(*) AS n FROM transactions WHERE account_id LIKE"
            " 'manual:qif-visa%%'").fetchone()["n"]

    def test_reimport_lands_on_the_account_it_made(self):
        old = base.split_account_id("qif", DOTTED, grouped_whole=True)
        self.assertEqual(old, "manual:qif-visa-4000-0000-0000-0002")
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name,"
            " access_token) VALUES ('manual','manual','Manual',NULL)"
            " ON CONFLICT (tenant_id, id) DO NOTHING")
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype)"
            " VALUES (%s,'manual',%s,'depository','checking')", (old, DOTTED))
        qifimport.import_qif(self.conn, "chk", _qif(DOTTED))
        self.conn.execute(
            "UPDATE transactions SET raw = jsonb_set(raw, '{account}',"
            " to_jsonb(%s::text)) WHERE account_id = %s", (DOTTED, old))
        n = self._count()
        self.assertEqual(n, 2)
        qifimport.import_qif(self.conn, "chk", _qif(DOTTED))
        self.assertEqual(self._count(), n)
        migrate.run(_admin_dsn(TEST_DB))
        ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM accounts WHERE id LIKE 'manual:qif-visa%%'")]
        self.assertEqual(ids, ["manual:qif-visa-0002"])
        qifimport.import_qif(self.conn, "chk", _qif(DOTTED))
        self.assertEqual(self._count(), n)
        self.assertEqual(self.conn.execute(
            "SELECT name FROM accounts WHERE id = 'manual:qif-visa-0002'"
        ).fetchone()["name"], "Visa …0002")


if __name__ == "__main__":
    unittest.main()
