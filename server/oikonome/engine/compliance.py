"""Compliance calendar.

Recurring obligations for a business entity, with a reminder ladder and an.ics
export. A state's LLC annual report is a DERIVED rule — from the entity's
state + formation date, for the states in STATE_ANNUAL_REPORTS (due the last
day of the anniversary month, annually). User-added obligations (a franchise
tax, a federal filing, a state this table does not know) come from
compliance_obligation. Adding a state is adding a row, not code.
"""
from __future__ import annotations

import calendar
import datetime as dt

# reminder ladder: alarms this many days before each due date
REMINDERS = (30, 7)

# States whose LLC annual report falls on the last day of the formation
# anniversary month, starting the year after formation: state code → the
# filing's title, fee, where to file and a note. A state not listed here
# derives nothing; its owner adds the obligation by hand.
STATE_ANNUAL_REPORTS: dict[str, dict] = {
    "ID": {"title": "Idaho LLC annual report", "fee": 0.0,
           "url": "https://sosbiz.idaho.gov",
           "note": "File free online — SOS Business Services."},
    "NV": {"title": "Nevada LLC annual list", "fee": 350.0,
           "url": "https://www.nvsilverflume.gov",
           "note": "The annual list of managers/members is due by the "
                   "last day of the anniversary month."},
    "WA": {"title": "Washington LLC annual report", "fee": 70.0,
           "url": "https://ccfs.sos.wa.gov",
           "note": "File online — Corporations & Charities Filing System."},
}


def _last_day(year: int, month: int) -> dt.date:
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def _state_annual_report(entity: dict, today: dt.date, n: int = 3) -> list[dict]:
    """The entity's state annual reports, due by the end of its anniversary
    month each year starting the year after formation — for a state the
    table knows; nothing otherwise."""
    fd = entity.get("formation_date")
    rule = STATE_ANNUAL_REPORTS.get((entity.get("state") or "").upper())
    if not fd or rule is None:
        return []
    if isinstance(fd, str):
        fd = dt.date.fromisoformat(fd)
    out, year = [], max(fd.year + 1, today.year)
    # step back one so we don't skip a due date earlier this year that's still ahead
    if _last_day(year - 1, fd.month) >= today and year - 1 >= fd.year + 1:
        year -= 1
    while len(out) < n:
        due = _last_day(year, fd.month)
        if due >= today:
            out.append({"title": rule["title"], "due": due,
                        "fee": rule["fee"], "url": rule["url"],
                        "note": rule["note"],
                        "source": "state", "recurrence": "yearly"})
        year += 1
    return out


def _next_occurrence(due: dt.date, recurrence: str, today: dt.date) -> dt.date:
    if recurrence != "yearly" or due >= today:
        return due
    y = today.year
    while True:
        try:
            cand = due.replace(year=y)
        except ValueError:                     # Feb 29 → 28
            cand = due.replace(year=y, day=28)
        if cand >= today:
            return cand
        y += 1


def _custom(conn, entity_id: str, today: dt.date) -> list[dict]:
    rows = conn.execute(
        "SELECT id, title, due_date, recurrence, fee, url, note FROM "
        "compliance_obligation WHERE entity_id = %s", (entity_id,)).fetchall()
    out = []
    for r in rows:
        nxt = _next_occurrence(r["due_date"], r["recurrence"], today)
        if r["recurrence"] == "once" and r["due_date"] < today:
            continue
        out.append({"id": str(r["id"]), "title": r["title"], "due": nxt,
                    "fee": float(r["fee"]) if r["fee"] is not None else None,
                    "url": r["url"], "note": r["note"], "source": "custom",
                    "recurrence": r["recurrence"]})
    return out


def _estimated_tax(entity: dict, today: dt.date) -> list[dict]:
    """The next federal 1040-ES quarterly estimated-tax deadline —
    shown only once the entity has set an assumed income-tax rate (i.e. it
    opted into the estimate)."""
    if entity.get("income_tax_rate") is None:
        return []
    from . import selfemploy
    # include LAST year: the Q4 payment for tax year Y falls on Jan 15 of Y+1,
    # so from Jan 1-14 the imminent deadline lives in quarterly_deadlines(Y-1)
    # — searching only (Y, Y+1) would skip it and point at April instead.
    upcoming = [d for yr in (today.year - 1, today.year, today.year + 1)
                for d in selfemploy.quarterly_deadlines(yr) if d >= today]
    if upcoming:
        # Name the reserve account when one is nominated: the whole point
        # of setting money aside is knowing where it is when the deadline
        # arrives, and this row is where the deadline shows up.
        note = "Quarterly estimated income + SE tax payment."
        if entity.get("tax_reserve_account_id"):
            note += " Paid from this business's nominated tax account."
        return [{"title": "Federal estimated tax (1040-ES)",
                 "due": min(upcoming), "fee": None,
                 "url": "https://www.irs.gov/payments",
                 "note": note,
                 "source": "federal", "recurrence": "quarterly"}]
    return []


