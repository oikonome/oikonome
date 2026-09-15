"""Weekly + monthly emails — the Week/Month lens payloads from
web/lenses.py, which are built to be reusable here, rendered as compact,
self-contained HTML. The daily email has its own path (report.build +
todayview.render_email); these are the longer cadences.

Weekly covers the LAST COMPLETED week (sent on the user's chosen
weekday); monthly covers the previous month (sent on the 1st)."""

from __future__ import annotations

import datetime as dt
import html as _html

from . import lenses

_MONEY = "text-align:right;white-space:nowrap"
_CARD = ("background:#1a212b;border:1px solid #2a323d;border-radius:12px;"
         "padding:16px 18px;margin:0 0 14px")
_BODY = ("margin:0;padding:18px;background:#0f141a;color:#dbe3ea;"
         "font:15px/1.5 -apple-system,'Segoe UI',Helvetica,Arial,sans-serif")
_MUT = "color:#8b98a3"
_VERDICT_COLORS = {"UNDER": "#5cb56b", "ON": "#f0b429", "OVER": "#e0695d"}


def _money(v) -> str:
    r = round(v or 0)
    return f"-${-r:,.0f}" if r < 0 else f"${r:,.0f}"


def _esc(s) -> str:
    return _html.escape(str(s or ""))


def _verdict_chip(verdict: str) -> str:
    color = next((c for k, c in _VERDICT_COLORS.items()
                  if verdict.upper().startswith(k)), "#8b98a3")
    return (f'<span style="color:{color};font-weight:700">'
            f"{_esc(verdict.upper())}</span>")


def _shell(title: str, inner: str) -> str:
    return (f'<div style="{_BODY}"><div style="max-width:640px;margin:0 auto">'
            f'<h1 style="font-size:19px;margin:0 0 12px">{_esc(title)}</h1>'
            f"{inner}"
            f'<p style="{_MUT};font-size:12px">Sent by your Oikonome '
            f"instance.</p></div></div>")


def _bucket_rows(buckets: dict) -> str:
    rows = ""
    for key in ("food", "other"):
        b = buckets.get(key) or {}
        actual = b.get("display_actual", b.get("actual", 0))
        budget_v = b.get("week_budget", b.get("month_budget", 0))
        rows += (f"<tr><td>{'Food' if key == 'food' else 'Everything else'}"
                 f'</td><td style="{_MONEY}">{_money(actual)}</td>'
                 f'<td style="{_MONEY};{_MUT}">of {_money(budget_v)}</td></tr>')
        for c in b.get("children") or []:
            rows += (f'<tr><td style="padding-left:14px;{_MUT}">'
                     f"{_esc(c['name'])}</td>"
                     f'<td style="{_MONEY};{_MUT}">{_money(c["actual"])}</td>'
                     f'<td style="{_MONEY};{_MUT}">of '
                     f"{_money(c.get('week_budget', c.get('month_budget', 0)))}"
                     f"</td></tr>")
    return rows


def build_weekly(conn, today: dt.date) -> tuple[str, str, str]:
    start = lenses.monday_of(today - dt.timedelta(days=7))
    d = lenses.week_summary(conn, start, today=today)
    verdict, variance = d["verdict"], d["variance"]
    subject = (f"Week of {start:%m/%d}: {verdict.upper()} budget "
               f"({'+' if variance >= 0 else ''}{_money(variance)})")
    days = "".join(
        f"<tr><td>{dt.date.fromisoformat(x['date']):%a %m/%d}</td>"
        f'<td style="{_MONEY}">{_money(x["variable"])}</td>'
        f'<td style="{_MONEY};{_MUT}">{_money(x["fixed"])} fixed</td></tr>'
        for x in d["days"])
    inner = (
        f'<div style="{_CARD}">Last week was {_verdict_chip(verdict)} — '
        f"variable spend {_money(d['actual'])} of "
        f"{_money(d['week_budget'])} budgeted "
        f"({'+' if variance >= 0 else ''}{_money(variance)}).</div>"
        f'<div style="{_CARD}"><table style="width:100%;border-collapse:'
        f'collapse;font-size:14px">{_bucket_rows(d["buckets"])}</table></div>'
        f'<div style="{_CARD}"><b>Day by day</b><table style="width:100%;'
        f'border-collapse:collapse;font-size:14px">{days}</table></div>')
    plain = (f"Week of {start:%m/%d}: {verdict.upper()} — "
             f"{_money(d['actual'])} of {_money(d['week_budget'])} "
             f"({'+' if variance >= 0 else ''}{_money(variance)})")
    return subject, plain, _shell(f"Your week — {start:%m/%d}", inner)


