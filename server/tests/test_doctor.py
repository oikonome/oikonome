"""Doctor: sectioned telemetry — system rows, per-connection last-sync,
LLM health + merchant-map stats, data summary."""

import os
import unittest
from unittest.mock import patch

from oikonome.sync import base as sync_base
from oikonome.sync import simplefin
from oikonome.web import doctor

from .test_simplefin import _transport
from .util import make_db, write_config


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])

    def tearDown(self):
        self.conn.close()

    def _checks(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("OIKONOME_LLM")}
        with patch.dict(os.environ, env, clear=True):
            return doctor.checks(self.tid)

    def test_a_malformed_email_schedule_does_not_crash_the_page(self):
        """Settings are validated on the API write path, not on restore — a
        backup can plant a non-dict under email_schedule.daily, and .get on
        it must not 500 the whole Doctor page. A malformed truthy entry
        reads as configured-on so the health row still shows the corruption."""
        from oikonome.engine import budget
        for bad in (True, "yes", 7, [1]):
            cfg = budget.load_config(self.conn)
            cfg["email_schedule"] = {"daily": bad}
            budget.save_config(self.conn, cfg)
            rows = self._checks()               # must not raise
            names = {r["name"] for r in rows if r["section"] == "Jobs"}
            self.assertIn("daily email (worker)", names)

    def test_a_failed_first_query_hands_the_connection_back(self):
        """The tenant connection is a pool checkout: when the very first
        query fails, checks() must still close it, or every failing health
        check strands one pool slot until the pool is exhausted."""
        class _Broken:
            closed = False

            def execute(self, *a, **k):
                raise RuntimeError("relation does not exist")

            def close(self):
                self.closed = True

        broken = _Broken()
        with patch("oikonome.db.tenancy.tenant_connect",
                   return_value=broken):
            rows = doctor.checks(self.tid)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["ok"])
        self.assertTrue(broken.closed)

    def test_rows_are_sectioned(self):
        rows = self._checks()
        sections = {r["section"] for r in rows}
        for expected in ("System", "Connections", "Jobs",
                         "Smart categorization", "Email", "Security", "Data"):
            self.assertIn(expected, sections, f"missing section {expected}")
        for r in rows:
            self.assertIn("name", r)
            self.assertIn("detail", r)

    def test_system_rows_have_version_and_db_size(self):
        rows = {r["name"]: r for r in self._checks()
                if r["section"] == "System"}
        self.assertIn("version", rows)
        self.assertIn("database size", rows)
        self.assertTrue(rows["database size"]["ok"])

    def test_connection_rows_show_last_sync_per_bank(self):
        sync_base.upsert_item(self.conn, "sfin-main", "simplefin", "SimpleFIN",
                              "https://u:p@bridge.test/simplefin")
        simplefin.sync(self.conn, "sfin-main",
                       "https://u:p@bridge.test/simplefin",
                       transport=_transport())
        conn_rows = [r for r in self._checks()
                     if r["section"] == "Connections"]
        # per-bank children with fresh sync stamps; the healthy bridge is
        # deliberately hidden (its children carry the meaningful state)
        names = [r["name"] for r in conn_rows]
        self.assertTrue(any("Demo Bank" in n for n in names), names)
        self.assertFalse(any("bridge" in n.lower() for n in names), names)
        bank = next(r for r in conn_rows if "Demo Bank" in r["name"])
        self.assertTrue(bank["ok"])
        self.assertIn("last sync", bank["detail"])

    def test_llm_unconfigured_is_informational_not_failure(self):
        rows = [r for r in self._checks()
                if r["section"] == "Smart categorization"]
        llm = next(r for r in rows if r["name"] == "LLM")
        self.assertTrue(llm["ok"])
        self.assertIn("not configured", llm["detail"])
        # merchant-map stats row always present
        self.assertTrue(any(r["name"] == "merchant map" for r in rows))

    def test_bundle_carries_sectioned_checks(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("OIKONOME_LLM")}
        with patch.dict(os.environ, env, clear=True):
            b = doctor.bundle(self.tid)
        self.assertIn("oikonome_version", b)
        self.assertTrue(all("section" in c for c in b["checks"]))


