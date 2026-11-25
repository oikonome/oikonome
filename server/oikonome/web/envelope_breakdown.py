"""A month's envelopes — every pool, monthly and yearly — as the ONE
source of the Envelopes row and of the list the row opens
(specs/today-figures.md, "Envelopes are their own row").

The row's bar is a pool gauge: left, what the pools paid out in the
period (`drawn`, which the matcher caps at what a pool has left); right,
that plus what is left in them (`cap`). So the bar is never past its
right figure and no client draws it red. The plan's monthly share of the
pools is said apart, as "set aside", in the caption. The row says
"over" only from `overflow` — what a pool's own period spent past the
pool, which the matcher already counts in Food or Everything else and
the row's figures never include.

`month` reads one month's status (its `fixed` bucket), `year` a Year
lens's months; `words`, `row_status`, `caption`, `table`, `sent` and
`apply_to_row` print it. Nothing else computes an envelope figure for a
page.
"""

from __future__ import annotations

from ..engine.reporting import whole_dollars

# the keys a reply carries (schemas.today.EnvelopeBreakdown); the cents
# each item keeps for `year` stay on the server
SENT = ("kind", "drawn", "cap", "set_aside", "overflow", "reserve",
        "unspent", "settled", "past", "y", "text")
ITEM_SENT = ("uid", "payee", "period_months", "pool", "drawn", "left",
             "cap", "year_drawn", "overflow", "unspent", "outside", "gone",
             "shown_used", "shown_of", "shown_period", "pair_spent",
             "pair_of", "pair_past", "pair_text")


def _money(d: int) -> str:
    return f"-${-d:,}" if d < 0 else f"${d:,}"


def _f(v) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _slug(payee: str) -> str:
    from ..engine.budget import bill_identity
    return bill_identity(payee)


_MONTH_NAMES = ("January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November",
                "December")


def month_period(kind: str, m: int) -> tuple[str, str]:
    """A month surface's period in words, (the year's use of a yearly
    pool, the month): the running month "this year" / "this month"; a
    closed month, or one to come, names itself — "by the end of June" /
    "in June"."""
    if kind == "running":
        return "this year", "this month"
    name = _MONTH_NAMES[m - 1]
    return f"by the end of {name}", f"in {name}"


def _item(e: dict, *, settled: bool, kind: str, m: int) -> dict:
    """One pool of month `m` (`kind` running, closed or future), from its
    month_status envelope entry alone: what it drew this month, what it
    has left at the month's end (now, on a running month), and the bar's
    right figure, `drawn + left`, printed as whole_dollars of the sum so
    drawn ≤ cap as printed; and its line (`pair_*`, pool_line), from the
    month's own figures — no other month is read."""
    months = 12 if int(_f(e.get("period_months")) or 1) > 1 else 1
    used = max(0.0, _f(e.get("used")))
    pool = max(0.0, _f(e.get("pool")) or _f(e.get("monthly")) * months)
    if months > 1:
        left = max(0.0, _f(e.get("period_left")))
    else:
        left = max(0.0, pool - used)
    drawn_d = whole_dollars(used)
    cap_d = max(drawn_d, whole_dollars(used + left))
    left_d = cap_d - drawn_d
    from ..engine.budget import envelope_shown
    su, so = envelope_shown({**e, "used": used})
    overflow = max(0.0, _f(e.get("overflow")))
    year_words, month_words = month_period(kind, m)
    period = year_words if months > 1 else month_words
    return {"uid": e.get("uid") if isinstance(e.get("uid"), str) else None,
            # what an older client prints as "spent of cap"
            # (budget.envelope_shown)
            "shown_used": whole_dollars(su), "shown_of": whole_dollars(so),
            "shown_period": period,
            **_month_pair(whole_dollars(su), whole_dollars(so),
                          whole_dollars(overflow), months > 1, period,
                          month_words),
            "_m": m,
            "_period_used": _f(e.get("period_used", used)),
            "payee": str(e.get("payee") or ""),
            "slug": _slug(str(e.get("payee") or "")),
            "period_months": months, "pool": whole_dollars(pool),
            "drawn": drawn_d, "left": left_d, "cap": cap_d,
            # a yearly pool's year so far, capped as the matcher booked it
            "year_drawn": (max(0, whole_dollars(pool) - left_d)
                           if months > 1 else drawn_d),
            "overflow": whole_dollars(overflow),
            "unspent": left_d if settled else 0,
            "outside": 0, "gone": False,
            # the cents `year` folds
            "_used": used, "_left": left, "_pool": pool,
            "_monthly": _f(e.get("monthly")),
            "_overflow": overflow}


