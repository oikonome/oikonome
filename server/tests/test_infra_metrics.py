"""Infra-stress sampler and admin-console panel.

The sampler writes one control-plane infra_metrics row per minute with
every source individually guarded (a dead source records NULL, never a
crash), trims rows past retention, and the console panel classifies
samples against the triggers, renders with zero rows (fresh
install) and flags sampler gaps as amber instead of silence.
"""

import datetime as dt
import os
import unittest

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.jobs import infra_metrics as im

from .util import _ensure_db

TOKEN = "test-admin-token-" + "x" * 32


def _admin(sql, params=()):
    admin = tenancy.admin_connect()
    try:
        cur = admin.execute(sql, params)
        return cur.fetchall() if cur.description else None
    finally:
        admin.close()


def _insert(ts_ago_s: int, **cols):
    keys = ["ts"] + list(cols)
    vals = [dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(seconds=ts_ago_s)] + list(cols.values())
    _admin(f"INSERT INTO infra_metrics ({', '.join(keys)}) "
           f"VALUES ({', '.join(['%s'] * len(keys))})", tuple(vals))


class SamplerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        _admin("DELETE FROM infra_metrics")
        self._orig = {n: getattr(im, n) for n in im._SOURCES}

    def tearDown(self):
        for n, f in self._orig.items():
            setattr(im, n, f)

    def test_sample_writes_row_with_mocked_sources(self):
        im._pg_stats = lambda: {"pg_total_conns": 12, "pg_active": 3,
                                "pg_idle": 9, "pg_max_conns": 25}
        im._req_stats = lambda: {"api_p95_ms": 420.0, "api_req_count": 88,
                                 "pool_size": 10, "pool_available": 7,
                                 "pool_waiting": 0, "pool_timeouts": 2}
        im._host_stats = lambda: {"host_load1": 0.42, "host_cpus": 2,
                                  "mem_used_gb": 1.5, "mem_total_gb": 3.8}
        im._host_health = lambda: {"disk_used_pct": 41, "containers": [
            {"name": "oikonome-app-1", "cpu_pct": 1.2, "mem": "150MiB"}]}
        # a dead source must record NULL, never crash the sample
        def _boom():
            raise ConnectionError("redis down")
        im._redis_mem = _boom
        im.sample()
        rows = _admin("SELECT * FROM infra_metrics")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["pg_total_conns"], 12)
        self.assertEqual(r["pg_max_conns"], 25)
        self.assertEqual(r["pg_active"], 3)
        self.assertEqual(r["pool_timeouts"], 2)
        self.assertAlmostEqual(r["api_p95_ms"], 420.0, places=1)
        self.assertEqual(r["disk_used_pct"], 41)
        self.assertEqual(r["containers"][0]["name"], "oikonome-app-1")
        self.assertIsNone(r["redis_mem_mb"])            # dead source → NULL

    def test_all_sources_dead_still_writes_a_row(self):
        def _boom():
            raise RuntimeError("everything is down")
        for n in im._SOURCES:
            setattr(im, n, _boom)
        im.sample()                                     # must not raise
        rows = _admin("SELECT * FROM infra_metrics")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["pg_total_conns"])

    def test_retention_deletes_old_rows(self):
        _insert(20 * 86400, pg_total_conns=5)           # 20 days old
        _insert(60, pg_total_conns=6)                   # inside retention
        for n in im._SOURCES:
            setattr(im, n, lambda: {})
        im.sample()
        rows = _admin("SELECT pg_total_conns FROM infra_metrics ORDER BY ts")
        self.assertEqual(len(rows), 2)                  # old row swept
        self.assertEqual(rows[0]["pg_total_conns"], 6)