# Doctor shows the pending LLM queue + per-service reachability;
# interactive imports never auto-watch collector heartbeats.
class LlmQueueAndServicesTests(unittest.TestCase):
    def setUp(self):
        from .util import make_db, write_config
        self.conn = make_db()
        write_config(self.conn)
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])

    def tearDown(self):
        self.conn.close()

    def _rows(self, section=None, name=None):
        from oikonome.web import doctor
        rows = doctor.checks(self.tid)
        if section:
            rows = [r for r in rows if r["section"] == section]
        if name:
            rows = [r for r in rows if r["name"] == name]
        return rows

    def test_services_section_present(self):
        names = {r["name"] for r in self._rows("Services")}
        self.assertIn("app", names)
        self.assertIn("postgres", names)
        self.assertIn("redis (job queue)", names)
        self.assertIn("worker", names)

    def test_receipt_queue_row_reflects_states(self):
        from .util import TODAY, add_txn
        from oikonome.engine import receipts
        empty = self._rows("Smart categorization", "receipt queue")
        self.assertTrue(empty and empty[0]["ok"])
        txn = add_txn(self.conn, TODAY, 10.0, "CORNER COFFEE", account="chk")
        rid = receipts.add(self.conn, txn, b"\x89PNG...", "image/png")
        self.conn.execute(
            "UPDATE receipts SET status='parsing', parsed_at=now() "
            "WHERE id=%s::uuid", (rid,))
        rid2 = receipts.add(self.conn, txn, b"\x89PNG...", "image/png")
        self.conn.execute(
            "UPDATE receipts SET status='failed', error='x' "
            "WHERE id=%s::uuid", (rid2,))
        row = self._rows("Smart categorization", "receipt queue")[0]
        self.assertFalse(row["ok"])                # failed present → warn
        self.assertTrue(row.get("busy"))           # parsing present → bar
        self.assertIn("1 parsing now", row["detail"])
        self.assertIn("1 failed", row["detail"])

    def test_live_categorize_progress(self):
        from oikonome.engine.compat import jsonb
        self.conn.execute(
            """INSERT INTO job_progress (id, state, progress)
               VALUES ('sync', 'running', %s)""",
            (jsonb({"categorize": {"done": 7, "total": 20}}),))
        row = self._rows("Smart categorization", "live categorize")[0]
        self.assertEqual(row["progress"], 35.0)
        self.assertIn("7/20", row["detail"])


class ImportAutoWatchTests(unittest.TestCase):
    def test_session_imports_never_watch_scripts_do(self):
        from .util import make_db
        from oikonome.sync import heartbeat
        conn = make_db()
        try:
            # simulate the UI-import context: auto-watch disabled
            tok = heartbeat.AUTO_WATCH.set(False)
            try:
                heartbeat.stamp(conn, "manual", 5, "Manual")
                conn.execute("UPDATE script_heartbeats SET last_push = "
                             "now() - interval '1 day' WHERE source='manual'")
                heartbeat.stamp(conn, "manual", 3, "Manual")
            finally:
                heartbeat.AUTO_WATCH.reset(tok)
            row = conn.execute(
                "SELECT expected_hours FROM script_heartbeats "
                "WHERE source='manual'").fetchone()
            self.assertIsNone(row["expected_hours"])   # never auto-watched
            # the same pattern from a script (default context) DOES watch
            heartbeat.stamp(conn, "acme-card", 5, "Acme Card")
            conn.execute("UPDATE script_heartbeats SET last_push = "
                         "now() - interval '1 day' WHERE source='acme-card'")
            heartbeat.stamp(conn, "acme-card", 3, "Acme Card")
            row = conn.execute(
                "SELECT expected_hours FROM script_heartbeats "
                "WHERE source='acme-card'").fetchone()
            self.assertEqual(row["expected_hours"], 24)
        finally:
            conn.close()

    def test_file_fed_items_never_warn_as_connections(self):
        # one-time historical imports (csv items, often status 'restored')
        # never sync — they collapse into one informational row
        from .util import make_db, write_config
        conn = make_db()
        try:
            write_config(conn)
            tid = str(conn.execute(
                "SELECT current_setting('app.tenant_id') AS t"
            ).fetchone()["t"])
            conn.execute(
                "INSERT INTO items (id, aggregator, institution_name, status) "
                "VALUES ('oldbank','csv','OldBank','restored'),"
                "       ('mint','csv','Mint (historical)','restored')")
            from oikonome.web import doctor
            rows = [r for r in doctor.checks(tid)
                    if r["section"] == "Connections"]
            file_rows = [r for r in rows if r["name"] == "file-fed sources"]
            self.assertEqual(len(file_rows), 1)
            self.assertTrue(file_rows[0]["ok"])
            self.assertIn("OldBank", file_rows[0]["detail"])
            self.assertFalse([r for r in rows
                              if "OldBank" in r["name"]
                              or "Mint" in r["name"]])
        finally:
            conn.close()


