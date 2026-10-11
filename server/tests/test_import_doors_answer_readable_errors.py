"""Two import doors answer a readable error instead of a bare 500.

* Tax-document analyze: the W-2 image and 1040 paths ask the AI backend in
  Settings → AI. A backend that is down answers 503, one that answers with an
  HTTP error or an unusable reply answers 502 — each naming the kind of
  failure and pointing at Settings → AI, never quoting the reply.
* Mapped CSV import: the token and column names must be text. A number or a
  list is a malformed request (400), and it must not consume the staged
  upload.
"""

import os
import unittest
import uuid
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from .util import _ensure_db


class _Client(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        security._limiter._hits.clear()
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"imperr-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})


class TaxdocBackendFailures(_Client):
    def _analyze_raising(self, exc):
        with mock.patch("oikonome.engine.taxdocs.analyze", side_effect=exc):
            return self.client.post(
                "/api/import/taxdoc/analyze",
                files={"file": ("w2.png", b"\x89PNG....", "image/png")})

    def test_an_unreachable_backend_is_a_503(self):
        r = self._analyze_raising(httpx.ConnectError("refused"))
        self.assertEqual(r.status_code, 503, r.text)
        self.assertIn("Settings → AI", r.json()["detail"])
        self.assertIn("ConnectError", r.json()["detail"])

    def test_a_backend_error_or_unusable_reply_is_a_502(self):
        from oikonome.engine.llm_categorize import LLMError
        req = httpx.Request("POST", "http://llm.invalid/v1/chat/completions")
        resp = httpx.Response(404, request=req)
        for exc, cause in (
                (httpx.HTTPStatusError("nf", request=req, response=resp),
                 "HTTP 404"),
                (LLMError("LLM reply too large"), "LLMError"),
                (KeyError("choices"), "KeyError")):
            r = self._analyze_raising(exc)
            self.assertEqual(r.status_code, 502, r.text)
            self.assertIn("Settings → AI", r.json()["detail"])
            self.assertIn(cause, r.json()["detail"])


class MappedImportBodyTypes(_Client):
    def test_a_non_string_token_or_column_is_a_400(self):
        base = {"token": "t", "date_col": "Date", "name_col": "Desc",
                "amount_col": "Amt"}
        for k, v in (("token", 7), ("token", {"a": 1}), ("token", ["x"]),
                     ("date_col", ["Date"]), ("amount_col", 3)):
            r = self.client.post("/api/import/mapped", json={**base, k: v})
            self.assertEqual(r.status_code, 400, f"{k}={v!r}: {r.text}")
            self.assertIn(k, r.json()["detail"])

    def test_a_well_typed_unknown_token_still_answers_expired(self):
        r = self.client.post("/api/import/mapped", json={
            "token": "no-such", "date_col": "Date", "name_col": "Desc",
            "amount_col": "Amt"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["result"].get("expired"))


if __name__ == "__main__":
    unittest.main()
