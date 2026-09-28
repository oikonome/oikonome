"""/api/logo — merchant logos through the instance's own cache, so no
client ever fetches plaid.com itself. Only Plaid's two logo hosts are
proxied (it is a logo cache, not an open fetch door); the first request
fetches and caches, later ones read the cache; a failed fetch is a 404
remembered briefly, not a round trip per row.

It takes a SESSION. Anonymous, it is a door that makes the instance fetch
from a CDN and grow an unbounded global table, 600 times a minute per IP.
And the cache is CAPPED — nothing else prunes it, so the writer prunes.
"""

import unittest
import uuid
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web import logos

from .util import _ensure_db

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
GOOD = "https://plaid-merchant-logos.plaid.com/acme_1.png"


def _signed_in_client() -> TestClient:
    import os
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    client = TestClient(appmod.app)
    client.post("/api/signup", data={
        "email": f"logo-{uuid.uuid4().hex[:8]}@example.dev",
        "password": "correct-horse-battery"})
    assert client.get("/api/me").status_code == 200, "test signup failed"
    return client


def _clear() -> None:
    admin = tenancy.admin_connect()
    try:
        admin.execute("DELETE FROM logo_cache WHERE url LIKE %s",
                      ("https://plaid-merchant-logos.plaid.com/acme%",))
    finally:
        admin.close()


class LogoProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = _signed_in_client()
        _clear()

    def test_only_plaid_logo_hosts_are_allowed(self):
        self.assertTrue(logos.allowed(GOOD))
        self.assertTrue(logos.allowed("https://plaid-counterparty-logos.plaid.com/x_2.png"))
        for bad in ("https://evil.example/x.png", "http://plaid-merchant-logos.plaid.com/x.png",
                    "https://plaid-merchant-logos.plaid.com/x.png?ssrf=1",
                    "https://plaid-merchant-logos.plaid.com.evil.example/x.png",
                    "https://plaid-merchant-logos.plaid.com/x.svg", "not a url"):
            self.assertFalse(logos.allowed(bad), bad)
        r = self.client.get("/api/logo", params={"u": "https://evil.example/x.png"})
        self.assertEqual(r.status_code, 400)

    def test_first_request_fetches_and_caches(self):
        calls = []

        def fake_fetch(url):
            calls.append(url)
            return ("image/png", PNG)
        with mock.patch.object(logos, "_fetch", fake_fetch):
            r1 = self.client.get("/api/logo", params={"u": GOOD})
            r2 = self.client.get("/api/logo", params={"u": GOOD})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.headers["content-type"], "image/png")
        self.assertEqual(r1.headers["x-content-type-options"], "nosniff")
        self.assertEqual(r1.content, PNG)
        self.assertIn("max-age", r1.headers["cache-control"])
        self.assertEqual(r2.content, PNG)
        self.assertEqual(calls, [GOOD], "the second request must be a cache read")

    def test_a_dead_logo_is_a_404_and_not_refetched_per_row(self):
        url = "https://plaid-merchant-logos.plaid.com/acme_gone_2.png"
        calls = []

        def fake_fetch(u):
            calls.append(u)
            return None
        with mock.patch.object(logos, "_fetch", fake_fetch):
            self.assertEqual(self.client.get("/api/logo", params={"u": url}).status_code, 404)
            self.assertEqual(self.client.get("/api/logo", params={"u": url}).status_code, 404)
        self.assertEqual(calls, [url])


