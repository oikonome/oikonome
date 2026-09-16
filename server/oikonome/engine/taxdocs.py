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


_TRANSCRIPT_FIELDS = (
    ("total_income", r"^\s*TOTAL INCOME\s*[:.]*\s*\$?([-()\d,.]+)"),
    ("agi", r"^\s*ADJUSTED GROSS INCOME\s*[:.]*\s*\$?([-()\d,.]+)"),
    ("taxable_income", r"^\s*TAXABLE INCOME\s*[:.]*\s*\$?([-()\d,.]+)"),
    ("tax_paid", r"^\s*TOTAL TAX(?: LIABILITY[A-Z ]*)?\s*[:.]*\s*\$?([-()\d,.]+)"),
    ("wages", r"^\s*WAGES, SALARIES, TIPS, ETC\.?\s*[:.]*\s*\$?([-()\d,.]+)"),
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
    from . import budget
    from . import llm_categorize as llm
    url = (llm._backend(conn, role="vision") or {}).get("url") or ""
    if _endpoint_is_local(url):
        return
    cfg = budget.load_config(conn)
    if str(cfg.get("taxdocs_allow_remote_llm") or "").strip().lower() in (
            "1", "true", "yes", "on"):
        return
    raise ValueError(
        f"reading a {kind} would send an image of the whole document — "
        f"including your Social Security number — to the LLM endpoint you "
        f"configured, which is not on this machine or network. Enter the "
        f"figures by hand, point Settings → AI at a local "
        f"model, or turn on 'allow sensitive documents to be sent to my "
        f"remote "
        f"model' in Settings → AI if you intend to send it.")


def _chat_json(conn, messages, max_tokens=800) -> dict:
    from . import llm_categorize as llm
    backend = llm._backend(conn, role="vision")
    if not backend.get("url"):
        raise ValueError("this document type needs the LLM — configure it "
                         "under Settings → AI")
    vision = any(isinstance(m.get("content"), list) for m in messages)
    if vision:
        vm = str(backend.get("vision_model") or "").strip()
        if vm:
            backend = {**backend, "model": vm}
    raw = llm._chat(messages, max_tokens=max_tokens, backend=backend)
    try:
        out = json.loads(raw)
    except ValueError:
        out = None
    # the model is untrusted input: a bare number, string or list is just
    # as unparseable as invalid JSON, and every caller reads fields off an
    # object
    if not isinstance(out, dict):
        raise ValueError("the model returned unparseable output — try a "
                         "clearer scan or a different model")
    return out


def _year_of(out: dict, complaint: str) -> int:
    """The model's `year`, or a ValueError the upload endpoint turns into
    a clean 400. int() alone raises TypeError on a null year, which the
    endpoint does not catch — a 500 in the browser for a bad scan."""
    try:
        return int(out["year"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(complaint)


def parse_w2(conn, data: bytes, mime: str) -> list[dict]:
    from . import receipts
    _require_offbox_consent(conn, "W-2")
    if mime == "application/pdf":
        img, mime = receipts._pdf_first_page_png(data), "image/png"
    else:
        img, mime = receipts._shrink_image(data, mime)
    b64 = base64.b64encode(img).decode()
    out = _chat_json(conn, [{"role": "user", "content": [
        {"type": "text", "text": _W2_PROMPT},
        {"type": "image_url",
         "image_url": {"url": f"data:{mime};base64,{b64}"}}]}])
    year = _year_of(out, "couldn't read a W-2 out of this image")
    # non-dict boxes (the model returning a list, say) are dropped rather
    # than fatal — the year row still previews, boxes stay hand-editable
    raw_boxes = out.get("boxes")
    boxes = ({str(k): v for k, v in raw_boxes.items()
              if isinstance(v, (int, float))}
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
    # the extracted text of a 1040 carries the SSN just as the image does
    _require_offbox_consent(conn, "tax return")
    out = _chat_json(conn, [{"role": "user",
                             "content": _1040_PROMPT + text[:12000]}])
    row: dict = {"year": _year_of(
        out, "couldn't read a tax return out of this document")}
    for f in ("total_income", "agi", "taxable_income", "tax_paid", "wages"):
        v = out.get(f)
        if isinstance(v, (int, float)):
            row[f] = float(v)
    return [row] if len(row) > 1 else []


# ---- analyze / commit --------------------------------------------------------

def _existing_years(conn) -> set[int]:
    return {r["year"] for r in conn.execute(
        "SELECT year FROM income_annual").fetchall()}


def analyze(conn, filename: str, data: bytes, mime: str) -> dict:
    """Detect + parse one document; stage the result behind a token for
    commit. Detection order: SSA XML → IRS transcript text → W-2 marker →
    1040 (text→LLM); images go straight to the W-2 vision path."""
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
    existing = _existing_years(conn)
    for r in rows:
        r["exists"] = r["year"] in existing
    # Staged in the tenant-bound table, so a token from tenant A can never
    # be committed under tenant B's conn
    from ..db import staging
    token = staging.put(conn, "taxdoc", meta={"kind": kind, "rows": rows})
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


def _insert_income_document(conn, year: int, payer, ein, primary_amount,
                            amounts_json: str) -> int:
    """Race-safe id allocation. income_documents has no identity —
    original integer ids travel in restores — so the next id is MAX+1,
    inserted ON CONFLICT DO NOTHING and recomputed on a lost race: a
    concurrent commit that grabbed the same id just bumps us to the next
    one instead of blowing up with a PK violation."""
    for _ in range(50):
        nid = conn.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 AS id "
            "FROM income_documents").fetchone()["id"]
        cur = conn.execute(
            """INSERT INTO income_documents
                   (id, year, form, payer, ein_last4, primary_amount,
                    amounts)
               VALUES (%s, %s, 'W-2', %s, %s, %s, %s)
               ON CONFLICT (tenant_id, id) DO NOTHING""",
            (nid, year, payer, _ein_last4(ein), primary_amount,
             amounts_json))
        if cur.rowcount:
            return nid
    raise ValueError("couldn't allocate a document id — please retry")


def commit(conn, token: str, rows: list[dict]) -> dict:
    """Write the (possibly user-edited) previewed rows. Annual kinds merge
    per column into income_annual; W-2s insert income_documents and fill
    primary_wages only when the year had none."""
    from ..db import staging
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
        except (KeyError, TypeError, ValueError):
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
        return _commit_rows(conn, kind, rows, years)


def _commit_rows(conn, kind, rows, years) -> dict:
    updated = created = docs = 0
    for r, year in zip(rows, years):
        if kind == "w2":
            # `rows` is arbitrary client JSON and the docstring says so
            # ("possibly user-edited"). A non-dict `boxes` — a list, a
            # string, a number — must be refused as the bad input it is,
            # not reach .items() and 500.
            raw_boxes = r.get("boxes")
            if raw_boxes is not None and not isinstance(raw_boxes, dict):
                raise ValueError("each row's `boxes` must be an object")
            boxes = {k: v for k, v in (raw_boxes or {}).items()
                     if isinstance(v, (int, float))}
            # a NaN/inf reaches DOUBLE PRECISION unchanged and every
            # report that sums the year 500s from then on
            if any(not math.isfinite(v) for v in boxes.values()):
                raise ValueError("every box must be a finite number")
            _insert_income_document(conn, year, r.get("employer"),
                                    r.get("ein"), boxes.get("1"),
                                    json.dumps(boxes))
            docs += 1
            if boxes.get("1"):
                conn.execute(
                    """INSERT INTO income_annual (year, primary_wages, source)
                       VALUES (%s, %s, 'w2')
                       ON CONFLICT (tenant_id, year) DO UPDATE SET
                           primary_wages = COALESCE(
                               income_annual.primary_wages,
                               EXCLUDED.primary_wages)""",
                    (year, boxes["1"]))
            continue
        if not isinstance(r, dict):
            raise ValueError("each row must be an object")
        fields = {f: float(r[f]) for f in ANNUAL_FIELDS
                  if isinstance(r.get(f), (int, float))}
        if any(not math.isfinite(v) for v in fields.values()):
            raise ValueError("every amount must be a finite number")
        if not fields:
            continue
        existed = conn.execute(
            "SELECT 1 FROM income_annual WHERE year=%s", (year,)).fetchone()
        sets = ", ".join(f"{f} = EXCLUDED.{f}" for f in fields)
        conn.execute(
            f"""INSERT INTO income_annual (year, source, {', '.join(fields)})
               VALUES (%s, %s, {', '.join(['%s'] * len(fields))})
               ON CONFLICT (tenant_id, year) DO UPDATE SET {sets},
                   source = CASE WHEN income_annual.source IS NULL THEN %s
                       WHEN position(%s in income_annual.source) > 0
                       THEN income_annual.source
                       ELSE income_annual.source || '+' || %s END""",
            (year, kind, *fields.values(), kind, kind, kind))
        if existed:
            updated += 1
        else:
            created += 1
    return {"kind": kind, "updated": updated, "created": created,
            "documents": docs}
