"""Receipts on transactions. Attach an image (or PDF) to a
transaction; when the tenant has a vision-capable LLM configured (the
same Settings backend llm_categorize uses), it extracts merchant, date,
total, tax, tip, and LINE ITEMS. Without an LLM the receipt simply
stores — attachment-only mode — and parses whenever one appears.

Line items are taggable (business/personal/anything) and drive the
expense report (tag + month → rows + linked images). Budget math never
reads these tables: the transaction's own amount stays the only truth
the verdict sees."""

from __future__ import annotations

import base64
import datetime as dt
import logging
import math

from ..envnum import env_num
import threading

from ..engine.compat import as_date, jsonb
from ..engine.merchant_dedup import DISPLAY_MERCHANT, MC_JOIN

log = logging.getLogger("oikonome.receipts")

MAX_IMAGE = 5 * 1024 * 1024
IMAGE_MIMES = ("image/png", "image/jpeg", "image/webp", "image/gif")
ALLOWED_MIMES = IMAGE_MIMES + ("application/pdf",)

# hardening: a 5 MB *byte* cap does not bound decoded PIXELS — a tiny
# uniform PNG can expand to hundreds of millions of pixels (a decompression
# bomb). Cap the decode area process-wide (backstop) AND refuse oversized
# images before decode in the image paths, so a crafted upload can't OOM or
# stall a shared server process. ~50 MP is well above any real phone photo.
MAX_DECODE_PX = 40_000_000
try:
    from PIL import Image as _PILImage
    _PILImage.MAX_IMAGE_PIXELS = 50_000_000
except Exception:                                 # noqa: BLE001
    pass

# The byte cap + MAX_DECODE_PX alone would leave
# the OPTIMIZE path able to OOM a shared container. A ~40 MP PNG of
# a uniform fill is a few KB (passes the 5 MB byte cap, passes the 40 MP
# decode ceiling) yet decodes to ~120 MB and its full-res cv2.warpPerspective
# buffer pushes ~240-360 MB per call; a handful of concurrent uploads can
# exhaust a memory-limited container. Two bounds:
#   * MAX_OPTIMIZE_PX — a realistic receipt-photo ceiling (~12 MP); above it we
#     skip optimization and keep the untouched original (never fail the upload);
#   * _OPTIMIZE_GATE — a small concurrency gate so a burst can't stack full-res
#     decodes. A blocked upload does NOT wait or fail; it just skips optimize.
# The heavy deskew/warp additionally runs at a bounded working resolution
# (_OPTIMIZE_WORK_PX) — the stored copy is thumbnailed to OPTIMIZED_MAX_PX
# anyway, so nothing is lost.
MAX_OPTIMIZE_PX = int(env_num("OIKONOME_RECEIPT_MAX_OPTIMIZE_PX",
                               "12000000"))
_OPTIMIZE_WORK_PX = int(env_num("OIKONOME_RECEIPT_OPTIMIZE_WORK_PX",
                                 "2400"))
_OPTIMIZE_GATE = threading.BoundedSemaphore(
    int(env_num("OIKONOME_RECEIPT_OPTIMIZE_CONCURRENCY", "3")))

# The PARSE path decodes too — _shrink_image opens the stored image and a
# PDF is rasterized — and every upload/parse-now spawns its own thread, so a
# burst of parse requests across stored receipts stacks full-res Pillow
# buffers exactly the way an ungated optimize would. Unlike optimize, a parse can't
# be skipped (the user asked for it), so the gate QUEUES: a thread waits its
# turn, and only gives up (status 'failed', retryable) after
# _DECODE_WAIT_S so a stuck decoder can't pin the queue forever. The gate
# covers the decode only, not the (long) vision call, so it turns over fast.
_DECODE_GATE = threading.BoundedSemaphore(
    int(env_num("OIKONOME_RECEIPT_PARSE_CONCURRENCY", "2")))
_DECODE_WAIT_S = float(env_num("OIKONOME_RECEIPT_PARSE_WAIT_S", "120"))
BUSY_ERROR = "server is busy reading receipts — try again in a minute"


class ProseAnswer(ValueError):
    """The model answered, but not with the fields it was asked for."""


PROSE_ERROR = ("the model answered in prose, not the expected fields — try "
               "a different vision model in Settings → AI")

# what marks the object in a reply as the receipt (or check) rather than
# an example, a restated question or JSON-looking text printed on the
# paper: every field the prompt asks for, so the object answering the
# whole schema outranks a fragment that happens to name one of them
_RECEIPT_KEYS = ("merchant", "date", "total", "tax", "tip", "items")
_CHECK_KEYS = ("check_number", "payee", "amount", "date", "memo", "bank")


class ReceiptRefusal(ValueError):
    """A refusal this module WROTE — no backend supplied a word of it.

    `_friendly_error` shows these verbatim and classifies everything else,
    because everything else is an AI backend's own text and that is not
    ours to put on a household's screen. A ValueError subclass so every
    door that already catches ValueError here (the upload routes, the
    tax-document gate) keeps behaving exactly as it did.
    """


# The vision model EXTRACTS only — small vision models OCR well but
# mis-categorize while reading (one store's receipt can come back
# as two unrelated categories). Line items are searchable and groupable instead; the
# tag column stays in the data model so existing expense-report tags
# keep working, but nothing auto-fills it.
_PROMPT = (
    "You are reading a purchase receipt. Return STRICT JSON only:\n"
    '{"merchant": str|null, "date": "YYYY-MM-DD"|null, "total": number|null,'
    ' "tax": number|null, "tip": number|null,'
    ' "items": [{"description": str, "qty": number|null, "amount": number}]}'
    "\nAmounts are positive numbers. items = individual purchased lines "
    "(omit subtotals/tax/change lines). If unreadable, return "
    '{"items": []}.')

# Check images: the ledger row for a written check is bank mechanics
# ("CHECK #1234") with no merchant behind it — the payee on the check's
# face is the real merchant. A check-kind attachment parses with this
# prompt instead of the receipt one; no line items, and the payee feeds
# the ordinary categorization flow afterwards.
_CHECK_PROMPT = (
    "You are reading a photo or scan of the FRONT of a bank check. "
    "Return STRICT JSON only:\n"
    '{"check_number": str|null, "payee": str|null, "amount": number|null,'
    ' "date": "YYYY-MM-DD"|null, "memo": str|null, "bank": str|null}'
    "\npayee is the pay-to-the-order-of line. amount is the courtesy-box "
    "number (positive). memo is the memo line. bank is the bank name "
    "printed on the check, if legible. Use null for anything unreadable.")

