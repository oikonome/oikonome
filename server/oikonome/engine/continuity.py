"""The continuity packet: one PDF a household's survivor can read cold.

A household's finances live in a dozen places — the bank, the card, the
brokerage, the mortgage servicer, the LLC's state filing — and in one
person's head. When that person is gone or laid up, the other one has to
reconstruct all of it from statements and guesses. This module writes the
briefing they would want instead: every account and where it is, what
the balances were on the day it was printed, every recurring bill and
what pays it, the income that arrives, the businesses and who to file
with, and a note the owner wrote in their own words about where the
rest is — the password manager, the will, the lawyer.

What it deliberately does NOT hold: any credential. No passwords, no
bank tokens, no full EIN. It points at where those live; it never is
where they live. A packet found in a drawer is a map of the money, not
a key to it.

Two doors use it: the download (a one-shot ticket after a fresh
elevation, like every other export) and the emailed copy (same
elevation, recipient checked by the household mail rule).
"""
from __future__ import annotations

import datetime as dt
import html
import io
import logging

log = logging.getLogger("oikonome.continuity")

# the owner's own words: where the rest is. Bounded — it is a page of
# prose, not a document store
NOTE_MAX = 4000
FOR_MAX = 120
KEY_NOTE = "continuity_note"
KEY_FOR = "continuity_for"

_STRUCTURE_WORDS = {
    "single_member_llc": "single-member LLC",
    "multi_member_llc": "multi-member LLC",
    "sole_prop": "sole proprietorship",
    "s_corp": "S corporation",
}

_KIND_WORDS = {
    "depository/checking": "checking",
    "depository/savings": "savings",
    "depository/money market": "money market",
    "depository/cd": "certificate of deposit",
    "depository/hsa": "HSA",
    "credit/credit card": "credit card",
    "loan/mortgage": "mortgage",
    "loan/student": "student loan",
    "loan/auto": "auto loan",
    "investment/401k": "401(k)",
    "investment/ira": "IRA",
    "investment/roth": "Roth IRA",
    "investment/brokerage": "brokerage",
    "investment/529": "529 plan",
    "investment/hsa": "HSA",
}


def settings_of(cfg: dict) -> dict:
    """The two fields the owner writes: who it is for and where the rest is."""
    return {"note": str(cfg.get(KEY_NOTE) or ""),
            "prepared_for": str(cfg.get(KEY_FOR) or "")}


def clean_note(text) -> str:
    """Whitespace-normalised per line, bounded. Raises ValueError past the cap
    so the door can say so instead of silently truncating a will's address."""
    lines = [" ".join(str(ln).split()) for ln in str(text or "").splitlines()]
    out = "\n".join(lines).strip()
    if len(out) > NOTE_MAX:
        raise ValueError(f"the note is limited to {NOTE_MAX} characters")
    return out


def clean_for(text) -> str:
    out = " ".join(str(text or "").split())
    if len(out) > FOR_MAX:
        raise ValueError(f"the name is limited to {FOR_MAX} characters")
    return out


def _kind_words(kind: str | None, subtype: str | None) -> str:
    key = f"{(kind or '').lower()}/{(subtype or '').lower()}"
    if key in _KIND_WORDS:
        return _KIND_WORDS[key]
    if subtype:
        return subtype.replace("_", " ")
    return (kind or "account").replace("_", " ")


def _money(v) -> str:
    try:
        f = float(v or 0)
    except (TypeError, ValueError):
        return "—"
    sign = "-" if f < 0 else ""
    return f"{sign}${abs(f):,.2f}"


def _date(v) -> str:
    """MM/DD/YY, the household's date shape everywhere else."""
    if isinstance(v, dt.datetime):
        v = v.date()
    if isinstance(v, dt.date):
        return v.strftime("%m/%d/%y")
    if isinstance(v, str) and len(v) >= 10:
        try:
            return dt.date.fromisoformat(v[:10]).strftime("%m/%d/%y")
        except ValueError:
            return v
    return str(v or "")


# ---- gathering --------------------------------------------------------------