def _month_pair(u: int, of: int, over: int, yearly: bool, period: str,
                month_words: str) -> dict:
    """A pool named on a month surface: the spent-of-cap pair from
    budget.envelope_shown, with its period said — "used $961 of $1,000 this
    year", "$40 of $85 this month" — and, only when the month itself sent
    part of the pool's charges to variable spending, that overflow: "·
    $15 past the pool this month, counted in spending"."""
    text = (f"used {_money(u)} of {_money(of)} {period}" if yearly
            else f"{_money(u)} of {_money(of)} {period}")
    if over > 0:
        text += (f" · {_money(over)} past the pool {month_words}, "
                 "counted in spending")
    return {"pair_spent": u, "pair_of": of, "pair_past": over,
            "pair_text": text}


def pool_figures(months: list[dict]) -> dict:
    """ONE pool over the Year's months, each `{"pool", "used",
    "overflow"}` as that month's own status booked it, oldest first:
    `spent` everything charged at its merchants (S), `pool` its size as
    it stands — the newest month's (P) — and `overflow` what the matcher
    sent to variable spending for it in those months (O, the months'
    own overflow summed; never S − P)."""
    return {"spent": sum(mo["used"] + mo["overflow"] for mo in months),
            "pool": months[-1]["pool"] if months else 0.0,
            "overflow": sum(mo["overflow"] for mo in months)}


def _pair(fig: dict, period: str) -> dict:
    """A yearly pool's line on the Year, in whole dollars: S
    (`pair_spent`), P (`pair_of`), O (`pair_past`, the months' own
    overflow), and the line (`pair_text`): "$1,050 spent in 2026 · pool
    $300 · $450 past the pool in 2026, counted in spending"."""
    s_d, p_d = whole_dollars(fig["spent"]), whole_dollars(fig["pool"])
    o_d = whole_dollars(fig["overflow"])
    text = (f"{_money(s_d)} spent {period} · pool {_money(p_d)}"
            + (f" · {_money(o_d)} past the pool {period}, counted in "
               "spending" if o_d > 0 else ""))
    return {"pair_spent": s_d, "pair_of": p_d, "pair_past": o_d,
            "pair_text": text}


def _monthly_year_pair(seen: list[dict], run_m: int | None) -> dict:
    """A MONTHLY pool's line on the Year: what its months spent at its
    merchants, its months' pools — the running month's said apart, the
    months through last month summed — and the overflow those months
    actually counted in spending (each month its own; a month under its
    pool never nets another's spill)."""
    s = sum(it["_used"] + it["_overflow"] for it in seen)
    o = sum(it["_overflow"] for it in seen)
    run = [it for it in seen if run_m is not None and it.get("_m") == run_m]
    before = [it for it in seen if not any(it is r for r in run)]
    s_d, o_d = whole_dollars(s), whole_dollars(o)
    p_d = whole_dollars(sum(it["_pool"] for it in seen))
    n = len(seen)
    pools = f"pools {_money(p_d)}"
    if run and before:
        b_d = whole_dollars(sum(it["_pool"] for it in before))
        last = max(it["_m"] for it in before)
        pools = (f"pools {_money(b_d)} through {_MONTH_NAMES[last - 1]}"
                 f" + {_money(p_d - b_d)} this month")
    elif run:
        pools = f"pool {_money(p_d)} this month"
    text = (f"{_money(s_d)} spent over {n} month{'s' if n != 1 else ''}"
            f" · {pools}"
            + (f" · {_money(o_d)} of it past a month's pool, counted in "
               "spending" if o_d > 0 else ""))
    return {"pair_spent": s_d, "pair_of": p_d, "pair_past": o_d,
            "pair_text": text}


def set_aside_dollars(fixed: dict) -> int:
    """The plan's monthly share of the pools in whole dollars: the plan
    less its due-dated part, each a headline in its own whole dollars, so
    the Bills plan and the set-aside add up to the plan as printed."""
    # the plan in the cents every reply sends it in (a binary sum can sit
    # on a half cent that the cents round away)
    return max(0, whole_dollars(round(_f(fixed.get("month_budget")), 2))
               - whole_dollars(_f(fixed.get("occ_budget",
                                            fixed.get("month_budget")))))


