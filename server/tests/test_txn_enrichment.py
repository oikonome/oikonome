"""What the aggregator knows about a transaction becomes columns (migration
098) — check number, payment channel, the payment app in front of the
merchant, location, MCC, authorized date — written on ingest for every
source, backfilled from raw, round-tripped by restore, searchable, and
shown with the category's PROVENANCE (category_source + why).

Also the invariant that keeps provenance honest: every SQL statement in the
package that writes category_primary also writes category_source."""

import json
import pathlib
import re
import unittest

import httpx

from oikonome.engine import llm_categorize
from oikonome.sync import base, plaid
from oikonome.web import data

from .util import make_db, write_config

STORAGE = {
    "transaction_id": "en1", "account_id": "p-chk", "date": "2026-08-16",
    "authorized_date": "2026-08-15",
    "amount": 120.00, "name": "Keystone Storage", "merchant_name": "Keystone Storage",
    "pending": False, "payment_channel": "in store", "check_number": None,
    "merchant_category_code": "4225",
    "location": {"city": "Springfield", "region": "IL", "address": "1 Storage Way",
                 "postal_code": "62701", "lat": 39.8, "lon": -89.65,
                 "store_number": "0001"},
    "counterparties": [{"name": "Keystone Storage", "type": "merchant", "entity_id": "ENT-U",
                        "confidence_level": "VERY_HIGH"}],
    "personal_finance_category": {"primary": "GENERAL_SERVICES",
                                  "detailed": "GENERAL_SERVICES_POSTAGE_AND_SHIPPING",
                                  "confidence_level": "LOW"}}
CHECK = {
    "transaction_id": "en2", "account_id": "p-chk", "date": "2026-08-15",
    "amount": 850.00, "name": "Check Paid #4417", "merchant_name": None,
    "pending": False, "payment_channel": "other", "check_number": "4417",
    "location": {}, "counterparties": [],
    "personal_finance_category": {"primary": "GENERAL_SERVICES",
                                  "detailed": "GENERAL_SERVICES_OTHER",
                                  "confidence_level": "LOW"}}
STREAMCO = {
    "transaction_id": "en3", "account_id": "p-chk", "date": "2026-08-15",
    "amount": 12.99, "name": "PAYPAL *STREAMCO", "merchant_name": "Streamco",
    "pending": False, "payment_channel": "online",
    "counterparties": [
        {"name": "PayPal", "type": "payment_app", "entity_id": "ENT-PP",
         "confidence_level": "VERY_HIGH"},
        {"name": "Streamco", "type": "merchant", "entity_id": "ENT-SP",
         "confidence_level": "VERY_HIGH"}],
    "personal_finance_category": {"primary": "ENTERTAINMENT",
                                  "detailed": "ENTERTAINMENT_MUSIC_AND_AUDIO",
                                  "confidence_level": "VERY_HIGH"}}
ACCOUNTS = {"accounts": [
    {"account_id": "p-chk", "name": "Plaid Checking", "type": "depository",
     "subtype": "checking", "mask": "4242",
     "balances": {"current": 3000.0, "available": 2900.0,
                  "iso_currency_code": "USD"}}]}
PAGE = {"added": [STORAGE, CHECK, STREAMCO], "modified": [], "removed": [],
        "has_more": False, "next_cursor": "c1"}


def _transport():
    def handler(request):
        path = request.url.path
        if path == "/accounts/get":
            return httpx.Response(200, text=json.dumps(ACCOUNTS))
        if path == "/transactions/sync":
            return httpx.Response(200, text=json.dumps(PAGE))
        if path == "/item/public_token/exchange":
            return httpx.Response(200, text=json.dumps(
                {"item_id": "plaid-item-1", "access_token": "access-prod-abc"}))
        return httpx.Response(404, text="{}")
    return httpx.MockTransport(handler)


