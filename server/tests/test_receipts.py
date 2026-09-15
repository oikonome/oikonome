"""Receipts: upload validation, attachment-only mode without an
LLM, vision parse with a mocked backend, line-item tagging, expense
report, nightly sweep."""

import json
import unittest

import httpx

from oikonome.engine import budget, receipts

from .util import TODAY, add_txn, make_db, write_config

LLM_JSON = {"merchant": "COSTCO", "date": "2026-07-10", "total": 63.00,
            "tax": 3.00, "tip": None,
            "items": [
                {"description": "PAPER TOWELS", "qty": 1, "amount": 20.00},
                {"description": "ROTISSERIE CHICKEN", "qty": 2,
                 "amount": 10.00},
                {"description": "PRINTER INK", "qty": 1, "amount": 30.00}]}


def _llm_transport(payload=LLM_JSON):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps(payload)}}]})
    return httpx.MockTransport(handler)


def _enable_llm(conn):
    cfg = budget.load_config(conn)
    cfg["llm_url"] = "http://llm.test"
    cfg["llm_model"] = "vision-model"
    budget.save_config(conn, cfg)


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.txn = add_txn(self.conn, TODAY, 63.00, "COSTCO WHOLESALE")

    def tearDown(self):
        self.conn.close()

    def test_upload_validation(self):
        with self.assertRaises(ValueError):
            receipts.add(self.conn, self.txn, b"", "image/png")
        with self.assertRaises(ValueError):
            receipts.add(self.conn, self.txn, b"x" * (5*1024*1024+1),
                         "image/png")
        with self.assertRaises(ValueError):
            receipts.add(self.conn, self.txn, b"x", "text/html")
        with self.assertRaises(ValueError):
            receipts.add(self.conn, "nope", b"x", "image/png")

    def test_attachment_only_without_llm(self):
        rid = receipts.add(self.conn, self.txn, b"\x89PNG...", "image/png")
        out = receipts.parse_pending(self.conn)
        self.assertEqual(out.get("skipped"), "no-llm")
        lst = receipts.for_txn(self.conn, self.txn)
        self.assertEqual([r["status"] for r in lst], ["uploaded"])
        self.assertEqual(str(lst[0]["id"]), rid)

    def test_parse_tag_and_report(self):
        _enable_llm(self.conn)
        rid = receipts.add(self.conn, self.txn, b"\x89PNG...", "image/png")
        out = receipts.parse_one(self.conn, rid,
                                 transport=_llm_transport())
        self.assertEqual((out["status"], out["items"]), ("parsed", 3))
        lst = receipts.for_txn(self.conn, self.txn)[0]
        self.assertEqual(lst["status"], "parsed")
        self.assertEqual(lst["parsed"]["merchant"], "COSTCO")
        self.assertEqual(len(lst["items"]), 3)
        # tag the ink as business → report finds exactly it
        receipts.set_tag(self.conn, rid, 2, "business")
        rep = receipts.report(self.conn, "business", TODAY.year, TODAY.month)
        self.assertEqual(rep["count"], 1)
        self.assertEqual(rep["total"], 30.00)
        self.assertEqual(rep["rows"][0]["payee"], "COSTCO WHOLESALE")
        # untagged month elsewhere: empty
        rep2 = receipts.report(self.conn, "business",
                               TODAY.year - 1, TODAY.month)
        self.assertEqual(rep2["count"], 0)

    def test_parse_failure_recorded(self):
        _enable_llm(self.conn)
        rid = receipts.add(self.conn, self.txn, b"\x89PNG...", "image/png")
        bad = httpx.MockTransport(lambda r: httpx.Response(200, json={
            "choices": [{"message": {"content": "not json"}}]}))
        out = receipts.parse_one(self.conn, rid, transport=bad)
        self.assertEqual(out["status"], "failed")
        lst = receipts.for_txn(self.conn, self.txn)[0]
        self.assertEqual(lst["status"], "failed")
        self.assertTrue(lst["error"])

    def test_pending_sweep_parses(self):
        # PDFs rasterize and parse in the sweep too; a corrupt one
        # records failed (with a reason) instead of silently sticking around
        _enable_llm(self.conn)
        receipts.add(self.conn, self.txn, b"\x89PNG...", "image/png")
        receipts.add(self.conn, self.txn, b"%PDF-garbage", "application/pdf")
        out = receipts.parse_pending(self.conn,
                                     transport=_llm_transport())
        self.assertEqual((out["parsed"], out["failed"]), (1, 1))
        statuses = sorted(r["status"]
                          for r in receipts.for_txn(self.conn, self.txn))
        self.assertEqual(statuses, ["failed", "parsed"])

    def test_delete(self):
        rid = receipts.add(self.conn, self.txn, b"\x89PNG...", "image/png")
        self.assertEqual(receipts.delete(self.conn, rid), 1)
        self.assertEqual(receipts.for_txn(self.conn, self.txn), [])


