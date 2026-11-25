"""An older phone build reads /api/retirement's old fields with their old
meaning.

With no spend saved and too little ledger to read one from, the page now
asks for a spend instead of projecting an assumed one: spend, rows and
stress come back empty. A phone built before that reply existed reads
those fields unguarded — the worst stress spend became the minimum of an
empty list (infinity) and it printed "+$∞ vs target — survives the worst
stress scenario". Only a client that says it can ask for a spend
(`ask_spend=1`) gets the spend-less reply; any other caller gets the
projection the older server sent, so every old field keeps its meaning.

The reads below replay the released phone screen's arithmetic."""

import math
import unittest

from .test_api_retirement import _make_client, _signup
from oikonome.db import tenancy

from .util import seed_accounts, write_config


def older_phone_headline(r: dict) -> dict:
    """The released phone screen's hero figures, as it computed them."""
    stress = [x["max_spend_at_ref"] for x in r["stress"]
              if x["label"] != "Average markets"]
    worst = min(stress) if stress else math.inf
    spend = r["spend"]
    return {"worst": worst,
            "margin": (worst - spend) if spend is not None else None,
            "last_age": r["rows"][-1]["age"] if r["rows"] else None,
            "ref_age": r["stress_ref_age"]}


class OlderPhoneReadsTheOldFields(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = _make_client()
        cls.tid = _signup(cls.client, "rop")
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()
        cls.client.post("/api/settings", json={"birthdate": "1970-01-01"})

    def test_a_caller_that_cannot_ask_for_a_spend_gets_a_projection(self):
        r = self.client.get("/api/retirement")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["inputs"]["spend_source"], "needed")
        h = older_phone_headline(body)
        self.assertTrue(math.isfinite(h["worst"]))
        self.assertIsNotNone(h["margin"])
        self.assertIsNotNone(h["last_age"])
        self.assertIsNotNone(h["ref_age"])
        self.assertIsNotNone(body["spend_now"])

    def test_a_current_client_gets_the_spend_less_reply(self):
        body = self.client.get("/api/retirement?ask_spend=1").json()
        self.assertEqual(body["inputs"]["spend_source"], "needed")
        self.assertIsNone(body["spend"])
        self.assertEqual(body["rows"], [])
        self.assertEqual(body["stress"], [])


if __name__ == "__main__":
    unittest.main()
