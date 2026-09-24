import os
"""Single source for the Today view — context builder + email renderer.

`templates/today_body.html` IS both the Today page and the daily email's
HTML body — edit the template once, both surfaces change.
`render_email()` renders the template standalone and
`_inline()` bakes the identical dark-theme styles into style= attributes,
because mail clients honor no <style> block, no <script>, and no CSS vars.
The interactive SVG chart is the one deliberate divergence.

This module must not import web.report (report imports it) — the gathered
status dict is passed in.
"""

import calendar
import datetime as dt
import html as _htmlmod
import math
import pathlib
import re
from html.parser import HTMLParser
from pathlib import Path

from jinja2 import (ChoiceLoader, Environment, FileSystemLoader,
                    select_autoescape)

from .. import ext as _ext

from ..engine.compat import as_date
from ..envnum import env_flag
from . import charts

HERE = Path(__file__).parent


def fmt_date(v) -> str:
    """Any date/'YYYY-MM-DD' → MM/DD/YY. Empty/None → ''. The single date
    format for dashboard + email, exposed as the `d` Jinja filter."""
    if not v:
        return ""
    s = str(v)[:10]
    parts = s.split("-")
    if len(parts) == 3 and len(parts[0]) == 4:
        y, m, day = parts
        return f"{m}/{day}/{y[2:]}"
    return s


_env = Environment(loader=ChoiceLoader(
    [FileSystemLoader(str(HERE / "templates"))]
    + [FileSystemLoader(d) for d in _ext.template_dirs()]),
                   autoescape=select_autoescape(["html"]))
_env.globals.update(charts=charts, money=charts._money)
# hosted() — templates show hosted-only chrome (the login page's
# "create an account" link); a callable so env flips need no restart
import os as _os  # noqa: E402
_env.globals.update(hosted=lambda: env_flag("OIKONOME_HOSTED"))
# signup_open() — hosted self-service signup (OIKONOME_OPEN_SIGNUP); the
# login page offers "Create an account" only then.
_env.globals.update(signup_open=lambda: env_flag("OIKONOME_OPEN_SIGNUP"))
# site_url() / privacy_url() — the operator's own site and privacy policy,
# linked from the sign-in page only when they publish them.
_env.globals.update(
    site_url=lambda: (os.environ.get("OIKONOME_SITE_URL") or "").strip() or None,
    privacy_url=lambda: (os.environ.get("OIKONOME_PRIVACY_URL") or "").strip() or None)
# tier_name() — templates render the DISPLAY label an installed add-on
# supplies for a tier key. Imported lazily so this module keeps working
# when no add-on is installed.
#
# An UNKNOWN key renders EMPTY, never the key itself: falling back to the
# raw value would print an internal placeholder into the console as
# though it were a label. Returning empty lets each caller choose its own
# fallback.


def _tier_name(tier):
    from .. import ext
    return ext.gate.tier_name(tier)


_env.globals.update(tier_name=_tier_name)
_env.filters["d"] = fmt_date


def _moneyc(v) -> str:
    """Cents formatter for transaction rows: cents on transaction and
    recurring surfaces only; aggregates stay whole-dollar."""
    return ("-$" if v < -0.005 else "$") + f"{abs(v):,.2f}"


_env.globals.update(moneyc=_moneyc)


def _asset_ver() -> str:
    """Cache-bust token for /static asset URLs. The origin already sends
    no-cache on /static, but a shared CDN in front of a hosted instance can
    still answer a browser's revalidation from its own edge cache and serve
    week-old bytes, a stale script after a deploy. A versioned URL is a
    fresh cache key end to end
    (browser AND edge). Keyed on the build version (changes every deploy);
    off-deploy (dev) it falls back to the file mtime so local edits bust
    too."""
    v = _os.environ.get("OIKONOME_VERSION")
    if v:
        return v
    try:
        return str(int((HERE / "static" / "pages.js").stat().st_mtime))
    except OSError:
        return "dev"


_env.globals.update(asset_ver=_asset_ver)

# money-map palette: the dim shades are the SPA's opacity blends
# (green .22 / amber .45 over the card #1a212b) baked to solid hex —
# email clients can't do opacity on table cells. The SPA's striped
# past-the-budget segment flattens to its darker stripe color here (no
# CSS gradients in mail clients).
_MAP_GREEN_DIM = "#294239"
_MAP_AMBER_DIM = "#77632a"
_MAP_RED_DEEP = "#8f382f"


def _plan_rows(st: dict) -> list[dict]:
    """planRows() (webapp/src/components/PlanBars.tsx) ported: the plan's
    rows in the wizard's order — categories (Food first, then carve-outs),
    Everything else, Bills, Savings, Excess. Keep the two in lockstep."""
    rows = []
    food, other = st["buckets"]["food"], st["buckets"]["other"]
    kids = food.get("children") or []
    rows.append({"label": "Food", "judged": True, "to": _ledger(st, "food"),
                 "actual": food["display_actual"] if kids else food["actual"],
                 "expected": (food["display_expected"] if kids
                              else food["expected"]),
                 "budget": (food["remaining_budget"] if kids
                            else food["month_budget"])})
    for c in kids:
        rows.append({"label": c["name"], "actual": c["actual"],
                     "expected": c["expected"], "budget": c["month_budget"],
                     "judged": True, "to": _ledger(st, c["name"])})
    okids = other.get("children") or []
    for c in okids:
        rows.append({"label": c["name"], "actual": c["actual"],
                     "expected": c["expected"], "budget": c["month_budget"],
                     "judged": True, "to": _ledger(st, c["name"])})
    row = {"label": "Everything else", "judged": True, "to": _ledger(st, "other"),
           "actual": other["display_actual"] if okids else other["actual"],
           "expected": (other["display_expected"] if okids
                        else other["expected"]),
           "budget": (other["remaining_budget"] if okids
                      else other["month_budget"])}
    if okids:
        row["caption"] = "unallocated — spending outside the categories"
    rows.append(row)
    fixed = st["buckets"]["fixed"]
    posted = fixed["actual"]
    pend = fixed.get("unpaid_due") or 0
    # posted/awaiting/still-due goes on the row's
    # RIGHT side via _row_delta — no caption, and no "not counted in the
    # verdict" tail anywhere (the pace carets already say which rows count)
    rows.append({"label": "Bills", "judged": False, "actual": posted,
                 "expected": fixed["expected"],
                 "budget": fixed["month_budget"], "pending": pend})
    goal = next((g for g in st.get("savings_goals") or []
                 if g["name"] == "Savings"), None)
    if goal and ((goal["monthly_plan"] or 0) > 0
                 or (goal.get("rate_90d") or 0) > 0):
        plan_only = goal.get("rate_90d") is None
        rows.append({"label": "Savings / Investment", "judged": False,
                     "actual": goal.get("rate_90d") or 0,
                     "expected": goal["monthly_plan"],
                     "budget": goal["monthly_plan"],
                     "actual_label": "—" if plan_only else None,
                     "caption": None if plan_only else "transferred vs plan"})
    excess = st.get("plan_surplus")
    if excess is not None and excess > 0.005:
        rows.append({"label": "Excess cash", "judged": False, "actual": 0,
                     "actual_label": "—", "expected": excess,
                     "budget": excess})
    return rows


