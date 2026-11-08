"""A repeated create of the same business makes one business.

A household may keep several businesses, so the create door cannot refuse a
second one. But a double tap, a second tab running the setup flow, or a
client retry after a lost response sends the SAME create again within
moments, and each used to insert a new row: two businesses with one name, the
books split between them. An identical active business created moments ago
is that request repeated, so the door answers it with the business it already
made. A different business, or the same name once the first is closed, is
still a new one.
"""
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy


class RepeatedBusinessCreateTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        from .util import _ensure_db
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"bizonce-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]

    def _count(self, name):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return conn.execute(
                "SELECT count(*) AS n FROM business_entity WHERE name = %s",
                (name,)).fetchone()["n"]
        finally:
            conn.close()

    def _create(self, name, structure="sole_prop"):
        r = self.client.post("/api/business/entities", json={
            "name": name, "structure": structure})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def test_the_same_create_sent_twice_makes_one_business(self):
        name = f"Twice {uuid.uuid4().hex[:6]} LLC"
        first = self._create(name)
        again = self._create(name)
        self.assertEqual(again, first)
        self.assertEqual(self._count(name), 1)

    def test_a_retry_with_a_corrected_answer_keeps_the_correction(self):
        """The first create landed but its answer was lost; the person fixed
        the start date and pressed Create again. One business, new date."""
        name = f"Retry {uuid.uuid4().hex[:6]} LLC"
        r = self.client.post("/api/business/entities", json={
            "name": name, "structure": "sole_prop",
            "business_start_date": "2026-01-01", "state": "ID"})
        self.assertEqual(r.status_code, 200, r.text)
        first = r.json()["id"]
        r = self.client.post("/api/business/entities", json={
            "name": name, "structure": "sole_prop",
            "business_start_date": "2025-06-01", "state": "ID"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["id"], first)
        self.assertEqual(self._count(name), 1)
        conn = tenancy.tenant_connect(self.tid)
        try:
            got = conn.execute(
                "SELECT business_start_date FROM business_entity WHERE id = %s",
                (first,)).fetchone()["business_start_date"]
        finally:
            conn.close()
        self.assertEqual(str(got), "2025-06-01")

    def test_a_different_business_is_still_created(self):
        name = f"Pair {uuid.uuid4().hex[:6]} LLC"
        a = self._create(name)
        b = self._create(name + " Two")
        c = self._create(name, structure="single_member_llc")
        self.assertEqual(len({a, b, c}), 3)

    def test_the_name_of_a_closed_business_can_be_used_again(self):
        name = f"Reopen {uuid.uuid4().hex[:6]} LLC"
        first = self._create(name)
        r = self.client.post(f"/api/business/entities/{first}/archive")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotEqual(self._create(name), first)


if __name__ == "__main__":
    unittest.main()
