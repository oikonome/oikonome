"""Migrations refuse a PostgreSQL server older than 16, before changing
anything, with a message that names the version.

The schema, the migrations and runtime queries call functions PostgreSQL
16 added. On an older external server a migration that uses one fails when
it is parsed, after every earlier migration has committed, with a bare
"function does not exist" — and the app never starts. The check runs first
so an operator on a managed 15 learns what to change, with nothing applied.
"""

import contextlib
import io
import unittest
from unittest import mock

from oikonome.db import migrate, tenancy

from .util import _ensure_db


class _Conn:
    """A connection that reports a given server version and records every
    other statement, so the test can prove nothing else ran."""

    def __init__(self, num, shown):
        self.num, self.shown, self.ran, self.closed = num, shown, [], False
        self.info = mock.Mock(host="db.example.invalid")

    def execute(self, sql, *a):
        row = None
        if "server_version_num" in sql:
            row = {"v": self.num}
        elif "server_version" in sql:
            row = {"v": self.shown}
        else:
            self.ran.append(sql)
        return mock.Mock(fetchone=lambda: row)

    def close(self):
        self.closed = True


class MinimumServerVersionTests(unittest.TestCase):

    def test_an_older_server_stops_the_run_before_any_statement(self):
        conn = _Conn(150004, "15.4")
        err = io.StringIO()
        with mock.patch.object(migrate, "admin_connect", return_value=conn), \
                contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit):
            migrate.run()
        self.assertEqual(conn.ran, [], "nothing may run on a too-old server")
        self.assertTrue(conn.closed)
        self.assertIn("PostgreSQL 16 or newer", err.getvalue())
        self.assertIn("15.4", err.getvalue())

    def test_the_test_server_passes_the_check(self):
        _ensure_db()
        conn = tenancy.admin_connect()
        try:
            migrate._require_server_version(conn)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
