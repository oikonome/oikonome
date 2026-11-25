"""tax & income documents → the lifetime income spine.

Analyze/commit pair behind the Import page's "Tax & income documents"
card (spec `specs/tax-documents.md`): SSA earnings-record XML and
IRS return transcripts parse deterministically; 1040 PDFs go text→chat-
LLM (regexing every TurboTax vintage is brittle); W-2 images/PDFs go
through the vision path (same model override + downscale receipts use).
Nothing writes until the user approves the previewed per-year rows, and
commits merge per column — a transcript never blanks SSA columns.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import re

# NOT xml.etree — the SSA earnings record is a file the USER uploads, and
# stdlib ElementTree expands internal entities without limit. It refuses
# external ones (no XXE file read or SSRF), but a few hundred bytes of
# nested <!ENTITY> definitions expand to gigabytes and take the container
# down with them — and on a multi-tenant server, every tenant with it.
# defusedxml refuses DTD entities outright (EntitiesForbidden). Guarded by
# test_inventory_xml_safety.py.
from defusedxml import ElementTree as ET

log = logging.getLogger("oikonome.taxdocs")

ANNUAL_FIELDS = ("total_income", "agi", "taxable_income", "tax_paid",
                 "wages", "primary_wages", "investment_income",
                 "capital_gain", "spouse_wages", "ss_earnings",
                 "medicare_earnings")

_W2_PROMPT = (
    "You are reading a U.S. W-2 (Wage and Tax Statement). Return STRICT "
    'JSON only: {"year": int, "employer": str|null, "ein": str|null, '
    '"boxes": {"1": number, "2": number, "3": number, "4": number, '
    '"5": number, "6": number}} — box numbers as printed (1 = wages, '
    "2 = federal tax withheld, 3 = SS wages, 5 = Medicare wages). Omit "
    "boxes you cannot read. If this is not a W-2, return {}.")

_1040_PROMPT = (
    "The following text is from a U.S. individual income tax return "
    "(Form 1040) or a full return PDF. Return STRICT JSON only: "
    '{"year": int, "total_income": number|null, "agi": number|null, '
    '"taxable_income": number|null, "tax_paid": number|null, '
    '"wages": number|null}. year = the TAX year of the return. tax_paid '
    "= total tax (liability, not refund). Use null when a line is "
    'absent. If this is not a tax return, return {}.\n\nTEXT:\n')

# every field each prompt asks for, so the reply's object answering the
# whole schema outranks a fragment the model quoted on its way there (a
# prior-year figure, a line copied off the form) that names only the year
_W2_KEYS = ("year", "employer", "ein", "boxes")
_1040_KEYS = ("year", "total_income", "agi", "taxable_income", "tax_paid",
              "wages")


# A tax document covers a working life at most — the SSA earnings record,
# the longest thing this ingests, is one row per year. Four figures is
# already absurd. Both doors are bounded by it: `commit`'s `rows` arrives
# from the CLIENT (the preview is editable), so its length is whatever the
# caller says it is, and the SSA parser reads however many <Earnings>
# elements the uploaded file happens to contain. Same shape and same number
# as `sync/plan_csv.py` and `sync/coinbase_push.py`.
MAX_ROWS = 20_000


# ---- deterministic parsers ---------------------------------------------------

def _num(s: str) -> float | None:
    s = s.strip().replace(",", "").replace("$", "")
    neg = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    s = s.strip("()-")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def parse_ssa_xml(data: bytes) -> list[dict]:
    """ssa.gov → my Social Security → earnings record (XML download).
    Namespace-agnostic: elements whose tag ends with 'Earnings' carrying
    startYear, with Fica/Medicare children. SSA writes -1 for years not
    yet recorded — skipped."""
    root = ET.fromstring(data)
    rows = {}
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag != "Earnings" or "startYear" not in el.attrib:
            continue
        try:
            year = int(el.attrib["startYear"])
        except ValueError:
            continue
        fica = medicare = None
        for child in el:
            ctag = child.tag.rsplit("}", 1)[-1]
            v = _num(child.text or "")
            if ctag == "FicaEarnings":
                fica = v
            elif ctag == "MedicareEarnings":
                medicare = v
        row = {"year": year}
        if fica is not None and fica >= 0:
            row["ss_earnings"] = fica
        if medicare is not None and medicare >= 0:
            row["medicare_earnings"] = medicare
        if len(row) > 1:
            rows[year] = row
        # The same bound `commit` puts on client-supplied rows, applied at
        # the parse: every row here is previewed, staged as JSON and echoed
        # in the response, and a crafted earnings record can carry far more
        # <Earnings> elements than a working life has years.
        if len(rows) > MAX_ROWS:
            raise ValueError(
                f"this file lists more than {MAX_ROWS} years of earnings — "
                f"is it the ssa.gov earnings-record XML?")
    return [rows[y] for y in sorted(rows)]


# Label/value patterns for the transcript text. Every whitespace run is
# HORIZONTAL ([ \t]) and every run sits between two different character
# classes, so no two adjacent quantifiers can both claim the same spaces:
# a `\s*` under re.M runs across newlines, so on a file of blank lines it
# would consume the rest of the document from every line start and then
# backtrack — quadratic work inside one C call that holds the GIL for the
# whole server. The value may still sit on the line below its label (one
# optional line break), since a wrapped transcript prints it there.
_LABEL_TAIL = r"[ \t:.]*(?:\r?\n[ \t]*)?\$?([-()\d,.]+)"
_TRANSCRIPT_FIELDS = (
    ("total_income", r"^[ \t]*TOTAL INCOME" + _LABEL_TAIL),
    ("agi", r"^[ \t]*ADJUSTED GROSS INCOME" + _LABEL_TAIL),
    ("taxable_income", r"^[ \t]*TAXABLE INCOME" + _LABEL_TAIL),
    ("tax_paid",
     r"^[ \t]*TOTAL TAX(?: LIABILITY(?:[ \t]+[A-Z]+)*)?" + _LABEL_TAIL),
    ("wages", r"^[ \t]*WAGES, SALARIES, TIPS, ETC\.?" + _LABEL_TAIL),
)


def parse_transcript_text(text: str) -> list[dict]:
    """IRS return transcript (Get Transcript text/PDF): uniform LABEL:
    $AMOUNT lines plus a TAX PERIOD marker."""
    m = re.search(r"TAX PERIOD:\s*(?:Dec(?:ember|\.)?\s*31,?\s*)?(\d{4})",
                  text, re.I)
    if not m:
        return []
    row: dict = {"year": int(m.group(1))}
    for field, pat in _TRANSCRIPT_FIELDS:
        fm = re.search(pat, text, re.I | re.M)
        if fm:
            v = _num(fm.group(1))
            if v is not None:
                row[field] = v
    return [row] if len(row) > 1 else []


# ---- LLM-assisted parsers ----------------------------------------------------

def _endpoint_is_local(url: str) -> bool:
    """True when the configured LLM lives on this box or this LAN.

    The bundled Ollama, a `localhost` override, or a private-range host are
    all "the data never leaves your control". Anything else is a third party,
    however legitimate.

    Judged by ADDRESS wherever an address can be had, because this gates
    whether an SSN-bearing page image may leave the box: an IP literal is
    classified directly (IPv4-mapped IPv6 folded to its embedded v4 first),
    and a hostname is resolved — it is local only when every resolved
    address is non-global. A name that merely LOOKS local (.local/.internal)
    but resolves to the public internet does not skip the consent gate; the
    suffix heuristic applies only when the name does not resolve at all
    (mDNS names often don't from inside a container).
    """
    import ipaddress
    from urllib.parse import urlsplit
    from ..web import netguard

    def _non_global(ip: str) -> bool:
        addr = ipaddress.ip_address(ip)
        mapped = getattr(addr, "ipv4_mapped", None)
        return not (mapped or addr).is_global

    host = (urlsplit(url or "").hostname or "").strip("[]").lower()
    if host in ("localhost", "ollama", "127.0.0.1", "::1"):
        return True
    try:
        return _non_global(host)
    except ValueError:
        pass                # a hostname, not an IP literal
    ips = netguard._resolved_ips(host)
    if ips:
        return all(_non_global(ip) for ip in ips)
    return host.endswith((".local", ".internal"))


def read_context(conn) -> dict:
    """Everything a parse needs from the household, read up front so the
    connection can go back to the pool before the model is asked: the
    vision backend and whether a sensitive document may leave the box. A
    W-2 or 1040 read waits seconds to minutes on the model, and a pooled
    connection held across that wait is one fewer for every household."""
    from . import budget
    from . import llm_categorize as llm
    cfg = budget.load_config(conn)
    return {"backend": dict(llm._backend(conn, role="vision") or {}),
            "remote_ok": str(cfg.get("taxdocs_allow_remote_llm") or "")
            .strip().lower() in ("1", "true", "yes", "on")}


def _ctx(conn_or_ctx) -> dict:
    return (conn_or_ctx if isinstance(conn_or_ctx, dict)
            else read_context(conn_or_ctx))


def _require_offbox_consent(conn, kind: str) -> None:
    """A tax document is not a receipt.

    W-2s and 1040s carry a full SSN, and the vision path ships the whole
    page image to whatever OpenAI-compatible endpoint the tenant configured
    — which netguard deliberately allows to be a third party, since BYO-LLM
    is the point. The consent a user gives for receipts does not cover
    transmitting an SSN.

    Local endpoints (bundled Ollama, LAN, loopback) are unaffected: nothing
    leaves the user's control, which is the self-host default. Sending to a
    remote endpoint takes a deliberate, separately-named opt-in.
    """
    ctx = _ctx(conn)
    url = ctx["backend"].get("url") or ""
    if _endpoint_is_local(url):
        return
    if ctx["remote_ok"]:
        return
    raise ValueError(
        f"reading a {kind} would send an image of the whole document — "
        f"including your Social Security number — to the LLM endpoint you "
        f"configured, which is not on this machine or network. Enter the "
        f"figures by hand, point Settings → AI at a local "
        f"model, or turn on 'allow sensitive documents to be sent to my "
        f"remote "
        f"model' in Settings → AI if you intend to send it.")


def _chat_json(conn, messages, keys: tuple[str, ...],
               max_tokens=800) -> dict:
    """The model's answer object. `keys` is the prompt's whole schema: the
    object naming the most of it wins, so a stray `{"year": ...}` before or
    after the answer never displaces it."""
    from . import llm_categorize as llm
    backend = _ctx(conn)["backend"]
    if not backend.get("url"):
        raise ValueError("this document type needs the LLM — configure it "
                         "under Settings → AI")
    vision = any(isinstance(m.get("content"), list) for m in messages)
    if vision:
        vm = str(backend.get("vision_model") or "").strip()
        if vm:
            backend = {**backend, "model": vm}
    raw = llm._chat(messages, max_tokens=max_tokens, backend=backend)
    out = llm.json_answer(raw, keys)
    # the model is untrusted input: a bare number, string or list is just
    # as unparseable as invalid JSON, and every caller reads fields off an
    # object
    if not isinstance(out, dict):
        raise ValueError("the model returned unparseable output — try a "
                         "clearer scan or a different model")
    return out


def _is_num(v) -> bool:
    """A JSON number. `bool` is an int subclass in Python, so a `true`
    would otherwise pass as 1 and reach a DOUBLE column as a boolean."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _year_of(out: dict, complaint: str) -> int:
    """The model's `year`, or a ValueError the upload endpoint turns into
    a clean 400. int() alone raises TypeError on a null year, which the
    endpoint does not catch — a 500 in the browser for a bad scan."""
    try:
        return int(out["year"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(complaint)


BUSY_ERROR = ("server is busy reading documents — try again in a "
              "minute")


def _require_vision(conn) -> None:
    """Refuse before any decode when there is no model to read the image:
    a decode with nowhere to send its result is memory spent for an error
    the caller would have had for free."""
    if not _ctx(conn)["backend"].get("url"):
        raise ValueError("this document type needs the LLM — configure it "
                         "under Settings → AI")


def _w2_image(data: bytes, mime: str) -> tuple[bytes, str]:
    """The W-2 as an image the vision model can take, decoded under the
    receipts' decode gate and inside their measured limits.

    The tax-document door shares the receipt decoder and must share its
    bounds: a few-KB PNG can declare tens of millions of pixels, and a
    burst of them decoded side by side on the request threads holds
    gigabytes at once on a shared server. So an image is held to the
    receipt byte cap and pixel ceiling — an image whose size cannot be read
    is refused rather than sent on raw — and every decode, PDF or image,
    waits its turn at the same gate the receipt parses queue on."""
    import io

    from PIL import Image

    from . import receipts
    if mime != "application/pdf":
        if mime not in receipts.IMAGE_MIMES:
            raise ValueError("a W-2 image must be PNG, JPEG, WebP or GIF")
        if len(data) > receipts.MAX_IMAGE:
            raise ValueError("this image is too large (5 MB max) — "
                             "upload a smaller scan or the PDF")
        try:
            with Image.open(io.BytesIO(data)) as im:
                px = (im.width or 0) * (im.height or 0)
        except Exception:                         # noqa: BLE001
            raise ValueError("couldn't read this image — upload a PNG or "
                             "JPEG scan of the W-2")
        if not px or px > receipts.MAX_DECODE_PX:
            raise ValueError("this image is too large to read — upload a "
                             "smaller scan or the PDF")
    if not receipts._DECODE_GATE.acquire(timeout=receipts._DECODE_WAIT_S):
        raise ValueError(BUSY_ERROR)
    try:
        if mime == "application/pdf":
            return receipts._pdf_first_page_png(data), "image/png"
        return receipts._shrink_image(data, mime)
    finally:
        receipts._DECODE_GATE.release()


def parse_w2(conn, data: bytes, mime: str) -> list[dict]:
    conn = _ctx(conn)
    _require_offbox_consent(conn, "W-2")
    _require_vision(conn)
    img, mime = _w2_image(data, mime)
    b64 = base64.b64encode(img).decode()
    out = _chat_json(conn, [{"role": "user", "content": [
        {"type": "text", "text": _W2_PROMPT},
        {"type": "image_url",
         "image_url": {"url": f"data:{mime};base64,{b64}"}}]}], _W2_KEYS)
    year = _year_of(out, "couldn't read a W-2 out of this image")
    # non-dict boxes (the model returning a list, say) are dropped rather
    # than fatal — the year row still previews, boxes stay hand-editable
    raw_boxes = out.get("boxes")
    boxes = ({str(k): v for k, v in raw_boxes.items() if _is_num(v)}
             if isinstance(raw_boxes, dict) else {})
    return [{"year": year,
             "employer": str(out.get("employer") or "")[:120] or None,
             # masked at the parse, not at the write: only the last four are
             # ever stored (`_ein_last4`) and nothing reads the rest back, so
             # the full nine digits had no reason to ride the analyze
             # response and sit in the staging row in plaintext. Same shape
             # the business-entity screens show.
             "ein": _ein_masked(out.get("ein")),
             "boxes": boxes,
             "box1": boxes.get("1")}]


def parse_1040_text(conn, text: str) -> list[dict]:
    conn = _ctx(conn)
    # the extracted text of a 1040 carries the SSN just as the image does
    _require_offbox_consent(conn, "tax return")
    out = _chat_json(conn, [{"role": "user",
                             "content": _1040_PROMPT + text[:12000]}],
                     _1040_KEYS)
    row: dict = {"year": _year_of(
        out, "couldn't read a tax return out of this document")}
    for f in ("total_income", "agi", "taxable_income", "tax_paid", "wages"):
        v = out.get(f)
        if _is_num(v):
            row[f] = float(v)
    return [row] if len(row) > 1 else []


# ---- analyze / commit --------------------------------------------------------

def _existing_years(conn) -> set[int]:
    return {r["year"] for r in conn.execute(
        "SELECT year FROM income_annual").fetchall()}


def analyze(conn, filename: str, data: bytes, mime: str) -> dict:
    """Detect + parse one document; stage the result behind a token for
    commit. One connection for all three steps; the upload door runs them
    apart (`read_context`, `parse`, `stage`) so no pooled connection waits
    on the model."""
    ctx = read_context(conn)
    kind, rows = parse(ctx, filename, data, mime)
    return stage(conn, kind, rows, filename)


def parse(ctx: dict, filename: str, data: bytes,
          mime: str) -> tuple[str, list[dict]]:
    """Detect + parse one document with no database at hand. Detection
    order: SSA XML → IRS transcript text → W-2 marker → 1040 (text→LLM);
    images go straight to the W-2 vision path."""
    conn = ctx
    name = (filename or "").lower()
    kind, rows = None, []
    head = data[:4096].lstrip()
    if head.startswith(b"<") or name.endswith(".xml"):
        try:
            rows = parse_ssa_xml(data)
            kind = "ssa"
        except ET.ParseError:
            raise ValueError("this XML didn't parse — download the "
                             "earnings record again from ssa.gov")
        if not rows:
            raise ValueError("no earnings years found — is this the "
                             "ssa.gov earnings-record XML?")
    elif mime.startswith("image/"):
        rows, kind = parse_w2(conn, data, mime), "w2"
    else:
        if mime == "application/pdf" or data[:5] == b"%PDF-":
            from ..sync import pdfimport
            text = pdfimport.extract_text(data)
        else:
            # The PDF branch is capped by extract_text; a plain-text upload
            # gets the same ceiling, since every parser below reads the
            # whole text and nothing a tax document holds comes near it.
            from ..sync.pdfimport import MAX_TEXT_BYTES
            if len(data) > MAX_TEXT_BYTES:
                raise ValueError(
                    "this file is far larger than any tax document — upload "
                    "the transcript, W-2 or return itself")
            text = data.decode("utf-8", errors="replace")
        upper = text.upper()
        if "TAX PERIOD" in upper and "ADJUSTED GROSS INCOME" in upper:
            rows, kind = parse_transcript_text(text), "transcript"
            if not rows:
                rows, kind = parse_1040_text(conn, text), "1040"
        elif "WAGE AND TAX STATEMENT" in upper:
            rows, kind = parse_w2(conn, data, "application/pdf"), "w2"
        elif "1040" in upper or "TAX RETURN" in upper:
            rows, kind = parse_1040_text(conn, text), "1040"
        else:
            raise ValueError(
                "didn't recognize this document — supported: ssa.gov "
                "earnings-record XML, IRS return transcripts, W-2s, and "
                "1040 return PDFs (docs/tax-documents.md has the how-to)")
    if not rows:
        raise ValueError("nothing usable found in this document")
    return kind, rows


def stage(conn, kind: str, rows: list[dict], filename=None) -> dict:
    """Mark which years already exist and stage the rows behind a commit
    token, with the file's name (the stored list shows it)."""
    existing = _existing_years(conn)
    for r in rows:
        r["exists"] = r["year"] in existing
        if kind == "w2":
            # stored W-2s this one looks like: the client asks whether it
            # is the same form (replace) or a second W-2 (keep both)
            r["matches"] = w2_matches(conn, r["year"], r.get("employer"),
                                      r.get("ein"), r.get("box1"))
    # Staged in the tenant-bound table, so a token from tenant A can never
    # be committed under tenant B's conn
    from ..db import staging
    token = staging.put(conn, "taxdoc", meta={
        "kind": kind, "rows": rows, "filename": _clean_filename(filename)})
    return {"token": token, "kind": kind, "rows": rows}


def _ein_last4(ein) -> str | None:
    """Only the last four are kept.

    The same nine digits on `business_entity` are encrypted at rest,
    surfaced only as a last4, and excluded from the export. Nothing reads
    this copy back, so it keeps only the last four rather than a full
    identifier with no reader."""
    digits = re.sub(r"\D", "", str(ein or ""))
    return digits[-4:] if len(digits) >= 4 else None


def _ein_masked(ein) -> str | None:
    """The last four, displayable — the only form of an EIN that leaves this
    module. Re-masking an already-masked value is a no-op, so a row that
    round-trips through the client and back into `commit` still stores the
    same four digits."""
    last4 = _ein_last4(ein)
    return f"•••••{last4}" if last4 else None


class HouseholdErasing(Exception):
    """The household is being deleted: nothing was written."""


ERASING_MSG = "this household is being deleted; nothing was saved"


def _may_write(conn) -> None:
    """Inside the write's transaction, before its first write: refuse a
    household an erasure has begun on. The request authenticated earlier,
    and a row written after the erasure wiped the household lands under a
    tenant id nothing will ever sweep (the same check every file import
    door makes)."""
    from .. import erasure
    tid = conn.execute("SELECT current_setting('app.tenant_id', true) AS t"
                       ).fetchone()["t"]
    if not erasure.push_may_write(conn, tid):
        raise HouseholdErasing(ERASING_MSG)


# A W-2 preview is one document, so a commit carries a handful of rows at
# most; the 20,000 bound is for the earnings record's years.
MAX_W2_ROWS = 50

# Whose a W-2 is (specs/w2-wages.md). The form does not say which member of
# the household it belongs to, so the preview asks; an 'unassigned' W-2 is
# listed and counts toward nobody's wages until its person is named.
W2_OWNERS = ("primary", "spouse")
_WAGE_COLUMN = {"primary": "primary_wages", "spouse": "spouse_wages"}
_W2_PERSON_LABEL = {"primary": "Primary earner", "spouse": "Spouse"}


def w2_people(conn) -> list[dict]:
    """The people a W-2 can be filed under, as the whose-pickers offer them
    ({owner, label}; specs/w2-wages.md, "Who a W-2 can belong to"): both,
    always. Nothing stored says who is in the household — logins do not,
    and no import writes a filing status — so the pickers never narrow on
    a guess. `conn` is kept for a later rule that has real evidence."""
    return [{"owner": o, "label": _W2_PERSON_LABEL[o]} for o in W2_OWNERS]

# "Looks like the same W-2": the same read box 1 and nothing saying it is
# another employer — the EINs' last four agree, or either side's EIN was
# not read (a re-read that missed the EIN on one side is still the form).
# Only ever used to ASK whether an upload is a form already stored: two
# spouses at one employer with the same box 1 hold two W-2s, so nothing is
# merged on it, and asking about a stranger costs one question.


class W2Unanswered(Exception):
    """A W-2 that looks like one already stored was committed without
    saying whether it is that form or a second one: nothing was written.
    `matches` holds, per committed row, the stored W-2s it looks like
    (empty for a row that needs no answer), so the client can ask — the
    lookalike may only have appeared through the person's own edit of the
    preview, which analyzing the file again would never show."""

    def __init__(self, matches: list[list[dict]]):
        super().__init__(UNANSWERED_MSG)
        self.matches = matches


UNANSWERED_MSG = ("this W-2 looks like one already stored — say whether it "
                  "is the same form or a second W-2")


def _box1(v) -> float | None:
    """A box 1 that was read: a number other than zero. An unread box 1 is
    never comparable and counts toward no sum."""
    # a box 1 too large for the database is no box 1 at all: comparing it
    # would overflow, and commit refuses it as the bad input it is
    return float(v) if _is_num(v) and _money(v) and v != 0 else None


def _lock_years(conn, years, bounded: bool) -> None:
    """One writer at a time over a year's W-2s and the wage figures summed
    from them, held to the end of the transaction: a sum taken beside a
    concurrent store would not see the other's document. Sorted, so two
    writers naming the same years cannot deadlock. A person's request
    (`bounded`) waits a few seconds behind a restore, then answers busy."""
    def take():
        for year in sorted(set(years)):
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext('income-documents'), "
                "hashtext(current_setting('app.tenant_id', true) || ':' "
                "|| %s))", (str(year),))
    if bounded:
        from ..sync.reanchor import bounded_lock_wait
        with bounded_lock_wait(conn):
            take()
    else:
        take()


# The years a W-2 can be stored for: the commit refuses any other, and the
# restore refuses a document outside them, so the set of W-2 year locks is
# bounded (a restore takes all of them up front).
W2_YEARS = range(1900, 2101)


def reconcile_unmarked(conn, year: int) -> None:
    """For W-2s stored by a build that never asked whose they were (every
    one landed under the primary earner): when the year's W-2-owned primary
    figure disagrees with what its primary W-2s add up to, only the W-2
    the figure stands for (its box 1 is the figure; failing that, the first
    stored) stays the primary earner's and the rest become unassigned —
    a spouse's form or a re-upload must not be summed in. If the figure
    still disagrees, it was never the W-2s' sum and stops belonging to
    them. Migration 227 applies the same rule to stored data; a restore of
    an archive older than 226 applies it per year. No figure moves here."""
    row = conn.execute(
        """SELECT primary_wages, primary_wages_w2,
                  (SELECT SUM(primary_amount) FROM income_documents
                    WHERE year = %(y)s AND form = 'W-2' AND owner = 'primary'
                      AND primary_amount IS NOT NULL
                      AND primary_amount <> 0) AS total
             FROM income_annual WHERE year = %(y)s""", {"y": year}).fetchone()
    # compared in cents: the stored figure may be a sum taken in another
    # order, and a last-bit difference must not unassign a person's W-2s
    if (row is None or row["primary_wages_w2"] is None
            or (row["total"] is not None
                and round(row["total"], 2)
                == round(row["primary_wages_w2"], 2))):
        return
    keep = conn.execute(
        """SELECT id FROM income_documents
            WHERE year = %s AND form = 'W-2' AND owner = 'primary'
            ORDER BY (primary_amount IS NOT DISTINCT FROM %s) DESC, id
            LIMIT 1""", (year, row["primary_wages"])).fetchone()
    if keep:
        conn.execute(
            "UPDATE income_documents SET owner = 'unassigned' "
            "WHERE year = %s AND form = 'W-2' AND owner = 'primary' "
            "AND id <> %s", (year, keep["id"]))
    conn.execute(
        """UPDATE income_annual SET primary_wages_w2 = NULL
            WHERE year = %(y)s
              AND round(primary_wages_w2::numeric, 2) IS DISTINCT FROM (
                SELECT round(SUM(primary_amount)::numeric, 2)
                  FROM income_documents
                 WHERE year = %(y)s AND form = 'W-2' AND owner = 'primary'
                   AND primary_amount IS NOT NULL
                   AND primary_amount <> 0)""", {"y": year})


def resum(conn, year: int) -> None:
    """THE rule for a person's W-2 wages (specs/w2-wages.md): each person's
    figure for the year becomes the sum of box 1 over their W-2s whose box 1
    was read — when the figure is the W-2s' to set: it is empty, or it
    still equals the value the W-2s last wrote (`<column>_w2`). Any other
    writer moved it away from that value and keeps it. A sum over no W-2
    is NULL, which empties a figure only the W-2s had set. Run under
    `_lock_years`."""
    for owner, col in _WAGE_COLUMN.items():
        mark = f"{col}_w2"
        conn.execute(
            f"""WITH s AS (
                    SELECT SUM(primary_amount) AS total
                      FROM income_documents
                     WHERE year = %(y)s AND form = 'W-2' AND owner = %(o)s
                       AND primary_amount IS NOT NULL
                       AND primary_amount <> 0)
                INSERT INTO income_annual (year, {col}, {mark}, source)
                SELECT %(y)s, s.total, s.total, 'w2' FROM s
                 WHERE s.total IS NOT NULL
                ON CONFLICT (tenant_id, year) DO UPDATE SET
                    {col} = EXCLUDED.{col},
                    {mark} = EXCLUDED.{col},
                    source = CASE
                        WHEN income_annual.source IS NULL THEN 'w2'
                        WHEN 'w2' = ANY(string_to_array(
                                 income_annual.source, '+'))
                        THEN income_annual.source
                        ELSE income_annual.source || '+w2' END
                 WHERE income_annual.{col} IS NULL
                    OR income_annual.{col} IS NOT DISTINCT FROM
                       income_annual.{mark}""",
            {"y": year, "o": owner})
        # no W-2 left for this person: the figure the W-2s set goes empty
        # (the INSERT above inserts nothing for an empty sum)
        conn.execute(
            f"""UPDATE income_annual SET {col} = NULL, {mark} = NULL
                 WHERE year = %(y)s AND {mark} IS NOT NULL
                   AND {col} IS NOT DISTINCT FROM {mark}
                   AND NOT EXISTS (
                       SELECT 1 FROM income_documents
                        WHERE year = %(y)s AND form = 'W-2'
                          AND owner = %(o)s
                          AND primary_amount IS NOT NULL
                          AND primary_amount <> 0)""",
            {"y": year, "o": owner})


def unsaid_owner(conn, year: int) -> str:
    """Whose a W-2 is when nobody said (an app older than the question, an
    archive older than migration 221): the primary earner's, as every W-2
    was, unless the year already holds a primary W-2 — exactly the case
    where it may be the spouse's — and then nobody's until named."""
    taken = conn.execute(
        "SELECT 1 FROM income_documents WHERE year = %s AND form = 'W-2' "
        "AND owner = 'primary' LIMIT 1", (year,)).fetchone()
    return "unassigned" if taken else "primary"


def w2_matches(conn, year: int, employer, ein, box1) -> list[dict]:
    """Stored W-2s of the year that look like this one (same read box 1,
    no EIN saying it is another employer) — what the preview asks about,
    and what a restore checks an archived W-2 against. An unread box 1
    matches nothing. `employer` is shown back, never compared."""
    b1 = _box1(box1)
    if b1 is None:
        return []
    last4 = _ein_last4(ein)
    return [{"id": r["id"], "owner": _shown_owner(r["owner"]),
             "employer": r["payer"], "box1": r["primary_amount"]}
            for r in conn.execute(
                """SELECT id, owner, payer, primary_amount
                     FROM income_documents
                    WHERE year = %(y)s AND form = 'W-2'
                      AND primary_amount = %(b)s
                      AND (ein_last4 IS NULL OR %(e)s::text IS NULL
                           OR ein_last4 = %(e)s)
                    ORDER BY id LIMIT 20""",
                {"y": year, "b": b1, "e": last4}).fetchall()]


def _shown_owner(owner) -> str:
    return owner if owner in W2_OWNERS else "unassigned"


def lock_document_ids(conn, bounded: bool = False) -> None:
    """One writer at a time allocates a household's document ids, to the
    end of the transaction: ids are MAX+1 across every year, so two
    writers of different years (a store beside a restore) would otherwise
    pick the same one. Always taken AFTER the year locks; a person's
    request (`bounded`) answers busy behind a restore, as at the year
    locks."""
    def take():
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext('income-document-ids'), "
            "hashtext(current_setting('app.tenant_id', true)))")
    if bounded:
        from ..sync.reanchor import bounded_lock_wait
        with bounded_lock_wait(conn):
            take()
    else:
        take()


def _new_document_id(conn) -> int:
    """income_documents has no identity — original integer ids travel in
    restores — so the next id is MAX+1, under `lock_document_ids`."""
    lock_document_ids(conn, bounded=True)
    return conn.execute("SELECT COALESCE(MAX(id), 0) + 1 AS id "
                        "FROM income_documents").fetchone()["id"]


def _store_w2(conn, year: int, owner: str, replaces: int | None, payer,
              ein, primary_amount, amounts_json: str,
              upload_id: int | None = None) -> bool:
    """Store one W-2: a new row, or, with `replaces`, that stored W-2 of
    the same year updated in place (the person said it is the same form;
    under the other person that is a move). True when it replaced."""
    last4 = _ein_last4(ein)
    if replaces is not None:
        hit = conn.execute(
            """UPDATE income_documents
                  SET owner = %s, payer = %s, ein_last4 = %s,
                      primary_amount = %s, amounts = %s,
                      upload_id = COALESCE(%s, upload_id)
                WHERE id = %s AND year = %s AND form = 'W-2'""",
            (owner, payer, last4, primary_amount, amounts_json, upload_id,
             replaces, year)).rowcount
        if not hit:
            raise ValueError("the W-2 this replaces is no longer stored "
                             "for that year — analyze the file again")
        return True
    for _ in range(50):
        cur = conn.execute(
            """INSERT INTO income_documents
                   (id, year, form, owner, payer, ein_last4, primary_amount,
                    amounts, upload_id)
               VALUES (%s, %s, 'W-2', %s, %s, %s, %s, %s, %s)
               ON CONFLICT (tenant_id, id) DO NOTHING""",
            (_new_document_id(conn), year, owner, payer, last4,
             primary_amount, amounts_json, upload_id))
        if cur.rowcount:
            return False
    raise ValueError("couldn't allocate a document id — please retry")


def list_w2s(conn) -> list[dict]:
    """The stored W-2s, newest year first — what the Import page lists so
    one can be removed or given to the right person."""
    return [{"id": r["id"], "year": r["year"], "employer": r["payer"],
             "ein": f"•••••{r['ein_last4']}" if r["ein_last4"] else None,
             "owner": _shown_owner(r["owner"]),
             "box1": r["primary_amount"]}
            for r in conn.execute(
                """SELECT id, year, payer, ein_last4, owner, primary_amount
                     FROM income_documents WHERE form = 'W-2'
                    ORDER BY year DESC, id LIMIT 2000""").fetchall()]


class W2Changed(Exception):
    """The stored W-2 a request named is not the one the person was shown:
    a document's id is a number within its household and comes back after
    a removal, so a stale list's id may now name another form."""


W2_CHANGED_MSG = "this W-2 changed since the list was shown — reload it"


def _seen(seen) -> tuple:
    """What the stored list showed for a W-2 — year, box 1, employer, EIN
    (masked) and whose — which a removal or an owner change must still find
    under the id. Two spouses' W-2s from one employer share a box 1, so
    box 1 alone would not tell them apart."""
    if not isinstance(seen, dict) or "year" not in seen or "box1" not in seen:
        raise ValueError("say which W-2: its `year` and `box1` as listed")
    year, box1 = seen["year"], seen["box1"]
    if isinstance(year, bool) or not isinstance(year, int):
        raise ValueError("`year` must be the W-2's year")
    if box1 is not None:
        try:
            ok = (not isinstance(box1, bool) and _is_num(box1)
                  and math.isfinite(float(box1)))
        except OverflowError:
            ok = False
        if not ok:
            raise ValueError("`box1` must be the W-2's box 1 as listed")
        box1 = float(box1)
    for f in ("employer", "ein", "owner"):
        if seen.get(f) is not None and not isinstance(seen[f], str):
            raise ValueError(f"`{f}` must be text")
    return (year, box1, seen.get("employer"), _ein_last4(seen.get("ein")),
            seen.get("owner"))


def _w2_year(conn, doc_id: int, seen: tuple) -> int | None:
    """The year of the stored W-2 under `doc_id`, when it is still the one
    the person was shown; None when there is none; W2Changed when the id
    now names another form. A client that sends only year and box 1 is
    held to those."""
    row = conn.execute(
        "SELECT year, primary_amount, payer, ein_last4, owner "
        "FROM income_documents WHERE id = %s AND form = 'W-2'",
        (doc_id,)).fetchone()
    if row is None:
        return None
    year, box1, employer, last4, owner = seen
    if ((row["year"], row["primary_amount"]) != (year, box1)
            or (employer is not None and row["payer"] != employer)
            or (last4 is not None and row["ein_last4"] != last4)
            or (owner is not None and _shown_owner(row["owner"]) != owner)):
        raise W2Changed(W2_CHANGED_MSG)
    return year


def delete_w2(conn, doc_id: int, seen) -> bool:
    """Remove one stored W-2 — the one the list showed (`seen`: its year
    and box 1) — and sum its year again. False when there is no such W-2."""
    shown = _seen(seen)
    with conn.transaction():
        year = _w2_year(conn, doc_id, shown)
        if year is None:
            return False
        _may_write(conn)
        _lock_years(conn, [year], bounded=True)
        # the form under the id may have changed while the year was
        # waited for: the list is checked again once the year is ours
        if _w2_year(conn, doc_id, shown) is None:
            return False
        if not conn.execute(
                "DELETE FROM income_documents WHERE id = %s AND form = 'W-2' "
                "AND year = %s AND primary_amount IS NOT DISTINCT FROM %s",
                (doc_id, year, shown[1])).rowcount:
            return False
        resum(conn, year)
    return True


def set_w2_owner(conn, doc_id: int, owner: str, seen) -> bool:
    """Say whose a stored W-2 is — the one the list showed (`seen`) — and
    sum its year again. An identical W-2 under the other person is never
    refused: that is two people's forms. False when there is no such W-2."""
    if owner not in W2_OWNERS:
        raise ValueError("`owner` must be primary or spouse")
    shown = _seen(seen)
    with conn.transaction():
        year = _w2_year(conn, doc_id, shown)
        if year is None:
            return False
        _may_write(conn)
        _lock_years(conn, [year], bounded=True)
        if _w2_year(conn, doc_id, shown) is None:
            return False
        if not conn.execute(
                "UPDATE income_documents SET owner = %s WHERE id = %s "
                "AND form = 'W-2' AND year = %s "
                "AND primary_amount IS NOT DISTINCT FROM %s",
                (owner, doc_id, year, shown[1])).rowcount:
            # gone between the look and the lock
            return False
        resum(conn, year)
    return True


def _money(v) -> bool:
    """A box or amount the database and every yearly sum can hold: finite
    and within a plausible bound. Same rule as the split editor's."""
    from .splits import _money as money
    return money(v)


def commit(conn, token: str, rows: list[dict], stored_by=None) -> dict:
    """Write the (possibly user-edited) previewed rows. Annual kinds merge
    per column into income_annual; a W-2 is stored under the person the row
    names (or replaces the stored one it says it is) and each person's
    wages for its year are summed again (`resum`, specs/w2-wages.md)."""
    from ..db import staging
    if not isinstance(rows, list):
        raise ValueError("rows must be a list")
    if len(rows) > MAX_ROWS:
        raise ValueError(f"too many rows (max {MAX_ROWS})")
    # Validate EVERY row before anything is written or the token is spent:
    # validating inside the write loop, after `staging.pop` burned the
    # single-use preview token, would leave a bad year on row five with rows
    # one to four imported, the token gone, and no way to retry.
    years: list[int] = []
    for r in rows:
        try:
            year = int(r["year"])
        except (KeyError, TypeError, ValueError, OverflowError):
            # OverflowError: JSON's Infinity is a float int() cannot take
            raise ValueError("every row needs a year")
        if not 1900 <= year <= 2100:
            raise ValueError(f"implausible year {year}")
        years.append(year)

    # One transaction over the token AND the writes: a failure here rolls the
    # pop back too, so the preview survives and the person can try again.
    with conn.transaction():
        stash = staging.pop(conn, "taxdoc", token)
        if stash is None:
            raise ValueError("this preview expired — analyze the file again")
        kind = (stash["meta"] or {}).get("kind")
        if kind == "w2" and len(rows) > MAX_W2_ROWS:
            raise ValueError(f"too many W-2 rows (max {MAX_W2_ROWS})")
        _may_write(conn)
        return _commit_rows(conn, kind, rows, years,
                            filename=(stash["meta"] or {}).get("filename"),
                            stored_by=stored_by)


def _w2_plan(conn, r, year) -> dict:
    """Validate one W-2 row into what `_store_w2` takes. `rows` is
    arbitrary client JSON ("possibly user-edited"), so every field is
    refused as the bad input it is rather than reaching the database."""
    if not isinstance(r, dict):
        raise ValueError("each row must be an object")
    raw_boxes = r.get("boxes")
    if raw_boxes is not None and not isinstance(raw_boxes, dict):
        raise ValueError("each row's `boxes` must be an object")
    boxes = {k: v for k, v in (raw_boxes or {}).items() if _is_num(v)}
    # a NaN/inf reaches DOUBLE PRECISION unchanged and every report that
    # sums the year 500s from then on; a 400-digit JSON integer is an int
    # that math.isfinite itself overflows on
    if not all(_money(v) for v in boxes.values()):
        raise ValueError("every box must be a finite number")
    for f in ("employer", "ein"):
        # bound straight into a text column: anything but text is an
        # adapter error at the database, not a refusal
        if r.get(f) is not None and not isinstance(r[f], str):
            raise ValueError(f"`{f}` must be text")
    # A row from a client that asks whose the W-2 is carries `owner` (and
    # `replaces` when it looked like a stored one); an older app sends
    # neither and is never asked.
    asked = "owner" in r or "replaces" in r
    owner = r.get("owner")
    if owner is not None and owner not in W2_OWNERS:
        raise ValueError("`owner` must be primary or spouse")
    replaces = r.get("replaces")
    if replaces is not None:
        if (isinstance(replaces, bool) or not isinstance(replaces, int)
                or not 0 < replaces < 2 ** 63):
            raise ValueError("`replaces` must be a stored W-2's id")
    box1 = boxes.get("1")
    unanswered: list[dict] = []
    if asked:
        matches = w2_matches(conn, year, r.get("employer"), r.get("ein"),
                             box1)
        if "replaces" not in r:
            unanswered = matches
        elif replaces is not None and replaces not in {
                m["id"] for m in matches}:
            # the form it said it is must still look like this one: an id
            # comes back after a removal and may now name another person's
            # W-2, and a row edited since it was answered is another form
            if matches:
                unanswered = matches
            else:
                raise ValueError(
                    "the stored W-2 this was to replace no longer matches "
                    "it (box 1 or the employer changed) — save it as a "
                    "second W-2 and remove the old one under Stored W-2s")
    return {"unanswered": unanswered,
            "year": year, "owner": owner, "replaces": replaces,
            "payer": r.get("employer"), "ein": r.get("ein"),
            "box1": float(box1) if _is_num(box1) else None,
            "amounts": json.dumps(boxes)}


def _commit_w2s(conn, rows, years, filename=None, stored_by=None) -> dict:
    _lock_years(conn, years, bounded=True)
    # every row is checked against what was stored BEFORE this commit, and
    # before anything is written: a refusal leaves the preview to retry
    plans = [_w2_plan(conn, r, y) for r, y in zip(rows, years)]
    targets = [p["replaces"] for p in plans if p["replaces"] is not None]
    if len(targets) != len(set(targets)):
        # two rows cannot both be the one stored form
        raise ValueError("two W-2s say they replace the same stored one")
    if any(p["unanswered"] for p in plans):
        raise W2Unanswered([p["unanswered"] for p in plans])
    docs = replaced = 0
    adopt_unexplained(conn, sorted(set(years)))
    upload = record_upload(conn, "w2", filename, stored_by)
    for p in plans:
        owner = p["owner"]
        if owner is None and p["replaces"] is not None:
            # the same form again, nobody named: it stays whose it was
            row = conn.execute(
                "SELECT owner FROM income_documents WHERE id = %s "
                "AND form = 'W-2'", (p["replaces"],)).fetchone()
            owner = row["owner"] if row else None
        if owner is None:
            owner = unsaid_owner(conn, p["year"])
        replaced += _store_w2(conn, p["year"], owner, p["replaces"],
                              p["payer"], p["ein"], p["box1"],
                              p["amounts"], upload_id=upload)
        docs += 1
    for year in sorted(set(years)):
        resum(conn, year)
    # a stored W-2 this upload replaced leaves its own upload empty: an
    # empty upload is no document, and is not kept
    conn.execute(
        """DELETE FROM income_uploads u
            WHERE u.kind = 'w2' AND u.removed_at IS NULL AND u.id <> %s
              AND NOT EXISTS (SELECT 1 FROM income_documents d
                               WHERE d.upload_id = u.id)
              AND NOT EXISTS (SELECT 1 FROM income_upload_values v
                               WHERE v.upload_id = u.id)""", (upload,))
    return {"kind": "w2", "updated": 0, "created": 0, "documents": docs,
            "replaced": replaced}


def _commit_rows(conn, kind, rows, years, *, filename=None,
                 stored_by=None) -> dict:
    """Store one document: an upload row, the columns it writes per year,
    then each year resolved (`resolve_year`) — the newest document's value
    is each column's, and a wage figure it writes is no longer the W-2s'
    to move, even when it happens to be the same number."""
    if kind == "w2":
        return _commit_w2s(conn, rows, years, filename, stored_by)
    if kind not in ("ssa", "transcript", "1040"):
        raise ValueError(f"unknown document kind {kind!r}")
    plans: list[tuple[int, dict]] = []
    for r, year in zip(rows, years):
        if not isinstance(r, dict):
            raise ValueError("each row must be an object")
        picked = {f: r[f] for f in ANNUAL_FIELDS if _is_num(r.get(f))}
        if not all(_money(v) for v in picked.values()):
            raise ValueError("every amount must be a finite number")
        fields = {f: float(v) for f, v in picked.items()}
        if fields:
            plans.append((year, fields))
    if not plans:
        return {"kind": kind, "updated": 0, "created": 0, "documents": 0}
    _lock_years(conn, [y for y, _f in plans], bounded=True)
    existing = _existing_years(conn)
    by_year: dict[int, dict] = {}
    for year, fields in plans:
        # two rows of one year in one document: the later row's columns win
        by_year.setdefault(year, {}).update(fields)
    # what an outside writer left is kept as its own upload first, so
    # this document outranks it and nothing is set back behind its back
    adopt_unexplained(conn, sorted(by_year))
    upload = record_upload(conn, kind, filename, stored_by)
    for year, fields in by_year.items():
        write_upload_values(conn, upload, year, fields)
    for year in sorted(by_year):
        resolve_year(conn, year)
    updated = sum(1 for y in by_year if y in existing)
    return {"kind": kind, "updated": updated,
            "created": len(by_year) - updated, "documents": 0}


# ---- stored documents and what each one wrote ---------------------------
# (specs/tax-documents.md, "Stored documents and what each one wrote")
#
# Every committed document is an upload (income_uploads) with, per year,
# the columns it wrote and their values (income_upload_values). Each
# income_annual column is the value of the NEWEST LIVE upload that wrote
# it (resolve_year), so removing a document hands each column it wrote to
# the next older document that wrote it, or empties it; a wage column no
# live upload sets goes back to the W-2s (resum). A column no upload ever
# wrote for the year — a demo seed, an older build's write — is never
# touched.

UPLOAD_KINDS = ("ssa", "transcript", "1040", "w2", "document",
                "legacy_ssa", "legacy_return")
# how long a removed document can be put back before the nightly purge
UNDO_DAYS = 30
FILENAME_MAX = 255
_WAGE_COLS = frozenset(_WAGE_COLUMN.values())

_KIND_LABEL = {"ssa": "Social Security earnings record",
               "legacy_ssa": "Social Security earnings record",
               "transcript": "IRS transcript",
               "1040": "Tax return (1040)",
               "legacy_return": "Tax return figures",
               "w2": "W-2"}
# the list's groups, in the order they are listed
GROUPS = (("earnings", "Earnings records"), ("returns", "Tax returns"),
          ("w2", "W-2s"), ("other", "Other forms"))
_GROUP_OF = {"ssa": "earnings", "legacy_ssa": "earnings",
             "transcript": "returns", "1040": "returns",
             "legacy_return": "returns", "w2": "w2", "document": "other"}
_WHOSE_LABEL = {"primary": "Primary earner", "spouse": "Spouse",
                # resum sums each person's W-2s: one nobody has claimed is
                # in no one's wages until somebody is named
                "unassigned": "Whose? not yet said — counts toward "
                              "nobody's wages"}


class UploadChanged(Exception):
    """The stored document a request named is not the one the list
    showed: removed, put back or replaced since."""


UPLOAD_CHANGED_MSG = "this document changed since the list was shown — reload it"


def _uuid_or_none(v) -> str | None:
    import uuid as _uuid
    try:
        return str(_uuid.UUID(str(v))) if v else None
    except ValueError:
        return None


# The identifiers a tax document's file name spells when payroll or tax
# software names it after the person or the employer: an SSN (3-2-4) and
# an EIN (2-7), its groups run together or parted by a short run of
# spaces (any Unicode space), dots, underscores or dashes (ASCII, the
# typographic dashes, the minus sign). Bounded runs, so the match stays
# linear. Only this path masks them: the card-number grammar is shared
# with ledger names, where these shapes are not expected.
_TAX_SEP = r"(?:[\s._\-\u2010-\u2015\u2212]{0,3})"
_TAX_ID_RE = re.compile(
    rf"(?<![0-9])(?:[0-9]{{3}}{_TAX_SEP}[0-9]{{2}}{_TAX_SEP}"
    rf"|[0-9]{{2}}{_TAX_SEP}[0-9]{{3}})([0-9]{{4}})(?![0-9])")
# fullwidth digits read as the digits they show
_FULLWIDTH_DIGITS = {0xFF10 + i: ord("0") + i for i in range(10)}


def _clean_filename(name) -> str | None:
    """A file name as the list shows it: card numbers cut to their last
    four (as an import batch's are), an SSN or EIN it spells likewise,
    bounded, never a path. Every reader of a stored name passes it through
    here too, so a name an earlier build kept whole is shown masked."""
    if not isinstance(name, str) or not name.strip():
        return None
    from ..sync import card_number
    base = re.split(r"[\\/]", name.strip())[-1]
    base = base.translate(_FULLWIDTH_DIGITS)
    cut = _TAX_ID_RE.sub(card_number.MARK + r"\1", card_number.cut(base))
    return cut[:FILENAME_MAX] or None


def _new_upload_id(conn) -> int:
    """Upload ids are MAX+1 like document ids (they travel in restores),
    allocated under the same lock."""
    lock_document_ids(conn, bounded=True)
    return conn.execute("SELECT COALESCE(MAX(id), 0) + 1 AS id "
                        "FROM income_uploads").fetchone()["id"]


def record_upload(conn, kind: str, filename=None, stored_by=None) -> int:
    """One stored document's row; its id."""
    if kind not in UPLOAD_KINDS:
        raise ValueError(f"unknown document kind {kind!r}")
    uid = _new_upload_id(conn)
    # dated by the clock when it is written, after the years' locks: the
    # commit that wrote last ranks last, whatever its transaction began at
    conn.execute(
        "INSERT INTO income_uploads (id, kind, filename, stored_by, "
        "stored_at) VALUES (%s, %s, %s, %s::uuid, clock_timestamp())",
        (uid, kind, _clean_filename(filename), _uuid_or_none(stored_by)))
    return uid


def _source_words(conn, year: int) -> str | None:
    """`source` as older readers read it: the live uploads that gave the
    year a figure, and 'w2' when W-2s are stored for it."""
    kinds = [r["kind"] for r in conn.execute(
        """SELECT DISTINCT u.kind FROM income_upload_values v
             JOIN income_uploads u ON u.id = v.upload_id
            WHERE v.year = %s AND u.removed_at IS NULL
              AND v.fields <> '{}'::jsonb
            ORDER BY u.kind""", (year,)).fetchall()]
    words = []
    for k in kinds:
        w = {"legacy_ssa": "ssa", "legacy_return": "return"}.get(k, k)
        if w not in words:
            words.append(w)
    if conn.execute("SELECT 1 FROM income_documents WHERE year = %s "
                    "AND form = 'W-2' LIMIT 1", (year,)).fetchone():
        words.append("w2")
    return "+".join(words) or None


def resolve_year(conn, year: int, *, prune: bool = False) -> None:
    """THE rule for a year's stored income figures: each column an upload
    ever wrote for the year is the value the newest live upload (stored_at,
    then id) wrote into it, else empty; a wage column none sets is handed
    back to the W-2s (`resum`). With `prune` (a document was taken away),
    a year left with no figure, no note, no filing
    status and no W-2 is removed. Run under
    `_lock_years`."""
    rows = conn.execute(
        """SELECT u.removed_at IS NULL AS live, v.fields
             FROM income_upload_values v
             JOIN income_uploads u ON u.id = v.upload_id
            WHERE v.year = %s
            ORDER BY u.stored_at, u.id""", (year,)).fetchall()
    ever: set[str] = set()
    now: dict[str, float] = {}
    for r in rows:
        f = r["fields"] if isinstance(r["fields"], dict) else {}
        cols = [c for c in f if c in ANNUAL_FIELDS and _is_num(f[c])]
        ever.update(cols)
        if r["live"]:
            now.update({c: float(f[c]) for c in cols})
    if ever:
        cols = sorted(ever)
        held = conn.execute("SELECT * FROM income_annual WHERE year = %s",
                            (year,)).fetchone()
        # a wage figure a document sets is no longer the W-2s' to move,
        # even when it is the number they wrote; one it leaves as it was
        # keeps its mark, so working a year out again changes nothing
        sets = [f"{c} = %s" for c in cols] + [
            f"{c}_w2 = NULL" for c in cols if c in _WAGE_COLS and (
                held is None or c not in now or held[c] != now[c]
                or held[f"{c}_w2"] == now[c])]
        vals = [now.get(c) for c in cols]
        # a year whose figures stay as they are is left exactly as it is,
        # its `source` included: working it out again is a no-op
        changed = held is None or any(
            held[c] != now.get(c)
            or (c in _WAGE_COLS and c in now and held[f"{c}_w2"] == now[c])
            for c in cols)
        if held is not None and changed:
            conn.execute(f"UPDATE income_annual SET {', '.join(sets)} "
                         "WHERE year = %s", (*vals, year))
        elif held is None and now:
            conn.execute(
                f"INSERT INTO income_annual (year, {', '.join(cols)}) "
                f"VALUES (%s, {', '.join(['%s'] * len(cols))})",
                (year, *vals))
    else:
        changed = False
    resum(conn, year)
    if changed:
        conn.execute("UPDATE income_annual SET source = %s WHERE year = %s",
                     (_source_words(conn, year), year))
    if not prune:
        return
    nothing = " AND ".join(f"{c} IS NULL" for c in ANNUAL_FIELDS)
    conn.execute(
        f"""DELETE FROM income_annual
             WHERE year = %(y)s AND {nothing} AND note IS NULL
               AND filing_status IS NULL AND COALESCE(joint, 0) = 0
               AND NOT EXISTS (SELECT 1 FROM income_documents
                                WHERE year = %(y)s AND form = 'W-2')""",
        {"y": year})


def write_upload_values(conn, upload_id: int, year: int,
                        fields: dict) -> None:
    conn.execute(
        "INSERT INTO income_upload_values (upload_id, year, fields) "
        "VALUES (%s, %s, %s::jsonb) ON CONFLICT (tenant_id, upload_id, year) "
        "DO UPDATE SET fields = EXCLUDED.fields",
        (upload_id, year, json.dumps(fields)))


def _years_label(years) -> str:
    """"1985–2024", "2019", "2018, 2020–2021"."""
    ys = sorted(set(years))
    out, i = [], 0
    while i < len(ys):
        j = i
        while j + 1 < len(ys) and ys[j + 1] == ys[j] + 1:
            j += 1
        out.append(str(ys[i]) if i == j else f"{ys[i]}–{ys[j]}")
        i = j + 1
    return ", ".join(out)


def _mmddyy(d) -> str:
    return f"{d.month:02d}/{d.day:02d}/{d.year % 100:02d}"


def _local_day(conn, ts):
    from .. import localtime
    from . import budget as _budget
    try:
        return ts.astimezone(
            localtime.tenant_tz(_budget.load_config(conn))).date()
    except OverflowError:
        # a moment at the calendar's edge (a restore by an older build
        # landed year 1): its UTC day, never a failed list
        return ts.date()


_STUB_RE = re.compile(r"^[A-Z0-9&\- ]{1,8}(?: [A-Z0-9&\-]{1,8}){0,3}$")
# an IRS name control of any length: every word cut to four letters or
# fewer ("CEDA RIDG HARB GRIL SERV")
_CUT_WORDS_RE = re.compile(r"^[A-Z0-9&\-]{1,4}(?: [A-Z0-9&\-]{1,4})*$")
_PAREN_RE = re.compile(r"\(([^()]{2,})\)")


def _is_stub(payer: str) -> bool:
    """Whether a payer reads as the IRS's abbreviation of a name: a few
    short capitalised words, or any number of words cut to four letters.
    A transcript line that joins the name control to a second name line
    ("DEPT O REVE / STATE OF …") is judged by the name control before the
    slash."""
    head = payer.split("/", 1)[0].strip()
    return bool(head) and bool(_STUB_RE.match(head)
                               or _CUT_WORDS_RE.match(head))


def payer_shown(conn, payer, ein_last4,
                named: bool = False) -> tuple[str | None, bool]:
    """(name to show, abbreviated) for a form's payer. A name a person
    typed (`named`, income_documents.payer_named_by_person) is shown
    exactly as typed. Otherwise it is read as an IRS transcript prints it:
    the payer's name control — a few capital letters — often with the real
    name in parentheses: that name is shown; failing it, the name another
    stored form with the same EIN last four carries, a typed one first;
    failing that, the abbreviation, marked so the client says the IRS
    abbreviates it."""
    if not isinstance(payer, str) or not payer.strip():
        return None, False
    if named is True:
        return payer, False
    if not isinstance(ein_last4, str):
        ein_last4 = None
    m = _PAREN_RE.search(payer)
    if m:
        return m.group(1).strip(), False
    if not _is_stub(payer.strip()):
        return payer, False
    if ein_last4:
        for r in conn.execute(
                "SELECT payer, payer_named_by_person FROM income_documents "
                "WHERE ein_last4 = %s AND payer IS NOT NULL "
                "ORDER BY payer_named_by_person DESC, id LIMIT 50",
                (ein_last4,)).fetchall():
            p = r["payer"]
            if r["payer_named_by_person"]:
                return p, False
            pm = _PAREN_RE.search(p)
            if pm:
                return pm.group(1).strip(), False
            if not _is_stub(p.strip()):
                return p, False
    return payer.strip(), True


def _years_by(conn, upload_id: int | None = None) -> dict[int, list[int]]:
    """THE rule for the years a stored document covers, per upload (all of
    them, or the one `upload_id`): each year it wrote a figure into, each
    year of a form stored under it, and, once removed, each year of a held
    form a put-back would write back (`_held_form`). A values row that
    gives no figure (an older restore landed some) covers no year. The
    list shows these years and the remove and put-back doors check the
    list's against them, so the two can never disagree."""
    one = "" if upload_id is None else " AND upload_id = %(u)s"
    years: dict[int, set] = {}
    for r in conn.execute(
            f"""SELECT upload_id, year FROM income_upload_values
                 WHERE fields <> '{{}}'::jsonb{one}
                UNION SELECT upload_id, year FROM income_documents
                 WHERE upload_id IS NOT NULL{one}""",
            {"u": upload_id}).fetchall():
        years.setdefault(r["upload_id"], set()).add(r["year"])
    for r in conn.execute(
            "SELECT id, held_documents FROM income_uploads "
            "WHERE removed_at IS NOT NULL AND held_documents IS NOT NULL"
            + ("" if upload_id is None else " AND id = %(u)s"),
            {"u": upload_id}).fetchall():
        held = r["held_documents"]
        for d in map(_held_form, held if isinstance(held, list) else []):
            if d is not None:
                years.setdefault(r["id"], set()).add(d["year"])
    return {u: sorted(ys) for u, ys in years.items()}


def _upload_years(conn, upload_id: int) -> list[int]:
    return _years_by(conn, upload_id).get(upload_id, [])


def _held(conn, upload_id: int) -> list | None:
    r = conn.execute("SELECT held_documents FROM income_uploads "
                     "WHERE id = %s", (upload_id,)).fetchone()
    h = r["held_documents"] if r else None
    return h if isinstance(h, list) else None


def _stored_at_iso(ts) -> str:
    return ts.isoformat()


def list_stored(conn) -> list[dict]:
    """Every stored tax and income document — what the Import page lists
    so each can be removed, and a removed one put back for UNDO_DAYS. The
    words are the server's; `seen` is what a removal sends back."""
    import datetime as _dt
    uploads = conn.execute(
        """SELECT id, kind, filename, stored_at, stored_by, removed_at,
                  held_documents,
                  stored_at < 'epoch'::timestamptz + interval '1 year'
                      AS before_list,
                  (now() - interval '%s days') AS undo_from
             FROM income_uploads
            ORDER BY stored_at DESC, id DESC LIMIT 5000""" % UNDO_DAYS
    ).fetchall()
    docs_by: dict[int, list] = {}
    for d in conn.execute(
            """SELECT id, upload_id, year, form, owner, payer, ein_last4,
                      primary_amount, payer_named_by_person
                 FROM income_documents WHERE upload_id IS NOT NULL
                ORDER BY year DESC, id""").fetchall():
        docs_by.setdefault(d["upload_id"], []).append(d)
    years_by = _years_by(conn)
    out = []
    for u in uploads:
        removed = u["removed_at"] is not None
        if removed and u["removed_at"] < u["undo_from"]:
            continue                      # past undo: the purge's
        docs = docs_by.get(u["id"], [])
        if removed:
            # a held form is what the removal wrote, or what a restore
            # landed: listed as the put-back would write it back, and one
            # that is not a readable form is left out, never a failed list
            held = u["held_documents"]
            docs = [d for d in map(_held_form,
                                   held if isinstance(held, list) else [])
                    if d is not None]
        years = years_by.get(u["id"], [])
        if not years:
            continue                      # holds nothing any more
        kind = u["kind"]
        if kind not in _GROUP_OF:
            continue
        form = docs[0].get("form") if docs else None
        form = form if isinstance(form, str) and form else "Form"
        label = form if kind == "document" else _KIND_LABEL[kind]
        w2s, owners = [], set()
        employer, abbreviated, payer = None, False, None
        for d in docs:
            shown, abbr = payer_shown(conn, d.get("payer"),
                                      d.get("ein_last4"),
                                      d.get("payer_named_by_person") is True)
            if employer is None and shown:
                # the stored name behind the name shown: what a rename
                # names the forms by
                payer = d.get("payer")
            employer = employer or shown
            abbreviated = abbreviated or abbr
            if d.get("form") == "W-2":
                owners.add(_shown_owner(d.get("owner")))
                w2s.append({"id": d.get("id"), "year": d.get("year"),
                            "employer": d.get("payer"),
                            "employer_shown": shown, "abbreviated": abbr,
                            "ein": (f"•••••{d['ein_last4']}"
                                    if d.get("ein_last4") else None),
                            "owner": _shown_owner(d.get("owner")),
                            "box1": d.get("primary_amount")})
        whose = (_WHOSE_LABEL[owners.pop()] if len(owners) == 1
                 else "Two people" if owners else None)
        # stored before the list existed (dated at the epoch by the
        # migration, a restore's backfill or a form given an upload), or
        # figures a writer outside the list left, found when they were
        day = _local_day(conn, u["stored_at"])
        stored_label = (
            "Stored earlier, date not kept" if u["before_list"]
            else f"Written outside this list, found {_mmddyy(day)}"
            if kind.startswith("legacy_") else f"Stored {_mmddyy(day)}")
        removed_label = None
        if removed:
            until = _local_day(conn, u["removed_at"]
                               + _dt.timedelta(days=UNDO_DAYS))
            removed_label = (f"Removed {_mmddyy(_local_day(conn, u['removed_at']))}"
                             f" — can be put back until {_mmddyy(until)}")
        out.append({
            "id": u["id"], "kind": kind, "group": _GROUP_OF[kind],
            "kind_label": label, "years": years,
            "years_label": _years_label(years), "whose_label": whose,
            # a figure stored before the list names no file
            "filename": (None if kind.startswith("legacy_")
                         else _clean_filename(u["filename"])),
            "stored_at": _stored_at_iso(u["stored_at"]),
            "stored_label": stored_label,
            "removed": removed, "removed_label": removed_label,
            "removable": not removed, "restorable": removed,
            "employer_shown": employer, "abbreviated": abbreviated,
            "payer": payer if not removed else None,
            "w2s": w2s if not removed else [],
            "seen": {"kind": kind, "years": years,
                     "stored_at": _stored_at_iso(u["stored_at"])},
        })
    return out


def stored_groups(rows: list[dict]) -> list[dict]:
    """The list's groups as the card prints them: name and count, in
    order, only those holding a document."""
    out = []
    for key, name in GROUPS:
        n = sum(1 for r in rows if r["group"] == key)
        if n:
            out.append({"key": key, "label": f"{name} ({n})", "count": n,
                        "collapsed": key == "other"})
    return out


def _upload_seen(seen) -> tuple:
    if not isinstance(seen, dict):
        raise ValueError("say which document: its `kind`, `years` and "
                         "`stored_at` as listed")
    kind, years, at = seen.get("kind"), seen.get("years"), seen.get(
        "stored_at")
    if (not isinstance(kind, str) or not isinstance(at, str)
            or not isinstance(years, list)
            or not all(isinstance(y, int) and not isinstance(y, bool)
                       for y in years) or len(years) > MAX_ROWS):
        raise ValueError("say which document: its `kind`, `years` and "
                         "`stored_at` as listed")
    return kind, sorted(set(years)), at


def _upload_row(conn, upload_id: int):
    return conn.execute(
        "SELECT id, kind, stored_at, removed_at, "
        "(removed_at IS NOT NULL AND removed_at >= now() - interval "
        f"'{UNDO_DAYS} days') AS undoable FROM income_uploads WHERE id = %s",
        (upload_id,)).fetchone()


def _check_seen(conn, upload_id: int, shown: tuple):
    """The upload under `upload_id` when it is still the one the list
    showed; None when there is none; UploadChanged when it moved."""
    row = _upload_row(conn, upload_id)
    if row is None:
        return None
    kind, years, at = shown
    if (row["kind"] != kind or _stored_at_iso(row["stored_at"]) != at
            or _upload_years(conn, upload_id) != years):
        raise UploadChanged(UPLOAD_CHANGED_MSG)
    return row


def _step_up_for(kind, years) -> bool:
    """THE rule: removing an earnings record, or any document that wrote
    more than one year, asks the person to prove it is them first (the
    step-up window): one tap would otherwise take decades of figures."""
    return kind in ("ssa", "legacy_ssa") or len(years) > 1


def needs_step_up(conn, upload_id: int) -> bool:
    """Whether removing the upload as it stands asks for the step-up — the
    early answer the confirm and the door give; `remove_upload` decides it
    again under its locks, over what it removes."""
    row = _upload_row(conn, upload_id)
    return bool(row) and _step_up_for(row["kind"],
                                      _upload_years(conn, upload_id))


def _remove(conn, upload_id: int, by) -> list[int]:
    """Mark one upload removed, hold its forms, resolve its years. The
    caller holds the years' locks. Returns the years."""
    years = _upload_years(conn, upload_id)
    docs = [dict(r) for r in conn.execute(
        """SELECT id, year, form, owner, payer, ein_last4, primary_amount,
                  amounts, notes, payer_named_by_person FROM income_documents
            WHERE upload_id = %s ORDER BY id""", (upload_id,)).fetchall()]
    conn.execute(
        "UPDATE income_uploads SET removed_at = now(), removed_by = %s::uuid,"
        " held_documents = %s::jsonb WHERE id = %s",
        (_uuid_or_none(by), json.dumps(docs) if docs else None, upload_id))
    conn.execute("DELETE FROM income_documents WHERE upload_id = %s",
                 (upload_id,))
    for y in years:
        resolve_year(conn, y, prune=True)
    return years


def _figures(conn, years) -> tuple[dict, dict]:
    """The two figures stored income feeds (Cash Flow's records): career
    earnings per year and reported income per year, over `years`."""
    want = set(years)
    career = {r["year"]: r["v"] for r in conn.execute(
        "SELECT year, COALESCE(medicare_earnings, ss_earnings, "
        "primary_wages, wages) AS v FROM income_annual").fetchall()
        if r["v"] is not None and r["year"] in want}
    # Reported income as its one reader reads it (the return's wages,
    # else the W-2s'), so a year the W-2s still report is never "lost"
    from . import reporting
    reported = {r[0]: tuple(r[1:5]) for r in reporting.reported_income(conn)
                if r[0] in want}
    return career, reported


class _Undo(Exception):
    pass


def removal_effect(conn, upload_id: int) -> str:
    """What removing the upload does to the figures people read, in words
    — worked out by removing it inside a savepoint and rolling back, so
    the words are the removal's own result, never a second rule. Names
    only Career earnings and Reported income: the only figures stored
    income feeds (nothing in retirement reads it)."""
    row = _upload_row(conn, upload_id)
    if row is None or row["removed_at"] is not None:
        # removed already: there is no removal to work out
        raise UploadChanged(UPLOAD_CHANGED_MSG)
    out: dict = {}
    try:
        with conn.transaction():
            years = _upload_years(conn, upload_id)
            _lock_years(conn, years, bounded=True)
            # removed by someone else while the years were waited for, or
            # its years moved: the words would be about the years locked,
            # not the ones the removal takes
            row = _upload_row(conn, upload_id)
            if (row is None or row["removed_at"] is not None
                    or _upload_years(conn, upload_id) != years):
                raise UploadChanged(UPLOAD_CHANGED_MSG)
            adopt_unexplained(conn, years)
            # Before and after are both read here, under the locks and over
            # the locked years alone: a removal changes only the years it
            # holds, and a store or put-back landing on another year (or on
            # these, before the locks) between two reads would otherwise be
            # reported as this removal's effect.
            out["before"] = _figures(conn, years)
            a0 = _annual_rows(conn, years)
            _remove(conn, upload_id, None)
            out["after"] = _figures(conn, years)
            a1 = _annual_rows(conn, years)
            out["stored"] = sorted(y for y in set(a0) | set(a1)
                                   if a0.get(y) != a1.get(y))
            raise _Undo
    except _Undo:
        pass
    c0, r0 = out["before"]
    c1, r1 = out["after"]
    parts = []
    for name, a, b in (("Career earnings", c0, c1),
                       ("Reported income", r0, r1)):
        lost = [y for y in a if y not in b]
        moved = [y for y in a if y in b and a[y] != b[y]]
        said = ([f"loses {_years_label(lost)}"] if lost else []) + (
            [f"changes for {_years_label(moved)}"] if moved else [])
        if said:
            # each figure named once: "Career earnings loses 2001 and
            # changes for 2019"
            parts.append(f"{name} " + " and ".join(said))
    if parts:
        return "; ".join(parts) + "."
    return _unchanged_words(conn, upload_id, out["stored"])


def _annual_rows(conn, years) -> dict:
    """Every stored income figure per year of `years` (provenance words
    aside), so a removal's effect on what is stored can be told from its
    effect on what is shown."""
    want = set(years)
    return {r["year"]: tuple(v for k, v in r.items() if k != "year")
            for r in conn.execute(
        """SELECT year, total_income, agi, taxable_income, tax_paid, wages,
                  primary_wages, investment_income, capital_gain,
                  spouse_wages, ss_earnings, medicare_earnings,
                  filing_status, joint
             FROM income_annual WHERE year = ANY(%s)""",
        (sorted(want),)).fetchall()}


def _unchanged_words(conn, upload_id: int, stored: list[int]) -> str:
    """Why removing the upload moves no figure, said truly. A W-2 is read
    by its person's wages (resum) — unless a return's figure for that
    column stands in for it, and then it is what backs the wages the day
    that return goes; one nobody has claimed is in no one's wages.
    `stored` is the years whose stored income the rolled-back removal
    changed: a change there that no shown figure carries means other
    numbers give those figures and this one backs them. Only a removal
    that changed nothing stored is "nothing reads"."""
    w2s = conn.execute(
        """SELECT DISTINCT year, owner FROM income_documents
            WHERE upload_id = %s AND form = 'W-2'
              AND primary_amount IS NOT NULL AND primary_amount <> 0
            ORDER BY year""", (upload_id,)).fetchall()
    stood_in = sorted({
        d["year"] for d in w2s if d["owner"] in _WAGE_COLUMN
        and conn.execute(
            """SELECT 1 FROM income_upload_values v
                 JOIN income_uploads u ON u.id = v.upload_id
                WHERE v.year = %s AND u.id <> %s AND u.removed_at IS NULL
                  AND jsonb_exists(v.fields, %s) LIMIT 1""",
            (d["year"], upload_id, _WAGE_COLUMN[d["owner"]])).fetchone()})
    if stood_in:
        return (f"No figure changes now: the {_years_label(stood_in)} tax "
                "return figures stand in for this W-2's wages. It backs "
                "them if that return is removed.")
    if w2s and all(d["owner"] not in _WAGE_COLUMN for d in w2s):
        return ("No figure changes: nobody has said whose this W-2 is, so "
                "it counts toward nobody's wages.")
    if stored:
        return (f"No figure shown changes: other numbers give the "
                f"{_years_label(stored)} figures shown. This W-2 backs the "
                "wages if they are removed." if w2s else
                f"No figure shown changes: other numbers give the "
                f"{_years_label(stored)} figures shown. This document backs "
                "them if they are removed.")
    return "No figure changes: nothing reads this document's numbers."


def remove_upload(conn, upload_id: int, seen, by=None,
                  step_up=None) -> list[int] | None:
    """Remove one stored document — the one the list showed (`seen`) —
    and resolve every year it wrote. None when there is no such live
    document. `step_up`, when given, is called under the years' locks
    when what is removed asks for the step-up (`_step_up_for`), and
    refuses by raising: the check the door made before the locks can
    have seen a document that has since moved."""
    shown = _upload_seen(seen)
    with conn.transaction():
        row = _check_seen(conn, upload_id, shown)
        if row is None or row["removed_at"] is not None:
            return None
        _may_write(conn)
        _lock_years(conn, shown[1], bounded=True)
        row = _check_seen(conn, upload_id, shown)
        if row is None or row["removed_at"] is not None:
            return None
        # what is removed is exactly what was checked: this kind, these
        # years, under their locks
        if step_up is not None and _step_up_for(row["kind"], shown[1]):
            step_up()
        adopt_unexplained(conn, shown[1])
        return _remove(conn, upload_id, by)


# a payer's name as a person types it: the cap the reader stores a W-2's
# employer under
PAYER_MAX = 120


def rename_payer(conn, upload_id: int, seen, payer, name) -> list[int] | None:
    """Rename the payer on one stored document — the one the list showed
    (`seen`) — on the forms it holds under `payer`, the stored name the
    list sent: a transcript holds several payers, and only the one shown
    is renamed. Under the years' locks the removal takes, checked again
    there; no figure reads a payer (matching compares box 1 and the EIN,
    never the name), so nothing is resolved and no step-up is asked for:
    it loses nothing. None when there is no such live document;
    UploadChanged when the list or the payer moved."""
    shown = _upload_seen(seen)
    if not isinstance(payer, str) or not payer:
        raise ValueError("say which payer: its name as listed")
    new = name.strip() if isinstance(name, str) else ""
    if not new or len(new) > PAYER_MAX:
        raise ValueError(f"the payer's name, {PAYER_MAX} characters at most")
    with conn.transaction():
        row = _check_seen(conn, upload_id, shown)
        if row is None or row["removed_at"] is not None:
            return None
        _may_write(conn)
        _lock_years(conn, shown[1], bounded=True)
        row = _check_seen(conn, upload_id, shown)
        if row is None or row["removed_at"] is not None:
            return None
        # marked as typed: the list shows it whole, never as an IRS name
        # control (payer_shown)
        n = conn.execute(
            "UPDATE income_documents SET payer = %s, "
            "payer_named_by_person = true "
            "WHERE upload_id = %s AND payer = %s",
            (new, upload_id, payer)).rowcount
        if not n:
            raise UploadChanged(UPLOAD_CHANGED_MSG)
        return shown[1]


def _held_form(d) -> dict | None:
    """A held form as it may be written back, or None for one that is not
    a form (a restore may have landed any JSON): the year a W-2 can be
    stored for, text where text belongs, the EIN cut to its last four."""
    if not isinstance(d, dict):
        return None
    year, form = d.get("year"), d.get("form")
    # a year read from JSON may be 1e999, NaN or a 400-digit integer (jsonb
    # prints 1e999 back as one), which int() or isfinite() cannot take:
    # the range is checked before anything converts it, so that form is
    # refused alone, never the restore landing it or the list
    if (isinstance(year, bool) or not _is_num(year)
            or not W2_YEARS.start <= year < W2_YEARS.stop
            or int(year) != year or not isinstance(form, str)
            or not form):
        return None
    amt = d.get("primary_amount")
    amounts = d.get("amounts")
    if isinstance(amounts, dict):
        # the boxes as the live door keeps them: finite numbers only
        amounts = {k: v for k, v in amounts.items()
                   if isinstance(k, str) and _is_num(v) and _money(v)}
    text = lambda v: v if isinstance(v, str) else None   # noqa: E731
    doc_id = d.get("id")
    return {"id": (doc_id if isinstance(doc_id, int)
                   and not isinstance(doc_id, bool) else None),
            "year": int(year), "form": form[:40],
            "owner": _shown_owner(d.get("owner")),
            "payer": text(d.get("payer")),
            "ein_last4": _ein_last4(text(d.get("ein_last4"))),
            "primary_amount": (float(amt) if _is_num(amt) and _money(amt)
                               else None),
            "amounts": amounts if isinstance(amounts, dict) else None,
            "notes": text(d.get("notes")),
            # a hold written before the mark existed carries none: unmarked
            "payer_named_by_person": d.get("payer_named_by_person") is True}


def restore_upload(conn, upload_id: int, seen) -> list[int] | None:
    """Put back a document removed within UNDO_DAYS: its figures resolve
    again and its forms come back to the people they were filed under. A
    held W-2 comes back as nobody's only when a W-2 stored since the
    removal, in another upload, is the same person's same form (box 1 and
    EIN last four): that one took its place. A twin the household held all
    along (two spouses' identical W-2s) is no reason. None when there is
    nothing to put back."""
    shown = _upload_seen(seen)
    with conn.transaction():
        row = _check_seen(conn, upload_id, shown)
        if row is None or not row["undoable"]:
            return None
        _may_write(conn)
        _lock_years(conn, shown[1], bounded=True)
        row = _check_seen(conn, upload_id, shown)
        if row is None or not row["undoable"]:
            return None
        adopt_unexplained(conn, shown[1])
        for d in map(_held_form, _held(conn, upload_id) or []):
            if d is None:
                continue
            owner = d["owner"]
            if d["form"] == "W-2" and conn.execute(
                    """SELECT 1 FROM income_documents x
                         JOIN income_uploads u ON u.id = x.upload_id
                        WHERE x.year = %s AND x.form = 'W-2'
                          AND x.owner = %s
                          AND x.primary_amount IS NOT DISTINCT FROM %s
                          AND x.ein_last4 IS NOT DISTINCT FROM %s
                          AND x.upload_id <> %s
                          AND u.stored_at > %s LIMIT 1""",
                    (d["year"], owner, d["primary_amount"], d["ein_last4"],
                     upload_id, row["removed_at"])).fetchone():
                owner = "unassigned"
            conn.execute(
                """INSERT INTO income_documents
                       (id, year, form, owner, payer, ein_last4,
                        primary_amount, amounts, notes, upload_id,
                        payer_named_by_person)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)""",
                (_new_document_id(conn), d["year"], d["form"], owner,
                 d["payer"], d["ein_last4"], d["primary_amount"],
                 json.dumps(d["amounts"]) if d["amounts"] is not None
                 else None, d["notes"], upload_id,
                 d["payer_named_by_person"]))
        conn.execute("UPDATE income_uploads SET removed_at = NULL, "
                     "removed_by = NULL, held_documents = NULL WHERE id = %s",
                     (upload_id,))
        for y in shown[1]:
            resolve_year(conn, y)
        return shown[1]


def purge_removed(conn) -> int:
    """The nightly sweep: a document removed more than UNDO_DAYS ago is
    gone for good, with the values it wrote. Its years were resolved when
    it was removed, so no figure moves. Each one is purged under its
    years' locks and checked again there: a put-back that won the race
    keeps its document."""
    gone = 0
    for r in conn.execute(
            "SELECT id FROM income_uploads WHERE removed_at < now() - "
            f"interval '{UNDO_DAYS} days' ORDER BY id").fetchall():
        with conn.transaction():
            years = _upload_years(conn, r["id"])
            _lock_years(conn, years, bounded=False)
            lock_document_ids(conn)
            # its years are read again under the locks: one that moved
            # while they were waited for is left for the next night
            if not conn.execute(
                    "SELECT 1 FROM income_uploads WHERE id = %s AND "
                    f"removed_at < now() - interval '{UNDO_DAYS} days'",
                    (r["id"],)).fetchone() \
                    or _upload_years(conn, r["id"]) != years:
                continue
            conn.execute("DELETE FROM income_upload_values "
                         "WHERE upload_id = %s", (r["id"],))
            conn.execute("DELETE FROM income_uploads WHERE id = %s",
                         (r["id"],))
            gone += 1
    return gone


_SSA_COLS = ("ss_earnings", "medicare_earnings")
_RETURN_COLS = ("total_income", "agi", "taxable_income", "tax_paid", "wages",
                "investment_income", "capital_gain")


# what a figure stored before the list is dated at: the epoch plus its id,
# so any document stored since outranks it and the moment a migration or a
# restore ran decides nothing
_BEFORE_LIST = "'epoch'::timestamptz + %s * interval '1 second'"


def _before_list_upload(conn, uid: int, kind: str) -> None:
    conn.execute(f"INSERT INTO income_uploads (id, kind, stored_at) "
                 f"VALUES (%s, %s, {_BEFORE_LIST})", (uid, kind, uid))


def backfill_household(conn) -> None:
    """Give a household's stored income uploads, as migration 231 gave
    every household its own — the twin of that file, run by a restore
    over what an archive from before the stored list landed (checked
    against the file by test_the_restore_backfill_gives_the_same_uploads_as_the_migration).
    The yearly figures are given uploads only when the household holds
    none yet; every form without an upload is given one of its own
    (`adopt_unexplained` covers what this leaves). No figure changes."""
    lock_document_ids(conn)
    if not conn.execute("SELECT 1 FROM income_uploads LIMIT 1").fetchone():
        rows = conn.execute(
            "SELECT * FROM income_annual ORDER BY year").fetchall()
        ssa = [(r["year"], {c: r[c] for c in _SSA_COLS if r[c] is not None})
               for r in rows if any(r[c] is not None for c in _SSA_COLS)]
        if ssa:
            _before_list_upload(conn, 1, "legacy_ssa")
            for year, f in ssa:
                write_upload_values(conn, 1, year, f)
        n = 1
        for r in rows:
            f = {c: r[c] for c in _RETURN_COLS if r[c] is not None}
            for c in _WAGE_COLS:
                if r[c] is not None and r[c] != r[f"{c}_w2"]:
                    f[c] = r[c]
            if not f:
                continue
            n += 1
            _before_list_upload(conn, n, "legacy_return")
            write_upload_values(conn, n, r["year"], f)
    _adopt_forms(conn, None)


def _adopt_forms(conn, years) -> int:
    """Every stored form without an upload is given one of its own, dated
    before the list (a form an older build stored, or a restore landed
    without its upload): listed and removable like any other. The ids
    lock is taken only when there is a form to adopt, and a person's
    request waits for it a few seconds, as at the year locks: every door
    passes here, and a restore holds that lock for its whole transaction.
    The restore and the nightly hold it already when they come here."""
    def loose():
        return conn.execute(
            "SELECT id, form FROM income_documents WHERE upload_id IS NULL"
            + (" AND year = ANY(%s)" if years is not None else "")
            + " ORDER BY id", (list(years),) if years is not None else ()
        ).fetchall()
    if not loose():
        return 0
    lock_document_ids(conn, bounded=True)
    rows = loose()
    if not rows:
        return 0
    nxt = conn.execute("SELECT COALESCE(MAX(id), 0) AS m "
                       "FROM income_uploads").fetchone()["m"]
    for d in rows:
        nxt += 1
        _before_list_upload(conn, nxt, "w2" if d["form"] == "W-2"
                            else "document")
        conn.execute("UPDATE income_documents SET upload_id = %s "
                     "WHERE id = %s", (nxt, d["id"]))
    return len(rows)


def _resolution(conn, year: int) -> dict:
    """What `resolve_year` would set each column to: the newest live
    upload's value of every column some live upload wrote."""
    now: dict[str, float] = {}
    for r in conn.execute(
            """SELECT v.fields FROM income_upload_values v
                 JOIN income_uploads u ON u.id = v.upload_id
                WHERE v.year = %s AND u.removed_at IS NULL
                ORDER BY u.stored_at, u.id""", (year,)).fetchall():
        f = r["fields"] if isinstance(r["fields"], dict) else {}
        now.update({c: float(f[c]) for c in f
                    if c in ANNUAL_FIELDS and _is_num(f[c])})
    return now


def adopt_unexplained(conn, years=None) -> int:
    """THE guard against a resolve setting back a figure it did not write.
    Every stored form without an upload is given one (`_adopt_forms`), and
    every yearly figure no live upload explains — a writer outside the
    list (the release before this one after a rollback, a hand load), or
    an archive's rows the restore landed — becomes an upload of its own,
    dated now: the figure people read is kept, and listed, and can be
    removed like any other. A wage figure still equal to what the W-2s
    last wrote is theirs. Every door that resolves a year calls this for
    it first, before it writes its own upload (resolve_year itself cannot:
    a document just written would read as drift); the restore calls it
    before and after landing, the nightly job over every year. Run under
    the years' locks. Returns how many uploads it created."""
    made = _adopt_forms(conn, years)
    rows = conn.execute(
        "SELECT * FROM income_annual"
        + (" WHERE year = ANY(%s)" if years is not None else "")
        + " ORDER BY year", (list(years),) if years is not None else ()
    ).fetchall()
    for r in rows:
        now = _resolution(conn, r["year"])
        # a figure a removed document wrote is that document's, held for
        # its put-back — an archive bringing it back (restored over the
        # removal) is the removal's to decide, never a new figure
        taken = {(c, float(f[c])) for f in (
            x["fields"] for x in conn.execute(
                """SELECT v.fields FROM income_upload_values v
                     JOIN income_uploads u ON u.id = v.upload_id
                    WHERE v.year = %s AND u.removed_at IS NOT NULL""",
                (r["year"],)).fetchall())
            if isinstance(f, dict) for c in f
            if c in ANNUAL_FIELDS and _is_num(f[c])}
        drift = {}
        for c in ANNUAL_FIELDS:
            v = r[c]
            # a figure no number can be (NaN or infinite, from an older
            # build) is no figure to keep: never adopted, never a failure
            if (v is None or not math.isfinite(v) or now.get(c) == v
                    or (c, float(v)) in taken):
                continue
            if c in _WAGE_COLS and v == r[f"{c}_w2"]:
                continue                  # the W-2s'
            drift[c] = float(v)
        for kind, cols in (("legacy_ssa", _SSA_COLS),
                           ("legacy_return", tuple(c for c in ANNUAL_FIELDS
                                                   if c not in _SSA_COLS))):
            f = {c: drift[c] for c in cols if c in drift}
            if not f:
                continue
            uid = _new_upload_id(conn)
            # dated after every upload of its year, so it is the newest
            # however the others are dated: the figure it keeps is the one
            # the next resolve finds, and the next night finds nothing new
            conn.execute(
                """INSERT INTO income_uploads (id, kind, stored_at)
                   VALUES (%s, %s, GREATEST(clock_timestamp(), (
                       SELECT max(u.stored_at) + interval '1 microsecond'
                         FROM income_uploads u
                         JOIN income_upload_values v ON v.upload_id = u.id
                        WHERE v.year = %s)))""", (uid, kind, r["year"]))
            write_upload_values(conn, uid, r["year"], f)
            made += 1
    return made


def adopt_household(conn) -> int:
    """The nightly sweep's `adopt_unexplained` over every year, under the
    years' locks (taken in order, as every writer takes them)."""
    with conn.transaction():
        years = sorted({r["year"] for r in conn.execute(
            "SELECT year FROM income_annual UNION "
            "SELECT year FROM income_documents").fetchall()
            if r["year"] in W2_YEARS})
        _lock_years(conn, years, bounded=False)
        # the nightly waits as long as it must (adopting asks for the ids
        # lock as a request does)
        lock_document_ids(conn)
        return adopt_unexplained(conn, None)
