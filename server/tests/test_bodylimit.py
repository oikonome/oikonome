"""A request body has a ceiling.

Starlette buffers the entire body to build Body(...)/Form(...) BEFORE the
handler or its auth dependency runs, so an unauthenticated POST of a
multi-gigabyte string is enough to OOM a shared container and take every
tenant with it. The webhooks and upload paths refuse to read unbounded;
the ordinary JSON/form routes owe the same ceiling.
"""
import os
import unittest

from fastapi.testclient import TestClient

from oikonome.web import security

from .util import _ensure_db


MULTIPART = "multipart/form-data; boundary=x"


class _Drain:
    """An inner ASGI app that reads the body to the end and reports how much
    of it the middleware actually let through."""

    def __init__(self):
        self.received = 0
        self.disconnected = False

    async def __call__(self, scope, receive, send):
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                self.disconnected = True
                break
            self.received += len(msg.get("body") or b"")
            if not msg.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-length", b"2")]})
        await send({"type": "http.response.body", "body": b"ok"})


def _drive_asgi(path, chunks, inner):
    """POST a chunked (content-length-free) multipart body through the real
    middleware. A test client hands the whole body over at once and would
    never exercise the streaming count."""
    import asyncio

    import oikonome.web.app as appmod

    state = {"i": 0}

    async def receive():
        i = state["i"]
        state["i"] += 1
        if i < len(chunks):
            return {"type": "http.request", "body": chunks[i],
                    "more_body": i + 1 < len(chunks)}
        return {"type": "http.disconnect"}

    scope = {"type": "http", "method": "POST", "path": path,
             "headers": [(b"content-type", MULTIPART.encode())],
             "client": ("192.0.2.9", 40000), "app": appmod.app}

    async def go():
        mw = security.BodySizeLimitMiddleware(inner)
        await asyncio.wait_for(mw(scope, receive, lambda m: _noop()), 5.0)

    async def _noop():
        return None

    asyncio.run(go())


class BodyLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)

    def test_oversized_unauthenticated_post_is_refused_with_413(self):
        """/api/invite/claim needs no session — it is the cheapest OOM."""
        big = "a" * (security.BODY_LIMIT + 1024)
        r = self.client.post("/api/invite/claim", json={"token": big})
        self.assertEqual(r.status_code, 413)
        self.assertIn("too large", r.text)

    def test_a_normal_body_is_untouched(self):
        r = self.client.post("/api/invite/claim", json={"token": "short"})
        self.assertNotEqual(r.status_code, 413)

    def test_uploads_keep_their_larger_ceiling(self):
        """A 50 MB statement import must still be possible — this is a
        ceiling on ordinary JSON, not a new upload limit."""
        self.assertGreater(
            security._body_limit_for("multipart/form-data; boundary=x",
                                     upload_route=True),
            security.BODY_LIMIT)
        self.assertEqual(security._body_limit_for("application/json"),
                         security.BODY_LIMIT)
        self.assertEqual(security._body_limit_for(""), security.BODY_LIMIT)

    def test_multipart_at_a_route_with_no_file_gets_a_form_ceiling(self):
        """The upload budget belongs to the routes that store bytes, not to
        the content-type. /login parses multipart too."""
        self.assertEqual(
            security._body_limit_for("multipart/form-data; boundary=x",
                                     upload_route=False),
            security.BODY_LIMIT)

    def test_per_route_upload_caps_still_produce_their_own_error(self):
        """The receipt route caps at 5 MB and says so; the middleware must
        not preempt that with a generic 413."""
        from oikonome.engine import receipts
        self.assertLess(receipts.MAX_IMAGE, security.UPLOAD_BODY_LIMIT)

    def test_a_lying_content_length_does_not_get_through(self):
        """No content-length (chunked) must still be counted as it streams,
        or the header check alone would leave the hole open."""
        big = b"a" * (security.BODY_LIMIT + 4096)

        def chunks():
            yield big

        r = self.client.post("/api/invite/claim", content=chunks(),
                             headers={"content-type": "application/json"})
        self.assertNotEqual(r.status_code, 200)


