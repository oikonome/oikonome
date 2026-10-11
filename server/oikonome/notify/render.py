"""Short-form summary renders — SMS-sized (and push-sized) phrasings of
the same payloads the cadence emails render. Never a second computation:
each function takes the dict its email counterpart already built
(report.gather for daily, lenses.month_summary /
year_summary for the rest), so the numbers cannot disagree with the
email or the app.

Target shape: verdict line + remaining/over + one top
driver, 2-3 lines, ~160-300 chars. Push uses the same text with the
first line as the notification title.
"""

from __future__ import annotations



def _m(v: float | None) -> str:
    """Whole-dollar money — SMS has no room for cents."""
    v = v or 0
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def _sign(v: float) -> str:
    return f"+{_m(v)}" if v >= 0 else f"−{_m(abs(v))}"


def verdict_phrase(verdict: str) -> str:
    """'OVER BUDGET' -> 'OVER budget'. The engine's verdict already ends in
    the word, so every subject, push title and text that wants "budget"
    after the verdict goes through here rather than appending it again."""
    return f"{(verdict or '').upper().replace(' BUDGET', '')} budget"


def short_daily(d: dict, bucket_names: bool = True) -> str:
    """From report.gather()'s dict (the daily email's source).

    `bucket_names=False` keeps the household's own custom-bucket names out
    of the text: each carve-out's allowance is folded back into the Food or
    Everything-else figure it was carved from, so only the two fixed labels
    appear. The daily push takes this form, because its text leaves the
    instance for the publisher's relay and the phone platforms, and a
    bucket name is often a merchant or a person.

    The folded figure is the sum of the Today tiles it stands for, each
    floored at zero as the tile is: an overspent carve-out adds $0, never a
    negative. That is the Today page's own rule for every total it draws
    across tiles (todayview.simple_day_meter, and the web and app headline
    that mirror it): one bucket's overspend is not taken out of another's
    room. Netting the pieces here instead would make the push quote a
    smaller allowance than the tiles the household opens it to."""
    verdict = d.get("verdict", "")
    variance = d.get("variance", 0) or 0
    lines = []
    if verdict == "OVER BUDGET":
        lines.append(f"Oikonome: OVER budget by {_m(variance)}.")
    elif verdict == "UNDER BUDGET":
        lines.append(f"Oikonome: UNDER budget by {_m(abs(variance))}.")
    else:
        lines.append("Oikonome: ON budget.")
    # the one top driver — the largest over-pace bucket, same source as
    # the email's "Why" section
    reasons = d.get("reasons") or []
    if reasons:
        r = max(reasons, key=lambda x: x.get("variance", 0))
        if (r.get("variance") or 0) > 0:
            lines.append(f"{r['bucket']} {_sign(r['variance'])} vs pace.")
    # the number to hit: per-day allowance for the rest of the month, one
    # entry per Today tile. The tiles come from todayview.allowances — the
    # function the Today page, the email HTML and the plain text all render
    # — so a carve-out gets its own entry and the parent's number is the
    # carved one, exactly as the page shows it. Deriving it here from the
    # raw parent buckets would count a carve-out's spending twice and name
    # a bucket by its internal key.
    today = d.get("today")
    dim = d.get("days_in_month")
    buckets = d.get("buckets") or {}
    if (today is not None and dim
            and all("month_budget" in (buckets.get(k) or {})
                    for k in ("food", "other"))):
        from ..web.todayview import allowances
        days_left = max(1, dim - today.day + 1)
        allow, kids, tiles = allowances(d)
        if bucket_names:
            rates = [f"{_m(a['rate'])}/day {label}" for label, a in tiles]
        else:
            # allowances lists Food's carve-outs before Everything else's;
            # each rate is already floored at zero, as its tile shows it
            n_food = len(buckets["food"].get("children") or [])
            folded = (("Food", allow["food"]["rate"]
                       + sum(k["rate"] for k in kids[:n_food])),
                      ("Everything else", allow["other"]["rate"]
                       + sum(k["rate"] for k in kids[n_food:])))
            rates = [f"{_m(r)}/day {label}" for label, r in folded]
        if rates:
            lines.append("Spend at most " + ", ".join(rates)
                         + f" — {days_left} days left.")
    return "\n".join(lines)


def push_daily(d: dict) -> str:
    """The daily push's one visible line, from the same source as the
    SMS: the verdict first, then the top driver and the daily allowance,
    joined on one line without the "Oikonome:" prefix (the notification's
    title already says who it is from). The relay shows at most 160
    characters; the verdict leads so a cut never loses it. No custom-bucket
    name rides along: the relay and the phone platforms see only the
    verdict, the driver and the two fixed allowance labels."""
    lines = [ln.strip().rstrip(".") for ln in short_daily(
        d, bucket_names=False).split("\n")
             if ln.strip()]
    if lines and lines[0].startswith("Oikonome: "):
        lines[0] = lines[0][len("Oikonome: "):]
    return " · ".join(lines)


def short_monthly(d: dict, label: str) -> str:
    """From lenses.month_summary() — the monthly email's source."""
    verdict = d.get("verdict", "")
    variance = d.get("variance", 0) or 0
    return (f"Oikonome, {label}: {verdict_phrase(verdict)} "
            f"({_sign(variance)}).\n"
            f"Variable {_m(d.get('variable_actual'))} of "
            f"{_m(d.get('variable_budget'))}; total out "
            f"{_m(d.get('spend_total'))}.")


def short_yearly(d: dict, year: int) -> str:
    """From lenses.year_summary() — the annual email's source."""
    cells = [c for c in d.get("cells", []) if c.get("verdict")]
    on = sum(1 for c in cells
             if c["verdict"] in ("ON BUDGET", "UNDER BUDGET"))
    t = d.get("totals") or {}
    lines = [f"Oikonome {year}: on budget {on} of {len(cells)} months."]
    if t.get("spend") is not None:
        line = f"Spent {_m(t['spend'])}"
        if t.get("income") is not None:
            line += f" on {_m(t['income'])} income"
            if t.get("saved") is not None:
                line += f" — saved {_m(t['saved'])}"
                if t.get("rate") is not None:
                    line += f" ({t['rate']:.0f}%)"
        lines.append(line + ".")
    return "\n".join(lines)
