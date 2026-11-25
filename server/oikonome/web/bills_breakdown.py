"""A month's due-dated bills in parts that add up to its plan — the ONE
source of every bills figure a page prints beside the Bills row, and of
the table the row opens (specs/today-figures.md, "The Bills row is one
breakdown"). An envelope is not a bill here: the plan is the month's
occurrences (`fixed.occ_budget`), posted their payments
(`fixed.occ_actual`), and the pools are the Envelopes row's
(envelope_breakdown).

    plan = posted − refunds + due + not_paid + under      (whole dollars)

`late` (a bill of an earlier month the running month's walk still
carries) is still due and in none of this month's plan; the bar's right
figure on a running month is posted + due + late. On the Year a late
bill one of its months planned is inside the plan, and a bill whose due
date moved into the running month after its month closed is `moved`.

`running` reads the running month, `closed` a month that is over,
`future` one to come, and `year` folds a Year lens's months. The row
prints two figures, posted (`caption`) and still due (`words`,
`row_status`); `table` writes how they add up to the plan, and
`apply_to_row` puts all of it on a money-map row. Nothing else computes
a bills part.
"""

from __future__ import annotations

import calendar
import datetime as dt

from ..engine.reporting import shown_dollars, whole_dollars

# the keys a reply carries (schemas.today.BillsBreakdown); the rest is
# what `year` needs to fold the months and stays on the server
SENT = ("kind", "plan", "posted", "refunds", "due", "awaiting", "late",
        "not_paid", "unpaid", "moved", "under", "still_due", "total",
        "text", "settled", "past", "y")


def _money(d: int) -> str:
    return f"-${-d:,}" if d < 0 else f"${d:,}"


def _iso(d) -> str:
    return d if isinstance(d, str) else d.isoformat()


def _cents(v: float) -> int:
    return int(round(float(v) * 100))


def _whole(values: list[float], target: int) -> list[int]:
    """`values` in whole dollars that add up to `target`, in ONE largest
    remainder pass, where a part exact in cents (a $600.00 payment, and
    nothing) prints exactly; the rest carry the rounding."""
    out = [_cents(v) // 100 if _cents(v) % 100 == 0 else None
           for v in values]
    loose = [i for i, d in enumerate(out) if d is None]
    fixed = sum(d for d in out if d is not None)
    if loose:
        for i, d in zip(loose, shown_dollars([values[i] for i in loose], 0,
                                             dollars=target - fixed)):
            out[i] = d
    return out


def _item(payee, cents: float, due, bill=None, uid=None,
          dates=None) -> dict:
    day = dt.date.fromisoformat(_iso(due)[:10])
    from ..engine.budget import bill_identity
    out = {"payee": str(payee), "cents": float(cents),
           "due": day.isoformat(), "y": day.year, "m": day.month,
           "bill": bill or bill_identity(str(payee)),
           "uid": uid if isinstance(uid, str) and uid else None}
    if dates is not None:
        # the dates the bill's CURRENT schedule has in the due month
        out["dates"] = [_iso(d)[:10] for d in dates]
    return out


def same_bill(a: dict, b: dict) -> bool:
    """Whether two occurrences (`_item`s, or the running month's own
    entries) are of the same bill: by the stable id (bills.uid) both
    recorded, which nothing rewrites. Only where one side recorded none —
    a month whose schedule was frozen before bills had one — by the id
    the bill had then (budget.bill_identity, its payee's slug), which a
    rename since has rewritten. The one place that fallback is read."""
    if a.get("uid") and b.get("uid"):
        return a["uid"] == b["uid"]
    return a["bill"] == b["bill"]