KINDS = ("receipt", "check")


def add(conn, txn_id: str | None, image: bytes, mime: str,
        kind: str = "receipt") -> str:
    """Store a receipt. With txn_id None the receipt WAITS for its charge:
    engine/receipt_match pairs it once the charge reaches the ledger. Only
    a purchase receipt can wait — a check image is read against the
    transaction it is attached to (its payee categorizes that row)."""
    if not image:
        raise ValueError("empty file")
    if len(image) > MAX_IMAGE:
        raise ValueError("receipt too large (5 MB max)")
    if mime not in ALLOWED_MIMES:
        raise ValueError("images or PDF only")
    if kind not in KINDS:
        raise ValueError("kind must be 'receipt' or 'check'")
    if txn_id is None:
        if kind != "receipt":
            raise ValueError("a check image is attached to its transaction")
    elif conn.execute("SELECT 1 FROM transactions WHERE id = %s AND removed=0",
                      (txn_id,)).fetchone() is None:
        raise ValueError("no such transaction")
    row = conn.execute(
        """INSERT INTO receipts (txn_id, image, mime, kind)
           VALUES (%s,%s,%s,%s) RETURNING id""",
        (txn_id, image, mime, kind)).fetchone()
    rid = str(row["id"])
    # store a cleaned-up optimized copy next to the original
    # (best-effort — never block the upload if optimization fails).
    opt = _optimize(image, mime)
    if opt:
        conn.execute(
            "UPDATE receipts SET image_optimized=%s, optimized_mime=%s "
            "WHERE id=%s::uuid", (opt[0], opt[1], rid))
    return rid


def for_txn(conn, txn_id: str) -> list[dict]:
    receipts = conn.execute(
        """SELECT id, mime, kind, status, parsed, error, created_at,
                  matched_at, match_method,
                  (image_optimized IS NOT NULL) AS has_optimized
           FROM receipts WHERE txn_id = %s ORDER BY created_at""",
        (txn_id,)).fetchall()
    txn = conn.execute("SELECT amount FROM transactions WHERE id = %s",
                       (txn_id,)).fetchone()
    out = []
    for r in receipts:
        items = conn.execute(
            """SELECT line, description, qty, amount, tag
               FROM receipt_items WHERE receipt_id = %s ORDER BY line""",
            (r["id"],)).fetchall()
        row = {**r, "id": str(r["id"]), "items": items}
        # a parsed check whose amount disagrees with the transaction gets a
        # quiet mismatch flag — display only, the transaction never changes.
        # Compare against abs(): positive = money out, and a check's
        # courtesy-box amount is always positive.
        if (r["kind"] == "check" and r["status"] == "parsed" and txn
                and isinstance((r["parsed"] or {}).get("amount"),
                               (int, float))):
            row["amount_mismatch"] = (
                abs(abs(txn["amount"] or 0.0) - float(r["parsed"]["amount"]))
                > 0.005)
        out.append(row)
    return out


def image(conn, receipt_id: str, variant: str = "original") -> dict | None:
    """The receipt image bytes + mime. variant='optimized' returns the
    cleaned-up copy when present, else falls back to the original."""
    r = conn.execute(
        "SELECT image, mime, image_optimized, optimized_mime "
        "FROM receipts WHERE id = %s::uuid", (receipt_id,)).fetchone()
    if r is None:
        return None
    if variant == "optimized" and r["image_optimized"] is not None:
        return {"image": r["image_optimized"], "mime": r["optimized_mime"]}
    return {"image": r["image"], "mime": r["mime"]}


def delete(conn, receipt_id: str) -> int:
    conn.execute("DELETE FROM receipt_items WHERE receipt_id = %s::uuid",
                 (receipt_id,))
    return conn.execute("DELETE FROM receipts WHERE id = %s::uuid",
                        (receipt_id,)).rowcount


def set_tag(conn, receipt_id: str, line: int, tag: str) -> int:
    return conn.execute(
        "UPDATE receipt_items SET tag = %s "
        "WHERE receipt_id = %s::uuid AND line = %s",
        (tag.strip()[:40], receipt_id, line)).rowcount


# ---- parsing ----------------------------------------------------------------

# A 'parsing' claim older than this is a died worker/thread — reclaimable.
# Shared by claim() and the nightly sweep's reclaim scan.
STALE_PARSE = "10 minutes"


# A receipt whose total and date a person typed in. The AI never writes
# over it: claim() refuses it, and every write a parse makes carries this
# guard, so a parse already in flight when the person saved finds the row
# taken and leaves it (the items it read are dropped with it).
MANUAL = "manual"
_NOT_MANUAL = "COALESCE(parsed->>'source', '') <> 'manual'"


class _TakenByHand(Exception):
    """Raised inside the parse's write transaction to roll it back when
    a person entered the details while the model was reading."""


def is_manual(conn, receipt_id: str) -> bool:
    r = conn.execute(
        "SELECT parsed->>'source' AS source FROM receipts"
        " WHERE id = %s::uuid", (receipt_id,)).fetchone()
    return bool(r) and r["source"] == MANUAL


def claim(conn, receipt_id: str) -> bool:
    """Single-flight claim: atomically flip to 'parsing' unless a
    live parse already holds the receipt. Upload + parse-now each spawn a
    thread into parse_one, and two concurrent runs DELETE line items out
    from under each other, then INSERT twice — flaky results and doubled
    items on a double-click or upload+retry. Whoever loses this UPDATE
    simply doesn't parse. parsed_at doubles as attempt-started-at while
    status='parsing', so a stale claim (died thread) is reclaimable."""
    return conn.execute(
        f"""UPDATE receipts SET status='parsing', error=NULL,
                parsed_at=now()
            WHERE id = %s::uuid
              AND {_NOT_MANUAL}
              AND (status <> 'parsing'
                   OR parsed_at < now() - interval '{STALE_PARSE}')
            RETURNING id""", (receipt_id,)).fetchone() is not None

# Longest side sent to the vision model. A 3072×4080 phone photo costs
# ~4,000 image tokens — alone it blows ollama's default 4,096 context and
# the upload fails. ~1,500px keeps a receipt's text comfortably legible
# at a fraction of the tokens, and is faster on CPU.
MAX_VISION_PX = 1568


