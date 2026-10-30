"""The ledger's month header says how much income is still expected this
month, which walks the household's income schedule. The download of the
same month carries rows only — no header — so it must not pay for that
walk: an export is the largest read the ledger serves, and a figure it
throws away is pure cost on every one.
"""

import unittest
from unittest import mock

from oikonome.engine import budget
from oikonome.web import api

from .util import make_db, write_config


class LedgerExportSkipsExpectedIncome(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _payload(self, **kw):
        calls = []

        def counted(*a, **k):
            calls.append(1)
            return 0.0
        with mock.patch.object(budget, "expected_income_remaining", counted):
            out = api._transactions_payload(self.conn, **kw)
        return out, len(calls)

    def test_the_page_reads_the_current_months_expected_income(self):
        out, n = self._payload()
        self.assertEqual(out["mode"], "month")
        self.assertEqual(n, 1)
        self.assertEqual(out["in_expected"], 0.0)

    def test_the_download_does_not_walk_the_income_schedule(self):
        out, n = self._payload(chunk=api._EXPORT_CHUNK)
        self.assertEqual(out["mode"], "month")
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
