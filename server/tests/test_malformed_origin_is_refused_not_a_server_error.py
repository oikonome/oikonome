"""A write whose Origin or Referer does not parse is refused as
cross-origin, never answered with a server error.

urlsplit raises on a malformed bracketed host, and the header is whatever
any client sends; the same-origin check must treat an unparseable claim as
a claim that is not this host.
"""

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from oikonome.web.security import OriginCheckMiddleware, claim_netloc


def _client():
    app = FastAPI()
    app.add_middleware(OriginCheckMiddleware)

    @app.post("/w")
    def w():
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


class MalformedOriginTests(unittest.TestCase):
    def test_unparseable_claims_have_no_netloc(self):
        for claim in ("http://[x", "http://a]b"):
            self.assertIsNone(claim_netloc(claim))
        self.assertEqual(claim_netloc("https://h.test:8/x"), "h.test:8")

    def test_a_malformed_origin_or_referer_is_a_403(self):
        c = _client()
        for hdr in ("origin", "referer"):
            for claim in ("http://[x", "http://a]b"):
                r = c.post("/w", headers={hdr: claim})
                self.assertEqual(r.status_code, 403, (hdr, claim, r.text))

    def test_same_origin_and_absent_claims_still_pass(self):
        c = _client()
        self.assertEqual(c.post("/w").status_code, 200)
        self.assertEqual(c.post(
            "/w", headers={"origin": "http://testserver"}).status_code, 200)


class _Req:
    """Just enough of a Request for the header-only guards."""

    def __init__(self, headers):
        self.headers = {k.lower(): v for k, v in headers.items()}


class MalformedClaimInOtherGuardsTests(unittest.TestCase):
    def test_sign_out_with_a_malformed_claim_is_not_a_person(self):
        from oikonome.web.app import _started_by_a_person
        for hdr in ("origin", "referer"):
            for claim in ("http://[x", "http://a]b"):
                self.assertFalse(_started_by_a_person(
                    _Req({hdr: claim, "host": "testserver"})), (hdr, claim))
        self.assertTrue(_started_by_a_person(
            _Req({"origin": "http://testserver", "host": "testserver"})))
        self.assertTrue(_started_by_a_person(_Req({"host": "testserver"})))

    def test_download_with_a_malformed_referer_is_a_403(self):
        from fastapi import HTTPException
        from oikonome.web.api import _same_site_download
        for claim in ("http://[x", "http://a]b"):
            with self.assertRaises(HTTPException) as cm:
                _same_site_download(_Req({"referer": claim,
                                          "host": "testserver"}))
            self.assertEqual(cm.exception.status_code, 403, claim)
        _same_site_download(_Req({"referer": "http://testserver/x",
                                  "host": "testserver"}))
        _same_site_download(_Req({"host": "testserver"}))


if __name__ == "__main__":
    unittest.main()
