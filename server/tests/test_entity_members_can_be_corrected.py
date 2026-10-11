"""A business's members can be corrected and removed, not only added.

Ownership shares decide how a multi-member estimate splits the profit: a
typo, a doubled tap or a partner leaving leaves shares that no longer add
to 100, and the estimate quietly taxes the whole profit as one person's.
So the owner may edit a member's name, share or manager flag and remove a
member; a member may not, an archived (read-only) business refuses both,
and one business's door cannot reach another's members.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


class EntityMemberDoorsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.app)
        r = self.client.post("/api/signup", data={
            "email": f"mem-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        self.eid = self._entity("Two Oaks LLC")
        r = self.client.post(f"/api/business/entities/{self.eid}/members",
                             json={"member_name": "Pat Rowan",
                                   "ownership_pct": 5})
        self.assertEqual(r.status_code, 200, r.text)
        self.mid = r.json()["id"]

    def _entity(self, name):
        return self.client.post("/api/business/entities", json={
            "name": name, "structure": "multi_member_llc"}).json()["id"]

    def _url(self, eid=None, mid=None):
        return (f"/api/business/entities/{eid or self.eid}/members/"
                f"{mid or self.mid}")

    def _members(self):
        return self.client.get(
            f"/api/business/entities/{self.eid}").json()["members"]

    def test_the_owner_corrects_a_share_and_the_manager_flag(self):
        r = self.client.patch(self._url(), json={"ownership_pct": 50,
                                                 "is_manager": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"id": self.mid, "member_name": "Pat Rowan",
                                    "ownership_pct": 50.0,
                                    "is_manager": True,
                                    "is_household": False})
        self.assertEqual(self._members()[0]["ownership_pct"], 50.0)

    def test_a_bad_value_is_a_400(self):
        for body in ({"ownership_pct": 150}, {"ownership_pct": "lots"},
                     {"member_name": ""}, {"is_manager": "yes"}):
            r = self.client.patch(self._url(), json=body)
            self.assertEqual(r.status_code, 400, (body, r.text))

    def test_the_owner_removes_a_member(self):
        r = self.client.delete(self._url())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._members(), [])
        self.assertEqual(self.client.delete(self._url()).status_code, 404)

    def test_another_entitys_door_cannot_reach_the_member(self):
        other = self._entity("Elsewhere LLC")
        self.assertEqual(self.client.patch(
            self._url(eid=other), json={"ownership_pct": 1}).status_code, 404)
        self.assertEqual(self.client.delete(
            self._url(eid=other)).status_code, 404)
        self.assertEqual(len(self._members()), 1)

    def test_an_archived_business_refuses_both(self):
        r = self.client.post(f"/api/business/entities/{self.eid}/archive")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.patch(
            self._url(), json={"ownership_pct": 50}).status_code, 409)
        self.assertEqual(self.client.delete(self._url()).status_code, 409)

    def test_a_member_of_the_household_may_not(self):
        from oikonome.auth import passwords
        email = f"memm-{uuid.uuid4().hex[:8]}@example.dev"
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s,%s,%s,'member',now())",
                (self.tid, email, passwords.hash_password(PW)))
        finally:
            admin.close()
        c = TestClient(self.app)
        r = c.post("/login", data={"email": email, "password": PW},
                   follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        self.assertEqual(c.patch(self._url(),
                                 json={"ownership_pct": 50}).status_code, 403)
        self.assertEqual(c.delete(self._url()).status_code, 403)
        self.assertEqual(self._members()[0]["ownership_pct"], 5.0)


if __name__ == "__main__":
    unittest.main()
