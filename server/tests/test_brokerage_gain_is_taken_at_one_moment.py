"""A brokerage account's gain compares value and money in at ONE moment.

Value now is the balance, but the cost basis comes from the holdings,
which the feed refreshes on its own clock. Subtracting an old basis from
today's balance counted every deposit made since the snapshot (and any
uninvested cash the feed does not list as a holding) as profit. The gain
is the holdings' own value against their basis; what the balance holds
beyond them counts as money in, and the table says so when the holdings
are old."""

import datetime as dt
import json
import unittest
from unittest import mock

from oikonome.engine import reporting

from .util import make_db, write_config

TODAY = dt.date(2026, 11, 17)


class GainAtOneMoment(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        x = self.conn.execute
        x("INSERT INTO items (id, aggregator, institution_name, access_token)"
          " VALUES ('brk','plaid','Contoso Brokerage','t')")
        # snapshot: 100k of positions bought for 80k; since then 30k was
        # deposited and the market added 5k, so the balance reads 135k
        x("INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
          " VALUES ('old','brk','Invented Taxable','investment','brokerage',"
          "         135000),"
          "        ('new','brk','Invented Joint','investment','brokerage',"
          "         12000)")
        stale = dt.datetime(2026, 8, 1, 12, tzinfo=dt.timezone.utc)
        fresh = dt.datetime(2026, 11, 16, 12, tzinfo=dt.timezone.utc)
        for acct, sym, value, basis, when in (
                ("old", "INVA", 100000.0, 80000.0, stale),
                # fresh positions of 10k (basis 8k) plus 2k of cash the feed
                # does not list as a holding
                ("new", "INVB", 10000.0, 8000.0, fresh)):
            x("""INSERT INTO holdings (account_id, symbol, name, quantity,
                                       price, value, as_of, raw)
                 VALUES (%s,%s,%s,1,%s,%s,%s,%s)""",
              (acct, sym, f"{sym} fund", value, value, when,
               json.dumps({"cost_basis": basis})))
        p = mock.patch.object(reporting, "_household_today",
                              return_value=TODAY)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.conn.close()

    def rows(self):
        return {r["name"]: r for r in reporting._brokerage_pl(self.conn)}

    def test_a_deposit_after_the_snapshot_is_not_profit(self):
        r = self.rows()["Invented Taxable"]
        self.assertEqual(r["value"], 135000.0)       # value now = balance
        self.assertEqual(r["pl"], 20000.0)           # not 135k - 80k = 55k
        self.assertEqual(r["basis"], 115000.0)
        self.assertEqual(r["pl_as_of"], "2026-08-01")

    def test_uninvested_cash_is_money_in_not_gain(self):
        r = self.rows()["Invented Joint"]
        self.assertEqual(r["value"], 12000.0)
        self.assertEqual(r["pl"], 2000.0)            # not 12k - 8k = 4k
        self.assertIsNone(r["pl_as_of"])             # fresh: no caveat

    def test_the_combined_table_says_which_gain_is_dated(self):
        rc = reporting._retirement_combined(self.conn)
        self.assertEqual(rc["total"]["pl"], 22000.0)
        self.assertEqual(
            rc["pl_note"],
            "Invented Taxable: gain counted to its holdings of 08/01/26; "
            "what the balance moved since counts as money in.")


if __name__ == "__main__":
    unittest.main()