class RecipientInviteBaseUrlTests(unittest.TestCase):
    """An install that can send mail but has no pinned OIKONOME_BASE_URL
    cannot deliver a recipient invitation: the link would be Host-derived and
    forgeable, so it is refused and logged. Settings still says "invite sent",
    so nobody learns the person was never asked. Doctor names it."""

    def setUp(self):
        self.conn = make_db()
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])

    def tearDown(self):
        self.conn.close()

    def _rows(self, base_url: str):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("OIKONOME_LLM")}
        env["OIKONOME_BASE_URL"] = base_url
        with patch.dict(os.environ, env, clear=True), \
             patch("oikonome.web.report.resolve_smtp",
                   return_value={"configured": True, "host": "smtp.example",
                                 "port": 587, "user": "u",
                                 "from": "n@example"}):
            return doctor.checks(self.tid)

    def _hits(self, rows):
        return [r for r in rows if r["name"] == "recipient invitations"]

    def test_warns_when_smtp_works_but_base_url_is_unset(self):
        hit = self._hits(self._rows(""))
        self.assertTrue(hit, "no row about undeliverable recipient invites")
        self.assertFalse(hit[0]["ok"])

    def test_silent_once_a_base_url_is_pinned(self):
        self.assertEqual([], self._hits(self._rows("https://x.example")))


def _mock_client(handler):
    """An httpx.Client stand-in whose every request is answered by
    `handler` — whatever transport the code under test asked for."""
    import httpx as _httpx
    real = _httpx.Client

    def _make(*a, **k):
        k["transport"] = _httpx.MockTransport(handler)
        return real(*a, **k)
    return _make