def gather(conn, *, members: list[dict], base_url: str = "",
           today: dt.date | None = None) -> dict:
    """Everything the packet prints, as plain data — so a test can read
    it without parsing a PDF, and the renderer never touches the DB."""
    from . import budget, compliance, debt, entities, links, reporting
    from .compat import as_dict
    from .. import localtime
    from ..web.pages import _cadence_label

    cfg = budget.load_config(conn)
    now = localtime.now_local(cfg)
    today = today or now.date()
    packet: dict = {
        "generated_at": now.strftime("%m/%d/%y %H:%M"),
        "generated_date": today,
        "instance_url": (base_url or "").rstrip("/"),
        **settings_of(cfg),
        "people": [{"email": m.get("email") or "",
                    "role": m.get("role") or "member"} for m in members],
    }

    # ---- accounts, grouped by institution ---------------------------------
    ent_names = {str(r["id"]): r["name"] for r in conn.execute(
        "SELECT id, name FROM business_entity").fetchall()}
    shadows = set(links.shadow_ids(conn))
    rows = conn.execute(
        """SELECT a.id, COALESCE(a.display_name, a.name) AS name,
                  a.official_name, a.type, a.subtype, a.mask, a.owner,
                  a.balance_current, a.balance_limit, a.currency,
                  a.entity_id, a.updated_at,
                  i.institution_name, i.aggregator, i.status, i.url
           FROM accounts a JOIN items i ON i.id = a.item_id
           WHERE a.user_removed_at IS NULL
           ORDER BY i.institution_name, a.type, name""").fetchall()
    by_inst: dict[str, dict] = {}
    for r in rows:
        if r["id"] in shadows:
            continue
        inst = r["institution_name"] or "Unknown institution"
        g = by_inst.setdefault(inst, {
            "institution": inst, "url": r["url"] or "",
            "source": _source_words(r["aggregator"]),
            "status": r["status"] or "", "accounts": []})
        g["accounts"].append({
            "name": r["name"] or r["official_name"] or "account",
            "official_name": r["official_name"] or "",
            "kind": _kind_words(r["type"], r["subtype"]),
            "type": r["type"] or "",
            "mask": r["mask"] or "",
            "owner": r["owner"] or "",
            "balance": r["balance_current"],
            "limit": r["balance_limit"],
            "business": ent_names.get(str(r["entity_id"] or ""), ""),
            "updated": _date(r["updated_at"]),
        })
    packet["institutions"] = list(by_inst.values())

    # ---- what it adds up to ------------------------------------------------
    try:
        nw = reporting.compute_networth(conn, today=today)
        packet["networth"] = {
            "financial": nw.get("current_total", 0.0),
            "property": nw.get("property_items", []),
            "property_net": nw.get("property_net", 0.0),
            "total": nw.get("full_total", 0.0),
        }
    except Exception:                                      # noqa: BLE001
        log.exception("continuity: net worth unavailable")
        packet["networth"] = None

    # ---- debts, with their rates and minimums -----------------------------
    try:
        packet["debts"] = [
            {"name": d["name"], "institution": d["institution"] or "",
             "mask": d["mask"] or "", "kind": d["kind"],
             "balance": d["balance"], "apr": d["apr"],
             "apr_source": d["apr_source"],
             "min_payment": d["min_payment"], "min_source": d["min_source"],
             "payment_reported": d.get("payment_reported")}
            for d in debt.load_debts(conn, cfg, today=today)]
    except Exception:                                      # noqa: BLE001
        log.exception("continuity: debts unavailable")
        packet["debts"] = []

    # ---- recurring bills and income ---------------------------------------
    acct_names = {r["id"]: (r["name"], r["mask"]) for r in conn.execute(
        "SELECT id, COALESCE(display_name, name) AS name, mask "
        "FROM accounts").fetchall()}
    bills, income = [], []
    for b in conn.execute(
            "SELECT payee, amount, frequency, due_on, raw, category, "
            "account_id FROM bills WHERE active = 1 "
            "ORDER BY (amount > 0), payee").fetchall():
        raw = as_dict(b["raw"])
        rec = raw.get("recurrence")
        if not isinstance(rec, dict):
            rec = {}
        interval = (budget.envelope_months(raw)
                    if raw.get("bill_type") == "envelope"
                    else rec.get("interval"))
        paid_from = ""
        if b["account_id"] and b["account_id"] in acct_names:
            n, m = acct_names[b["account_id"]]
            paid_from = f"{n} ····{m}" if m else (n or "")
        row = {"payee": b["payee"], "amount": abs(b["amount"] or 0),
               "cadence": _cadence_label(rec.get("frequency") or b["frequency"],
                                         interval),
               "due_on": _date(b["due_on"]), "category": b["category"] or "",
               "account": paid_from,
               "note": str(raw.get("note") or "")}
        (income if (b["amount"] or 0) > 0 else bills).append(row)
    packet["bills"], packet["income"] = bills, income

    # ---- businesses ---------------------------------------------------------
    out_ents = []
    for e in entities.list_entities(conn, include_archived=False):
        try:
            obligations = compliance.obligations(conn, e, today)[:4]
        except Exception:                                  # noqa: BLE001
            obligations = []
        out_ents.append({
            "name": e["name"],
            "structure": _STRUCTURE_WORDS.get(e["structure"], e["structure"]),
            "state": e.get("state") or "",
            "formation_date": _date(e.get("formation_date")),
            "ein_last4": e.get("ein_last4") or "",
            "registered_agent": e.get("registered_agent") or "",
            "fiscal_year_end": e.get("fiscal_year_end") or "",
            "members": entities.list_members(conn, e["id"]),
            "accounts": [a["name"] + (f" ····{a['mask']}" if a["mask"] else "")
                         for g in packet["institutions"]
                         for a in g["accounts"] if a["business"] == e["name"]],
            "obligations": [{"title": o.get("title") or o.get("kind") or "",
                             "due": _date(o.get("due"))} for o in obligations],
        })
    packet["entities"] = out_ents
    return packet


