"""Exports promise that credentials never travel. `llm_backends` stores
each backend's (encrypted) api_key inside a LIST of dicts — a scrub that
only walks nested dicts ships every backend key in both the user-facing
'Your data' ZIP and the operator tenant-data archive. Secret-named keys
must be dropped inside list elements too, while the non-secret backend
fields (id/name/url/model) and savings-goal `tokens` (merchant match
words, user data — the reason lists were historically left alone) stay
intact."""

import io
import json
import unittest
import zipfile

from oikonome import tenant_export
from oikonome.sync.restore import scrub_config

from .util import _ensure_db, make_db, write_config

_BACKENDS = [{"id": "b1", "name": "My GPU box", "url": "http://llm.lan:8000",
              "model": "qwen3:4b", "api_key": "enc:t1:deadbeef"},
             {"id": "b2", "name": "Cloud", "url": "https://api.example.com",
              "model": "big-model", "api_key": "enc:t1:cafef00d"}]


class ScrubConfigListTests(unittest.TestCase):

    def test_api_key_dropped_inside_list_of_dicts(self):
        out = scrub_config({"llm_backends": [dict(b) for b in _BACKENDS]})
        self.assertIn("llm_backends", out)
        self.assertEqual(len(out["llm_backends"]), 2)
        for ent, orig in zip(out["llm_backends"], _BACKENDS):
            self.assertNotIn("api_key", ent)
            self.assertEqual(ent["url"], orig["url"])
            self.assertEqual(ent["model"], orig["model"])
            self.assertEqual(ent["name"], orig["name"])
            self.assertEqual(ent["id"], orig["id"])

    def test_extra_body_dropped_per_backend_and_top_level(self):
        """extra_body is arbitrary JSON merged into every request to the
        backend — an auth-in-body vendor's credential lives there. It must
        not ride an export, whether per-backend inside llm_backends or as
        the legacy top-level twin."""
        out = scrub_config({
            "llm_backends": [
                {"id": "b1", "url": "http://llm.lan:8000", "model": "m",
                 "extra_body": '{"organization": "org-secret-77"}'}],
            "llm_extra_body": '{"auth_in_body": "shh"}'})
        self.assertNotIn("llm_extra_body", out)
        self.assertNotIn("extra_body", out["llm_backends"][0])
        self.assertEqual(out["llm_backends"][0]["url"], "http://llm.lan:8000")

    def test_savings_goal_tokens_still_travel(self):
        goals = [{"name": "Car", "target": 20000,
                  "tokens": ["northwind", "transfer"]}]
        out = scrub_config({"savings_goals": goals})
        self.assertEqual(out["savings_goals"], goals)

    def test_secret_keys_dropped_in_deeper_list_nesting(self):
        out = scrub_config(
            {"outer": [{"inner": [{"password": "x", "keep": 1}]}]})
        self.assertEqual(out, {"outer": [{"inner": [{"keep": 1}]}]})


class TenantExportArchiveTests(unittest.TestCase):

    def test_archive_settings_carry_no_backend_api_key(self):
        _ensure_db()
        conn = make_db()
        try:
            write_config(conn, llm_backends=[dict(b) for b in _BACKENDS])
            tid = conn.execute(
                "SELECT current_setting('app.tenant_id') AS tid"
            ).fetchone()["tid"]
        finally:
            conn.close()
        data = tenant_export.build_archive(tid, operator="test",
                                           consented=True)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            rows = json.loads(z.read("data/tenant_settings.json"))
        self.assertEqual(len(rows), 1)
        cfg = rows[0]["config"]
        if isinstance(cfg, str):
            cfg = json.loads(cfg)
        backends = cfg.get("llm_backends") or []
        self.assertEqual(len(backends), 2)
        for ent in backends:
            self.assertNotIn("api_key", ent)
            self.assertTrue(ent.get("url"))
        # belt: the ciphertext appears NOWHERE in the whole member
        self.assertNotIn("deadbeef", json.dumps(rows))


if __name__ == "__main__":
    unittest.main()