class BreachTests(unittest.TestCase):
    def test_pg_conn_thresholds(self):
        # rows without the server's cap classify against the default
        # constants, sized for a small managed database's connection cap
        self.assertEqual(im.breaches({"pg_total_conns": 22}),
                         {"pg_total_conns": "red"})
        self.assertEqual(im.breaches({"pg_total_conns": 17}),
                         {"pg_total_conns": "amber"})
        self.assertEqual(im.breaches({"pg_total_conns": 10}), {})
        self.assertEqual(im.breaches({"pg_total_conns": None}), {})

    def test_pg_conns_are_scored_against_the_servers_own_cap(self):
        """16 connections is two-thirds of a small managed database's
        connection cap and a sixth of a container's 100: the same count must
        not breach on the second.
        The pools alone hold ~16 while anything is running, so an absolute
        threshold reads every busy minute on a big server as pressure."""
        self.assertEqual(im.breaches({"pg_total_conns": 16,
                                      "pg_max_conns": 100}), {})
        self.assertEqual(im.breaches({"pg_total_conns": 16,
                                      "pg_max_conns": 25}),
                         {"pg_total_conns": "amber"})
        self.assertEqual(im.breaches({"pg_total_conns": 21,
                                      "pg_max_conns": 25}),
                         {"pg_total_conns": "red"})
        self.assertEqual(im.breaches({"pg_total_conns": 70,
                                      "pg_max_conns": 100}),
                         {"pg_total_conns": "amber"})
        # chart guide lines follow the cap the same way
        self.assertEqual(im.pg_conn_lines(100), (64.0, 84.0))
        self.assertEqual(im.pg_conn_lines(None), (16, 21))

    def test_p95_thresholds(self):
        self.assertEqual(im.breaches({"api_p95_ms": 2500.0}),
                         {"api_p95_ms": "red"})
        self.assertEqual(im.breaches({"api_p95_ms": 1200.0}),
                         {"api_p95_ms": "amber"})
        self.assertEqual(im.breaches({"api_p95_ms": 300.0}), {})

    def test_p95_needs_a_crowd_before_it_counts_as_pressure(self):
        """One slow export is a slow endpoint, not capacity: a >2s p95 over
        a single request must not breach, the same p95 over a busy window
        must. A row with no recorded count is judged on the p95 alone."""
        self.assertEqual(im.breaches({"api_p95_ms": 3300.0,
                                      "api_req_count": 1}), {})
        self.assertEqual(im.breaches({"api_p95_ms": 3300.0,
                                      "api_req_count": im.P95_MIN_REQS - 1}),
                         {})
        self.assertEqual(im.breaches({"api_p95_ms": 3300.0,
                                      "api_req_count": im.P95_MIN_REQS}),
                         {"api_p95_ms": "red"})
        self.assertEqual(im.breaches({"api_p95_ms": 1200.0,
                                      "api_req_count": None}),
                         {"api_p95_ms": "amber"})

    def test_pool_timeout_delta(self):
        # any NEW timeout since the previous sample is red
        self.assertEqual(im.breaches({"pool_timeouts": 7}, prev_timeouts=5),
                         {"pool_timeout_delta": "red"})
        self.assertEqual(im.breaches({"pool_timeouts": 5}, prev_timeouts=5),
                         {})
        # counter drop = web-process restart, clamped — never a phantom
        self.assertEqual(im.breaches({"pool_timeouts": 0}, prev_timeouts=5),
                         {})
        # no previous sample → no delta to judge
        self.assertEqual(im.breaches({"pool_timeouts": 7}), {})

    def test_host_thresholds(self):
        self.assertEqual(
            im.breaches({"mem_used_gb": 3.7, "mem_total_gb": 3.8}),
            {"mem_pct": "red"})
        self.assertEqual(
            im.breaches({"host_load1": 2.5, "host_cpus": 2}),
            {"load_per_cpu": "red"})
        self.assertEqual(im.breaches({"disk_used_pct": 92}),
                         {"disk_used_pct": "red"})
        self.assertEqual(
            im.breaches({"mem_used_gb": 1.0, "mem_total_gb": 3.8,
                         "host_load1": 0.2, "host_cpus": 2,
                         "disk_used_pct": 40}), {})


class PanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        os.environ["OIKONOME_ADMIN_TOKEN"] = TOKEN
        from oikonome.web import security
        security._limiter._hits.clear()
        _admin("DELETE FROM infra_metrics")
        self.client = TestClient(self.appmod.app)
        r = self.client.post("/admin/console/login", data={"token": TOKEN},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)

    def tearDown(self):
        os.environ.pop("OIKONOME_ADMIN_TOKEN", None)

    def test_zero_rows_renders_fresh_install_note(self):
        r = self.client.get("/admin/console")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Infra stress", r.text)
        self.assertIn("No samples yet", r.text)

    def test_fragment_requires_auth(self):
        bare = TestClient(self.appmod.app)
        r = bare.get("/admin/console/infra", follow_redirects=False)
        self.assertEqual(r.status_code, 401)

    def test_gap_renders_amber_warning_not_silence(self):
        # latest sample 30 min old → the sampler is dead; the panel must
        # say so, not show a stale chart as if all were well
        _insert(3600, pg_total_conns=8)
        _insert(1800, pg_total_conns=9)
        r = self.client.get("/admin/console/infra")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Sampler gap", r.text)
        self.assertIn("30 minutes", r.text)

    def test_intra_window_gap_counted(self):
        _insert(7200, pg_total_conns=8)
        _insert(60, pg_total_conns=9)                   # ~2h hole between
        r = self.client.get("/admin/console/infra")
        self.assertIn("1 sampler gap", r.text)

    def test_breach_sample_marked_as_spike(self):
        _insert(120, pg_total_conns=23, api_p95_ms=2500.0)
        r = self.client.get("/admin/console/infra")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Trigger breaches in window", r.text)
        self.assertIn("pg_total_conns red", r.text)
        self.assertIn("api_p95_ms red", r.text)
        # the gauge itself goes red too
        self.assertIn("23 / 25", r.text)

    def test_healthy_samples_no_spikes(self):
        _insert(120, pg_total_conns=5, api_p95_ms=200.0)
        _insert(60, pg_total_conns=6, api_p95_ms=180.0)
        r = self.client.get("/admin/console/infra")
        self.assertIn("No trigger breaches", r.text)
        self.assertIn("<svg", r.text)                   # sparkline rendered

    def test_window_param_validated(self):
        _insert(60, pg_total_conns=6)
        r = self.client.get("/admin/console/infra?win=bogus")
        self.assertEqual(r.status_code, 200)            # falls back to 24h
        r = self.client.get("/admin/console/infra?win=14d")
        self.assertEqual(r.status_code, 200)

    def test_chart_renders_axes_and_ticks(self):
        # a couple of samples an hour apart → a real time-series chart with
        # labeled X (time) and Y (value) axis ticks, not a bare sparkline
        _insert(3600, pg_total_conns=8)
        _insert(60, pg_total_conns=9)
        r = self.client.get("/admin/console/infra")
        self.assertEqual(r.status_code, 200)
        self.assertIn("<svg", r.text)
        # axis tick labels carry the .ax class (both X-time and Y-value)
        self.assertIn('class="ax"', r.text)
        # a Y tick label of "0" (the baseline) and a viewBox are present
        self.assertIn("viewBox=", r.text)
        self.assertIn(">0</text>", r.text)

    def test_chart_carries_tooltip_data(self):
        _insert(120, pg_total_conns=7)
        _insert(60, pg_total_conns=9)
        r = self.client.get("/admin/console/infra")
        self.assertEqual(r.status_code, 200)
        # the hover tooltip reads these — points JSON + label + viewBox dims
        self.assertIn("data-chart-points=", r.text)
        self.assertIn("data-chart-label=", r.text)
        self.assertIn("data-chart-vw=", r.text)
        # the JSON encodes each sample's pixel position + timestamp + value
        # (attribute-escaped, so match on the escaped-quote form)
        self.assertIn("&#34;x&#34;", r.text)
        self.assertIn("&#34;t&#34;", r.text)
        self.assertIn("&#34;d&#34;", r.text)

    def test_summary_and_detail_markup_present(self):
        # The fragment carries BOTH the one-line summary (for the standalone
        # ?summary=1 endpoint) and the full detail. The panel is always
        # expanded, with no collapse toggle, so the console shows the detail;
        # CSS hides the sumline there.
        _insert(60, pg_total_conns=6, mem_used_gb=1.9, mem_total_gb=3.8,
                disk_used_pct=41, host_load1=0.4, api_p95_ms=36.0)
        r = self.client.get("/admin/console/infra")
        self.assertEqual(r.status_code, 200)
        # no collapse control…
        self.assertNotIn("data-infra-toggle", r.text)
        # …but the one-line summary markup is rendered (summary endpoint)
        self.assertIn("infra-sumline", r.text)
        # summary renders each gauge value with a threshold class — a calm
        # value is green (pill g), colored by the SAME thresholds
        self.assertIn('PG <span class="pill g">6/25</span>', r.text)
        self.assertIn('Disk <span class="pill g">41%</span>', r.text)
        self.assertIn('p95 <span class="pill g">36ms</span>', r.text)
        # the full detail (expanded view) is still there intact
        self.assertIn("infra-detail", r.text)
        self.assertIn("<svg", r.text)
        self.assertIn("PG connections", r.text)

    def test_summary_only_fragment_skips_heavy_charts(self):
        # the collapsed strip's 20s refresh (?summary=1) renders only the
        # one-line status from the latest sample — no full-window chart SVG.
        _insert(3600, pg_total_conns=8)
        _insert(60, pg_total_conns=6, mem_used_gb=1.9, mem_total_gb=3.8,
                disk_used_pct=41, host_load1=0.4, api_p95_ms=36.0)
        r = self.client.get("/admin/console/infra?summary=1")
        self.assertEqual(r.status_code, 200)
        # the one-line summary is present and colored…
        self.assertIn("infra-sumline", r.text)
        self.assertIn('PG <span class="pill g">6/25</span>', r.text)
        # …but the expensive charts / spike scan are NOT rendered
        self.assertNotIn("<svg", r.text)
        self.assertNotIn("Trigger breaches in window", r.text)
        # the full (expanded) fragment DOES render the charts
        full = self.client.get("/admin/console/infra")
        self.assertIn("<svg", full.text)

    def test_summary_only_flags_sampler_gap(self):
        # even the cheap path must surface a dead sampler (latest row stale)
        _insert(1800, pg_total_conns=9)                 # 30 min old
        r = self.client.get("/admin/console/infra?summary=1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Sampler gap", r.text)

    def test_summary_colors_breach_and_degrades_missing(self):
        # a breaching PG value is red in the summary; a NULL metric shows a
        # muted em-dash, never a crash
        _insert(120, pg_total_conns=23)                 # >20 → red; others NULL
        r = self.client.get("/admin/console/infra")
        self.assertEqual(r.status_code, 200)
        self.assertIn('PG <span class="pill r">23/25</span>', r.text)
        # memory/disk/load/p95 never reported → muted "—"
        self.assertIn('Mem <span class="pill m">—</span>', r.text)

    def test_panel_always_expanded_client_wiring(self):
        # the infra panel is always expanded: the client forces it open and
        # wires NO collapse toggle.
        from pathlib import Path

        import oikonome.web as webpkg
        base = Path(webpkg.__file__).parent
        js = (base / "static" / "pages.js").read_text()
        self.assertIn('infra.classList.remove("collapsed")', js)  # forced open
        self.assertNotIn("oikonome.infra.collapsed", js)          # no persistence
        self.assertNotIn("data-infra-toggle", js)                 # no toggle wiring

    def test_full_page_panel_has_a_heading_and_no_caption(self):
        _insert(60, pg_total_conns=6)
        r = self.client.get("/admin/console")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Infra stress", r.text)
        self.assertNotIn("auto-refreshes every 20s", r.text)
        self.assertNotIn("measured against the", r.text)


class ReqMetricsTests(unittest.TestCase):
    def test_p95_math(self):
        import time

        from oikonome.web import reqmetrics as rm
        rm._ring.clear()
        now = time.time()
        for ms in range(1, 101):                        # 1..100 ms
            rm._ring.append((now, float(ms)))
        p95, n = rm._p95(now)
        self.assertEqual(n, 100)
        self.assertAlmostEqual(p95, 94.0, delta=2.0)
        rm._ring.clear()
        self.assertEqual(rm._p95(now), (None, 0))

    def test_record_never_raises_without_redis(self):
        from oikonome.web import reqmetrics as rm
        old = os.environ.get("REDIS_URL")
        os.environ["REDIS_URL"] = "redis://127.0.0.1:1/0"   # nothing there
        try:
            rm._last_publish = 0.0
            rm.record(12.0)                             # must not raise
        finally:
            if old is None:
                os.environ.pop("REDIS_URL", None)
            else:
                os.environ["REDIS_URL"] = old


if __name__ == "__main__":
    unittest.main()
