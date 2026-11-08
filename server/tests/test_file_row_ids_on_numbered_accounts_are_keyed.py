"""A file row imported into a split account an earlier release named by its
full account number carries no id an export holder can search for that
number.

A file row's id was an unkeyed hash of "<account id>|date|cents|name|n".
Migration 199 moved the account onto an id that keeps only the last four,
but left the rows' ids alone, and each was still the hash over the old id
with the whole number in it. Every other input of that hash rides the
export beside the row, and the last four ride the new account id, so the
number was a short search away. These rows are re-id'd under a secret the
household keeps and no export carries, and a re-import of the same file
still lands on them.
"""
import hashlib
import hmac
import io
import json
import pathlib
import re
import tempfile
import unittest
import zipfile
from unittest import mock

import psycopg

from oikonome import tenant_export
from oikonome.db import crypto, migrate
from oikonome.sync import (base, csvimport, export, mintimport,
                           qifimport, restore)

from .util import TEST_DB, _admin_dsn, make_db, write_config

MIGRATION_199 = (pathlib.Path(base.__file__).parent.parent / "db"
                 / "migrations"
                 / "199_split_account_ids_and_source_cells_lose_full_numbers.sql")

NUMBER = "1234567890123"            # invented
TWIN = "9876543210123"              # invented; the same last four
A_ROWS = [(4, -61.20, "SPRINGFIELD GROCER"), (5, -5.00, "SERVICE FEE"),
          (5, -5.00, "SERVICE FEE")]
T_ROWS = [(9, -14.00, "NORTHWIND FUEL"), (10, -3.50, "CONTOSO STREAM")]
H_ROWS = [(8, -12.00, "FABRIKAM CAFE")]
UNKEYED = re.compile(r"^[a-z]+:[0-9a-f]{40}$")


def _legacy_ensure(conn, prefix, name, *, key=None, type_=None,
                   subtype=None, lines=None):
    """The split account as an earlier release made it: the plain slug of
    the source name, a full number and all."""
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
    out = []
    for name, rows in accounts:
        out += ["!Account", f"N{name}", "TBank", "^", "!Type:Bank"]
        for day, amount, payee in rows:
            out += [f"D03/{day:02d}/2026", f"T{amount:.2f}", f"P{payee}",
                    "^"]
    return "\n".join(out) + "\n"


def _mint(*accounts):
    out = ["Date,Description,Original Description,Amount,Transaction Type,"
           "Category,Account Name,Labels,Notes"]
    for name, rows in accounts:
        for day, amount, payee in rows:
            kind = "debit" if amount < 0 else "credit"
            out.append(f"3/{day}/2026,{payee},{payee},{abs(amount):.2f},"
                       f"{kind},Groceries,{name},,")
    return "\n".join(out) + "\n"


def _tenant(conn) -> str:
    return conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]


def _export(conn) -> bytes:
    with tempfile.TemporaryFile() as fh:
        export.build_zip(conn, out=fh)
        fh.seek(0)
        return fh.read()


def _secret(conn) -> str | None:
    row = conn.execute("SELECT secret FROM txn_id_keys").fetchone()
    return None if row is None else crypto.decrypt(conn, row["secret"])


