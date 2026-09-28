"""A settings page loaded before a member was removed must not save that
member back onto the household's mail list.

Every client saves the WHOLE recipient list from a copy it loaded earlier,
so without a server-side check a stale page's next unrelated save (a
cadence toggle, a mute) writes the removed person's address back, and the
household's balances and spending go on reaching someone who no longer
holds an account. The invariant: an address that was not on the stored
list and belongs to no one in the household is saved only when the client
names it as an add; a client that does not name its adds may still add a
new address, but never one whose owner was removed.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget

from .util import _ensure_db, seed_accounts, write_config

PW = "correct-horse-battery"
MEMBER_PW = "member-horse-battery"


class StaleSettingsPageCannotRestoreRemovedMember(unittest.TestCase):
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
        self.owner = TestClient(self.app)
        self.owner_email = f"own-{uuid.uuid4().hex[:8]}@example.dev"
        self.owner.post("/api/signup", data={"email": self.owner_email,
                                             "password": PW})
        # repointing mail, the AI endpoint or the daily report is a step-up act
        self.owner.post("/api/auth/elevate",
                        json={"password": "correct-horse-battery"})
        self.tid = self.owner.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()
        r = self.owner.post("/api/invites", json={
            "label": "partner", "role": "member", "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.b_email = f"member-{uuid.uuid4().hex[:8]}@example.dev"
        got = TestClient(self.app).post("/api/invite/claim", json={
            "token": r.json()["url"].rsplit("token=", 1)[1],
            "email": self.b_email, "password": MEMBER_PW})
        self.assertEqual(got.status_code, 200, got.text)
        self.b = next(u["id"] for u in
                      self.owner.get("/api/users").json()["users"]
                      if u["email"] == self.b_email)
        # the page as it was loaded, before the removal
        self.stale = [self.owner_email, self.b_email]
        r = self.owner.post("/api/settings",
                            json={"email_recipients": self.stale})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.owner.request("DELETE", f"/api/users/{self.b}",
                               json={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)

    def _recipients(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return [str(x).lower() for x in
                    budget.load_config(conn).get("email_recipients") or []]
        finally:
            conn.close()

    def test_stale_full_list_save_does_not_restore_removed_member(self):
        r = self.owner.post("/api/settings",
                            json={"email_recipients": self.stale,
                                  "email_muted": []})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(self.b_email, self._recipients(),
                         "a stale page put the removed member back")
        self.assertIn(self.owner_email, self._recipients())

    def test_named_adds_keep_the_new_address_and_drop_the_stale_one(self):
        new = f"outside-{uuid.uuid4().hex[:6]}@example.dev"
        unnamed = f"stray-{uuid.uuid4().hex[:6]}@example.dev"
        r = self.owner.post("/api/settings", json={
            "email_recipients": self.stale + [new, unnamed],
            "email_recipients_added": [new]})
        self.assertEqual(r.status_code, 200, r.text)
        got = self._recipients()
        self.assertIn(new, got)
        self.assertNotIn(unnamed, got)
        self.assertNotIn(self.b_email, got)

    def test_client_without_named_adds_can_still_add_someone_new(self):
        new = f"outside-{uuid.uuid4().hex[:6]}@example.dev"
        r = self.owner.post("/api/settings", json={
            "email_recipients": [self.owner_email, new]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn(new, self._recipients())

    def test_deliberately_adding_the_removed_person_back_is_allowed(self):
        r = self.owner.post("/api/settings", json={
            "email_recipients": self.stale,
            "email_recipients_added": [self.b_email]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn(self.b_email, self._recipients())

    def test_stale_legacy_email_form_does_not_restore_removed_member(self):
        r = self.owner.post("/settings/email", data={
            "email_recipients": ", ".join(self.stale),
            "email_send_hour_utc": "14"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        self.assertNotIn(self.b_email, self._recipients(),
                         "the legacy form put the removed member back")

    def test_removed_address_is_not_kept_in_the_clear(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            cfg = budget.load_config(conn)
        finally:
            conn.close()
        self.assertNotIn(self.b_email, str(cfg).lower())


if __name__ == "__main__":
    unittest.main()
