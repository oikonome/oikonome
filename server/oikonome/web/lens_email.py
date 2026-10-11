"""Monthly + yearly emails — the Month/Year lens payloads from
web/lenses.py, which are built to be reusable here, rendered through the
daily email's shell. The daily email has its own path (report.build +
todayview.render_email); these are the longer cadences.

Monthly covers the previous month (sent on the 1st); yearly the previous
calendar year (sent on Jan 1). There is no weekly report."""

from __future__ import annotations

import datetime as dt
import html as _html

from ..notify.render import verdict_phrase
from . import lenses



def _money(v) -> str:
    r = round(v or 0)
    return f"-${-r:,.0f}" if r < 0 else f"${r:,.0f}"


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# the strip's cell colours: the pill palette the inliner uses, by verdict
_TONES = {"UNDER": ("rgba(92,181,107,.16)", "#5cb56b"),
          "ON": ("rgba(240,180,41,.16)", "#f0b429"),
          "OVER": ("rgba(224,105,93,.16)", "#e0695d"),
          "net": ("rgba(139,152,163,.16)", "#8b98a3"),
          "future": ("#232c38", "#8b98a3")}


def _signed(v) -> str:
    """+$1,280 / −$310 — the Year lens's sign convention (a real minus)."""
    r = round(v or 0)
    return f"−${-r:,.0f}" if r < 0 else f"+${r:,.0f}"


def _tone_of(v) -> str:
    return "" if v is None else ("pos" if v >= 0 else "neg")


def _budget_rows(bills_posted: float, bills_planned: float, bills_note: str,
                 buckets: dict, children: list[dict], pinned: list[dict],
                 months: int = 1) -> list[dict]:
    """The budget bars both cadences draw: Bills (without any envelope
    pinned to Today), Food, Everything else, each carve-out, then each
    pinned envelope — every bar red or green by whether it ran over.
    `months` > 1 is a year: the envelope's cap is its monthly cap times
    the budgeted months."""
    rows = []

    def row(label, actual, plan, note=""):
        over = (actual or 0) > (plan or 0) + 0.5
        diff = (actual or 0) - (plan or 0)
        frac = 0.0
        if plan and plan > 0:
            frac = min(1.0, max(0.0, (actual or 0) / plan))
        elif (actual or 0) > 0:
            frac = 1.0
        rows.append({"label": label, "actual": actual, "plan": plan,
                     "actual_text": _money(actual), "plan_text": _money(plan),
                     "frac": round(frac * 100, 1),
                     "bar": "#e0695d" if over else "#5cb56b",
                     "note": note or (f"{_money(diff)} over" if over else
                                      f"{_money(-diff)} under" if diff < -0.5
                                      else "on the plan"),
                     "note_tone": ("neg" if over else
                                   "pos" if diff < -0.5 else ""),
                     "child": False,
                     "actual_tone": "neg" if over else ""})

    row("Bills", bills_posted, bills_planned, note=bills_note)
    for key, name in (("food", "Food"), ("other", "Everything else")):
        b = buckets.get(key) or {}
        actual = b.get("display_actual", b.get("actual", 0)) or 0
        plan = b.get("month_budget", 0) or 0
        if b.get("children"):
            # the carve-outs have bars of their own below, so the parent
            # bar is what is left of BOTH its spend and its budget — the
            # lens page's rule
            plan = b.get("remaining_budget", plan) or 0
        row(name, actual, plan)
        for c in b.get("children") or []:
            row(c["name"], c.get("actual") or 0, c.get("month_budget") or 0)
    for c in children:
        row(c["name"], c.get("actual") or 0, c.get("month_budget") or 0)
    for e in sorted(pinned, key=lambda e: -(e.get("monthly") or 0)):
        em = int(e.get("period_months") or 1)
        overflow = e.get("overflow") or 0
        if months == 1 and em > 1:
            # a pool over several months: the period's swipes against the
            # period's cap, or a $25/mo envelope paid once a year reads as
            # nine times over on the month it is paid
            cap = e.get("pool") or (e.get("monthly") or 0) * em
            used = (e.get("period_used") if e.get("period_used") is not None
                    else e.get("used") or 0) + overflow
            scope = f"{em}-month envelope"
        else:
            cap = e.get("monthly") or 0
            used = (e.get("used") or 0) + overflow
            scope = "envelope"
        row(e["payee"], used, cap,
            note=(scope + " · " + (f"{_money(overflow)} spilled past the cap"
                                   if overflow > 0.5 else
                                   f"{_money(cap - used)} of the cap left"
                                   if cap - used > 0.5 else "cap used exactly")))
    return rows