def _shrink_image(img: bytes, mime: str) -> tuple[bytes, str]:
    """Downscale anything larger than MAX_VISION_PX on its longest side
    and re-encode as JPEG. Anything unreadable passes through untouched —
    the model call will surface its own error."""
    import io

    from PIL import Image
    try:
        im = Image.open(io.BytesIO(img))
        if (im.width or 0) * (im.height or 0) > MAX_DECODE_PX:
            return img, mime          # too large to decode safely — pass through
        if max(im.size) <= MAX_VISION_PX:
            return img, mime
        # JPEG can decode at a reduced scale (DCT draft mode) — a phone
        # photo then never materializes at full resolution at all.
        try:
            im.draft("RGB", (MAX_VISION_PX, MAX_VISION_PX))
        except Exception:                         # noqa: BLE001
            pass
        im.thumbnail((MAX_VISION_PX, MAX_VISION_PX))
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=88)
        return buf.getvalue(), "image/jpeg"
    except Exception:                             # noqa: BLE001
        return img, mime


# Longest side of the stored optimized copy. Matches MAX_VISION_PX so the
# vision path (which prefers the optimized copy) never re-thumbnails it.
OPTIMIZED_MAX_PX = MAX_VISION_PX

# Document-detect bounds: the largest quad must fill a sane fraction of the
# frame (a receipt photographed on a table, not a speck or the whole frame),
# and the warped output must keep a plausible receipt aspect.
_QUAD_MIN_FRAC = 0.18
_QUAD_MAX_FRAC = 0.985
_DETECT_MAX_PX = 1000                 # detection runs on a downscaled copy


def _cv_document_warp(im):
    """OpenCV document detect → largest 4-point contour → perspective warp.
    Takes and returns a PIL Image (RGB). Returns None when cv2 is missing,
    no confident quad is found, or the result looks insane — the caller
    falls back to the Pillow-only crop. Never raises."""
    try:
        import cv2
        import numpy as np
    except Exception:                             # noqa: BLE001
        return None
    try:
        from PIL import Image
        rgb = np.asarray(im.convert("RGB"))
        h, w = rgb.shape[:2]
        scale = min(1.0, _DETECT_MAX_PX / float(max(h, w)))
        small = (cv2.resize(rgb, (int(w * scale), int(h * scale)))
                 if scale < 1.0 else rgb)
        gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blur, 60, 180)
        edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=2)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        frame = float(small.shape[0] * small.shape[1])
        quad = None
        for c in sorted(contours, key=cv2.contourArea, reverse=True)[:8]:
            peri = cv2.arcLength(c, True)
            approx = cv2.approxPolyDP(c, 0.02 * peri, True)
            if len(approx) == 4 and cv2.isContourConvex(approx):
                frac = cv2.contourArea(approx) / frame
                if _QUAD_MIN_FRAC < frac < _QUAD_MAX_FRAC:
                    quad = approx.reshape(4, 2).astype(np.float32)
                    break
        if quad is None:
            return None
        quad /= scale                             # back to full resolution
        # order corners: top-left, top-right, bottom-right, bottom-left
        s = quad.sum(axis=1)
        d = np.diff(quad, axis=1).ravel()
        tl, br = quad[np.argmin(s)], quad[np.argmax(s)]
        tr, bl = quad[np.argmin(d)], quad[np.argmax(d)]
        src = np.array([tl, tr, br, bl], dtype=np.float32)
        ww = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
        hh = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
        if ww < 40 or hh < 40:
            return None
        aspect = ww / float(hh)
        if not (0.1 < aspect < 10):
            return None
        dst = np.array([[0, 0], [ww - 1, 0], [ww - 1, hh - 1], [0, hh - 1]],
                       dtype=np.float32)
        m = cv2.getPerspectiveTransform(src, dst)
        out = cv2.warpPerspective(rgb, m, (ww, hh),
                                  flags=cv2.INTER_LINEAR,
                                  borderMode=cv2.BORDER_REPLICATE)
        return Image.fromarray(out)
    except Exception as e:                        # noqa: BLE001
        log.info("receipt cv warp skipped: %s", type(e).__name__)
        return None


def _pillow_crop(gray):
    """Fallback crop when no document quad was found: the receipt's bright
    region, but only when the detected box is a sane fraction of the frame —
    never crop aggressively or to nothing."""
    lo, hi = gray.getextrema()
    if hi > lo:
        t = lo + (hi - lo) * 0.4
        bbox = gray.point(lambda p, t=t: 255 if p >= t else 0).getbbox()
        if bbox:
            w, h = gray.size
            x0, y0, x1, y1 = bbox
            frac = ((x1 - x0) * (y1 - y0)) / float(w * h)
            if 0.15 < frac < 0.985:
                pad = int(min(w, h) * 0.02)
                return gray.crop((max(0, x0 - pad), max(0, y0 - pad),
                                  min(w, x1 + pad), min(h, y1 + pad)))
    return gray


def _optimize(img: bytes, mime: str) -> tuple[bytes, str] | None:
    """Turn a receipt PHOTO into a clean, legible render — orient,
    document-detect + perspective-deskew (OpenCV largest-quad warp), then
    grayscale + autocontrast + sharpen — stored ALONGSIDE the untouched
    original. Best-effort: returns None on skip/failure so the UI falls back
    to the original; a missing/failed cv2 falls back to a Pillow-only
    bright-region crop. Never blocks or fails the upload."""
    if mime not in IMAGE_MIMES:          # PDFs keep only the original for now
        return None
    import io

    from PIL import Image, ImageFilter, ImageOps
    # Cap concurrent full-res decodes. A burst can't stack enough
    # heavy buffers to OOM the shared container — the overflow upload simply
    # keeps its untouched original (the gate never blocks or fails it).
    if not _OPTIMIZE_GATE.acquire(blocking=False):
        log.info("receipt optimize skipped: concurrency gate full")
        return None
    try:
        im0 = Image.open(io.BytesIO(img))
        # refuse a decompression bomb BEFORE decoding: .size reads the header
        # only. Above the realistic receipt ceiling (or the hard decode
        # backstop) → skip optimization; the original is kept.
        if (im0.width or 0) * (im0.height or 0) > min(MAX_OPTIMIZE_PX,
                                                      MAX_DECODE_PX):
            return None
        im = ImageOps.exif_transpose(im0)
        # bound the working resolution BEFORE the full-res np.asarray/warp
        # (the OOM source): the stored copy thumbnails to OPTIMIZED_MAX_PX
        # anyway, so deskewing at a few thousand px on the long side is lossless
        # for the enhanced-render purpose.
        if max(im.size) > _OPTIMIZE_WORK_PX:
            im.thumbnail((_OPTIMIZE_WORK_PX, _OPTIMIZE_WORK_PX))
        warped = _cv_document_warp(im)
        gray = ImageOps.grayscale(warped if warped is not None else im)
        if warped is None:
            gray = _pillow_crop(gray)
        out = ImageOps.autocontrast(gray, cutoff=1)
        out = out.filter(ImageFilter.UnsharpMask(radius=1.5, percent=120,
                                                 threshold=3))
        out.thumbnail((OPTIMIZED_MAX_PX, OPTIMIZED_MAX_PX))
        buf = io.BytesIO()
        out.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:                        # noqa: BLE001
        log.info("receipt optimize skipped: %s", type(e).__name__)
        return None
    finally:
        _OPTIMIZE_GATE.release()


