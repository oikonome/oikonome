"""Paged search results must split tied rows the same way on every page.

The search serves page N as a slice of the full ordered id list, recomputed
on every request. Rows tied on date and amount (two same-day coffees, a
transfer's two legs) have no defined order without a unique final key, so
two requests can split the tie differently: one row shows on both pages
and another on neither. The id breaks every tie.
"""

import datetime as dt
import unittest

from oikonome.web import data

from .util import add_txn, make_db, write_config

DAY = dt.date(2026, 9, 14)


class SearchPagesHaveAStableOrder(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)

    def test_tied_rows_page_in_id_order_with_none_lost_or_repeated(self):
        ids = [f"tie-{n}" for n in range(5)]
        for tid in reversed(ids):        # stored opposite to id order
            add_txn(self.conn, DAY, 4.50, "COFFEE", account="chk",
                    txn_id=tid)
        seen = []
        for page in (1, 2, 3):
            rows = data.search_transactions(self.conn, "", page=page,
                                            per_page=2)[0]
            seen += [r["id"] for r in rows]
        self.assertEqual(seen, ids)


if __name__ == "__main__":
    unittest.main()