def _pie(cats_source, posted: float, bill_cats=(), *,
         max_slices: int = 8, floor: float = 0.03) -> dict:
    """The slices worth drawing — the everyday spend and the bills under
    their categories, the tail folded into Other — and the legend list.

    `bill_cats` is [(label, amount)] for the bills: the paid bills by
    their charges' transaction category (so a mortgage reads as a loan
    payment, the power and water as utilities, and a bill the household
    tagged "Subscriptions" as that) and each envelope by its name. A bill
    category that is also an everyday category adds to that slice: medical
    bills and the pharmacy run are one Medical. Whatever of `posted` the
    labels do not account for — a charge with no category — stays a plain
    Bills slice rather than vanishing. Every label passes through
    lenses.ring_label, so the rent and the mortgage are one housing slice
    whichever side they come from. `max_slices` and `floor` bound the
    ring: a slice past the count or under the share folds into Other."""
    by: dict[str, float] = {}
    for c in cats_source:
        if c[1] > 0:
            lab = lenses.ring_label(c[0])
            by[lab] = by.get(lab, 0.0) + c[1]
    labelled = 0.0
    for label, v in bill_cats:
        if v > 0:
            lab = lenses.ring_label(label)
            by[lab] = by.get(lab, 0.0) + v
            labelled += v
    rest = posted - labelled
    if rest > 0.5:
        by["Bills"] = by.get("Bills", 0.0) + rest
    cats_all = sorted(by.items(), key=lambda x: -x[1])
    total = sum(v for _, v in cats_all) or 0
    slices: list[tuple[str, float, float]] = []
    tail = 0.0
    for i, (c, v) in enumerate(cats_all):
        share = v / total if total else 0
        if i < max_slices and share >= floor:
            slices.append((c, v, share))
        else:
            tail += v
    if tail > 0.5 and total:
        slices.append(("Other", tail, tail / total))
    return {"cats": [{"label": c, "amount_text": _money(v), "amount": v,
                      "pct": round(100 * sh)} for c, v, sh in slices],
            "cat_slices": slices, "cat_total_text": _money(total),
            "cat_total": total, "cat_pie": None}


def _verdict_words(verdict: str) -> tuple[str, str]:
    """('under budget', 'pos') — the words and tone for a verdict."""
    v = (verdict or "").upper()
    key = next((k for k in ("UNDER", "ON", "OVER") if v.startswith(k)), "")
    text = {"UNDER": "under budget", "ON": "on budget",
            "OVER": "over budget"}.get(key, "no verdict")
    tone = {"UNDER": "pos", "ON": "warn", "OVER": "neg"}.get(key, "warn")
    return text, tone


def _variable_line(va, vb, variance, tolerance, final=True, tail="") -> str:
    if variance > tolerance:
        judged = f'<b class="neg">{_money(variance)} over</b>'
    elif variance < -tolerance:
        judged = f'<b class="pos">{_money(-variance)} under</b>'
    else:
        judged = f'<b class="warn">within tolerance ({_money(tolerance)})</b>'
    return (f"Variable spend <b>{_money(va)}</b> of {_money(vb)} — {judged}"
            + ("" if final else " so far") + tail + ".")