class KeyedIdsTests(unittest.TestCase):
    """QIF: one source named by its number, one by a word."""

    PREFIX, MODULE = "qif", qifimport
    OLD = f"manual:qif-{NUMBER}"

    def _file(self, *accounts):
        return _qif(*accounts)

    def _import(self, conn, text):
        return self.MODULE.import_qif(conn, "chk", text)

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        self.text = self._file((NUMBER, A_ROWS), ("Harbor Savings", H_ROWS))
        with mock.patch.object(self.MODULE, "ensure_split_account",
                               _legacy_ensure):
            self._import(self.conn, self.text)
        # every id the earlier release minted for the numbered source
        ids = base.FileImportIds(self.PREFIX, self.OLD)
        self.legacy_ids = {ids.id_for(*self._line(r)) for r in A_ROWS}
        held = {r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions WHERE account_id = %s",
            (self.OLD,)).fetchall()}
        self.assertEqual(held, self.legacy_ids)
        # the account moves, as migration 199 moved the stored ones
        self.conn.execute(MIGRATION_199.read_text())

    @staticmethod
    def _line(row):
        import datetime as dt
        day, amount, payee = row
        return dt.date(2026, 3, day), -amount, payee

    def _moved(self, conn):
        return conn.execute(
            "SELECT account_id AS v FROM account_adoptions"
            " WHERE old_account_id = %s",
            (base.split_lineage_id(self.OLD),)).fetchone()["v"]

    def _ids(self, conn, aid):
        return {r["id"] for r in conn.execute(
            "SELECT id FROM transactions WHERE account_id = %s",
            (aid,)).fetchall()}

    def _count(self, conn):
        return conn.execute(
            "SELECT count(*) AS n FROM transactions WHERE account_id LIKE %s",
            (f"manual:{self.PREFIX}-%",)).fetchone()["n"]

    def _assert_keyed(self, conn, aid):
        ids = self._ids(conn, aid)
        self.assertEqual(len(ids), len(A_ROWS))
        self.assertEqual(ids & self.legacy_ids, set(), ids)
        for tid in ids:
            self.assertIsNone(UNKEYED.match(tid), tid)
        return ids

    def test_migrate_rekeys_every_row_hashed_over_the_full_number(self):
        before = self._count(self.conn)
        migrate.run(_admin_dsn(TEST_DB))
        moved = self._moved(self.conn)
        ids = self._assert_keyed(self.conn, moved)
        secret = _secret(self.conn).encode()
        self.assertEqual(ids, {
            f"{self.PREFIX}:k" + hmac.new(secret, old.encode(),
                                          hashlib.sha1).hexdigest()
            for old in self.legacy_ids})
        # rows of a source never named by a number keep their ids
        harbor = base.FileImportIds(
            self.PREFIX, f"manual:{self.PREFIX}-harbor-savings")
        self.assertEqual(
            self._ids(self.conn, f"manual:{self.PREFIX}-harbor-savings"),
            {harbor.id_for(*self._line(r)) for r in H_ROWS})
        self.assertEqual(self._count(self.conn), before)

    def test_running_it_again_changes_nothing(self):
        base.key_file_import_ids(self.conn)
        moved = self._moved(self.conn)
        first = self._assert_keyed(self.conn, moved)
        self.assertEqual(base.key_file_import_ids(self.conn), 0)
        migrate.run(_admin_dsn(TEST_DB))
        self.assertEqual(self._ids(self.conn, moved), first)

    def test_a_reimport_lands_on_the_keyed_rows_and_adds_nothing(self):
        base.key_file_import_ids(self.conn)
        moved = self._moved(self.conn)
        keyed = self._assert_keyed(self.conn, moved)
        before = self._count(self.conn)
        self._import(self.conn, self.text)
        self.assertEqual(self._count(self.conn), before)
        self.assertEqual(self._ids(self.conn, moved), keyed)

    def test_a_card_sharing_the_last_four_is_told_apart_by_its_keyed_rows(
            self):
        """Two numbered sources cut to one lineage key: the re-import
        finds which account is whose by recomputing the rows' ids under
        each full id, and the keyed form is what the account holds."""
        twin = self._file((TWIN, T_ROWS), ("Harbor Savings", H_ROWS))
        with mock.patch.object(self.MODULE, "ensure_split_account",
                               _legacy_ensure):
            self._import(self.conn, twin)
        self.conn.execute(MIGRATION_199.read_text())
        targets = {r["account_id"] for r in self.conn.execute(
            "SELECT account_id FROM account_adoptions"
            " WHERE old_account_id = %s",
            (base.split_lineage_id(self.OLD),)).fetchall()}
        self.assertEqual(len(targets), 2)
        base.key_file_import_ids(self.conn)
        before = self._count(self.conn)
        self._import(self.conn, self.text)
        self._import(self.conn, twin)
        self.assertEqual(self._count(self.conn), before)
        for r in self.conn.execute(
                "SELECT id FROM transactions WHERE account_id = ANY(%s)",
                (sorted(targets),)).fetchall():
            self.assertIsNone(UNKEYED.match(r["id"]), r["id"])

    def test_an_ofx_row_naming_the_numbered_account_moves_to_the_id_ofx_gives_it_now(
            self):
        """An OFX line keyed by its FITID names its account in plain text;
        it moves onto the id an OFX import into the moved account makes,
        so a re-import still finds it by FITID."""
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name)"
            " VALUES (%s, %s, '2026-03-12', 7.25, 'FITID LINE')",
            (f"ofx:{self.OLD}:FIT-77", self._moved(self.conn)))
        base.key_file_import_ids(self.conn)
        moved = self._moved(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT id FROM transactions WHERE name = 'FITID LINE'"
        ).fetchone()["id"], f"ofx:{moved}:FIT-77")

    def test_an_older_archive_of_two_cards_sharing_the_last_four_restores_onto_them(
            self):
        """The restore places each numbered account of an older archive by
        the rows it holds here, which are now keyed."""
        twin = self._file((TWIN, T_ROWS), ("Harbor Savings", H_ROWS))
        with mock.patch.object(self.MODULE, "ensure_split_account",
                               _legacy_ensure):
            self._import(self.conn, twin)
        older = _export(self.conn)
        self.conn.execute(MIGRATION_199.read_text())
        base.key_file_import_ids(self.conn)
        accounts = self.conn.execute(
            "SELECT count(*) AS n FROM accounts").fetchone()["n"]
        before = self._count(self.conn)
        restore.restore_zip(self.conn, older)
        self.assertEqual(self._count(self.conn), before)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM accounts").fetchone()["n"], accounts)

    def test_an_archive_keyed_here_earlier_restores_onto_its_own_rows(self):
        """Two cards sharing the last four, keyed under this household's
        secret before the archive was written, which the household then
        restores over itself. Its keyed ids are the rows here,
        each on its own account: nothing lands twice and no account is
        added, and a second restore adds nothing either."""
        twin = self._file((TWIN, T_ROWS), ("Harbor Savings", H_ROWS))
        with mock.patch.object(self.MODULE, "ensure_split_account",
                               _legacy_ensure):
            self._import(self.conn, twin)
        self.conn.execute(MIGRATION_199.read_text())
        base.key_file_import_ids(self.conn)
        secret = _secret(self.conn)
        keyed_earlier = _export(self.conn)
        by_account = {r["account_id"]: r["n"] for r in self.conn.execute(
            "SELECT account_id, count(*) AS n FROM transactions"
            " GROUP BY account_id").fetchall()}
        accounts = self.conn.execute(
            "SELECT count(*) AS n FROM accounts").fetchone()["n"]
        for _ in range(2):
            restore.restore_zip(self.conn, keyed_earlier)
            self.assertEqual(_secret(self.conn), secret)
            self.assertEqual({r["account_id"]: r["n"] for r in
                              self.conn.execute(
                "SELECT account_id, count(*) AS n FROM transactions"
                " GROUP BY account_id").fetchall()}, by_account)
            self.assertEqual(self.conn.execute(
                "SELECT count(*) AS n FROM accounts").fetchone()["n"],
                accounts)

    def test_the_secret_never_leaves_and_a_restore_elsewhere_mints_its_own(
            self):
        older = _export(self.conn)        # written before the re-id
        base.key_file_import_ids(self.conn)
        secret = _secret(self.conn)
        self.assertTrue(secret)
        stored = self.conn.execute(
            "SELECT secret FROM txn_id_keys").fetchone()["secret"]
        data = _export(self.conn)
        archive = tenant_export.build_archive(
            _tenant(self.conn), operator="t", consented=True)
        for blob in (data, archive):
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                text = b"".join(z.read(n) for n in z.namelist())
            self.assertNotIn(secret.encode(), text)
            self.assertNotIn(stored.encode(), text)
        keyed = self._ids(self.conn, self._moved(self.conn))
        # an archive written before the re-id, restored into another
        # household: its rows are re-id'd there under a secret of its own
        dest = make_db()
        self.addCleanup(dest.close)
        write_config(dest)
        restore.restore_zip(dest, older)
        mine = _secret(dest)
        self.assertTrue(mine)
        self.assertNotEqual(mine, secret)
        there = self._assert_keyed(dest, self._moved(dest))
        self.assertEqual(there & keyed, set())
        before = self._count(dest)
        self._import(dest, self.text)
        self.assertEqual(self._count(dest), before)
        # and one written after it carries the source's keyed ids, which
        # nothing there can recompute: each row lands keyed under that
        # household's own secret, by the id an import there mints for it
        other = make_db()
        self.addCleanup(other.close)
        write_config(other)
        restore.restore_zip(other, data)
        theirs = _secret(other)
        self.assertTrue(theirs)
        self.assertNotEqual(theirs, secret)
        moved = self._moved(other)
        there = self._assert_keyed(other, moved)
        self.assertEqual(there & keyed, set())
        import datetime as dt
        self.assertEqual(there, {
            base.keyed_file_id(theirs.encode(), base.file_row_id(
                self.PREFIX, moved, dt.date(2026, 3, day), -amount, payee,
                n))
            for (day, amount, payee), n in zip(A_ROWS, (1, 1, 2))})

    def _note_and_pin(self, conn, tid):
        conn.execute(
            "INSERT INTO transaction_notes (txn_id, note) VALUES (%s, 'kept')",
            (tid,))
        conn.execute(
            "INSERT INTO manual_categories (transaction_id, category)"
            " VALUES (%s, 'GENERAL_SERVICES')", (tid,))

    def _attached(self, conn):
        """How many notes and pins still stand on a row of the ledger."""
        notes = conn.execute(
            "SELECT count(*) AS n FROM transaction_notes n"
            "  JOIN transactions t ON t.id = n.txn_id"
            " WHERE n.note = 'kept'").fetchone()["n"]
        pins = conn.execute(
            "SELECT count(*) AS n FROM manual_categories m"
            "  JOIN transactions t ON t.id = m.transaction_id"
            " WHERE m.category = 'GENERAL_SERVICES'").fetchone()["n"]
        return notes, pins

    def test_a_household_restored_elsewhere_after_keying_reimports_without_doubling(
            self):
        """The archive carries the keyed ids and never the secret, so the
        household it lands in cannot recompute them; the re-import still
        lands on the restored rows, and their notes and pins stay put."""
        base.key_file_import_ids(self.conn)
        keyed = self._assert_keyed(self.conn, self._moved(self.conn))
        self._note_and_pin(self.conn, sorted(keyed)[0])
        data = _export(self.conn)
        dest = make_db()
        self.addCleanup(dest.close)
        write_config(dest)
        restore.restore_zip(dest, data)
        before = self._count(dest)
        self.assertEqual(self._attached(dest), (1, 1))
        for _ in range(2):
            self._import(dest, self.text)
            self.assertEqual(self._count(dest), before)
            self.assertEqual(self._attached(dest), (1, 1))
        # and the restored rows answer exactly to the ids an import mints
        # there now, so the doors that only look rows up by id find them
        there = self._ids(dest, self._moved(dest))
        self.assertEqual(there & keyed, set())
        for tid in there:
            self.assertIsNone(UNKEYED.match(tid), tid)

    def test_keyed_rows_whose_secret_is_not_here_take_a_reimport_without_doubling(
            self):
        """A household holding keyed rows under no secret at all, or
        under a secret that did not produce them: a re-import matches each
        line to its row by content, one to one."""
        base.key_file_import_ids(self.conn)
        keyed = self._assert_keyed(self.conn, self._moved(self.conn))
        self._note_and_pin(self.conn, sorted(keyed)[0])
        before = self._count(self.conn)
        self.conn.execute("DELETE FROM txn_id_keys")
        self._import(self.conn, self.text)
        self.assertEqual(self._count(self.conn), before)
        self.assertEqual(self._attached(self.conn), (1, 1))
        # the import keyed its account's source under a secret of its own
        # (account_sources); this one did not produce the rows either
        self.conn.execute("DELETE FROM txn_id_keys")
        self.conn.execute(
            "INSERT INTO txn_id_keys (secret) VALUES (%s)",
            (crypto.encrypt(self.conn, "f" * 64),))
        self._import(self.conn, self.text)
        self.assertEqual(self._count(self.conn), before)
        self.assertEqual(self._attached(self.conn), (1, 1))
        self.assertEqual(self._ids(self.conn, self._moved(self.conn)), keyed)

    def _log_on(self, conn, tid):
        from oikonome.engine import activity
        activity.record(conn, "owner@example.dev", "split", "set",
                        target=tid, label="SPRINGFIELD GROCER",
                        detail={"parts": []})
        activity.record(conn, "owner@example.dev", "category", "bulk",
                        label="SPRINGFIELD GROCER",
                        detail={"after": "GENERAL_SERVICES", "count": 1,
                                "ids": [tid, "plaid-row-1"]})

    def _assert_names_no_moved_id(self, blob, ids):
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            text = b"".join(z.read(n) for n in z.namelist())
        for tid in ids:
            self.assertNotIn(tid.split(":", 1)[1].encode(), text, tid)

    def test_no_export_names_a_row_by_the_id_it_was_moved_off(self):
        """The activity log names a row by its id beside a sentence giving
        its date, amount and name; the old id follows the row."""
        old = sorted(self.legacy_ids)[0]
        self._log_on(self.conn, old)
        older = _export(self.conn)
        base.key_file_import_ids(self.conn)
        new = base.keyed_file_id(_secret(self.conn).encode(), old)
        self.assertEqual({r["target"] for r in self.conn.execute(
            "SELECT target FROM activity_log WHERE target IS NOT NULL")},
            {new})
        self.assertEqual(self.conn.execute(
            "SELECT detail->'ids' AS v FROM activity_log"
            " WHERE detail ? 'ids'").fetchone()["v"], [new, "plaid-row-1"])
        self._assert_names_no_moved_id(_export(self.conn), self.legacy_ids)
        self._assert_names_no_moved_id(tenant_export.build_archive(
            _tenant(self.conn), operator="t", consented=True),
            self.legacy_ids)
        # an archive written before the move, restored here or elsewhere
        restore.restore_zip(self.conn, older)
        self._assert_names_no_moved_id(_export(self.conn), self.legacy_ids)
        dest = make_db()
        self.addCleanup(dest.close)
        write_config(dest)
        restore.restore_zip(dest, older)
        self._assert_names_no_moved_id(_export(dest), self.legacy_ids)

    def test_a_log_left_behind_by_an_earlier_move_is_carried_by_the_next_migrate(
            self):
        old = sorted(self.legacy_ids)[0]
        self._log_on(self.conn, old)
        base.key_file_import_ids(self.conn)
        new = base.keyed_file_id(_secret(self.conn).encode(), old)
        # as a build that moved the rows and not the log left it
        self.conn.execute(
            "UPDATE activity_log SET target = %s WHERE target = %s",
            (old, new))
        self.conn.execute(
            "UPDATE activity_log SET detail = jsonb_set(detail, '{ids}',"
            " jsonb_build_array(%s::text, 'plaid-row-1')) WHERE detail ? 'ids'",
            (old,))
        migrate.run(_admin_dsn(TEST_DB))
        self.assertEqual({r["target"] for r in self.conn.execute(
            "SELECT target FROM activity_log WHERE target IS NOT NULL")},
            {new})
        self.assertEqual(self.conn.execute(
            "SELECT detail->'ids' AS v FROM activity_log"
            " WHERE detail ? 'ids'").fetchone()["v"], [new, "plaid-row-1"])

    def test_migrate_leaves_a_household_whose_sync_is_running_to_the_next_run(
            self):
        """Moving a row while an import or restore of the same household
        is writing could leave the line twice; the household waits for
        the next migrate instead."""
        tid = _tenant(self.conn)
        moved = self._moved(self.conn)
        holder = psycopg.connect(_admin_dsn(TEST_DB), autocommit=True)
        self.addCleanup(holder.close)
        key = f"oikonome:sync:{tid}"
        holder.execute("SELECT pg_advisory_lock(hashtext(%s))", (key,))
        with self.assertLogs("oikonome.db", "WARNING") as logs:
            migrate.run(_admin_dsn(TEST_DB))
        self.assertEqual(self._ids(self.conn, moved), self.legacy_ids)
        self.assertTrue(any(tid in m for m in logs.output), logs.output)
        holder.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
        migrate.run(_admin_dsn(TEST_DB))
        self._assert_keyed(self.conn, moved)

    def test_a_household_busy_at_its_turn_is_taken_again_after_the_pass(
            self):
        """A sync or restore holding the lock at the household's turn is
        usually done by the end of the pass; the household is tried once
        more then rather than left until the next deploy."""
        tid = _tenant(self.conn)
        moved = self._moved(self.conn)
        real = base.tenant_sync_lock
        turns = []

        def busy_first(conn, tenant_id):
            turns.append(str(tenant_id))
            if turns.count(str(tenant_id)) == 1 and str(tenant_id) == tid:
                import contextlib
                return contextlib.nullcontext(False)
            return real(conn, tenant_id)

        with mock.patch.object(base, "tenant_sync_lock", busy_first):
            migrate.run(_admin_dsn(TEST_DB))
        self.assertEqual(turns.count(tid), 2)
        self._assert_keyed(self.conn, moved)

    def test_an_older_archive_restored_here_again_adds_nothing(self):
        older = _export(self.conn)
        base.key_file_import_ids(self.conn)
        moved = self._moved(self.conn)
        keyed = self._assert_keyed(self.conn, moved)
        note = keyed and sorted(keyed)[0]
        self.conn.execute(
            "INSERT INTO transaction_notes (txn_id, note) VALUES (%s, 'kept')",
            (note,))
        before = self._count(self.conn)
        restore.restore_zip(self.conn, older)
        self.assertEqual(self._count(self.conn), before)
        self.assertEqual(self._ids(self.conn, moved), keyed)
        self.assertEqual(self.conn.execute(
            "SELECT note FROM transaction_notes WHERE txn_id = %s",
            (note,)).fetchone()["note"], "kept")

    def _household_keyed_on_its_own(self):
        """Another household that imported the same file under the same
        earlier release, and was keyed under a secret of its own."""
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        with mock.patch.object(self.MODULE, "ensure_split_account",
                               _legacy_ensure):
            self._import(conn, self.text)
        conn.execute(MIGRATION_199.read_text())
        base.key_file_import_ids(conn)
        return conn

    def test_an_archive_keyed_under_another_secret_lands_on_the_rows_keyed_here(
            self):
        """Both households keyed the same lines from the id over the full
        old account id, each under its own secret; neither keyed id is the
        one an import mints today, so the archive's rows meet this
        household's by content, one to one, instead of landing beside
        them — and a later restore or re-import adds nothing."""
        base.key_file_import_ids(self.conn)
        mine = self._assert_keyed(self.conn, self._moved(self.conn))
        self._note_and_pin(self.conn, sorted(mine)[0])
        data = _export(self.conn)
        dest = self._household_keyed_on_its_own()
        moved = self._moved(dest)
        keyed = self._assert_keyed(dest, moved)
        before = self._count(dest)
        for _ in range(2):
            restore.restore_zip(dest, data)
            self.assertEqual(self._count(dest), before)
            self.assertEqual(self._ids(dest, moved), keyed)
            self.assertEqual(self._attached(dest), (1, 1))
        self._import(dest, self.text)
        self.assertEqual(self._count(dest), before)
        self.assertEqual(self._ids(dest, moved), keyed)

    def _log_target(self, conn, tid):
        from oikonome.engine import activity
        activity.record(conn, "owner@example.dev", "split", "set",
                        target=tid, label="FITID LINE",
                        detail={"parts": []})

    def test_a_stale_ofx_mention_follows_its_own_account_and_is_read_once(
            self):
        """A mention an earlier move left on an OFX id naming the numbered
        account is carried to the id OFX gives the line on the account
        that number moved to — never onto a row of another account that
        repeats the FITID — whether or not that row is still here; and
        once carried, no later migrate looks for it again. A mention on a
        numbered account nothing moved off is not looked for at all."""
        moved = self._moved(self.conn)
        # banks repeat FITIDs across accounts
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name)"
            " VALUES ('ofx:chk:FIT-77', 'chk', '2026-03-12', 7.25,"
            "         'ANOTHER LINE')")
        for fitid in ("FIT-77", "FIT-88"):
            self._log_target(self.conn, f"ofx:{self.OLD}:{fitid}")
        self._log_target(self.conn, "ofx:manual:qif-55555555:FIT-9")
        base.carry_stale_mentions(self.conn)
        self.assertEqual({r["target"] for r in self.conn.execute(
            "SELECT target FROM activity_log")},
            {f"ofx:{moved}:FIT-77", f"ofx:{moved}:FIT-88",
             "ofx:manual:qif-55555555:FIT-9"})
        self.assertEqual(self.conn.execute(
            base.STALE_MENTION_TENANTS_SQL, base.MENTION_ARGS).fetchall(),
            [])

    def test_a_mention_of_a_deleted_row_keeps_its_plain_id(self):
        """A mention of a plain row the household deleted, held in no form,
        is left on its plain id: the row may come back under it (a
        re-import on an account with no number), and a keyed form nothing
        will ever hold would orphan the mention for good."""
        base.key_file_import_ids(self.conn)
        gone = f"{self.PREFIX}:" + "ab" * 20
        self._log_on(self.conn, gone)
        base.carry_stale_mentions(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT target FROM activity_log WHERE kind = 'split'"
            " AND label = 'SPRINGFIELD GROCER' ORDER BY at DESC LIMIT 1"
        ).fetchone()["target"], gone)


