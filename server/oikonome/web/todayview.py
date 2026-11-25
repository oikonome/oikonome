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

from ..engine import budget, forecast, merchant_sql
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


def android_test_url() -> str | None:
    """OIKONOME_ANDROID_TEST_URL when it is an https link, else None: where a
    tester gets THIS instance's Android build (a Play internal-testing link,
    an App Tester invite). Unset on an instance with no test build, and the
    sign-in page then shows nothing. Shared by the template and /api/access."""
    u = (os.environ.get("OIKONOME_ANDROID_TEST_URL") or "").strip()
    return u if u.startswith("https://") and len(u) <= 300 else None


_env.globals.update(android_test_url=android_test_url)
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


def _cat_filter(stored):
    from ..engine.categories import display_label
    return display_label(stored)


_env.filters["cat"] = _cat_filter


def _moneyc(v) -> str:
    """Cents formatter for transaction rows: cents on transaction and
    recurring surfaces only; aggregates stay whole-dollar."""
    return ("-$" if v < -0.005 else "$") + f"{abs(v):,.2f}"


_env.globals.update(moneyc=_moneyc)


# The cash tile's line when it has no paycheck figure, keyed by the
# gathered status' `income_due_note` (report.gather decides; nothing here
# re-derives it). The web and mobile Today pages carry the same three
# strings. A missing or unknown key reads as "none".
NO_PAYCHECK_NOTE = {
    "none": "no paycheck set up — add your income under Bills & Income "
            "and this counts it",
    "budgeted": "your income is budgeted, not scheduled — this counts "
                "no paycheck",
    "off": "your income is switched off under Bills & Income — this "
           "counts no paycheck",
}


# What stands in for the cash notice while no paycheck is scheduled. A
# forecast that counts no income always runs short, so "short $X — slow
# spending to $0/day" is a verdict on the setup, not on the household's
# money; the useful sentence is the step that is missing.
PAYCHECK_ASK = {
    "none": "Add your paycheck under Bills & Income to see what's safe "
            "to spend.",
    "budgeted": "Add your paycheck under Bills & Income to see what's safe "
                "to spend.",
    "off": "Your income is switched off under Bills & Income — switch it "
           "on to see what's safe to spend.",
}


def paycheck_ask(st: dict) -> str | None:
    """The ask that replaces the cash notice, or None when a paycheck is
    scheduled (or the read could not tell)."""
    return PAYCHECK_ASK.get(st.get("income_due_note") or "")


def no_paycheck_note(st: dict) -> str:
    return NO_PAYCHECK_NOTE.get(st.get("income_due_note") or "none",
                                NO_PAYCHECK_NOTE["none"])


_env.globals.update(no_paycheck_note=no_paycheck_note)


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


def _pace(st: dict) -> dict:
    """The month's pace window as the status read it (budget.pace_window,
    specs/short-ledger-pacing.md); a status built by hand, without a
    ledger, paces from the 1st."""
    return st.get("pace") or budget.pace_window_for(None, st["today"])


def paced_budget(st: dict, month_budget: float) -> float:
    """What the month may spend of a bucket's budget: its share from the
    pace start to month end — the whole budget unless the ledger began
    inside the month. The tiles hand this out a day at a time and the hero
    adds it up, so every row that says "$X left" measures this, never the
    whole month's budget."""
    return month_budget * _pace(st)["share"]


def _savings_row(st: dict) -> list[dict]:
    """The plan's Savings row: every goal the month's excess subtracts
    (month_status `plan_goals`, whose plans sum to `savings_plan`), so the
    row's plan is the plan line's "saved" figure. Its actual is what those
    goals received THIS month (`saved_this_month`); a goal with no
    destination account has nothing to measure, and with none measurable
    the row carries the plan alone. A status without the month's figure
    falls back to the goal's average over the last 3 months (the pace
    window, savings.pace_since) and says so in the caption.
    A status built by hand without `plan_goals` reads the Savings goal
    alone."""
    from ..engine.savings import is_plan_savings_goal
    progress = st.get("savings_goals") or []
    if st.get("plan_goals") is not None:
        names = {str(g["name"]) for g in st["plan_goals"]}
        counted = [g for g in progress if str(g["name"]) in names]
        plan = float(st.get("savings_plan") or 0)
    else:
        counted = [g for g in progress if is_plan_savings_goal(g)][:1]
        plan = sum(float(g.get("monthly_plan") or 0) for g in counted)
    measured = [g for g in counted if g.get("rate_90d") is not None]
    monthly = all(g.get("saved_this_month") is not None for g in measured)
    if monthly:
        actual = sum(float(g["saved_this_month"]) for g in measured)
        caption = "transferred vs plan"
    else:
        actual = sum(float(g["rate_90d"]) for g in measured)
        caption = "avg / mo, last 3 months"
    if not measured:
        actual, caption = 0.0, None
    if plan <= 0.005 and actual <= 0.005:
        return []
    return [{"label": "Savings / Investment", "judged": False,
             "actual": round(actual, 2), "expected": plan, "budget": plan,
             "actual_label": None if measured else "—",
             "caption": caption}]


def plan_total(rows: list[dict], basis: str = "budget") -> dict:
    """The "Variable spending" bar over the plan's judged rows: each
    figure is the sum of its rows, so the bar and the rows under it can
    never disagree. `basis` is row_status's."""
    judged = [r for r in rows if r["judged"]
              and r.get("actual_label") != "—"]
    out = {"label": "Variable spending", "judged": True,
           "actual": round(sum(r["actual"] for r in judged), 2),
           "expected": round(sum(r["expected"] for r in judged), 2),
           "budget": round(sum(r["budget"] for r in judged), 2)}
    out["status"] = row_status(out, basis)
    return out


def today_map(st: dict) -> tuple[list[dict], dict]:
    """Today's money-map rows and their "Variable spending" row, each
    with its status, on the PACE basis: a judged row says how far it is
    from where it should be by today, as the headline does
    (specs/today-figures.md). The rows' whole dollars are apportioned to
    the Variable spending row's (reporting.shown_dollars), so the figures
    printed beside the rows add up to the one printed beside their total,
    which is the verdict's own variance. Bills and Savings keep their
    budget-basis words. The page, the phone and both emails print these."""
    from ..engine.reporting import shown_dollars
    from . import bills_breakdown
    rows = _plan_rows(st)
    total = plan_total(rows, "pace")
    judged = [r for r in rows if r["judged"]
              and r.get("actual_label") != "—"]
    shown = shown_dollars([r["actual"] - r["expected"] for r in judged],
                          total["actual"] - total["expected"])
    by_id = {id(r): d for r, d in zip(judged, shown)}
    bd = st.get("bills_breakdown")
    for r in rows:
        if r["label"] == "Bills" and not r["judged"]:
            # the running month's bills (bills_breakdown): posted / total,
            # the still-due words, a caption naming posted and the plan,
            # and the table the row opens. A past day has no breakdown
            # (still due is a now-fact): its row prints its figures and no
            # words or caption from another reading of them.
            if bd is not None:
                bills_breakdown.apply_to_row(r, bd)
            else:
                r["status"] = r["caption"] = None
        elif id(r) in by_id:
            r["status"] = _pace_status(r, by_id[id(r)])
        else:
            r["status"] = row_status(r)
    without_empty_bills(rows, bd, st["buckets"]["fixed"])
    with_envelopes(rows, st.get("envelopes_breakdown"),
                   lambda k: _ledger(st, k))
    return rows, total


def without_empty_bills(rows: list[dict], bd: dict | None,
                        fixed: dict) -> None:
    """Drop the Bills row, in place, from a period with no due-dated bill
    (bills_breakdown.nothing_dated). A row with no breakdown — a past
    day, which reads no schedule, or parts that could not be read —
    stays as it prints: nothing says the period had no bill."""
    from . import bills_breakdown
    if bills_breakdown.nothing_dated(bd):
        rows[:] = [r for r in rows
                   if not (r["label"] == "Bills" and not r["judged"])]


def with_envelopes(rows: list[dict], bd: dict | None, ledger) -> None:
    """The Envelopes row (envelope_breakdown), in place, directly under
    Bills — Today's, a Month lens's or a Year lens's; nothing when the
    period has no pool (specs/today-figures.md, "Envelopes are their own
    row")."""
    from . import envelope_breakdown
    if not envelope_breakdown.has_row(bd):
        return
    # under Bills; with no Bills row (no dated bill), where Bills would
    # be: after the spending rows, before Savings and Excess
    at = next((i + 1 for i, r in enumerate(rows)
               if r["label"] == "Bills" and not r["judged"]), None)
    if at is None:
        at = max((i + 1 for i, r in enumerate(rows) if r["judged"]),
                 default=0)
    rows.insert(at, envelope_breakdown.row(bd, to=ledger("envelopes")))


# How far this month's bill schedule may sit from the evened average
# before the plan line calls the month light or heavy.
BILLS_WEIGHT_MIN = 100.0


def bills_weight(st: dict) -> str | None:
    """Beside the plan line's bills, which are the EVENED monthly average,
    not this month's: when THIS month's schedule sits well away from that
    average, the words say the figure is evened out and give this month's
    own bills — "evened out; this month lighter: $940" — in the words the
    page, the app and both emails print. Read without them, the average
    beside "bills" was taken for this month's bills. No difference is
    printed: it would be a third figure to reconcile with the two. None
    within BILLS_WEIGHT_MIN, or without the figures."""
    if st.get("scheduled_month") is None or st.get("avg_bills") is None:
        return None
    weight = st["scheduled_month"] - st["avg_bills"]
    this_month = charts._money(st["scheduled_month"])
    if weight > BILLS_WEIGHT_MIN:
        return f"evened out; this month heavier: {this_month}"
    if weight < -BILLS_WEIGHT_MIN:
        return f"evened out; this month lighter: {this_month}"
    return None