def plan_split(planned: float, bills_d: int, set_aside_d: int,
               occ: float) -> tuple[float, float]:
    """The Month card's plan in its two parts, in cents: the due-dated
    bills and the pools' set-aside, adding up to `planned` to the cent
    AND printing (whole dollars, halves to even) as `bills_d` and
    `set_aside_d` — the Bills row's plan and the list's set-aside. The
    bills part is the cents nearest the bills' own plan (`occ`) that
    print as `bills_d` and leave a set-aside that prints as
    `set_aside_d`; the set-aside is the rest."""
    total = int(round(float(planned) * 100))
    want = int(round(float(occ) * 100))
    lo, hi = bills_d * 100 - 50, bills_d * 100 + 50
    for b in sorted(range(lo, hi + 1), key=lambda c: (abs(c - want), c)):
        if (whole_dollars(b / 100) == bills_d
                and whole_dollars((total - b) / 100) == set_aside_d):
            return b / 100, (total - b) / 100
    # the printed parts cannot both hold (they do not add up to the
    # plan as printed): keep the cents, nearest the bills' own plan
    b = min(max(want, 0), total) if total >= 0 else want
    return b / 100, (total - b) / 100


def month(fixed: dict, *, kind: str, y: int, m: int,
          reserve: int = 0) -> dict:
    """A month's pools (`fixed` its status's fixed bucket): `kind` is
    running, closed or future; `reserve` the running month's envelopes
    part of report.month_end_parts, in whole dollars. A month whose plan
    has no pool has no `items`, and no Envelopes row (`has_row`); `year`
    still reads it, as the month a deleted pool is gone from.

    Settled: a closed month for its monthly pools; a closed December for
    every pool (the yearly pool's year is over)."""
    envs = fixed.get("envelopes") or []
    items = [_item(e, settled=kind == "closed" and (
        m == 12 or int(_f(e.get("period_months")) or 1) <= 1),
        kind=kind, m=m)
        for e in envs]
    return _finish({"kind": kind, "y": y, "m": m, "items": items,
                    "set_aside": set_aside_dollars(fixed),
                    "reserve": max(0, int(reserve)) if kind == "running"
                    else 0,
                    "settled": kind == "closed", "past": False,
                    # the row's figure is its pools' this month, as
                    # printed (words names the period)
                    "overflow": sum(i["overflow"] for i in items)})


def _finish(bd: dict) -> dict:
    bd["drawn"] = sum(i["drawn"] for i in bd["items"])
    bd["cap"] = sum(i["cap"] for i in bd["items"])
    bd["unspent"] = sum(i["unspent"] for i in bd["items"])
    bd["text"] = words(bd)
    return bd


def _slug_once(mo: dict | None, slug: str) -> bool:
    return mo is not None and sum(
        1 for it in mo["items"] if it["slug"] == slug) == 1


def _same(a: dict, a_mo: dict, b: dict, b_mo: dict, newest: dict) -> bool:
    """Two months' entries of one pool: by the stable id both carry and
    nothing else — a pool deleted and made again is a different pool.
    Only an entry from a month frozen before pools had ids matches by
    payee slug, and only when that slug is one pool's in both months and
    in the newest month (`newest`)."""
    if a.get("uid") and b.get("uid"):
        return a["uid"] == b["uid"]
    return (a["slug"] == b["slug"] and _slug_once(a_mo, a["slug"])
            and _slug_once(b_mo, b["slug"])
            and _slug_once(newest, a["slug"]))