def build_monthly(conn, today: dt.date) -> tuple[str, str, str]:
    prev_last = today.replace(day=1) - dt.timedelta(days=1)
    y, m = prev_last.year, prev_last.month
    d = lenses.month_summary(conn, y, m, today=today)
    verdict, variance = d["verdict"], d["variance"]
    label = f"{prev_last:%B %Y}"
    subject = (f"{label} report card: {verdict.upper()} budget "
               f"({'+' if variance >= 0 else ''}{_money(variance)})")
    cats = "".join(
        f"<tr><td>{_esc(c)}</td><td style='{_MONEY}'>{_money(v)}</td></tr>"
        # entries trail extra elements (the raw category key the clients
        # link with); this email renders the label and the amount
        for c, v, *_ in (d.get("by_category") or [])[:8])
    big = "".join(
        f"<tr><td>{dt.date.fromisoformat(t['date']):%m/%d}</td>"
        f"<td>{_esc(t['payee'])}</td>"
        f"<td style='{_MONEY}'>{_money(t['amount'])}</td></tr>"
        for t in (d.get("biggest") or [])[:5])
    income = d.get("income") or {}
    inner = (
        f'<div style="{_CARD}">{label} finished {_verdict_chip(verdict)} — '
        f"variable spend {_money(d['variable_actual'])} of "
        f"{_money(d['variable_budget'])} "
        f"({'+' if variance >= 0 else ''}{_money(variance)}). "
        f"Total out {_money(d.get('spend_total', 0))}"
        + (f", income {_money(income['actual'])}"
           if income.get("actual") is not None else "") + ".</div>"
        f'<div style="{_CARD}"><table style="width:100%;border-collapse:'
        f'collapse;font-size:14px">{_bucket_rows(d["buckets"])}</table></div>'
        + (f'<div style="{_CARD}"><b>Top categories</b><table style="width:'
           f'100%;border-collapse:collapse;font-size:14px">{cats}</table>'
           f"</div>" if cats else "")
        + (f'<div style="{_CARD}"><b>Biggest transactions</b><table style='
           f'"width:100%;border-collapse:collapse;font-size:14px">{big}'
           f"</table></div>" if big else ""))
    plain = (f"{label}: {verdict.upper()} — {_money(d['variable_actual'])} "
             f"of {_money(d['variable_budget'])} "
             f"({'+' if variance >= 0 else ''}{_money(variance)})")
    return subject, plain, _shell(f"{label} — report card", inner)


def build_yearly(conn, today: dt.date) -> tuple[str, str, str]:
    """the annual report card — the previous CALENDAR year's Year
lens (sent on Jan 1): 12 verdict cells, annual totals vs the year
before, top categories YoY."""
    year = today.year - 1
    d = lenses.year_summary(conn, year, today=today)
    cells = [c for c in d["cells"] if c.get("verdict")]
    on = sum(1 for c in cells
             if c["verdict"] in ("ON BUDGET", "UNDER BUDGET"))
    subject = f"{year} in review: on budget {on} of {len(cells)} months"

    # verdict months show the frozen-snapshot verdict; "net" months (no
    # budget was recorded) show income − spending — a cash-flow fact, not
    # a verdict, and the footnote below says so (mirrors the Year page)
    def _month_row(c) -> str:
        label = f"{dt.date(year, c['m'], 1):%B}"
        if c.get("verdict"):
            return (f"<tr><td>{label}</td>"
                    f"<td>{_verdict_chip(c['verdict'])}</td>"
                    f'<td style="{_MONEY}">'
                    f"{'+' if (c['variance'] or 0) >= 0 else ''}"
                    f"{_money(c['variance'])}</td></tr>")
        net = c.get("net")
        val = (f"{'+' if net >= 0 else ''}{_money(net)} net"
               if net is not None else f"{_money(c.get('spend'))} spent")
        return (f"<tr><td>{label}</td>"
                f'<td style="{_MUT}">no budget</td>'
                f'<td style="{_MONEY};{_MUT}">{val}</td></tr>')

    non_future = [c for c in d["cells"] if c["status"] != "future"]
    months = "".join(_month_row(c) for c in non_future)
    net_note = (
        f'<p style="{_MUT};font-size:12px;margin:8px 0 0">Months without a '
        f"budget snapshot show net (income − spending), not a budget "
        f"verdict.</p>"
        if any(c["status"] == "net" for c in non_future) else "")
    t, p = d["totals"], d["prev_totals"]

    def _tot_row(label, cur, prev):
        pv = (f'<td style="{_MONEY};{_MUT}">{_money(prev)} in {year - 1}</td>'
              if prev is not None else f'<td style="{_MUT}"></td>')
        cv = _money(cur) if cur is not None else "—"
        return (f"<tr><td>{label}</td>"
                f'<td style="{_MONEY}">{cv}</td>{pv}</tr>')

    totals = (_tot_row("Income", t.get("income"), p.get("income"))
              + _tot_row("Spend", t.get("spend"), p.get("spend"))
              + _tot_row("Saved", t.get("saved"), p.get("saved")))
    if t.get("rate") is not None:
        totals += (f"<tr><td>Savings rate</td>"
                   f'<td style="{_MONEY}">{t["rate"]:.0f}%</td><td></td></tr>')
    cats = "".join(
        f"<tr><td>{_esc(c)}</td>"
        f'<td style="{_MONEY}">{_money(cur)}</td>'
        f'<td style="{_MONEY};{_MUT}">{_money(prev)} prior</td></tr>'
        for c, cur, prev, *_ in (d.get("categories") or [])[:10])
    inner = (
        f'<div style="{_CARD}">{year}: on budget <b>{on}</b> of '
        f"<b>{len(cells)}</b> months.</div>"
        f'<div style="{_CARD}"><b>Month by month</b><table style="width:100%;'
        f'border-collapse:collapse;font-size:14px">{months}</table>'
        f"{net_note}</div>"
        f'<div style="{_CARD}"><b>The year in total</b><table style="width:'
        f'100%;border-collapse:collapse;font-size:14px">{totals}</table></div>'
        + (f'<div style="{_CARD}"><b>Top categories</b><table style="width:'
           f'100%;border-collapse:collapse;font-size:14px">{cats}</table>'
           f"</div>" if cats else ""))
    plain = (f"{year} in review: on budget {on} of {len(cells)} months. "
             f"Spend {_money(t.get('spend'))}"
             + (f", income {_money(t['income'])}"
                if t.get("income") is not None else "") + ".")
    return subject, plain, _shell(f"{year} — the year in review", inner)
