"""An AI endpoint URL can carry its own credential — a password in the
userinfo, or a key in the query string — and Settings masks both on every
display. The exports promise "credentials scrubbed", so the same mask
applies on the way out: the 'Your data' ZIP, the operator's portability
archive (settings and the frozen per-month budget copies alike) and the
budget snapshots the ZIP carries. On the way back in, a masked URL is never
saved as a live endpoint: it keeps the destination's own URL when that is
the one it masks, and is dropped otherwise."""

import io
import json
import unittest
import zipfile

from oikonome import tenant_export
from oikonome.sync import export, restore
from oikonome.sync.restore import scrub_config

from .util import _ensure_db, make_db, write_config

LLM_URL = "https://user:hunter2pw@llm.example.test/v1?key=abc123secret"
BACKENDS = [{"id": "b1", "name": "Proxy",
             "url": "https://proxy.example.test/?api_key=zzz999secret",
             "model": "m", "api_key": "enc:t1:deadbeef"}]
SECRETS = ("hunter2pw", "abc123secret", "zzz999secret", "deadbeef")


def _cfg():
    return {"llm_url": LLM_URL, "llm_backends": [dict(b) for b in BACKENDS],
            "food_monthly": 400}


class ScrubConfigUrlTests(unittest.TestCase):
    def test_scrub_masks_credentials_inside_endpoint_urls(self):
        out = json.dumps(scrub_config(_cfg()))
        for s in SECRETS:
            self.assertNotIn(s, out)
        self.assertIn("llm.example.test", out)
        self.assertIn("proxy.example.test", out)

    def test_urls_without_credentials_pass_unchanged(self):
        cfg = {"llm_url": "http://llm.lan:8000/v1",
               "llm_backends": [{"id": "b1", "url": "http://llm.lan:9000"}]}
        self.assertEqual(scrub_config(cfg), cfg)


class ExportArchivesCarryNoUrlCredentialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, **_cfg())
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS tid"
        ).fetchone()["tid"]

    def tearDown(self):
        self.conn.close()

    def test_your_data_zip_settings_hold_no_url_credential(self):
        data = export.build_zip(self.conn)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            body = z.read("tenant_settings.csv").decode()
        for s in SECRETS:
            self.assertNotIn(s, body)

    def test_portability_archive_holds_no_url_credential(self):
        """Settings AND a budget snapshot frozen before the mask existed
        (its config copy still holding the live URL)."""
        from oikonome.engine.compat import jsonb
        self.conn.execute(
            "INSERT INTO budget_snapshots (year, month, config, bills)"
            " VALUES (2026, 1, %s, NULL)", (jsonb(_cfg()),))
        data = tenant_export.build_archive(self.tid, operator="test",
                                           consented=True)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            body = "".join(z.read(n).decode(errors="replace")
                           for n in z.namelist())
        for s in SECRETS:
            self.assertNotIn(s, body)


class RestoreNeverSavesAMaskedUrlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def _settings_zip(self, cfg):
        import csv
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=["config"])
        w.writeheader()
        w.writerow({"config": json.dumps(cfg)})
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("tenant_settings.csv", s.getvalue())
        return buf.getvalue()

    def _config(self, conn):
        return conn.execute(
            "SELECT config FROM tenant_settings").fetchone()["config"]

    def test_restore_onto_itself_keeps_the_live_urls(self):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn, **_cfg())
        restore.restore_zip(conn, self._settings_zip(scrub_config(_cfg())))
        cfg = self._config(conn)
        self.assertEqual(cfg["llm_url"], LLM_URL)
        self.assertEqual(cfg["llm_backends"][0]["url"], BACKENDS[0]["url"])

    def test_restore_elsewhere_never_saves_the_mask(self):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn, llm_url="http://other.lan:8000")
        restore.restore_zip(conn, self._settings_zip(scrub_config(_cfg())))
        cfg = self._config(conn)
        self.assertEqual(cfg["llm_url"], "http://other.lan:8000")
        self.assertNotIn("***", json.dumps(cfg.get("llm_backends")))


if __name__ == "__main__":
    unittest.main()
