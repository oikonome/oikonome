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
import math

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


# ---- federal dates -------------------------------------------------------
# A federal filing or payment due on a Saturday, a Sunday or a legal
# holiday in the District of Columbia is timely on the next business day
# (the tax code's weekend-and-holiday rule). The holidays are the federal
# ones (5 U.S.C. §6103) plus
# D.C.'s Emancipation Day, April 16 — the one that moves an April 15
# return when the 16th is observed on the 15th or the 17th. A holiday on a
# Saturday is observed the Friday before, on a Sunday the Monday after.

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """The n-th `weekday` (Monday 0) of the month; n = -1 is the last."""
    if n > 0:
        first = dt.date(year, month, 1)
        return first + dt.timedelta(days=(weekday - first.weekday()) % 7
                                    + 7 * (n - 1))
    last = _last_day(year, month)
    return last - dt.timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d: dt.date) -> dt.date:
    if d.weekday() == 5:
        return d - dt.timedelta(days=1)
    if d.weekday() == 6:
        return d + dt.timedelta(days=1)
    return d


def federal_holidays(year: int) -> frozenset:
    """The days federal deadlines in `year` cannot fall on besides weekends
    (observed dates). New Year's Day of the NEXT year is listed when it is
    observed on this year's December 31."""
    fixed = [(1, 1), (6, 19), (7, 4), (11, 11), (12, 25), (4, 16)]
    days = {_observed(dt.date(year, m, d)) for m, d in fixed}
    days |= {_nth_weekday(year, 1, 0, 3),      # Martin Luther King Jr. Day
             _nth_weekday(year, 2, 0, 3),      # Washington's Birthday
             _nth_weekday(year, 5, 0, -1),     # Memorial Day
             _nth_weekday(year, 9, 0, 1),      # Labor Day
             _nth_weekday(year, 10, 0, 2),     # Columbus Day
             _nth_weekday(year, 11, 3, 4)}     # Thanksgiving
    nxt = _observed(dt.date(year + 1, 1, 1))
    if nxt.year == year:
        days.add(nxt)
    return frozenset(days)


def business_day(d: dt.date) -> dt.date:
    """`d`, or the next day that is neither a weekend nor a federal
    holiday: when a federal date that falls on `d` is actually due."""
    while d.weekday() >= 5 or d in federal_holidays(d.year):
        d += dt.timedelta(days=1)
    return d


# The annual federal return each structure files, and its due date in the
# year after the tax year (month, day), before the business-day rule.
_RETURNS = {
    "sole_prop": ("Federal return with Schedule C (Form 1040)", (4, 15),
                  "The business's profit or loss goes on Schedule C of "
                  "the owner's own Form 1040."),
    "single_member_llc": ("Federal return with Schedule C (Form 1040)",
                          (4, 15),
                          "A single-member LLC files on the owner's own "
                          "Form 1040, with Schedule C."),
    "multi_member_llc": ("Partnership return (Form 1065)", (3, 15),
                         "Each member gets a Schedule K-1 for their own "
                         "return."),
    "s_corp": ("S corporation return (Form 1120-S)", (3, 15),
               "Each shareholder gets a Schedule K-1 for their own "
               "return."),
}


def _began(entity: dict) -> int | None:
    """The first tax year the business existed in, when it is known."""
    for k in ("formation_date", "business_start_date"):
        v = entity.get(k)
        if v:
            return (dt.date.fromisoformat(v) if isinstance(v, str) else v).year
    return None


