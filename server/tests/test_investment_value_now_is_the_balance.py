"""An investment table's "value now" is the balance the totals count.

The P/L tables summed holdings, which the feed refreshes on its own clock:
a crypto account whose holdings were last written months ago read a value
far from the balance Net Worth counts, and its profit with it. Value now is
the account's balance; profit and return are measured against it. Top
holdings lists positions at most two days old, each with the day it was
priced, of accounts whose positions are not well above the balance, and
says per account why the rest are left out."""

import datetime as dt
import json
import unittest

from oikonome.engine import reporting
from oikonome.web import data
from unittest import mock

from .util import make_db, write_config

TODAY = dt.date(2026, 11, 17)


class StaleHoldings(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        x = self.conn.execute
        x("INSERT INTO items (id, aggregator, institution_name, access_token)"
          " VALUES ('brk','plaid','Contoso Brokerage','t'),"
          "        ('cb','coinbase','Coinbase','t')")
        x("INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
          " VALUES ('ira','brk','Invented IRA','investment','ira',1000),"
          "        ('tax','brk','Invented Taxable','investment','brokerage',500),"
          "        ('wal','cb','Invented Wallet','investment','crypto',800)")
        old = dt.datetime(2026, 11, 7, 12, tzinfo=dt.timezone.utc)
        new = dt.datetime(2026, 11, 16, 12, tzinfo=dt.timezone.utc)
        for acct, sym, value, basis, when in (
                ("ira", "INVA", 1200.0, 900.0, old),
                ("tax", "INVB", 300.0, 200.0, new),
                ("tax", "INVC", 150.0, 100.0, new)):
            x("""INSERT INTO holdings (account_id, symbol, name, quantity,
                                       price, value, as_of, raw)
                 VALUES (%s,%s,%s,1,%s,%s,%s,%s)""",
              (acct, sym, f"{sym} fund", value, value, when,
               json.dumps({"cost_basis": basis})))
        x("""INSERT INTO crypto_holdings (account_id, currency, quantity,
                                          native_usd, as_of)
             VALUES ('wal','BTC',1,1100,%s)""", (old,))
        x("""INSERT INTO transactions (id, account_id, date, amount, name,
                                       raw, pending, removed)
             VALUES ('cb1','wal','2026-01-05',-600,'Buy',%s,0,0)""",
          (json.dumps({"type": "buy",
                       "native_amount": {"amount": "600"}}),))

        p = mock.patch.object(reporting, "_household_today",
                              return_value=TODAY)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.conn.close()

    def test_brokerage_value_now_is_the_balance(self):
        rows = {r["name"]: r for r in reporting._brokerage_pl(self.conn)}
        ira = rows["Invented IRA"]
        self.assertEqual(ira["value"], 1000.0)      # not the stale 1,200
        # the gain is the holdings' own (1,200 against 900), dated; the
        # 200 the balance sits below them counts as money out
        self.assertEqual(ira["pl"], 300.0)
        self.assertEqual(ira["basis"], 700.0)
        self.assertEqual(ira["pl_as_of"], "2026-11-07")
        self.assertEqual(rows["Invented Taxable"]["value"], 500.0)

    def test_coinbase_value_now_is_the_balance(self):
        rows = {r["name"]: r for r in reporting._coinbase_pl(self.conn)}
        w = rows["Invented Wallet"]
        self.assertEqual(w["value"], 800.0)         # not the stale 1,100
        self.assertEqual(w["pl"], 200.0)

    def _rows(self):
        return [dict(r) for r in self.conn.execute(
            """SELECT a.id AS account_id, a.name AS acct,
                      a.balance_current AS bal, h.symbol, h.name AS fund,
                      h.quantity AS qty, h.price, h.value, h.as_of
                 FROM holdings h JOIN accounts a ON a.id = h.account_id""")]

    def test_top_holdings_lists_fresh_positions_and_says_why_others_are_not(self):
        out = data.fresh_holdings(self._rows(), TODAY)
        self.assertEqual([h["symbol"] for h in out["holdings"]],
                         ["INVB", "INVC"])
        self.assertEqual(out["as_of_text"], "holdings as of 11/16/26")
        self.assertEqual(out["hidden_text"],
                         "Holdings not shown: Invented IRA (last priced "
                         "11/07) — priced more than 2 days ago")

    def test_positions_priced_a_little_above_the_balance_are_listed_dated(self):
        # yesterday's prices on a day the market fell: 505 of positions
        # against a 500 balance is 1% over, and is what the account holds
        self.conn.execute(
            "UPDATE holdings SET value = 355 WHERE symbol = 'INVB'")
        out = data.fresh_holdings(self._rows(), TODAY)
        self.assertEqual([h["symbol"] for h in out["holdings"]],
                         ["INVB", "INVC"])
        self.assertEqual({h["priced_text"] for h in out["holdings"]},
                         {"priced 11/16"})
        self.assertNotIn("Invented Taxable", out["hidden_text"])

    def test_positions_well_above_the_balance_are_left_out_with_that_reason(self):
        self.conn.execute(
            "UPDATE holdings SET value = 380 WHERE symbol = 'INVB'")
        out = data.fresh_holdings(self._rows(), TODAY)
        self.assertEqual(out["holdings"], [])
        self.assertEqual(
            out["hidden_text"],
            "Holdings not shown: Invented IRA (last priced 11/07) — priced "
            "more than 2 days ago; Invented Taxable (6% over) — they add up "
            "to more than the account's balance")

    def test_one_reason_is_said_once_for_every_account_it_holds_back(self):
        # five accounts over their balance read as five copies of one
        # sentence; the reason is said once, the accounts listed before it
        self.conn.execute(
            "UPDATE holdings SET value = 380 WHERE symbol = 'INVB'")
        rows = self._rows()
        twin = [dict(r, account_id="t2", acct="Invented Taxable Two")
                for r in rows if r["acct"] == "Invented Taxable"]
        out = data.fresh_holdings(rows + twin, TODAY)
        self.assertEqual(out["hidden_text"].count("more than the account's"),
                         1)
        self.assertIn("Invented Taxable (6% over), Invented Taxable Two "
                      "(6% over)", out["hidden_text"])

    def test_a_quantity_is_printed_one_way_and_an_unknown_one_not_at_all(self):
        self.conn.execute(
            "UPDATE holdings SET quantity = 1234.5 WHERE symbol = 'INVB'")
        self.conn.execute(
            "UPDATE holdings SET quantity = NULL WHERE symbol = 'INVC'")
        got = {h["symbol"]: h["qty_text"]
               for h in data.fresh_holdings(self._rows(), TODAY)["holdings"]}
        self.assertEqual(got, {"INVB": "1,234.500", "INVC": None})


    def test_an_unnamed_account_with_stale_positions_does_not_break_the_list(self):
        old = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
        rows = [{"account_id": "a1", "acct": None, "bal": 100, "symbol": "X",
                 "fund": "X fund", "qty": 1, "price": 10, "value": 10,
                 "as_of": old},
                {"account_id": "a2", "acct": "Invented Brokerage", "bal": 100,
                 "symbol": "Y", "fund": "Y fund", "qty": 1, "price": 10,
                 "value": 10, "as_of": old}]
        out = data.fresh_holdings(rows, TODAY)
        self.assertEqual(out["holdings"], [])
        self.assertIn("an account", out["hidden_text"])
        self.assertIn("Invented Brokerage", out["hidden_text"])


class TopHoldingsCard(unittest.TestCase):
    """The card prints what the server decided, and with nothing listed it
    is the reason alone."""

    def _read(self, rel):
        import pathlib
        return (pathlib.Path(__file__).resolve().parents[2] / rel).read_text()

    def test_no_table_is_drawn_over_no_rows(self):
        web = self._read("webapp/src/pages/Accounts.tsx")
        self.assertIn('holds.data!.holdings.length > 0 && (\n'
                      '          <table className="holds-table">', web)

    def test_both_clients_print_the_servers_quantity_and_pricing_day(self):
        web = self._read("webapp/src/pages/Accounts.tsx")
        mob = self._read("mobile/src/app/(tabs)/accounts.tsx")
        for src in (web, mob):
            self.assertIn("h.qty_text", src)
            self.assertIn("h.priced_text", src)
        self.assertNotIn("h.qty ?? 0", web)
        self.assertNotIn("h.qty.toFixed", mob)


if __name__ == "__main__":
    unittest.main()