class PerRoleBackendHealthTests(unittest.TestCase):
    """Doctor must probe every DISTINCT routed backend, not just the
    categorize role's. Otherwise, with vision routed at a second endpoint, a
    dead vision backend shows all green while every receipt and
    tax-document upload fails."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_a_dead_vision_backend_gets_its_own_red_row(self):
        write_config(
            self.conn,
            llm_backends=[
                {"id": "cat", "url": "http://127.0.0.1:18080",
                 "model": "m-cat"},
                {"id": "vis", "url": "http://127.0.0.1:18081",
                 "model": "m-vis"}],
            llm_roles={"categorize": "cat", "vision": "vis"})
        import httpx as _httpx

        def _handler(req):
            if req.url.port == 18080:       # the categorize backend is up
                return _httpx.Response(200, json={"data": [{"id": "m-cat"}]})
            raise _httpx.ConnectError("down", request=req)

        _Client = _mock_client(_handler)

        env = {k: v for k, v in os.environ.items()
               if not k.startswith("OIKONOME_LLM")}
        with patch.dict(os.environ, env, clear=True), \
             patch("httpx.Client", _Client):
            rows = doctor._llm_rows(self.conn)
        by_name = {r["name"]: r for r in rows}
        # one endpoint+health pair per backend, labeled with its role(s)
        self.assertIn("endpoint (categorize)", by_name, sorted(by_name))
        self.assertIn("endpoint (vision)", by_name, sorted(by_name))
        self.assertTrue(by_name["model (categorize)"]["ok"])
        self.assertFalse(by_name["model (vision)"]["ok"])

    def test_one_backend_for_everything_keeps_the_plain_row_names(self):
        write_config(
            self.conn,
            llm_backends=[{"id": "one", "url": "http://127.0.0.1:18080",
                           "model": "m-one"}],
            llm_roles={"categorize": "one", "assistant": "one",
                       "vision": "one"})
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("OIKONOME_LLM")}

        def _handler(req):
            import httpx as _h
            raise _h.ConnectError("down", request=req)

        _Client = _mock_client(_handler)

        with patch.dict(os.environ, env, clear=True), \
             patch("httpx.Client", _Client):
            rows = doctor._llm_rows(self.conn)
        by_name = {r["name"]: r for r in rows}
        self.assertIn("endpoint", by_name, sorted(by_name))
        self.assertIn("model", by_name, sorted(by_name))
        self.assertFalse(by_name["model"]["ok"])


class DoctorProbeExposureTests(unittest.TestCase):
    """The LLM probe runs whenever ANY member opens Doctor or builds a
    feedback bundle, against an endpoint the owner chose, and its rows are
    readable by every member. So the read is bounded, credentials in the
    URL are masked, and on hosted the operator's own backend is never
    described."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _rows(self, handler, **env_extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("OIKONOME_LLM")
               and k != "OIKONOME_HOSTED"}
        env.update(env_extra)
        with patch.dict(os.environ, env, clear=True), \
             patch("httpx.Client", _mock_client(handler)):
            return doctor._llm_rows(self.conn)

    def test_a_reply_that_inflates_past_the_cap_is_refused(self):
        """A small compressed body that decompresses to megabytes must be
        cut off at the reply cap, not buffered whole and parsed — otherwise
        an owner's server can make every Doctor load allocate whatever it
        likes."""
        import gzip
        import json

        import httpx as _httpx

        from oikonome.engine import llm_categorize as llm
        write_config(self.conn, llm_url="http://127.0.0.1:18090",
                     llm_model="m-one")
        big = json.dumps({"data": [{"id": "m-one"}],
                          "pad": "0" * (llm.MAX_RESPONSE_BYTES * 4)})
        packed = gzip.compress(big.encode())
        self.assertLess(len(packed), llm.MAX_RESPONSE_BYTES)

        def _handler(req):
            return _httpx.Response(
                200, content=packed,
                headers={"content-encoding": "gzip",
                         "content-type": "application/json"})
        rows = {r["name"]: r for r in self._rows(_handler)}
        self.assertFalse(rows["model"]["ok"], rows["model"])

    def test_the_probe_never_follows_a_redirect(self):
        """The vetted address is the only one probed — a 30x must not send
        the fetch somewhere the netguard never saw."""
        import httpx as _httpx
        write_config(self.conn, llm_url="http://127.0.0.1:18091",
                     llm_model="m-one")
        seen = []

        def _handler(req):
            seen.append(str(req.url))
            if req.url.port == 18091:
                return _httpx.Response(
                    302, headers={"location": "http://127.0.0.1:18092/v1/models"})
            return _httpx.Response(200, json={"data": [{"id": "m-one"}]})
        rows = {r["name"]: r for r in self._rows(_handler)}
        self.assertEqual(1, len(seen), seen)
        self.assertFalse(rows["model"]["ok"])

    def test_a_credential_in_the_endpoint_url_is_masked(self):
        import httpx as _httpx
        write_config(self.conn,
                     llm_url="http://someone:hunter2-invented@127.0.0.1:18093",
                     llm_model="m-one")
        rows = self._rows(lambda req: _httpx.Response(
            200, json={"data": [{"id": "m-one"}]}))
        text = " ".join(r["detail"] for r in rows)
        self.assertNotIn("hunter2-invented", text)
        self.assertIn("127.0.0.1:18093", text)

    def test_hosted_never_describes_the_operators_backend(self):
        """On hosted a household with no AI of its own runs on the
        operator's env backend; its URL, model and model list are the
        operator's infrastructure, withheld by Settings and so here too."""
        import httpx as _httpx
        rows = self._rows(
            lambda req: _httpx.Response(200, json={"data": [
                {"id": "operator-other-model"}]}),
            OIKONOME_HOSTED="1",
            OIKONOME_LLM_URL="http://operator-llm.invalid:11434",
            OIKONOME_LLM_MODEL="operator-model")
        text = " ".join(r["detail"] for r in rows)
        self.assertTrue(rows)
        for leaked in ("operator-llm", "operator-model",
                       "operator-other-model"):
            self.assertNotIn(leaked, text)

    def test_self_host_still_names_its_own_env_backend(self):
        import httpx as _httpx
        rows = self._rows(
            lambda req: _httpx.Response(200, json={"data": [
                {"id": "local-model"}]}),
            OIKONOME_LLM_URL="http://127.0.0.1:18094",
            OIKONOME_LLM_MODEL="local-model")
        text = " ".join(r["detail"] for r in rows)
        self.assertIn("127.0.0.1:18094", text)
        self.assertIn("local-model", text)


