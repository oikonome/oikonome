"""Pointing the household's AI tasks somewhere new tells the owner.

Every transaction the categorize pass reads, every receipt photo and every
assistant question goes to the endpoint the settings name. A stolen session
that repoints it receives the household's money from then on, and nothing
on any page says it happened. So a save that changes where (or to which
model) any AI task's text goes sends the account a security notice, once
the save has committed. Only the owner may change these keys (members'
bodies drop them), so the acting account is the owner. A save that leaves
every task's destination as it was — the masked URL echoed back, an
unrelated key, the same backend list re-posted — sends none."""

import os
import unittest
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from .util import _ensure_db

PW = "correct-horse-battery"


def _env_without_llm():
    return {k: v for k, v in os.environ.items()
            if not k.startswith("OIKONOME_LLM") and k != "OIKONOME_HOSTED"}


class ChangingTheAiRouteSendsANotice(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod
        cls.client = TestClient(appmod.app)
        r = cls.client.post("/api/signup", data={
            "email": f"airoute-n-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        assert r.status_code == 200, r.text
        cls.client.post("/api/auth/elevate", json={"password": PW})

    def setUp(self):
        self.env = patch.dict(os.environ, _env_without_llm(), clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self._save({"llm_url": "", "llm_backends": [], "llm_roles": {}})
        self._save({"llm_url": "http://user:s3cret@10.0.0.5:11434/v1",
                    "llm_model": "m"})
        self.sent = []
        p = patch.object(self.appmod, "_notify_security_change",
                         lambda user, change: self.sent.append(change))
        p.start()
        self.addCleanup(p.stop)

    def _save(self, body, code=200):
        r = self.client.post("/api/settings", json=body)
        self.assertEqual(r.status_code, code, r.text)
        return r.json()

    def test_changing_the_endpoint_sends_one_notice(self):
        self._save({"llm_url": "http://10.0.0.6:11434/v1", "llm_model": "m"})
        self.assertEqual(self.sent, ["ai_route_changed"])

    def test_changing_the_model_sends_one_notice(self):
        self._save({"llm_url": "http://user:s3cret@10.0.0.5:11434/v1",
                    "llm_model": "other"})
        self.assertEqual(self.sent, ["ai_route_changed"])

    def test_echoing_the_masked_endpoint_back_sends_none(self):
        shown = self.client.get("/api/settings").json()["llm_url"]
        self.assertNotIn("s3cret", shown)
        self._save({"llm_url": shown, "llm_model": "m"})
        self.assertEqual(self.sent, [])

    def test_an_unrelated_save_sends_none(self):
        self._save({"food_monthly": 420})
        self.assertEqual(self.sent, [])

    def test_routing_a_task_to_a_new_backend_sends_one_notice(self):
        self._save({"llm_backends": [
            {"id": "far", "name": "Far", "url": "http://10.0.0.9:8000/v1",
             "model": "m"}], "llm_roles": {"assistant": "far"}})
        self.assertEqual(self.sent, ["ai_route_changed"])
        # re-posting the same list and routes moves nothing
        shown = self.client.get("/api/settings").json()
        self._save({"llm_backends": shown["llm_backends"],
                    "llm_roles": shown["llm_roles"]})
        self.assertEqual(self.sent, ["ai_route_changed"])

    def test_swapping_only_a_backends_key_sends_one_notice(self):
        """On a router service the key decides whose account receives and
        logs the prompts, so a new key on the same endpoint is a new
        destination; the masked echo of the stored key is not."""
        backends = [{"id": "far", "name": "Far",
                     "url": "http://10.0.0.9:8000/v1", "model": "m",
                     "api_key": "sk-invented-one"}]
        self._save({"llm_backends": backends,
                    "llm_roles": {"assistant": "far"}})
        self.sent.clear()
        shown = self.client.get("/api/settings").json()
        self.assertNotIn("api_key", shown["llm_backends"][0])
        self._save({"llm_backends": shown["llm_backends"],
                    "llm_roles": shown["llm_roles"]})
        self.assertEqual(self.sent, [])
        self._save({"llm_backends": [{**backends[0],
                                      "api_key": "sk-invented-two"}],
                    "llm_roles": {"assistant": "far"}})
        self.assertEqual(self.sent, ["ai_route_changed"])

    def test_swapping_only_the_endpoints_key_sends_one_notice(self):
        self._save({"llm_url": "http://user:s3cret@10.0.0.5:11434/v1",
                    "llm_model": "m", "llm_api_key": "sk-invented-three"})
        self.assertEqual(self.sent, ["ai_route_changed"])
        self._save({"llm_url": "http://user:s3cret@10.0.0.5:11434/v1",
                    "llm_model": "m", "llm_api_key": ""})
        self.assertEqual(self.sent, ["ai_route_changed"])

    def test_the_notice_names_no_endpoint(self):
        text = self.appmod._SECURITY_CHANGES["ai_route_changed"]
        self.assertNotIn("http", text)
        self.assertIn("ai_route_changed", self.appmod._KEEPS_OTHER_SIGN_INS)


if __name__ == "__main__":
    unittest.main()


class RestoringAnArchiveThatMovesTheAiRouteSendsANotice(unittest.TestCase):
    """A restore merges the archive's AI keys over the stored ones and
    carries the stored key onto an entry of the same id, so an archive
    edited to name another endpoint repoints the household as surely as a
    settings save does — and tells the owner the same way, once the
    restore has committed."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod
        cls.client = TestClient(appmod.app)
        cls.email = f"airoute-r-{uuid.uuid4().hex[:8]}@example.dev"
        r = cls.client.post("/api/signup", data={
            "email": cls.email, "password": PW})
        assert r.status_code == 200, r.text
        cls.client.post("/api/auth/elevate", json={"password": PW})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]

    def setUp(self):
        self.env = patch.dict(os.environ, _env_without_llm(), clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        r = self.client.post("/api/settings", json={
            "llm_url": "", "llm_backends": [
                {"id": "b1", "name": "Box", "url": "http://10.0.0.5:8000/v1",
                 "model": "m", "api_key": "sk-invented-four"}],
            "llm_roles": {"assistant": "b1"}})
        self.assertEqual(r.status_code, 200, r.text)
        self.sent = []
        p = patch.object(self.appmod, "_notify_security_change",
                         lambda user, change: self.sent.append(
                             (user.get("email"), change)))
        p.start()
        self.addCleanup(p.stop)

    def _restore_with(self, url: str):
        import csv
        import io
        import json
        import zipfile

        from oikonome.db import tenancy
        from oikonome.sync import restore
        cfg = {"llm_backends": [{"id": "b1", "name": "Box", "url": url,
                                 "model": "m"}],
               "llm_roles": {"assistant": "b1"}}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            s = io.StringIO()
            w = csv.DictWriter(s, fieldnames=["config"])
            w.writeheader()
            w.writerow({"config": json.dumps(cfg)})
            z.writestr("tenant_settings.csv", s.getvalue())
        conn = tenancy.tenant_connect(self.tid)
        try:
            restore.restore_zip(conn, buf.getvalue())
        finally:
            conn.close()

    def test_an_archive_naming_another_endpoint_under_the_same_id(self):
        self._restore_with("http://10.0.0.77:8000/v1")
        self.assertEqual(self.sent, [(self.email, "ai_route_restored")])

    def test_an_archive_with_the_same_route_sends_none(self):
        self._restore_with("http://10.0.0.5:8000/v1")
        self.assertEqual(self.sent, [])