def from_parts(kind: str, *, plan: float, posted: float, due: float = 0.0,
               awaiting: float = 0.0, late=(), unpaid=(), covered=(),
               own=(), paid=(), still_due_dollars: int | None = None) -> dict:
    """The breakdown from its figures in cents. `late`, `unpaid` and
    `covered` are (payee, planned, due date[, bill identity[, stable
    id[, the dates the bill's current schedule has in its due month (a
    late one)]]]) of a
    carried occurrence, an occurrence no payment met, and one left out of
    `unpaid` because its bill was paid on another date (still in the
    plan's remainder); `own` the running month's own occurrences (bill
    identity, planned, due date, match window, stable id, the dates the
    bill's current schedule has in the match window before the month),
    which `year` reads; `paid` (payee, due date, bill identity, stable
    id) the occurrences a closed month counted paid, which `fold` reads.

    Posted, due, each unpaid bill and the remainder are apportioned in
    ONE pass to the plan's whole dollars (a part exact in cents prints
    exactly); the late bills in one pass of their own. On a running month
    `still_due_dollars` is the bills part of report.month_end_parts: the
    late bills and `due` then print exactly that figure between them
    (`due` takes what the late bills leave), and posted, the unpaid bills
    and the remainder the plan's rest. So every printed sum holds as
    printed."""
    late_items = [_item(*x) for x in late]
    unpaid_items = [_item(*x) for x in unpaid]
    covered_items = [_item(*x) for x in covered]
    late_d = _whole([i["cents"] for i in late_items],
                    whole_dollars(sum(i["cents"] for i in late_items)))
    rest = plan - posted - due - sum(i["cents"] for i in unpaid_items)
    vals = [posted, due] + [i["cents"] for i in unpaid_items] + [rest]
    if still_due_dollars is None:
        out = _whole(vals, whole_dollars(plan))
    else:
        # the late bills keep their own pass; this month's own part is
        # what the printed still-due figure leaves of them (never below
        # nothing: a unit short comes off the largest late bill)
        d_d = int(still_due_dollars) - sum(late_d)
        while d_d < 0 and late_d and max(late_d) > 0:
            late_d[late_d.index(max(late_d))] -= 1
            d_d += 1
        d_d = max(0, d_d)
        sub = _whole([vals[0]] + vals[2:], whole_dollars(plan) - d_d)
        # every part exact in cents with `due` pinned off its own rounding:
        # the remainder takes the dollar, so the parts add up to the plan
        sub[-1] = whole_dollars(plan) - d_d - sum(sub[:-1])
        out = [sub[0], d_d] + sub[1:]
    # a unit the pass took back never leaves posted, due or a bill below
    # nothing it was not already below: it comes off the remainder instead
    for i in range(len(out) - 1):
        if out[i] < 0 <= _cents(vals[i]):
            out[-1] += out[i]
            out[i] = 0
    p_d, d_d = out[:2]
    # refunds past the charges (posted below nothing) are their own part:
    # posted prints nothing, never a figure below it
    refunds, p_d = max(0, -p_d), max(0, p_d)
    for it, d in zip(unpaid_items, out[2:-1]):
        it["amount"] = d
    for it, d in zip(late_items, late_d):
        it["amount"], it["inside_plan"] = max(d, 0), False
    for it in covered_items:
        it["amount"] = whole_dollars(it["cents"])
    bd = {"kind": kind, "plan": whole_dollars(plan), "posted": p_d,
          "refunds": refunds, "due": d_d, "awaiting": max(0, min(whole_dollars(awaiting), d_d)),
          "late": late_items,
          "not_paid": sum(i["amount"] for i in unpaid_items),
          "unpaid": unpaid_items, "moved": 0,
          "moved_items": [], "under": out[-1], "covered": covered_items,
          "own": [dict(bill=b, amount=whole_dollars(a), due=_iso(d)[:10],
                       window=w, uid=u, before=list(x))
                  for b, a, d, w, u, x in own],
          "still_due_cents": round(due + sum(i["cents"] for i in late_items),
                                   2)}
    # the occurrences the month decided that another month may also read
    # (server-only, outside SENT): each keyed (occurrence_key) and homed in
    # its due month, the one month that plans an unpaid occurrence
    # (budget.occurrence_home), with what this month decided of it
    bd["rows"] = ([dict(i, state="not_paid") for i in unpaid_items]
                  + [dict(i, state="covered") for i in covered_items]
                  + [dict(i, state="late") for i in late_items]
                  + [dict(_item(p, 0.0, d, b, u), state="paid")
                     for p, d, b, u in paid])
    return _finish(bd)


def _finish(bd: dict) -> dict:
    # settled: nothing more will post against it — a month that is over,
    # or a Year with no month running or to come
    bd.setdefault("settled", bd["kind"] == "closed")
    bd.setdefault("past", False)
    bd.setdefault("y", None)
    bd["still_due"] = bd["due"] + sum(i["amount"] for i in bd["late"])
    bd["total"] = (bd["posted"] + bd["still_due"] if bd["kind"] == "running"
                   else bd["plan"])
    bd["text"] = words(bd)
    return bd