def _map_cells(row: dict) -> list[dict]:
    """One MoneyMap bar as table cells: [{'w': %-of-the-row's-own-extent,
    'bg': color}]. Mirrors Track in MoneyMap.tsx: every bar is a gauge of
    ITS OWN budget; overspend sits past a 2px
    ink budget-divider cell ({'budget': True}) in the deep-red overflow
    shade. Pace renders as the product's post tick: _insert_pace splits
    these cells to drop a 2px ink cell in the bar, and the template's
    stub rows above/below (driven by 'pace_pct', see _prep_row) complete
    the tick's overshoot."""
    unbudgeted = row["budget"] <= 0.005 and row["actual"] > 0.005
    if unbudgeted:
        return [{"w": 100.0, "bg": _MAP_AMBER_DIM}]
    pending = row.get("pending") or 0
    extent = max(row["budget"], row["actual"] + pending, 1)
    isover = (row["actual"] > row["expected"] + 0.5 if row["judged"]
              else row["actual"] > row["budget"] + 0.5)
    spent = min(row["actual"], row["budget"])
    pend = min(pending, max(row["budget"] - spent, 0))
    over = max(row["actual"] - row["budget"], 0)
    remaining = max(row["budget"] - spent - pend, 0)
    cells = []
    for width, bg in [(spent, "var(--red)" if isover else "var(--green)"),
                      (pend, "var(--amber)"),
                      (remaining, _MAP_GREEN_DIM)]:
        if width > 0.005:
            cells.append({"w": 100 * width / extent, "bg": bg})
    if over > 0.005:
        cells.append({"budget": True})
        cells.append({"w": 100 * over / extent, "bg": _MAP_RED_DEEP})
    return cells


def _insert_pace(cells: list[dict], pace_pct: float) -> list[dict]:
    """Split the segment run at today's pace and drop in the post tick's
    in-bar 2px ink cell ({'pace': True}). Mail clients have no absolute
    positioning, so the web's .bartick overlay becomes three stacked
    table rows: stub above, this in-bar cell, stub below."""
    out: list[dict] = []
    acc = 0.0
    placed = False
    for c in cells:
        w = c.get("w")
        if w is None:
            # a widthless cell (the budget divider): if today sits right
            # at it, the tick goes BEFORE it — pace never exceeds budget
            if not placed and acc >= pace_pct - 0.2:
                out.append({"pace": True})
                placed = True
            out.append(c)
            continue
        if placed:
            out.append(c)
            continue
        if acc + w > pace_pct + 0.2:
            head = pace_pct - acc
            if head > 0.2:
                out.append(dict(c, w=head))
            out.append({"pace": True})
            tail = w - max(head, 0.0)
            if tail > 0.2:
                out.append(dict(c, w=tail))
            placed = True
        else:
            out.append(c)
        acc += w
    if not placed:
        out.append({"pace": True})
    return out


def _row_delta(row: dict) -> dict | None:
    """Right-side status text (rowDelta in MoneyMap.tsx) — over/under is
    never color-alone."""
    money = charts._money
    if row["budget"] <= 0.005 or row.get("actual_label") == "—":
        return None
    pend = row.get("pending") or 0
    left = row["budget"] - row["actual"] - pend
    if row["actual"] > row["budget"] + 0.5:
        return {"text": f"over by {money(row['actual'] - row['budget'])}",
                "color": "var(--red)"}
    # a judged row past its pace but inside its budget: name it, in red —
    # there is no legend, so the red fill alone would not say it
    if row["judged"] and row["actual"] > row["expected"] + 0.5:
        return {"text": (f"{money(row['actual'] - row['expected'])} "
                         "ahead of pace"),
                "color": "var(--red)"}
    if row["judged"]:
        return {"text": f"{money(left)} left", "color": "var(--green)"}
    if row["label"] == "Bills" and (left > 0.5 or pend > 0.5):
        return {"text": ((f"{money(pend)} awaiting · " if pend > 0.5 else "")
                         + f"{money(max(left, 0))} still due"),
                "color": "var(--mut)"}
    return None


def _ledger(st: dict, bucket: str) -> str:
    """The in-app path a bucket label opens: the rows the month verdict
    counts in that bucket, for the month on view (the SPA's Today.tsx
    tileTo / PlanBars.planRows build the same path — keep the three in
    lockstep).

    `bucket` is 'food', 'other', or a custom carve-out's configured name.
    It is NOT a category: Food is a category plus two override strings,
    minus every bill-shaped row, plus envelope overflow, and a carve-out
    is a plan label matched by merchant rules as well as category rules.
    A `cat=` link would land on rows the tile never counted — a warehouse-
    club grocery month opens an empty ledger under a $300 Food tile — so the
    ledger asks the engine for the same rows the number came from
    (budget.bucket_ledger, behind ?bucket=).
    """
    from urllib.parse import quote
    d = st["today"]
    # as_of: the day the number was read on. A past day of the month shows
    # that day's figures, so its listing must stop there too, not at today
    return (f"/transactions?bucket={quote(bucket)}"
            f"&y={d.year}&m={d.month}&as_of={d.isoformat()}")