class MintKeyedIdsTests(KeyedIdsTests):
    PREFIX, MODULE = "mint", mintimport
    OLD = f"manual:mint-{NUMBER}"

    def _file(self, *accounts):
        return _mint(*accounts)

    def _import(self, conn, text):
        return mintimport.import_mint(conn, "chk", text)


NUMBERED_LINE = "PAYMENT TO CARD " + NUMBER       # invented
CSV_MAPPING = {"date": "Date", "name": "Description",
               "debit": "Debit", "credit": "Credit"}
CSV_TEXT = ("Date,Description,Debit,Credit\n"
            f"2026-03-04,{NUMBERED_LINE},61.20,\n"
            "2026-03-05,CORNER GROCER …4321,12.00,\n"
            "2026-03-06,HARBOR BAKERY,8.00,\n")


def _rows_on(conn, aid):
    return {r["name"]: r["id"] for r in conn.execute(
        "SELECT id, name FROM transactions WHERE account_id = %s",
        (aid,)).fetchall()}


class PreMaskKeyedIdsTests(unittest.TestCase):
    """A file row imported before names were masked has an id hashed over
    the full number its statement line printed, though the name stored
    beside it (and every export of it) shows the last four alone. It is
    re-id'd under the household's secret like a row on a once-numbered
    account; a row whose id already hashes the masked name, and a row
    whose name never held a number, keep theirs."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        csvimport.import_csv(self.conn, "chk", CSV_TEXT, CSV_MAPPING)
        import datetime as dt
        ids = base.FileImportIds("csv", "chk")
        masked = ids.id_for(dt.date(2026, 3, 4), 61.20, NUMBERED_LINE)
        # the id the line had before names were masked
        self.full = ids.legacy_id(masked)
        self.assertIsNotNone(self.full)
        self.conn.execute("UPDATE transactions SET id = %s WHERE id = %s",
                          (self.full, masked))
        self.conn.execute(
            "INSERT INTO transaction_notes (txn_id, note) VALUES (%s, 'mine')",
            (self.full,))
        # a 20-digit digest of the same importer family, whose name was
        # masked after it landed
        self.short = "mint:" + "0123456789abcdef0123"
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name)"
            " VALUES (%s, 'chk', '2026-02-01', 9.99, 'TRANSFER …7788')",
            (self.short,))
        self.before = _rows_on(self.conn, "chk")

    def _secret(self) -> bytes:
        return _secret(self.conn).encode()

    def test_migrate_keys_every_row_whose_id_hashes_a_full_number(self):
        migrate.run(_admin_dsn(TEST_DB))
        after = _rows_on(self.conn, "chk")
        secret = self._secret()
        self.assertEqual(after["PAYMENT TO CARD …0123"],
                         base.keyed_file_id(secret, self.full))
        self.assertEqual(after["TRANSFER …7788"],
                         base.keyed_file_id(secret, self.short))
        # hashed over the masked name already, or never holding a number
        for name in ("CORNER GROCER …4321", "HARBOR BAKERY"):
            self.assertEqual(after[name], self.before[name], name)
        self.assertEqual(len(after), len(self.before))
        self.assertEqual(self.conn.execute(
            "SELECT txn_id FROM transaction_notes WHERE note = 'mine'"
        ).fetchone()["txn_id"], after["PAYMENT TO CARD …0123"])

    def test_running_it_again_changes_nothing(self):
        self.assertEqual(base.key_file_import_ids(self.conn), 2)
        first = _rows_on(self.conn, "chk")
        self.assertEqual(base.key_file_import_ids(self.conn), 0)
        migrate.run(_admin_dsn(TEST_DB))
        self.assertEqual(_rows_on(self.conn, "chk"), first)

    def test_a_reimport_lands_on_the_keyed_row_and_adds_nothing(self):
        base.key_file_import_ids(self.conn)
        keyed = _rows_on(self.conn, "chk")
        csvimport.import_csv(self.conn, "chk", CSV_TEXT, CSV_MAPPING)
        self.assertEqual(_rows_on(self.conn, "chk"), keyed)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM transactions WHERE account_id = 'chk'"
        ).fetchone()["n"], len(keyed))

    def test_an_older_archive_restored_here_again_adds_nothing(self):
        older = _export(self.conn)
        base.key_file_import_ids(self.conn)
        keyed = _rows_on(self.conn, "chk")
        restore.restore_zip(self.conn, older)
        self.assertEqual(_rows_on(self.conn, "chk"), keyed)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM transactions WHERE account_id = 'chk'"
        ).fetchone()["n"], len(keyed))

    def test_an_archive_under_masked_ids_lands_on_the_pre_masking_row(self):
        """An archive naming the line by the id an import mints today (its
        name hashed masked) meets this household's row held under the id
        from before names were masked, which no archive can spell, by its
        content: one row, with the note this household put on it."""
        other = make_db()
        self.addCleanup(other.close)
        write_config(other)
        csvimport.import_csv(other, "chk", CSV_TEXT, CSV_MAPPING)
        masked = _export(other)
        restore.restore_zip(self.conn, masked)
        # (the restore then keys what hashes a whole number, as migrate does)
        after = _rows_on(self.conn, "chk")
        self.assertEqual(set(after), set(self.before))
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM transactions WHERE account_id = 'chk'"
        ).fetchone()["n"], len(self.before))
        self.assertEqual(self.conn.execute(
            "SELECT txn_id FROM transaction_notes WHERE note = 'mine'"
        ).fetchone()["txn_id"], after["PAYMENT TO CARD …0123"])

    def test_an_older_archive_restored_elsewhere_is_keyed_there(self):
        older = _export(self.conn)
        dest = make_db()
        self.addCleanup(dest.close)
        write_config(dest)
        restore.restore_zip(dest, older)
        there = _rows_on(dest, "chk")
        secret = _secret(dest).encode()
        self.assertEqual(there["PAYMENT TO CARD …0123"],
                         base.keyed_file_id(secret, self.full))
        self.assertEqual(there["HARBOR BAKERY"],
                         self.before["HARBOR BAKERY"])

    def test_a_household_restored_elsewhere_after_keying_reimports_without_doubling(
            self):
        base.key_file_import_ids(self.conn)
        data = _export(self.conn)
        dest = make_db()
        self.addCleanup(dest.close)
        write_config(dest)
        restore.restore_zip(dest, data)
        before = len(_rows_on(dest, "chk"))
        csvimport.import_csv(dest, "chk", CSV_TEXT, CSV_MAPPING)
        self.assertEqual(dest.execute(
            "SELECT count(*) AS n FROM transactions WHERE account_id = 'chk'"
        ).fetchone()["n"], before)
        self.assertEqual(dest.execute(
            "SELECT count(*) AS n FROM transaction_notes n"
            "  JOIN transactions t ON t.id = n.txn_id WHERE n.note = 'mine'"
        ).fetchone()["n"], 1)

    def _household_keyed_from_its_full_id(self):
        """Another household that imported the same file before names were
        masked and was keyed under a secret of its own."""
        dest = make_db()
        self.addCleanup(dest.close)
        write_config(dest)
        csvimport.import_csv(dest, "chk", CSV_TEXT, CSV_MAPPING)
        import datetime as dt
        masked = base.FileImportIds("csv", "chk").id_for(
            dt.date(2026, 3, 4), 61.20, NUMBERED_LINE)
        dest.execute("UPDATE transactions SET id = %s WHERE id = %s",
                     (self.full, masked))
        base.key_file_import_ids(dest)
        return dest

    def test_an_archive_keyed_under_another_secret_lands_on_the_row_keyed_here(
            self):
        """Both households keyed the line from its pre-masking id, each
        under its own secret, so the id an import mints for it today finds
        neither: the archived row meets this household's by content,
        and a later restore or re-import adds nothing."""
        base.key_file_import_ids(self.conn)
        data = _export(self.conn)
        dest = self._household_keyed_from_its_full_id()
        mine = _rows_on(dest, "chk")["PAYMENT TO CARD …0123"]

        def payments():
            return [r["id"] for r in dest.execute(
                "SELECT id FROM transactions WHERE account_id = 'chk'"
                "   AND name = 'PAYMENT TO CARD …0123'")]

        restore.restore_zip(dest, data)
        self.assertEqual(payments(), [mine])
        after = sorted(r["id"] for r in dest.execute(
            "SELECT id FROM transactions"))
        self.assertEqual(dest.execute(
            "SELECT txn_id FROM transaction_notes WHERE note = 'mine'"
        ).fetchone()["txn_id"], mine)
        restore.restore_zip(dest, data)
        csvimport.import_csv(dest, "chk", CSV_TEXT, CSV_MAPPING)
        self.assertEqual(payments(), [mine])
        self.assertEqual(sorted(r["id"] for r in dest.execute(
            "SELECT id FROM transactions")), after)

    def _mention_in_ledger_and_feed_note(self, tid):
        """The two records outside the activity log that name a row by id
        with no key to follow: what a reconnect guard matched a feed row
        to, and the note an earlier guard left on the feed row itself."""
        self.conn.execute(
            "INSERT INTO account_adoptions (account_id, old_account_id,"
            " item_id, guard_ledger) VALUES ('chk', 'manual:chk-old',"
            " 'feed-item', %s)",
            (json.dumps({"absorbed": {"feed-1": tid}}),))
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name,"
            " raw) VALUES ('feed-2', 'chk', '2026-03-09', 1, 'FEED LINE', %s)",
            (json.dumps({"_carried_from": tid}),))

    def _ledger_and_feed_note(self, conn):
        return (conn.execute(
            "SELECT guard_ledger->'absorbed' AS v FROM account_adoptions"
            " WHERE old_account_id = 'manual:chk-old'").fetchone()["v"],
            conn.execute(
                "SELECT raw->>'_carried_from' AS v FROM transactions"
                " WHERE id = 'feed-2'").fetchone()["v"])

    def _assert_never_named(self, blob, tid):
        digest = tid.split(":", 1)[1].encode()
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            self.assertEqual(
                [n for n in z.namelist() if digest in z.read(n)], [])

    def test_no_export_names_the_row_by_its_old_id_in_a_guard_ledger_or_feed_note(
            self):
        self._mention_in_ledger_and_feed_note(self.full)
        base.key_file_import_ids(self.conn)
        new = base.keyed_file_id(self._secret(), self.full)
        self.assertEqual(self._ledger_and_feed_note(self.conn),
                         ({"feed-1": new}, new))
        self._assert_never_named(_export(self.conn), self.full)
        self._assert_never_named(tenant_export.build_archive(
            _tenant(self.conn), operator="t", consented=True), self.full)

    def test_a_guard_ledger_or_feed_note_left_behind_is_carried_by_the_next_migrate(
            self):
        self._mention_in_ledger_and_feed_note(self.full)
        base.key_file_import_ids(self.conn)
        new = base.keyed_file_id(self._secret(), self.full)
        # as a build that moved the row and not these left them
        self.conn.execute(
            "UPDATE account_adoptions SET guard_ledger ="
            " jsonb_build_object('absorbed', jsonb_build_object('feed-1', %s::text))"
            " WHERE old_account_id = 'manual:chk-old'", (self.full,))
        self.conn.execute(
            "UPDATE transactions SET raw = jsonb_build_object("
            "'_carried_from', %s::text) WHERE id = 'feed-2'", (self.full,))
        migrate.run(_admin_dsn(TEST_DB))
        self.assertEqual(self._ledger_and_feed_note(self.conn),
                         ({"feed-1": new}, new))

    def test_a_feed_note_follows_its_row_to_the_id_a_restore_elsewhere_gives_it(
            self):
        self._mention_in_ledger_and_feed_note(self.full)
        base.key_file_import_ids(self.conn)
        data = _export(self.conn)
        dest = make_db()
        self.addCleanup(dest.close)
        write_config(dest)
        restore.restore_zip(dest, data)
        there = _rows_on(dest, "chk")["PAYMENT TO CARD …0123"]
        self.assertEqual(dest.execute(
            "SELECT raw->>'_carried_from' AS v FROM transactions"
            " WHERE id = 'feed-2'").fetchone()["v"], there)


FOREIGN_SECRET = b"f" * 64                     # a secret that is not here
PLAIN_MAPPING = {"date": "Date", "amount": "Amount", "name": "Description"}
LONG_LINE = ("2026-03-06", "-9.99", "X" * 650)
NUL_LINE = ("2026-03-07", "-3.00", "ACME\x00STORE")


def _plain_csv(*lines):
    return "Date,Amount,Description\n" + "".join(
        f"{d},{a},{n}\n" for d, a, n in lines)


class ForeignKeyedRowsMeetTheirLineTests(unittest.TestCase):
    """A row keyed under a secret that is not here is found by its line's
    content, read as the ledger stores it: a name cut to its stored length,
    or with a NUL the ledger cannot hold, is still the same line."""

    def _household(self, *lines, foreign=False):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        csvimport.import_csv(conn, "chk", _plain_csv(*lines), PLAIN_MAPPING)
        if foreign:
            from oikonome.sync import adopt
            adopt.rewrite_txn_ids(conn, [
                (r["id"], base.keyed_file_id(FOREIGN_SECRET, r["id"]))
                for r in conn.execute(
                    "SELECT id FROM transactions ORDER BY id")])
        return conn

    @staticmethod
    def _ids(conn):
        return sorted(r["id"] for r in conn.execute(
            "SELECT id FROM transactions WHERE account_id = 'chk'"))

    def test_a_reimport_finds_a_row_keyed_elsewhere_by_its_stored_name(self):
        for line in (LONG_LINE, NUL_LINE):
            with self.subTest(name=line[2][:8]):
                conn = self._household(line, foreign=True)
                before = self._ids(conn)
                csvimport.import_csv(conn, "chk", _plain_csv(line),
                                     PLAIN_MAPPING)
                self.assertEqual(self._ids(conn), before)

    def test_an_archive_keyed_elsewhere_lands_on_the_row_held_here(self):
        """The archive's name is the stored one, so the id an import here
        minted over the full name cannot be recomputed from it; the row it
        holds is still the archived line's."""
        for line in (LONG_LINE, NUL_LINE):
            with self.subTest(name=line[2][:8]):
                data = _export(self._household(line, foreign=True))
                dest = self._household(line)
                held = self._ids(dest)
                for _ in range(2):
                    restore.restore_zip(dest, data)
                    self.assertEqual(self._ids(dest), held)
                csvimport.import_csv(dest, "chk", _plain_csv(line),
                                     PLAIN_MAPPING)
                self.assertEqual(self._ids(dest), held)