def _plan(fixed: dict) -> float:
    """The month's due-dated plan: its occurrences, no envelope share (a
    status from before the split has only the combined figure)."""
    return fixed.get("occ_budget", fixed["month_budget"])


def _posted(fixed: dict) -> float:
    """What the month's occurrences posted, no envelope draw."""
    return fixed.get("occ_actual", fixed["actual"])


def running(conn, cfg: dict, st: dict, today: dt.date, *,
            memo: dict | None = None, parts: dict | None = None) -> dict:
    """The running month (`st` its month_status): the walk read ONCE —
    report.month_end_parts (`parts`, read here when not given), the reads
    report.bills_remaining_month sums — whose bills part is the row's
    still-due figure, so the Bills row, the Bills page tile and the cash
    tile's "bills $X" print one figure."""
    from . import report
    from ..engine import budget
    from ..engine.budget import bill_identity
    first = today.replace(day=1)
    fixed = st["buckets"]["fixed"]
    envs = fixed.get("envelopes") or []
    if parts is None:
        parts = report.month_end_parts(conn, cfg, today, envs, memo=memo)
    occs = parts["occs"]

    def ident(o):
        b = o["occ"]["bill"]
        return b.get("id") or bill_identity(b.get("payee"))

    def uid(o):
        return o["occ"]["bill"].get("uid")

    def before(o):
        """The dates the bill's CURRENT schedule has in the match window
        before this month: a closed month's unpaid date among them is
        still the bill's own date, never one moved into this month."""
        lo = o["occ"]["due"] - dt.timedelta(
            days=max(o["occ"].get("window") or 0, 1))
        out, y, m = [], lo.year, lo.month
        while (y, m) < (first.year, first.month):
            out += [d.isoformat() for d in budget._occurrences_in_month(
                o["occ"]["bill"], y, m) if lo <= d < first]
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return out
    own = [o for o in occs if o["occ"]["due"] >= first]

    def dates(o):
        d = o["occ"]["due"]
        return budget._occurrences_in_month(o["occ"]["bill"], d.year,
                                            d.month)
    return from_parts(
        "running", plan=_plan(fixed), posted=_posted(fixed),
        due=sum(o["planned"] for o in own),
        awaiting=fixed.get("unpaid_due") or 0,
        still_due_dollars=parts["bills"],
        late=[(o["payee"], o["planned"], o["occ"]["due"], ident(o), uid(o),
               dates(o))
              for o in occs if o["occ"]["due"] < first],
        own=[(ident(o), o["planned"], o["occ"]["due"],
              o["occ"].get("window") or 0, uid(o), before(o)) for o in own])


def closed(st: dict) -> dict:
    """A month that is over, from its own state: nothing in it is still
    due; each occurrence no payment met is not paid — except a bill paid
    on another of its dates in the month (`paid_bills`), which is never
    also not paid (that occurrence is `covered`)."""
    from ..engine import budget
    fixed = st["buckets"]["fixed"]
    paid = set(st.get("paid_bills") or ())
    entries = budget.overdue_entries(st)
    ids = st.get("overdue_bill_ids")
    if not isinstance(ids, list) or len(ids) != len(entries):
        ids = [None] * len(entries)
    uids = st.get("overdue_bill_uids")
    if not isinstance(uids, list) or len(uids) != len(entries):
        uids = [None] * len(entries)
    # a status stored before it named its paid occurrences names none
    paid_occs = [tuple(x) for x in st.get("paid_occurrences") or ()
                 if isinstance(x, (list, tuple)) and len(x) == 4]
    return from_parts(
        "closed", plan=_plan(fixed), posted=_posted(fixed),
        unpaid=[(p, a, d, b, u) for (p, a, d, _g), b, u
                in zip(entries, ids, uids) if p not in paid],
        covered=[(p, a, d, b, u) for (p, a, d, _g), b, u
                 in zip(entries, ids, uids) if p in paid],
        paid=paid_occs)


def future(st: dict) -> dict:
    """A month to come: its plan is still due, less anything already
    posted against it."""
    fixed = st["buckets"]["fixed"]
    plan, posted = _plan(fixed), _posted(fixed)
    return from_parts("future", plan=plan, posted=posted,
                      due=max(0.0, plan - posted))


