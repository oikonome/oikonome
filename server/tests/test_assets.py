"""manual_assets becomes writable through /api/settings — the
Net Worth page owns the property/vehicles/mortgage editor. Auto-valuation keys ride through
untouched; a value edit arrives with as_of cleared and is stamped today
so the source tag shows when the human last spoke."""

import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from .util import _ensure_db


class ManualAssetsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"assets-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})

    def _get(self):
        return self.client.get("/api/settings").json()["manual_assets"]

    def test_add_edit_delete_round_trip(self):
        r = self.client.post("/api/settings", json={"manual_assets": [
            {"name": "Home", "kind": "property", "value": 450000},
            {"name": "Mortgage", "kind": "mortgage", "value": -210000},
        ]})
        self.assertEqual(r.status_code, 200, r.text)
        got = self._get()
        self.assertEqual([a["name"] for a in got], ["Home", "Mortgage"])
        self.assertEqual(got[0]["value"], 450000)
        self.assertEqual(got[0]["as_of"], dt.date.today().isoformat())
        # edit one, delete the other
        r = self.client.post("/api/settings", json={"manual_assets": [
            {"name": "Home", "kind": "property", "value": 475000}]})
        self.assertEqual(r.status_code, 200)
        got = self._get()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["value"], 475000)
        # empty list clears the key entirely
        r = self.client.post("/api/settings", json={"manual_assets": []})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self._get())

    def test_auto_valuation_keys_ride_through(self):
        r = self.client.post("/api/settings", json={"manual_assets": [
            {"name": "Car", "kind": "vehicle", "value": 18000,
             "auto": "depreciate", "rate": 0.12}]})
        self.assertEqual(r.status_code, 200, r.text)
        got = self._get()[0]
        self.assertEqual(got["auto"], "depreciate")
        self.assertEqual(got["rate"], 0.12)

    def test_provided_as_of_is_kept(self):
        r = self.client.post("/api/settings", json={"manual_assets": [
            {"name": "Home", "kind": "property", "value": 450000,
             "as_of": "2026-01-15"}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._get()[0]["as_of"], "2026-01-15")

    def test_bad_shapes_are_400(self):
        self.assertEqual(self.client.post(
            "/api/settings",
            json={"manual_assets": "nope"}).status_code, 400)
        self.assertEqual(self.client.post(
            "/api/settings",
            json={"manual_assets": [{"kind": "property", "value": 1}]}
        ).status_code, 400)                       # nameless

    def test_as_of_that_is_not_a_date_is_400_and_nothing_saved(self):
        for bad in ("soon", "2026-13-40", "9999-01-01"):
            r = self.client.post("/api/settings", json={"manual_assets": [
                {"name": "Boat", "kind": "vehicle", "value": 1,
                 "as_of": bad}]})
            self.assertEqual(r.status_code, 400, bad)
        self.assertFalse(any(a["name"] == "Boat" for a in self._get() or []))

    def test_assets_feed_the_networth_report(self):
        self.client.post("/api/settings", json={"manual_assets": [
            {"name": "Cabin", "kind": "property", "value": 120000}]})
        r = self.client.get("/api/reports/networth").json()
        names = [p["name"] for p in r["property_items"]]
        self.assertIn("Cabin", names)


if __name__ == "__main__":
    unittest.main()