def month_mail_context(d: dict, year: dict, y: int, m: int,
                       savings_plan: float | None = None) -> dict:
    """Everything the monthly template renders, computed ONCE from the
    Month lens payload (`d`, lenses.month_summary) and the Year lens
    payload (`year`, lenses.year_summary) for the same year. The plain
    text mirror reads this same dict, so the two renders cannot drift.

    The mail is about NET: what each month kept or lost, not its totals.
    The budget section is the fixed bar and then the
    variable budget split the way the household split it (the buckets,
    their carve-outs, and every envelope pinned to Today), every bar red
    or green by whether it ran over. The categories are the everyday
    spend and the bills by their kind, in one ring.

    `savings_plan` is accepted for callers that still pass it; the Savings
    row left the mail with the other plan rows."""
    label = f"{dt.date(y, m, 1):%B %Y}"
    verdict = (d.get("verdict") or "").upper()
    variance = d.get("variance") or 0
    tolerance = d.get("tolerance") or 0
    income = (d.get("income") or {}).get("actual")
    spend = d.get("spend_total") or 0
    net = (income - spend) if income is not None else None

    # ---- the year strip: a month's net, coloured by its sign; months
    #      after the report month are not yet ----
    strip, ytd_net, counted, judged, kept = [], 0.0, 0, 0, 0
    net_known, unknown_months = True, False
    for c in year.get("cells") or []:
        cm = c["m"]
        if cm > m or c.get("status") == "future":
            bg, fg = _TONES["future"]
            strip.append({"abbr": _MONTHS[cm - 1], "text": "·", "bg": bg,
                          "fg": fg, "current": False, "net": None})
            continue
        v = (c.get("verdict") or "").upper()
        cnet = c.get("net")
        if cnet is None:
            # income unknown for the month: nothing to colour, the spend
            # alone is shown so the cell is not blank
            bg, fg = _TONES["net"]
            text = _money(c.get("spend") or 0) + " out"
            unknown_months = True
            net_known = False
        else:
            bg, fg = _TONES["UNDER" if cnet >= 0 else "OVER"]
            text = _signed(cnet)
            ytd_net += cnet
        strip.append({"abbr": _MONTHS[cm - 1], "text": text, "bg": bg,
                      "fg": fg, "current": cm == m, "net": cnet})
        counted += 1
        if v:
            judged += 1
            if v.startswith(("UNDER", "ON")):
                kept += 1
    plural = "s" if counted != 1 else ""
    ytd = {"net_text": _signed(ytd_net) if net_known else "—",
           "tone": _tone_of(ytd_net) if net_known else "",
           "months_text": (f"{counted} month{plural} so far · on or under "
                           f"budget {kept} of {judged}" if judged else
                           f"{counted} month{plural} so far"),
           "months": counted, "net": ytd_net if net_known else None,
           "unknown_months": unknown_months}

    # ---- the month's verdict and net ----
    status = d.get("status") or "final"
    verdict_text, verdict_tone = _verdict_words(verdict)
    if net is None:
        sentence = ("Income is not known for this month (no bank account "
                    "feeds it), so there is no net.")
    elif net >= 0:
        sentence = f"You made money this month: {_money(net)} more in than out."
    else:
        sentence = f"You lost money this month: {_money(-net)} more out than in."
    flow = {"net_text": _signed(net) if net is not None else "—",
            "tone": _tone_of(net), "sentence": sentence, "net": net}
    variable_line = _variable_line(d.get("variable_actual") or 0,
                                   d.get("variable_budget") or 0,
                                   variance, tolerance, final=status == "final")
    notes = []
    if status == "final" and d.get("budget_source") == "live":
        notes.append("No budget snapshot exists for this month — the verdict "
                     "compares it against the current budget, applied "
                     "retroactively.")
    if d.get("variable_scale"):
        notes.append(f"Heavy bill month: variable budgets auto-scaled from "
                     f"{_money(d['variable_scale']['static_total'])}.")

    # ---- the budget bars ----
    bills = d.get("bills") or {}
    paid_n = len(bills.get("paid") or [])
    awaiting = bills.get("awaiting") or 0
    overdue = bills.get("overdue") or []
    posted, planned = bills.get("posted") or 0, bills.get("planned") or 0
    # An envelope the household pinned to Today is a cap it watches like a
    # budget of its own (a monthly helper, say), so it gets its own bar
    # and leaves the Bills bar, which otherwise counts it. The other
    # envelopes stay inside Bills: eight bars for eight small caps is
    # noise.
    pinned = [e for e in bills.get("envelopes") or [] if e.get("show_today")]
    bills_posted = posted - sum(e.get("used") or 0 for e in pinned)
    bills_planned = planned - sum(e.get("monthly") or 0 for e in pinned)
    bits = [f"{paid_n} paid"]
    if awaiting:
        bits.append(f"{_money(awaiting)} due, not posted")
    if overdue:
        bits.append(f"{len(overdue)} overdue")
    bdiff = bills_posted - bills_planned
    bits.append(f"{_money(bdiff)} over the plan" if bdiff > 0.5 else
                f"{_money(-bdiff)} under the plan" if bdiff < -0.5 else
                "on the plan")
    if pinned:
        bits.append("without " + ", ".join(e["payee"] for e in pinned))
    rows = _budget_rows(bills_posted, bills_planned, " · ".join(bits),
                        d.get("buckets") or {}, [], pinned)

    source = d.get("by_category_variable")
    if source is None:
        source = d.get("by_category") or []
    bill_cats: dict[str, float] = {}
    for r in bills.get("paid") or []:
        if r.get("category"):
            lab = lenses.ring_label(r["category"], r.get("detailed"))
            bill_cats[lab] = bill_cats.get(lab, 0.0) + (r.get("amount") or 0)
    for e in bills.get("envelopes") or []:
        if e.get("used"):
            bill_cats[e["payee"]] = bill_cats.get(e["payee"], 0.0) + e["used"]
    pie = _pie(source, posted, list(bill_cats.items()))
    month_name = f"{dt.date(y, m, 1):%B}"
    status_text = ("final" if status == "final" else
                   f"day {dt.date.fromisoformat(d['as_of']).day} "
                   f"of {d.get('days_in_month')}"
                   if status == "in_progress" else "planned")
    return {"label": label, "year": y, "month_name": month_name,
            "status_text": status_text,
            "verdict": verdict, "verdict_text": verdict_text,
            "verdict_tone": verdict_tone, "variance": variance,
            "strip": strip, "ytd": ytd,
            "flow": flow, "variable_line": variable_line, "notes": notes,
            "rows": rows, "year_strip": [], **pie,
            # the shared template's fields
            "headline": f"{month_name} {y}: {verdict_text}",
            "tiles": [{"eyebrow": f"Net for {month_name}",
                       "value": flow["net_text"], "tone": flow["tone"],
                       "sub": sentence},
                      {"eyebrow": "Year to date net", "value": ytd["net_text"],
                       "tone": ytd["tone"], "sub": ytd["months_text"]}],
            "strip_legend": ("Green made money, red lost money · the number "
                             "is income − spending"
                             + (" · grey = income unknown"
                                if unknown_months else "")),
            "bars_title": "Monthly budget",
            "bars_intro": ("The bills in one bar, then the variable budget as "
                           "you split it, then any bill pinned to Today. Each "
                           "bar is the plan, the fill is what happened: green "
                           "under, red over."),
            "pie_title": "Spending by category",
            "pie_sub": f"{pie['cat_total_text']} out · the bills under "
                       f"their categories."}