def _prep_row(r: dict, indent: bool) -> dict:
    extent = max(r["budget"], r["actual"] + (r.get("pending") or 0), 1)
    pace = (100 * r["expected"] / extent
            if r["judged"] and r["expected"] > 0
            and r["expected"] < extent - 0.5 else None)
    cells = _map_cells(r)
    if pace is not None:
        cells = _insert_pace(cells, pace)
    return dict(r, cells=cells, indent=indent, pace_pct=pace,
                delta=_row_delta(r),
                unbudgeted=r["budget"] <= 0.005 and r["actual"] > 0.005)


def money_map(st: dict) -> dict:
    """The money map for the shared template: hero bar + rows,
    each with precomputed email-safe cells. Mirrors MoneyMap.tsx exactly —
    when the SPA map changes, change this with it.

    The hero is the VERDICT's own aggregation (variable only) and renders
    inside the hero card, not the map; rows with nothing measurable
    (plan-only savings, excess) fold into one muted footer sentence; the
    legend flags say whether a state it explains is actually on screen."""
    money = charts._money
    rows = _plan_rows(st)
    measurable = [r for r in rows if r.get("actual_label") != "—"]
    folded = [r for r in rows if r.get("actual_label") == "—"]
    judged = [r for r in measurable if r["judged"]]
    unjudged = [r for r in measurable if not r["judged"]]
    total = None
    if len(judged) > 1:
        total = {"label": "Variable spending", "judged": True,
                 "actual": sum(r["actual"] for r in judged),
                 "expected": sum(r["expected"] for r in judged),
                 "budget": sum(r["budget"] for r in judged)}
    var_budget = (st["buckets"]["food"]["month_budget"]
                  + st["buckets"]["other"]["month_budget"])
    # hero bar for the verdict card: the SAME number the pill judges —
    # expected = actual − the engine's own variance, one source
    hero = _prep_row({"label": "", "judged": True,
                      "actual": st["variable_actual"],
                      "expected": (st["variable_actual"]
                                   - (st.get("variance") or 0)),
                      "budget": var_budget}, False)
    ordered = [_prep_row(total, False)] if total else []
    ordered += [_prep_row(r, bool(total)) for r in judged]
    ordered += [_prep_row(r, False) for r in unjudged]
    shown = ([total] if total else []) + judged + unjudged
    folded_bits = []
    for r in folded:
        if r["label"] == "Excess cash":
            folded_bits.append(f"{money(r['budget'])} excess stays in checking")
        else:
            folded_bits.append(f"{r['label'].lower()} plan "
                               f"{money(r['budget'])}/mo (plan only)")
    return {"rows": ordered, "hero": hero, "folded": folded_bits,
            "has_overflow": any(r["actual"] > r["budget"] + 0.5
                                for r in shown),
            "has_pending": any((r.get("pending") or 0) > 0.5
                               for r in shown)}


def recover_days(month_budget: float, actual: float, days_left: int,
                 days_in_month: int) -> int | None:
    """How many no-spend days bring a reduced daily back to its plan.

    A tile whose daily has been reduced (spent ahead of pace) answers "how
    long do I stop spending to get the old number back": the smallest
    n >= 0 with (month_budget - actual) / (days_left - n) >= plan, i.e.
    n = ceil(days_left - remaining / plan). Returns 0 when there is
    nothing to recover (the forward rate already meets the plan) and None
    when the month cannot get there — nothing left to spread, or every
    remaining day would have to be a no-spend day (see
    specs/today-recovery-days.md).
    """
    if days_in_month <= 0 or month_budget <= 0 or days_left <= 0:
        return None
    plan = month_budget / days_in_month
    remaining = month_budget - actual
    if remaining <= 0:
        return None
    # a hair of slack so an exact division (remaining/plan an integer)
    # does not round up on float noise and cost a day it does not need
    n = max(0, math.ceil(days_left - remaining / plan - 1e-9))
    return None if n >= days_left else n


def allowances(st: dict) -> tuple[dict, list, list]:
    """The spend-at-most card's numbers: per-category /day rate PLUS the
    today-ledger. The /day rate amortizes today's spending
    over the whole rest of the month, so spending money barely moves it and
    never answers the question the card exists for — can I spend more TODAY?
    So each category also carries: the allowance FIXED at the start of the
    day (remaining at midnight ÷ days left), today's spend deducted from it
    in full, and the signed remainder. Negative = over for today; tomorrow's
    allowance recomputes and absorbs it.

    Returns (allow{food,other}, allow_kids[], allow_tiles[(label, entry)]).
    One function because THREE surfaces render it — SPA, email HTML, email
    plain text — and a second derivation would let them drift.
    """
    days_left = max(1, st["days_in_month"] - st["today"].day + 1)

    def _allow(month_budget, actual, rows):
        spent = sum(r["amount"] for r in rows
                    if as_date(r["date"]) == st["today"])
        start_remaining = max(0.0, month_budget - (actual - spent))
        allowance = start_remaining / days_left
        rate = max(0.0, (month_budget - actual) / days_left)
        plan = month_budget / st["days_in_month"] if st["days_in_month"] else 0.0
        # the plan-vs-now daily, said IN the tile: what
        # this category's daily budget was, and what the month's spending
        # has moved it to for every remaining day. Composed here once so
        # the page, the email HTML and the plain text say the same words.
        note = None
        tone = None
        p_, r_ = round(plan), round(rate)
        if month_budget > 0 and p_ != r_:
            note = (f"daily {'reduced' if r_ < p_ else 'raised'} "
                    f"from ${p_:,.0f} to ${r_:,.0f}")
            # a reduced daily is bad news, a raised one good — carried as
            # data so the renderers color the note without reading its words
            tone = "neg" if r_ < p_ else "pos"
        # the question a reduced daily raises — how many no-spend days
        # bring it back — answered in the tile, in one place, so the page,
        # the email HTML and the plain text say the same number
        recover = None
        recover_note = None
        if tone == "neg":
            recover = recover_days(month_budget, actual, days_left,
                                   st["days_in_month"])
            if recover is None:
                recover_note = f"can't get back to ${p_:,.0f} this month"
            elif recover == 1:
                recover_note = (f"one no-spend day and the daily is back "
                                f"to ${p_:,.0f}")
            elif recover > 0:
                recover_note = (f"skip {recover} days of spending and the "
                                f"daily is back to ${p_:,.0f}")
        return {"rate": rate,
                "plan": plan,
                "spent_today": round(spent, 2),
                "today_allowance": round(allowance, 2),
                "left_today": round(allowance - spent, 2),
                # the tile meter is MONTH-scoped: full
                # track = the month's budget, fill = month-to-date spend,
                # with the pace post tick at today — same read as the
                # money map bars and the pinned-bill cards
                "month_budget": round(month_budget, 2),
                "month_spent": round(actual, 2),
                "daily_note": note,
                "daily_note_tone": tone,
                "recover_days": recover,
                "recover_note": recover_note}

    allow = {}
    allow_kids = []
    for k in ("food", "other"):
        b = st["buckets"][k]
        kids = b.get("children") or []
        if kids:
            # the children get their own tiles, so the parent's tile uses
            # the carved (display) numbers — otherwise a carve-out's money
            # counts twice across the card
            kid_ids = {id(r) for c in kids for r in c["rows"]}
            own_rows = [r for r in b["rows"] if id(r) not in kid_ids]
            allow[k] = _allow(b["remaining_budget"], b["display_actual"],
                              own_rows)
            for c in kids:
                allow_kids.append({"name": c["name"],
                                   **_allow(c["month_budget"], c["actual"],
                                            c["rows"])})
        else:
            allow[k] = _allow(b["month_budget"], b["actual"], b["rows"])
    tiles = ([("Food", allow["food"]), ("Everything else", allow["other"])]
             + [(k["name"], k) for k in allow_kids])
    # the email's label links: the category's rows for the month
    for label, entry in tiles:
        entry["to"] = _ledger(st, "food" if label == "Food"
                              else "other" if label == "Everything else"
                              else label)
    # the Why's per-bucket lines name the same buckets, so they open the
    # same doors (the web and the app link them from their own tile map)
    for e in (st.get("why") or {}).get("entries") or []:
        e["to"] = _ledger(st, "food" if e["name"] == "Food"
                          else "other" if e["name"] == "Everything else"
                          else e["name"])
    return allow, allow_kids, tiles


