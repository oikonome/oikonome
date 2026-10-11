"""The business tax and compliance doors run on the household's own day.

An instance clock in UTC is hours ahead of a US household. Read on that
clock, a 1040-ES deadline drops off the compliance list (and the .ics) in the
evening of its own due date, and on the household's New Year's Eve the
estimated-tax, 1099 and mileage figures jump to next year's empty one. Every
door therefore takes the day from the household's settings, the way the
Today page and lenses do. The tests pin that day well away from the instance
clock's and check each door follows it.
"""
import datetime as dt
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config


class BusinessDoorsUseHouseholdDay(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"day-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()
        r = cls.client.post("/api/business/entities", json={
            "name": f"Day {uuid.uuid4().hex[:6]} LLC",
            "structure": "sole_prop"})
        cls.eid = r.json()["id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            conn.execute("UPDATE business_entity SET income_tax_rate = 0.2 "
                         "WHERE id = %s", (cls.eid,))
        finally:
            conn.close()
        # a household year that is never the instance clock's year
        cls.year = dt.date.today().year - 2

    def _on(self, day):
        return mock.patch("oikonome.web.api._today", return_value=day)

    def test_a_deadline_due_today_is_still_listed(self):
        due = dt.date(self.year, 4, 15)
        with self._on(due):
            obs = self.client.get(
                f"/api/business/entities/{self.eid}/compliance").json()
            ics = self.client.get(
                f"/api/business/entities/{self.eid}/compliance.ics",
                headers={"Sec-Fetch-Site": "same-origin"}).text
        est = [o for o in obs["obligations"] if "1040-ES" in o["title"]]
        self.assertEqual(est[0]["due"], due.isoformat())
        self.assertIn(due.strftime("%Y%m%d"), ics)

    def test_estimated_tax_follows_the_household_year(self):
        with self._on(dt.date(self.year, 12, 31)):
            r = self.client.get(
                f"/api/business/entities/{self.eid}/estimated-tax")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["year"], self.year)

    def test_mileage_and_vendors_follow_the_household_year(self):
        from oikonome.engine import selfemploy
        seen = []
        real = selfemploy.vendor_totals

        def spy(conn, entity_id, year=None):
            seen.append(year)
            return real(conn, entity_id, year)
        with self._on(dt.date(self.year, 12, 31)), \
                mock.patch.object(selfemploy, "vendor_totals", spy):
            m = self.client.get(
                f"/api/business/entities/{self.eid}/mileage").json()
            v = self.client.get(
                f"/api/business/entities/{self.eid}/vendors")
        self.assertEqual(m["summary"]["year"], self.year)
        self.assertEqual(v.status_code, 200, v.text)
        self.assertEqual(seen, [self.year])


if __name__ == "__main__":
    unittest.main()
