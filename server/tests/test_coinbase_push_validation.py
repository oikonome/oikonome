"""The Coinbase push door validates money before it writes, and writes
the whole push or none of it.

Two invariants, one failure mode. The payload is JSON from a host-side
script, so `amount` can arrive as null, an empty string, or a word; the
door used to hand it straight to a NUMERIC NOT NULL column, and the
database's complaint is a psycopg error rather than the ValueError the
route translates into "bad payload" — so a single bad row 500'd. Worse,
the connection is autocommit: every row before the bad one had already
committed, leaving the account holding half a restatement while the
caller was told the push failed.
"""

import datetime as dt
import unittest
from unittest import mock

import psycopg

from oikonome.sync import base, coinbase_push

from .util import make_db, write_config

TODAY = dt.date.today()


def _payload(**over) -> dict:
    """A legit collector payload (mirrors community-scripts/coinbase)."""
    p = {
        "items": [{"id": "coinbase-test", "institution_name": "Coinbase"}],
        "accounts": [{"id": "coinbase-test", "item_id": "coinbase-test",
                      "name": "Coinbase (test)", "type": "investment",
                      "subtype": "crypto", "balance_current": 1234.56,
                      "balance_available": None}],
        "transactions": [
            {"id": "coinbase:t-one", "account_id": "coinbase-test",
             "date": TODAY.isoformat(), "amount": 100.0, "name": "BUY BTC"},
            {"id": "coinbase:t-two", "account_id": "coinbase-test",
             "date": TODAY.isoformat(), "amount": -25.0, "name": "SELL ETH"},
        ],
    }
    p.update(over)
    return p


class CoinbasePushMoneyValidationTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _rows(self) -> set[str]:
        return {r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions WHERE id LIKE 'coinbase:%'"
        ).fetchall()}

    def _accounts(self) -> set[str]:
        return {r["id"] for r in self.conn.execute(
            "SELECT id FROM accounts WHERE id LIKE 'coinbase-%'").fetchall()}

    def test_an_unusable_amount_is_a_bad_payload_not_a_database_error(self):
        """ValueError is what the route turns into a 400; a psycopg error
        escapes it as a 500."""
        for bad in (None, "", "n/a", [], {}, True, float("nan"),
                    float("inf")):
            p = _payload(transactions=[
                {"id": "coinbase:t-bad", "account_id": "coinbase-test",
                 "date": TODAY.isoformat(), "amount": bad, "name": "BUY BTC"}])
            with self.assertRaises(ValueError, msg=repr(bad)) as caught:
                coinbase_push.import_payload(self.conn, p)
            # the message names the field, so the 400 is actionable
            self.assertIn("amount", str(caught.exception))
            self.assertEqual(self._rows(), set(), repr(bad))

    def test_an_unusable_balance_is_refused_the_same_way(self):
        acct = dict(_payload()["accounts"][0], balance_current="lots")
        with self.assertRaises(ValueError) as caught:
            coinbase_push.import_payload(self.conn, _payload(accounts=[acct]))
        self.assertIn("balance_current", str(caught.exception))
        self.assertEqual(self._accounts(), set())

    def test_a_missing_balance_stays_missing(self):
        """The collector sends an explicit null for balances Coinbase does
        not report — absent is not an error."""
        acct = dict(_payload()["accounts"][0],
                    balance_current=None, balance_available=None)
        coinbase_push.import_payload(self.conn, _payload(accounts=[acct]))
        row = self.conn.execute(
            "SELECT balance_current, balance_available FROM accounts "
            "WHERE id='coinbase-test'").fetchone()
        self.assertIsNone(row["balance_current"])
        self.assertIsNone(row["balance_available"])

    def test_numbers_that_arrive_as_strings_still_work(self):
        """Coinbase's own API states money as strings, so a collector
        forwarding them verbatim must not be broken by the check."""
        p = _payload(transactions=[
            {"id": "coinbase:t-str", "account_id": "coinbase-test",
             "date": TODAY.isoformat(), "amount": "1,250.75",
             "name": "BUY BTC"}])
        p["accounts"][0]["balance_current"] = "99.50"
        coinbase_push.import_payload(self.conn, p)
        row = self.conn.execute(
            "SELECT amount FROM transactions WHERE id='coinbase:t-str'"
        ).fetchone()
        self.assertEqual(float(row["amount"]), 1250.75)

    def test_a_bad_row_late_in_the_batch_writes_nothing_at_all(self):
        """The rows before it must not be left committed — the check runs
        over the whole payload before the first write."""
        good = [{"id": f"coinbase:t-{i}", "account_id": "coinbase-test",
                 "date": TODAY.isoformat(), "amount": float(i),
                 "name": "BUY BTC"} for i in range(20)]
        good.append({"id": "coinbase:t-bad", "account_id": "coinbase-test",
                     "date": TODAY.isoformat(), "amount": None,
                     "name": "BUY BTC"})
        with self.assertRaises(ValueError):
            coinbase_push.import_payload(self.conn, _payload(transactions=good))
        self.assertEqual(self._rows(), set())
        self.assertEqual(self._accounts(), set())

    def test_a_failure_mid_write_leaves_no_half_applied_push(self):
        """Whatever raises — a constraint, a dropped connection — the push
        is one transaction: the caller's error and the database agree that
        nothing was applied."""
        real = base.upsert_transactions

        def half_then_fail(conn, txns):
            real(conn, txns[:1])
            raise psycopg.errors.NotNullViolation("amount")

        with mock.patch("oikonome.sync.base.upsert_transactions",
                        half_then_fail):
            with self.assertRaises(psycopg.Error):
                coinbase_push.import_payload(self.conn, _payload())
        self.assertEqual(self._rows(), set())
        self.assertEqual(self._accounts(), set())
        self.assertEqual([r["id"] for r in self.conn.execute(
            "SELECT id FROM items WHERE id='coinbase-test'").fetchall()], [])

    def test_a_good_push_still_lands(self):
        out = coinbase_push.import_payload(self.conn, _payload())
        self.assertEqual(out["pushed"], 2)
        self.assertEqual(self._rows(), {"coinbase:t-one", "coinbase:t-two"})


if __name__ == "__main__":
    unittest.main()
