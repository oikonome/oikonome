"""The AI backends, their per-task routing and the hand-valued assets are
saved WHOLE, the way the clients send them, and name the version they read.

The web and phone AI cards post the backend rows they seeded from the
settings view (stripped of the presence flags) with the routing map; the
Net Worth editors post the asset list they merged onto a fresh read. Each
names the version of the view it was built on. A save built on a view that
another device has since saved over is refused with 409 settings_moved and
writes nothing; a save on the current version lands, and a backend row
sent back without its key keeps the stored key."""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget

from .util import _ensure_db

PW = "correct-horse-battery"
URL = "https://ai.example.com/v1"


class AiAndAssetSavesCompareAndSwap(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        r = cls.client.post("/api/signup", data={
            "email": f"cas-ai-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        assert r.status_code == 200, r.text
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        # a new AI destination is an outbound route: the door wants a
        # fresh proof of identity, as a person changing it would give
        r = cls.client.post("/api/auth/elevate", json={"password": PW})
        assert r.status_code == 200, r.text

    def setUp(self):
        r = self.client.post("/api/settings", json={
            "llm_backends": [], "llm_roles": {}, "manual_assets": []})
        self.assertEqual(r.status_code, 200, r.text)

    def _stored(self) -> dict:
        conn = tenancy.tenant_connect(self.tid)
        try:
            return budget.load_config(conn)
        finally:
            conn.close()

    def _view(self) -> dict:
        r = self.client.get("/api/settings")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _save(self, body: dict, view: dict):
        keys = [k for k in ("llm_backends", "llm_roles", "manual_assets")
                if k in body]
        return self.client.post("/api/settings", json={
            **body, "list_versions": {k: view["list_versions"][k]
                                      for k in keys}})

    @staticmethod
    def _card_rows(view: dict) -> list:
        # what both AI cards send: the view's rows less the presence flags
        return [{k: v for k, v in b.items()
                 if k not in ("api_key_set", "extra_body_set", "local")}
                for b in view["llm_backends"]]

    def test_the_view_publishes_versions_for_the_three_lists(self):
        v = self._view()["list_versions"]
        for k in ("llm_backends", "llm_roles", "manual_assets"):
            self.assertIsInstance(v.get(k), str, k)

    def test_an_ai_card_save_on_a_stale_view_is_refused_and_writes_nothing(self):
        a = self._view()
        r = self._save({"llm_backends": [
            {"id": "aa11", "name": "One", "url": URL, "model": "m-1",
             "api_key": "sk-test-one"}],
            "llm_roles": {"assistant": "aa11"}}, a)
        self.assertEqual(r.status_code, 200, r.text)
        # device B read before device A saved, and saves its own backend
        r = self._save({"llm_backends": [
            {"id": "bb22", "name": "Two", "url": URL, "model": "m-2"}],
            "llm_roles": {"assistant": "bb22"}}, a)
        self.assertEqual(r.status_code, 409, r.text)
        d = r.json().get("detail", r.json())
        self.assertEqual(d["error"], "settings_moved")
        self.assertIn("llm_backends", d["keys"])
        cfg = self._stored()
        self.assertEqual([b["id"] for b in cfg["llm_backends"]], ["aa11"])
        self.assertEqual(cfg["llm_roles"].get("assistant"), "aa11")

    def test_a_card_save_on_the_current_view_lands_and_keeps_the_key(self):
        r = self._save({"llm_backends": [
            {"id": "aa11", "name": "One", "url": URL, "model": "m-1",
             "api_key": "sk-test-one"}]}, self._view())
        self.assertEqual(r.status_code, 200, r.text)
        view = r.json()
        rows = self._card_rows(view) + [
            {"id": "cc33", "name": "Three", "url": URL, "model": "m-3"}]
        r = self._save({"llm_backends": rows,
                        "llm_roles": {"assistant": "cc33"}}, view)
        self.assertEqual(r.status_code, 200, r.text)
        out = {b["id"]: b for b in r.json()["llm_backends"]}
        self.assertEqual(set(out), {"aa11", "cc33"})
        self.assertTrue(out["aa11"]["api_key_set"])
        self.assertEqual(self._stored()["llm_roles"].get("assistant"), "cc33")

    def test_an_asset_save_on_a_stale_read_is_refused_and_writes_nothing(self):
        a = self._view()
        r = self._save({"manual_assets": [
            {"name": "House", "value": 300000, "kind": "property"}]}, a)
        self.assertEqual(r.status_code, 200, r.text)
        r = self._save({"manual_assets": [
            {"name": "Car", "value": 9000, "kind": "vehicle"}]}, a)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual([x["name"] for x in self._stored()["manual_assets"]],
                         ["House"])
        # built on the read the refusal sends it back to, the save lands
        r = self._save({"manual_assets": [
            {"name": "House", "value": 300000, "kind": "property"},
            {"name": "Car", "value": 9000, "kind": "vehicle"}]}, self._view())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            sorted(x["name"] for x in self._stored()["manual_assets"]),
            ["Car", "House"])


if __name__ == "__main__":
    unittest.main()