def map_rows(food: dict, other: dict, fixed: dict, *, ledger,
             budget=lambda v: v, pending: float = 0.0,
             savings: list[dict] = (), excess: float | None = None,
             excess_gone: bool = False) -> list[dict]:
    """A money map's rows in the wizard's order — categories (Food first,
    then carve-outs), Everything else, Bills, Savings, Excess — from one
    bucket set: Today's month, a Month lens's month or a Year lens's
    budgeted months. The map on the page, the phone's map and the email
    draw these rows.

    `budget` turns a variable bucket's budget into what the rows measure
    against (Today's paced share; a lens's buckets arrive paced); Bills,
    Savings and Excess are not paced. `ledger(bucket)` is the path a label
    opens. `savings` is the Savings row, if any (_savings_row on Today,
    lenses.lens_savings_row on a lens)."""
    rows = []
    kids = food.get("children") or []
    rows.append({"label": "Food", "judged": True, "to": ledger("food"),
                 "actual": food["display_actual"] if kids else food["actual"],
                 "expected": (food["display_expected"] if kids
                              else food["expected"]),
                 "budget": budget(food["remaining_budget"] if kids
                                  else food["month_budget"])})
    for c in kids:
        rows.append({"label": c["name"], "actual": c["actual"],
                     "expected": c["expected"],
                     "budget": budget(c["month_budget"]),
                     "judged": True, "to": ledger(c["name"])})
    okids = other.get("children") or []
    for c in okids:
        rows.append({"label": c["name"], "actual": c["actual"],
                     "expected": c["expected"],
                     "budget": budget(c["month_budget"]),
                     "judged": True, "to": ledger(c["name"])})
    row = {"label": "Everything else", "judged": True, "to": ledger("other"),
           "actual": other["display_actual"] if okids else other["actual"],
           "expected": (other["display_expected"] if okids
                        else other["expected"]),
           "budget": budget(other["remaining_budget"] if okids
                            else other["month_budget"])}
    if okids:
        row["caption"] = "unallocated — spending outside the categories"
    # every dollar went to the carve-outs and nothing was spent outside
    # them: a "$0 / $0" row says nothing (the tiles leave it off too)
    if not (okids and other["remaining_budget"] <= 0.005
            and abs(other["display_actual"]) <= 0.005):
        rows.append(row)
    # the due-dated bills only: the envelopes are their own row
    # (with_envelopes); a bucket from before the split has only the
    # combined figures
    posted = fixed.get("occ_actual", fixed["actual"])
    pend = pending or 0
    # the still-due figure goes on the row's RIGHT side via _row_delta;
    # the caption names what posted and what awaits, and that bills are
    # planned but never judged — one caption for web, phone and email
    rows.append({"label": "Bills", "judged": False, "actual": posted,
                 "expected": fixed.get("occ_expected", fixed["expected"]),
                 "budget": fixed.get("occ_budget", fixed["month_budget"]),
                 "pending": pend,
                 "to": ledger("fixed"),
                 "caption": (f"posted {charts._money(posted)}"
                             + (f" · awaiting {charts._money(pend)}"
                                if pend > 0.005 else "")
                             + " · not counted in the verdict")})
    rows.extend(savings)
    if excess is not None and excess > 0.005:
        # a PLAN figure — the plan line's income less its budgets, its
        # evened-out bills and its savings — said as one, so it is not
        # read against this month's verdict or this month's Bills row
        rows.append({"label": "Excess cash", "judged": False, "actual": 0,
                     "actual_label": "—", "expected": excess,
                     "budget": excess,
                     "caption": excess_words(excess, excess_gone)})
    return rows


def excess_words(excess: float, used_up: bool) -> str:
    """The plan's excess in words, for the Excess cash row and the Budget
    summary: a plan figure, said as one, and — once the month's overrun
    of its variable budget is at least the whole excess — that none of
    it is left this month. A smaller overrun leaves the words as they
    are: the plan's figure, which they never claimed was this month's.
    `used_up` is excess_used_up's."""
    return (f"{charts._money(excess)} planned excess"
            " (the plan's, with bills evened out)"
            + (" · none this month: spending past the month's budget has"
               " used it up" if used_up else ""))


def plan_excess_words(st: dict) -> dict | None:
    """The words and tone after the Budget summary line's excess figure:
    "planned excess", and "none this month" (muted, not green) once the
    month's overrun has used the whole excess up (excess_used_up). None
    when the line prints no excess."""
    legs = st.get("plan_legs") or {}
    ex = legs.get("excess")
    if ex is None:
        return None
    gone = excess_used_up(st)
    return {"text": ("planned excess · none this month" if gone
                     else "planned excess"),
            "tone": "neg" if ex < 0 else "mut" if gone else "pos"}


def excess_used_up(st: dict) -> bool:
    """The month's variable spending is past its whole (paced) budget by
    at least the plan's excess (plan_surplus): the overrun has spent all
    of it. Past the budget by less, some of the excess is still there,
    and nothing is said; and a plan with no excess (none, zero or below)
    had nothing to use up, so nothing is said of it either."""
    ex = st.get("plan_surplus")
    if ex is None or ex <= 0.5:
        return False
    over = ((st.get("variable_actual") or 0)
            - (st.get("variable_budget_paced") or 0))
    return over > 0.5 and over >= ex - 0.5


def _plan_rows(st: dict) -> list[dict]:
    """Today's plan rows (map_rows over the month's state). A judged row's
    budget is its paced share (paced_budget), the money its tile hands
    out."""
    b = st["buckets"]
    return map_rows(b["food"], b["other"], b["fixed"],
                    ledger=lambda k: _ledger(st, k),
                    budget=lambda v: paced_budget(st, v),
                    pending=b["fixed"].get("unpaid_due") or 0,
                    savings=_savings_row(st),
                    excess=st.get("plan_surplus"),
                    excess_gone=excess_used_up(st))


def sent_plan_rows(rows: list[dict]) -> list[dict]:
    """The rows as a reply sends them: every figure to the cent, and the
    words beside it — the status a row already carries (today_map), else
    row_status over the figures as sent — the page, the phone and the
    mail print these and compute nothing."""
    out = []
    for r in rows:
        row = {"label": r["label"], "judged": bool(r["judged"]),
               "to": r.get("to"),
               "actual": round(r["actual"], 2),
               "expected": round(r["expected"], 2),
               "budget": round(r["budget"], 2),
               "pending": round(r.get("pending") or 0, 2),
               "caption": r.get("caption"),
               "actual_label": r.get("actual_label"),
               # the Bills row's parts (bills_breakdown) and the
               # Envelopes row's (envelope_breakdown), None elsewhere
               "breakdown": r.get("breakdown")}
        row["status"] = (r["status"] if "status" in r
                         else row_status(row))
        out.append(row)
    return out


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
    # a savings month that was a net withdrawal has a negative actual; the
    # bar draws it as nothing filled (the figure beside it says how much)
    actual = max(row["actual"], 0)
    extent = max(row["budget"], actual + pending, 1)
    isover = (actual > row["expected"] + 0.5 if row["judged"]
              else actual > row["budget"] + 0.5)
    spent = min(actual, row["budget"])
    pend = min(pending, max(row["budget"] - spent, 0))
    over = max(actual - row["budget"], 0)
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


def row_status(row: dict, basis: str = "budget") -> dict | None:
    """What a money-map row says beside its figures, so over/under is
    never colour-alone — the one rule the page, the phone and the email
    print (specs/shown-figures.md, specs/today-figures.md):

    - `over`: spent past the budget, by `amount`;
    - `ahead`: a judged row past today's pace but inside its budget,
      `amount` more than planned;
    - `left`: a judged row on or under pace, `amount` left (budget less
      spent less awaiting);
    - `due`: the Bills row, `amount` still due and `pending` awaiting;
    - None: nothing to say (no budget, a plan-only row, or a Bills row
      with nothing left to come).

    `text` is the words every surface prints and `tone` their colour
    (`neg`, `pos`, `mut`); no client composes them. Differences under 50
    cents count as none, as the bars draw them.

    `basis="pace"` (Today) judges a judged row against where it should
    be by today instead (_pace_status); the lenses keep the budget
    basis."""
    if basis == "pace" and row["judged"]:
        return _pace_status(row)
    if row["budget"] <= 0.005 or row.get("actual_label") == "—":
        return None
    pend = row.get("pending") or 0
    left = row["budget"] - row["actual"] - pend
    if row["actual"] > row["budget"] + 0.5:
        out = {"kind": "over",
               "amount": round(row["actual"] - row["budget"], 2),
               "pending": 0.0}
    elif row["judged"] and row["actual"] > row["expected"] + 0.5:
        out = {"kind": "ahead",
               "amount": round(row["actual"] - row["expected"], 2),
               "pending": 0.0}
    elif row["judged"]:
        out = {"kind": "left", "amount": round(left, 2), "pending": 0.0}
    elif row["label"] == "Bills" and (left > 0.5 or pend > 0.5):
        out = {"kind": "due", "amount": round(max(left, 0), 2),
               "pending": round(pend if pend > 0.5 else 0.0, 2)}
    else:
        return None
    out["vs_pace"] = None
    out["text"], out["tone"] = status_words(out)
    return out


def _pace_status(row: dict, dollars: int | None = None) -> dict | None:
    """A judged row against where it should be by today: `vs_pace` is
    spent less expected, to the cent, and the words say it in whole
    dollars — `dollars` when today_map apportioned them, else the row's
    own rounding. `kind` and `amount` keep the meanings an older phone
    reads them by: `ahead` (more than planned, by `amount`) past the
    pace, else `left` (budget less spent less awaiting)."""
    from ..engine.reporting import whole_dollars
    if row.get("actual_label") == "—":
        return None
    diff = row["actual"] - row["expected"]
    if (row["budget"] <= 0.005 and abs(row["actual"]) <= 0.5
            and abs(diff) <= 0.5):
        return None
    pend = row.get("pending") or 0
    if diff > 0.5:
        out = {"kind": "ahead", "amount": round(diff, 2), "pending": 0.0}
    else:
        out = {"kind": "left",
               "amount": round(row["budget"] - row["actual"] - pend, 2),
               "pending": 0.0}
    out["vs_pace"] = round(diff, 2)
    d = whole_dollars(diff) if dollars is None else dollars
    money = charts._money
    if d > 0:
        out["text"], out["tone"] = f"{money(d)} over pace", "neg"
    elif d < 0:
        out["text"], out["tone"] = f"{money(-d)} under pace", "pos"
    else:
        out["text"], out["tone"] = "on pace", "pos"
    return out


