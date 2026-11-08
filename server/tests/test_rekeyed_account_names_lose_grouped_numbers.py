"""An account's name, the name the person gave it and the institution's
name for it lose a card number typed in groups, wherever they are kept:
when a re-key moves the account off an id that spelled the number, in
the one pass `oikonome migrate` makes over every hand-made account's
stored names, and when an archive brings such a name back.

The re-key cut the number from the id and copied the names across
whole, and restore and migration 189 cut only unbroken runs of eight, so
"Visa 4111 1111 1111 1111" kept riding every page, /export and the
portability archive.
"""
import re
import tempfile
import unittest

from oikonome.db import migrate, tenancy
from oikonome.sync import base, export, restore

from .util import TEST_DB, _admin_dsn, make_db, write_config

GROUPED_RUN = re.compile(r"[0-9](?:[^A-Za-z0-9\u2026]*[0-9]){11}")
FULL = "Visa 4111 1111 1111 1111"          # a test card number
RENAMED = "My card 4111-1111-1111-1111"
# the same number split the other ways a card or a statement prints it
DOTTED = "Visa 4111.1111.1111.1111"
SLASHED = "Card 4111/1111/1111/1111"
SPACED = "Card 4111  1111  1111  1111"


def _seed(conn, aid, full=FULL, renamed=RENAMED, official=None):
    conn.execute(
        "INSERT INTO items (id, aggregator, institution_name, access_token)"
        " VALUES ('manual','manual','Manual',NULL)"
        " ON CONFLICT (tenant_id, id) DO NOTHING")
    conn.execute(
        "INSERT INTO accounts (id, item_id, name, display_name,"
        " official_name, type, subtype) VALUES"
        " (%s,'manual',%s,%s,%s,'credit','credit card')",
        (aid, full, renamed, official or full))


def _names(conn):
    return {r["id"]: (r["name"], r["display_name"], r["official_name"])
            for r in conn.execute(
                "SELECT id, name, display_name, official_name FROM accounts"
                " WHERE item_id = 'manual'").fetchall()}


class _Base(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def assertCut(self, names):
        self.assertTrue(names)
        for aid, cols in names.items():
            for v in cols:
                self.assertFalse(GROUPED_RUN.search(v or ""), (aid, v))


class Rekey(_Base):
    def test_rekey_cuts_every_name_of_the_moved_account(self):
        _seed(self.conn, "manual:visa-4111-1111-1111-1111")
        moved = base.rekey_numbered_accounts(self.conn)
        self.assertEqual(list(moved.values()), ["manual:visa-1111"])
        names = _names(self.conn)
        self.assertCut(names)
        self.assertEqual(names["manual:visa-1111"],
                         ("Visa …1111", "My card …1111",
                          "Visa …1111"))



class StoredNames(_Base):
    def test_migrate_cuts_grouped_numbers_from_stored_manual_names(self):
        # an id that never spelled the number: nothing to re-key, only
        # the names to cut
        _seed(self.conn, "manual:visa")
        migrate.run(_admin_dsn(TEST_DB))
        self.assertCut(_names(self.conn))
        self.assertEqual(base.mask_manual_account_names(self.conn), 0)

    def test_short_dated_names_keep_their_digits(self):
        # a year range or a date is not a number to cut
        _seed(self.conn, "manual:college", "College 2023-2024",
              "Checking 12/31/2024", "Fund 2024.01")
        self.assertEqual(base.mask_manual_account_names(self.conn), 0)
        self.assertEqual(_names(self.conn)["manual:college"],
                         ("College 2023-2024", "Checking 12/31/2024",
                          "Fund 2024.01"))


class _RenameBetweenReadAndWrite:
    """`conn` with a rename committed on another connection right after
    the name cut reads the accounts, as a person renaming the account in
    another tab would."""

    def __init__(self, conn, other, aid, renamed):
        self._conn, self._other = conn, other
        self._aid, self._renamed, self.fired = aid, renamed, False

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, *a, **kw):
        cur = self._conn.execute(sql, *a, **kw)
        if (not self.fired and str(sql).lstrip().startswith("SELECT id, ")
                and "FROM accounts" in str(sql)):
            self.fired = True
            rows = cur.fetchall()
            self._other.execute(
                "UPDATE accounts SET display_name = %s WHERE id = %s",
                (self._renamed, self._aid))
            return _Rows(rows)
        return cur


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class ConcurrentRename(_Base):
    def test_a_rename_between_the_read_and_the_write_survives(self):
        _seed(self.conn, "manual:visa")
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        other = tenancy.tenant_connect(str(tid))
        self.addCleanup(other.close)
        racing = _RenameBetweenReadAndWrite(
            self.conn, other, "manual:visa", "Joint card")
        self.assertEqual(base.mask_manual_account_names(racing), 1)
        self.assertTrue(racing.fired)
        self.assertEqual(_names(self.conn)["manual:visa"],
                         ("Visa …1111", "Joint card", "Visa …1111"))


class Restore(_Base):
    def test_restore_cuts_a_grouped_name_an_archive_brings(self):
        _seed(self.conn, "manual:visa")
        with tempfile.TemporaryFile() as fh:
            export.build_zip(self.conn, out=fh)
            fh.seek(0)
            blob = fh.read()
        dest = make_db()
        self.addCleanup(dest.close)
        restore.restore_zip(dest, blob)
        self.assertIn("manual:visa", _names(dest))
        self.assertCut(_names(dest))


class ALinkedAccountsDisplayName(_Base):
    """A display name is typed by a person whatever the account's
    source, so a card number typed into a linked account's is cut by the
    migrate pass like a hand-made account's; its bank-given name is not
    the pass's business."""

    def test_the_pass_cuts_a_linked_accounts_display_name(self):
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name,"
            " access_token) VALUES ('it-bank','plaid','Bank','tok')"
            " ON CONFLICT (tenant_id, id) DO NOTHING")
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, display_name, type,"
            " subtype) VALUES ('linked-card','it-bank','Visa Signature',"
            " 'Visa 4111 1111 1111 1111','credit','credit card')")
        self.assertGreaterEqual(base.mask_manual_account_names(self.conn), 1)
        row = self.conn.execute(
            "SELECT name, display_name FROM accounts WHERE id='linked-card'"
        ).fetchone()
        self.assertEqual(row["display_name"], "Visa \u20261111")
        self.assertEqual(row["name"], "Visa Signature")
        self.assertEqual(base.mask_manual_account_names(self.conn), 0)
