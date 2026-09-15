"""Community scripts (community-scripts/) — framework checks.

The scripts are unsupported-by-design examples, but the repo still proves
two things about them: (a) every script folder follows the documented
convention (script.toml manifest with the required keys), and (b) each
example actually runs end-to-end offline — pull against its synthetic
fixture, then push --dry-run — with correct payload shape, ids and sign
conventions, and (where the door is a product parser) a fixture the
product side actually recognizes. No network, no credentials, no
database."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO / "community-scripts"
COINBASE = SCRIPTS_DIR / "coinbase"
SCRIPT = COINBASE / "coinbase_sync.py"
FIXTURE = COINBASE / "example-fixture.json"

MANIFEST_KEYS = {"name", "description", "source-kind", "doors",
                 "schedule-suggestion"}


def run_script(script: Path, *args: str,
               env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(script), *args],
                          capture_output=True, text=True, timeout=60,
                          env={**os.environ, **(env or {})} if env else None)


def run(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return run_script(SCRIPT, *args)


class TestConvention(unittest.TestCase):
    def test_every_script_folder_follows_the_convention(self):
        folders = [d for d in SCRIPTS_DIR.iterdir() if d.is_dir()]
        self.assertTrue(folders)
        for d in folders:
            with self.subTest(script=d.name):
                self.assertTrue((d / "README.md").is_file(),
                                f"{d.name}: README.md required")
                manifest = d / "script.toml"
                if not manifest.exists():
                    # stubs are README-only by design
                    self.assertIn("stub", (d / "README.md").read_text()[:200])
                    continue
                m = tomllib.loads(manifest.read_text())
                self.assertEqual(MANIFEST_KEYS, set(m),
                                 f"{d.name}: manifest keys")
                self.assertEqual(m["name"], d.name)
                self.assertIsInstance(m["doors"], list)

    def test_docs_page_exists_and_pointer_survives(self):
        self.assertIn("community script",
                      (REPO / "docs" / "community-scripts.md")
                      .read_text().lower())
        self.assertIn("community-scripts.md",
                      (REPO / "docs" / "collectors.md").read_text())


class TestCoinbaseExample(unittest.TestCase):
    def _pull(self, out: Path) -> dict:
        p = run("pull", "--fixture", str(FIXTURE), "--label", "example",
                "--out", str(out))
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(out.read_text())

    def test_pull_fixture_payload_shape(self):
        with tempfile.TemporaryDirectory() as td:
            payload = self._pull(Path(td) / "payload.json")

        self.assertEqual(payload["source"], "coinbase")
        self.assertEqual(payload["prefix"], "coinbase:")
        self.assertEqual([i["id"] for i in payload["items"]],
                         ["coinbase-example"])

        (acct,) = payload["accounts"]
        self.assertEqual(acct["id"], "coinbase-example")
        self.assertEqual((acct["type"], acct["subtype"]),
                         ("investment", "crypto"))
        # 0.5 BTC * 60000 spot + 250 fiat USD at par; zero-ETH wallet skipped
        self.assertEqual(acct["balance_current"], 30250.00)

        txns = {t["id"]: t for t in payload["transactions"]}
        self.assertEqual(len(txns), 4)  # zero-balance wallet history included
        buy = txns["coinbase:22222222-aaaa-4bbb-8ccc-000000000001"]
        self.assertEqual(buy["amount"], -20000.00)  # native_amount verbatim
        self.assertEqual(buy["category_primary"], "TRANSFER_IN")
        self.assertEqual(buy["category_detailed"], "COINBASE_BUY")
        self.assertEqual(buy["date"], "2026-01-15")
        self.assertEqual(buy["pending"], 0)
        self.assertEqual(buy["raw"]["type"], "buy")  # raw travels for P/L
        dep = txns["coinbase:22222222-aaaa-4bbb-8ccc-000000000002"]
        self.assertEqual(dep["category_primary"], "TRANSFER_OUT")
        send = txns["coinbase:22222222-aaaa-4bbb-8ccc-000000000004"]
        self.assertEqual(send["pending"], 1)
        for t in txns.values():  # every id prefix-owned => stale sweep safe
            self.assertTrue(t["id"].startswith("coinbase:"))
            self.assertEqual(t["account_id"], "coinbase-example")

    def test_push_dry_run(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "payload.json"
            self._pull(out)
            p = run("push", "--in", str(out), "--dry-run")
            self.assertEqual(p.returncode, 0, p.stderr)
            summary = json.loads(p.stdout)
        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["transactions"], 4)
        self.assertEqual(summary["accounts"], {"coinbase-example": 30250.00})

    def test_missing_spot_price_aborts_pull(self):
        fixture = json.loads(FIXTURE.read_text())
        fixture["spot"] = {}  # BTC held but unpriced -> must refuse
        with tempfile.TemporaryDirectory() as td:
            fx = Path(td) / "fixture.json"
            fx.write_text(json.dumps(fixture))
            p = run("pull", "--fixture", str(fx), "--out",
                    str(Path(td) / "payload.json"))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no spot price", p.stderr)

    def test_bridge_code_delegates_to_the_product_door(self):
        """The bridge must not carry its own upsert/prune copy — a
        duplicated prune drifts from the door (min/max window vs
        per-date-set). It delegates to coinbase_push.import_payload."""
        src = SCRIPT.read_text()
        self.assertIn("from oikonome.sync import coinbase_push", src)
        self.assertIn("coinbase_push.import_payload(conn, payload)", src)
        # no bridge-local restatement logic left behind
        self.assertNotIn("UPDATE transactions SET removed=1", src)
        self.assertNotIn("upsert_transactions", src)


class _Door(BaseHTTPRequestHandler):
    """Stands in for an instance's import endpoint. Records who called."""

    def do_POST(self):                                 # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        self.server.calls.append(
            (self.path, self.headers.get("Authorization", "")))
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):                         # quiet
        pass


