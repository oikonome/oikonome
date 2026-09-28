"""A receipt upload gets an OPTIMIZED (auto-cropped + touched-up)
copy stored alongside the untouched original; best-effort (never blocks the
upload); the UI falls back to the original when there's no optimized copy."""

import io
import unittest

from PIL import Image, ImageDraw

from oikonome.engine import receipts

from .util import TODAY, add_txn, make_db, write_config


def _receipt_png() -> bytes:
    """A phone-photo-ish receipt: a bright paper rectangle with text lines on
    a dark table (so the auto-crop has something to crop to)."""
    im = Image.new("RGB", (400, 600), (28, 28, 30))          # dark table
    d = ImageDraw.Draw(im)
    d.rectangle([80, 60, 320, 540], fill=(242, 240, 235))    # bright paper
    for y in range(100, 520, 28):
        d.line([100, y, 300, y], fill=(35, 35, 35), width=2)  # "text"
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


class ReceiptOptimizeTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.txn = add_txn(self.conn, TODAY, 10, "STORE")

    def tearDown(self):
        self.conn.close()

    def _rec(self, rid):
        return [r for r in receipts.for_txn(self.conn, self.txn)
                if r["id"] == rid][0]

    def test_optimized_stored_cropped_and_grayscale(self):
        rid = receipts.add(self.conn, self.txn, _receipt_png(), "image/png")
        self.assertTrue(self._rec(rid)["has_optimized"])
        opt = receipts.image(self.conn, rid, variant="optimized")
        self.assertEqual(opt["mime"], "image/jpeg")
        im = Image.open(io.BytesIO(bytes(opt["image"])))
        self.assertEqual(im.mode, "L")                       # grayscale
        self.assertLess(im.size[0], 400)                     # cropped to paper

    def test_original_is_preserved_untouched(self):
        png = _receipt_png()
        rid = receipts.add(self.conn, self.txn, png, "image/png")
        orig = receipts.image(self.conn, rid)                # default original
        self.assertEqual(orig["mime"], "image/png")
        self.assertEqual(bytes(orig["image"]), png)

    def test_variant_falls_back_to_original_for_pdf(self):
        rid = receipts.add(self.conn, self.txn, b"%PDF-1.4 x", "application/pdf")
        self.assertFalse(self._rec(rid)["has_optimized"])
        opt = receipts.image(self.conn, rid, variant="optimized")
        self.assertEqual(opt["mime"], "application/pdf")     # fell back

    def test_corrupt_image_never_blocks_upload(self):
        rid = receipts.add(self.conn, self.txn, b"\x89PNG not an image",
                           "image/png")
        self.assertTrue(rid)                                 # upload succeeded
        self.assertFalse(self._rec(rid)["has_optimized"])    # just no optimized

    def test_oversized_image_skips_optimize_before_decode(self):
        # a decompression bomb must be refused BEFORE the
        # heavy Pillow decode. Proven with a tiny cap so no giant buffer is
        # allocated in the test: the 400x600 image exceeds the patched cap →
        # optimize is skipped, the original is kept, the upload is not blocked.
        import oikonome.engine.receipts as R
        png = _receipt_png()                                 # 400*600 = 240k px
        saved = R.MAX_DECODE_PX
        R.MAX_DECODE_PX = 1000
        try:
            rid = receipts.add(self.conn, self.txn, png, "image/png")
        finally:
            R.MAX_DECODE_PX = saved
        self.assertTrue(rid)                                 # upload not blocked
        self.assertFalse(self._rec(rid)["has_optimized"])    # optimize skipped
        self.assertEqual(bytes(receipts.image(self.conn, rid)["image"]), png)

    def test_optimize_pixel_ceiling_is_realistic(self):
        # the OPTIMIZE ceiling is a realistic receipt max
        # (~12 MP), well below the 40 MP hard decode backstop — a compressible
        # bomb that slips under the byte cap + MAX_DECODE_PX is still refused
        # before its full-res buffers are built. Proven with a tiny patched cap.
        import oikonome.engine.receipts as R
        png = _receipt_png()                                 # 400*600 = 240k px
        saved = R.MAX_OPTIMIZE_PX
        R.MAX_OPTIMIZE_PX = 1000
        try:
            rid = receipts.add(self.conn, self.txn, png, "image/png")
        finally:
            R.MAX_OPTIMIZE_PX = saved
        self.assertTrue(rid)
        self.assertFalse(self._rec(rid)["has_optimized"])

    def test_concurrency_gate_falls_back_to_original(self):
        # when the optimize concurrency gate is saturated, a further
        # upload keeps its untouched original instead of stacking another
        # full-res decode — a burst of those exhausts a shared container's
        # memory. The upload itself never blocks or fails.
        import oikonome.engine.receipts as R
        held = 0
        while R._OPTIMIZE_GATE.acquire(blocking=False):       # drain the gate
            held += 1
        try:
            rid = receipts.add(self.conn, self.txn, _receipt_png(), "image/png")
            self.assertTrue(rid)                              # not blocked
            self.assertFalse(self._rec(rid)["has_optimized"])  # optimize skipped
        finally:
            for _ in range(held):
                R._OPTIMIZE_GATE.release()