if __name__ == "__main__":
    unittest.main()


# PDFs rasterize and parse; a dedicated vision model overrides the
# chat model; text-only-model failures say what to do; stale 'parsing' rows
# get reclaimed by the sweep.
def _tiny_pdf() -> bytes:
    import io

    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument.new()
    doc.new_page(200, 200)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


class VisionParseTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.txn = add_txn(self.conn, TODAY, 63.00, "COSTCO WHOLESALE")

    def tearDown(self):
        self.conn.close()

    def test_pdf_rasterizes_and_parses(self):
        _enable_llm(self.conn)
        rid = receipts.add(self.conn, self.txn, _tiny_pdf(), "application/pdf")
        out = receipts.parse_one(self.conn, rid, transport=_llm_transport())
        self.assertEqual((out["status"], out["items"]), ("parsed", 3))

    def test_vision_model_overrides_chat_model(self):
        _enable_llm(self.conn)
        cfg = budget.load_config(self.conn)
        cfg["llm_vision_model"] = "qwen2.5vl:3b"
        budget.save_config(self.conn, cfg)
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["model"] = json.loads(request.content)["model"]
            return httpx.Response(200, json={"choices": [{"message": {
                "content": json.dumps(LLM_JSON)}}]})

        rid = receipts.add(self.conn, self.txn, b"\x89PNG...", "image/png")
        receipts.parse_one(self.conn, rid,
                           transport=httpx.MockTransport(handler))
        self.assertEqual(seen["model"], "qwen2.5vl:3b")

    def test_text_only_model_failure_is_actionable(self):
        _enable_llm(self.conn)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={
                "error": "model 'qwen2.5:1.5b' does not support images"})

        rid = receipts.add(self.conn, self.txn, b"\x89PNG...", "image/png")
        out = receipts.parse_one(self.conn, rid,
                                 transport=httpx.MockTransport(handler))
        self.assertEqual(out["status"], "failed")
        row = receipts.for_txn(self.conn, self.txn)[0]
        self.assertIn("vision-capable model", row["error"])
        self.assertIn("Settings", row["error"])

    def test_stale_parsing_reclaimed_by_sweep(self):
        _enable_llm(self.conn)
        rid = receipts.add(self.conn, self.txn, b"\x89PNG...", "image/png")
        self.conn.execute(
            "UPDATE receipts SET status='parsing', "
            "parsed_at=now() - interval '20 minutes' WHERE id=%s::uuid",
            (rid,))
        out = receipts.parse_pending(self.conn, transport=_llm_transport())
        self.assertEqual(out["parsed"], 1)
        # a FRESH parsing row is left alone (another thread owns it)
        rid2 = receipts.add(self.conn, self.txn, b"\x89PNG...", "image/png")
        self.conn.execute(
            "UPDATE receipts SET status='parsing', parsed_at=now() "
            "WHERE id=%s::uuid", (rid2,))
        out = receipts.parse_pending(self.conn, transport=_llm_transport())
        self.assertEqual(out["parsed"], 0)