def simple_day_meter(allow_tiles) -> tuple[float, float]:
    """(day_spent, day_allow) for the simple face's full-width day bar.

    The bar sits directly under the headline number, which floors each
    bucket at zero (an over bucket contributes $0 — its overspend cannot
    be absorbed by a neighbour's unused allowance). Raw sums would tell a
    different story on the same screen: one bucket $20 over and another
    with $30 untouched would render a bar implying ~$10 left
    while the headline says $30. So spend counts against the bar only up
    to each bucket's own allowance — bar remaining always equals the
    headline — and once every bucket is exhausted the true overshoot is
    added back so the bar's over state (spent > allowance) trips exactly
    when the day as a whole is genuinely over, agreeing with the chips.
    """
    left = sum(max(0.0, a["left_today"]) for _, a in allow_tiles)
    day_allow = sum(a["today_allowance"] for _, a in allow_tiles)
    day_spent = day_allow - left
    if left < 0.005:
        day_spent += sum(max(0.0, -a["left_today"]) for _, a in allow_tiles)
    return round(day_spent, 2), round(day_allow, 2)


def _bill_cards(st: dict) -> list[dict]:
    """Cards for bills pinned to Today (bill setting "pin to Today").
    Envelope pins answer "how much is left in the pool this period"; fixed
    (occurrence) pins answer "how much of this bill is still to go out, and
    when". Sentences are composed HERE, once, because three surfaces render
    them — the SPA, the email HTML and the plain text."""
    cards = []
    today = st["today"]
    # the pace post tick: where "today" sits on the card's own period, so the
    # bar answers ahead-or-behind at a glance (the money map's idiom). A
    # monthly pool paces on the month; an annual pool on the calendar year.
    month_frac = today.day / st["days_in_month"]
    year_days = (dt.date(today.year, 12, 31)
                 - dt.date(today.year, 1, 1)).days + 1
    year_frac = (today.timetuple().tm_yday) / year_days
    for env in st["buckets"]["fixed"].get("envelopes") or []:
        if not env.get("show_today"):
            continue
        pool, pm = env["pool"], env["period_months"]
        period = "month" if pm == 1 else "year"
        over = env["period_left"] <= 0.005
        status = tone = None
        if env["overflow"] > 0.005:
            status = (f"over by ${env['overflow']:,.0f} — "
                      "counted as variable spend")
            tone = "neg"
        cards.append({
            "kind": "envelope", "label": env["payee"],
            "left": round(env["period_left"], 2), "over": over,
            "sub": (f"used ${env['period_used']:,.0f} of ${pool:,.0f} "
                    f"this {period}"),
            "frac": min(1.0, env["period_used"] / pool) if pool > 0 else 1.0,
            "pace_frac": round(month_frac if pm == 1 else year_frac, 4),
            "status": status, "status_tone": tone})
    for p in st.get("pinned_bills") or []:
        remaining = max(0.0, p["planned"] - p["paid"])
        due = (dt.date.fromisoformat(p["next_due"]).strftime("%m/%d")
               if p["next_due"] else None)
        if p["overdue"]:
            status, tone = f"overdue — was due {due}", "neg"
        elif due:
            status, tone = f"due {due}", None
        elif p["paid_count"]:
            status, tone = "all paid", "pos"
        else:
            status = tone = None
        cards.append({
            "kind": "bill", "label": p["payee"],
            "left": round(remaining, 2), "over": bool(p["overdue"]),
            "sub": (f"paid ${p['paid']:,.0f} of ${p['planned']:,.0f} "
                    "this month"),
            "frac": (min(1.0, p["paid"] / p["planned"])
                     if p["planned"] > 0 else 1.0),
            "pace_frac": round(month_frac, 4),
            "status": status, "status_tone": tone})
    return cards


