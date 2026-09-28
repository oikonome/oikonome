"""The restore-ZIP SSRF door and the LLM's missing use-time re-check.

Restore door — Settings and the .oikx bundle import netguard
llm_url/smtp_host, and `restore.restore_zip` applies the same guard to the
export's tenant config, or a crafted export ZIP would walk past both.

Use time — even a value that somehow lands in config (an old restore, a door
nobody has thought of yet) must never be FETCHED. SimpleFIN re-guards every
fetch(); the LLM client does the same — but only for
TENANT-sourced backends. The operator's env/bundled backend is deliberately
exempt: the standard install's default is http://ollama:11434, a private
compose hostname that the netguard hosted policy would (correctly, for
tenant data) refuse.
"""

import csv
import io
import json
import os
import unittest
import uuid
import zipfile
from unittest import mock

import httpx

from oikonome.engine import budget, llm_categorize, receipts
from oikonome.engine.compat import as_date, jsonb
from oikonome.sync import restore

from .util import TODAY, add_txn, make_db, write_config

METADATA = "http://169.254.169.254/latest/meta-data/"
INTERNAL = "http://10.1.2.3:6379/"


def _export_zip(config: dict | None = None, txns: list[dict] | None = None
                ) -> bytes:
    """A minimal export ZIP the way /export writes one (CSV per table,
    JSON in the config cell)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if config is not None:
            s = io.StringIO()
            w = csv.DictWriter(s, fieldnames=["config"])
            w.writeheader()
            w.writerow({"config": json.dumps(config)})
            z.writestr("tenant_settings.csv", s.getvalue())
        if txns:
            s = io.StringIO()
            w = csv.DictWriter(
                s, fieldnames=["id", "account_id", "date", "amount", "name"])
            w.writeheader()
            for t in txns:
                w.writerow(t)
            z.writestr("transactions.csv", s.getvalue())
    return buf.getvalue()


class RestoreConfigGuardTests(unittest.TestCase):
    """Restore applies the SAME netguard policy as Settings / .oikx."""

    def setUp(self):
        os.environ.pop("OIKONOME_HOSTED", None)
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_llm_url_at_metadata_never_lands(self):
        # what matters is that the planted URL cannot end up in stored
        # config, where the next categorize run would fetch it. Refusing
        # the ARCHIVE was a second, separate decision, and the wrong one:
        # an endpoint is instance-local, so a legitimate export from a
        # self-hosted box looks the same as this one from here.
        data = _export_zip(config={"llm_url": METADATA, "llm_model": "x"})
        counts = restore.restore_zip(self.conn, data)
        self.assertNotIn("llm_url", budget.load_config(self.conn))
        self.assertIn("AI endpoint", " ".join(counts.get("_notes") or []))

    def test_smtp_host_internal_never_lands_when_hosted(self):
        data = _export_zip(config={"smtp_host": "10.1.2.3"})
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            counts = restore.restore_zip(self.conn, data)
        self.assertNotIn("smtp_host", budget.load_config(self.conn))
        self.assertIn("mail server", " ".join(counts.get("_notes") or []))

    def test_the_ledger_still_lands_when_an_endpoint_is_dropped(self):
        # dropping an endpoint must not cost the person their data: an
        # export must not be refused entirely over one unreachable AI
        # backend
        data = _export_zip(
            config={"llm_url": METADATA, "llm_model": "x"},
            txns=[{"id": "keep-1", "account_id": "chk",
                   "date": "2026-07-01", "amount": "12.5", "name": "X"}])
        restore.restore_zip(self.conn, data)
        self.assertIsNotNone(self.conn.execute(
            "SELECT id FROM transactions WHERE id='keep-1'").fetchone())

    def test_a_real_rejection_still_lands_no_rows(self):
        # the conn is autocommit — validation must run BEFORE any insert,
        # or a rejected ZIP still leaves its transactions behind. A
        # MALFORMED config is still a rejection (a corrupt file, not an
        # unreachable endpoint), so the atomicity it protects is unchanged.
        data = _export_zip(
            config={"llm_backends": "nonsense"},
            txns=[{"id": "evil-1", "account_id": "chk",
                   "date": "2026-07-01", "amount": "12.5", "name": "X"}])
        with self.assertRaises(ValueError):
            restore.restore_zip(self.conn, data)
        self.assertIsNone(self.conn.execute(
            "SELECT id FROM transactions WHERE id='evil-1'").fetchone())

    def test_happy_path_config_still_merges_and_keeps_secrets(self):
        # destination's credential-shaped keys survive; the ZIP's benign
        # config lands — the guard must not break real migration
        cfg = budget.load_config(self.conn)
        cfg["llm_api_key"] = "enc:keep-me"
        budget.save_config(self.conn, cfg)
        data = _export_zip(
            config={"food_monthly": 750, "llm_url": "https://api.openai.com",
                    "llm_model": "gpt-4o-mini"},
            txns=[{"id": "ok-1", "account_id": "chk",
                   "date": "2026-07-01", "amount": "12.5", "name": "OK"}])
        counts = restore.restore_zip(self.conn, data)
        self.assertEqual(counts.get("settings"), 1)
        self.assertEqual(counts.get("transactions"), 1)
        got = budget.load_config(self.conn)
        self.assertEqual(got["food_monthly"], 750)
        self.assertEqual(got["llm_url"], "https://api.openai.com")
        self.assertEqual(got["llm_api_key"], "enc:keep-me")

    def test_local_llm_url_still_restores_on_self_host(self):
        # a local LLM on a private address is a FIRST-CLASS self-host setup
        data = _export_zip(config={"llm_url": "http://192.168.1.50:11434",
                                   "llm_model": "qwen"})
        counts = restore.restore_zip(self.conn, data)
        self.assertEqual(counts.get("settings"), 1)
        self.assertEqual(budget.load_config(self.conn)["llm_url"],
                         "http://192.168.1.50:11434")

    def test_crafted_secret_keys_are_stripped(self):
        # a legit export never carries secret-shaped keys (the export scrubs
        # them) — any present in a ZIP are crafted and must not land
        data = _export_zip(config={"food_monthly": 600,
                                   "simplefin_access_url": METADATA,
                                   "llm_api_key": "enc:attacker"})
        counts = restore.restore_zip(self.conn, data)
        self.assertEqual(counts.get("settings"), 1)
        got = budget.load_config(self.conn)
        self.assertEqual(got["food_monthly"], 600)
        self.assertNotIn("simplefin_access_url", got)
        self.assertNotIn("llm_api_key", got)


def _capture_transport(requests, reply=None):
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps(reply or {"1": "TRAVEL"})}}]})
    return httpx.MockTransport(handler)


def _add_pending_merchant(conn):
    conn.execute(
        """INSERT INTO transactions (id, account_id, date, amount, name,
               merchant_name, category_primary, pending, removed, raw)
           VALUES (%s,'card',%s,42,'Mystery Shop','Mystery Shop',NULL,0,0,%s)""",
        (f"llm-{uuid.uuid4().hex[:8]}", as_date("2026-07-01"), jsonb({})))


class LlmUseTimeGuardTests(unittest.TestCase):
    """Tenant-config backends are re-guarded at REQUEST time;
    operator env backends are not."""

    def setUp(self):
        os.environ.pop("OIKONOME_HOSTED", None)
        self.conn = make_db()
        write_config(self.conn)
        _add_pending_merchant(self.conn)

    def tearDown(self):
        self.conn.close()

    def _set_llm(self, url):
        cfg = budget.load_config(self.conn)
        cfg["llm_url"], cfg["llm_model"] = url, "m"
        budget.save_config(self.conn, cfg)

    def test_poisoned_tenant_config_is_never_fetched(self):
        # value written straight into config — the shape a restore (or any
        # door that skips the write-time check) could leave behind
        self._set_llm(METADATA)
        reqs = []
        stats = llm_categorize.run(self.conn,
                                   transport=_capture_transport(reqs))
        self.assertEqual(reqs, [])                 # no request went out
        self.assertGreaterEqual(stats["unparseable"], 1)

    def test_tenant_private_url_blocked_when_hosted(self):
        self._set_llm(INTERNAL)
        reqs = []
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            llm_categorize.run(self.conn, transport=_capture_transport(reqs))
        self.assertEqual(reqs, [])

    def test_tenant_loopback_llm_still_works_on_self_host(self):
        # local mock/real LLM servers bind 127.0.0.1 — first-class self-host
        self._set_llm("http://127.0.0.1:8080")
        reqs = []
        stats = llm_categorize.run(self.conn,
                                   transport=_capture_transport(reqs))
        self.assertEqual(len(reqs), 1)
        self.assertEqual(stats["merchants_classified"], 1)

    def test_env_bundled_backend_exempt_even_when_hosted(self):
        # the standard install's default is http://ollama:11434 — a private
        # compose hostname the hosted policy would refuse if it were tenant
        # data; the operator's own env backend must keep working
        reqs = []
        env = {"OIKONOME_HOSTED": "1", "OIKONOME_LLM_URL": "http://ollama:11434",
               "OIKONOME_LLM_MODEL": "qwen2.5:1.5b"}
        with mock.patch.dict(os.environ, env):
            stats = llm_categorize.run(self.conn,
                                       transport=_capture_transport(reqs))
        self.assertEqual(len(reqs), 1)
        self.assertEqual(stats["merchants_classified"], 1)

    def test_receipt_vision_path_is_guarded_too(self):
        # receipts build their backend from the same tenant config and call
        # the same _chat — provenance must travel with the dict
        self._set_llm(METADATA)
        txn = add_txn(self.conn, TODAY, 63.00, "BIGBOX WHOLESALE")
        rid = receipts.add(self.conn, txn, b"\x89PNG...", "image/png")
        reqs = []
        out = receipts.parse_one(self.conn, rid,
                                 transport=_capture_transport(reqs))
        self.assertEqual(out["status"], "failed")
        self.assertEqual(reqs, [])


if __name__ == "__main__":
    unittest.main()
