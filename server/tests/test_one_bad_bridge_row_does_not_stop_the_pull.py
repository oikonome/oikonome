"""One malformed row or balance from a SimpleFIN bridge is one skipped row.

A bridge pull carries every bank under the token. A row with no id, no
amount or an amount that is not a number, or a balance that is not a
number, used to raise out of the pull: the bridge read as failed for every
bank, and since the bad row stays in the next window, it failed the same
way every hour. A balance of NaN or infinity parsed cleanly and was stored,
and then every reply carrying that account failed to serialise.
"""
import copy
import math
import unittest

from oikonome.sync import base, simplefin

from .test_simplefin import PAYLOAD, _transport
from .util import make_db, write_config


class BadBridgeRowTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _sync(self, payload):
        return simplefin.sync(self.conn, "sfin-demo",
                              "https://u:p@bridge.test/simplefin",
                              transport=_transport(payload))

    def _ids(self):
        return {r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions WHERE id LIKE 'sfin:%'").fetchall()}

    def _balance(self, aid):
        return self.conn.execute(
            "SELECT balance_current FROM accounts WHERE id=%s",
            (aid,)).fetchone()["balance_current"]

    def test_a_row_with_a_bad_amount_is_skipped_and_the_rest_land(self):
        p = copy.deepcopy(PAYLOAD)
        rows = p["accounts"][0]["transactions"]
        rows.append({"id": "bad1", "posted": 1752537600, "amount": "1,234.50",
                     "description": "X"})
        rows.append({"id": "bad2", "posted": 1752537600, "description": "Y"})
        rows.append({"posted": 1752537600, "amount": "-5.00",
                     "description": "NO ID"})
        rows.append("not a row")
        self._sync(p)
        self.assertEqual(self._ids(), {"sfin:acc-chk:t1", "sfin:acc-chk:t2",
                                       "sfin:acc-card:t3"})

    def test_a_bad_balance_is_unknown_not_a_failed_pull(self):
        p = copy.deepcopy(PAYLOAD)
        p["accounts"][0]["balance"] = "n/a"
        p["accounts"][0]["available-balance"] = "NaN"
        p["accounts"][1]["balance"] = "1e999"
        self._sync(p)
        self.assertIsNone(self._balance("acc-chk"))
        self.assertIsNone(self._balance("acc-card"))
        self.assertIn("sfin:acc-card:t3", self._ids())

    def test_an_account_with_no_id_is_skipped(self):
        p = copy.deepcopy(PAYLOAD)
        p["accounts"].append({"name": "Mystery", "balance": "1.00",
                              "transactions": []})
        p["accounts"].append("not an account")
        self._sync(p)
        self.assertIn("sfin:acc-chk:t1", self._ids())

    def test_no_aggregator_can_store_a_non_finite_balance(self):
        base.upsert_accounts(self.conn, "it1", [base.Account(
            id="chk", name="Checking", type="depository", subtype="checking",
            balance_current=math.inf, balance_available=float("nan"))])
        row = self.conn.execute(
            "SELECT balance_current, balance_available FROM accounts"
            " WHERE id='chk'").fetchone()
        self.assertEqual((row["balance_current"], row["balance_available"]),
                         (None, None))


if __name__ == "__main__":
    unittest.main()