def build_context(st: dict) -> dict:
    """Everything today_body.html needs beyond the gathered status dict —
    shared verbatim by the /today route and the email renderer so the two
    surfaces cannot diverge."""
    days_left = max(1, st["days_in_month"] - st["today"].day + 1)
    allow, allow_kids, allow_tiles = allowances(st)
    bill_cards = _bill_cards(st)
    rw = st.get("runway") or {}
    # an unset manual-account balance reads $0 — headroom computed
    # from it would panic a brand-new files-only user (email included)
    headroom = (rw["checking"] - rw["card_debt"] - rw["due_total"]
                if rw.get("checking") is not None
                and not rw.get("balance_unreliable") else None)
    # MTD by category, Amazon subcategories clustered under one parent
    by_cat: dict[str, float] = {}
    for r in st["rows"]:
        by_cat[r["category"]] = by_cat.get(r["category"], 0.0) + r["amount"]
    amazon = {k: v for k, v in by_cat.items() if k.startswith("Amazon")}
    entries = [[k, v] for k, v in by_cat.items() if k not in amazon]
    if amazon:
        entries.append(["Amazon", sum(amazon.values())])
    entries.sort(key=lambda x: -x[1])
    amazon_subs = sorted(([k.replace("Amazon - ", ""), v] for k, v in amazon.items()),
                         key=lambda x: -x[1])
    # recent transactions split variable vs fixed
    fixed_ids = {r["txn_id"] for r in st["buckets"]["fixed"]["rows"]}
    recent = sorted(st["yesterday_rows"], key=lambda r: (r["date"], -r["amount"]),
                    reverse=True)
    # ---- the hero's composed sentences: ONE source for the SPA, the
    # email HTML and the plain text.
    # pace_line sits beside the verdict word; each tile carries its own
    # plan-vs-now daily note (composed in allowances above), which is
    # where the get-back-on-track framing lives.
    over_ = st["verdict"] == "OVER BUDGET"
    under_ = st["verdict"] == "UNDER BUDGET"
    d_ = st["today"].day
    if over_:
        pace_line = f"${st['variance']:,.0f} ahead of pace on day {d_}"
    elif under_:
        pace_line = f"${-st['variance']:,.0f} under pace on day {d_}"
    else:
        pace_line = f"on pace · day {d_} of {st['days_in_month']}"
    # the Why's chips: the top offenders ACROSS the over buckets, one
    # scannable row (the per-bucket sentences live behind an expander)
    agg: dict[str, dict] = {}
    for e in (st.get("why") or {}).get("entries") or []:
        if e["tone"] != "over":
            continue
        for t in e["top"]:
            c = agg.setdefault(t["payee"],
                               {"payee": t["payee"], "amount": 0.0,
                                "count": 0})
            c["amount"] += t["amount"]
            c["count"] += t["count"]
    why_chips = sorted(agg.values(), key=lambda c: -c["amount"])[:5]
    for c in why_chips:
        c["amount"] = round(c["amount"], 2)

    # the sweep-goal sentence, composed HERE once (the bill_cards idiom)
    # because three surfaces render it — the SPA, the email HTML and the
    # plain text — and a second derivation is how wording drifts. A month
    # already swept says so; otherwise the plan cap and what the month
    # has made available so far.
    sweep_line = None
    if (st.get("sweep_plan") or 0) > 0.005:
        plan_ = st["sweep_plan"]
        posted_ = st.get("sweep_posted") or 0
        avail_ = st.get("sweep_available")
        if posted_ >= plan_ - 0.005:
            sweep_line = f"swept ${posted_:,.0f} to savings this month"
        elif avail_ is None:
            sweep_line = f"sweep up to ${plan_:,.0f} at month end"
        else:
            sweep_line = (f"sweep up to ${plan_:,.0f} — "
                          f"${avail_:,.0f} available so far this month")

    # the SIMPLE hero face (today_view setting): ONE number — what is
    # still spendable today across every variable bucket — and a chip per
    # bucket. Composed here, whole-dollar like every aggregate, so the
    # page, the email HTML and the plain text render identical strings.
    # A bucket already over contributes $0 to the number (its allowance
    # cannot be borrowed by another bucket) and says so in its chip.
    simple_left = sum(max(0.0, a["left_today"]) for _, a in allow_tiles)
    simple_chips = []
    for label_, a_ in allow_tiles:
        if a_["left_today"] < -0.5:
            simple_chips.append({"text": f"{label_} over by "
                                 f"${-a_['left_today']:,.0f}",
                                 "tone": "neg"})
        else:
            simple_chips.append({"text": f"{label_} "
                                 f"${max(0.0, a_['left_today']):,.0f} today",
                                 "tone": ""})

    # top-pane bars are DAY-scoped: today's spend against
    # today's allowance, totalled across the buckets for the simple face's
    # full-width bar. The month gauges live in the Monthly budget card.
    day_spent, day_allow = simple_day_meter(allow_tiles)

    day = {
        "days_left": days_left, "allow": allow, "allow_kids": allow_kids,
        "allow_tiles": allow_tiles, "bill_cards": bill_cards,
        "day_spent": day_spent, "day_allow": day_allow,
        "simple": {"left_today": simple_left, "chips": simple_chips},
        "pace_line": pace_line, "sweep_line": sweep_line,
        "why_chips": why_chips if over_ else [],
        "headroom": headroom,
        "mtd": entries, "amazon_subs": amazon_subs, "amazon_total": sum(amazon.values()),
        "recent_var": [r for r in recent if r["txn_id"] not in fixed_ids],
        "recent_fix": [r for r in recent if r["txn_id"] in fixed_ids],
        "summaries": st.get("amazon_summaries") or {},
    }
    fc = st.get("forecast")
    # while imported accounts still sit at their $0 default,
    # every forecast number is seeded from a lie — suppress the whole
    # forecast (SPA card, email card, cash notice) rather than present
    # $0 as truth. The verdict card's set-your-balance nudge remains.
    if (st.get("runway") or {}).get("balance_unreliable"):
        fc = None
    fc_chart = ""
    fc_rows = []
    fc_email_chart = None
    if fc:
        try:
            # two user-facing card scenarios. Autopay statement is
            # the primary series (the realistic case); pay-all-now is the
            # comparison. Full-balance-at-due-dates stays engine-internal.
            stmt = fc["pace_stmt"]
            series = stmt["series"]
            negd = stmt.get("negative_date")
            mx = next((i for i, (_, v) in enumerate(series) if v < 0), None)
            mlab = ("runs out " + fmt_date(negd) if negd else "")
            # dated card autopays as lines, the same marks the web and the
            # app draw (the series is one point per day from today, so a due
            # date is found by its own index; a date off the end is dropped
            # rather than clamped, which would claim a payment lands there)
            day_ix = {d: i for i, (d, _) in enumerate(series)}
            ev_at: dict[int, list[str]] = {}
            for d0, _amt, nm in fc.get("card_autopay", []):
                if d0 in day_ix:
                    ev_at.setdefault(day_ix[d0], []).append(nm)
            ev = [(i, f"{len(ns)} autopays" if len(ns) > 2
                   else " + ".join(ns) + " autopay")
                  for i, ns in sorted(ev_at.items())]
            # static render so the zero-line + critical-date marker survive
            fc_chart = charts.line(series, h=220, color=charts.PRIMARY,
                                   interactive=False, hover=True, zero_line=True,
                                   mark_x=mx, mark_label=mlab,
                                   second=fc["pace_now"]["series"],
                                   events=ev,
                                   labels=("autopay statement balance",
                                           "pay all cards now"))
            # the email's chart: the same picture as a PNG (mail clients
            # strip SVG). It rides in the HTML as a data: URI and report.send
            # moves it into an inline image part, so every sender of this
            # HTML gets the part without knowing about it.
            from . import emailchart
            png = emailchart.render(
                series, second=fc["pace_now"]["series"], events=ev,
                mark_x=mx, mark_label=mlab,
                labels=("autopay statement balance", "pay all cards now"))
            fc_email_chart = _data_uri(png) if png else None
            # event list: the current month's bills/paychecks/card payoffs,
            # PLUS paid fixed bills as ✓ rows and a >=14-day forward horizon.
            # Card payoffs are the autopay scenario's ('X autopay'); each
            # balance column is its own scenario's end-of-day position.
            bal_now = dict(fc["pace_now"]["series"])
            bal_stmt = dict(series)
            merged = sorted(fc["events"] + fc.get("card_events_stmt", []),
                            key=lambda e: e[0])
            today_ = st["today"]
            month_end = today_.replace(
                day=calendar.monthrange(today_.year, today_.month)[1])
            horizon = max(month_end, today_ + dt.timedelta(days=14)).isoformat()
            fc_rows = [(d0, a, p, bal_now.get(d0), bal_stmt.get(d0))
                       for d0, a, p in merged if d0 <= horizon]
            paid = sorted(
                (r["date"].isoformat(), -r["amount"], f'{r["payee"]} — paid ✓',
                 None, None)
                for r in st["buckets"]["fixed"].get("occ_rows", []))
            fc_rows = list(paid) + fc_rows
        except Exception:
            fc_chart, fc_rows, fc_email_chart = "", [], None
    # The card scenarios differ only in payment TIMING — after the last card
    # is paid the balance lines converge, and a trough past that point is
    # identical in both. Duplicate "low" tiles read as a bug, so the template
    # collapses them and shows what still differs: when each strategy first
    # goes negative. (Mirrors the SPA Today page.)
    fc_lows: list[dict] = []
    fc_lows_converged = False
    if fc:
        fc_lows = [
            {"label": "pay all cards now", "short": "cards now",
             **{k: fc["pace_now"][k] for k in ("min", "min_date", "negative_date")}},
            {"label": "autopay statement", "short": "autopay",
             **{k: fc["pace_stmt"][k]
                for k in ("min", "min_date", "negative_date")}},
        ]
        fc_lows_converged = all(
            l["min"] == fc_lows[0]["min"]
            and l["min_date"] == fc_lows[0]["min_date"] for l in fc_lows)

    # cash: quiet until it matters — three states, computed here
    # because the template can't do date math. OK = no dict (silence).
    # Mirrors the SPA Today page and report.py's plain text.
    cash = None
    p0 = fc.get("pace_stmt") if fc else None
    headroom = day["headroom"]
    income = st.get("income")
    paycheckish = (income / 2) if income else 1000
    neg_in = None
    if p0 and p0.get("negative_date"):
        neg_in = (dt.date.fromisoformat(p0["negative_date"])
                  - st["today"]).days
    # the shortfall quoted beside negative_date must be the balance ON that
    # day — `min` can be a far deeper trough weeks later, and mixing the two
    # would pair one day's date with another day's depth
    first_neg = p0.get("first_neg_amount") if p0 else None
    slow_to = None
    if first_neg is not None and first_neg < 0 and neg_in and neg_in > 0:
        slow_to = max(0.0, (p0.get("rate") or 0) + first_neg / neg_in)
    if ((headroom is not None and headroom < 0)
            or (neg_in is not None and neg_in <= 14)):
        if headroom is not None and headroom < 0:
            cash = {"state": "critical", "kind": "today",
                    "short": -headroom, "slow_to": slow_to}
        else:
            cash = {"state": "critical", "kind": "date",
                    "short": -(first_neg if first_neg is not None
                               else p0["min"]),
                    "by": p0.get("negative_date"),
                    "slow_to": slow_to}
    elif ((headroom is not None and headroom < paycheckish)
          or (p0 and (p0["min"] < 0 or neg_in is not None))):
        if p0 and (p0["min"] < 0 or neg_in is not None):
            cash = {"state": "tight", "kind": "forecast",
                    "low": p0["min"], "low_date": p0["min_date"]}
        else:
            cash = {"state": "tight", "kind": "headroom",
                    "headroom": headroom}

    # ---- cash timeline: recent activity flows into
    # the scheduled events across a TODAY divider. Past = the paid ✓ rows
    # (no balance) + day.recent_var (the template merges them); future
    # events show through the forecast low point, the rest is a count.
    today_iso = st["today"].isoformat()
    tl_past, tl_future = [], []
    for r in fc_rows:
        (tl_past if (r[3] is None and r[4] is None and r[0] <= today_iso)
         else tl_future).append(r)
    low_date = fc["pace_stmt"]["min_date"] if fc else None
    tl_pre = ([r for r in tl_future if r[0] <= low_date]
              if low_date else tl_future)
    if not tl_pre:
        tl_pre = tl_future
    tl_rest = len(tl_future) - len(tl_pre)

    # Absolute base for links the EMAIL renders (alerts that point at a page).
    # mailable_base() and not the raw env, for the same reason the brand
    # header uses it: a LAN url is what gets the whole message filtered.
    from .report import mailable_base
    return {"st": st, "day": day, "fc": fc, "fc_chart": fc_chart,
            "fc_rows": fc_rows, "fc_lows": fc_lows,
            "fc_email_chart": fc_email_chart,
            "fc_lows_converged": fc_lows_converged, "cash": cash,
            "tl_past": tl_past, "tl_pre": tl_pre, "tl_rest": tl_rest,
            "app_base": mailable_base(),
            "map": money_map(st)}


