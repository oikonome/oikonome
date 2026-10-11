"""A file row a script token pushed never sets aside a bank-feed row on an
account it is not linked to.

A token is barred from importing into a feed account, yet the overlap guard
matches feed rows against every file row in the household. Without the
scoping, a row the token planted on a manual account would make the feed
drop the real charge on any account, for good, since the cursor moves past
it. A signed-in import keeps its cross-account matching (that is how a
manual history meets the feed linked later), and a token row still stands in
for a feed row on an account linked to its own.
"""

import datetime as dt
import unittest
import uuid

from oikonome.sync import csvimport, heartbeat
from oikonome.sync.base import ImportOverlapGuard, Transaction

from .util import make_db, write_config

DAY = dt.date.today() - dt.timedelta(days=5)
CSV = f"Date,Description,Amount\n{DAY.isoformat()},NETFLIX,-89.99\n"
MAPPING = {"date": "Date", "amount": "Amount", "name": "Description"}


def _feed_row(account="card", day=DAY):
    return Transaction(id="plaid-x1", account_id=account, date=day,
                       amount=89.99, name="NETFLIX.COM")


class TokenPushedRowsStayInTheirLane(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _import(self, via, text=CSV):
        tok = heartbeat.PUSH_VIA.set(via)
        try:
            return csvimport.import_csv(self.conn, "chk", text, MAPPING)
        finally:
            heartbeat.PUSH_VIA.reset(tok)

    def _link(self, *accounts):
        g = uuid.uuid4()
        for a in accounts:
            self.conn.execute(
                "INSERT INTO account_links (group_id, account_id)"
                " VALUES (%s, %s)", (g, a))

    def test_token_row_does_not_cover_an_unlinked_feed_charge(self):
        self._import("token")
        self.assertIsNone(ImportOverlapGuard(self.conn).match(_feed_row()))

    def test_signed_in_row_still_covers_across_accounts(self):
        self._import("app")
        self.assertIsNotNone(ImportOverlapGuard(self.conn).match(_feed_row()))

    def test_token_row_covers_a_linked_feed_account(self):
        self._link("chk", "card")
        self._import("token")
        self.assertIsNotNone(ImportOverlapGuard(self.conn).match(_feed_row()))

    def test_token_cannot_plant_a_row_ahead_of_its_charge(self):
        later = dt.date.today() + dt.timedelta(days=10)
        self._import("token",
                     f"Date,Description,Amount\n{later.isoformat()},X,-5\n")
        n = self.conn.execute(
            "SELECT count(*) AS n FROM transactions WHERE id LIKE 'csv:%%'"
        ).fetchone()["n"]
        self.assertEqual(n, 0)

    def test_a_file_column_named_like_the_mark_neither_breaks_nor_spoofs(self):
        # a CSV header can be any string; the stored row must not carry it
        # as the mark, and a stray text value already stored must not make
        # the guard's read fail every later feed sync
        for cell in ("x", "true"):
            self.conn.execute("DELETE FROM transactions")
            self._import("app",
                         "Date,Description,Amount,_token_pushed\n"
                         f"{DAY.isoformat()},NETFLIX,-89.99,{cell}\n")
            raw = self.conn.execute(
                "SELECT raw FROM transactions WHERE id LIKE 'csv:%%'"
            ).fetchone()["raw"]
            self.assertNotIn("_token_pushed", raw)
            self.assertIsNotNone(
                ImportOverlapGuard(self.conn).match(_feed_row()))
        self.conn.execute(
            "UPDATE transactions SET raw = raw || '{\"_token_pushed\": \"x\"}'"
            " WHERE id LIKE 'csv:%%'")
        self.assertIsNotNone(ImportOverlapGuard(self.conn).match(_feed_row()))


if __name__ == "__main__":
    unittest.main()