def _source_words(aggregator: str | None) -> str:
    a = (aggregator or "").lower()
    return {"plaid": "linked through Plaid", "mx": "linked through MX",
            "simplefin": "linked through SimpleFIN",
            "csv": "imported from statements",
            "manual": "entered by hand"}.get(a, a or "linked")


# ---- rendering ---------------------------------------------------------------

def render_pdf(p: dict) -> bytes:
    """The packet as a PDF. Pure: takes gather()'s dict, returns bytes."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (KeepTogether, Paragraph,
                                    SimpleDocTemplate, Spacer, Table,
                                    TableStyle)

    ss = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=ss["BodyText"], fontName="Helvetica",
                          fontSize=10, leading=14, alignment=TA_LEFT)
    small = ParagraphStyle("small", parent=body, fontSize=8.5, leading=11,
                           textColor=colors.HexColor("#555555"))
    cell = ParagraphStyle("cell", parent=body, fontSize=9, leading=11.5)
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontName="Helvetica-Bold",
                        fontSize=22, leading=26, alignment=TA_LEFT,
                        spaceAfter=6)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName="Helvetica-Bold",
                        fontSize=14, leading=18, spaceBefore=14, spaceAfter=6)
    h3 = ParagraphStyle("h3", parent=ss["Heading3"], fontName="Helvetica-Bold",
                        fontSize=11, leading=14, spaceBefore=8, spaceAfter=3)

    def P(text, style=body):
        # reportlab reads a Paragraph as mini-XML; anything the ledger
        # spells with & < > must arrive as text, not markup
        return Paragraph(html.escape(str(text or ""), quote=False), style)

    def table(head: list[str], rows: list[list], widths=None):
        data = [[P(h, cell) for h in head]]
        for r in rows:
            data.append([P(c, cell) if not hasattr(c, "wrap") else c
                         for c in r])
        t = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e9e9e9")),
            ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor("#999999")),
            ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#cccccc")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        return t

    W = letter[0] - 1.5 * inch
    story: list = []
    title = "Continuity packet"
    if p.get("prepared_for"):
        title += f" for {p['prepared_for']}"
    story.append(P(title, h1))
    story.append(P(f"Printed {p.get('generated_at', '')} from Oikonome"
                   + (f" at {p['instance_url']}" if p.get("instance_url")
                      else "") + ".", small))
    story.append(Spacer(1, 10))
    story.append(P(
        "This is a map of the household's money on the day it was printed: "
        "every account and where it is held, the balances that day, the "
        "bills that come due and what pays them, the income that arrives, "
        "and the businesses. It holds no passwords and no account "
        "credentials — the section below says where those are kept. "
        "Balances move; the institutions and the bills are what to act "
        "on.", body))

    # ---- where the rest is --------------------------------------------------
    story.append(P("Where the rest is", h2))
    if p.get("note"):
        for para in str(p["note"]).split("\n"):
            story.append(P(para if para.strip() else " ", body))
    else:
        story.append(P("(The owner has not written this section yet. It is "
                       "meant to say where passwords are kept, where the "
                       "will and insurance policies are, and who to call — "
                       "Settings → Data → Continuity packet.)", small))

    # ---- who can sign in -----------------------------------------------------
    story.append(P("Who can sign in to Oikonome", h2))
    story.append(P(
        "These accounts can open the household in Oikonome and see "
        "everything current — balances, transactions, bills — and download "
        "the full export from Settings → Data. A viewer sees; the owner "
        "can also change things.", body))
    story.append(table(["Email", "Role"],
                       [[m["email"], m["role"]] for m in p.get("people", [])],
                       widths=[W * 0.7, W * 0.3]))

    # ---- what it adds up to ---------------------------------------------------
    nw = p.get("networth")
    if nw:
        story.append(P("What it adds up to", h2))
        rows = [["Financial accounts (cash, investments, less cards)",
                 _money(nw["financial"])]]
        for it in nw.get("property") or []:
            rows.append([f"{it.get('name', '')} ({it.get('kind', '')}, "
                         f"{it.get('source', '')})", _money(it.get("value"))])
        rows.append(["Net worth", _money(nw["total"])])
        story.append(table(["", "Amount"], rows, widths=[W * 0.75, W * 0.25]))

    # ---- accounts -------------------------------------------------------------
    story.append(P("Accounts, by institution", h2))
    if not p.get("institutions"):
        story.append(P("No accounts are connected.", small))
    for g in p.get("institutions", []):
        block = [P(g["institution"], h3)]
        meta = g["source"]
        if g.get("url"):
            meta += f" · {g['url']}"
        block.append(P(meta, small))
        rows = []
        for a in g["accounts"]:
            name = a["name"]
            if a.get("official_name") and a["official_name"] != a["name"]:
                name += f" ({a['official_name']})"
            tags = []
            if a.get("owner"):
                tags.append(a["owner"])
            if a.get("business"):
                tags.append(f"business: {a['business']}")
            bal = "—"
            if a["balance"] is not None:
                bal = _money(a["balance"])
                if a["type"] in ("credit", "loan"):
                    bal = f"owes {bal}"
            rows.append([name, a["kind"],
                         f"····{a['mask']}" if a["mask"] else "",
                         bal, ", ".join(tags)])
        block.append(table(["Account", "Type", "Number", "Balance", "Notes"],
                           rows, widths=[W * 0.34, W * 0.16, W * 0.12,
                                         W * 0.16, W * 0.22]))
        story.append(KeepTogether(block))

    # ---- debts -----------------------------------------------------------------
    if p.get("debts"):
        story.append(P("Debts", h2))
        story.append(P("Every card and loan carrying a balance, with its "
                       "rate and the least that must be paid each month: "
                       "the lender's minimum on a card or loan, and on a "
                       "mortgage the servicer's whole payment, escrow "
                       "included (estimated where marked).", body))
        rows = []
        for d in p["debts"]:
            # A mortgage's planning minimum is principal and interest only;
            # the servicer collects that plus escrow, and a survivor who
            # pays the smaller figure falls behind. So the reported payment
            # is what the page says to pay, with the P&I beside it.
            if d.get("payment_reported") is not None:
                due = f"{_money(d['payment_reported'])} incl. escrow"
                # no P&I note when the minimum IS the reported payment
                # (no term and no original principal to derive it from)
                if (d["min_source"] != "you"
                        and d["min_payment"] != d["payment_reported"]):
                    due += (f" (P&I {_money(d['min_payment'])}"
                            + (" est." if d["min_source"] == "estimate"
                               else "") + ")")
            else:
                due = (_money(d["min_payment"])
                       + (" (est.)" if d["min_source"] == "estimate"
                          else ""))
            rows.append([f"{d['institution']} {d['name']}".strip(),
                         f"····{d['mask']}" if d["mask"] else "",
                         _money(d["balance"]),
                         (f"{d['apr']:.2f}%" if d["apr"] else "—")
                         + (" (est.)" if d["apr_source"] == "none" else ""),
                         due])
        story.append(table(["Debt", "Number", "Balance", "Rate",
                            "Monthly payment"],
                           rows, widths=[W * 0.38, W * 0.12, W * 0.17,
                                         W * 0.15, W * 0.18]))

    # ---- bills -------------------------------------------------------------------
    story.append(P("Recurring bills", h2))
    if not p.get("bills"):
        story.append(P("No recurring bills are set up.", small))
    else:
        story.append(P("What comes due, how often, and the account that "
                       "pays it where Oikonome knows. Keep these paid first: "
                       "housing, utilities, insurance, minimums on the "
                       "debts above.", body))
        rows = [[b["payee"], _money(b["amount"]), b["cadence"],
                 b["due_on"] or "", b["account"] or "",
                 b["category"].replace("_", " ")]
                for b in p["bills"]]
        story.append(table(["Bill", "Amount", "Cadence", "Next due",
                            "Paid from", "Category"], rows,
                           widths=[W * 0.28, W * 0.13, W * 0.14, W * 0.11,
                                   W * 0.2, W * 0.14]))

    # ---- income --------------------------------------------------------------------
    story.append(P("Income", h2))
    if not p.get("income"):
        story.append(P("No recurring income is set up.", small))
    else:
        story.append(P("What arrives on a schedule and where it lands. "
                       "Paychecks stop; pensions, Social Security and "
                       "annuities have survivor rules worth a call.", body))
        rows = [[b["payee"], _money(b["amount"]), b["cadence"],
                 b["due_on"] or "", b["account"] or ""]
                for b in p["income"]]
        story.append(table(["Source", "Amount", "Cadence", "Next",
                            "Deposited to"], rows,
                           widths=[W * 0.34, W * 0.14, W * 0.15, W * 0.12,
                                   W * 0.25]))

    # ---- businesses --------------------------------------------------------------
    if p.get("entities"):
        story.append(P("Businesses", h2))
        story.append(P("The full EIN is not printed here; it is on the "
                       "formation and tax documents, and in Oikonome under "
                       "Business for an owner who signs in.", body))
        for e in p["entities"]:
            block = [P(e["name"], h3)]
            facts = [f"{e['structure']}"]
            if e.get("state"):
                facts.append(f"registered in {e['state']}")
            if e.get("formation_date"):
                facts.append(f"formed {e['formation_date']}")
            if e.get("ein_last4"):
                facts.append(f"EIN ending {e['ein_last4']}")
            if e.get("fiscal_year_end"):
                facts.append(f"fiscal year ends {e['fiscal_year_end']}")
            block.append(P(" · ".join(facts), body))
            if e.get("registered_agent"):
                block.append(P(f"Registered agent: {e['registered_agent']}",
                               body))
            if e.get("members"):
                ms = ", ".join(
                    f"{m['member_name']}"
                    + (f" ({m['ownership_pct']:g}%)"
                       if m.get("ownership_pct") is not None else "")
                    + (", manager" if m.get("is_manager") else "")
                    for m in e["members"])
                block.append(P(f"Members: {ms}", body))
            if e.get("accounts"):
                block.append(P("Accounts: " + ", ".join(e["accounts"]), body))
            if e.get("obligations"):
                block.append(table(
                    ["Coming due", "Date"],
                    [[o["title"], o["due"]] for o in e["obligations"]],
                    widths=[W * 0.7, W * 0.3]))
            story.append(KeepTogether(block))

    # ---- what to do first ------------------------------------------------------
    steps: list = [P("If you are reading this because I am gone", h2)]
    for line in (
        "1. Keep the bills above paid from the account that pays them today. "
        "Nothing needs to move quickly except that.",
        "2. Sign in to Oikonome with one of the accounts listed under "
        "\"Who can sign in\" to see today's balances and every transaction. "
        "Settings → Data downloads everything as spreadsheets.",
        "3. Call each institution in the accounts section. Ask for the "
        "deceased-accounts or estate desk; they will say which documents "
        "they need (usually a death certificate and proof you are the "
        "executor or joint owner).",
        "4. Retirement and investment accounts pay to their named "
        "beneficiary, not through the will — ask each one who is named.",
        "5. Employers, pensions and Social Security each have a survivor "
        "process. Social Security: 1-800-772-1213.",
        "6. For each business above, the registered agent and the state's "
        "business registry know what must be filed and by when.",
        "7. Print a fresh copy of this packet from Settings → Data → "
        "Continuity packet whenever the accounts change.",
    ):
        steps.append(P(line, body))
        steps.append(Spacer(1, 4))
    story.append(KeepTogether(steps))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter, leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        title=title, author="Oikonome", subject="Continuity packet")

    def _footer(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(0.75 * inch, 0.5 * inch,
                          f"Continuity packet · printed {p.get('generated_at', '')}")
        canvas.drawRightString(letter[0] - 0.75 * inch, 0.5 * inch,
                               f"page {d.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


def build(conn, *, members: list[dict], base_url: str = "") -> bytes:
    return render_pdf(gather(conn, members=members, base_url=base_url))


def filename(today: dt.date | None = None) -> str:
    d = today or dt.date.today()
    return f"oikonome-continuity-{d:%Y-%m-%d}.pdf"
