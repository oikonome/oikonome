"""A malformed id in a /business route path is a 404, never a 500.

business_entity.id and every nested record id (equity movement, mileage
trip, compliance obligation) are Postgres uuid columns, so a path segment
that is not a uuid fails in the DB cast — before any of our own checks —
and surfaces as a bare 500 from the global exception handler. The invariant
these tests protect: an unparseable id means "no such thing", and every
route says so the same way.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from .util import _ensure_db


class MalformedBusinessIdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"bizid-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.eid = cls.client.post("/api/business/entities", json={
            "name": "Gadgets LLC", "structure": "sole_prop"}).json()["id"]

    def test_status_change_on_a_malformed_entity_id_is_404(self):
        """/status is the one lifecycle door that does not go through the
        shared archive/restore helper, so it reached set_status with the raw
        string and 500'd on the uuid cast."""
        r = self.client.post("/api/business/entities/not-a-uuid/status",
                             json={"status": "archived"})
        self.assertEqual(r.status_code, 404, r.text)

    def test_deleting_a_malformed_sub_resource_id_is_404(self):
        """The entity id in these paths was validated; the second segment
        was not, and it lands in a DELETE ... WHERE id = %s against a uuid
        column just the same."""
        for path in ("equity", "mileage", "compliance"):
            r = self.client.delete(
                f"/api/business/entities/{self.eid}/{path}/not-a-uuid")
            self.assertEqual(r.status_code, 404, f"{path}: {r.text}")

    def test_wellformed_but_unknown_ids_still_404(self):
        """The guard must not swallow the real not-found answer."""
        gone = str(uuid.uuid4())
        self.assertEqual(self.client.post(
            f"/api/business/entities/{gone}/status",
            json={"status": "archived"}).status_code, 404)
        for path in ("equity", "mileage", "compliance"):
            self.assertEqual(self.client.delete(
                f"/api/business/entities/{self.eid}/{path}/{gone}"
            ).status_code, 404, path)


if __name__ == "__main__":
    unittest.main()