def obligations(conn, entity: dict, today: dt.date | None = None) -> list[dict]:
    """The next occurrence of every obligation (derived state + federal rules +
    custom), soonest first. An archived entity is a closed business: it owes
    no FUTURE derived filings (annual report, estimated tax), so those rules
    stop generating — but its stored custom obligations remain listed, since
    they are records the user wrote and the page stays readable."""
    today = today or dt.date.today()
    derived = ([] if entity.get("status") == "archived"
               else _state_annual_report(entity, today, n=1)
               + _estimated_tax(entity, today))
    items = derived + _custom(conn, entity["id"], today)
    items.sort(key=lambda o: o["due"])
    return [{**o, "due": o["due"].isoformat()} for o in items]


def add_obligation(conn, entity_id: str, *, title: str, due_date,
                   recurrence: str = "yearly", fee=None, url=None,
                   note=None) -> dict:
    if not (title or "").strip():
        raise ValueError("title is required")
    if recurrence not in ("yearly", "once"):
        raise ValueError("recurrence must be yearly or once")
    if fee is not None and fee != "":
        try:
            fee = float(fee)          # raw NUMERIC insert would 500 on a bad
        except (TypeError, ValueError):   # literal; surface a clean 400 instead
            raise ValueError("fee must be a number")
        # …and a well-formed but absurd number overflows NUMERIC(10,2) in the
        # driver, which is the same 500 by another road. Bound it at the door.
        if abs(fee) > 99_999_999.99:
            raise ValueError("fee is too large")
    else:
        fee = None
    url = _safe_url(url)
    from .compat import req_date
    due_date = req_date(due_date)
    r = conn.execute(
        """INSERT INTO compliance_obligation
             (entity_id, title, due_date, recurrence, fee, url, note)
           VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (entity_id, title.strip(), due_date, recurrence, fee, url,
         note)).fetchone()
    return {"id": str(r["id"])}


def _safe_url(url):
    """An obligation's link is rendered as an anchor href on the Business
    page, and every member of the household sees it. With the scheme
    unchecked, a `javascript:` (or `data:`) link stored on one obligation
    runs for whoever opens the page. http/https or nothing."""
    u = (url or "").strip()
    if not u:
        return None
    if u.lower().startswith(("http://", "https://")):
        return u
    raise ValueError("a link must start with http:// or https://")


def delete_obligation(conn, obligation_id: str, entity_id: str) -> bool:
    return conn.execute(
        "DELETE FROM compliance_obligation WHERE id = %s AND entity_id = %s",
        (obligation_id, entity_id)).rowcount > 0


def _ics_escape(s: str) -> str:
    # Normalize CR/CRLF to escaped \n FIRST — a bare \r left in a field is a
    # line terminator to many .ics parsers, letting owner-controlled title/
    # note/url text inject new calendar lines/components (RFC 5545 §3.3.11).
    return (s or "").replace("\\", "\\\\").replace(";", "\\;") \
        .replace(",", "\\,").replace("\r\n", "\n").replace("\r", "\n") \
        .replace("\n", "\\n")


def ics(conn, entity: dict, today: dt.date | None = None, n: int = 3) -> str:
    """An .ics with the next `n` occurrences of each obligation, each carrying
    the reminder ladder (VALARM at 30 and 7 days before)."""
    today = today or dt.date.today()
    # same set the in-app list (obligations) shows — including the federal
    # estimated-tax deadline, so the .ics doesn't silently drop it; and the
    # same archived rule — a closed business exports no future derived filings
    derived = ([] if entity.get("status") == "archived"
               else _state_annual_report(entity, today, n=n)
               + _estimated_tax(entity, today))
    events = derived + _custom(conn, entity["id"], today)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//Oikonome//Compliance//EN", "CALSCALE:GREGORIAN"]
    for i, o in enumerate(events):
        due = o["due"]
        if isinstance(due, str):
            due = dt.date.fromisoformat(due)
        dtstart = due.strftime("%Y%m%d")
        dtend = (due + dt.timedelta(days=1)).strftime("%Y%m%d")
        fee = (f" (${o['fee']:.2f})" if o.get("fee") else
               " (free)" if o.get("fee") == 0.0 else "")
        summary = _ics_escape(f"{o['title']} — {entity['name']}{fee}")
        desc = _ics_escape(
            f"{o.get('note') or ''} {o.get('url') or ''}".strip())
        uid = f"oiko-{entity['id']}-{o.get('source','')}-{dtstart}-{i}@oikonome.com"
        lines += ["BEGIN:VEVENT", f"UID:{uid}",
                  f"DTSTART;VALUE=DATE:{dtstart}",
                  f"DTEND;VALUE=DATE:{dtend}",
                  f"SUMMARY:{summary}"]
        if desc:
            lines.append(f"DESCRIPTION:{desc}")
        for days in REMINDERS:
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY",
                      f"TRIGGER:-P{days}D",
                      f"DESCRIPTION:{summary} due in {days} days",
                      "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
