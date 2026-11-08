"""A re-key holds the account it moves before it reads the rows it
re-hashes, so no row can land on the old id in between.

The rows are read and re-hashed under the new id first, and only then is
the account moved. A writer that does not take the household's sync lock
(a hand-entered row, a door outside the importers) could add a row to the
old account in that window; the move would carry it across with an id
hashed over the old number, past the re-hash that should have proved it.
Taking the account's row lock first makes such a writer wait for the
move, then find the account gone.
"""
import os
import unittest
from unittest import mock

import psycopg

from oikonome.db import tenancy
from oikonome.sync import base, csvimport

from .test_reimport_into_a_rekeyed_numbered_account_adds_nothing import (
    CSV, CSV_MAP, OLD, _seed)
from .util import TEST_DB, make_db, write_config


class AccountHeldFirst(unittest.TestCase):
    def test_a_row_cannot_land_on_the_old_id_while_it_is_rehashed(self):
        conn = make_db()
        write_config(conn)
        self.addCleanup(conn.close)
        _seed(conn, OLD, "Everyday Checking …1234")
        csvimport.import_csv(conn, OLD, CSV, CSV_MAP)
        tid = conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        dsn = os.environ.get(
            "OIKONOME_TEST_DSN",
            f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}")
        other = tenancy.tenant_connect(tid, dsn)
        self.addCleanup(other.close)
        seen = {}
        real = base.rehash_file_rows

        def racing(c, old, new, **kw):
            try:
                with other.transaction():
                    other.execute("SET LOCAL lock_timeout = '300ms'")
                    other.execute(
                        "INSERT INTO transactions (id, account_id, date,"
                        " amount, name) VALUES ('hand-1', %s, '2026-03-20',"
                        " -1, 'BY HAND')", (old,))
                seen["landed"] = True
            except psycopg.errors.LockNotAvailable:
                seen["landed"] = False
            return real(c, old, new, **kw)

        with mock.patch.object(base, "rehash_file_rows", racing):
            moved = base.rekey_numbered_accounts(conn)
        self.assertEqual(list(moved), [OLD])
        self.assertIs(seen.get("landed"), False)


if __name__ == "__main__":
    unittest.main()