def year(months: list[dict], *, past: bool | None = None) -> dict | None:
    """A Year lens's Envelopes row from its budgeted months' `month`
    breakdowns. Each pool is matched across the months (`_same`) and read
    as a whole, never as a sum of rounded monthly figures:

    - `drawn`: the months' `used` summed in cents, rounded once;
    - a yearly pool's `left`: the newest budgeted month's (nothing when
      the pool is no longer in it: deleted, "no longer tracked"), and
      `cap` = drawn + left; `outside` = the pool less the cap, signed —
      what it spent before the plan's months or before it was set up,
      or a resize;
    - a monthly pool's `cap`: its months' pools summed; `left` the
      running month's;
    - `overflow`: the months' overflow summed — never the pool's
      uncapped spend less the pool;
    - `set_aside`: the months' shares summed in cents, rounded once;
    - settled (a year that is over, or every budgeted month closed):
      each pool's `unspent` is cap − drawn.
    None without a month that had a pool."""
    months = sorted([mo for mo in months if mo is not None],
                    key=lambda mo: (mo["y"], mo["m"]))
    if not any(mo["items"] for mo in months):
        return None
    newest = months[-1]
    settled = bool(past) or all(mo["kind"] == "closed" for mo in months)
    run = next((mo for mo in months if mo["kind"] == "running"), None)
    pools: list[dict] = []
    for n, mo in enumerate(months):
        for it in mo["items"]:
            p = next((p for p in pools
                      if _same(p["last"], p["last_mo"], it, mo, newest)),
                     None)
            if p is None:
                p = {"used": 0.0, "pool_sum": 0.0, "overflow": 0.0,
                     "monthly": 0.0, "seen": [], "first_n": n, "spent": 0.0}
                pools.append(p)
            p["seen"].append(it)
            p["last"] = it
            p["last_mo"] = mo
            # a monthly pool's months, each its own period, summed
            p["spent"] += it["_used"] + it["_overflow"]
            p["used"] += it["_used"]
            p["pool_sum"] += it["_pool"]
            p["overflow"] += it["_overflow"]
            p["monthly"] += it["_monthly"]
    items = []
    for p in pools:
        last = p["last"]
        here = next((it for it in newest["items"]
                     if _same(last, p["last_mo"], it, newest, newest)), None)
        drawn_d = whole_dollars(p["used"])
        yearly = last["period_months"] > 1
        causes: list[tuple[str, int]] = []
        if yearly:
            left = here["_left"] if here is not None else 0.0
            cap_d = max(drawn_d, whole_dollars(p["used"] + left))
            left_d = cap_d - drawn_d
            outside = whole_dollars(last["_pool"]) - cap_d
            causes = _causes(p, here is None, outside)
            period = f"in {newest['y']}"
            shown = (whole_dollars(last["_period_used"]), last["pool"],
                     period)
            # the Year's own months of the pool, each as it was booked
            pair = _pair(pool_figures(
                [{"pool": it["_pool"], "used": it["_used"],
                  "overflow": it["_overflow"]} for it in p["seen"]]),
                period)
        else:
            cap_d = max(drawn_d, whole_dollars(p["pool_sum"]))
            r = (next((it for it in run["items"]
                       if _same(last, p["last_mo"], it, run, newest)), None)
                 if run is not None else None)
            left_d = r["left"] if r is not None else 0
            outside = 0
            shown = (drawn_d, cap_d, "over the plan's months")
            pair = _monthly_year_pair(
                p["seen"], run["m"] if run is not None else None)
        items.append({**pair,
            "shown_used": shown[0], "shown_of": shown[1],
            "shown_period": shown[2], "causes": causes,
            "uid": last["uid"], "payee": last["payee"], "slug": last["slug"],
            "period_months": last["period_months"], "pool": last["pool"],
            "drawn": drawn_d, "left": left_d, "cap": cap_d,
            "year_drawn": drawn_d,
            # the line's own figure: the row is its lines
            "overflow": pair["pair_past"],
            "unspent": cap_d - drawn_d if settled else 0,
            "outside": outside, "gone": here is None,
            "_used": p["used"], "_left": 0.0, "_pool": last["_pool"],
            "_period_used": last["_period_used"], "_m": last["_m"],
            "_monthly": p["monthly"], "_overflow": p["overflow"]})
    return _finish({
        "kind": "year", "y": newest["y"], "m": None, "items": items,
        "set_aside": whole_dollars(sum(p["monthly"] for p in pools)),
        "reserve": run["reserve"] if run is not None else 0,
        "settled": settled,
        "past": (past if past is not None
                 else all(mo["kind"] == "closed" for mo in months)),
        "overflow": sum(it["overflow"] for it in items)})


# why a yearly pool's Year does not draw all of it, each said only when
# it is not $0 (specs/today-figures.md, "What the Year's pool does not
# draw")
BEFORE_PLAN = "spent before the plan's months"
BEFORE_SET_UP = "spent before the pool was set up"
BETWEEN = "spent in months outside the plan"
RESIZED = "pool resized during the year"
REMOVED = "left unspent in a pool that was removed"


