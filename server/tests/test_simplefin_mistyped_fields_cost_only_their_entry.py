"""A SimpleFIN reply whose text fields are not strings still syncs.

The bridge URL is the household's own, so a reply's types are whatever
that server sends. Every bank rides on one bridge token: a numeric account
name, an id given as a number, or an object where a description belongs
must cost at most that entry — never raise out of the pull for every bank
on the bridge.
"""

import json
import unittest

import httpx

from oikonome.sync import simplefin

from .util import make_db, write_config

PAYLOAD = {
    "errors": [],
    "accounts": [
        {"id": "acc-ok", "name": "Everyday Checking (1234)",
         "currency": "USD", "balance": "10.00", "org": {"name": "Demo Bank"},
         "transactions": [
             {"id": "t1", "posted": 1752537600, "amount": "-1.00",
              "description": "SAFEWAY", "payee": "Safeway"}]},
        {"id": 42, "name": 1234, "currency": 840, "balance": "5.00",
         "org": {"name": 7, "domain": ["x"]},
         "transactions": [
             {"id": 9, "posted": 1752537600, "amount": "-2.00",
              "description": {"x": 1}, "payee": 5}]},
        {"id": 43, "balance": "1.00", "transactions": []},
        {"id": True, "name": "a boolean is not an id"},
    ],
}


def _transport():
    return httpx.MockTransport(
        lambda request: httpx.Response(200, text=json.dumps(PAYLOAD)))


class MistypedFieldTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_the_pull_survives_and_keeps_every_well_formed_entry(self):
        simplefin.sync(self.conn, "sfin-demo",
                       "https://u:p@bridge.test/simplefin",
                       transport=_transport())
        names = {r["id"]: r["name"] for r in self.conn.execute(
            "SELECT id, name FROM accounts")}
        self.assertEqual(names.get("acc-ok"), "Everyday Checking (1234)")
        self.assertEqual(names.get("42"), "1234")
        self.assertEqual(names.get("43"), "43")
        self.assertNotIn("True", names)
        rows = {r["id"]: r for r in self.conn.execute(
            "SELECT id, name, merchant_name FROM transactions")}
        self.assertIn("sfin:acc-ok:t1", rows)
        self.assertEqual(rows["sfin:42:9"]["name"], "?")

    def test_well_formed_spells_numbers_as_strings_and_drops_objects(self):
        out = simplefin._well_formed(PAYLOAD)
        a = out["accounts"][1]
        self.assertEqual((a["id"], a["name"], a["currency"]),
                         ("42", "1234", "840"))
        self.assertEqual(a["org"], {"name": "7", "domain": None})
        t = a["transactions"][0]
        self.assertEqual((t["id"], t["description"], t["payee"]),
                         ("9", None, "5"))
        self.assertEqual(len(out["accounts"]), 3)


if __name__ == "__main__":
    unittest.main()