def _skewed_receipt_png(size=(600, 800)) -> bytes:
    """A perspective-skewed receipt photo: a bright convex quad (paper shot
    at an angle) with text lines, on a dark table."""
    im = Image.new("RGB", size, (24, 24, 26))
    d = ImageDraw.Draw(im)
    tl, tr, br, bl = (150, 90), (470, 140), (430, 720), (110, 660)
    d.polygon([tl, tr, br, bl], fill=(243, 241, 236))
    for i in range(1, 14):                          # "text" following the skew
        t = i / 15
        x0 = tl[0] + (bl[0] - tl[0]) * t + 25
        y0 = tl[1] + (bl[1] - tl[1]) * t
        x1 = tr[0] + (br[0] - tr[0]) * t - 25
        y1 = tr[1] + (br[1] - tr[1]) * t
        d.line([x0, y0, x1, y1], fill=(30, 30, 30), width=3)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


class DocumentWarpTests(unittest.TestCase):
    """The OpenCV path: document-detect → largest-quad → perspective warp,
    with the Pillow-only path as the guaranteed fallback."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.txn = add_txn(self.conn, TODAY, 10, "STORE")

    def tearDown(self):
        self.conn.close()

    def _rec(self, rid):
        return [r for r in receipts.for_txn(self.conn, self.txn)
                if r["id"] == rid][0]

    def test_skewed_photo_yields_a_deskewed_optimized_copy(self):
        rid = receipts.add(self.conn, self.txn, _skewed_receipt_png(),
                           "image/png")
        self.assertTrue(self._rec(rid)["has_optimized"])
        opt = receipts.image(self.conn, rid, variant="optimized")
        im = Image.open(io.BytesIO(bytes(opt["image"])))
        self.assertEqual(im.mode, "L")
        # the warp crops to the quad and fills the frame with paper: every
        # corner of the optimized copy is bright, where the raw photo's
        # corners were dark table
        w, h = im.size
        self.assertGreater(h, w)                      # receipt stays portrait
        for x, y in ((3, 3), (w - 4, 3), (3, h - 4), (w - 4, h - 4)):
            self.assertGreater(im.getpixel((x, y)), 140,
                               f"corner ({x},{y}) still dark — no warp?")

    def test_cv_warp_detects_the_quad(self):
        im = Image.open(io.BytesIO(_skewed_receipt_png())).convert("RGB")
        out = receipts._cv_document_warp(im)
        self.assertIsNotNone(out)
        # roughly the paper's own proportions (tall), not the photo frame's
        self.assertGreater(out.size[1], out.size[0])

    def test_cv_failure_falls_back_to_pillow_only_path(self):
        # cv2 missing/failing must degrade to the bright-region crop, never
        # block the upload or lose the optimized copy entirely
        saved = receipts._cv_document_warp
        receipts._cv_document_warp = lambda im: None
        try:
            rid = receipts.add(self.conn, self.txn, _receipt_png(),
                               "image/png")
        finally:
            receipts._cv_document_warp = saved
        self.assertTrue(self._rec(rid)["has_optimized"])
        opt = receipts.image(self.conn, rid, variant="optimized")
        im = Image.open(io.BytesIO(bytes(opt["image"])))
        self.assertEqual(im.mode, "L")
        self.assertLess(im.size[0], 400)              # Pillow crop still ran


class ParseUsesOptimizedTests(unittest.TestCase):
    """The vision model reads the ENHANCED copy when one exists (better
    OCR); receipts without one (PDFs, failed optimize) keep the original."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        import json as _json

        import httpx
        from oikonome.engine import budget
        cfg = budget.load_config(self.conn)
        cfg["llm_url"] = "http://llm.test"
        cfg["llm_model"] = "vision-model"
        # llm.test reads as remote to the sensitive-documents gate; the
        # check-kind test here needs the consent a real remote needs
        cfg["taxdocs_allow_remote_llm"] = "1"
        budget.save_config(self.conn, cfg)
        self.txn = add_txn(self.conn, TODAY, 10, "STORE")
        self.sent_urls = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = _json.loads(request.read())
            for m in body.get("messages", []):
                for part in (m.get("content") or []):
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        self.sent_urls.append(part["image_url"]["url"])
            return httpx.Response(200, json={"choices": [{"message": {
                "content": _json.dumps({"items": []})}}]})
        self.transport = httpx.MockTransport(handler)

    def tearDown(self):
        self.conn.close()

    def test_parse_sends_the_optimized_image(self):
        import base64
        rid = receipts.add(self.conn, self.txn, _skewed_receipt_png(),
                           "image/png")
        opt = bytes(receipts.image(self.conn, rid, variant="optimized")["image"])
        out = receipts.parse_one(self.conn, rid, transport=self.transport)
        self.assertEqual(out["status"], "parsed")
        self.assertEqual(len(self.sent_urls), 1)
        prefix, b64 = self.sent_urls[0].split(",", 1)
        self.assertIn("image/jpeg", prefix)
        # the optimized copy is <= MAX_VISION_PX, so it passes through
        # _shrink_image untouched — the model got exactly the enhanced bytes
        self.assertEqual(base64.b64decode(b64), opt)

    def test_parse_without_optimized_sends_the_original(self):
        # force no optimized copy (as for a failed optimize) — original serves
        saved = receipts._cv_document_warp
        receipts._optimize, saved_opt = (lambda i, m: None), receipts._optimize
        try:
            rid = receipts.add(self.conn, self.txn, _receipt_png(),
                               "image/png")
        finally:
            receipts._optimize = saved_opt
            receipts._cv_document_warp = saved
        out = receipts.parse_one(self.conn, rid, transport=self.transport)
        self.assertEqual(out["status"], "parsed")
        self.assertEqual(len(self.sent_urls), 1)

    def test_check_kind_parses_from_the_optimized_copy(self):
        import json as _json

        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": {
                "content": _json.dumps({"check_number": "1234",
                                        "payee": "ACME LANDSCAPING",
                                        "amount": 10.0, "date": None,
                                        "memo": None, "bank": None})}}]})
        rid = receipts.add(self.conn, self.txn, _skewed_receipt_png(),
                           "image/png", kind="check")
        out = receipts.parse_one(self.conn, rid,
                                 transport=httpx.MockTransport(handler))
        self.assertEqual(out["status"], "parsed")
        self.assertEqual(out["payee"], "ACME LANDSCAPING")


if __name__ == "__main__":
    unittest.main()