def _pdf_first_page_png(pdf_bytes: bytes) -> bytes:
    """Rasterize page 1 to PNG for the vision model (receipts are one page;
    page 1 carries the items), capped at MAX_VISION_PX on the longest
    side. pypdfium2 is self-contained — no poppler.

    Raises ValueError for a file the rasterizer cannot handle — a truncated
    upload, an encrypted document, a page whose geometry pdfium refuses.
    Callers turn that into "we could not read this file"; the library's own
    exception would make a bad scan a 500 on the tax-document upload door,
    which catches ValueError and nothing else."""
    import io

    import pypdfium2 as pdfium
    try:
        doc = pdfium.PdfDocument(pdf_bytes)
    except Exception as e:                        # noqa: BLE001 — see above
        raise ReceiptRefusal(f"couldn't read this PDF ({type(e).__name__}) — "
                             f"try re-exporting it, or upload an image")
    try:
        page = doc[0]
        w, h = page.get_size()
        scale = min(2.0, MAX_VISION_PX / max(w, h))
        pil = page.render(scale=scale).to_pil()
        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:                        # noqa: BLE001 — see above
        raise ReceiptRefusal(f"couldn't render page 1 of this PDF "
                             f"({type(e).__name__}) — try re-exporting it, "
                             f"or upload an image")
    finally:
        doc.close()


def _friendly_error(e: Exception, vision_set: bool) -> str:
    """Say what to DO about a failed parse, in words we wrote.

    `receipts.error` is rendered in the receipt panel, so it is the one
    string from this path a person reads. The exception's own text and the
    backend's HTTP body are not ours to show: that body is written by whatever endpoint
    the household pointed the vision role at, and it can echo back the
    prompt (which carries the receipt's own contents — a check's payee, a
    pharmacy's line items), the endpoint URL with its credentials in the
    userinfo, or an API key quoted in an auth complaint. A gateway's HTML
    error page arriving in the panel is the same leak, less obviously.

    So the body is read to CLASSIFY and never quoted. Every branch returns
    a fixed sentence naming a cause and the setting that fixes it; the raw
    text is logged server-side, where the operator — who already has the
    endpoint and its key — can read it.
    """
    if isinstance(e, ReceiptRefusal):
        return str(e)[:400]          # our own words — nothing to classify
    if isinstance(e, ProseAnswer):
        log.warning("receipt parse failed: the reply held no JSON object")
        return PROSE_ERROR
    body = ""
    resp = getattr(e, "response", None)
    status = getattr(resp, "status_code", None)
    if resp is not None:
        try:
            body = resp.text[:500]
        except Exception:                         # noqa: BLE001
            body = ""
    # The warning names the class of failure only. The body (and the
    # exception text, which can carry the request URL) is the same text
    # withheld from the panel — it can echo the receipt's contents or a
    # credential — so it goes to DEBUG and is marked no_capture, which
    # keeps it out of the feedback bundle's log ring.
    log.warning("receipt parse failed: %s | status=%s",
                type(e).__name__, status)
    log.debug("receipt parse failure detail: %s | body=%s", e, body[:500],
              extra={"no_capture": True})
    lower = (type(e).__name__ + " " + str(e) + " " + body).lower()
    if any(s in lower for s in ("does not support images", "image input",
                                "vision", "multimodal", "invalid content")):
        return ("this model can't read images — set a vision-capable model "
                "(e.g. qwen2.5vl) under Settings → AI → "
                "Vision model")
    if "context" in lower and ("exceed" in lower or "size" in lower):
        return ("the receipt didn't fit the model's context window — raise "
                "it (bundled Ollama: OLLAMA_CONTEXT_LENGTH in docker/.env, "
                "then restart) and retry")
    hint = ("" if vision_set else
            " — if your chat model is text-only, set a vision model in "
            "Settings → AI")
    if "timeout" in lower or "timed out" in lower:
        return ("the AI backend didn't answer in time — a large model can "
                "take a while to load; retry, or check the endpoint under "
                "Settings → AI" + hint)
    if status in (401, 403) or any(s in lower for s in (
            "unauthorized", "forbidden", "invalid api key",
            "incorrect api key", "authentication")):
        return ("the AI backend refused our credentials — check the API key "
                "under Settings → AI")
    if (status in (404, 502, 503, 504) or isinstance(e, OSError)
            or any(s in lower for s in ("connect", "unreachable", "refused",
                                        "unavailable", "name or service",
                                        "temporary failure"))):
        return ("the AI backend is unreachable — check that it is running "
                "and that the endpoint under Settings → AI is right" + hint)
    return ("the AI backend's answer couldn't be read — the server log has "
            "the detail" + hint)


def _require_check_offbox_consent(conn) -> None:
    """A check image shows the account's full routing and account number.

    Same doctrine as taxdocs._require_offbox_consent, same opt-in switch:
    local endpoints (bundled Ollama, LAN, loopback) pass untouched; a
    remote endpoint needs the sensitive-documents consent. Checks look at
    BOTH backends — the vision model reads the image, and the chat model
    later receives the payee and the memo line (where people write account
    numbers) for categorization."""
    from . import budget
    from . import llm_categorize as llm
    from .taxdocs import _endpoint_is_local
    urls = [(llm._backend(conn, role="vision") or {}).get("url") or "",
            (llm._backend(conn) or {}).get("url") or ""]
    if all(_endpoint_is_local(u) for u in urls if u):
        return
    cfg = budget.load_config(conn)
    if str(cfg.get("taxdocs_allow_remote_llm") or "").strip().lower() in (
            "1", "true", "yes", "on"):
        return
    raise ValueError(
        "reading a check would send its image — including the full bank "
        "routing and account number printed on its face — to the LLM "
        "endpoint you configured, which is not on this machine or network. "
        "Enter the details by hand, point Settings → AI at a "
        "local model, or turn on 'allow sensitive documents to be sent to "
        "my remote model' in Settings → AI if you intend to "
        "send it.")


