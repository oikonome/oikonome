"""admin-console section nav.

A tablist splits the (crowded) single-page console into views — Overview ·
Users · Ops · Deploys · Audit · Infra, plus any pane an installed
add-on contributes — shown one at a time client-side (Users right after
Overview, Deploys after Ops). Infra is its OWN tab, rightmost; its panel
is ALWAYS EXPANDED — no collapse toggle. Deployments and
version history are their own Deploys page; the tenants tab is labelled
"Users".
The auth model is UNCHANGED: every view lives behind the one authed render,
and a fragment route is gated exactly like the infra fragment. These tests
guard that the nav renders, that nothing from the old page is lost, and
that auth still holds on every surface.
"""

import os
import unittest

from fastapi.testclient import TestClient

TOKEN = "test-admin-token-" + "x" * 32


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        from .util import _ensure_db
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        os.environ["OIKONOME_ADMIN_TOKEN"] = TOKEN
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.appmod.app)

    def tearDown(self):
        os.environ.pop("OIKONOME_ADMIN_TOKEN", None)

    def _login(self):
        self.client.post("/admin/console/login", data={"token": TOKEN},
                         follow_redirects=False)


class NavRenderTests(_Base):
    def test_nav_renders_all_views(self):
        self._login()
        html = self.client.get("/admin/console").text
        self.assertIn('data-console-nav', html)
        self.assertIn('role="tablist"', html)
        for key in ("overview", "deploys", "tenants", "ops", "audit",
                    "infra"):
            self.assertIn(f'data-nav="{key}"', html)
            self.assertIn(f'id="view-{key}"', html)
            self.assertIn(f'data-view="{key}"', html)
        # the tenants view is labelled "Users" in the nav (the label rides
        # a .cn-t span, as the left rail lays it out)
        self.assertIn('data-nav="tenants"', html)
        self.assertIn('<span class="cn-t">Users</span></button>', html)

    def test_infra_is_a_tab_rightmost(self):
        # Infra is its own tab, rightmost, and
        # Deploys is its own tab as well.
        self._login()
        html = self.client.get("/admin/console").text
        self.assertIn('data-nav="infra"', html)
        self.assertIn('id="view-infra"', html)
        self.assertIn('data-view="infra"', html)
        # six tabs of the product's own — overview, deploys, tenants, ops,
        # audit, infra — plus whatever panes an installed add-on always
        # renders (a `when`-gated pane needs its data configured, which
        # this instance does not have)
        from oikonome import ext
        always = sum(1 for p in ext.admin_panes() if not p.get("when"))
        self.assertEqual(html.count('role="tab"'), 6 + always)
        # infra's nav button is the last one in the tablist
        nav_start = html.index('data-console-nav')
        nav = html[nav_start:html.index('</nav>', nav_start)]
        self.assertGreater(nav.rindex('data-nav="infra"'),
                           nav.rindex('data-nav="audit"'))

    def test_nothing_from_the_old_page_is_lost(self):
        # every section header the single-page console carried must still be
        # present — the nav reorganizes, it does not drop
        self._login()
        html = self.client.get("/admin/console").text
        for marker in ("System health", "Infra stress",
                       "Fleet", "Outstanding invites",
                       "Unverified users", "Broadcast", "Deployments",
                       "Host &amp; backups", "Host operations", "Jobs",
                       "Audit trail", "App log"):
            self.assertIn(marker, html, marker)

    def test_only_one_view_active_by_default(self):
        self._login()
        html = self.client.get("/admin/console").text
        # exactly one section server-renders as active (Overview); JS may
        # switch to the last-viewed one after load
        self.assertEqual(html.count('class="console-view active"'), 1)
        self.assertIn('id="view-overview" data-view="overview"', html)

    def test_infra_panel_lives_in_its_own_view_always_expanded(self):
        self._login()
        html = self.client.get("/admin/console").text
        # the #infra-panel is preserved, still refreshes
        self.assertIn('id="infra-panel"', html)
        self.assertIn('data-infra-refresh', html)
        # it lives INSIDE the infra console-view
        view_i = html.index('id="view-infra"')
        panel_i = html.index('id="infra-panel"')
        next_section = html.index('</section>', view_i)
        self.assertLess(view_i, panel_i)
        self.assertLess(panel_i, next_section)
        # always-expanded: there is no collapse toggle and
        # the detail view renders (the sumline element stays in the DOM for the
        # standalone ?summary=1 fragment, but CSS hides it while expanded)
        self.assertNotIn('data-infra-toggle', html)
        self.assertNotIn('infra-caret', html)
        self.assertIn('infra-detail', html)

    def test_nav_order(self):
        # Users right after Overview; Deploys right after Ops
        self._login()
        html = self.client.get("/admin/console").text
        nav_start = html.index('data-console-nav')
        nav = html[nav_start:html.index('</nav>', nav_start)]
        order = [k for k in ("overview", "tenants", "ops", "deploys", "audit")]
        idxs = [nav.index(f'data-nav="{k}"') for k in order]
        self.assertEqual(idxs, sorted(idxs),
                         "nav order must be Overview·Users·Ops·Deploys·Audit")
        # the two adjacencies the order is defined by
        self.assertLess(nav.index('data-nav="overview"'),
                        nav.index('data-nav="tenants"'))      # Users after Overview
        self.assertLess(nav.index('data-nav="ops"'),
                        nav.index('data-nav="deploys"'))      # Deploys after Ops


class NavAuthTests(_Base):
    def test_dashboard_unauthed_shows_login_not_views(self):
        r = self.client.get("/admin/console")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Operator token", r.text)
        # no view content leaks without a session
        self.assertNotIn('data-console-nav', r.text)
        self.assertNotIn('data-view="audit"', r.text)

    def test_console_off_hides_the_nav(self):
        os.environ.pop("OIKONOME_ADMIN_TOKEN", None)
        r = self.client.get("/admin/console", follow_redirects=False)
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