# ---- email rendering ------------------------------------------------------
# The dark palette, baked into inline styles for the mail render.
_VARS = {
    "--ink": "#dbe3ea", "--mut": "#8b98a3", "--line": "#2a323d",
    "--bg": "#0f141a", "--card": "#1a212b", "--hover": "#232c38",
    "--green": "#5cb56b", "--amber": "#f0b429", "--red": "#e0695d",
    "--blue": "#4f9bd6",
}
# Element/class styles mirroring the web stylesheet (email clients get no
# <style> block). Element style first, then classes in listed order, then
# the tag's own style= LAST so explicit inline styles keep winning.
_ELEMENT_STYLES = {
    # max-width + word-break (on td below) keep a long payee INSIDE the card
    # on a phone; without them one wide cell widens the whole table past the
    # shell and the row runs off to the right.
    #
    # NOT table-layout:fixed: fixed layout changes what `width:1%` MEANS.
    # Under auto layout it reads as "shrink-wrap this narrow column", which is
    # exactly what the date and amount cells want; under fixed layout it is
    # taken literally — 1% of the table, about 6px against 14px of padding —
    # so their white-space:nowrap content would overflow the cell and paint
    # on top of the payee.
    # The transactions table is the exposed one: it has no <thead>, so under
    # fixed layout its FIRST ROW sizes the columns, and that row is a data
    # row carrying the 1% widths.
    "table": ("width:100%;max-width:100%;border-collapse:collapse;"
              "font-size:14px;"),
    "th": ("text-align:left;padding:5px 7px;border-bottom:1px solid #2a323d;"
           "color:#8b98a3;font-weight:600;font-size:12px;text-transform:uppercase;"
           "letter-spacing:.03em;"),
    "td": ("text-align:left;padding:5px 7px;border-bottom:1px solid #2a323d;"
           "font-size:14px;word-break:break-word;"),
    "h2": "font-size:17px;margin:14px 0 8px;font-weight:700;",
    "p": "margin:6px 0;",
    "code": "font-family:ui-monospace,Menlo,monospace;font-size:12px;",
}
_CLASS_STYLES = {
    "grid": "",            # email is single-column: cards just stack
    "cols2": "", "cols3": "", "cols4": "",
    "card": ("background:#1a212b;border:1px solid #2a323d;border-radius:10px;"
             "padding:16px;margin:0 0 14px;"),
    "kpi": "font-size:32px;font-weight:700;letter-spacing:-.02em;",
    "sm": "font-size:22px;",
    "sub": "color:#8b98a3;font-size:13px;",
    "mut": "color:#8b98a3;",
    "ax": "color:#8b98a3;font-size:11px;",
    "num": "text-align:right;",
    "pos": "color:#5cb56b;", "neg": "color:#e0695d;",
    "pill": ("display:inline-block;padding:2px 9px;border-radius:999px;"
             "font-size:12px;font-weight:700;"),
    "g": "background:rgba(92,181,107,.16);color:#5cb56b;",
    "a": "background:rgba(240,180,41,.16);color:#f0b429;",
    "r": "background:rgba(224,105,93,.16);color:#e0695d;",
    "m": "background:rgba(139,152,163,.16);color:#8b98a3;",
    "note": ("background:rgba(240,180,41,.10);border:1px solid rgba(240,180,41,.28);"
             "color:#e6c874;border-radius:8px;padding:8px 11px;font-size:13px;"),
    "hide-m": "",          # .hide-m subtrees are dropped by the inliner
}
_VOID = {"br", "hr", "img", "input", "meta", "link", "col"}


