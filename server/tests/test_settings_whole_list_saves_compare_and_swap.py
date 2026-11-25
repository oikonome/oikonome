"""A whole-list settings save names the version of the list it edited.

Savings goals, budget categories, income scenarios and the mail schedule
are each saved WHOLE from a copy the client read earlier. Two devices that
each read the list, edit a different entry and save would otherwise lose
the first device's edit without a word. So the settings read publishes a
version per list, a save may send it back, and a save whose version no
longer matches the stored list is refused with 409 and writes nothing — the
client re-reads, re-applies its edit and saves again. A save that sends no
version (an older client) replaces the list as before."""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget

from .util import _ensure_db

PW = "correct-horse-battery"

LISTS = ("savings_goals", "custom_buckets", "income_scenarios",
         "email_schedule", "llm_backends", "llm_roles", "manual_assets")

# one edit per list, two distinct values each: what device A and device B
# would each save
EDITS = {
    "savings_goals": ([{"name": "Trip", "target": 1200}],
                      [{"name": "Roof", "target": 4400}]),
    "custom_buckets": ([{"name": "Pets", "parent": "other", "monthly": 40}],
                       [{"name": "Gym", "parent": "other", "monthly": 25}]),
    "income_scenarios": ([{"name": "Base", "take_home": 2100,
                           "cadence": "monthly"}],
                         [{"name": "Raise", "take_home": 2600,
                           "cadence": "monthly"}]),
    "email_schedule": ({"daily": {"on": True, "hour": 6}},
                       {"daily": {"on": True, "hour": 9}}),
}


class WholeListSavesCompareAndSwap(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        r = cls.client.post("/api/signup", data={
            "email": f"cas-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        assert r.status_code == 200, r.text
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]

    def setUp(self):
        r = self.client.post("/api/settings", json={
            "savings_goals": [], "custom_buckets": [],
            "income_scenarios": [], "email_schedule": None,
            "food_monthly": 300})
        self.assertEqual(r.status_code, 200, r.text)

    def _stored(self) -> dict:
        conn = tenancy.tenant_connect(self.tid)
        try:
            return budget.load_config(conn)
        finally:
            conn.close()

    def _versions(self) -> dict:
        return self.client.get("/api/settings").json()["list_versions"]

    def test_the_read_publishes_a_version_for_each_list(self):
        v = self._versions()
        self.assertEqual(set(v), set(LISTS))
        for k in LISTS:
            self.assertIsInstance(v[k], str)
            self.assertTrue(v[k])
        # a list never set has a version that stays put across reads
        self.assertEqual(v, self._versions())

    def test_a_stale_device_is_refused_and_its_retry_lands(self):
        # the AI and asset lists have their own edits and tests in
        # test_ai_and_asset_saves_compare_and_swap
        for key in EDITS:
            with self.subTest(key=key):
                self.setUp()
                a_val, b_val = EDITS[key]
                seen_by_a = self._versions()
                seen_by_b = self._versions()
                r = self.client.post("/api/settings", json={
                    key: a_val, "list_versions": {key: seen_by_a[key]}})
                self.assertEqual(r.status_code, 200, r.text)
                self.assertNotEqual(r.json()["list_versions"][key],
                                    seen_by_b[key])
                after_a = self._stored().get(key)
                r = self.client.post("/api/settings", json={
                    key: b_val, "list_versions": {key: seen_by_b[key]}})
                self.assertEqual(r.status_code, 409, r.text)
                body = r.json()
                self.assertEqual(body["error"], "settings_moved")
                self.assertEqual(body["keys"], [key])
                self.assertEqual(set(body["list_versions"]), set(LISTS))
                self.assertEqual(self._stored().get(key), after_a,
                                 "the refused save changed the stored list")
                # B re-reads and retries with the fresh version
                fresh = self._versions()
                self.assertEqual(fresh, body["list_versions"])
                r = self.client.post("/api/settings", json={
                    key: b_val, "list_versions": {key: fresh[key]}})
                self.assertEqual(r.status_code, 200, r.text)
                self.assertNotEqual(self._stored().get(key), after_a)

    def test_a_refused_save_writes_nothing_else_from_its_body(self):
        stale = self._versions()["savings_goals"]
        self.client.post("/api/settings",
                         json={"savings_goals": EDITS["savings_goals"][0]})
        r = self.client.post("/api/settings", json={
            "savings_goals": EDITS["savings_goals"][1],
            "food_monthly": 999,
            "list_versions": {"savings_goals": stale}})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self._stored().get("food_monthly"), 300)

    def test_a_save_without_versions_still_replaces_the_list(self):
        self.client.post("/api/settings",
                         json={"savings_goals": EDITS["savings_goals"][0]})
        r = self.client.post("/api/settings",
                             json={"savings_goals": EDITS["savings_goals"][1]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            [g["name"] for g in self._stored()["savings_goals"]], ["Roof"])

    def test_a_stale_version_for_a_list_not_in_the_body_is_ignored(self):
        stale = self._versions()["custom_buckets"]
        self.client.post("/api/settings",
                         json={"custom_buckets": EDITS["custom_buckets"][0]})
        r = self.client.post("/api/settings", json={
            "food_monthly": 410, "list_versions": {"custom_buckets": stale}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._stored().get("food_monthly"), 410)

    def test_the_versions_are_never_stored(self):
        v = self._versions()
        self.client.post("/api/settings", json={
            "savings_goals": EDITS["savings_goals"][0], "list_versions": v})
        self.assertNotIn("list_versions", self._stored())

    def test_a_malformed_versions_field_is_refused(self):
        r = self.client.post("/api/settings", json={
            "savings_goals": [], "list_versions": ["x"]})
        self.assertEqual(r.status_code, 400, r.text)

    def test_a_member_may_send_versions_for_the_lists_it_may_save(self):
        r = self.client.post("/api/invites", json={
            "label": "partner", "role": "member", "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        token = r.json()["url"].rsplit("token=", 1)[1]
        import oikonome.web.app as appmod
        member = TestClient(appmod.app)
        got = member.post("/api/invite/claim", json={
            "token": token,
            "email": f"m-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "family-member-pass"})
        self.assertEqual(got.status_code, 200, got.text)
        stale = member.get("/api/settings").json()["list_versions"]
        r = member.post("/api/settings", json={
            "savings_goals": EDITS["savings_goals"][0],
            "list_versions": stale})
        self.assertEqual(r.status_code, 200, r.text)
        r = member.post("/api/settings", json={
            "savings_goals": EDITS["savings_goals"][1],
            "list_versions": stale})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertNotIn("list_versions", self._stored())


if __name__ == "__main__":
    unittest.main()