def status_words(st: dict) -> tuple[str, str]:
    """A row status in the words and tone every surface prints."""
    money = charts._money
    if st["kind"] == "over":
        return f"over by {money(st['amount'])}", "neg"
    if st["kind"] == "ahead":
        return f"{money(st['amount'])} over pace so far", "neg"
    if st["kind"] == "left":
        return f"{money(st['amount'])} left", "pos"
    return ((f"{money(st['pending'])} awaiting · " if st["pending"] else "")
            + f"{money(st['amount'])} still due"), "mut"


_TONE_COLOR = {"neg": "var(--red)", "pos": "var(--green)",
               "mut": "var(--mut)"}


def _row_delta(row: dict) -> dict | None:
    """The email's words and colour for row_status (MoneyMap.tsx prints
    the same status text)."""
    st = row["status"] if "status" in row else row_status(row)
    if st is None:
        return None
    return {"text": st["text"], "color": _TONE_COLOR[st["tone"]]}


def _ledger(st: dict, bucket: str) -> str:
    """The in-app path a bucket label opens: the rows the month verdict
    counts in that bucket, for the month on view (the SPA's Today.tsx
    tileTo builds the same path for the tiles — keep the two in
    lockstep).

    `bucket` is 'food', 'other', or a custom carve-out's configured name.
    It is NOT a category: Food is a category plus two override strings,
    minus every bill-shaped row, plus envelope overflow, and a carve-out
    is a plan label matched by merchant rules as well as category rules.
    A `cat=` link would land on rows the tile never counted — a month of
    groceries filed by merchant rule opens an empty ledger under a $300 Food tile — so the
    ledger asks the engine for the same rows the number came from
    (budget.bucket_ledger, behind ?bucket=).
    """
    d = st["today"]
    # as_of: the day the number was read on. A past day of the month shows
    # that day's figures, so its listing must stop there too, not at today
    return ledger_path(bucket, d.year, d.month, d)