def _plain(ctx: dict) -> str:
    """The text/plain mirror of either report, from the same context."""
    lines = [ctx["headline"]]
    for t in ctx["tiles"]:
        lines.append(f"{t['eyebrow']}: {t['value']}"
                     + (f" — {t['sub']}" if t.get("sub") else ""))
    lines.append(_html.unescape(
        __import__("re").sub(r"<[^>]+>", "", ctx["variable_line"])))
    done = [c for c in ctx["strip"] if c["text"] != "·"]
    if done:
        lines.append("  " + " · ".join(f"{c['abbr']} {c['text']}" for c in done))
    if ctx.get("year_strip"):
        lines.append("Net by year: " + " · ".join(
            f"{c['abbr']} {c['text']}" for c in ctx["year_strip"]))
    if ctx["rows"]:
        lines.append(ctx["bars_title"] + ":")
    for r in ctx["rows"]:
        lines.append(f"  {r['label']}: {r['actual_text']} of {r['plan_text']}"
                     + (f" — {r['note']}" if r["note"] else ""))
    if ctx["cats"]:
        lines.append(f"{ctx['pie_title']} ({ctx['cat_total_text']}): "
                     + ", ".join(f"{c['label']} {c['pct']}% {c['amount_text']}"
                                 for c in ctx["cats"]))
    return "\n".join(lines)


