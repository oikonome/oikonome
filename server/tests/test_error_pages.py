"""branded error pages for browser routes; API routes keep JSON.

Browser-facing paths render the dark-theme error shell (open-arc mark +
wordmark + link home) on 404 and unhandled 500s; anything under /api keeps
its machine-readable body — {"detail":...} JSON for HTTPExceptions and
Starlette's plain-text 500 — because the SPA and integrations parse those.
"""

import unittest

from fastapi.testclient import TestClient

from .util import _ensure_db


def _boom_page():
    raise RuntimeError("errpage simulated failure")


def _boom_api():
    raise RuntimeError("errpage simulated failure")


def _need_auth():
    from fastapi import HTTPException
    raise HTTPException(401, "not signed in")


def _throttled():
    from fastapi import HTTPException
    raise HTTPException(429, "too many requests — try again later")


class ErrorPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()                      # redirect DSNs BEFORE app import
        from oikonome.web.app import app
        cls.app = app
        # raise_server_exceptions=False lets the 500 handler's response
        # come back instead of the test re-raising the simulated error
        cls.client = TestClient(app, raise_server_exceptions=False)
        # temporary raising routes to exercise the 500 path both sides of
        # the /api boundary; removed in tearDownClass
        app.get("/x-errpage-boom-page")(_boom_page)
        app.get("/api/errpage-boom")(_boom_api)
        app.get("/x-errpage-need-auth")(_need_auth)
        app.post("/x-errpage-throttled")(_throttled)
        app.post("/api/errpage-throttled")(_throttled)

    @classmethod
    def tearDownClass(cls):
        app = cls.app
        app.router.routes[:] = [
            r for r in app.router.routes
            if getattr(r, "path", "") not in ("/x-errpage-boom-page",
                                              "/api/errpage-boom",
                                              "/x-errpage-need-auth",
                                              "/x-errpage-throttled",
                                              "/api/errpage-throttled")]

    def test_browser_404_is_branded_html(self):
        r = self.client.get("/no-such-page-errpage")
        self.assertEqual(r.status_code, 404)
        self.assertIn("text/html", r.headers["content-type"])
        # the heading is the status itself — "404" with the quotes
        self.assertIn(">404<", r.text)
        self.assertIn("Oikonome", r.text)
        self.assertIn("M39.7 78.2", r.text)     # the open-arc brand mark
        self.assertIn('href="/app/"', r.text)   # link home
        self.assertNotIn('"detail"', r.text)

    def test_rate_limited_form_post_is_a_page_not_json(self):
        """The limiter is a dependency and fires before a form route can
        re-render itself, so a browser posting /signup once too often
        must still get the branded page — never a raw {"detail"} body."""
        r = self.client.post("/x-errpage-throttled")
        self.assertEqual(r.status_code, 429)
        self.assertIn("text/html", r.headers["content-type"])
        self.assertIn("Slow down", r.text)
        self.assertIn("Retry-After", r.headers)
        self.assertNotIn('"detail"', r.text)
        # the API side keeps JSON for the SPA / integrations
        r = self.client.post("/api/errpage-throttled")
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()["detail"], "too many requests — try again later")

    def test_404_carries_a_classical_quote(self):
        # the garnish under the functional message: one of the known
        # genuinely-classical quotes, Greek first, translation beneath
        from oikonome.web.app import _ERROR_QUOTES
        r = self.client.get("/no-such-page-errpage")
        self.assertIn('lang="grc"', r.text)
        self.assertTrue(any(q[0] in r.text and q[1] in r.text
                            and q[2] in r.text
                            for q in _ERROR_QUOTES[404]),
                        "no known 404 quote rendered")
        # garnish, not replacement: the plain statement is still there
        self.assertIn("nothing at this address", r.text)

    def test_500_carries_a_classical_quote(self):
        from oikonome.web.app import _ERROR_QUOTES
        r = self.client.get("/x-errpage-boom-page")
        self.assertIn('lang="grc"', r.text)
        self.assertTrue(any(q[0] in r.text and q[1] in r.text
                            and q[2] in r.text
                            for q in _ERROR_QUOTES[500]),
                        "no known 500 quote rendered")
        self.assertIn("Something went wrong", r.text)

    def test_api_404_stays_json(self):
        r = self.client.get("/api/no-such-endpoint-errpage")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json(), {"detail": "Not Found"})

    def test_api_error_detail_contract_unchanged(self):
        # a real endpoint's HTTPException still serializes {"detail": ...}
        r = self.client.get("/api/accounts")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json(), {"detail": "not signed in"})

    def test_browser_500_is_branded_html(self):
        r = self.client.get("/x-errpage-boom-page")
        self.assertEqual(r.status_code, 500)
        self.assertIn("text/html", r.headers["content-type"])
        self.assertIn("Something went wrong", r.text)
        self.assertIn("logged", r.text)
        self.assertIn("Oikonome", r.text)

    def test_api_500_stays_plain(self):
        # Starlette's opaque body, preserved exactly
        r = self.client.get("/api/errpage-boom")
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.text, "Internal Server Error")
        self.assertNotIn("text/html", r.headers["content-type"])

    def test_anonymous_page_401_still_redirects_to_login(self):
        # the older redirect rule rides the same handler — keep it pinned
        # now that the handler matches Starlette's class
        r = self.client.get("/x-errpage-need-auth", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/login")


if __name__ == "__main__":
    unittest.main()
