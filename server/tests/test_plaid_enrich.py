"""Plaid Enrich for imported history: only rows Plaid never described are
sent, newest first, inside a monthly cap; what comes back is merged into
the row's raw record in Plaid's own shape so the resolver (merchant row,
logo) and the enrichment columns pick it up; a generic import category
takes Plaid's; usage is recorded; a Plaid error is reported, not raised."""

import datetime as dt
import json
import unittest

import httpx

from oikonome.engine import budget
from oikonome.sync import plaid

from .util import add_txn, make_db, write_config

TODAY = dt.date.today()


def _transport(seen: list, fail=False):
    def handler(request):
        if request.url.path == "/transactions/enrich":
            body = json.loads(request.content)
            seen.append(body)
            if fail:
                return httpx.Response(400, text=json.dumps(
                    {"error_code": "PRODUCTS_NOT_SUPPORTED", "error_type": "INVALID_REQUEST",
                     "error_message": "no enrich"}))
            out = []
            for t in body["transactions"]:
                if "COSTCO" in t["description"].upper():
                    out.append({"id": t["id"], "enrichments": {
                        "merchant_name": "Costco", "website": "costco.com",
                        "logo_url": "https://plaid-merchant-logos.plaid.com/costco_235.png",
                        "entity_id": "ENT-COSTCO", "payment_channel": "in store",
                        "counterparties": [{"name": "Costco", "type": "merchant",
                                            "entity_id": "ENT-COSTCO",
                                            "confidence_level": "VERY_HIGH"}],
                        "location": {"city": "Springfield", "region": "IL"},
                        "personal_finance_category": {"primary": "GENERAL_MERCHANDISE",
                                                      "detailed": "GENERAL_MERCHANDISE_SUPERSTORES",
                                                      "confidence_level": "VERY_HIGH"}}})
                else:
                    out.append({"id": t["id"], "enrichments": {}})
            return httpx.Response(200, text=json.dumps({"enriched_transactions": out}))
        return httpx.Response(404, text="{}")
    return httpx.MockTransport(handler)


class EnrichTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")
        self.a = add_txn(self.conn, TODAY - dt.timedelta(days=1), 88.13,
                         "COSTCO WHSE #0007", account="chk", primary="GENERAL_MERCHANDISE")
        self.conn.execute("UPDATE transactions SET category_source='import' WHERE id=%s",
                          (self.a,))
        self.b = add_txn(self.conn, TODAY - dt.timedelta(days=2), 12.0,
                         "MYSTERY SHOP", account="chk")

    def tearDown(self):
        self.conn.close()

    def test_candidates_are_the_undescribed_rows_newest_first(self):
        ids = [r["id"] for r in plaid.enrich_candidates(self.conn)]
        self.assertEqual(ids, [self.a, self.b])

    def test_enrich_merges_plaid_shape_and_resolves_the_merchant(self):
        seen = []
        st = plaid.enrich_rows(self.conn, limit=100, transport=_transport(seen))
        self.assertEqual((st["sent"], st["enriched"], st["error"]), (2, 2, None))
        self.assertEqual(seen[0]["account_type"], "depository")
        self.assertEqual(seen[0]["transactions"][0]["direction"], "OUTFLOW")
        r = self.conn.execute(
            """SELECT t.raw, t.category_plaid, t.category_source, t.location_city,
                      t.payment_channel, m.name AS merchant, m.logo_url, m.plaid_entity_id
                 FROM transactions t LEFT JOIN merchants m ON m.id = t.merchant_id
                WHERE t.id=%s""", (self.a,)).fetchone()
        self.assertEqual(r["raw"]["merchant_entity_id"], "ENT-COSTCO")
        self.assertIsNotNone(r["raw"]["_enriched_at"])
        self.assertEqual((r["category_plaid"], r["category_source"]),
                         ("GENERAL_MERCHANDISE", "plaid"))
        self.assertEqual((r["location_city"], r["payment_channel"]), ("Springfield", "in store"))
        self.assertEqual((r["merchant"], r["plaid_entity_id"]), ("Costco", "ENT-COSTCO"))
        self.assertIn("costco_235.png", r["logo_url"])
        # nothing came back for the other row, but it is marked so it is
        # not sent (and billed) again
        self.assertEqual(plaid.enrich_candidates(self.conn), [])
        self.assertEqual(plaid.enrich_usage(self.conn)["used"], 2)

    def test_cap_stops_the_send(self):
        with budget.config_txn(self.conn) as cfg:
            cfg["plaid_enrich_cap"] = 1
        seen = []
        st = plaid.enrich_rows(self.conn, limit=100, transport=_transport(seen))
        self.assertEqual(st["sent"], 1)
        st2 = plaid.enrich_rows(self.conn, limit=100, transport=_transport(seen))
        self.assertTrue(st2["cap_hit"])
        self.assertEqual(len(seen), 1)

    def test_plaid_error_is_reported_and_bills_nothing(self):
        seen = []
        st = plaid.enrich_rows(self.conn, limit=100, transport=_transport(seen, fail=True))
        self.assertIsNotNone(st["error"])
        self.assertEqual(st["sent"], 0)
        self.assertEqual(plaid.enrich_usage(self.conn)["used"], 0)


if __name__ == "__main__":
    unittest.main()