def _same_occurrence(a: dict, b: dict) -> bool:
    """Whether `a` and `b` are one occurrence: one key, the bill's stable
    id (bills.uid) and the scheduled due date. Where one side
    recorded no stable id, its bill's id then (same_bill), or — the
    payee spelled another way since — the same due date alone; two
    entries that each carry a stable id and differ are two bills, whatever
    their dates. Never a nearest date."""
    if a["due"] != b["due"]:
        return False
    if a.get("uid") and b.get("uid"):
        return a["uid"] == b["uid"]
    return True


def _paid_by(p: dict, li: dict) -> bool:
    """Whether the closed month's paid row `p` is the late row `li`'s
    occurrence: the same due date and the same bill — by the stable id
    both carry, else by the bill's id or its payee."""
    if p["due"] != li["due"]:
        return False
    if p.get("uid") and li.get("uid"):
        return p["uid"] == li["uid"]
    return p["bill"] == li["bill"] or p["payee"] == li["payee"]


def fold(months: list[dict]) -> dict:
    """The year's occurrences, each counted once, from its budgeted
    months' rows (`rows` of each breakdown; "y"/"m" of each month).

    Every row is keyed (_same_occurrence) and homed in its due month. A key
    is decided by its home month — not paid, or `covered` (its bill paid
    on another of the month's dates) — and, while it is still unpaid, by
    the running month's walk, which carries it as late: the later
    reading wins, so a late occurrence the year planned is still due,
    not "not paid", and counted once. A late occurrence whose home month
    the year holds no plan for is outside the plan.

    One cross-key rule: a bill whose date was changed after its month
    closed (`dates`, the current schedule's dates in that month, no
    longer hold the closed month's date) carries the new date late, and
    that late occurrence is the closed month's unpaid one, re-dated —
    paired in date order, of the same bill (same_bill: by the stable id,
    or the id a month frozen before stable ids recorded).

    A late row whose occurrence a closed month counted paid (its `paid`
    rows: the payment that month bound to it) is not late at all: the
    closed month decided it, and the walk, reading today's schedule, only
    missed the payment. It is left out (`paid_late`).

    Returns {"late": [late item + inside_plan, home row], "left": {(y, m):
    the home month's rows not carried late}, "paid_late": [late rows a
    closed month paid]}."""
    homes = {(mo["y"], mo["m"]): [r for r in mo.get("rows") or ()
                                  if r["state"] in ("not_paid", "covered")]
             for mo in months if mo["kind"] == "closed"}
    paid = [r for mo in months if mo["kind"] == "closed"
            for r in mo.get("rows") or () if r["state"] == "paid"]
    run = next((mo for mo in months if mo["kind"] == "running"), None)
    # an occurrence a closed month counted paid is paid: that month's word
    # stands for its own month, so the walk never carries it late here
    carried, paid_late = [], []
    for r in (run or {}).get("rows") or ():
        if r["state"] != "late":
            continue
        (paid_late if any(_paid_by(p, r) for p in paid)
         else carried).append(r)
    placed, stray = [], []
    for li in carried:
        pool = homes.get((li["y"], li["m"]))
        same = [r for r in pool or () if _same_occurrence(r, li)]
        # several legacy rows on one date: the late bill's own row first
        # (same_bill: a row of another bill is that bill's, whatever its
        # state), then a not-paid one, then its own amount
        same.sort(key=lambda r: (not same_bill(r, li),
                                 r["state"] != "not_paid",
                                 r["amount"] != li["amount"]))
        if same:
            pool.remove(same[0])
            placed.append((li, same[0]))
        else:
            stray.append(li)
    # re-dated inside its month: the home rows of the bill whose dates the
    # schedule no longer has, and the late dates no row owns, in order
    for li in sorted(stray, key=lambda i: i["due"]):
        pool = homes.get((li["y"], li["m"]))
        gone = sorted((r for r in pool or ()
                       if same_bill(r, li) and li.get("dates") is not None
                       and r["due"] not in li["dates"]),
                      key=lambda r: r["due"])
        if gone:
            pool.remove(gone[0])
            placed.append((li, gone[0]))
        else:
            placed.append((li, None))
    order = {id(li): n for n, li in enumerate(carried)}
    placed.sort(key=lambda p: order[id(p[0])])
    return {"late": placed, "left": homes, "paid_late": paid_late}