def _causes(p: dict, gone: bool, outside: int) -> list[tuple[str, int]]:
    """A yearly pool's `outside` (its size less what the plan's months
    drew less what is left) by cause, in cents and then apportioned once
    to `outside` as printed: what it had drawn before its first month on
    the Year (before the plan's months, or — first seen later — before
    the pool was set up); between two of its months, what it drew in
    months the plan does not cover, or its change of size when its size
    changed; and what a removed pool still held."""
    seen = p["seen"]
    first = seen[0]
    parts: dict[str, float] = {}

    def add(label, cents):
        parts[label] = parts.get(label, 0.0) + cents
    add(BEFORE_PLAN if p["first_n"] == 0 else BEFORE_SET_UP,
        first["_pool"] - (first["_left"] + first["_used"]))
    for a, b in zip(seen, seen[1:]):
        step = a["_left"] + (b["_pool"] - a["_pool"]) - (b["_left"]
                                                         + b["_used"])
        add(RESIZED if abs(b["_pool"] - a["_pool"]) > 0.005 else BETWEEN,
            step)
    if gone:
        add(REMOVED, seen[-1]["_left"])
    labels = [k for k, v in parts.items() if abs(v) > 0.005]
    if not labels:
        return []
    from ..engine.reporting import shown_dollars
    dollars = shown_dollars([parts[k] for k in labels],
                            sum(parts[k] for k in labels), dollars=outside)
    return [(k, d) for k, d in zip(labels, dollars) if d]


def has_row(bd: dict | None) -> bool:
    """Whether the map draws an Envelopes row: a period with a pool and
    something to print — never a "$0 / $0" row with nothing to say."""
    return (bd is not None and bool(bd["items"])
            and bool(bd["drawn"] or bd["cap"] or bd["text"]))


def pool_line(it: dict) -> str:
    """One pool named on its own, in the words every surface prints
    (`pair_text`): on a month surface the spent-of-cap pair with its
    period and the month's own overflow ("used $961 of $1,000 this year
    · $15 past the pool this month, counted in spending"); on the Year
    the Year's months of it (_pair, _monthly_year_pair). The gauge pair
    (paid out of what was held) is the aggregate row's alone."""
    return it["pair_text"]


def _period(bd: dict) -> str:
    """The row's period in words: a month view's month, the Year's
    year."""
    if bd["kind"] == "year":
        return f"in {bd['y']}"
    return month_period(bd["kind"], bd.get("m") or 1)[1]


def words(bd: dict) -> str | None:
    """The words beside the row, two phrases at most: what went past a
    pool ("$X past the pool this month, counted in spending", the row's
    period said: the sum of its pools' figures for it), then, settled, what
    was left unspent. On a row of several pools a phrase that is one
    pool's names it ("Trips: $X past the pool, counted in spending");
    one that is several pools' says so ("2 pools past their amounts,
    $X counted in spending"). A closed month's unspent is its monthly
    pools' ("$U of monthly pools unspent": a yearly pool's remainder is
    still the pool's until its year is over). What is still expected to
    month end is the caption's figure; never "under plan", "over plan"
    or "ahead": a pool is paid when it is paid. None with nothing to
    say."""
    items = bd["items"]
    many = len(items) > 1
    out = []
    over = [it for it in items if it["overflow"] > 0]
    when = _period(bd)
    if bd["overflow"] > 0:
        if many and len(over) == 1:
            out.append(f"{over[0]['payee']}: {_money(bd['overflow'])} past "
                       f"the pool {when}, counted in spending")
        elif many and len(over) > 1:
            out.append(f"{len(over)} pools past their amounts {when}, "
                       f"{_money(bd['overflow'])} counted in spending")
        else:
            out.append(f"{_money(bd['overflow'])} past the pool {when}, "
                       "counted in spending")
    if bd["settled"] and bd["unspent"] > 0:
        left = [it for it in items if it["unspent"] > 0]
        monthly_only = bd["kind"] == "closed" and bd.get("m") != 12
        if many and len(left) == 1:
            out.append(f"{left[0]['payee']}: {_money(bd['unspent'])} "
                       "unspent")
        elif monthly_only and any(it["period_months"] > 1 for it in items):
            out.append(f"{_money(bd['unspent'])} of monthly pools unspent")
        else:
            out.append(f"{_money(bd['unspent'])} unspent")
    return " · ".join(out[:2]) or None