class LogoProxyNeedsASessionTests(unittest.TestCase):
    """Anonymous, this endpoint is a cache-fill and CDN-hammer door that
    needs no account. The logo of a merchant discloses little; making a
    stranger's request spend our network and our disk is the problem."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()
        from oikonome.web.app import app
        cls.app = app

    def test_a_signed_out_caller_gets_401_and_no_fetch(self):
        anon = TestClient(self.app)
        with mock.patch.object(logos, "_fetch") as fetch:
            r = anon.get("/api/logo", params={"u": GOOD})
        self.assertEqual(401, r.status_code)
        fetch.assert_not_called()


class LogoFetchTests(unittest.TestCase):
    """What comes back from the CDN is not taken on trust."""

    def _client(self, handler):
        # bind the real class first: logos.httpx IS httpx, so a lambda that
        # looked the name up again would call the patch
        real = httpx.Client
        return mock.patch.object(
            logos.httpx, "Client",
            lambda **kw: real(transport=httpx.MockTransport(handler)))

    def test_only_png_is_accepted(self):
        """image/svg+xml is a SCRIPTABLE document. Served back from our own
        origin it would be same-origin script, so a non-PNG body is a miss
        however the .png-only allowlist was satisfied."""
        for ct in ("image/svg+xml", "text/html", "application/octet-stream"):
            with self._client(lambda req, ct=ct: httpx.Response(
                    200, content=b"<svg/>", headers={"content-type": ct})):
                self.assertIsNone(logos._fetch(GOOD), ct)
        with self._client(lambda req: httpx.Response(
                200, content=PNG, headers={"content-type": "image/png"})):
            self.assertEqual(("image/png", PNG), logos._fetch(GOOD))

    def test_an_oversized_body_stops_at_the_cap_instead_of_being_read(self):
        """The size check must bound what is READ. Reading the whole body
        and measuring it afterwards lets the upstream decide how much
        memory the instance spends."""
        served = []

        def handler(request):
            def stream():
                for _ in range(40):
                    served.append(1)
                    yield b"\x00" * (64 * 1024)
            return httpx.Response(200, headers={"content-type": "image/png"},
                                  content=stream())
        with self._client(handler):
            self.assertIsNone(logos._fetch(GOOD))
        self.assertLess(len(served), 40,
                        "the body was read past the cap before measuring")


class LogoCacheIsCappedTests(unittest.TestCase):
    """logo_cache is global and nothing else prunes it — so the writer
    prunes, or an authenticated client can fill the disk one miss at a
    time."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_a_new_entry_evicts_the_oldest_past_the_cap(self):
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM logo_cache")
            for i in range(6):
                admin.execute(
                    "INSERT INTO logo_cache (url, content_type, bytes, fetched_at)"
                    " VALUES (%s,'image/png',%s, now() - make_interval(days => %s))",
                    (f"https://plaid-merchant-logos.plaid.com/old_{i}.png",
                     PNG, 10 - i))
            with mock.patch.object(logos, "_CAP_ROWS", 4), \
                 mock.patch.object(logos, "_fetch",
                                   lambda u: ("image/png", PNG)):
                logos.cached_logo(GOOD)
            rows = [r["url"] for r in admin.execute(
                "SELECT url FROM logo_cache ORDER BY fetched_at DESC").fetchall()]
            self.assertEqual(4, len(rows), rows)
            self.assertEqual(GOOD, rows[0], "the fresh fetch must survive")
            self.assertNotIn("https://plaid-merchant-logos.plaid.com/old_0.png",
                             rows, "the stalest row must be the first evicted")
        finally:
            admin.execute("DELETE FROM logo_cache")
            admin.close()

    def test_the_byte_cap_evicts_too(self):
        """Rows bound a flood of empty MISS rows; bytes bound the disk a
        real fill costs. Both, or one of the two floods is free."""
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM logo_cache")
            for i in range(4):
                admin.execute(
                    "INSERT INTO logo_cache (url, content_type, bytes, fetched_at)"
                    " VALUES (%s,'image/png',%s, now() - make_interval(days => %s))",
                    (f"https://plaid-merchant-logos.plaid.com/big_{i}.png",
                     b"\x00" * 1000, 10 - i))
            with mock.patch.object(logos, "_CAP_BYTES", 2500), \
                 mock.patch.object(logos, "_fetch",
                                   lambda u: ("image/png", b"\x00" * 1000)):
                logos.cached_logo(GOOD)
            rows = [r["url"] for r in admin.execute(
                "SELECT url FROM logo_cache ORDER BY fetched_at DESC").fetchall()]
            self.assertEqual(2, len(rows), rows)
            self.assertEqual(GOOD, rows[0])
        finally:
            admin.execute("DELETE FROM logo_cache")
            admin.close()

    def test_a_flood_of_misses_cannot_evict_real_logos(self):
        """The cache is instance-wide and miss rows are free to fabricate
        (any signed-in user, any allowed .png URL that will never resolve).
        A prune that ranks misses and real logos together by recency lets
        a burst of fresh misses push every real logo past the row cap — one
        user wiping the whole instance's cache. Misses and real rows
        must each be trimmed against their own cap."""
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM logo_cache")
            for i in range(3):        # real logos, cached a while ago
                admin.execute(
                    "INSERT INTO logo_cache (url, content_type, bytes, fetched_at)"
                    " VALUES (%s,'image/png',%s, now() - make_interval(hours => %s))",
                    (f"https://plaid-merchant-logos.plaid.com/acmereal_{i}.png",
                     PNG, 10 - i))
            for i in range(8):        # a flood of brand-new misses
                admin.execute(
                    "INSERT INTO logo_cache (url, content_type, bytes, fetched_at)"
                    " VALUES (%s,%s,'', now())",
                    (f"https://plaid-merchant-logos.plaid.com/acmemiss_{i}.png",
                     logos._MISS_TYPE))
            conn = tenancy.control_connect()
            try:
                with mock.patch.object(logos, "_CAP_ROWS", 4), \
                     mock.patch.object(logos, "_CAP_MISS_ROWS", 3):
                    logos._prune(conn)
            finally:
                conn.close()
            urls = [r["url"] for r in admin.execute(
                "SELECT url FROM logo_cache").fetchall()]
            real = [u for u in urls if "acmereal_" in u]
            misses = [u for u in urls if "acmemiss_" in u]
            self.assertEqual(3, len(real),
                             "fresh misses must never evict real logos")
            self.assertEqual(3, len(misses), "misses trim to their own cap")
        finally:
            admin.execute("DELETE FROM logo_cache")
            admin.close()

    def test_expired_misses_are_swept_at_prune(self):
        """The read path already treats a miss older than a day as absent,
        so an expired miss row is dead weight — prune drops it instead of
        letting it count against anything."""
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM logo_cache")
            admin.execute(
                "INSERT INTO logo_cache (url, content_type, bytes, fetched_at)"
                " VALUES (%s,%s,'', now() - interval '2 days')",
                ("https://plaid-merchant-logos.plaid.com/acmestale.png",
                 logos._MISS_TYPE))
            conn = tenancy.control_connect()
            try:
                logos._prune(conn)
            finally:
                conn.close()
            self.assertIsNone(admin.execute(
                "SELECT 1 FROM logo_cache WHERE url LIKE '%%acmestale%%'"
            ).fetchone())
        finally:
            admin.execute("DELETE FROM logo_cache")
            admin.close()


if __name__ == "__main__":
    unittest.main()
