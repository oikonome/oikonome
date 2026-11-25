"""A crypto account's cash in and cash out count money that crossed to or
from the bank, once.

A sale that settles into the account's cash wallet is written as two rows
of one trade — the coin leaving, the dollars arriving — and the dollars
then leave as a withdrawal. Counting every row read one sale as cash in
AND as cash out twice, so both columns and the return grew with every sale
while the profit, where the two legs cancel, stayed right. A trade's two
legs are one movement inside the account: only what they leave over
counts. A trade written as a single row went straight to or from the bank
and counts whole, as it always did."""

import datetime as dt
import unittest

from oikonome.engine import reporting
from oikonome.engine.compat import jsonb

from .util import make_db, write_config

BALANCE = 5000.0


def _leg(kind, native, *, coin=True, trade=None):
    raw = {"type": kind,
           "amount": {"amount": "0.01" if native > 0 else "-0.01",
                      "currency": "BTC"} if coin else
                     {"amount": f"{native:.2f}", "currency": "USD"},
           "native_amount": {"amount": f"{native:.2f}", "currency": "USD"}}
    if trade:
        raw[kind] = {"id": trade}
    return raw


# every figure invented; the SHAPES are the two a feed has written
ROWS = [
    # bought with bank money, one row (the older shape, and still a buy's)
    _leg("buy", 2000, trade="b1"),
    # sold straight to the bank, one row with no trade record
    _leg("sell", -400),
    # dollars sent in from the bank
    _leg("fiat_deposit", 300, coin=False),
    # a sale into the cash wallet: two legs of one trade, then the
    # withdrawal that carries the money to the bank
    _leg("sell", -1500, trade="s1"),
    _leg("sell", 1500, coin=False, trade="s1"),
    _leg("fiat_withdrawal", -1500, coin=False),
    # a buy paid from the cash wallet: two legs, no bank money at all
    _leg("buy", -250, coin=False, trade="b2"),
    _leg("buy", 250, trade="b2"),
    # the holder's own coin moving between venues: never a cash flow
    _leg("pro_withdrawal", 900),
]


class CashInAndOutCountBankMoneyOnce(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        x = self.conn.execute
        x("INSERT INTO items (id, aggregator, institution_name, access_token)"
          " VALUES ('cb','coinbase','Coinbase','t')")
        x("INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
          " VALUES ('wal','cb','Invented Wallet','investment','crypto',%s)",
          (BALANCE,))
        for i, raw in enumerate(ROWS):
            x("""INSERT INTO transactions (id, account_id, date, amount, name,
                                           raw, pending, removed)
                 VALUES (%s,'wal',%s,%s,'row',%s,0,0)""",
              (f"cb{i}", dt.date(2026, 3, 1 + i),
               -float(raw["native_amount"]["amount"]), jsonb(raw)))

    def _row(self):
        (row,) = reporting._coinbase_pl(self.conn)
        return row

    def test_a_sale_into_the_cash_wallet_is_cash_out_once_and_never_cash_in(self):
        row = self._row()
        # the bank-funded buy and the deposit; not the sale's dollars
        # arriving, not the wallet-funded buy
        self.assertEqual(row["cash_in"], 2300.0)
        # the direct sale and the withdrawal; not the coin leg of the sale
        # the withdrawal already carries, not the wallet paying for a buy
        self.assertEqual(row["cash_out"], 1900.0)
        self.assertEqual(row["pct"], 200.0)

    def test_the_profit_is_the_one_every_row_summed_to(self):
        """Profit is value plus everything out minus everything in over
        every fiat row; a trade's two legs cancel in that sum, so leaving
        them out of the columns must not move it."""
        every_row = sum(float(r["native_amount"]["amount"]) for r in ROWS
                        if r["type"] != "pro_withdrawal")
        row = self._row()
        self.assertEqual(row["pl"], round(BALANCE - every_row, 2))
        self.assertEqual(row["pl"], 4600.0)
        self.assertEqual(row["pl"],
                         round(row["value"] + row["cash_out"]
                               - row["cash_in"], 2))

    def test_a_fee_between_a_trades_legs_is_never_cash_out(self):
        """A trade whose dollar leg is short of its coin leg (a fee taken
        between them) moved no money to the bank: the columns stay bank
        money only, and the fee is gone from the value, not added to it."""
        self.conn.execute(
            "UPDATE transactions SET raw = %s WHERE id = 'cb4'",
            (jsonb(_leg("sell", 1490, coin=False, trade="s1")),))
        row = self._row()
        self.assertEqual(row["cash_in"], 2300.0)
        self.assertEqual(row["cash_out"], 1900.0)
        self.assertEqual(row["pl"], 4600.0)

    def test_a_fee_lowers_the_profit_by_the_fee(self):
        """Dollars in from the bank, a wallet-funded buy that loses a fee
        between its legs, the coin still worth what it cost after the fee:
        the profit is minus the fee."""
        x = self.conn.execute
        x("UPDATE accounts SET balance_current = 990 WHERE id = 'wal'")
        x("DELETE FROM transactions WHERE account_id = 'wal'")
        for i, raw in enumerate((
                _leg("fiat_deposit", 1000, coin=False),
                _leg("buy", -1000, coin=False, trade="b9"),
                _leg("buy", 990, trade="b9"))):
            x("""INSERT INTO transactions (id, account_id, date, amount,
                                           name, raw, pending, removed)
                 VALUES (%s,'wal',%s,%s,'row',%s,0,0)""",
              (f"fee{i}", dt.date(2026, 4, 1 + i),
               -float(raw["native_amount"]["amount"]), jsonb(raw)))
        row = self._row()
        self.assertEqual((row["cash_in"], row["cash_out"], row["pl"]),
                         (1000.0, 0.0, -10.0))


class AnUnreadableNativeAmountIsSkipped(unittest.TestCase):
    """A stored crypto row whose native amount is not a plain decimal (a
    pushed or restored raw keeps whatever it was sent) is left out of the
    figures; the page that reads them never fails on it."""

    BAD = ({"amount": "1e400", "currency": "USD"},
           {"amount": "Infinity", "currency": "USD"},
           {"amount": "NaN", "currency": "USD"},
           "400")

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        x = self.conn.execute
        x("INSERT INTO items (id, aggregator, institution_name, access_token)"
          " VALUES ('cb','coinbase','Coinbase','t')")
        x("INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
          " VALUES ('wal','cb','Invented Wallet','investment','crypto',700)")
        today = reporting._household_today(self.conn)
        rows = [{"type": "fiat_deposit",
                 "native_amount": {"amount": "500.00", "currency": "USD"}}]
        for bad in self.BAD:
            rows += [{"type": "fiat_withdrawal", "native_amount": bad},
                     {"type": "fiat_deposit", "native_amount": bad}]
        for i, raw in enumerate(rows):
            x("""INSERT INTO transactions (id, account_id, date, amount, name,
                                           raw, pending, removed)
                 VALUES (%s,'wal',%s,%s,'row',%s,0,0)""",
              (f"bad{i}", today, -500.0 if i == 0 else 1.0, jsonb(raw)))
        self.today = today

    def test_the_cash_flows_skip_it(self):
        (row,) = reporting._coinbase_pl(self.conn)
        self.assertEqual((row["cash_in"], row["cash_out"], row["pl"]),
                         (500.0, 0.0, 200.0))

    def test_the_unarrived_withdrawal_line_skips_it(self):
        self.assertIsNone(
            reporting._unarrived_withdrawals(self.conn, self.today))


if __name__ == "__main__":
    unittest.main()