class UploadEndpointTests(unittest.TestCase):
    """Route shape: exercise the real endpoint, so a stray helper
    absorbing the upload route's decorator cannot pass unnoticed."""

    @classmethod
    def setUpClass(cls):
        import os
        import uuid as _uuid

        from fastapi.testclient import TestClient

        from oikonome.db import tenancy as _tenancy

        from .util import seed_accounts, write_config as _wc
        os.environ["OIKONOME_DEV"] = "1"
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"rcpt-{_uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = _tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            _wc(conn)
            from .util import add_txn as _add
            cls.txn = _add(conn, TODAY, 63.00, "COSTCO WHOLESALE",
                           account="chk")
        finally:
            conn.close()

    def test_upload_route_accepts_multipart(self):
        r = self.client.post(
            f"/api/transactions/{self.txn}/receipt",
            files={"file": ("r.png", b"\x89PNG...", "image/png")})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # no LLM configured on this tenant → stays attachment-only
        self.assertEqual(body["receipts"][0]["status"], "uploaded")

    def test_parse_route_requires_llm(self):
        r = self.client.post(
            f"/api/transactions/{self.txn}/receipt",
            files={"file": ("r.png", b"\x89PNG...", "image/png")})
        rid = r.json()["id"]
        pr = self.client.post(f"/api/receipts/{rid}/parse")
        self.assertEqual(pr.status_code, 400)
        self.assertIn("no LLM configured", pr.text)

    def test_image_serve_never_trusts_the_stored_type(self):
        """A stored receipt's mime is data, not policy: a real image type
        renders inline with sniffing off, anything else — including a row
        a restore or an old upload planted with text/html — is a named
        download under a neutral type, so origin-hosted attacker HTML
        cannot render from this route."""
        r = self.client.post(
            f"/api/transactions/{self.txn}/receipt",
            files={"file": ("r.png", b"\x89PNG...", "image/png")})
        rid = r.json()["id"]
        img = self.client.get(f"/api/receipts/{rid}/image")
        self.assertEqual(img.status_code, 200)
        self.assertEqual(img.headers["content-type"].split(";")[0], "image/png")
        self.assertEqual(img.headers["content-disposition"], "inline")
        self.assertEqual(img.headers["x-content-type-options"], "nosniff")
        from oikonome.db import tenancy as _tenancy
        tid = self.client.get("/api/me").json()["tenant_id"]
        conn = _tenancy.tenant_connect(tid)
        try:
            conn.execute("UPDATE receipts SET mime='text/html' WHERE id=%s",
                         (rid,))
        finally:
            conn.close()
        bad = self.client.get(f"/api/receipts/{rid}/image")
        self.assertEqual(bad.status_code, 200)
        self.assertEqual(bad.headers["content-type"].split(";")[0],
                         "application/octet-stream")
        self.assertTrue(bad.headers["content-disposition"]
                        .startswith("attachment; filename="))

    def test_oversize_receipt_streams_to_413_at_the_receipt_cap(self):
        # the stream cap is the RECEIPT limit (5 MB), not the general
        # import cap — the server must not buffer 50 MB just to refuse
        r = self.client.post(
            f"/api/transactions/{self.txn}/receipt",
            files={"file": ("big.png", b"\x89" + b"x" * (5 * 1024 * 1024),
                            "image/png")})
        self.assertEqual(r.status_code, 413)
        self.assertIn("5 MB", r.text)