def month_mail_plain(ctx: dict) -> str:
    return _plain(ctx)


def _render(heading: str, ctx: dict) -> str:
    from . import todayview
    if ctx["cat_slices"]:
        # rasterised for the mail only (clients strip SVG); report.send
        # turns the data: URI into an inline image part
        from . import emailchart
        png = emailchart.pie(ctx["cat_slices"])
        ctx["cat_pie"] = todayview._data_uri(png) if png else None
    return todayview.render_mail(heading, "report_body.html", ctx=ctx)


def build_monthly(conn, today: dt.date) -> tuple[str, str, str]:
    """The monthly report card: the previous month read the way the Month
    lens reads it — the year strip it sits in with the running total,
    the month's net, every budget item, spending by category. Both payloads are the lens pages' own, so the email
    and the pages cannot disagree."""
    prev_last = today.replace(day=1) - dt.timedelta(days=1)
    y, m = prev_last.year, prev_last.month
    d = lenses.month_summary(conn, y, m, today=today)
    year = lenses.year_summary(conn, y, today=today)
    ctx = month_mail_context(d, year, y, m)
    verdict, variance = d["verdict"], d["variance"]
    subject = (f"{ctx['label']} report card: {verdict_phrase(verdict)} "
               f"({'+' if variance >= 0 else ''}{_money(variance)})")
    if ctx["flow"]["net"] is not None:
        subject += f" · net {ctx['flow']['net_text']}"
    html = _render(f"{ctx['label']} — report card", ctx)
    return subject, _plain(ctx), html


# ---- the year ---------------------------------------------------------------