class TestMirrorTargets(unittest.TestCase):
    """`targets` in the credentials file must actually deliver.

    The docs page tells an operator to add a second instance there and
    promises each one is pushed independently. An example that parses the
    list and never uses it gives that operator no error and no second push
    — the mirror silently receives nothing, which looks exactly like a
    mirror that is working."""

    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Door)
        self.srv.calls = []
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _creds(self, td: Path) -> dict:
        f = td / "credentials.json"
        f.write_text(json.dumps({
            "url": self.url, "api_token": "oik_primary",
            "targets": [{"name": "staging", "url": self.url,
                         "api_token": "oik_mirror"}]}))
        return {"OIKONOME_CREDENTIALS": str(f)}

    def _both_doors_saw_it(self, endpoint: str):
        self.assertEqual([c[0] for c in self.srv.calls],
                         [endpoint, endpoint])
        self.assertEqual([c[1] for c in self.srv.calls],
                         ["Bearer oik_primary", "Bearer oik_mirror"])

    def test_coinbase_pushes_the_primary_then_the_mirror(self):
        script = SCRIPTS_DIR / "coinbase" / "coinbase_sync.py"
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            env = self._creds(td)
            out = td / "payload.json"
            p = run_script(script, "pull", "--fixture",
                           str(SCRIPTS_DIR / "coinbase" /
                               "example-fixture.json"), "--out", str(out))
            self.assertEqual(p.returncode, 0, p.stderr)
            p = run_script(script, "push", "--in", str(out), env=env)
            self.assertEqual(p.returncode, 0, p.stderr)
        self._both_doors_saw_it("/api/import/coinbase")
        self.assertIn("staging", p.stdout)

    def test_a_mirror_that_is_down_never_fails_the_collection(self):
        """A staging copy mid-deploy must not make a nightly timer report
        that the real collection failed — but it must still be said."""
        script = SCRIPTS_DIR / "coinbase" / "coinbase_sync.py"
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            f = td / "credentials.json"
            f.write_text(json.dumps({
                "url": self.url, "api_token": "oik_primary",
                # a port nothing is listening on
                "targets": [{"name": "staging", "url": "http://127.0.0.1:1",
                             "api_token": "oik_mirror"}]}))
            out = td / "payload.json"
            p = run_script(script, "pull", "--fixture",
                           str(SCRIPTS_DIR / "coinbase" /
                               "example-fixture.json"), "--out", str(out))
            self.assertEqual(p.returncode, 0, p.stderr)
            p = run_script(script, "push", "--in", str(out),
                           env={"OIKONOME_CREDENTIALS": str(f)})
        self.assertEqual(p.returncode, 0, "the primary decides the exit code")
        self.assertEqual(len(self.srv.calls), 1)
        self.assertIn("staging", p.stderr)


if __name__ == "__main__":
    unittest.main()