class ParseClaimTests(unittest.TestCase):
    """Upload + parse-now each spawn a thread into parse_one; without
    a single-flight claim, two concurrent runs DELETE line items out from
    under each other and INSERT twice."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _enable_llm(self.conn)
        self.txn = add_txn(self.conn, TODAY, 63.00, "COSTCO WHOLESALE")
        self.rid = receipts.add(self.conn, self.txn, b"\x89PNG...",
                                "image/png")

    def tearDown(self):
        self.conn.close()

    def _status(self):
        return receipts.for_txn(self.conn, self.txn)[0]["status"]

    def test_claim_is_single_flight(self):
        self.assertTrue(receipts.claim(self.conn, self.rid))
        self.assertEqual(self._status(), "parsing")
        # the second claimant loses while the first is live…
        self.assertFalse(receipts.claim(self.conn, self.rid))
        # …but a died thread's stale claim is reclaimable
        self.conn.execute(
            "UPDATE receipts SET parsed_at=now() - interval '20 minutes' "
            "WHERE id=%s::uuid", (self.rid,))
        self.assertTrue(receipts.claim(self.conn, self.rid))

    def test_unclaimed_parse_backs_off_a_live_parse(self):
        # first parse succeeded and wrote items
        out = receipts.parse_one(self.conn, self.rid,
                                 transport=_llm_transport())
        self.assertEqual(out["items"], 3)
        # another thread now holds a fresh claim; an unclaimed parse_one
        # (the sweep path) must back off WITHOUT touching the line items
        self.conn.execute(
            "UPDATE receipts SET status='parsing', parsed_at=now() "
            "WHERE id=%s::uuid", (self.rid,))
        out = receipts.parse_one(self.conn, self.rid,
                                 transport=_llm_transport())
        self.assertEqual(out, {"status": "parsing"})
        items = self.conn.execute(
            "SELECT COUNT(*) AS n FROM receipt_items WHERE receipt_id "
            "= %s::uuid", (self.rid,)).fetchone()["n"]
        self.assertEqual(items, 3)                    # untouched

    def test_claimed_parse_proceeds(self):
        # the API routes claim first, then hand the thread claimed=True —
        # the parse must run even though status is already 'parsing'
        self.assertTrue(receipts.claim(self.conn, self.rid))
        out = receipts.parse_one(self.conn, self.rid, claimed=True,
                                 transport=_llm_transport())
        self.assertEqual((out["status"], out["items"]), ("parsed", 3))


# The vision model extracts only — any category field it returns is
# IGNORED; items are searchable/groupable instead. The tag machinery
# (manual set_tag + expense report) stays.
LLM_JSON_CATS = {"merchant": "COSTCO", "date": "2026-07-10", "total": 63.00,
                 "tax": 3.00, "tip": None,
                 "items": [
                     {"description": "PAPER TOWELS", "qty": 1,
                      "amount": 20.00, "category": "household"},
                     {"description": "ROTISSERIE CHICKEN", "qty": 2,
                      "amount": 10.00, "category": "dining"},
                     {"description": "PRINTER INK", "qty": 1,
                      "amount": 30.00, "category": "electronics"}]}


class ItemsNotAutoTaggedTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.txn = add_txn(self.conn, TODAY, 63.00, "COSTCO WHOLESALE")

    def tearDown(self):
        self.conn.close()

    def test_model_categories_are_ignored(self):
        _enable_llm(self.conn)
        rid = receipts.add(self.conn, self.txn, b"\x89PNG...", "image/png")
        receipts.parse_one(self.conn, rid,
                           transport=_llm_transport(LLM_JSON_CATS))
        items = receipts.for_txn(self.conn, self.txn)[0]["items"]
        self.assertTrue(all(i["tag"] == "" for i in items))
        # manual tagging + the expense report still work on top
        receipts.set_tag(self.conn, rid, 2, "business")
        rep = receipts.report(self.conn, "business", TODAY.year, TODAY.month)
        self.assertEqual((rep["count"], rep["total"]), (1, 30.00))


class ItemSearchTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.txn = add_txn(self.conn, TODAY, 63.00, "COSTCO WHOLESALE")
        _enable_llm(self.conn)
        self.rid = receipts.add(self.conn, self.txn, b"\x89PNG...",
                                "image/png")
        receipts.parse_one(self.conn, self.rid, transport=_llm_transport())

    def tearDown(self):
        self.conn.close()

    def test_search_and_grouping(self):
        out = receipts.search_items(self.conn, q="paper")
        self.assertEqual(len(out["rows"]), 1)
        self.assertEqual(out["rows"][0]["description"], "PAPER TOWELS")
        self.assertEqual(out["rows"][0]["payee"], "COSTCO WHOLESALE")
        out = receipts.search_items(self.conn, group="item")
        self.assertEqual(len(out["groups"]), 3)
        top = out["groups"][0]
        self.assertEqual(top["key"], "printer ink")     # biggest total
        self.assertEqual(top["total"], 30.00)
        out = receipts.search_items(self.conn, group="merchant")
        self.assertEqual(out["groups"][0]["key"], "COSTCO WHOLESALE")
        self.assertEqual(out["groups"][0]["n"], 3)
        # unknown group key falls back to item, no crash
        out = receipts.search_items(self.conn, group="nonsense")
        self.assertEqual(len(out["groups"]), 3)

    def test_transactions_search_matches_line_items(self):
        from oikonome.web import data
        rows, total, _, _, _, _hits = data.search_transactions(self.conn, "rotisserie")
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["id"], self.txn)
        rows = data.transactions(self.conn, TODAY.year, TODAY.month,
                                 search="rotisserie")
        self.assertEqual([r["id"] for r in rows], [self.txn])
        rows, total, _, _, _, _hits = data.search_transactions(self.conn, "zzz-nothing")
        self.assertEqual(total, 0)


class UnreadablePdfTests(unittest.TestCase):
    """A PDF the rasterizer cannot open is BAD INPUT, not a server fault.

    The tax-document upload door turns ValueError into a 400 and catches
    nothing else, so a pdfium exception escaping here would make a corrupt
    or crafted W-2 scan a 500 with a stack trace behind it."""

    def test_a_corrupt_pdf_reads_as_bad_input(self):
        with self.assertRaises(ValueError) as cm:
            receipts._pdf_first_page_png(b"%PDF-1.4\nnot really a pdf")
        self.assertIn("PDF", str(cm.exception))

    def test_an_empty_file_reads_as_bad_input(self):
        with self.assertRaises(ValueError):
            receipts._pdf_first_page_png(b"")


class ImageShrinkTests(unittest.TestCase):
    """A 3072x4080 phone photo alone exceeds ollama's default
    4,096-token context, so big images downscale before the vision call
    and backend error bodies surface."""

    def _png(self, w, h):
        import io

        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (w, h), "white").save(buf, format="PNG")
        return buf.getvalue()

    def test_big_image_shrinks_small_passes_through(self):
        import io

        from PIL import Image
        big = self._png(3072, 4080)
        out, mime = receipts._shrink_image(big, "image/png")
        self.assertEqual(mime, "image/jpeg")
        self.assertLessEqual(max(Image.open(io.BytesIO(out)).size),
                             receipts.MAX_VISION_PX)
        small = self._png(800, 1200)
        out, mime = receipts._shrink_image(small, "image/png")
        self.assertEqual((out, mime), (small, "image/png"))
        # garbage passes through for the model call to reject
        out, mime = receipts._shrink_image(b"not an image", "image/png")
        self.assertEqual(out, b"not an image")

    def test_parse_sends_shrunken_image(self):
        conn = make_db()
        try:
            write_config(conn)
            _enable_llm(conn)
            txn = add_txn(conn, TODAY, 10.0, "HARBOR GRILL", account="chk")
            rid = receipts.add(conn, txn, self._png(3000, 4000), "image/png")
            seen = {}

            def handler(request: httpx.Request) -> httpx.Response:
                seen["bytes"] = len(request.content)
                return httpx.Response(200, json={"choices": [{"message": {
                    "content": json.dumps(LLM_JSON)}}]})

            out = receipts.parse_one(conn, rid,
                                     transport=httpx.MockTransport(handler))
            self.assertEqual(out["status"], "parsed")
            # a 3000×4000 PNG is several hundred KB even blank; the shrunk
            # JPEG payload must come in far under it
            self.assertLess(seen["bytes"], 200_000)
        finally:
            conn.close()

    def test_context_error_is_actionable(self):
        conn = make_db()
        try:
            write_config(conn)
            _enable_llm(conn)
            txn = add_txn(conn, TODAY, 10.0, "HARBOR GRILL", account="chk")
            rid = receipts.add(conn, txn, b"\x89PNG...", "image/png")
            bad = httpx.MockTransport(lambda r: httpx.Response(400, json={
                "error": {"message": "request (4133 tokens) exceeds the "
                                     "available context size (4096 tokens)",
                          "type": "exceed_context_size_error"}}))
            receipts.parse_one(conn, rid, transport=bad)
            row = receipts.for_txn(conn, txn)[0]
            self.assertIn("context window", row["error"])
            self.assertIn("OLLAMA_CONTEXT_LENGTH", row["error"])
        finally:
            conn.close()

    def test_a_backend_body_never_reaches_the_receipt_panel(self):
        """`receipts.error` is rendered to the household, so it says what
        WE decided, never what the AI backend said.

        The backend's body is written by whatever endpoint the household
        pointed the vision role at, and it routinely echoes back the
        prompt — which carries the receipt's own contents — the endpoint
        URL with its credentials, or a quoted API key. Classified message
        on screen; raw text to the server log only."""
        conn = make_db()
        try:
            write_config(conn)
            _enable_llm(conn)
            cfg = budget.load_config(conn)
            cfg["llm_vision_model"] = "some-vl"
            budget.save_config(conn, cfg)
            txn = add_txn(conn, TODAY, 10.0, "HARBOR GRILL", account="chk")
            rid = receipts.add(conn, txn, b"\x89PNG...", "image/png")
            secret = "sk-live-7f3a tenant@example.dev model runner quit"
            bad = httpx.MockTransport(lambda r: httpx.Response(500, json={
                "error": secret}))
            with self.assertLogs("oikonome.receipts", "WARNING") as logs:
                receipts.parse_one(conn, rid, transport=bad)
            row = receipts.for_txn(conn, txn)[0]
            for leak in ("sk-live-7f3a", "tenant@example.dev", "runner quit",
                         "llm.test", "HTTPStatusError"):
                self.assertNotIn(leak, row["error"])
            self.assertIn("AI backend", row["error"])
            # …and the operator still gets the whole thing
            self.assertIn(secret, "\n".join(logs.output))
        finally:
            conn.close()

    def test_the_apps_own_refusal_is_shown_in_its_own_words(self):
        """The classifier must not swallow the messages this module wrote
        itself — those name a cause and a fix and no backend supplied a
        word of them."""
        msg = receipts._friendly_error(
            receipts.ReceiptRefusal(receipts.BUSY_ERROR), True)
        self.assertEqual(msg, receipts.BUSY_ERROR)

    def test_each_backend_failure_names_its_own_cause(self):
        """Four classes, four different things to do about them — a single
        'it failed' would send everyone to the same wrong place."""
        import httpx as _httpx

        def _status(code):
            req = _httpx.Request("POST", "http://llm.test/v1/chat")
            return _httpx.HTTPStatusError(
                "boom", request=req,
                response=_httpx.Response(code, text="backend detail",
                                         request=req))
        self.assertIn("didn't answer in time", receipts._friendly_error(
            _httpx.ReadTimeout("timed out"), True))
        self.assertIn("credentials", receipts._friendly_error(_status(401),
                                                              True))
        self.assertIn("unreachable", receipts._friendly_error(
            _httpx.ConnectError("connection refused"), True))
        self.assertIn("couldn't be read", receipts._friendly_error(
            ValueError("items not a list"), True))