def row_status(bd: dict) -> dict | None:
    """The row's status: the words as `text`; `kind` / `amount` in the
    meanings an older phone reads them by — `over` (tone neg) by what
    the pools' periods spent past them, the only way this row is ever
    negative; else `left` by what was unspent. None with nothing to
    say."""
    text = words(bd)
    if not text:
        return None
    if bd["overflow"] > 0:
        kind, amount = "over", bd["overflow"]
    else:
        kind, amount = "left", bd["unspent"]
    return {"kind": kind, "amount": float(amount), "pending": 0.0,
            "vs_pace": None, "text": text,
            "tone": "neg" if bd["overflow"] > 0 else "mut"}


def caption(bd: dict) -> str | None:
    """The row's one figure under the gauge: what the pools are still
    expected to pay out to month end ("$R still expected this month ·
    not counted in the verdict", the envelopes half of the month-end
    figure); None when nothing is (the row says nothing it has no figure
    for). The plan's set-aside is the pool list's total line."""
    if bd["reserve"] <= 0:
        return None
    return (f"{_money(bd['reserve'])} still expected this month · "
            "not counted in the verdict")


def _line(it: dict, kind: str) -> str:
    """A pool's line in the list: its own line first (pool_line), then
    what the period drew and what is left."""
    bits = [pool_line(it)]
    if kind == "year":
        if it["period_months"] > 1:
            bits += [f"{_money(it['drawn'])} over the plan's months",
                     f"{_money(it['left'])} left"]
    elif it["period_months"] > 1:
        bits += [f"{_money(it['drawn'])} "
                 f"{month_period(kind, it.get('_m') or 1)[1]}",
                 f"{_money(it['left'])} left"]
    else:
        bits.append(f"{_money(it['left'])} left")
    if it["unspent"] > 0 and kind in ("closed", "year"):
        bits.append(f"{_money(it['unspent'])} unspent")
    return " · ".join(bits)


def listed(it: dict) -> tuple:
    """The one order pools are printed in, wherever they are listed: by
    the name shown, then the stable id — two builds of the same data
    print the same text."""
    return (str(it.get("payee") or "").lower(), str(it.get("payee") or ""),
            it.get("uid") or "")


def table(bd: dict) -> dict:
    """The list the row opens: one line per pool ("Garden (yearly pool
    $960)": "$410 spent this year · pool $960 · $0 this month · $550
    left"), on the
    Year a signed line for each cause a yearly pool did not draw all of
    it (`_causes`), and the plan's set-aside. A list, not an equation: the pools' period
    figures and the set-aside are different quantities, and no sum
    between them is claimed."""
    rows = []
    for it in sorted(bd["items"], key=listed):
        kind = "yearly" if it["period_months"] > 1 else "monthly"
        label = (f"{it['payee']} ({kind} pool {_money(it['pool'])})"
                 + (" (no longer tracked)" if it["gone"] else ""))
        rows.append({"label": label, "text": _line(it, bd["kind"])})
    outside = []
    for it in sorted(bd["items"], key=listed):
        for label, d in it.get("causes") or ():
            outside.append({"label": f"{it['payee']}: {label}",
                            "amount": abs(d), "sign": "+" if d > 0 else "-",
                            "figure": ("+" if d > 0 else "−")
                            + f"${abs(d):,}"})
    return {"rows": rows, "outside": outside,
            "total": {"label": "Set aside in the plan",
                      "amount": bd["set_aside"],
                      "figure": _money(bd["set_aside"])}}


def sent(bd: dict | None) -> dict | None:
    """The breakdown as a reply carries it, with its list."""
    if bd is None:
        return None
    out = {k: bd[k] for k in SENT}
    out["items"] = [{k: it[k] for k in ITEM_SENT} for it in bd["items"]]
    out["table"] = table(bd)
    return out


def row(bd: dict, *, to: str | None) -> dict:
    """The Envelopes money-map row, printed from the breakdown: drawn /
    cap, unjudged, the caption, the words, and the list it opens."""
    return {"label": "Envelopes", "judged": False,
            "actual": float(bd["drawn"]), "expected": float(bd["drawn"]),
            "budget": float(bd["cap"]), "pending": 0.0, "to": to,
            "caption": caption(bd), "status": row_status(bd),
            "breakdown": sent(bd)}
