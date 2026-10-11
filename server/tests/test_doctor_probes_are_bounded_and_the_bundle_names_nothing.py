"""Doctor's outbound probes and its public bundle.

The probes reach servers outside the database — a household's AI
endpoint, an Ollama service — while a person waits on the page. The probed
server decides how fast it answers, so each probe is bounded as a whole,
not per read, and none runs while the request holds a pooled database
connection: one slow endpoint must not hold a slot every other household
needs. The Ollama row probes the address actually configured.

The diagnostic bundle is meant to be attached to a public issue, so it
names no host (base URL, mail relay, AI endpoint), no bank and no label the
household typed, and says that it withholds them. The signed-in page keeps
the names.
"""

import json
import os
import threading
import time
import unittest
import uuid
from unittest import mock

import httpx

from oikonome.db import tenancy
from oikonome.web import doctor

from .util import _ensure_db, write_config


def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OIKONOME_LLM", "OIKONOME_SMTP"))
           and k not in ("OIKONOME_HOSTED", "OIKONOME_BASE_URL")}
    env.update(extra)
    return mock.patch.dict(os.environ, env, clear=True)


class _Tenant(unittest.TestCase):
    def setUp(self):
        _ensure_db()
        admin = tenancy.admin_connect()
        try:
            self.tid = str(tenancy.create_tenant(
                admin, f"doc-{uuid.uuid4().hex[:8]}"))
        finally:
            admin.close()
        self.conn = tenancy.tenant_connect(self.tid)

    def tearDown(self):
        self.conn.close()


class ProbeBoundsTests(_Tenant):
    def test_no_probe_runs_while_the_connection_is_held(self):
        write_config(self.conn, llm_url="http://127.0.0.1:18101",
                     llm_model="m")
        real = tenancy.tenant_connect
        handed = []

        def connect(tid):
            c = real(tid)
            handed.append(c)
            return c
        held_during_probe = []

        def probe(url, transport=None):
            held_during_probe.append(
                any(not getattr(c, "_returned", c.closed) for c in handed))
            return {"data": [{"id": "m"}]}
        with _clean_env(), \
                mock.patch.object(tenancy, "tenant_connect", connect), \
                mock.patch.object(doctor, "_probe_json", probe):
            rows = doctor.checks(self.tid)
        self.assertTrue(held_during_probe)
        self.assertEqual(set(held_during_probe), {False})
        model = next(r for r in rows if r["name"] == "model")
        self.assertTrue(model["ok"], model)

    def test_a_trickling_answer_is_cut_off_at_the_deadline(self):
        def drip():
            yield b'{"data": ['
            for _ in range(50):
                time.sleep(0.1)
                yield b" "
        real = httpx.Client

        def client(*a, **k):
            k["transport"] = httpx.MockTransport(
                lambda req: httpx.Response(200, content=drip()))
            return real(*a, **k)
        t0 = time.monotonic()
        with mock.patch("httpx.Client", client), \
                mock.patch.object(doctor, "PROBE_DEADLINE", 0.3):
            with self.assertRaises(TimeoutError):
                doctor._probe_json("http://127.0.0.1:18102/v1/models")
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_trickling_headers_are_cut_off_at_the_deadline(self):
        """A server that sends its status line and headers a byte at a
        time never trips the per-read timeout; the deadline still ends
        the probe."""
        from .test_outbound_integrations import _Trickle
        srv = _Trickle(interval=0.2, life=8.0)
        out: dict = {}

        def probe():
            try:
                doctor._probe_json(f"http://127.0.0.1:{srv.port}/v1/models")
            except Exception as e:                # noqa: BLE001
                out["e"] = e
        t0 = time.monotonic()
        with mock.patch.object(doctor, "PROBE_DEADLINE", 1.0):
            th = threading.Thread(target=probe, daemon=True)
            th.start()
            th.join(5.0)
        self.assertFalse(th.is_alive(), "the probe outlived its deadline")
        self.assertLess(time.monotonic() - t0, 4.0)
        self.assertIn("Timeout", type(out.get("e")).__name__)

    def test_the_ollama_row_probes_the_configured_address(self):
        seen = []

        def probe(url, transport=None):
            seen.append(url)
            return {"version": "0.9"}
        with _clean_env(OIKONOME_LLM_URL="http://ollama.lan.example:11434"), \
                mock.patch.object(doctor, "_probe_json", probe):
            rows = doctor.checks(self.tid)
        self.assertIn("http://ollama.lan.example:11434/api/version", seen)
        self.assertNotIn("http://ollama:11434/api/version", seen)
        row = next(r for r in rows if r["name"] == "ollama (LLM)")
        self.assertTrue(row["ok"])

    def test_a_households_own_endpoint_is_not_probed_as_the_service(self):
        write_config(self.conn, llm_url="http://127.0.0.1:18103/ollama",
                     llm_model="m")
        seen = []

        def probe(url, transport=None):
            seen.append(url)
            return {"data": [{"id": "m"}]}
        with _clean_env(), mock.patch.object(doctor, "_probe_json", probe):
            rows = doctor.checks(self.tid)
        self.assertFalse(any(u.endswith("/api/version") for u in seen), seen)
        self.assertFalse(any(r["name"] == "ollama (LLM)" for r in rows))


class BundleNamesNothingTests(_Tenant):
    BASE = "https://quietfox.tail1a2b.ts.net"
    SMTP = "mail.lantern-home.lan"
    LLM = "http://10.20.30.40:11434"
    BANK = "Bridgeport Federal Credit Union"
    LABEL = "Joint checking Robin and Ash"
    COLLECTOR = "Grandma Ivy pension"

    def setUp(self):
        super().setUp()
        write_config(self.conn, llm_url=self.LLM, llm_model="m-one")
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, "
            "access_token, status) VALUES ('doc-p','plaid',%s,'t','ok')",
            (self.BANK,))
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name) "
            "VALUES ('doc-f','csv',%s)", (self.LABEL,))
        self.conn.execute(
            "INSERT INTO script_heartbeats (source, label, expected_hours) "
            "VALUES ('doc-src', %s, 24)", (self.COLLECTOR,))

    def _run(self, fn):
        with _clean_env(OIKONOME_BASE_URL=self.BASE,
                        OIKONOME_SMTP_HOST=self.SMTP), \
                mock.patch.object(doctor, "_probe_json",
                                  lambda url, transport=None:
                                  {"data": [{"id": "m-one"}]}):
            return fn(self.tid)

    def test_the_bundle_withholds_hosts_banks_and_labels(self):
        b = self._run(doctor.bundle)
        text = json.dumps(b, default=str)
        for secret in ("quietfox", "tail1a2b", "lantern-home",
                       "10.20.30.40", "Bridgeport", "Robin and Ash",
                       "Grandma Ivy"):
            self.assertNotIn(secret, text)
        self.assertIn("withheld", b["redacted"])
        names = [c["name"] for c in b["checks"]]
        self.assertIn("Plaid connection 1", names)

    def test_the_signed_in_page_keeps_the_names(self):
        text = json.dumps(self._run(doctor.checks), default=str)
        for name in ("quietfox", "lantern-home", "10.20.30.40",
                     "Bridgeport", "Robin and Ash", "Grandma Ivy"):
            self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main()
