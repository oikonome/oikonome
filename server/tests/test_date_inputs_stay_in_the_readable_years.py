"""A date a caller types is refused (400) outside the years the app can
read, not only when it is malformed.

`date.fromisoformat` accepts 0001-01-01 and 9999-12-31. Taken as-is, such
a date either fails later — a day window stepped past the end of the
calendar raises OverflowError (a 500), or `as_date` refuses it inside a
write that has already committed half of itself — or it is stored and
poisons every reader after it (goal pacing, age from a birthdate). Every
door that parses a caller's ISO date goes through one range check.
"""
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config

BAD = ("0001-01-01", "1899-12-31", "2101-01-01", "9999-12-31")


class DateInputsStayInTheReadableYears(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        security._limiter._hits.clear()
        cls.client = TestClient(appmod.app)
        r = cls.client.post("/api/signup", data={
            "email": f"years-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def test_the_shared_check(self):
        from fastapi import HTTPException

        from oikonome.web.api import _iso_date
        self.assertEqual(str(_iso_date("2026-02-03", "x")), "2026-02-03")
        for bad in BAD + ("nonsense", ""):
            with self.assertRaises(HTTPException, msg=bad) as cm:
                _iso_date(bad, "x")
            self.assertEqual(cm.exception.status_code, 400)

    def test_today_for_a_day_outside_the_years(self):
        for bad in BAD:
            r = self.client.get(f"/api/today/full?date={bad}")
            self.assertEqual(r.status_code, 400, bad)

    def test_a_bill_rename_with_such_a_next_due_changes_nothing(self):
        r = self.client.post("/api/bills/save", json={
            "payee": "Gas Co", "amount": "40", "cadence": "MONTHLY",
            "next_due": "2026-03-01"})
        self.assertEqual(r.status_code, 200, r.text)
        for bad in BAD:
            r = self.client.post("/api/bills/save", json={
                "payee": "Gas Company", "orig_payee": "Gas Co",
                "amount": "40", "cadence": "MONTHLY", "next_due": bad})
            self.assertEqual(r.status_code, 400, bad)
        conn = tenancy.tenant_connect(self.tid)
        try:
            names = {row["payee"] for row in conn.execute(
                "SELECT payee FROM bills").fetchall()}
        finally:
            conn.close()
        self.assertIn("Gas Co", names)
        self.assertNotIn("Gas Company", names)

    def test_a_goal_target_date_and_a_birthdate(self):
        for bad in BAD:
            r = self.client.post("/api/settings", json={"savings_goals": [
                {"name": "Trip", "target": 1000, "target_date": bad}]})
            self.assertEqual(r.status_code, 400, bad)
            r = self.client.post("/api/settings", json={"birthdate": bad})
            self.assertEqual(r.status_code, 400, bad)


if __name__ == "__main__":
    unittest.main()