class MultipartAtANonUploadDoorTests(unittest.TestCase):
    """`multipart/form-data` at a door that takes no file is just a form.

    Every Form(...) route parses multipart, /login and /signup included —
    both reachable with no account, no cookie and no Origin header. A
    ceiling keyed on the content-type alone would let each of those doors
    accept a 64 MB body for a request whose largest legitimate field is a
    password: an anonymous slowloris getting 64 MB of parser per
    connection.

    The doors are the ones the derived upload matcher already finds, so the
    ceiling and the ingest cap can never disagree about what an upload is.
    """

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)

    # comfortably over the form ceiling, comfortably under the upload one
    BIG = security.BODY_LIMIT + 512 * 1024

    def test_an_oversized_multipart_login_is_refused(self):
        # a file PART does not make /login an upload door — the matcher
        # reads the route's signature, not the shape of the request
        r = self.client.post(
            "/login", data={"email": "a@b.co", "password": "x" * self.BIG},
            files={"file": ("x.txt", b"x")})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r.json()["detail"], "request body too large")

    def test_a_body_that_size_is_accepted_at_a_real_upload_door(self):
        """Proof the refusal above is about the ROUTE and not the size: the
        same number of bytes at a door that takes a file reaches the door,
        and is turned away by its auth rather than by the ceiling."""
        r = self.client.post(
            "/api/import",
            files={"file": ("statement.csv", b"a" * self.BIG)})
        self.assertNotEqual(r.status_code, 413)
        self.assertEqual(r.status_code, 401)

    def test_a_streamed_body_is_cut_off_at_the_form_ceiling_too(self):
        """The attack has no content-length: it opens a multipart POST and
        keeps sending. So the ceiling is also counted as the body streams —
        the header check alone leaves the actual hole open."""
        chunks = [b"a" * 256 * 1024] * 8      # 2 MB, no content-length
        reader = _Drain()
        _drive_asgi("/login", chunks, reader)
        self.assertLessEqual(reader.received, security.BODY_LIMIT)
        self.assertTrue(reader.disconnected)

    def test_a_streamed_upload_that_size_is_delivered_in_full(self):
        chunks = [b"a" * 256 * 1024] * 8
        reader = _Drain()
        _drive_asgi("/api/import", chunks, reader)
        self.assertEqual(reader.received, sum(len(c) for c in chunks))
        self.assertFalse(reader.disconnected)

    def test_an_ordinary_login_form_is_untouched(self):
        """The ceiling has to sit above what the doors actually post. Every
        non-upload Form field in the product is a short string — the longest
        capped anywhere is a 500-character note — so a megabyte is three
        orders of magnitude of headroom."""
        r = self.client.post("/login", data={"email": "a@b.co",
                                             "password": "hunter2"},
                             files={"file": ("x.txt", b"x")})
        self.assertNotEqual(r.status_code, 413)

    def test_a_router_it_cannot_read_widens_the_ceiling_rather_than_narrows(
            self):
        """Fail OPEN. A derivation that comes back empty must keep the
        64 MB upload ceiling, not silently cap every upload in the product
        at a megabyte — a broken derivation has to break loudly or not at
        all."""
        from fastapi import FastAPI, Form
        empty = FastAPI()

        @empty.post("/nothing")
        def nothing(x: str = Form("")):
            return {"ok": True}

        self.assertEqual(security.upload_route_paths(empty), [])
        self.assertTrue(security._accepts_upload({"app": empty,
                                                  "path": "/nothing"}))
        self.assertTrue(security._accepts_upload({"path": "/nothing"}))

    def test_the_real_app_never_lands_on_that_fallback(self):
        import oikonome.web.app as appmod
        self.assertTrue(security.upload_route_paths(appmod.app))


if __name__ == "__main__":
    unittest.main()
