"""The Business tab is hidden until a business exists.

`/api/me` carries `has_business`; the SPA shows the top-level Business tab
only when it is true (most households never run a business,
and a permanently empty top-level tab makes the app look like it was built for
someone else).

The flag has to be exactly "does a business_entity row exist for this tenant",
because the tab is the only discoverable route to the Business page. Too
eager and every household sees a tab for a feature they don't use; too lazy
and someone who just finished the wizard can't find what they made.
"""
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import entities

from .util import _ensure_db


class HasBusinessFlagTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"navgate-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]

    def _me(self):
        r = self.client.get("/api/me")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_false_for_a_fresh_account(self):
        """The default. A new household has no business, so no tab."""
        me = self._me()
        self.assertIn("has_business", me, "the SPA reads this key by name")
        self.assertFalse(me["has_business"])

    def test_true_once_the_wizard_creates_one(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            entities.create_entity(conn, name="Acme LLC",
                                   structure="sole_prop")
        finally:
            conn.close()
        self.assertTrue(self._me()["has_business"])

    def test_stays_true_for_an_archived_business(self):
        """Someone who closed their LLC keeps the tab: their books and past
        Schedule C data are still in there, and hiding the only route back to
        them would be worse than showing one extra tab."""
        conn = tenancy.tenant_connect(self.tid)
        try:
            e = entities.create_entity(conn, name="Closed Co",
                                       structure="single_member_llc")
            entities.set_status(conn, e["id"], "archived")
            rows = entities.list_entities(conn, include_archived=False)
            self.assertNotIn("Closed Co", [r["name"] for r in rows],
                             "fixture check: it really is archived")
        finally:
            conn.close()
        self.assertTrue(self._me()["has_business"])

    def test_the_route_is_reachable_regardless(self):
        """has_business hides the TAB, never the route — a bookmark or a link
        from the wizard must still land on the page, which has its own empty
        state. The SPA serves one shell for every /app path, so what this
        asserts is that the server does not gate it."""
        for path in ("/app/business", "/app/business/setup"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)


class OtherTenantsUnaffectedTests(unittest.TestCase):
    """RLS check: one tenant's business must not light up another's tab."""

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.app = app

    def test_business_is_not_visible_across_tenants(self):
        c1, c2 = TestClient(self.app), TestClient(self.app)
        c1.post("/api/signup", data={
            "email": f"biz-a-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        c2.post("/api/signup", data={
            "email": f"biz-b-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        tid1 = c1.get("/api/me").json()["tenant_id"]

        conn = tenancy.tenant_connect(tid1)
        try:
            entities.create_entity(conn, name="Only Mine LLC",
                                   structure="single_member_llc")
        finally:
            conn.close()

        self.assertTrue(c1.get("/api/me").json()["has_business"])
        self.assertFalse(c2.get("/api/me").json()["has_business"],
                         "another tenant's business must not show a tab here")


if __name__ == "__main__":
    unittest.main()


class SpaWiringTests(unittest.TestCase):
    """The SPA side of the gate, asserted against source — the nav is the one
    surface a server test cannot reach."""

    def _src(self, name):
        import pathlib
        return (pathlib.Path(__file__).resolve().parents[2]
                / "webapp" / "src" / name).read_text(encoding="utf-8")

    def test_nav_tab_is_gated_on_has_business(self):
        app = self._src("App.tsx")
        i = app.index('to="/business" className={tab}')
        window = app[max(0, i - 400):i]
        self.assertIn("me.data.has_business", window,
                      "the Business tab must be gated on has_business")

    def test_the_route_is_still_registered(self):
        """Hiding the tab must not remove the route: the wizard navigates to
        /business on completion, and links/bookmarks have to keep working."""
        app = self._src("App.tsx")
        self.assertIn('path="/business"', app)
        self.assertIn('path="/business/setup"', app)

    def test_wizard_refetches_me_so_the_tab_appears(self):
        """Creating the entity is the moment has_business flips. Without this
        invalidation the tab stays hidden until the next full page load, which
        reads as 'the wizard didn't work'."""
        w = self._src("components/BusinessWizard.tsx")
        create = w[w.index("const create = useMutation"):]
        create = create[:create.index("const assign")]
        self.assertIn('queryKey: ["me"]', create,
                      "entity creation must invalidate the me query")

class HeaderBellTests(unittest.TestCase):
    """Alerts is a header bell with a count, not a nav item.
    Settings left the nav too — it has always been in the user menu."""

    def _src(self, name):
        import pathlib
        return (pathlib.Path(__file__).resolve().parents[2]
                / "webapp" / "src" / name).read_text(encoding="utf-8")

    def test_alerts_and_settings_are_not_nav_items(self):
        app = self._src("App.tsx")
        # Anchor on the navscroll wrapper and walk back to its <nav>, rather
        # than matching an exact snippet: a multi-line onClick on <nav>
        # would break an exact-snippet assertion over markup it does not
        # care about. (A regex is no better here — `<nav[^>]*>` stops dead on the
        # `>` in an arrow function.) The claim is about which destinations
        # are in the nav, so it should survive anything attached to it.
        wrapper = app.index('<div className="navscroll">')
        nav = app[app.rindex("<nav", 0, wrapper):]
        nav = nav[:nav.index("</nav>")]
        for gone in ('to="/alerts" className={tab}',
                     'to="/settings" className={tab}'):
            self.assertNotIn(gone, nav, f"{gone} is back in the nav")
        # Accounts deliberately STAYS: it is the only route to fixing a
        # broken bank connection, and "Accounts" in a menu opened from your
        # own email address reads as user accounts, not bank accounts.
        self.assertIn('to="/accounts" className={tab}', nav)

    def test_both_destinations_stay_reachable(self):
        app = self._src("App.tsx")
        self.assertIn('to="/alerts"', app, "the bell must link to Alerts")
        self.assertIn('to="/settings"', app,
                      "Settings must still be in the user menu")
        self.assertIn('path="/alerts"', app)
        self.assertIn('path="/settings"', app)

    def test_bell_counts_only_live_alerts(self):
        """A dismissed row is one the user already answered; badging it again
        trains them to ignore the bell."""
        app = self._src("App.tsx")
        self.assertIn("a.active && !a.dismissed", app)

    def test_bell_severity_colours_match_the_page_vocabulary(self):
        app = self._src("App.tsx")
        for sev, var in (("bad", "--red"), ("warn", "--amber")):
            self.assertIn(sev, app)
            self.assertIn(var, app)

    def test_bell_is_labelled_for_screen_readers(self):
        """It is an icon-only control — without a label it is an unnamed link."""
        app = self._src("App.tsx")
        i = app.index('<NavLink to="/alerts"')
        window = app[i:i + 900]
        self.assertIn("aria-label={alertLabel}", window)
        self.assertIn("title={alertLabel}", window)
        self.assertIn('aria-hidden="true"', window,
                      "the emoji and badge must be hidden from the a11y tree")