def year(months: list[dict], *, past: bool | None = None) -> dict | None:
    """A Year lens's Bills row: the sum of its budgeted months'
    breakdowns (`months`: each a breakdown with its "y" and "m"), its
    occurrences counted once by key (`fold`).

    - A late bill the running month carries leaves its home month's not
      paid (or, for a bill paid on its other dates, that month's plan
      remainder) for the year's late part: counted once. One no budgeted
      month holds is outside this year's plan and said apart.
    - A closed month's unpaid occurrence whose bill now falls due in the
      running month within its match window, and whose CURRENT schedule
      no longer has that closed date (its due date was moved after the
      month closed), is that same bill — known only by the stable id both
      recorded, never by payee — : it leaves the closed month's not
      paid as `moved`, planned there and owed here once. A date the
      schedule still has is a miss beside the next real occurrence and
      stays where its month put it.
    None without a month."""
    if not months:
        return None
    closed = sorted([mo for mo in months if mo["kind"] == "closed"],
                    key=lambda mo: (mo["y"], mo["m"]))
    run = next((mo for mo in months if mo["kind"] == "running"), None)
    not_paid = sum(mo["not_paid"] for mo in months)
    under = sum(mo["under"] for mo in months)
    folded = fold(months)
    late = []
    for li, home in folded["late"]:
        if home is None:
            late.append(dict(li, inside_plan=False))
            continue
        if home["state"] == "not_paid":
            not_paid -= home["amount"]
            under += home["amount"]
        under -= li["amount"]
        late.append(dict(li, inside_plan=True))
    left = folded["left"]
    moved, moved_items = 0, []
    if run is not None and closed:
        prev = closed[-1]
        pool = left[(prev["y"], prev["m"])]
        own = list(run.get("own") or [])
        for c in [r for r in pool if r["state"] == "not_paid"] \
                + [r for r in pool if r["state"] == "covered"]:
            day = dt.date.fromisoformat(c["due"])
            # moved only between two entries that both carry the
            # bill's stable id: a month frozen before bills had one
            # names it by payee, and a payee deleted and made again is
            # a different bill whose new date is no moved old one
            o = next((o for o in own if o.get("uid") and c.get("uid")
                      and same_bill(o, c)
                      and c["due"] not in o["before"]
                      and 0 < (dt.date.fromisoformat(o["due"]) - day).days
                      <= max(o["window"], 1)), None)
            if o is None:
                continue
            own.remove(o)
            pool.remove(c)
            if c["state"] == "not_paid":
                not_paid -= c["amount"]
            else:
                under -= c["amount"]
            moved += c["amount"]
            moved_items.append(c)
    # posted net of the months' refunds past their charges: refunds are
    # said only when the year as a whole refunded more than it was charged
    net = sum(mo["posted"] - mo["refunds"] for mo in months)
    # finished: a year that is over, or one whose every budgeted month is
    # closed
    finished = bool(past) or all(mo["kind"] == "closed" for mo in months)
    bd = {"kind": "year", "plan": sum(mo["plan"] for mo in months),
          "settled": finished,
          # a year that is over (`past`; without it, one whose every month
          # is closed): named by its number, never "this year"
          "past": (past if past is not None
                   else all(mo["kind"] == "closed" for mo in months)),
          "y": max(mo["y"] for mo in months),
          "posted": max(0, net), "refunds": max(0, -net),
          "due": run["due"] if run else 0,
          "awaiting": run["awaiting"] if run else 0,
          "late": late, "not_paid": not_paid,
          "unpaid": [r for k in sorted(left) for r in left[k]
                     if r["state"] == "not_paid"],
          "moved": moved, "moved_items": moved_items,
          "under": under, "covered": [], "own": [], "rows": [],
          "paid_late": folded["paid_late"]}
    return _finish(bd)


def nothing_dated(bd: dict | None) -> bool:
    """A period with no due-dated bill at all — nothing planned, posted,
    refunded, still due, late, not paid or moved: its map has no Bills
    row and its emails no Bills bar (specs/today-figures.md, "A period
    with no dated bill has no Bills row")."""
    return (bd is not None and not any(
        bd[k] for k in ("plan", "posted", "refunds", "still_due", "not_paid",
                        "moved"))
            and not bd["late"])


def months_words(items: list[dict]) -> str:
    """The months a set of bills was due in, each with its year: "Mar
    2026", "Feb and Mar 2026", "Dec 2025 and Jan 2026"."""
    def join(xs):
        return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]
    keys = sorted({(i["y"], i["m"]) for i in items})
    years = sorted({y for y, _m in keys})
    return join([join([calendar.month_abbr[m] for y2, m in keys if y2 == y])
                 + f" {y}" for y in years])