class KeyedRowsSurviveASignRuleChangeTests(unittest.TestCase):
    """A row an earlier reading of its file stored, later re-id'd under
    the household's secret, is still the row a re-import under the
    corrected reading moves or rewrites: never left beside the corrected
    row, counting the money twice."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _live(self, aid):
        return sorted((r["name"], float(r["amount"])) for r in
                      self.conn.execute(
                          "SELECT name, amount FROM transactions"
                          " WHERE account_id = %s AND removed = 0", (aid,)))

    def test_a_keyed_csv_row_under_the_printed_sign_is_corrected_in_place(
            self):
        import datetime as dt
        line = "REFUND TO CARD " + NUMBER
        text = ("Date,Description,Debit,Credit\n"
                f"2026-07-11,{line},,-150.00\n")
        csvimport.import_csv(self.conn, "chk", text, CSV_MAPPING)
        new = base.FileImportIds("csv", "chk").id_for(
            dt.date(2026, 7, 11), -150.00, line)
        old = base.FileImportIds("csv", "chk")
        # the row as an import before both rules stored it: the printed
        # sign subtracted as it stood, the id hashed over the full number
        full = old.legacy_id(old.id_for(dt.date(2026, 7, 11), 150.00, line))
        self.conn.execute(
            "UPDATE transactions SET id = %s, amount = 150 WHERE id = %s",
            (full, new))
        self.conn.execute(
            "INSERT INTO transaction_notes (txn_id, note) VALUES (%s, 'mine')",
            (full,))
        self.assertEqual(base.key_file_import_ids(self.conn), 1)
        r = csvimport.import_csv(self.conn, "chk", text, CSV_MAPPING)
        self.assertEqual(self._live("chk"),
                         [("REFUND TO CARD …0123", -150.0)])
        self.assertEqual(r["corrected"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT txn_id FROM transaction_notes WHERE note = 'mine'"
        ).fetchone()["txn_id"], new)

    def test_a_keyed_qif_investment_row_signed_as_written_is_rewritten(self):
        import datetime as dt
        payee = "HARBOR ACCT " + NUMBER
        # two accounts, so the file splits by the account each names
        qif = ("!Account\nNEveryday Checking\nTBank\n^\n!Type:Bank\n"
               "D03/04/2026\nT-61.20\nPCORNER GROCER\n^\n"
               "!Account\nNHarbor Brokerage\nTInvst\n^\n!Type:Invst\n"
               f"D03/02/2026\nNBuy\nP{payee}\nYHARBOR TOTAL MARKET\n"
               "I80\nQ25\nT2,000.00\n^\n")
        aid = base.ensure_split_account(self.conn, "qif", "Harbor Brokerage",
                                        type_="investment",
                                        subtype="brokerage")
        old = base.FileImportIds("qif", aid)
        d = dt.date(2026, 3, 2)
        # signed as written and hashed over the full payee, as an import
        # before both rules gave it; its name masked since
        full = old.legacy_id(old.id_for(d, -2000.0, payee))
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name,"
            " pending, removed, raw) VALUES (%s,%s,%s,-2000,%s,0,0,"
            " jsonb_build_object('section','Invst','action','Buy'))",
            (full, aid, d, base.mask_long_digits(payee)))
        self.assertEqual(base.key_file_import_ids(self.conn), 1)
        keyed = base.keyed_file_id(_secret(self.conn).encode(), full)
        qifimport.import_qif(self.conn, "chk", qif)
        self.assertEqual(self._live(aid), [("HARBOR ACCT …0123", 2000.0)])
        self.assertEqual({r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions WHERE account_id = %s",
            (aid,)).fetchall()}, {keyed})


    def test_a_keyed_pdf_row_keeps_its_stored_sign_when_the_reading_is_unsure(
            self):
        """A statement line whose page the time budget left unsigned keeps
        the row and the sign an earlier, unhurried import stored, keyed
        since or not."""
        import datetime as dt

        from oikonome.sync import pdfimport
        name = "CARD PAYMENT " + NUMBER
        line = "03/09 " + name + " 50.00"
        d = dt.date(2026, 3, 9)
        old = base.FileImportIds("pdf", "chk")
        full = old.legacy_id(old.id_for(d, -50.0, name))
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name,"
            " pending, removed, raw) VALUES (%s,'chk',%s,-50,%s,0,0,"
            " jsonb_build_object('line', %s::text, 'flow', NULL))",
            (full, d, base.mask_long_digits(name),
             base.mask_long_digits(line)))
        self.assertEqual(base.key_file_import_ids(self.conn), 1)
        keyed = base.keyed_file_id(_secret(self.conn).encode(), full)
        parsed = {"rows": [{"date": d, "amount": 50.0, "name": name,
                            "line": line, "flow": None,
                            "sign_unknown": True}],
                  "confidence": "low", "warnings": [], "period": None}
        with mock.patch.object(pdfimport, "extract_statement",
                               return_value=("", None)), \
                mock.patch.object(pdfimport, "parse_statement",
                                  return_value=parsed):
            pdfimport.import_pdf(self.conn, "chk", b"%PDF", amount_sign="bank")
        self.assertEqual(self._live("chk"), [("CARD PAYMENT …0123", -50.0)])
        self.assertEqual({r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions WHERE account_id = 'chk'")},
            {keyed})

class KeyTableSchemaTests(unittest.TestCase):
    """A fresh install (schema.sql) and an upgraded one (the migrations)
    hold the same table, under row-level security, with no index the
    migration does not make."""

    SNAP = f"{TEST_DB}_txn_id_keys"

    @staticmethod
    def _shape(dsn):
        with psycopg.connect(dsn, autocommit=True) as c:
            cols = c.execute(
                """SELECT column_name, data_type, is_nullable,
                          column_default
                     FROM information_schema.columns
                    WHERE table_name = 'txn_id_keys'
                    ORDER BY column_name""").fetchall()
            rls = c.execute(
                "SELECT relrowsecurity FROM pg_class"
                " WHERE relname = 'txn_id_keys'").fetchone()
            pols = c.execute(
                "SELECT polname, pg_get_expr(polqual, polrelid),"
                " pg_get_expr(polwithcheck, polrelid) FROM pg_policy"
                " WHERE polrelid = 'txn_id_keys'::regclass").fetchall()
            idx = c.execute(
                "SELECT indexname FROM pg_indexes"
                " WHERE tablename = 'txn_id_keys' ORDER BY 1").fetchall()
        return cols, rls, pols, idx

    def test_a_fresh_install_and_an_upgrade_agree(self):
        make_db().close()
        with psycopg.connect(_admin_dsn("postgres"), autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {self.SNAP} WITH (FORCE)")
            c.execute(f"CREATE DATABASE {self.SNAP}")
        self.addCleanup(self._drop)
        with psycopg.connect(_admin_dsn(self.SNAP), autocommit=True) as c:
            c.execute(migrate.APP_ROLE_SQL.format(pw="apppass"))
            c.execute((migrate.HERE / "schema.sql").read_text())
        fresh = self._shape(_admin_dsn(self.SNAP))
        upgraded = self._shape(_admin_dsn(TEST_DB))
        self.assertTrue(fresh[0])
        self.assertEqual(fresh[1], (True,))
        self.assertEqual(fresh, upgraded)

    def _drop(self):
        with psycopg.connect(_admin_dsn("postgres"), autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {self.SNAP} WITH (FORCE)")


class KeyedIdFormTests(unittest.TestCase):
    def test_a_keyed_id_keeps_its_prefix_and_reads_as_keyed(self):
        secret = b"s" * 32
        old = "qif:" + "a" * 40
        new = base.keyed_file_id(secret, old)
        self.assertTrue(new.startswith("qif:k"))
        self.assertTrue(base.is_file_import_id(new))
        self.assertIsNone(UNKEYED.match(new))
        self.assertNotEqual(base.keyed_file_id(b"t" * 32, old), new)
        self.assertEqual(json.loads(json.dumps(new)), new)


class ARowHandedToOneSourceGroupIsNotHandedToAnotherTests(
        unittest.TestCase):
    """One id instance may serve several source groups of a file. A stored
    row it has already resolved one group's line to is spoken for: no
    adoption path (keyed or renamed) hands it to a later group's line,
    or the two lines would land as one row."""

    def setUp(self):
        import datetime as dt
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        self.secret = base.txn_id_secret(self.conn, create=True)
        self.line = (dt.date(2026, 3, 4), 12.0, "CORNER GROCER")

    def _store(self, tid):
        d, amount, name = self.line
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name)"
            " VALUES (%s, 'chk', %s, %s, %s)", (tid, d, amount, name))

    def _resolve(self, ids, tid):
        d, amount, name = self.line
        return ids.resolve(self.conn, [base.Transaction(
            id=tid, account_id="chk", date=d, amount=amount, name=name)])

    def test_the_keyed_path_skips_a_row_already_issued(self):
        ids = base.FileImportIds("csv", "chk")
        tid = ids.id_for(*self.line)
        keyed = base.keyed_file_id(self.secret, tid)
        self._store(keyed)
        ids._issued.add(keyed)
        self.assertNotEqual(self._resolve(ids, tid)[0].id, keyed)

    def test_the_renamed_path_skips_a_row_already_issued(self):
        former = base.FileImportIds("csv", "chk-old")
        old = former.id_for(*self.line)
        self._store(old)
        ids = base.FileImportIds("csv", "chk", former=("chk-old",))
        tid = ids.id_for(*self.line)
        ids._issued.add(old)
        self.assertNotEqual(self._resolve(ids, tid)[0].id, old)


if __name__ == "__main__":
    unittest.main()