def _estimated_tax(entity: dict, today: dt.date) -> list[dict]:
    """The next federal 1040-ES quarterly estimated-tax deadline. Every
    structure here passes its profit to its owners, who pay the estimates
    on their own returns."""
    from . import selfemploy
    # include LAST year: the Q4 payment for tax year Y falls on Jan 15 of Y+1,
    # so from Jan 1-14 the imminent deadline lives in quarterly_deadlines(Y-1)
    # — searching only (Y, Y+1) would skip it and point at April instead.
    began = _began(entity)
    upcoming = [d for yr in (today.year - 1, today.year, today.year + 1)
                if began is None or yr >= began
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


def _annual_return(entity: dict, today: dt.date) -> list[dict]:
    """The next federal annual return for the entity's structure: the first
    tax year whose due date is still ahead, from the year the business
    began."""
    rule = _RETURNS.get(entity.get("structure") or "")
    if rule is None:
        return []
    title, (m, d), note = rule
    began = _began(entity)
    for tax_year in (today.year - 1, today.year):
        if began is not None and tax_year < began:
            continue
        due = business_day(dt.date(tax_year + 1, m, d))
        if due >= today:
            return [{"title": f"{title} for {tax_year}", "due": due,
                     "fee": None, "url": "https://www.irs.gov/filing",
                     "note": note, "source": "federal",
                     "recurrence": "yearly"}]
    return []


def _forms_1099(conn, entity: dict, today: dt.date) -> list[dict]:
    """The 1099-NEC (and 1096) filing, January 31, for a tax year in which
    the business paid any vendor at least the 1099-NEC threshold outside a
    card — the vendors list's own figures (selfemploy.vendor_totals)."""
    from . import selfemploy
    for tax_year in (today.year - 1, today.year):
        due = business_day(dt.date(tax_year + 1, 1, 31))
        if due < today:
            continue
        threshold = selfemploy.reportable_1099_threshold(tax_year)
        over = [v for v in selfemploy.vendor_totals(conn, entity["id"],
                                                    tax_year)
                if v["paid"] >= threshold]
        if not over:
            return []
        n = len(over)
        return [{"title": f"1099-NEC forms for {tax_year}", "due": due,
                 "fee": None, "url": "https://www.irs.gov/filing/e-file-"
                                     "information-returns-with-iris",
                 "note": (f"{n} payee{'s' if n != 1 else ''} paid "
                          f"${threshold:,.0f} or more outside a card — "
                          "a contractor who is not a corporation gets a "
                          "1099-NEC, and a copy goes to the IRS."),
                 "source": "federal", "recurrence": "yearly"}]
    return []


def _federal(conn, entity: dict, today: dt.date) -> list[dict]:
    return (_estimated_tax(entity, today) + _forms_1099(conn, entity, today)
            + _annual_return(entity, today))


def obligations(conn, entity: dict, today: dt.date | None = None) -> list[dict]:
    """The next occurrence of every obligation (derived state + federal rules +
    custom), soonest first. The federal dates follow the entity's structure
    and the year (specs/business-books.md → Deadlines). An archived entity is a closed business: it owes
    no FUTURE derived filings (annual report, estimated tax), so those rules
    stop generating — but its stored custom obligations remain listed, since
    they are records the user wrote and the page stays readable."""
    today = today or dt.date.today()
    derived = ([] if entity.get("status") == "archived"
               else _state_annual_report(entity, today, n=1)
               + _federal(conn, entity, today))
    items = derived + _custom(conn, entity["id"], today)
    items.sort(key=lambda o: o["due"])
    return [{**o, "due": o["due"].isoformat()} for o in items]


# the largest fee NUMERIC(10,2) holds
FEE_MAX = 99_999_999.99


class FeeTooLarge(ValueError):
    """A well-formed fee past what the column holds."""


def check_obligation(*, title, recurrence, fee, url) -> tuple:
    """An obligation's fields as the column takes them, or ValueError
    (FeeTooLarge for a fee past the column). The one judgement both doors
    that write an obligation apply: the Business page's add, and a
    restore's archived row."""
    if not (title or "").strip():
        raise ValueError("title is required")
    if recurrence not in ("yearly", "once"):
        raise ValueError("recurrence must be yearly or once")
    if fee is not None and fee != "":
        try:
            fee = float(fee)          # raw NUMERIC insert would 500 on a bad
        except (TypeError, ValueError, OverflowError):   # literal → clean 400
            raise ValueError("fee must be a number")
        # NaN passes the size check below (its comparisons are all False)
        if not math.isfinite(fee):
            raise ValueError("fee must be a number")
        # …and a well-formed but absurd number overflows NUMERIC(10,2) in the
        # driver, which is the same 500 by another road. Bound it at the door.
        if abs(fee) > FEE_MAX:
            raise FeeTooLarge("fee is too large")
    else:
        fee = None
    return title.strip(), recurrence, fee, _safe_url(url)


def add_obligation(conn, entity_id: str, *, title: str, due_date,
                   recurrence: str = "yearly", fee=None, url=None,
                   note=None) -> dict:
    title, recurrence, fee, url = check_obligation(
        title=title, recurrence=recurrence, fee=fee, url=url)
    from .compat import req_date
    due_date = req_date(due_date)
    r = conn.execute(
        """INSERT INTO compliance_obligation
             (entity_id, title, due_date, recurrence, fee, url, note)
           VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (entity_id, title, due_date, recurrence, fee, url,
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
               + _federal(conn, entity, today))
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