def ledger_path(bucket: str, y: int, m: int | None = None,
                as_of: dt.date | None = None) -> str:
    """The ledger listing a map label opens: the bucket's rows for month
    `m` of `y`, or with no month the year's bucket — the union over the
    budgeted months a Year lens map sums."""
    from urllib.parse import quote
    return (f"/transactions?bucket={quote(bucket)}&y={y}"
            + (f"&m={m}" if m else "")
            + (f"&as_of={as_of.isoformat()}" if as_of else ""))


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
    rows, total = today_map(st)
    measurable = [r for r in rows if r.get("actual_label") != "—"]
    folded = [r for r in rows if r.get("actual_label") == "—"]
    judged = [r for r in measurable if r["judged"]]
    unjudged = [r for r in measurable if not r["judged"]]
    total = total if len(judged) > 1 else None
    # the hero's track is the month's spendable share, the "$X of $Y
    # left" the card prints beside it
    var_budget = paced_budget(st, st["buckets"]["food"]["month_budget"]
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
            folded_bits.append(r["caption"])
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
    dim = st["days_in_month"]
    # The verdict's own window (budget.pace_window): the month may spend
    # the budget's share from the pace start — the later of the 1st and
    # the ledger's first day — to month end. Spreading the WHOLE month's
    # budget over the days left would hand out money the verdict never
    # expects to see spent, and following the tiles would turn it over.
    # (a status built by hand, without a ledger, paces from the 1st)
    pace = _pace(st)
    days_left = max(1, pace["days_left"])
    paced_days = pace["paced_days"]

    def _allow(month_budget, actual, rows):
        spendable = paced_budget(st, month_budget)
        spent = sum(r["amount"] for r in rows
                    if as_date(r["date"]) == st["today"])
        # one rule for the rate and the days it covers (budget.daily_rate):
        # the Why's "keeps it green", its recovery cap and the push/SMS
        # read the same numbers. Once today has spend, today is charged at
        # the larger of its allowance and the spend, so a $1 coffee never
        # "raises" the daily and left_today + rate x rate_days is exactly
        # what is left of the month.
        dr = budget.daily_rate(spendable, actual, spent, days_left)
        allowance = dr["allowance"]
        rate_days = dr["rate_days"]
        rate = dr["rate"]
        plan = month_budget / dim if dim else 0.0
        # the plan-vs-now daily, said IN the tile: what
        # this category's daily budget was, and what the month's spending
        # has moved it to for every remaining day. Composed here once so
        # the page, the email HTML and the plain text say the same words.
        note = None
        tone = None
        # both figures in the whole dollars every surface prints
        # (budget.shown_daily, floored): the recovery sentence and the SMS
        # say the same numbers as this note. The direction is decided in
        # cents, never from the printed dollars — rounding one side and
        # flooring the other called an on-plan month "reduced".
        p_, r_ = budget.shown_daily(plan), budget.shown_daily(rate)
        reduced = rate < plan - 0.005
        raised = rate > plan + 0.005
        # on the month's last day the rate is simply what is left of
        # today — there is no later day for a daily to be reduced on or
        # recovered over, so the plan-vs-now note has nothing to say; and
        # a move too small to change the printed dollars says nothing
        last_day = days_left <= 1
        if (month_budget > 0 and (reduced or raised) and p_ != r_
                and not last_day):
            note = (f"daily {'reduced' if reduced else 'raised'} "
                    f"from ${p_:,.0f} to ${r_:,.0f}")
            # a reduced daily is bad news, a raised one good — carried as
            # data so the renderers color the note without reading its words
            tone = "neg" if reduced else "pos"
        # the question a reduced daily raises — how many no-spend days
        # bring it back — answered in the tile, in one place, so the page,
        # the email HTML and the plain text say the same number
        recover = None
        recover_note = None
        if tone == "neg":
            # over the paced days, so its plan is still month_budget / dim
            recover = recover_days(spendable, dr["spread_actual"],
                                   rate_days, paced_days)
            if recover is None:
                recover_note = f"can't get back to ${p_:,.0f} this month"
            elif recover == 1:
                recover_note = (f"one no-spend day and the daily is back "
                                f"to ${p_:,.0f}")
            elif recover > 0:
                recover_note = (f"skip {recover} days of spending and the "
                                f"daily is back to ${p_:,.0f}")
        # the line under the day's figure, composed once for the page
        # (every width), the app and the email
        money = charts._money
        left_t = allowance - spent
        allow_d = budget.shown_daily(allowance)
        if left_t < -0.5:
            # with no daily left this month, "of $0" reads as a figure
            # out of nothing: the line says there is none
            day_line = (f"over today by {money(-left_t)} · spent "
                        f"{money(spent)}"
                        + (f" of ${allow_d:,}" if allow_d > 0
                           else ", no daily left"))
        elif spent > 0.005:
            day_line = f"of ${allow_d:,} today · spent {money(spent)}"
        else:
            day_line = f"of ${allow_d:,} today · nothing spent yet"
        return {"rate": rate,
                # the whole dollars every surface prints for the day's
                # figures (budget.shown_daily): floored, once, here
                "rate_dollars": budget.shown_daily(rate),
                "today_allowance_dollars": allow_d,
                "left_dollars": budget.shown_daily(left_t),
                "day_line": day_line,
                # how many days `rate` covers: what is left of today (when
                # not over) plus rate x rate_days is the remainder the hero
                # shows
                "rate_days": rate_days,
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
                # nothing to hand out today and nothing spent today: a
                # "$0 of $0" row is a big zero, an empty bar and a caption
                # restating it, so every surface folds it to one line —
                # decided here once so the page, the app and both email
                # mirrors fold the same rows
                "zero_day": (round(allowance, 2) <= 0.005
                             and abs(spent) <= 0.005),
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
            # every dollar of this bucket went to its carve-outs and
            # nothing has landed in what is left: the tile would read
            # "$0 of $0" beside the tiles that hold the money
            # (a refund landing in it is not nothing: it gives the bucket
            # room again, so a negative figure keeps the tile)
            allow[k]["empty"] = (b["remaining_budget"] <= 0.005
                                 and abs(b["display_actual"]) <= 0.005
                                 and abs(allow[k]["spent_today"]) <= 0.005)
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


def shown_tiles(tiles) -> list:
    """The tiles a person is shown: a parent bucket carved out to nothing
    is left off. It carries no allowance and no spend, so every total
    over the shown tiles equals the total over all of them."""
    return [(label, a) for label, a in tiles if not a.get("empty")]


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
    from . import envelope_breakdown
    cards = []
    today = st["today"]
    # the pace post tick: where "today" sits on the card's own period, so the
    # bar answers ahead-or-behind at a glance (the money map's idiom). A
    # monthly pool paces on the month; an annual pool on the calendar year.
    month_frac = today.day / st["days_in_month"]
    year_days = (dt.date(today.year, 12, 31)
                 - dt.date(today.year, 1, 1)).days + 1
    year_frac = (today.timetuple().tm_yday) / year_days
    # one order, whatever order the rows were read in: by the name
    # shown, then the stable id (envelope_breakdown.listed)
    for env in sorted(st["buckets"]["fixed"].get("envelopes") or [],
                      key=envelope_breakdown.listed):
        if not env.get("show_today"):
            continue
        pool, pm = env["pool"], env["period_months"]
        over = env["period_left"] <= 0.005
        status = tone = None
        # the pool named on its own: its own pair, its period said — the
        # words the Month card, the pool list and both emails print
        item = envelope_breakdown._item(env, settled=False, kind="running",
                                        m=today.month)
        if item["pair_past"] > 0:
            # this month's own overflow for the pool
            status = (f"${item['pair_past']:,} past the pool this month, "
                      "counted in spending")
            tone = "neg"
        cards.append({
            "kind": "envelope", "label": env["payee"],
            "left": round(env["period_left"], 2), "over": over,
            # the spent-of-cap pair, its period said; the spill past it
            # this month is the card's status
            "sub": (f"used ${item['pair_spent']:,} of "
                    f"${item['pair_of']:,} {item['shown_period']}"),
            "frac": budget.envelope_frac(env["period_used"], pool),
            "pace_frac": round(month_frac if pm == 1 else year_frac, 4),
            "status": status, "status_tone": tone})
    for p in sorted(st.get("pinned_bills") or [],
                    key=lambda p: (str(p.get("payee") or "").lower(),
                                   str(p.get("payee") or ""),
                                   str(p.get("uid") or ""))):
        remaining = max(0.0, p["planned"] - p["paid"])
        due = (dt.date.fromisoformat(p["next_due"]).strftime("%m/%d")
               if p["next_due"] else None)
        gone = p.get("withdrawn_on")
        if p["overdue"] and gone:
            # charged, then taken back by the feed: still unpaid in the
            # sums, but a warning to check, not a missed payment
            status = ("charge withdrawn by the bank feed "
                      f"({dt.date.fromisoformat(gone):%m/%d})")
            tone = "warn"
        elif p["overdue"]:
            status, tone = f"overdue — was due {due}", "neg"
        elif due:
            status, tone = f"due {due}", None
        elif p["paid_count"]:
            status, tone = "all paid", "pos"
        else:
            status = tone = None
        cards.append({
            "kind": "bill", "label": p["payee"],
            "left": round(remaining, 2),
            "over": bool(p["overdue"]) and not gone,
            "sub": (f"paid ${p['paid']:,.0f} of ${p['planned']:,.0f} "
                    "this month"),
            "frac": (min(1.0, p["paid"] / p["planned"])
                     if p["planned"] > 0 else 1.0),
            "pace_frac": round(month_frac, 4),
            "status": status, "status_tone": tone})
    return cards


MONTH_END_LABEL = "checking at month end"
MONTH_END_NOTE = ("after this month's bills, paychecks, card payments "
                  "and spending at your usual pace")


def month_end_left(st: dict, fc: dict) -> dict | None:
    """The balance the month ends on: the forecast's realistic walk
    (autopay statement balances, the forecast's daily pace, the bills and
    paychecks still due) on the month's last day — the one figure the
    "left at month end" tile prints and the cash timeline's month-end row
    shows (specs/today-figures.md). `balance_now` is the pay-all-cards-now
    walk on the same day, for the timeline's second column. None when the
    walk does not reach the day (it always does: it runs 60 days)."""
    today = st["today"]
    end = today.replace(
        day=calendar.monthrange(today.year, today.month)[1]).isoformat()
    usual = fc["pace_usual"]
    stmt = dict(usual["series"])
    now = dict(fc["pace_now"]["series"])
    if end not in stmt:
        return None
    # beside checking: what the same walk still holds on the cards that
    # day, and its next card payment after the month
    cards = dict(usual.get("cards_owed") or []).get(end)
    nxt = sorted(d0 for d0, _a, _c in usual.get("outflows") or []
                 if d0 > end)
    nxt_amt = (round(sum(a for d0, a, _c in usual["outflows"]
                         if d0 == nxt[0]), 2) if nxt else None)
    return {"date": end, "balance": stmt[end], "balance_now": now.get(end),
            "cards": cards, "next_payment": (
                {"date": nxt[0], "amount": nxt_amt} if nxt else None),
            "cards_note": month_end_cards_note(cards, nxt[0] if nxt else None,
                                               nxt_amt)}


def month_end_cards_note(cards: float | None, nxt: str | None,
                         amount: float | None) -> str | None:
    """The line under "checking at month end": what the usual walk still
    holds on the cards that day ("with $1,240 still on your cards then")
    and its next card payment after the month ("next card payment $1,180
    on 08/03/26"). Facts the checking figure does not already include;
    None with nothing on the cards."""
    if cards is None or cards <= 0.5:
        return None
    money = charts._money
    out = f"with {money(cards)} still on your cards then"
    if nxt and amount:
        out += f" · next card payment {money(amount)} on {fmt_date(nxt)}"
    return out


def pace_sentence(fc: dict) -> str | None:
    """The sentence under the cash chart that names the forecast's daily
    pace and what it was read over (forecast.forecast_pace: the last 60
    days' variable spending less its one-offs, or the plan's daily on a
    ledger under a week old), naming the one-offs it leaves out. Beside
    the Today page's "running $X/day this month" it says which window
    each rate is. None without a pace."""
    basis = fc.get("pace_basis")
    if not basis:
        return None
    money = charts._money
    out = _pace_words(fc, basis)
    factor = (fc.get("heavy_factor") or {}).get("factor")
    if out and factor:
        out += (f" A heavier month spends {money(basis['daily'] * factor)} a "
                "day on everyday purchases.")
    return out


def _pace_words(fc: dict, basis: dict) -> str:
    """pace_sentence's first sentence: the everyday pace, its window and
    one-offs, and the envelope money beside it."""
    money = charts._money
    env = fc.get("envelope_daily") or 0.0
    plus = (f" — plus {money(env)} a day of envelope money"
            if env >= 0.5 else "")
    if basis.get("basis") == "plan":
        return (f"The forecast spends {money(basis['daily'])} a day on "
                "everyday purchases — your plan's daily spending, until the "
                f"ledger holds a week{plus}.")
    days = basis.get("days") or 0
    one_offs = fc.get("one_offs") or []
    words = (f"The forecast spends {money(basis['daily'])} a day on everyday "
             f"purchases — your last {days} days")
    if not one_offs:
        return words + plus + "."
    named = ", ".join(f"{o['payee']} {money(o['amount'])} "
                      f"({fmt_date(o['date'])})" for o in one_offs[:3])
    more = len(one_offs) - 3
    return (words + ", less "
            + ("a one-off charge: " if len(one_offs) == 1
               else "one-off charges: ")
            + named + (f" and {more} more" if more > 0 else "") + plus + ".")


# The cash timeline's three card scenarios (specs/today-figures.md, "Card
# spending leaves checking when the household pays the card"): key, the
# words every surface labels it with, and the forecast walk it reads.
# "Your usual" is the headline: the cash notice, the low point, "left at
# month end" and the chart's default all read it.
CASH_SCENARIOS = (("usual", "Your usual", "pace_usual"),
                  ("now", "Pay all cards today", "pace_today"),
                  ("due", "Pay on the due date", "pace_due"))

# the cautious measure beside the scenarios (forecast's pace_now): checking
# less everything owed on cards, day by day — the list's second column and
# the quiet warning, never a scenario
AFTER_CARDS_LABEL = "after card balances"


def low_words(walk: dict) -> str:
    """A walk's low point in the words every surface prints: "$3,270 on
    05/12/27", with "· below $0 from 05/06/27" when it goes negative."""
    money = charts._money
    out = f"{money(walk['min'])} on {fmt_date(walk['min_date'])}"
    if walk.get("negative_date"):
        out += f" · below $0 from {fmt_date(walk['negative_date'])}"
    return out


def pay_today_low_words(walk: dict) -> str:
    """The pay-all-cards-today scenario's low in its own words: "If you
    paid every card off today, your low would be $2,880 on 05/19/27."
    (with "· below $0 from …" when it dips)."""
    money = charts._money
    out = (f"If you paid every card off today, your low would be "
           f"{money(walk['min'])} on {fmt_date(walk['min_date'])}")
    if walk.get("negative_date"):
        out += f" · below $0 from {fmt_date(walk['negative_date'])}"
    return out + "."


def low_cards_note(walk: dict) -> str | None:
    """Beside a scenario's low point: what the same walk still holds on
    the cards that day (walk["cards_owed"]), "with $2,950 still on your
    cards then" — so a scenario that keeps more checking only by carrying
    card debt says so. None at half a dollar or less."""
    held = dict(walk.get("cards_owed") or []).get(walk.get("min_date"))
    if held is None or held <= 0.5:
        return None
    return f"with {charts._money(held)} still on your cards then"


def autopay_marks(fc: dict, walk: dict) -> list:
    """The dated card payments an installed phone draws as "<card>
    autopay" lines (`marks`, `card_autopay`): only the payments of a card
    the household really pays by autopay (forecast.card_timing mode
    "autopay"), matched by the card's id (`walk["mark_cards"]`), never its
    name — two cards may share one. A statement paid at its due date any
    other way is a due date, which `due_marks` draws."""
    auto = {c.get("id") for c in fc.get("card_timing") or []
            if c.get("mode") == "autopay"}
    return [m for m, cid in zip(walk.get("marks") or [],
                                walk.get("mark_cards") or [])
            if cid in auto]


def due_marks(walk: dict) -> list[dict]:
    """A walk's card due-date marks in the words every surface prints:
    [{date, card, label, words}] — one per card per statement due date
    inside the chart (forecast.card_due_dates, the same dates in every
    scenario), `label` the chart's caption ("Harbor card due") and `words`
    the hovered day's line: what this scenario still pays that statement
    on its day ("Harbor card due — $412 owed"), "paid" when the scenario
    has already paid it, "carried balance" for a card it carries."""
    money = charts._money
    out = []
    for d0, card, owed in walk.get("due_marks") or []:
        state = ("carried balance" if owed is None
                 else "paid" if owed <= 0.005 else f"{money(owed)} owed")
        out.append({"date": d0, "card": card, "label": f"{card} due",
                    "words": f"{card} due — {state}"})
    return out


# the timeline lists at least this many days ahead, low point or not
TIMELINE_MIN_DAYS = 14


def timeline_horizon(today: dt.date, walk: dict) -> str:
    """The last day a scenario's timeline rows run to (specs/today-figures.md,
    "The timeline runs through the low"): the month's end or two weeks
    out, whichever is later — and never before the walk's own low point,
    so the events that make the low are always listed. The clients show
    the rows up to the low by default and the rest behind "all N events";
    the email lists a shorter span and counts the rest (email_timeline)."""
    month_end = today.replace(
        day=calendar.monthrange(today.year, today.month)[1])
    out = max(month_end,
              today + dt.timedelta(days=TIMELINE_MIN_DAYS)).isoformat()
    return max(out, walk.get("min_date") or out)


# the email's timeline lists at most this many scheduled events (the
# month-end row and the low point's own day aside)
EMAIL_TIMELINE_MAX = 25


def email_timeline(future: list, low_date: str | None,
                   today: dt.date) -> tuple[list, int, str | None]:
    """The usual walk's rows the email lists, and the count of the rest in
    words (specs/today-figures.md, "The timeline runs through the low"):
    the rows up to the low point but no further than the month's end or
    two weeks out, whichever is later (the page and the phone list
    through the low), at most EMAIL_TIMELINE_MAX of them, plus the
    month-end row and the low point's own day. (rows, hidden, words):
    "+ 4 more scheduled events after the low point" when every hidden row
    comes after the low, "+ 4 more scheduled events" when some come
    before it; words None with none hidden."""
    if not low_date:
        return future, 0, None
    month_end = today.replace(
        day=calendar.monthrange(today.year, today.month)[1])
    span = max(month_end,
               today + dt.timedelta(days=TIMELINE_MIN_DAYS)).isoformat()
    upto = min(span, low_date)
    shown, n = [], 0
    for r in future:
        if r[1] is None or r[0] == low_date:
            shown.append(r)
        elif r[0] <= upto and n < EMAIL_TIMELINE_MAX:
            shown.append(r)
            n += 1
    if not any(r[1] is not None for r in shown):
        # nothing before the low in the span: its first rows, capped
        shown = [r for r in future if r[1] is None] + [
            r for r in future if r[1] is not None][:EMAIL_TIMELINE_MAX]
        shown.sort(key=lambda r: r[0])
    kept = {id(r) for r in shown}
    hidden = [r for r in future if id(r) not in kept]
    if not hidden:
        return shown, 0, None
    k = len(hidden)
    words = f"+ {k} more scheduled event{'' if k == 1 else 's'}"
    if all(r[0] > low_date for r in hidden):
        words += " after the low point"
    return shown, k, words


def scenario_events(fc: dict, key: str) -> list[tuple]:
    """A scenario's dated rows for the cash timeline, in date order:
    [(date, amount, label, card)]: checking's own events, each
    bill a card pays on its day with the card named ("Acme Power — on
    Northwind card", `card` set: it moves no checking balance that day),
    and the card payments the walk makes (the pay-today walk's payoff
    first, "Northwind card — paid in full")."""
    w = fc[{k: walk for k, _label, walk in CASH_SCENARIOS}[key]]
    evs = [(d, a, p, None) for d, a, p in w.get("checking_events", [])]
    evs += [(d, a, f"{p} — on {card}", card)
            for d, a, p, card in w.get("charged_bills", [])]
    evs += [(d, a, p, None) for d, a, p in w.get("card_rows", [])]
    return sorted(evs, key=lambda e: e[0])


def heavy_label(fc: dict) -> str:
    """The heavier line's words, the legend's and the quiet line's alike
    (forecast.heavy_month_factor): the pace the household's own spending
    ran at in one 30-day stretch in five over the last year, surprise
    purchases included; with too little history, the default's margin."""
    heavy = fc.get("heavy_factor") or {}
    if heavy.get("basis") == "household":
        return ("heavier month — spending "
                f"{round((heavy.get('factor') or 1.0) * 100) - 100}% above "
                "the forecast's pace, as one 30-day stretch in five ran in "
                "the last year")
    return (f"heavier month — "
            f"{round((heavy.get('factor') or 1.25) * 100) - 100}% above "
            "your usual pace")


def cards_note(after: float, last_day: str, stays: float = 0.0) -> str | None:
    """The sentence under the chart for card spending a walk still owes on
    its last day: what is paid after it (forecast's cards_after_horizon,
    "$3,240 of card spending is paid after 05/30/27, when the chart
    ends.") and a carried balance that stays on the card ("$5,100 stays
    on your cards as a carried balance."). None at half a dollar or less
    of both."""
    out = []
    if after > 0.5:
        out.append(f"{charts._money(after)} of card spending is paid after "
                   f"{fmt_date(last_day)}, when the chart ends.")
    if stays > 0.5:
        out.append(f"{charts._money(stays)} stays on your cards as a "
                   "carried balance.")
    return " ".join(out) or None


# the net of own-account transfers in or out of checking, over the last
# forecast_score.OWN_TRANSFER_NOTE_DAYS days, from which the page says the
# forecast leaves them out
OWN_TRANSFER_NOTE_MIN = 500.0


def transfers_note(st: dict, fc: dict | None) -> str | None:
    """The sentence under the chart saying the forecast does not walk
    money moved between the household's own accounts, for a household
    whose walked checking sent or took back at least OWN_TRANSFER_NOTE_MIN
    that way (the larger direction) over the last 60 days to the end of
    yesterday (forecast_score.recent_own_transfers). One direction: "…
    over the last 60 days they moved about $1,200 out of checking." (or
    "into checking"); both: "… over the last 60 days about $4,000 left
    checking for your other accounts and about $3,800 came back." None
    below it, and when the forecast walks a transfer of its own (a savings
    plan, a month-end sweep): it would no longer be true. A bill set up to
    pay one of the household's own accounts is walked as a bill; the
    sentence says so ("… not in this forecast unless you set one up as a
    bill — …")."""
    from ..engine import forecast_score as _fs
    own = st.get("own_transfers")
    if not fc or not own:
        return None
    out, back = own.get("out") or 0.0, own.get("in") or 0.0
    if max(out, back) < OWN_TRANSFER_NOTE_MIN:
        return None
    # a bill the household set up to move money to its own account is
    # walked like any bill, and nothing but its name would say so — a name
    # is no test (an account called Savings beside a bill "Savings club"),
    # so the sentence itself carries the exception
    if any(p.endswith("(savings plan)")
           for _d, _a, p in fc.get("events") or []):
        return None
    if (fc.get("pace_usual") or {}).get("sweep_events"):
        return None
    money = charts._money
    lead = ("Transfers between your own accounts are not in this forecast "
            "unless you set one up as a bill "
            f"— over the last {_fs.OWN_TRANSFER_NOTE_DAYS} days ")
    ro, rb = round(out, -1), round(back, -1)
    if ro >= 10 and rb >= 10:
        return (lead + f"about {money(ro)} left checking for your other "
                f"accounts and about {money(rb)} came back.")
    if ro >= rb:
        return lead + f"they moved about {money(ro)} out of checking."
    return lead + f"they moved about {money(rb)} into checking."


def stopped_paychecks_note(fc: dict | None) -> str | None:
    """The sentence saying which paychecks the forecast leaves out because
    their money stopped (forecast.stopped_paychecks), one per series:
    "Northgate Payroll has not arrived since 03/06/26, so it is not in
    this forecast — turn it off on the Bills page or it will be expected
    again when it returns." None when every paycheck is walked."""
    gone = (fc or {}).get("stopped_paychecks") or []
    if not gone:
        return None
    return " ".join(
        f"{g['payee']} has not arrived since "
        f"{dt.date.fromisoformat(g['last']):%m/%d/%y}, so it is not in this "
        "forecast — turn it off on the Bills page or it will be expected "
        "again when it returns." for g in gone)


# the band's sentence and legend once it is the forecast's own misses
# (specs/forecast-grading.md, "The band"): what it is, with no frequency
# the household's own record has not measured
OWN_BAND_TEXT = ("The shaded band is the range the forecast's own past "
                 "misses suggest.")
OWN_BAND_LABEL = "range of past misses"
# the charts that draw one line beside the walk (the server's, the email's)
# draw the band's low edge, named so
OWN_BAND_LOW = "low end of the forecast's past results"
# the first results show once some horizon has 30 results over 45 days:
# the 7-day one, about this many days after the first night
FIRST_RESULTS_DAYS = 7 + 45


def score_sentences(score: dict | None, today: dt.date) -> dict | None:
    """The forecast's track record in words, decided once
    (forecast_score.summarise): {text, bias_text, note, horizon, basis}.
    `text` "Over the last 3 months, the 30-day forecast landed within $250
    of your real balance 4 times in 5." once a horizon qualifies;
    `bias_text` "Lately the 30-day forecast has come out about $250 above
    your real balance." only when the misses lean; `note` (web and phone
    only) "The forecast now checks itself …" for the first 60 days before
    any result; `basis` "own" once the band is the forecast's own. None
    before the first night was kept: a new household is told nothing."""
    from ..engine import forecast_score as fs
    if not score:
        return None
    money = charts._money
    h = score.get("horizon")
    th = score.get("typical_horizon")
    text = bias = note = None
    own = (", not counting money you moved between your own accounts"
           if score.get("transfers_out") else "")
    parts = []
    if h:
        # a frequency only for a horizon with enough separate stretches
        # behind it (forecast_score.summarise)
        parts.append(f"Over the last {score.get('window') or '3 months'}, "
                     f"the {h}-day forecast landed within "
                     f"{money(score['within'])} of your real balance 4 "
                     f"times in 5{own}.")
    if th:
        # a longer horizon has too few separate stretches for a frequency:
        # its typical (median) miss, and the weeks it rests on
        parts.append(f"Over your last {score['typical_weeks']} weeks the "
                     f"{th}-day forecast was typically within "
                     f"{money(score['typical'])}"
                     + (own if not h else "") + ".")
    text = " ".join(parts) or None
    b = score.get("bias")
    if b:
        way = "below" if b["median"] > 0 else "above"
        bias = (f"Lately the {b.get('horizon') or 30}-day forecast has "
                f"come out about "
                f"{money(fs.plain_up(abs(b['median'])))} {way} your real "
                "balance.")
    first = score.get("first_night")
    if not h and not th and first:
        since = (today - dt.date.fromisoformat(first)).days
        if since < fs.NOTE_DAYS:
            weeks = max(1, math.ceil((FIRST_RESULTS_DAYS - since) / 7))
            note = ("The forecast now checks itself against your real "
                    "balance each night; the first results show here in "
                    f"about {weeks} week{'s' if weeks != 1 else ''}.")
    return {"text": text, "bias_text": bias, "note": note, "horizon": h,
            "basis": ("own" if score.get("band")
                      else "none")}


def cash_band(fc: dict, band: dict | None = None) -> dict | None:
    """The quiet cash line under the verdict, decided once, for when the
    usual walk (pace_usual) stays at or above $0 but another walk does not:
    paying every card off today (pace_now), which can dip when card
    balances outrun checking even while the usual walk stays above $0, or
    a heavier month (pace_low,
    forecast.heavy_month_factor). {kind "now" | "heavier", low, date,
    text}: "Your usual pattern stays above $0 (low $3,270 on 05/12/27). If
    you paid every card off today you would dip to -$385 on 04/21/27.",
    with the heavier month's sentence after it when that dips too. None
    when every walk stays above $0, and when the usual walk itself goes
    below $0 (the cash notice is the one warning then). Separate from
    cash_notice, so an older phone that words the notice from its kind
    stays quiet.

    `band`: the usual walk's band once it is the forecast's own misses
    (forecast_score.band): its lower edge stands in for the heavier month
    ({kind "band"}: "… At the low end of the forecast's past misses it
    would dip to -$165 around 06/08/27.")."""
    usual, now, low = (fc.get("pace_usual"), fc.get("pace_now"),
                       fc.get("pace_low"))
    # `now`: cash after card balances (pace_now) — checking less every
    # dollar owed on cards that day
    if not usual or usual["min"] < 0:
        return None
    money = charts._money
    like = heavy_label(fc).split(" — ", 1)[1]
    lead = (f"Your usual pattern stays above $0 (low {money(usual['min'])} "
            f"on {fmt_date(usual['min_date'])}).")
    if band is not None:
        lo_d, lo_v = min(band["lo"], key=lambda p: p[1])
        own = None
        if lo_v < 0:
            own = (f"At the low end of the forecast's past misses it would "
                   f"dip to {money(lo_v)} around {fmt_date(lo_d)}.")
        if now is not None and now["min"] < 0:
            text = (lead + f" On {fmt_date(now['min_date'])} your card "
                    f"balances would be about {money(-now['min'])} more than "
                    "your checking — you could not clear every card that "
                    "day.")
            if own:
                text += " " + own
            return {"kind": "after_cards", "low": now["min"],
                    "date": now["min_date"], "text": text}
        if own:
            return {"kind": "band", "low": round(lo_v, 2), "date": lo_d,
                    "text": lead + " " + own}
        return None
    heavier = (low is not None and low["min"] < 0)
    if now is not None and now["min"] < 0:
        text = (lead + f" On {fmt_date(now['min_date'])} your card "
                f"balances would be about {money(-now['min'])} more than "
                "your checking — you could not clear every card that day.")
        if heavier:
            text += (f" A heavier month ({like}) would take your usual "
                     f"pattern to {money(low['min'])} around "
                     f"{fmt_date(low['min_date'])}.")
        return {"kind": "after_cards", "low": now["min"],
                "date": now["min_date"], "text": text}
    if heavier:
        return {"kind": "heavier", "low": low["min"], "date": low["min_date"],
                "text": (lead + f" A heavier month ({like}) would take it "
                         f"to {money(low['min'])} around "
                         f"{fmt_date(low['min_date'])}.")}
    return None


# a round card whose own gap between payments is at least this long is paid
# "about once a month, after a payday"
ROUND_MONTHLY_GAP_DAYS = 25


def card_timing_note(fc: dict) -> str | None:
    """The sentence under the chart saying how the usual walk pays each
    card (forecast.card_timing): cards paid in rounds after payday ("You
    usually pay Northwind card right after payday when checking can cover
    it; otherwise at the due date."), cards paid sooner than their due
    dates ("Card spending reaches checking about 15 days after the charge
    on Harbor card, as your payments over the past 90 days show."), a
    carried balance ("Summit card carries a balance: the forecast pays
    $120 a month, as you have been, and the rest stays on the card."),
    autopay and statement cards ("… is paid at the statement due date").
    None when every card is paid at its due dates (the chart's lines say
    it)."""
    money = charts._money
    t = [c for c in fc.get("card_timing") or [] if c.get("charged")
         or c.get("mode") != "charge"]

    def names(cs):
        return (", ".join(cs[:-1]) + " and " + cs[-1]) if len(cs) > 1 else cs[0]

    parts = []
    rounds = [c for c in t if c["mode"] == "rounds"]
    if rounds:
        cushion = (fc.get("card_habit") or {}).get("cushion") or 0.0
        keep = f" and keep {money(cushion)}" if cushion > 0.5 else ""
        # one clause per rhythm: a card paid about monthly is not paid
        # "right after payday"; one with no statement cycle falls back to
        # a month after the charge, not a due date
        groups: dict[tuple, list[str]] = {}
        for c in rounds:
            monthly = (c.get("gap") or 0) >= ROUND_MONTHLY_GAP_DAYS
            groups.setdefault((monthly, bool(c.get("cycle", True))),
                              []).append(c["card"])
        for (monthly, cycle), cards_ in sorted(groups.items()):
            parts.append(
                f"You usually pay {names(cards_)} "
                + ("about once a month, after a payday" if monthly
                   else "right after payday")
                + f" when checking can cover it{keep}; otherwise "
                + ("at the due date" if cycle
                   else f"about {forecast.ROUND_FALLBACK_DAYS} days after "
                        "the charge"))
    lag = [c for c in t if c["mode"] == "lag"]
    if lag:
        bits = [f"about {round(lag[0]['lag'])} days after the charge on "
                f"{lag[0]['card']}"]
        bits += [f"{round(c['lag'])} days on {c['card']}" for c in lag[1:]]
        parts.append("Card spending reaches checking " + (
            ", ".join(bits[:-1]) + " and " + bits[-1] if len(bits) > 1
            else bits[0]) + ", as your payments over the past 90 days show")
    for c in t:
        if c["mode"] == "carried":
            parts.append(f"{c['card']} carries a balance: the forecast pays "
                         f"{money(c.get('payment') or 0)} a month, as you "
                         "have been, and the rest stays on the card")
    if not parts:
        return None
    due = [c["card"] for c in t if c["mode"] in ("statement", "autopay")]
    now = [c["card"] for c in t if c["mode"] == "charge"]
    if due:
        parts.append(names(due) + (" are" if len(due) > 1 else " is")
                     + " paid at the statement due date")
    if now:
        parts.append(names(now) + (" are" if len(now) > 1 else " is")
                     + " paid the day of the charge")
    # one sentence a clause, each opening with a capital
    return " ".join(p[0].upper() + p[1:] + "." for p in parts)


def cash_sentence(note: dict) -> str | None:
    """The cash notice in the words the page, the phone and both emails
    print — composed here once, from the notice build_context decided
    (`state`, `kind`, `amount`, `until`, `slow_to_per_day`, `ask`)."""
    if not note.get("state"):
        return None
    if note.get("ask"):
        return note["ask"]
    money = charts._money
    amount = note.get("amount") or 0.0
    until = note.get("until")
    slow = (f", or slow spending to {money(note['slow_to_per_day'])}/day"
            if (note.get("slow_to_per_day") or 0) >= 1 else "")
    kind = note.get("kind")
    if not slow and note.get("scheduled") and kind in ("today", "date"):
        slow = (" — bills and card payments due before "
                f"{fmt_date(note.get('runs_out') or until)} exceed what is "
                "coming in")
    # the today/headroom figures pay every card's whole balance now (the
    # cash card's "card debt") and the bills due before the paycheck: the
    # sentence says so, with the same card figure
    cards = note.get("card_debt") or 0.0
    payday = (f"your next paycheck ({fmt_date(until)})" if until
              else "your next paycheck")
    if kind == "today":
        if cards > 0.5:
            return (f"Short {money(amount)} today if you paid off all "
                    f"{money(cards)} on your cards and the bills due before "
                    f"{payday}. Cover it from savings{slow}.")
        return (f"Short {money(amount)} today — checking doesn't cover the "
                f"bills due before {payday}. Cover it from savings{slow}.")
    if kind == "date":
        return (f"Short {money(amount)} by "
                f"{fmt_date(until) if until else 'now'} — cover it from "
                f"savings{slow}.")
    if kind == "forecast" and note.get("runs_out"):
        return (f"Cash runs out around {fmt_date(note['runs_out'])} "
                f"(low {money(amount)} on {fmt_date(until)}).")
    if kind == "forecast":
        return f"Cash gets tight around {fmt_date(until)} (low {money(amount)})."
    return (f"Cash is tight — {money(amount)} left until your next paycheck"
            + (f" ({fmt_date(until)})" if until else "")
            + (f" if you paid off all {money(cards)} on your cards and the "
               "bills due before then." if cards > 0.5
               else " after the bills due before then."))


def hero_face(st: dict) -> dict:
    """The hero's figures and words from a month status alone: the days
    left, each bucket's day (allowances), the simple face's one number and
    chips, the day bar and the pace phrase. build_context renders the page
    and the mail from it, and the home-screen widget (/api/today/glance)
    reads it without the rest of the Today read — the forecast, the
    runway, the alerts — so the two can never show different numbers."""
    days_left = max(1, st["days_in_month"] - st["today"].day + 1)
    allow, allow_kids, every_tile = allowances(st)
    allow_tiles = shown_tiles(every_tile)
    # ---- the hero's composed sentences: ONE source for the SPA, the
    # email HTML and the plain text.
    # pace_line sits beside the verdict word; each tile carries its own
    # plan-vs-now daily note (composed in allowances above), which is
    # where the get-back-on-track framing lives.
    over_ = st["verdict"] == "OVER BUDGET"
    under_ = st["verdict"] == "UNDER BUDGET"
    d_ = st["today"].day
    # whole dollars as the money map's Variable spending row rounds the
    # same variance (reporting.whole_dollars), so the two print one figure
    from ..engine.reporting import whole_dollars
    if over_:
        pace_line = (f"${whole_dollars(st['variance']):,} more than "
                     "planned by today")
    elif under_:
        pace_line = (f"${whole_dollars(-st['variance']):,} less than "
                     "planned by today")
    else:
        pace_line = f"on pace · day {d_} of {st['days_in_month']}"
    # the SIMPLE hero face (today_view setting): ONE number — what is
    # still spendable today across every variable bucket — and a chip per
    # bucket. Composed here, whole-dollar like every aggregate, so the
    # page, the email HTML and the plain text render identical strings.
    # A bucket already over contributes $0 to the number (its allowance
    # cannot be borrowed by another bucket) and says so in its chip.
    # the tiles' own whole dollars, so the number is the sum of the chips
    simple_left = sum(a["left_dollars"] for _, a in allow_tiles)
    simple_chips = []
    for label_, a_ in allow_tiles:
        if a_["left_today"] < -0.5:
            # today's figure, so it says "today" like its sibling chips
            # and is not read as the month's overrun
            simple_chips.append({"text": f"{label_} over by "
                                 f"${-a_['left_today']:,.0f} today",
                                 "tone": "neg"})
        else:
            simple_chips.append({"text": f"{label_} "
                                 f"${a_['left_dollars']:,} today",
                                 "tone": ""})

    # the month's figure beside the pace: the pace line says what is
    # over by TODAY, so this one names the month, or the two over-figures
    # side by side read as one thing printed twice. The figure itself is
    # the client's (month_remaining_paced); the words after it are here.
    rem_ = st["month_remaining_paced"]
    bud_ = st["variable_budget_paced"]
    days_ = f"{days_left} day{'' if days_left == 1 else 's'} left"
    month_words = (f"over the month's ${whole_dollars(bud_):,} · {days_}"
                   if rem_ < 0 else
                   f"of the month's ${whole_dollars(bud_):,} left · {days_}")
    # under a spent month the amount still to hand out today is a bucket
    # not yet over; the label says so rather than read as the month's
    left_label = ("Left to spend today, in budgets not yet over"
                  if rem_ < 0 and simple_left > 0
                  else "Left to spend today")

    # top-pane bars are DAY-scoped: today's spend against
    # today's allowance, totalled across the buckets for the simple face's
    # full-width bar. The month gauges live in the Monthly budget card.
    day_spent, day_allow = simple_day_meter(allow_tiles)
    return {"days_left": days_left, "allow": allow, "allow_kids": allow_kids,
            "every_tile": every_tile, "allow_tiles": allow_tiles,
            "pace_line": pace_line,
            "month_words": month_words, "left_label": left_label,
            "simple": {"left_today": simple_left, "chips": simple_chips},
            "day_spent": day_spent, "day_allow": day_allow}


def build_context(st: dict, *, email: bool = False) -> dict:
    """Everything today_body.html needs beyond the gathered status dict —
    shared verbatim by the /today route and the email renderer so the two
    surfaces cannot diverge."""
    face = hero_face(st)
    days_left, pace_line = face["days_left"], face["pace_line"]
    allow, allow_kids = face["allow"], face["allow_kids"]
    every_tile, allow_tiles = face["every_tile"], face["allow_tiles"]
    simple_left, simple_chips = (face["simple"]["left_today"],
                                 face["simple"]["chips"])
    day_spent, day_allow = face["day_spent"], face["day_allow"]
    bill_cards = _bill_cards(st)
    rw = st.get("runway") or {}
    # an unset manual-account balance reads $0 — headroom computed
    # from it would panic a brand-new files-only user (email included).
    # No checking account at all arrives as checking None and stays quiet
    # the same way (`no_checking` is the explicit flag).
    headroom = (rw["checking"] - rw["card_debt"] - rw["due_total"]
                if rw.get("checking") is not None
                and not rw.get("no_checking")
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
    # newest first, then the larger amount, then the id: the order the
    # page's ledger query lists the same rows in (data.transactions_by_ids),
    # so the email and the page list a day's rows alike
    recent = sorted(st["yesterday_rows"],
                    key=lambda r: (-as_date(r["date"]).toordinal(),
                                   -abs(r["amount"]), str(r["txn_id"])))
    over_ = st["verdict"] == "OVER BUDGET"
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

    recent_var = [r for r in recent if r["txn_id"] not in fixed_ids]

    day = {
        "days_left": days_left, "allow": allow, "allow_kids": allow_kids,
        "allow_tiles": allow_tiles, "every_tile": every_tile,
        "bill_cards": bill_cards,
        "day_spent": day_spent, "day_allow": day_allow,
        "simple": {"left_today": simple_left, "chips": simple_chips},
        "pace_line": pace_line, "sweep_line": sweep_line,
        "month_words": face["month_words"], "left_label": face["left_label"],
        "bills_weight": bills_weight(st),
        # the plan line's bills leg in words: "bills & envelopes" when the
        # plan has a pool (budget.month_status)
        "plan_bills_label": st.get("plan_bills_label") or "bills",
        # the plan line's last words, the Budget summary's: "planned
        # excess", "· none this month" once the overrun has used it up
        "plan_excess_words": plan_excess_words(st),
        "why_chips": why_chips if over_ else [],
        "headroom": headroom,
        "mtd": entries, "amazon_subs": amazon_subs, "amazon_total": sum(amazon.values()),
        "recent_var": recent_var,
        # the recent window's variable spending: the verdict's netted rows
        # summed once, the figure the page and both email renders print
        "recent_var_total": round(sum(r["amount"] for r in recent_var), 2),
        "recent_fix": [r for r in recent if r["txn_id"] in fixed_ids],
        "summaries": st.get("amazon_summaries") or {},
    }
    fc = st.get("forecast")
    # the forecast's own grading, kept each night (forecast_score)
    from ..engine import forecast_score as _fscore
    score = st.get("forecast_score")
    # while imported accounts still sit at their $0 default,
    # every forecast number is seeded from a lie — suppress the whole
    # forecast (SPA card, email card, cash notice) rather than present
    # $0 as truth. The verdict card's set-your-balance nudge remains.
    # A household with no checking account is the same unknown: its walk
    # starts from $0 minus the cards, so it is suppressed too, and the
    # verdict card asks for the account instead.
    if (rw.get("balance_unreliable") or rw.get("no_checking")
            or (fc or {}).get("no_checking")):
        fc = None
    fc_chart = ""
    fc_rows = []
    tl_rows: list = []
    fc_email_chart = None
    # the chart's mark beside the first day below $0, in the page's
    # MM/DD/YY — the web chart, the phone's and the email's PNG draw it
    runs_out_label = ""
    pace_note = None
    month_end_ = month_end_left(st, fc) if fc else None
    if fc:
        negd_ = fc["pace_usual"].get("negative_date")
        runs_out_label = "runs out " + fmt_date(negd_) if negd_ else ""
        pace_note = pace_sentence(fc)
    scenarios: list[dict] = []
    if fc:
        try:
            today_ = st["today"]
            # the three card scenarios, each with its own walk, low point,
            # chart lines and timeline rows (CASH_SCENARIOS); the clients
            # draw the one selected, the email the usual one
            after_bal = dict(fc["pace_now"]["series"])
            for key, label, walk_key in CASH_SCENARIOS:
                w = fc[walk_key]
                bal = dict(w["series"])
                horizon = timeline_horizon(today_, w)
                est = set(fc.get("estimated") or [])
                rows = [{"date": d0, "amount": a, "label": p,
                         "balance": bal.get(d0), "kind": "event",
                         "card": card,
                         "estimated": p.split(" — on ")[0] in est,
                         "after_cards": after_bal.get(d0)}
                        for d0, a, p, card in scenario_events(fc, key)
                        if d0 <= horizon]
                if month_end_ and month_end_["date"] > today_.isoformat():
                    at = sum(1 for r in rows if r["date"] <= month_end_["date"])
                    # the tile above reads the usual walk: another
                    # scenario's month-end row names its scenario, so one
                    # label never carries two figures
                    rows.insert(at, {"date": month_end_["date"], "amount": None,
                                     "label": (MONTH_END_LABEL if key == "usual"
                                               else f"{MONTH_END_LABEL} — "
                                                    f"{label.lower()}"),
                                     "balance": bal.get(month_end_["date"]),
                                     "kind": "month_end", "card": None,
                                     "estimated": False,
                                     "after_cards": after_bal.get(
                                         month_end_["date"])})
                neg = w.get("negative_date")
                after = w.get("cards_after_horizon") or 0.0
                heavy = (fc.get("heavy") or {}).get(key)
                # the forecast's own misses around this walk, once they
                # qualify (forecast_score.band); the heavier line until then
                own = _fscore.band(w["series"], score,
                                   heavy=heavy["series"] if heavy else None)
                scenarios.append({
                    "key": key, "label": label, "series": w["series"],
                    "min": w["min"], "min_date": w["min_date"],
                    "negative_date": neg,
                    "low_words": (pay_today_low_words(w) if key == "now"
                                  else low_words(w)),
                    # what the walk still holds on the cards at its low
                    "low_cards_note": low_cards_note(w),
                    "runs_out_label": ("runs out " + fmt_date(neg)
                                       if neg else ""),
                    # dated autopay payments (an installed phone draws
                    # them, labelled "autopay")
                    "marks": autopay_marks(fc, w),
                    # each card's statement due dates, drawn as lines in
                    # every scenario, with this scenario's words for them
                    "due_marks": due_marks(w),
                    "rows": rows,
                    # the same walk in a heavier month, drawn beside it
                    "heavy": heavy["series"] if heavy else None,
                    "heavy_label": (heavy_label(fc) if heavy else None),
                    "band": own,
                    "band_text": OWN_BAND_TEXT if own else None,
                    "band_label": OWN_BAND_LABEL if own else None,
                    # card spending the walk still owes when the chart ends
                    "cards_after_horizon": round(after, 2),
                    "cards_note": cards_note(
                        after, w["series"][-1][0],
                        w.get("stays_on_card") or 0.0)})
            usual = scenarios[0]
            series = usual["series"]
            mx = next((i for i, (_, v) in enumerate(series) if v < 0), None)
            mlab = runs_out_label
            # dated card payments as lines, the same marks the web and the
            # app draw (the series is one point per day from today, so a due
            # date is found by its own index; a date off the end is dropped
            # rather than clamped, which would claim a payment lands there)
            day_ix = {d: i for i, (d, _) in enumerate(series)}
            ev_at: dict[int, list[str]] = {}
            # the usual scenario's card due dates (due_marks), as the web
            # and the app draw them
            for m in usual["due_marks"]:
                if m["date"] in day_ix:
                    ev_at.setdefault(day_ix[m["date"]], []).append(m["card"])
            ev = [(i, f"{len(ns)} cards due" if len(ns) > 2
                   else " + ".join(ns) + " due")
                  for i, ns in sorted(ev_at.items())]
            # the second line: the low edge of the forecast's own band
            # once it qualifies, else the heavier month
            second, second_label = ((usual["band"]["lo"], OWN_BAND_LOW)
                                    if usual["band"] else
                                    (usual["heavy"], heavy_label(fc)))
            # static render so the zero-line + critical-date marker survive
            fc_chart = charts.line(series, h=220, color=charts.PRIMARY,
                                   interactive=False, hover=True, zero_line=True,
                                   mark_x=mx, mark_label=mlab,
                                   second=second,
                                   events=ev, labels=(usual["label"],
                                                      second_label))
            # the email's chart: the same picture as a PNG (mail clients
            # strip SVG). It rides in the HTML as a data: URI and report.send
            # moves it into an inline image part, so every sender of this
            # HTML gets the part without knowing about it.
            # Rasterised only for a mail: the PNG is a supersampled PIL
            # render that costs more than the rest of the context put
            # together, and the API's callers (the SPA, the widget, the
            # integrations summary) never show it.
            fc_email_chart = None
            if email:
                from . import emailchart
                png = emailchart.render(
                    series, second=second, events=ev, mark_x=mx,
                    mark_label=mlab, labels=(usual["label"], second_label))
                fc_email_chart = _data_uri(png) if png else None
            # the usual walk's rows (the email's timeline), and the rows an
            # installed client reads (forecast_rows): the due-date walk's,
            # beside pace_stmt, each with the pay-all-cards-now balance
            bal_now = dict(fc["pace_now"]["series"])
            paid = sorted(
                (r["date"].isoformat(), -r["amount"],
                 f'{merchant_sql.shown_payee(r)} — paid ✓',
                 None, None, None)
                for r in st["buckets"]["fixed"].get("occ_rows", []))
            tl_rows = list(paid) + [
                (r["date"], r["amount"], r["label"], bal_now.get(r["date"]),
                 r["balance"], r["card"]) for r in usual["rows"]]
            fc_rows = list(paid) + [
                (r["date"], r["amount"], r["label"], bal_now.get(r["date"]),
                 r["balance"], r["card"]) for r in scenarios[2]["rows"]]
        except Exception:
            fc_chart, fc_rows, fc_email_chart = "", [], None
            tl_rows = []
            scenarios = []
    # each scenario's low point, side by side in the server's words
    fc_lows: list[dict] = [
        {"label": sc["label"], "short": sc["key"], "min": sc["min"],
         "min_date": sc["min_date"], "negative_date": sc["negative_date"],
         "words": sc["low_words"], "cards_note": sc["low_cards_note"]}
        for sc in scenarios]
    fc_lows_converged = False

    # cash: quiet until it matters — three states, computed here
    # because the template can't do date math. OK = no dict (silence).
    # Mirrors the SPA Today page and report.py's plain text.
    cash = None
    p0 = fc.get("pace_usual") if fc else None
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
    # decided against the walk itself (forecast.slow_to_rate): card
    # payments, bills and paydays are all in it, and a dip that comes from
    # what is scheduled gets no spending advice
    slow = (fc or {}).get("slow_to") or {}
    slow_to = slow.get("rate")
    scheduled = bool(slow.get("scheduled"))
    if ((headroom is not None and headroom < 0)
            or (neg_in is not None and neg_in <= 14)):
        if headroom is not None and headroom < 0:
            cash = {"state": "critical", "kind": "today",
                    "short": -headroom, "slow_to": slow_to,
                    "scheduled": scheduled,
                    # the payday the figure stops at: "before the next
                    # paycheck" with no date reads as a verdict on the
                    # month; with one it is the timing statement it is
                    "next_paycheck": st.get("next_paycheck")}
        else:
            cash = {"state": "critical", "kind": "date",
                    "short": -(first_neg if first_neg is not None
                               else p0["min"]),
                    "by": p0.get("negative_date"),
                    "slow_to": slow_to, "scheduled": scheduled}
    elif ((headroom is not None and headroom < paycheckish)
          or (p0 and (p0["min"] < 0 or neg_in is not None))):
        if p0 and (p0["min"] < 0 or neg_in is not None):
            cash = {"state": "tight", "kind": "forecast",
                    "low": p0["min"], "low_date": p0["min_date"]}
        else:
            cash = {"state": "tight", "kind": "headroom",
                    "headroom": headroom,
                    "next_paycheck": st.get("next_paycheck")}

    # the notice every client draws, decided here and nowhere else:
    # `state` critical | tight | None (quiet); `kind` which sentence;
    # `amount` the dollar it quotes (the shortfall, the headroom, or the
    # forecast low); `until` the date it runs to (the next paycheck, the
    # day the balance goes negative, the low's date); `slow_to_per_day`
    # the daily spend that clears the dip; `ask` the setup step that
    # replaces the alarm when no paycheck is scheduled
    cash_notice = {"state": None, "kind": None, "amount": None,
                   "slow_to_per_day": None, "until": None, "ask": None,
                   "runs_out": None, "text": None, "scheduled": False,
                   "card_debt": None}
    if cash:
        kind = cash["kind"]
        cash_notice = {
            "state": cash["state"], "kind": kind,
            "amount": round(cash["short"] if kind in ("today", "date")
                            else cash["low"] if kind == "forecast"
                            else cash["headroom"], 2),
            "slow_to_per_day": (round(cash["slow_to"], 2)
                                if cash.get("slow_to") is not None
                                else None),
            "until": (cash.get("next_paycheck") if kind in ("today",
                                                           "headroom")
                      else cash.get("by") if kind == "date"
                      else cash.get("low_date")),
            "ask": paycheck_ask(st),
            # the dip comes from bills and card payments, not spending
            "scheduled": bool(cash.get("scheduled")),
            # a forecast below $0 says when it gets there, not just how
            # low it goes
            "runs_out": (p0.get("negative_date")
                         if p0 and kind in ("date", "forecast") else None),
            # the card debt a today/headroom figure pays off (the cash
            # card's figure)
            "card_debt": (round(rw["card_debt"], 2)
                          if kind in ("today", "headroom") else None)}
        cash_notice["text"] = cash_sentence(cash_notice)
    if cash and paycheck_ask(st):
        cash = {"state": "ask", "text": paycheck_ask(st)}

    # ---- cash timeline: recent activity flows into
    # the scheduled events across a TODAY divider. Past = the paid ✓ rows
    # (no balance) + day.recent_var (the template merges them); future
    # events show through the forecast low point, the rest is a count.
    today_iso = st["today"].isoformat()
    tl_past, tl_future = [], []
    for r in tl_rows:
        (tl_past if (r[3] is None and r[4] is None and r[0] <= today_iso)
         else tl_future).append(r)
    low_date = fc["pace_usual"]["min_date"] if fc else None
    tl_pre, tl_rest, tl_rest_words = email_timeline(
        tl_future, low_date, st["today"])

    # Absolute base for links the EMAIL renders (alerts that point at a page).
    # mailable_base() and not the raw env, for the same reason the brand
    # header uses it: a LAN url is what gets the whole message filtered.
    from .report import mailable_base
    return {"st": st, "day": day, "fc": fc, "fc_chart": fc_chart,
            "fc_rows": fc_rows, "fc_lows": fc_lows,
            "fc_email_chart": fc_email_chart,
            "fc_lows_converged": fc_lows_converged, "cash": cash,
            "runs_out_label": runs_out_label, "pace_note": pace_note,
            "scenarios": scenarios,
            "card_timing_note": card_timing_note(fc) if fc else None,
            "transfers_note": transfers_note(st, fc),
            "stopped_paychecks_note": stopped_paychecks_note(fc),
            "cash_band": (cash_band(fc, scenarios[0]["band"] if scenarios
                                    else None) if fc else None),
            "forecast_score": (score_sentences(score, st["today"])
                               if fc else None),
            "month_end": month_end_,
            "month_end_label": MONTH_END_LABEL if month_end_ else None,
            "month_end_cards_note": ((month_end_ or {}).get("cards_note")),
            "month_end_note": (no_paycheck_note(st)
                               if st.get("income_due_note")
                               else MONTH_END_NOTE) if month_end_ else None,
            "cash_notice": cash_notice,
            "tl_past": tl_past, "tl_pre": tl_pre, "tl_rest": tl_rest,
            "tl_rest_words": tl_rest_words,
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
    # only ever on a .sub status line (the web scopes it .sub.warn)
    "warn": "color:#f0b429;",
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
    canonical webapp/public/icon.svg (re-render only from it, never by hand) —
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
        **(ctx if ctx is not None else build_context(st, email=True)))
    return mail_shell(f'Daily budget — {fmt_date(st["today"])}', body)


def mail_shell(heading: str, body: str) -> str:
    """The one mail shell: brand header, the heading, the body with the
    dark stylesheet baked in. Every cadence email goes through here, so
    the monthly reads as the daily's sibling, not a second design."""
    return (
        '<div style="background:#0f141a;padding:8px;">'
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'max-width:680px;margin:0 auto;padding:16px;color:#dbe3ea;background:#0f141a;'
        'border-radius:12px;font-size:15px;line-height:1.5;">'
        f'{_brand_header()}'
        f'<h2 style="font-size:18px;margin:0 0 12px;">{heading}</h2>'
        f'{_inline(body)}</div></div>')


def render_mail(heading: str, template: str, **ctx) -> str:
    """A cadence email from its own template, email mode, in the shared
    shell — the monthly and yearly bodies render this way."""
    body = _env.get_template(template).render(email=True, **ctx)
    return mail_shell(heading, body)