def _style(css: str) -> str:
    """CSS-var → hex and rem → px (mail clients handle neither reliably)."""
    for var, hexv in _VARS.items():
        css = css.replace(f"var({var})", hexv)
    return re.sub(r"([\d.]+)rem",
                  lambda m: f"{round(float(m.group(1)) * 16)}px", css)


class _Inliner(HTMLParser):
    """Rebuild the rendered body with the stylesheet inlined and email
    hazards removed (<script>, comments, CSS var()/rem tokens).

    MOBILE-FIRST: an email is read on a phone more often than anywhere
    else, and a mail client on a ~380px screen otherwise gets a layout
    designed for 680px:

    - `.grid.colsN` STACKS: side-by-side <td>s would squeeze two 49%
      panes to ~170px each on a phone, so the grid div passes through as a
      plain block and its cards render full-width, one under the other. No
      mail client needs to support anything for stacked divs to work.
    - `display:flex` containers carrying `gap:` would have their tiles jam
      together, because Gmail supports flex but NOT the gap property. The
      gap is stripped and re-applied as right+bottom margins on the direct
      children, which every client understands.
    """

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.out: list[str] = []
        self._skip = 0                     # inside <script>/<style>/<svg>
        self._stack: list[str] = []        # open tags
        self._flex: list[dict] = []        # {depth, gap} of flex-gap parents
        # depth at which a .hide-m subtree started (None = not skipping one).
        self._hide_depth: int | None = None

    def _tag(self, tag, attrs, self_closing=False, extra=""):
        ad = {k: v for k, v in attrs}
        style = _ELEMENT_STYLES.get(tag, "")
        for c in (ad.get("class") or "").split():
            style += _CLASS_STYLES.get(c, "")
        style = _style(style + (ad.get("style") or "")) + extra
        # Gmail ignores flex `gap` — lift it off the container here; the
        # caller re-applies it as margins on the direct children
        self._last_gap = None
        if "display:flex" in style:
            m = re.search(r"(?<![-\w])gap:([\d.]+)px;?", style)
            if m:
                self._last_gap = round(float(m.group(1)))
                style = style.replace(m.group(0), "")
        parts = [tag]
        for k, v in attrs:
            if k in ("style", "class"):
                continue
            parts.append(k if v is None else f'{k}="{_htmlmod.escape(v, quote=True)}"')
        if style:
            parts.append(f'style="{_htmlmod.escape(style, quote=True)}"')
        return "<" + " ".join(parts) + (" /" if self_closing else "") + ">"

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "svg"):
            self._skip += 1
            return
        if self._skip:
            return
        ad = {k: v for k, v in attrs}
        classes = (ad.get("class") or "").split()
        # .hide-m columns are DROPPED from the email, not kept: otherwise
        # the recent-transactions and forecast text runs off to the right,
        # outside the card, on a phone.
        #
        # The web hides these on narrow screens via a media query, which no mail
        # client honours — yet a mail client on a phone is the narrowest viewport
        # this HTML ever gets: five columns, three of them white-space:nowrap,
        # inside a 680px shell on a ~380px screen run off the card.
        #
        # Dropping the element (rather than display:none) is deliberate: a
        # hidden <td> still counts as a column in most mail clients.
        if self._hide_depth is None and "hide-m" in classes:
            self._hide_depth = len(self._stack)
            if tag not in _VOID:
                self._stack.append(tag)
            return
        if self._hide_depth is not None:
            if tag not in _VOID:
                self._stack.append(tag)
            return
        # direct child of a flex-gap container → the stripped gap comes
        # back as margins (right + bottom, so wrapping rows space too)
        extra = ""
        if self._flex and len(self._stack) == self._flex[-1]["depth"]:
            g = self._flex[-1]["gap"]
            extra = f"margin:0 {g}px {g}px 0;"
        self.out.append(self._tag(tag, attrs, self_closing=tag in _VOID,
                                  extra=extra))
        if tag not in _VOID:
            self._stack.append(tag)
            if self._last_gap:
                self._flex.append({"depth": len(self._stack),
                                   "gap": self._last_gap})

    def handle_startendtag(self, tag, attrs):
        if self._hide_depth is not None:
            return
        if not self._skip and tag not in ("script", "style", "svg"):
            self.out.append(self._tag(tag, attrs, self_closing=True))

    def handle_endtag(self, tag):
        if tag in ("script", "style", "svg"):
            self._skip = max(0, self._skip - 1)
            return
        if self._skip or tag in _VOID:
            return
        if self._hide_depth is not None:
            if self._stack:
                self._stack.pop()
            if len(self._stack) == self._hide_depth:
                self._hide_depth = None      # the hidden subtree just closed
            return
        if self._stack:
            self._stack.pop()
        if self._flex and len(self._stack) < self._flex[-1]["depth"]:
            self._flex.pop()               # left the flex-gap container
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self._skip and self._hide_depth is None:
            self.out.append(data)

    def handle_entityref(self, name):
        if not self._skip and self._hide_depth is None:
            self.out.append(f"&{name};")

    def handle_charref(self, name):
        if not self._skip and self._hide_depth is None:
            self.out.append(f"&#{name};")

    def handle_comment(self, data):
        pass