def words(bd: dict) -> str | None:
    """The words beside the Bills row on every surface — what an older
    phone reads, and the row's only words where there is no table. In
    order, two phrases at most: its one still-due figure ("$D still due"
    — this month's own bills and any bill late from an earlier month);
    on a settled month or year, "$R refunded" when bill refunds came to
    more than the charges, then "$N not paid"; "over plan by $X"
    whenever `under` is below zero, else, settled, "$U under plan".
    Nothing when there is nothing to say. How the figures add up to the plan is the
    table's (`table`), never a sentence."""
    out = []
    if bd["still_due"] > 0:
        out.append(f"{_money(bd['still_due'])} still due")
    if bd["settled"] and bd["refunds"] > 0:
        out.append(f"{_money(bd['refunds'])} refunded")
    if bd["settled"] and bd["not_paid"] > 0:
        out.append(f"{_money(bd['not_paid'])} not paid")
    if bd["under"] < 0:
        out.append(f"over plan by {_money(-bd['under'])}")
    elif bd["settled"] and bd["under"] > 0:
        out.append(f"{_money(bd['under'])} under plan")
    return " · ".join(out[:2]) or None


def _month_year(item: dict, *, day: bool = False) -> str:
    day_ = dt.date.fromisoformat(item["due"]).day if day else None
    return (f"{calendar.month_abbr[item['m']]} {day_}, {item['y']}" if day
            else f"{calendar.month_abbr[item['m']]} {item['y']}")


def table(bd: dict) -> dict:
    """"How this adds up": the breakdown as the rows a reader adds, the
    ONE builder of the table every surface opens from the Bills row.

    `rows` are in order, each a label, a whole-dollar `amount` (never
    below nothing) and its `sign` — "+" added, "-" taken off — ending in
    the total row (sign "="), which is the plan as printed: the signed
    rows add up to it to the dollar. `figure` is each row as printed
    ("+$1,000", "−$35", "$1,965"). A bill late from an earlier month is
    still due and in none of the plan's months: it is listed under
    `outside_heading` in `outside`, with its month and year, and is in
    no sum. The Year's table sums its months (`year`)."""
    yr = bd["kind"] == "year"
    run = bd["kind"] == "running"
    rows: list[dict] = []

    def add(label: str, value: int, sign: str = "+", *, keep=False,
            into: list | None = None):
        """One signed row; a bill missed on several dates (a weekly one)
        is one row for the bill, its dates' dollars added."""
        into = rows if into is None else into
        same = next((r for r in into if r["label"] == label), None)
        if same is not None:
            value += same["amount"] if same["sign"] == sign \
                else -same["amount"]
            into.remove(same)
        if value == 0 and not keep:
            return
        if value < 0:
            value, sign = -value, "-" if sign == "+" else "+"
        into.append({"label": label, "amount": int(value), "sign": sign,
                     "figure": ("−" if sign == "-" else "+")
                     + f"${int(value):,}"})

    late_in = [i for i in bd["late"] if yr and i.get("inside_plan")]
    late_out = [i for i in bd["late"] if not (yr and i.get("inside_plan"))]
    # a bill late on two dates of one month, one inside the plan and one
    # outside it, names each date, so no label is both above the total
    # and below it
    both = ({(i["y"], i["m"], i["payee"]) for i in late_in}
            & {(i["y"], i["m"], i["payee"]) for i in late_out})

    def dated_label(i):
        dated = (i["y"], i["m"], i["payee"]) in both
        return f"Late from {_month_year(i, day=dated)}: {i['payee']}"
    # ...and two different bills of one payee late on one date, one
    # inside and one outside, carry their amounts
    clash = ({dated_label(i) for i in late_in}
             & {dated_label(i) for i in late_out})

    def late_label(i):
        lab = dated_label(i)
        return f"{lab} (${int(i['amount']):,})" if lab in clash else lab
    past = yr and bd.get("past")
    add(f"Paid in {bd['y']}" if past else "Paid this year" if yr
        else "Paid" if bd["kind"] == "closed" else "Paid so far",
        bd["posted"], keep=True)
    add("Refunds beyond charges", bd["refunds"], "-")
    # "still due" names one figure on a page — the row's, this month's own
    # bills and the late ones together — so once a late bill is part of
    # it, this month's own part takes another name
    add("Due this month, not paid yet" if bd["late"] else "Still due",
        bd["due"])
    for i in late_in:
        add(late_label(i), i["amount"])
    if yr:
        add("Not paid" if past else "Not paid in earlier months",
            bd["not_paid"])
    else:
        for i in bd["unpaid"]:
            add(f"Not paid: {i['payee']}", i["amount"])
    if bd.get("moved"):
        add(f"Planned in {months_words(bd['moved_items'])}, now due this "
            "month", bd["moved"])
    if bd["under"] >= 0:
        add("Under plan", bd["under"])
    else:
        add("Over plan", -bd["under"], "-")
    plan = bd["plan"]
    rows.append({"label": ("Plan for the year's budgeted months" if yr
                           else "This month's plan" if run
                           else "The month's plan"),
                 "amount": plan, "sign": "=", "figure": _money(plan).replace("-", "−")})
    outside: list[dict] = []
    for i in late_out:
        add(late_label(i), i["amount"], into=outside)
    return {"rows": rows,
            "outside_heading": (None if not outside
                                else "Also still due, from outside this "
                                     "year's plan" if yr
                                else "Also still due, from earlier months "
                                     "(not in this plan)"),
            "outside": outside}