def parse_one(conn, receipt_id: str, transport=None,
              claimed: bool = False) -> dict:
    """Run the vision LLM over one stored receipt (PDFs are rasterized
    first). Stamps status 'parsing' while it runs so the UI can show live
    progress. Raises ValueError for user-facing problems; records failed
    status on LLM/parse errors.

    claimed=True means the caller already holds the claim() — the API
    routes claim before spawning their thread so the first status poll
    sees 'parsing'. Everyone else (the sweep) claims here; losing the
    claim returns {'status': 'parsing'} and touches nothing."""
    from . import llm_categorize as llm
    r = conn.execute(
        "SELECT r.id, r.txn_id, r.image, r.mime, r.kind, r.status, "
        "       r.image_optimized, r.optimized_mime, "
        "       t.date AS txn_day, r.created_at "
        "FROM receipts r LEFT JOIN transactions t ON t.id = r.txn_id "
        "WHERE r.id = %s::uuid",
        (receipt_id,)).fetchone()
    if r is None:
        raise ValueError("no such receipt")
    backend = llm._backend(conn, role="vision")
    if not backend.get("url"):
        raise ValueError("no LLM configured — Settings → AI")
    # receipts read with a dedicated vision model when configured —
    # the chat model that categorizes merchants is often text-only
    vision = str(backend.get("vision_model") or "").strip()
    if vision:
        backend = {**backend, "model": vision}
    is_check = r["kind"] == "check"
    if is_check:
        # a check is not a coffee receipt: its face carries the full
        # routing and account number (the MICR line), and people write
        # account numbers on memo lines — the same class of document the
        # tax-doc gate exists for. Both hops are gated: the image to the
        # vision model here, and the payee+memo to the chat model after
        # the parse. On an unclaimed call the refusal leaves the row
        # untouched, like "no LLM configured"; the API routes claim
        # BEFORE calling (claimed=True), and a raise there would strand
        # the row at 'parsing' — endless spinner, retry button hidden —
        # so the refusal is recorded as a failed parse with its reason.
        try:
            _require_check_offbox_consent(conn)
        except ValueError as e:
            # Recorded as a FAILED parse with its reason on both paths:
            # the clients show `error` (and the retry) for 'failed' only,
            # so a refusal left at 'uploaded' would be invisible, and one
            # left at a stale 'parsing' (the sweep reclaiming a died
            # thread's row) an endless spinner. Guarded on the state read, so a
            # parse that finished concurrently is never marked failed.
            conn.execute(
                "UPDATE receipts SET status='failed', error=%s "
                "WHERE id=%s AND status IN ('uploaded', 'parsing')",
                (str(e)[:400], r["id"]))
            raise
    if not claimed and not claim(conn, receipt_id):
        return {"status": "parsing"}      # someone else is mid-parse
    try:
        # the vision model reads the ENHANCED copy when one exists —
        # cropped/deskewed/contrast-boosted text OCRs better than the raw
        # photo. The untouched original stays the fallback (and the PDF path).
        if r["image_optimized"] is not None:
            img, mime = bytes(r["image_optimized"]), r["optimized_mime"]
        else:
            img, mime = bytes(r["image"]), r["mime"]
        if not _DECODE_GATE.acquire(timeout=_DECODE_WAIT_S):
            raise ReceiptRefusal(BUSY_ERROR)
        try:
            if mime == "application/pdf":
                img, mime = _pdf_first_page_png(img), "image/png"
            else:
                img, mime = _shrink_image(img, mime)
        finally:
            _DECODE_GATE.release()
        b64 = base64.b64encode(img).decode()
        messages = [{"role": "user", "content": [
            {"type": "text", "text": _CHECK_PROMPT if is_check else _PROMPT},
            {"type": "image_url",
             "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]}]
        raw = llm._chat(messages, max_tokens=1500, transport=transport,
                        backend=backend)
        parsed = llm.json_answer(
            raw, _CHECK_KEYS if is_check else _RECEIPT_KEYS)
        if parsed is None:
            # the reply can carry the receipt's own contents: DEBUG only,
            # and kept out of the feedback bundle's log ring
            log.debug("receipt parse: reply was not JSON: %r",
                      (raw or "")[:200], extra={"no_capture": True})
            raise ProseAnswer("no JSON object in the reply")
        items = [] if is_check else (parsed.get("items") or [])
        if not isinstance(items, list):
            raise ValueError("items not a list")
    except Exception as e:                        # noqa: BLE001
        conn.execute(
            "UPDATE receipts SET status='failed', error=%s WHERE id=%s"
            f" AND {_NOT_MANUAL}",
            (_friendly_error(e, bool(vision)), r["id"]))
        return {"status": "failed", "error": str(e)}
    # bound everything the model returned BEFORE any write: a line whose
    # amount is not a finite number is dropped, a qty that is not becomes
    # unknown, strings are capped — and the writes below then run as one
    # transaction under the same failure handler as the call itself, so a
    # refused value can never strand the row at 'parsing' with half its
    # items written
    clean_items, clean_rows = [], []
    for i, it in enumerate(items[:200]):
        if not isinstance(it, dict):
            continue
        amount = _finite(it.get("amount"))
        if amount is None:
            continue
        qty = it.get("qty")
        qty = _finite(qty) if qty is not None else None
        desc = str(it.get("description") or "")[:200]
        clean_rows.append((r["id"], i, desc, qty, amount))
        clean_items.append({"line": i, "description": desc, "amount": amount})
    # an ambiguous printed date (03/04) is read against the charge the
    # paper is attached to, or else the day it was photographed — that day
    # on the household's own calendar, the one the matcher's "today" is
    # kept in: a receipt snapped at 7pm Pacific is not tomorrow's in UTC
    near = _as_day(r["txn_day"]) or _snap_day(conn, r["created_at"])
    meta = (_clean_check_meta(parsed, near) if is_check
            else _clean_receipt_meta(parsed, near))
    try:
        with conn.transaction():
            # the row first, under a lock: a person who typed the details
            # in while the model was reading keeps them (and the line
            # items stay as they were)
            if conn.execute(
                    "SELECT 1 FROM receipts WHERE id=%s"
                    f" AND {_NOT_MANUAL} FOR UPDATE",
                    (r["id"],)).fetchone() is None:
                raise _TakenByHand
            conn.execute("DELETE FROM receipt_items WHERE receipt_id = %s",
                         (r["id"],))
            for row in clean_rows:
                conn.execute(
                    """INSERT INTO receipt_items (receipt_id, line,
                                                  description, qty, amount)
                       VALUES (%s,%s,%s,%s,%s)""", row)
            conn.execute(
                "UPDATE receipts SET status='parsed', parsed=%s, error=NULL, "
                "parsed_at=now() WHERE id=%s", (jsonb(meta), r["id"]))
    except _TakenByHand:
        return {"status": "parsed", "kept": MANUAL}
    except Exception as e:                        # noqa: BLE001
        conn.execute(
            "UPDATE receipts SET status='failed', error=%s WHERE id=%s"
            f" AND {_NOT_MANUAL}",
            (_friendly_error(e, bool(vision)), r["id"]))
        return {"status": "failed", "error": str(e)}
    if r["txn_id"] is None and not is_check:
        # a receipt snapped before its charge: now that its total, date and
        # store are known, look for the charge. Best-effort like the check
        # categorize below — a matcher failure never un-parses the receipt,
        # and the next sync tries again.
        try:
            from . import receipt_match
            receipt_match.match_receipt(conn, str(r["id"]))
        except Exception as e:                    # noqa: BLE001
            log.warning("receipt match after parse failed: %s",
                        type(e).__name__)
    if is_check and meta.get("payee"):
        # the check's payee is the transaction's real merchant — feed it
        # through the ordinary categorization flow (rule cache + LLM).
        # Best-effort like the parse itself: a categorize failure never
        # un-parses the check.
        try:
            llm.categorize_check_payee(conn, r["txn_id"], meta["payee"],
                                       memo=meta.get("memo") or "",
                                       transport=transport)
        except Exception as e:                    # noqa: BLE001
            log.warning("check payee categorize failed: %s", e)
    return {"status": "parsed", "items": len(clean_items), **meta}


def _snap_day(conn, created_at) -> dt.date | None:
    """The household's calendar day a receipt was photographed on."""
    if not isinstance(created_at, dt.datetime):
        return _as_day(created_at)
    if created_at.tzinfo is None:
        return created_at.date()
    from .. import localtime
    from . import budget
    return created_at.astimezone(
        localtime.tenant_tz(budget.load_config(conn))).date()


def _as_day(v) -> dt.date | None:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    try:
        return as_date(str(v)[:10]) if v else None
    except ValueError:
        return None


def _like_literal(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _finite(v) -> float | None:
    """A model-returned number, or None. `json.loads` accepts NaN and
    Infinity; DOUBLE PRECISION stores them, JSONB refuses them, and the
    JSON the API serves cannot carry them — so none of them may land."""
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: an int too wide for a double — a barcode read as
        # a price is exactly that shape
        return None
    return f if math.isfinite(f) else None


# How close to the snap date a reading of an ambiguous printed date must
# be to win over the other reading.
_NEAR_SNAP_DAYS = 7


def iso_date(v, near: dt.date | None = None) -> str | None:
    """A date the model read, as ISO YYYY-MM-DD, or None.

    Asked for ISO, a vision model still copies the date as printed —
    09/26/2026, 9/26/26, 26.09.2026 — and the matcher, the list and the
    edit form all read ISO. A two-number date is read the way the CSV
    import reads one: day-first when the first number cannot be a month,
    month-first when the second cannot. When both can (03/04), `near` —
    the day the paper was photographed, or the charge it is attached to —
    decides: a receipt is printed on or before that day and usually within
    the week, so a reading after it, or a reading far from it while the
    other is close, is the wrong one. Only when the day cannot tell does
    month-first stand (the US receipt). That guess is left in rather than
    dropped: with no printed date the matcher centres its window on the
    snap date and could pair a same-amount charge there, while a wrong
    printed date only makes the automatic match miss, and the receipt
    waits for a person. Anything that is not a date comes back None, and
    the matcher falls back to the snap date."""
    from ..sync.csvimport import _SLASH_DATE, _parse_date, date_order
    if v is None or isinstance(v, (bool, float)):
        return None
    if isinstance(v, int):
        # a model that drops the separators returns 20240105 as a number
        v = str(v)
    if isinstance(v, dt.datetime):
        v = v.date()
    if isinstance(v, dt.date):
        return v.isoformat()
    s = str(v).strip()[:40]
    if not s:
        return None
    try:
        return as_date(s[:10]).isoformat()
    except ValueError:
        pass
    if len(s) == 8 and s.isdigit():
        try:
            return dt.date(int(s[:4]), int(s[4:6]), int(s[6:])).isoformat()
        except ValueError:
            return None
    two = date_order([s])
    s2 = s.replace(".", "/").replace("-", "/") if s[:1].isdigit() else s
    m = _SLASH_DATE.match(s2)
    if near is not None and m and m.group(1) != m.group(2) \
            and int(m.group(1)) <= 12 and int(m.group(2)) <= 12:
        picked = _nearer_reading(s2, near, _parse_date)
        if picked is not None:
            return picked.isoformat()
    try:
        return _parse_date(s2, two).isoformat()
    except (ValueError, TypeError):
        return None


def _nearer_reading(s: str, near: dt.date, parse) -> dt.date | None:
    """Of the month-first and day-first readings of an ambiguous date, the
    one the photograph's day vouches for — or None when it vouches for
    neither (both fit, or neither does)."""
    try:
        readings = {parse(s, "mdy"), parse(s, "dmy")}
    except (ValueError, TypeError):
        return None
    fits = [d for d in readings
            if near - dt.timedelta(days=366) <= d <= near + dt.timedelta(days=1)]
    if len(fits) == 1:
        return fits[0]
    close = [d for d in fits
             if abs((near - d).days) <= _NEAR_SNAP_DAYS]
    return close[0] if len(close) == 1 else None


def _clean_check_meta(parsed: dict, near: dt.date | None = None) -> dict:
    """Bound what the model returned before it hits JSONB — strings capped,
    amount coerced to a positive float or dropped."""
    def s(key: str, n: int):
        v = parsed.get(key)
        return None if v is None else str(v).strip()[:n] or None
    amount = _finite(parsed.get("amount"))
    return {"check_number": s("check_number", 20), "payee": s("payee", 120),
            "amount": abs(amount) if amount is not None else None,
            "date": iso_date(parsed.get("date"), near), "memo": s("memo", 160),
            "bank": s("bank", 80)}


def _clean_receipt_meta(parsed: dict, near: dt.date | None = None) -> dict:
    """The plain-receipt sibling of _clean_check_meta — the vision model's
    reply is not the prompted schema by contract, only by habit: a
    merchant that comes back as an object would crash the SPA's render, and
    a non-finite total would strand the row."""
    def s(key: str, n: int):
        v = parsed.get(key)
        if v is None:
            return None
        if isinstance(v, dict):
            v = v.get("name") or v.get("value") or ""
        return str(v).strip()[:n] or None
    return {"merchant": s("merchant", 120),
            "date": iso_date(parsed.get("date"), near),
            "total": _finite(parsed.get("total")),
            "tax": _finite(parsed.get("tax")),
            "tip": _finite(parsed.get("tip"))}


def set_details(conn, receipt_id: str, *, total: float, date: dt.date,
                merchant: str | None = None,
                keep_merchant: bool = True) -> dict | None:
    """A person types in what the AI could not read — or read wrong: the
    total, the date and, optionally, the store. Stored as the receipt's
    parsed fields under the same keys a parse writes (so the matcher reads
    them like any other), marked `source: manual`, status 'parsed'. With
    `keep_merchant` a store the AI already read survives a save that names
    none; tax and tip it read survive too. From here on no parse writes
    over the row (see MANUAL). Returns the row's txn_id and parsed fields,
    or None when there is no such purchase receipt."""
    with conn.transaction():
        row = conn.execute(
            "SELECT txn_id, kind, parsed FROM receipts"
            " WHERE id = %s::uuid FOR UPDATE", (receipt_id,)).fetchone()
        if row is None or row["kind"] != "receipt":
            return None
        was = row["parsed"] if isinstance(row["parsed"], dict) else {}
        store = (merchant or "").strip()[:80] or None
        if store is None and keep_merchant:
            store = was.get("merchant")
        meta = {"merchant": store, "date": date.isoformat(),
                "total": round(float(total), 2),
                "tax": _finite(was.get("tax")),
                "tip": _finite(was.get("tip")),
                "source": MANUAL}
        conn.execute(
            "UPDATE receipts SET status='parsed', parsed=%s, error=NULL,"
            " parsed_at=now() WHERE id=%s::uuid",
            (jsonb(meta), receipt_id))
    return {"txn_id": row["txn_id"], "parsed": meta}


def parse_pending(conn, limit: int = 10, transport=None) -> dict:
    """Nightly sweep: parse stored-but-unparsed receipts (PDFs included —
    they rasterize) when an LLM is configured. Silent no-op otherwise
    (attachment-only mode). Also reclaims receipts stuck 'parsing' for
    over 10 minutes (a died worker/thread mid-parse)."""
    from . import llm_categorize as llm
    if not llm.configured(conn, role="vision"):
        return {"parsed": 0, "failed": 0, "skipped": "no-llm"}
    # a check the sweep refused (off-box consent missing) is marked
    # failed with its reason, so it leaves this queue instead of filling
    # every night's batch and starving every newer receipt; anything
    # with a recorded error still queues last
    rows = conn.execute(
        f"""SELECT id FROM receipts WHERE status = 'uploaded'
           OR (status = 'parsing'
               AND parsed_at < now() - interval '{STALE_PARSE}')
           ORDER BY (error IS NOT NULL), created_at LIMIT %s""",
        (limit,)).fetchall()
    parsed = failed = 0
    for r in rows:
        try:
            out = parse_one(conn, str(r["id"]), transport=transport)
            if out["status"] == "parsed":
                parsed += 1
            else:
                failed += 1
        except ValueError:
            failed += 1
    return {"parsed": parsed, "failed": failed}


# ---- expense report ---------------------------------------------------------

def report(conn, tag: str, year: int, month: int) -> dict:
    """Tag + month → the Schedule-C rows: every tagged line item with its
    transaction context and receipt link."""
    start = dt.date(year, month, 1)
    end = (start.replace(year=year + 1, month=1) if month == 12
           else start.replace(month=month + 1))
    # the same rows the item search lists: a receipt still waiting for its
    # charge is a real purchase a person can tag, and belongs on the report
    # under the date and store it printed (the snap day when it printed
    # none); one whose charge left the ledger does not
    rows = conn.execute(
        f"""SELECT * FROM (
               SELECT ri.receipt_id::text AS receipt_id, ri.line,
                      ri.description, ri.qty, ri.amount, ri.tag,
                      r.txn_id, {_ITEM_DATE} AS date, {_ITEM_PAYEE} AS payee
                 FROM receipt_items ri
                 JOIN receipts r ON r.id = ri.receipt_id
                 LEFT JOIN transactions t ON t.id = r.txn_id
                 {MC_JOIN}
                 {_WM_JOIN}
                WHERE ri.tag = %s
                  AND (r.txn_id IS NULL OR t.removed = 0)) x
           WHERE x.date >= %s AND x.date < %s
           ORDER BY x.date, x.receipt_id, x.line""",
        (_waiting_store_names(conn), tag.strip(), start, end)).fetchall()
    total = round(sum(r["amount"] or 0 for r in rows), 2)
    return {"tag": tag, "year": year, "month": month,
            "total": total, "count": len(rows), "rows": rows}


# ---- item search & grouping ----------------------------------------

# "How much do I spend at X" is a question about what the user calls the
# merchant, so the merchant grouping uses the same display key the ledger
# and the Merchants catalog render — otherwise one business splits into a
# row per raw spelling on a screen that exists to total it.
#
# A receipt snapped before its charge reached the ledger is already read:
# its lines are real purchases, and a person can tag them while the
# receipt waits. So the items join the ledger LEFT, and a waiting
# receipt's lines carry the store and date the receipt itself printed
# (the snap day when it printed none) until the charge arrives.
# what the Receipts page calls a store nobody knows (the clients' own
# UNKNOWN_STORE)
UNKNOWN_STORE = "Unknown store"
#
# The store a waiting receipt printed is the model's raw spelling ("CORNER
# MARKET #0123"), and the ledger shows that business under its display name
# ("Corner Market"). The printed name is put through the canonicaliser the
# ledger uses (the matcher reads store names the same way) and filed under
# the ledger's display name for that business when the ledger has one, so
# a store's waiting and attached receipts total on ONE row. The mapping is
# computed per request (`_waiting_store_names`) and joined in as `wm`.
_ITEM_PAYEE = (f"COALESCE({DISPLAY_MERCHANT}, wm.store, "
               "NULLIF(r.parsed->>'merchant', ''))")
_WM_JOIN = ("LEFT JOIN jsonb_each_text(%s::jsonb) AS wm(printed, store)"
            " ON r.txn_id IS NULL AND wm.printed = r.parsed->>'merchant'")
# the printed date is cast only when it is one: a stored string that is
# not a date must hide one row's date, never fail the whole page. And it
# is believed only inside the window the matcher believes (a year before
# the snap to a day after, receipt_match.facts): a misread year would file
# the purchase under a month it never happened in, so the snap day stands
# in, as it does for the matcher. The window check sits in its own CASE so
# the cast only ever runs on a string already proven to be a date.
_ITEM_DATE = ("COALESCE(t.date, CASE WHEN r.parsed->>'date' ~ "
              "'^[0-9]{4}-[0-9]{2}-[0-9]{2}$' AND "
              "pg_input_is_valid(r.parsed->>'date', 'date') "
              "THEN CASE WHEN (r.parsed->>'date')::date BETWEEN "
              "r.created_at::date - 366 AND r.created_at::date + 1 "
              "THEN (r.parsed->>'date')::date END END, r.created_at::date)")
def _store_key(name) -> str:
    from .merchant_dedup import canonical_merchant
    name = str(name or "").strip()
    return (canonical_merchant(name) or name).casefold() if name else ""


def _waiting_store_names(conn) -> str:
    """JSON {printed store name: name to file it under} for the stores
    printed on waiting receipts that have line items. The ledger's display
    name for the same business wins (the one on the most rows when a few
    spellings canonicalise alike); a store the ledger has never seen files
    under its canonical form, so two spellings of it still share a row."""
    import json
    from .merchant_sql import RAW_KEY
    printed = [r["m"] for r in conn.execute(
        """SELECT DISTINCT r.parsed->>'merchant' AS m
             FROM receipts r
            WHERE r.txn_id IS NULL
              AND NULLIF(r.parsed->>'merchant', '') IS NOT NULL
              AND EXISTS (SELECT 1 FROM receipt_items ri
                           WHERE ri.receipt_id = r.id)
            LIMIT 500""").fetchall()]
    keys = {m: _store_key(m) for m in printed}
    keys = {m: k for m, k in keys.items() if k}
    if not keys:
        return "{}"
    # read only ledger names sharing a word with a printed store, so the
    # canonicaliser runs over a handful of rows, not the whole ledger
    words = sorted({w for k in keys.values() for w in k.split()
                    if len(w) >= 3})
    ledger: dict[str, tuple[int, str]] = {}
    if words:
        pats = [f"%{_like_literal(w)}%" for w in words[:200]]
        for r in conn.execute(
                f"""SELECT {DISPLAY_MERCHANT} AS d, {RAW_KEY} AS raw,
                           count(*) AS n
                      FROM transactions t {MC_JOIN}
                     WHERE t.removed = 0
                       AND ({DISPLAY_MERCHANT} ILIKE ANY(%s)
                            OR {RAW_KEY} ILIKE ANY(%s))
                     GROUP BY 1, 2""", (pats, pats)).fetchall():
            if not r["d"]:
                continue
            for k in {_store_key(r["d"]), _store_key(r["raw"])}:
                n, d = ledger.get(k, (0, ""))
                # the ledger name on the most rows; ties by name, stable
                if (r["n"], r["d"]) > (n, d):
                    ledger[k] = (r["n"], r["d"])
    from .merchant_dedup import canonical_merchant
    return json.dumps({
        m: ledger[k][1] if k in ledger
        else (canonical_merchant(m.strip()) or m.strip())
        for m, k in keys.items()})


_GROUP_KEYS = {
    "item": "LOWER(TRIM(ri.description))",
    # a waiting receipt that printed no store has no payee at all; its
    # group wears the name the clients show for one, so every such receipt
    # totals under one visible row instead of a blank key
    "merchant": f"COALESCE({_ITEM_PAYEE}, '{UNKNOWN_STORE}')",
    "month": f"to_char({_ITEM_DATE}, 'YYYY-MM')",
}


def search_items(conn, q: str = "", group: str = "item",
                 limit: int = 300) -> dict:
    """Line items across every parsed receipt — searchable like
    transactions and groupable ('how much do I spend on shampoo'). Returns
    the newest matching rows (capped) plus aggregates by `group`. Items of
    a receipt still waiting for its charge are included (txn_id null); an
    item whose charge was removed from the ledger is not."""
    # r.txn_id IS NULL, not t.id IS NULL: a receipt pointing at a charge
    # that is gone must stay hidden like its removed charge
    where, params = "(r.txn_id IS NULL OR t.removed = 0)", []
    if (q or "").strip():
        # a typed search is a literal, not a pattern: % and _ in it must
        # not widen the match, and it is bounded like any search box
        where += " AND ri.description ILIKE %s ESCAPE '\\'"
        params.append(f"%{_like_literal(q.strip()[:100])}%")
    names = _waiting_store_names(conn)
    rows = conn.execute(
        f"""SELECT ri.receipt_id::text AS receipt_id, ri.line,
                   ri.description, ri.qty, ri.amount, ri.tag,
                   r.txn_id, {_ITEM_DATE} AS date,
                   {_ITEM_PAYEE} AS payee
            FROM receipt_items ri
            JOIN receipts r ON r.id = ri.receipt_id
            LEFT JOIN transactions t ON t.id = r.txn_id
            {MC_JOIN}
            {_WM_JOIN}
            WHERE {where}
            ORDER BY 8 DESC, ri.receipt_id, ri.line
            LIMIT %s""", [names] + params + [limit + 1]).fetchall()
    truncated = len(rows) > limit
    key = _GROUP_KEYS.get(group, _GROUP_KEYS["item"])
    groups = conn.execute(
        f"""SELECT {key} AS key, COUNT(*) AS n,
                   ROUND(SUM(ri.amount)::numeric, 2)::float8 AS total,
                   MAX({_ITEM_DATE}) AS last_date
            FROM receipt_items ri
            JOIN receipts r ON r.id = ri.receipt_id
            LEFT JOIN transactions t ON t.id = r.txn_id
            {MC_JOIN}
            {_WM_JOIN}
            WHERE {where}
            GROUP BY key ORDER BY total DESC LIMIT 150""",
        [names] + params).fetchall()
    return {"rows": rows[:limit], "groups": groups, "truncated": truncated}