def _inline(body: str) -> str:
    p = _Inliner()
    p.feed(body)
    p.close()
    return "".join(p.out)


def _data_uri(png: bytes) -> str:
    import base64
    return "data:image/png;base64," + base64.b64encode(png).decode()


_MARK: bytes | None = None


def _mark_png() -> bytes:
    global _MARK
    if _MARK is None:
        _MARK = (pathlib.Path(__file__).parent / "static"
                 / "logo-mark3.png").read_bytes()
    return _MARK


def _brand_header() -> str:
    """Clickable brand header for the mail shell: the wordmark, linking to
    this instance's app (login → Today when signed out).

    The mark is static/logo-mark3.png — a straight 2x render of the
    canonical site/icon.svg (re-render only from icon.svg, never by hand) —
    carried inside the message; see the comment on `mark` below.

    Base URL (the <a href> only) follows the same rule as the reset email:
    OIKONOME_BASE_URL only, never a request-derived origin; without one the
    link falls back to relative /app/, same as jobs/link_alert.py's CTA."""
    # mailable_base(), not the raw env (see its docstring). Without a
    # mailable base the header is simply not a link — the
    # email mirrors the Today page, so it loses nothing by being terminal.
    from .report import mailable_base
    base = mailable_base()
    # The mark travels INSIDE the message as an inline image part (data:
    # URI here, turned into a cid: part by report.send). Proton and Gmail
    # also list such a part among the attachments — every non-text part is
    # listed whatever its disposition — and that is accepted in exchange
    # for the mark and the cash chart. A remote src is never the
    # alternative: a fetch on open is an open-tracking pixel, and it breaks
    # for a LAN self-host behind the recipient's image proxy.
    word = ('<span style="color:#4f9bd6;font-size:17px;font-weight:700;'
            'letter-spacing:.3px;vertical-align:middle;">Oikonome</span>')
    # alt="" — the wordmark beside it already says the name
    mark = (f'<img src="{_data_uri(_mark_png())}" alt="" width="28" height="28" '
            'style="vertical-align:middle;margin-right:9px;border:0;">')
    if not base:
        return (f'<div style="margin:0 0 14px;">{mark}{word}</div>')
    return (f'<a href="{base}/app/" '
            'style="text-decoration:none;display:inline-block;margin:0 0 14px;">'
            f'{mark}{word}</a>')


def render_email(st: dict, *, summary: bool = False,
                 ctx: dict | None = None) -> str:
    """The daily email's full HTML: today_body.html (email mode) inlined and
    wrapped in the mail shell. summary=True renders only the verdict pane —
    the hero card with the tiles and pinned bills — for households that
    schedule the short form.

    `ctx` is a prebuilt build_context(st) — the plain-text mirror needs
    the same context, so the email sender builds it once and hands it to
    both renders instead of paying for the whole hero computation twice
    per send."""
    # the email face is the email_schedule.daily.summary choice, NOT the
    # page's today_view toggle — a summary email is the
    # verdict pane, a detail email the full report, whatever the page shows
    body = _env.get_template("today_body.html").render(
        email=True, summary=summary,
        face="summary" if summary else "detail",
        **(ctx if ctx is not None else build_context(st)))
    return (
        '<div style="background:#0f141a;padding:8px;">'
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'max-width:680px;margin:0 auto;padding:16px;color:#dbe3ea;background:#0f141a;'
        'border-radius:12px;font-size:15px;line-height:1.5;">'
        f'{_brand_header()}'
        f'<h2 style="font-size:18px;margin:0 0 12px;">Daily budget — {fmt_date(st["today"])}</h2>'
        f'{_inline(body)}</div></div>')
