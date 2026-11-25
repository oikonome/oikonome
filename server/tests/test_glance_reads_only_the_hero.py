"""The home-screen widget's glance reads the month status and the hero
built from it (todayview.hero_face, the page's own), and nothing else. It
used to run the whole Today read — the 60-day forecast walk, the runway,
goal progress, the anomaly watch and the alert log — for a reply of a few
figures, costing nearly a full Today on every widget refresh, and writing
the alert log as it went. The figures are still the page's to the
dollar. Figures are invented."""

import datetime as dt
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, add_txn, seed_accounts, write_config


class GlanceCostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"glance-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn, food_monthly=640, other_monthly=910)
            today = dt.date.today()
            add_txn(conn, today, 23.0, "CORNER CAFE",
                    primary="FOOD_AND_DRINK")
            add_txn(conn, today.replace(day=1), 140.0, "PINE OUTFITTERS")
        finally:
            conn.close()

    def test_the_glance_is_the_pages_hero_without_the_rest(self):
        full = self.client.get("/api/today/full").json()
        with mock.patch("oikonome.web.report.gather",
                        side_effect=AssertionError("full Today read")), \
             mock.patch("oikonome.engine.forecast.build",
                        side_effect=AssertionError("forecast walk")):
            r = self.client.get("/api/today/glance")
        self.assertEqual(r.status_code, 200, r.text)
        g = r.json()
        self.assertEqual(g["left_today"], round(full["simple"]["left_today"]))
        self.assertEqual(g["verdict"], full["verdict"])
        self.assertEqual(g["pace_line"], full["pace_line"])
        self.assertEqual(g["days_left"], full["days_left"])
        self.assertEqual([c["text"] for c in g["chips"]],
                         [c["text"] for c in full["simple"]["chips"]])
        self.assertEqual((g["day_spent"], g["day_allow"]),
                         (round(full["day_meter"]["spent"]),
                          round(full["day_meter"]["allow"])))


if __name__ == "__main__":
    unittest.main()
