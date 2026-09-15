"""An upload the instance refuses is refused before it is received.

The upload doors carry per-route rate limits, and a route-level limiter is a
dependency: FastAPI solves dependencies only after it has called
`request.form()`, so by the time one answers, Starlette has already read the
whole multipart off the wire, spooled the parts over 1 MB to disk and kept the
rest resident. A caller looping 64 MB multiparts therefore pays nothing for the
429s — every refused request is materialized first, and the resident set
grows with the number of connections rather than with the hourly budget. That
is the same shape of mistake as a door that locks after the expensive half has
already run.

So the ingest budget lives in the ASGI middleware, ahead of the parse. These
tests are about ORDER, not about status codes: a refused upload must leave the
multipart parser and the handler untouched. The route budgets stay, bounding
the parse and the work that follows for the requests that ARE let in.
"""

import unittest
import uuid

from fastapi.testclient import TestClient
from starlette.formparsers import MultiPartParser

from oikonome.web import pages, security

from .util import _ensure_db


def _client() -> TestClient:
    import os
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    client = TestClient(appmod.app)
    r = client.post("/api/signup", data={
        "email": f"ingest-{uuid.uuid4().hex[:8]}@example.dev",
        "password": "correct-horse-battery"})
    assert r.status_code == 200, r.text
    return client


class _Counters:
    """How far into the request the server actually got: one count for
    Starlette parsing the multipart, one for the handler starting work."""

    def __init__(self):
        self.parsed = 0
        self.handled = 0
        self._parse = MultiPartParser.parse
        self._read = pages.read_capped

    def __enter__(self):
        async def parse(inner_self):
            self.parsed += 1
            return await self._parse(inner_self)

        async def read_capped(*a, **kw):
            self.handled += 1
            return await self._read(*a, **kw)

        MultiPartParser.parse = parse
        pages.read_capped = read_capped
        return self

    def __exit__(self, *exc):
        MultiPartParser.parse = self._parse
        pages.read_capped = self._read


def _upload(client, path="/api/import"):
    return client.post(path, files={
        "file": ("rows.csv", b"date,amount,name\n", "text/csv")})


class AnUploadOverBudgetIsRefusedBeforeItIsRead(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = _client()

    def setUp(self):
        # the ingest window is process-global and keyed per client IP
        security._limiter._hits.clear()
        security._reset_ingest_slots()

    tearDown = setUp

    def test_an_allowed_upload_is_read_and_handled(self):
        """The control: without this, every assertion below passes on a
        server that simply never accepts an upload at all."""
        with _Counters() as c:
            r = _upload(self.client)
            self.assertNotIn(r.status_code, (429, 503), r.text)
            self.assertEqual(c.parsed, 1)
            self.assertEqual(c.handled, 1)
        # and the ingest slot is handed back, not held for the next caller
        self.assertEqual(security._ingest_inflight, 0)

    def test_the_body_of_an_over_budget_upload_is_never_parsed(self):
        # Spend the ingest window on the upload door itself. The route's own
        # limiter is exhausted by the same loop, so the STATUS below cannot
        # tell the two apart — the parse counter can, and that is the claim:
        # a refusal that happens before the body is read.
        for _ in range(security.UPLOAD_INGEST_LIMIT):
            _upload(self.client)
        with _Counters() as c:
            r = _upload(self.client)
            self.assertEqual(r.status_code, 429, r.text)
            self.assertEqual(c.parsed, 0)
            self.assertEqual(c.handled, 0)

    def test_an_upload_past_the_in_flight_cap_is_never_parsed(self):
        """A rate limit bounds requests over time; only an in-flight cap
        bounds how many bodies are resident at once, which is the number
        that decides whether the box survives."""
        # from distinct addresses, because no ONE client may hold them all
        ips = [f"203.0.113.{n}" for n in
               range(security.UPLOAD_INGEST_CONCURRENCY)]
        self.assertTrue(all(security._take_ingest_slot(ip) for ip in ips))
        try:
            with _Counters() as c:
                r = _upload(self.client)
                self.assertEqual(r.status_code, 503, r.text)
                self.assertEqual(c.parsed, 0)
                self.assertEqual(c.handled, 0)
        finally:
            for ip in ips:
                security._free_ingest_slot(ip)

    def test_ordinary_json_requests_do_not_spend_the_upload_budget(self):
        """The gate is on multipart ingest. A signed-in client browsing the
        app must not be able to lock itself out of uploading."""
        for _ in range(security.UPLOAD_INGEST_LIMIT + 5):
            self.client.get("/api/me")
        with _Counters() as c:
            r = _upload(self.client)
            self.assertNotIn(r.status_code, (429, 503), r.text)
            self.assertEqual(c.parsed, 1)


if __name__ == "__main__":
    unittest.main()