class TransportRowPasskeyPromiseTests(unittest.TestCase):
    """Passkey sign-in exists only in hosted mode; a self-host that turns
    on HTTPS gets TLS, not passkeys, so Doctor must not promise them."""

    def setUp(self):
        self.conn = make_db()
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])

    def tearDown(self):
        self.conn.close()

    def _transport(self, **env_extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("OIKONOME_LLM")
               and k not in ("OIKONOME_HOSTED", "OIKONOME_BASE_URL")}
        env.update(env_extra)
        with patch.dict(os.environ, env, clear=True):
            rows = doctor.checks(self.tid)
        return next(r for r in rows
                    if r["section"] == "Security" and r["name"] == "transport")

    def test_self_host_https_does_not_promise_passkeys(self):
        row = self._transport(OIKONOME_BASE_URL="https://home.example.test")
        self.assertNotIn("passkey", row["detail"])
        row = self._transport()
        self.assertNotIn("passkey", row["detail"])

    def test_hosted_https_names_passkeys(self):
        row = self._transport(OIKONOME_BASE_URL="https://app.example.test",
                              OIKONOME_HOSTED="1")
        self.assertIn("passkeys available", row["detail"])


class DoctorProbeNetguardTests(unittest.TestCase):
    """Doctor checks the tenant's llm_url at use time too, not only at save
    time; otherwise a URL stored without passing the save-time guard is an
    owner-triggered SSRF. The metadata IP is blocked in BOTH hosted and self-host modes, so a
    planted row must yield a 'not probed' warn row, never a fetch."""

    def setUp(self):
        import uuid

        from .util import _ensure_db
        _ensure_db()
        from oikonome.db import tenancy
        admin = tenancy.admin_connect()
        try:
            self.tid = tenancy.create_tenant(
                admin, f"llmprobe-{uuid.uuid4().hex[:8]}")
        finally:
            admin.close()
        self.conn = tenancy.tenant_connect(self.tid)

    def tearDown(self):
        self.conn.close()

    def test_planted_metadata_url_is_not_probed(self):
        from oikonome.engine import budget
        from oikonome.web import doctor
        cfg = budget.load_config(self.conn)
        # written straight to config — a row that never passed the
        # save-time guard
        cfg["llm_url"] = "http://169.254.169.254/latest"
        budget.save_config(self.conn, cfg)
        rows = doctor._llm_rows(self.conn)
        model = [r for r in rows if r["name"] == "model"]
        self.assertEqual(len(model), 1)
        self.assertFalse(model[0]["ok"])
        self.assertIn("not probed", model[0]["detail"])
        self.assertIn("metadata", model[0]["detail"])

    def test_env_backend_stays_exempt(self):
        """The operator's own env/bundled backend (private compose
        hostname by design) is never second-guessed — the probe still
        runs and reports reachability, not a block."""
        from oikonome.web import doctor
        prev = os.environ.get("OIKONOME_LLM_URL")
        os.environ["OIKONOME_LLM_URL"] = "http://127.0.0.1:1"  # closed port
        try:
            rows = doctor._llm_rows(self.conn)
        finally:
            if prev is None:
                os.environ.pop("OIKONOME_LLM_URL", None)
            else:
                os.environ["OIKONOME_LLM_URL"] = prev
        model = [r for r in rows if r["name"] == "model"]
        self.assertEqual(len(model), 1)
        self.assertNotIn("not probed", model[0]["detail"])


if __name__ == "__main__":
    unittest.main()
