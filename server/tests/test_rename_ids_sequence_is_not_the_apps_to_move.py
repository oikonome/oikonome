"""The app role cannot move the merchant-rename id counter.

Every household's rename journal draws ids from one sequence. A restore
gives each journal row a fresh id, so nothing the app does sets the
counter; a role that could would let one statement push it to the end of
its range for everyone. The app keeps drawing and reading ids.
"""
import unittest

import psycopg

from .test_restore import _fresh_tenant


class RenameSequenceGrantTests(unittest.TestCase):
    def setUp(self):
        self.conn = _fresh_tenant()
        self.addCleanup(self.conn.close)

    def test_the_app_can_draw_and_read_but_not_set(self):
        self.conn.execute("INSERT INTO merchant_renames (raw_merchant,"
                          " to_canonical) VALUES ('KIOSK 1', 'Kiosk')")
        self.conn.execute("SELECT last_value FROM merchant_renames_id_seq")
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.conn.transaction():
                self.conn.execute(
                    "SELECT setval('merchant_renames_id_seq', 10)")


if __name__ == "__main__":
    unittest.main()
