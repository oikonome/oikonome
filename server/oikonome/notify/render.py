"""Short-form summary renders — SMS-sized (and push-sized) phrasings of
the same payloads the cadence emails render. Never a second computation:
each function takes the dict its email counterpart already built
(report.gather for daily, lenses.week_summary / month_summary /
year_summary for the rest), so the numbers cannot disagree with the
email or the app.

Target shape: verdict line + remaining/over + one top
driver, 2-3 lines, ~160-300 chars. Push uses the same text with the
first line as the notification title.
"""

from __future__ import annotations

import datetime as dt


def _m(v: float | None) -> str:
    """Whole-dollar money — SMS has no room for cents."""
    v = v or 0
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def _sign(v: float) -> str:
    return f"+{_m(v)}" if v >= 0 else f"−{_m(abs(v))}"


def short_daily(d: dict) -> str:
    """From report.gather()'s dict (the daily email's source)."""
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
    # the number to hit: per-day allowance for the rest of the month.
    # Derived from the keys report.gather() ACTUALLY carries (today,
    # days_in_month, buckets[k].month_budget/actual) — mirroring
    # todayview.build_context so the SMS number matches the email exactly.
    # Reading a key gather does not produce would drop this line silently,
    # so it is derived from the keys the dict really carries.
    today = d.get("today")
    dim = d.get("days_in_month")
    buckets = d.get("buckets") or {}
    if today is not None and dim:
        days_left = max(1, dim - today.day + 1)
        rates = []
        for k, lbl in (("food", "food"), ("other", "else")):
            b = buckets.get(k) or {}
            if "month_budget" in b:
                rate = max(0.0, (b["month_budget"] - b.get("actual", 0))
                           / days_left)
                rates.append(f"{_m(rate)}/day {lbl}")
        if rates:
            lines.append("Spend at most " + ", ".join(rates)
                         + f" — {days_left} days left.")
    return "\n".join(lines)


def push_daily(d: dict) -> str:
    """The daily push's one visible line, from the same source as the
    SMS: the verdict first, then the top driver and the daily allowance,
    joined on one line without the "Oikonome:" prefix (the notification's
    title already says who it is from). The relay shows at most 160
    characters; the verdict leads so a cut never loses it."""
    lines = [ln.strip().rstrip(".") for ln in short_daily(d).split("\n")
             if ln.strip()]
    if lines and lines[0].startswith("Oikonome: "):
        lines[0] = lines[0][len("Oikonome: "):]
    return " · ".join(lines)


def short_weekly(d: dict, start: dt.date) -> str:
    """From lenses.week_summary() — the weekly email's source."""
    verdict = d.get("verdict", "")
    variance = d.get("variance", 0) or 0
    head = (f"Oikonome, week of {start:%m/%d}: {verdict.replace(' BUDGET', '')}"
            f" budget ({_sign(variance)}).")
    return (head + f"\nVariable spend {_m(d.get('actual'))} of "
                   f"{_m(d.get('week_budget'))} planned.")


def short_monthly(d: dict, label: str) -> str:
    """From lenses.month_summary() — the monthly email's source."""
    verdict = d.get("verdict", "")
    variance = d.get("variance", 0) or 0
    return (f"Oikonome, {label}: {verdict.replace(' BUDGET', '')} budget "
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