class EnrichmentFromRawTests(unittest.TestCase):
    def test_plaid_shape(self):
        e = base.enrichment_from_raw(STORAGE)
        self.assertEqual(e["mcc"], "4225")
        self.assertEqual(e["payment_channel"], "in store")
        self.assertEqual((e["location_city"], e["location_region"], e["location_store"]),
                         ("Springfield", "IL", "0001"))
        self.assertAlmostEqual(e["location_lat"], 39.8)
        self.assertEqual(str(e["authorized_date"]), "2026-08-15")
        self.assertIsNone(e["payment_processor"])
        self.assertEqual(base.enrichment_from_raw(STREAMCO)["payment_processor"], "PayPal")
        self.assertEqual(base.enrichment_from_raw(CHECK)["check_number"], "4417")

    def test_no_raw_is_all_null(self):
        self.assertTrue(all(v is None for v in base.enrichment_from_raw({}).values()))
        self.assertTrue(all(v is None for v in base.enrichment_from_raw(None).values()))


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")
        plaid.link_item(self.conn, "public-xyz", "Demo", transport=_transport())

    def tearDown(self):
        self.conn.close()

    def _row(self, tid):
        return self.conn.execute(
            "SELECT * FROM transactions WHERE id=%s", (tid,)).fetchone()

    def test_columns_land_on_ingest(self):
        r = self._row("en1")
        self.assertEqual((r["mcc"], r["payment_channel"], r["location_city"],
                          r["location_store"], str(r["authorized_date"])),
                         ("4225", "in store", "Springfield", "0001", "2026-08-15"))
        self.assertEqual(self._row("en2")["check_number"], "4417")
        self.assertEqual(self._row("en3")["payment_processor"], "PayPal")
        # Plaid decided these categories, and the row says so
        self.assertEqual(self._row("en3")["category_source"], "plaid")

    def test_mcc_sharpens_a_low_confidence_plaid_answer(self):
        # Plaid said postage & shipping at LOW; the merchant's MCC 4225 says
        # storage — the MCC pass wins, and says so
        llm_categorize.categorize_new(self.conn)
        r = self._row("en1")
        self.assertEqual((r["category_primary"], r["category_detailed"],
                          r["category_source"]),
                         ("GENERAL_SERVICES", "GENERAL_SERVICES_STORAGE", "mcc"))
        # a VERY_HIGH Plaid answer is left alone even with an MCC present
        self.conn.execute("UPDATE transactions SET mcc='5411' WHERE id='en3'")
        llm_categorize.mcc_classify(self.conn)
        self.assertEqual(self._row("en3")["category_source"], "plaid")

    def test_mcc_never_overrides_a_fuel_labelled_row(self):
        """The statement line calling a row the brand's pump is direct
        evidence; the merchant's registered line of business (a warehouse's
        grocery MCC on its fuel arm) must not overwrite it, at any Plaid
        confidence."""
        self.conn.execute(
            "UPDATE transactions SET category_primary='TRANSPORTATION', "
            "category_detailed='TRANSPORTATION_GAS', category_source='fuel', "
            "mcc='5411' WHERE id='en1'")
        llm_categorize.mcc_classify(self.conn)
        r = self._row("en1")
        self.assertEqual((r["category_primary"], r["category_source"]),
                         ("TRANSPORTATION", "fuel"))

    def test_fuel_rows_the_mcc_pass_overwrote_are_put_back(self):
        """A pump row an MCC pass flipped to the warehouse's grocery code is
        put back to the ingest's stamp by the next pass — the row is an
        outlet, and the receipt says pump."""
        self.conn.execute(
            "UPDATE transactions SET merchant_outlet='Example Warehouse Gas', "
            "category_primary='FOOD_AND_DRINK', category_detailed='FOOD_AND_DRINK_GROCERIES', "
            "category_source='mcc', mcc='5411' WHERE id='en1'")
        llm_categorize.mcc_classify(self.conn)
        r = self._row("en1")
        self.assertEqual((r["category_primary"], r["category_detailed"],
                          r["category_source"]),
                         ("TRANSPORTATION", "TRANSPORTATION_GAS", "fuel"))

    def test_ledger_rows_carry_the_facts_and_the_why(self):
        rows, *_ = data.search_transactions(self.conn, "keystone")
        r = rows[0]
        self.assertEqual((r["mcc"], r["payment_channel"], r["location_city"]),
                         ("4225", "in store", "Springfield"))
        self.assertEqual(r["category_source"], "plaid")
        from oikonome.web import api as _api
        self.assertEqual(_api._category_why(dict(r)), "Plaid, low confidence")
        # a person's override explains itself
        data.set_category(self.conn, "en1", "RENT_AND_UTILITIES")
        rows, *_ = data.search_transactions(self.conn, "keystone")
        self.assertTrue(rows[0]["override_manual"])
        self.assertEqual(_api._category_why(dict(rows[0])),
                         "you set it on this transaction")
        # and undoing it hands the why back to the layer underneath: the
        # flag says a person set THIS category, so a row carrying none of
        # theirs must not still be claiming they did
        data.clear_category(self.conn, "en1")
        rows, *_ = data.search_transactions(self.conn, "keystone")
        self.assertFalse(rows[0]["override_manual"])
        self.assertEqual(_api._category_why(dict(rows[0])),
                         "Plaid, low confidence")

    def test_search_reaches_check_number_city_and_processor(self):
        self.assertEqual([r["id"] for r in data.search_transactions(self.conn, "4417")[0]],
                         ["en2"])
        self.assertEqual([r["id"] for r in data.search_transactions(self.conn, "springfield")[0]],
                         ["en1"])
        self.assertEqual([r["id"] for r in data.search_transactions(self.conn, "paypal")[0]],
                         ["en3"])
        self.assertEqual({r["id"] for r in data.search_transactions(
            self.conn, "", checks_only=True)[0]}, {"en2"})
        self.assertEqual({r["id"] for r in data.search_transactions(
            self.conn, "", channel="online")[0]}, {"en3"})

    def test_restore_round_trips_the_columns(self):
        from oikonome.sync import restore
        from .test_restore import _export_zip
        z = _export_zip(self.conn, ["items", "accounts", "merchants",
                                    "merchant_canonical", "transactions"])
        fresh = make_db()
        try:
            restore.restore_zip(fresh, z)
            r = fresh.execute("SELECT * FROM transactions WHERE id='en1'").fetchone()
            self.assertEqual((r["mcc"], r["location_city"], r["category_source"]),
                             ("4225", "Springfield", "plaid"))
            self.assertEqual(fresh.execute(
                "SELECT check_number FROM transactions WHERE id='en2'").fetchone()["check_number"],
                "4417")
        finally:
            fresh.close()