def year_mail_context(d: dict, year: int) -> dict:
    """Everything the yearly template renders, from one Year lens payload
    built with `with_detail=True` — the monthly's anatomy at year scale:
    the year's net beside last year's, the twelve months, the verdict
    count, then every year the ledger holds with its net; the four bars
    as annual totals against the budgeted months' plan; the year's
    category ring."""
    det = d.get("detail") or {}
    cells = d.get("cells") or []
    strip, net_sum, counted, judged, kept, over_months = [], 0.0, 0, 0, 0, []
    net_known, unknown_months = True, False
    for c in cells:
        cm = c["m"]
        if c.get("status") == "future":
            bg, fg = _TONES["future"]
            strip.append({"abbr": _MONTHS[cm - 1], "text": "·", "bg": bg,
                          "fg": fg, "current": False, "net": None})
            continue
        v = (c.get("verdict") or "").upper()
        cnet = c.get("net")
        if cnet is None:
            bg, fg = _TONES["net"]
            text = _money(c.get("spend") or 0) + " out"
            unknown_months = True
            net_known = False
        else:
            bg, fg = _TONES["UNDER" if cnet >= 0 else "OVER"]
            text = _signed(cnet)
            net_sum += cnet
        strip.append({"abbr": _MONTHS[cm - 1], "text": text, "bg": bg,
                      "fg": fg, "current": False, "net": cnet})
        counted += 1
        if v:
            judged += 1
            if v.startswith(("UNDER", "ON")):
                kept += 1
            else:
                over_months.append(_MONTHS[cm - 1])
    net = net_sum if net_known else None
    final = counted == 12
    if net is None:
        headline, tone = f"{year}: income unknown", "warn"
        sentence = ("Income is not known for part of the year (no bank "
                    "account feeds it), so there is no yearly net.")
    elif net >= 0:
        headline, tone = f"{year}: you made money", "pos"
        sentence = f"{_money(net)} more in than out"
    else:
        headline, tone = f"{year}: you lost money", "neg"
        sentence = f"{_money(-net)} more out than in"
    t = d.get("totals") or {}
    if net is not None and t.get("income"):
        sentence += (f" · {100 * net / t['income']:.1f}% of income kept"
                     if net >= 0 else "")
    prev = d.get("prev_totals") or {}
    prev_net = prev.get("saved")
    # last year's net from the year strip when the payload carries it:
    # that figure is unknown when any month of the year had no bank feed,
    # the rule this year's headline follows, so the two tiles compare like
    # with like
    prev_year = next((yr for yr in det.get("years") or []
                      if yr.get("y") == year - 1), None)
    if det.get("years") is not None:
        prev_net = prev_year.get("net") if prev_year else None
    prev_tile = {"eyebrow": f"Net for {year - 1}",
                 "value": _signed(prev_net) if prev_net is not None else "—",
                 "tone": _tone_of(prev_net),
                 "sub": ("last year, for comparison" if prev_net is not None
                         else "income not known for all of last year")}
    ba = d.get("buckets_annual") or {}
    va = (ba.get("food", {}).get("actual") or 0) + (ba.get("other", {}).get("actual") or 0)
    vb = ((ba.get("food", {}).get("month_budget") or 0)
          + (ba.get("other", {}).get("month_budget") or 0))
    budgeted = det.get("budgeted_months") or judged
    # the year has no engine verdict of its own; its line states the plain
    # over/under of the sums, with the monthly verdicts counted beside it
    variance = va - vb
    tolerance = 0.0
    tail = (f" across {budgeted} budgeted month{'s' if budgeted != 1 else ''}"
            f" · on or under budget <b>{kept} of {judged}</b>"
            + (f" ({', '.join(over_months)} over)" if over_months else "")
            if judged else "")
    variable_line = _variable_line(va, vb, variance, tolerance, final=True,
                                   tail=tail)
    notes = []
    if judged and judged < counted:
        notes.append(f"{counted - judged} month{'s' if counted - judged != 1 else ''}"
                     " had no budget on record; the bars and the verdict count "
                     "cover the budgeted months only.")

    # bars: annual sums over the budgeted months; a pinned envelope's annual
    # cap is its monthly cap over those months
    envs = det.get("envelopes") or {}
    pinned = [{"payee": k, "used": v["used"], "monthly": v["monthly"],
               "overflow": v["overflow"], "period_months": 1}
              for k, v in envs.items() if v.get("show_today")]
    fixed = ba.get("fixed") or {}
    bills_posted = (fixed.get("actual") or 0) - sum(e["used"] for e in pinned)
    bills_planned = ((fixed.get("month_budget") or 0)
                     - sum(e["monthly"] for e in pinned))
    bdiff = bills_posted - bills_planned
    bits = [f"{det.get('bills_paid', 0)} paid"]
    bits.append(f"{_money(bdiff)} over the plan" if bdiff > 0.5 else
                f"{_money(-bdiff)} under the plan" if bdiff < -0.5 else
                "on the plan")
    if pinned:
        bits.append("without " + ", ".join(e["payee"] for e in pinned))
    children = [{"name": k, "actual": v["actual"],
                 "month_budget": v["month_budget"]}
                for k, v in (det.get("children") or {}).items()]
    # the parents' annual figures: with carve-outs, what is left of both
    # the spend and the budget (the lens page's rule, at year scale)
    buckets = {}
    for k in ("food", "other"):
        b = dict(ba.get(k) or {})
        kids = [c for c in children if (det.get("children") or {})
                .get(c["name"], {}).get("parent") == k]
        if kids:
            b["display_actual"] = (b.get("actual") or 0) - sum(c["actual"] for c in kids)
            b["remaining_budget"] = ((b.get("month_budget") or 0)
                                     - sum(c["month_budget"] for c in kids))
            b["children"] = [{"name": c["name"], "actual": c["actual"],
                              "month_budget": c["month_budget"]} for c in kids]
        buckets[k] = b
    rows = _budget_rows(bills_posted, bills_planned, " · ".join(bits),
                        buckets, [], pinned, months=max(1, budgeted))
    if not budgeted:
        # a year before the household budgeted: nothing to draw bars
        # against, and a "$0 of $0" bar says less than saying so
        rows = []
        variable_line = (f"No budget was on record for {year}, so there are "
                         "no bars or verdicts — the net and the categories "
                         "are the whole story.")
        notes = []

    bill_cats = [(c[0], c[1]) for c in det.get("categories_bills") or []]
    bill_cats += [(k, v["used"]) for k, v in envs.items() if v.get("used")]
    # a year has room for more slices than a month: twelve, down to 2%,
    # so Other does not swallow a fifth of the year
    pie = _pie(det.get("categories_variable") or [],
               det.get("fixed_total") or 0, bill_cats, max_slices=12, floor=0.02)
    # every year back to the ledger's first, each its net coloured by sign;
    # a year without bank coverage shows what went out, in grey
    year_strip = []
    for yr in det.get("years") or []:
        if yr.get("net") is None:
            bg, fg = _TONES["net"]
            # a year with no bank coverage: what went out, or nothing on
            # record at all (a gap in the ledger reads as a gap)
            text = (_money(yr["spend"]) + " out") if yr.get("spend") else "·"
        else:
            bg, fg = _TONES["UNDER" if yr["net"] >= 0 else "OVER"]
            text = _signed(yr["net"])
        year_strip.append({"abbr": str(yr["y"]), "text": text, "bg": bg,
                           "fg": fg, "current": yr["y"] == year,
                           "net": yr.get("net")})
    walked = det.get("months_walked") or counted
    return {"label": str(year), "year": year, "status_text": "final" if final
            else f"{counted} of 12 months",
            "verdict_tone": tone, "headline": headline,
            "tiles": [{"eyebrow": f"Net for {year}",
                       "value": _signed(net) if net is not None else "—",
                       "tone": _tone_of(net), "sub": sentence}, prev_tile],
            "strip": strip,
            "strip_legend": ("Green made money, red lost money · the number "
                             "is income − spending"
                             + (" · grey = income unknown"
                                if unknown_months else "")),
            "variable_line": variable_line, "notes": notes,
            "rows": rows, "year_strip": year_strip, **pie,
            "net": net, "kept": kept, "judged": judged, "counted": counted,
            "bars_title": "Yearly budget",
            "bars_intro": (f"Annual totals over the {budgeted} budgeted month"
                           f"{'s' if budgeted != 1 else ''}: the bills in one "
                           "bar, then the variable budget as you split it, "
                           "then any bill pinned to Today. Green under, red over."),
            "pie_title": f"Spending by category — {year}",
            "pie_sub": f"{pie['cat_total_text']} out over {walked} month"
                       f"{'s' if walked != 1 else ''} · the bills under "
                       f"their categories."}


def build_yearly(conn, today: dt.date) -> tuple[str, str, str]:
    """The year in review: the previous CALENDAR year (sent on Jan 1),
    read the way the monthly is — the Year lens's own payload with the
    mail's detail, so the email and the page cannot disagree."""
    year = today.year - 1
    d = lenses.year_summary(conn, year, today=today, with_detail=True)
    ctx = year_mail_context(d, year)
    subject = f"{year} in review"
    if ctx["net"] is not None:
        subject += f": net {_signed(ctx['net'])}"
    if ctx["judged"]:
        subject += f" · on or under budget {ctx['kept']} of {ctx['judged']} months"
    html = _render(f"{year} — the year in review", ctx)
    return subject, _plain(ctx), html