def sent(bd: dict | None) -> dict | None:
    """The breakdown as a reply carries it, with its table."""
    if bd is None:
        return None
    out = {k: bd[k] for k in SENT}
    out["late"] = [{k: i[k] for k in ("payee", "amount", "due", "y", "m",
                                      "inside_plan")} for i in bd["late"]]
    out["unpaid"] = [{k: i[k] for k in ("payee", "amount", "due")}
                     for i in bd["unpaid"]]
    out["table"] = table(bd)
    return out


def row_status(bd: dict) -> dict | None:
    """The Bills row's status: the words (`words`) as `text`, and `kind`
    / `amount` in the meanings an older phone reads them by — `due` with
    the still-due total and its awaiting part (`pending`), else `over` by
    what the plan was exceeded by, else `left` by what it had left.
    Nothing (None) when there is nothing to say."""
    text = words(bd)
    if not text:
        return None
    if bd["still_due"] > 0:
        kind, amount = "due", bd["still_due"]
    elif bd["under"] < 0:
        kind, amount = "over", -bd["under"]
    else:
        kind, amount = "left", max(bd["under"], 0)
    return {"kind": kind, "amount": float(amount),
            "pending": float(bd["awaiting"]) if kind == "due" else 0.0,
            "vs_pace": None, "text": text,
            "tone": "neg" if bd["under"] < 0 else "mut"}


def caption(bd: dict) -> str:
    """The Bills row's caption: posted — "posted this year" on the Year,
    "posted in 2025" on a past one — with the awaiting part of still due
    (due already, not posted) when there is one, and, on a running month,
    whose right figure is posted + still due, the plan."""
    if bd["kind"] == "year":
        head = (f"posted in {bd['y']}" if bd.get("past")
                else "posted this year")
    else:
        head = "posted"
    return (f"{head} {_money(bd['posted'])}"
            + (f" · {_money(bd['awaiting'])} awaiting"
               if bd["awaiting"] > 0 else "")
            + (f" · plan {_money(bd['plan'])}" if bd["kind"] == "running"
               else "")
            + " · not counted in the verdict")


def apply_to_row(row: dict, bd: dict) -> None:
    """A money-map Bills row printed from the breakdown, in place: posted
    / total, the still-due words beside it, the caption, and the
    breakdown (with its table) for the row to open."""
    row["breakdown"] = sent(bd)
    row["actual"] = float(bd["posted"])
    row["budget"] = float(bd["total"])
    row["pending"] = float(bd["awaiting"])
    row["caption"] = caption(bd)
    row["status"] = row_status(bd)


def card_lines(bd: dict) -> list[str]:
    """The Month lens's "paid vs planned" card's lines: a closed month's
    bills not paid, each by name; nothing for any other month (its parts
    are the Bills row's table)."""
    if bd["kind"] != "closed":
        return []
    # one order, whatever order the rows were read in: by name, then the
    # bill's stable id
    return [f"not paid: {i['payee']} {_money(i['amount'])}"
            for i in sorted(bd["unpaid"], key=lambda i: (
                str(i.get("payee") or "").lower(), str(i.get("payee") or ""),
                str(i.get("uid") or ""), str(i.get("due") or "")))]