class SourceInvariantTests(unittest.TestCase):
    def test_every_primary_writer_stamps_the_source(self):
        """Provenance is only honest if no writer forgets it: any SQL in the
        package that assigns category_primary (an UPDATE ... SET, or an
        INSERT column list) must also carry category_source in the same
        statement. Renames of a category value are exempt — the source
        does not change when the name does."""
        root = pathlib.Path(__file__).resolve().parents[1] / "oikonome"
        stmt = re.compile(r'"""(.*?)"""|"((?:[^"\\]|\\.)*)"', re.S)
        offenders = []
        for f in sorted(root.rglob("*.py")):
            text = f.read_text(encoding="utf-8")
            # stitch adjacent string literals the way Python does, so a
            # statement split across "..." "..." lines is read whole
            joined = re.sub(r'"\s*\n\s*"', "", text)
            for m in stmt.finditer(joined):
                sql = m.group(1) or m.group(2) or ""
                if not re.search(r"\bcategory_primary\b", sql):
                    continue
                writes = (re.search(r"UPDATE\s+transactions\b[^;]*?\bSET\b[^;]*?category_primary\s*=", sql, re.I | re.S)
                          or re.search(r"INSERT\s+INTO\s+transactions\b[^;]*?category_primary", sql, re.I | re.S))
                if not writes:
                    continue
                if "category_source" in sql:
                    continue
                # a rename keeps the source
                if re.search(r"category_primary\s*=\s*%s\s*\"?\s*WHERE\s+category_primary\s*=\s*%s", sql, re.I | re.S):
                    continue
                offenders.append(f"{f.relative_to(root)}: {sql.strip()[:80]!r}")
        self.assertEqual(offenders, [], "writers of category_primary that never say who: "
                         + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
