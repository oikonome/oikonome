"""How close the forecast came, against the World's truth.

For each forecast made on day `d` and each horizon `h`, `P` is the walk's
balance at the end of `d + h` and `A` the true end-of-day available
checking balance that day; `e = P − A` (positive = rosier than reality).
Every amount is divided by the household's scale `I` (World.scale: its
mean monthly income, or its mean monthly spending when larger), so
households of every size read on one scale.

The reply is read defensively: every walk the engine sends (a top-level
object with a `series`) is scored — a product scenario under the key every
surface names it with (usual, now, due: todayview.CASH_SCENARIOS), any
other walk under the engine's own name — so a scenario added later is
measured the day it appears. The headline is "usual" when the engine sends
it, else `pace`. A band is read from `band.lo` when the engine sends one,
else from a `pace_low` walk, else there is none. Each scenario's
heavier-month line (`heavy`) is scored by how often the truth falls below
it (`heavy_below_<key>_<h>`; the line means about one time in five).
"""

from __future__ import annotations

import datetime as dt
import statistics

HORIZONS = (7, 14, 30, 45, 60)
HEADLINE = ("usual", "pace")
DAY = dt.timedelta(days=1)
# the scenarios the product shows, by the key every surface names them with
# (todayview.CASH_SCENARIOS: key, words, the walk it reads); this copy is
# the fallback for an engine that does not say
_SCENARIOS = (("usual", "pace_stmt"), ("now", "pace_now"),
              ("due", "pace_due"))


def scenario_keys() -> dict[str, str]:
    """{walk name: scenario key} as the engine under test names them."""
    try:
        from oikonome.web import todayview
        got = {walk: key for key, _words, walk in todayview.CASH_SCENARIOS}
        if got:
            return got
    except Exception:                                    # noqa: BLE001
        pass
    return {walk: key for key, walk in _SCENARIOS}


def walks(fc: dict) -> dict[str, list]:
    """{name: series} for every walk in the reply: a product scenario under
    its key (usual, now, due), every other walk under the engine's name."""
    keys = scenario_keys()
    out = {}
    for k, v in fc.items():
        if isinstance(v, dict) and isinstance(v.get("series"), list) \
                and v["series"]:
            out[keys.get(k, k)] = v["series"]
    return out


def heavy_lines(fc: dict) -> dict[str, dict]:
    """{scenario key: {day: balance}} of each scenario's heavier-month line
    (`heavy`), when the engine sends them."""
    heavy = fc.get("heavy")
    if not isinstance(heavy, dict):
        return {}
    return {k: _by_day(v["series"]) for k, v in heavy.items()
            if isinstance(v, dict) and isinstance(v.get("series"), list)}


def headline(fc: dict) -> str | None:
    return next((k for k in HEADLINE if k in walks(fc)), None)


def _walk_of(fc: dict, name: str) -> dict:
    """The reply's walk object behind a scored name."""
    back = {key: walk for walk, key in scenario_keys().items()}
    return fc.get(back.get(name, name)) or {}


def _by_day(series: list) -> dict[dt.date, float]:
    out = {}
    for row in series:
        try:
            out[dt.date.fromisoformat(str(row[0]))] = float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
    return out


def band_lo(fc: dict) -> tuple[str, dict] | None:
    band = fc.get("band")
    if isinstance(band, dict) and isinstance(band.get("lo"), list):
        return f"band:{band.get('basis') or '?'}", _by_day(band["lo"])
    low = fc.get("pace_low")
    if isinstance(low, dict) and isinstance(low.get("series"), list):
        return "pace_low", _by_day(low["series"])
    return None


def horizon_problems(fc: dict, day: dt.date) -> list[str]:
    """What says the engine ignored `today`: a walk that does not start on
    the day and step a day at a time, or a dated event outside the
    horizon."""
    out = []
    end = day + dt.timedelta(days=int(fc.get("days") or 60))
    for name, series in walks(fc).items():
        days = [str(r[0]) for r in series[:2]]
        if days != [day.isoformat(), (day + DAY).isoformat()]:
            out.append(f"{name} starts {days}, not {day.isoformat()}")
    for key in ("events", "bill_events"):
        for ev in fc.get(key) or []:
            try:
                d = dt.date.fromisoformat(str(ev[0]))
            except (TypeError, ValueError, IndexError):
                continue
            if not day < d <= end:
                out.append(f"{key} dated {d.isoformat()} outside "
                           f"({day.isoformat()}, {end.isoformat()}]")
    return out


def sample(world, fc: dict, day: dt.date) -> dict:
    """One forecast's errors, every amount a share of the scale."""
    scale = world.scale or 1.0
    truth = world.checking
    out = {"day": day.isoformat(), "walks": {}}
    for name, series in walks(fc).items():
        got = _by_day(series)
        errs = {}
        for h in HORIZONS:
            t = day + dt.timedelta(days=h)
            if t in got and t in truth:
                errs[str(h)] = (got[t] - truth[t]) / scale
        out["walks"][name] = errs
    head = headline(fc)
    out["headline"] = head
    if head is None:
        return out
    # scenario fit (reported, not held): how far "your usual" sits from
    # "pay on the due date" at 30 days
    if "due" in out["walks"] and "30" in out["walks"]["due"] \
            and "30" in out["walks"][head]:
        out["fit_due_30"] = abs(out["walks"][head]["30"]
                                - out["walks"]["due"]["30"])
    # each scenario's heavier-month line: is the truth below it? (the line
    # is meant to be passed about one time in five)
    below = {}
    for key, line in heavy_lines(fc).items():
        below[key] = {str(h): truth[t] < line[t] for h in HORIZONS
                      if (t := day + dt.timedelta(days=h)) in line
                      and t in truth}
    if below:
        out["heavy_below"] = below
    hw = _walk_of(fc, head)
    got = _by_day(hw["series"])
    window = [day + dt.timedelta(days=k) for k in range(1, 61)
              if day + dt.timedelta(days=k) in truth]
    if window:
        tmin_day = min(window, key=lambda x: truth[x])
        pred = [(x, got[x]) for x in window if x in got]
        if pred:
            pmin_day, pmin = min(pred, key=lambda r: r[1])
            out["low_amt"] = abs(pmin - truth[tmin_day]) / scale
            out["low_days"] = abs((pmin_day - tmin_day).days)
        cf = world.counterfactual(day)
        cf_neg = next((x for x in window if cf.get(x, 0.0) < 0), None)
        real_neg = next((x for x in window if truth[x] < 0), None)
        out["pred_short"] = hw.get("negative_date") is not None
        # which line would have warned: each scenario, and each scenario's
        # heavier-month line, below $0 inside the horizon
        short = {}
        for key in ("usual", "now", "due"):
            w = _walk_of(fc, key)
            if w.get("series"):
                short[key] = w.get("negative_date") is not None
        for key, line in heavy_lines(fc).items():
            short[f"heavy_{key}"] = any(
                v < 0 for d, v in line.items() if day < d <= day + 60 * DAY)
        out["short_by"] = short
        # the alarm as the product raises it: the red notice reads "usual",
        # the quiet warning cash after card balances (pace_now; before the
        # pay-all-cards-today walk that was the "now" scenario itself) or
        # the heavier usual line; any of them below $0 within 30 days,
        # against a real dip within 30 days
        warn = (fc.get("pace_now") or {} if "pace_today" in fc
                else _walk_of(fc, "now"))
        lines = {"usual": _by_day(_walk_of(fc, "usual").get("series") or []),
                 "now": _by_day(warn.get("series") or []),
                 "heavy_usual": heavy_lines(fc).get("usual", {})}
        w30 = [x for x in window if x <= day + 30 * DAY]
        out["alarm30"] = any(line.get(x, 0.0) < 0 for line in lines.values()
                             if line for x in w30)
        out["cf_short30"] = any(cf.get(x, 0.0) < 0 for x in w30)
        out["real_short30"] = any(truth[x] < 0 for x in w30)
        out["cf_short"] = cf_neg is not None
        out["real_short"] = real_neg is not None
        out["lead"] = (cf_neg - day).days if cf_neg else None
        lo = band_lo(fc)
        if lo is not None:
            name, series = lo
            out["band"] = name
            out["band_short"] = any(v < 0 for d, v in series.items()
                                    if day < d <= day + 60 * DAY)
            out["band_cover"] = {str(h): truth[day + dt.timedelta(days=h)]
                                 >= series[day + dt.timedelta(days=h)]
                                 for h in HORIZONS
                                 if day + dt.timedelta(days=h) in series
                                 and day + dt.timedelta(days=h) in truth}
    return out


def _r(x):
    return None if x is None else round(float(x), 4)


def _pr(samples, pred_key, actual_key):
    tp = sum(1 for s in samples if s.get(pred_key) and s.get(actual_key))
    fp = sum(1 for s in samples if s.get(pred_key) and not s.get(actual_key))
    fn = sum(1 for s in samples if not s.get(pred_key) and s.get(actual_key))
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    return prec, rec, tp + fn


def aggregate(samples: list[dict]) -> dict:
    """Per walk and horizon nMAE and bias; the headline's low point,
    shortfall precision/recall (against the counterfactual balance and the
    real one), band warning recall and coverage. None where there was
    nothing to measure."""
    names = sorted({n for s in samples for n in s["walks"]})
    per_walk = {}
    for n in names:
        row = {}
        for h in HORIZONS:
            es = [s["walks"][n][str(h)] for s in samples
                  if str(h) in s["walks"].get(n, {})]
            if es:
                row[str(h)] = {"nmae": _r(sum(abs(e) for e in es) / len(es)),
                               "bias": _r(sum(es) / len(es)), "n": len(es)}
        per_walk[n] = row
    heads = [s for s in samples if s.get("headline")]
    head = heads[0]["headline"] if heads else None
    m: dict = {}
    if head:
        for h in HORIZONS:
            cell = per_walk.get(head, {}).get(str(h))
            m[f"nmae_{h}"] = cell["nmae"] if cell else None
            m[f"bias_{h}"] = cell["bias"] if cell else None
        lows = [s["low_amt"] for s in heads if "low_amt" in s]
        m["low_amt"] = _r(statistics.mean(lows)) if lows else None
        lday = [s["low_days"] for s in heads if "low_days" in s]
        m["low_days"] = _r(statistics.median(lday)) if lday else None
        prec, rec, pos = _pr(heads, "pred_short", "cf_short")
        m["sf_precision"], m["sf_recall"] = _r(prec), _r(rec)
        m["sf_positives"] = pos
        prec, rec, _ = _pr(heads, "pred_short", "real_short")
        m["sf_real_precision"], m["sf_real_recall"] = _r(prec), _r(rec)
        fits = [s["fit_due_30"] for s in heads if "fit_due_30" in s]
        m["fit_due_30"] = _r(statistics.mean(fits)) if fits else None
        leads = [s["lead"] for s in heads if s.get("lead") is not None]
        m["lead_days"] = _r(statistics.median(leads)) if leads else None
        banded = [s for s in heads if "band" in s]
        if banded:
            _, rec, _ = _pr(banded, "band_short", "cf_short")
            m["band_recall"] = _r(rec)
            for h in HORIZONS:
                cov = [s["band_cover"][str(h)] for s in banded
                       if str(h) in s["band_cover"]]
                m[f"band_cov_{h}"] = _r(sum(cov) / len(cov)) if cov else None
            m["band"] = banded[0]["band"]
        else:
            m["band"] = None
        prec, rec, pos = _pr(heads, "alarm30", "cf_short30")
        m["alarm_precision"], m["alarm_recall"] = _r(prec), _r(rec)
        m["alarm_positives"] = pos
        prec, rec, _ = _pr(heads, "alarm30", "real_short30")
        m["alarm_real_precision"], m["alarm_real_recall"] = _r(prec), _r(rec)
        lines = sorted({k for s in heads for k in s.get("short_by", {})})
        for k in lines:
            got = [dict(s, _p=s["short_by"].get(k)) for s in heads
                   if k in s.get("short_by", {})]
            prec, rec, _ = _pr(got, "_p", "cf_short")
            m[f"sf_precision_{k}"], m[f"sf_recall_{k}"] = _r(prec), _r(rec)
        keys = sorted({k for s in heads for k in s.get("heavy_below", {})})
        for k in keys:
            for h in HORIZONS:
                hits = [s["heavy_below"][k][str(h)] for s in heads
                        if str(h) in s.get("heavy_below", {}).get(k, {})]
                if hits:
                    m[f"heavy_below_{k}_{h}"] = _r(sum(hits) / len(hits))
    return {"headline": head, "n": len(samples), "metrics": m,
            "walks": per_walk}


# ---- the rule a change is held to -------------------------------------------

# a share of I (nMAE, low point), an absolute share of I (|bias|), an
# absolute rate (precision, recall, coverage), days (low-point day)
CEILING_NMAE_30 = 0.25
CEILING_BIAS_30 = 0.10
CEILING_RECALL = 0.7
# a recall is a rate only over a few dips: one or two are an anecdote
CEILING_RECALL_MIN_POSITIVES = 3


def _kind(metric: str) -> str:
    if metric.startswith(("nmae_", "low_amt")):
        return "error"
    if metric.startswith("bias_"):
        return "bias"
    if metric.startswith(("sf_", "alarm_", "band_recall", "band_cov_")) \
            and not metric.endswith("positives"):
        return "rate"
    if metric == "low_days":
        return "days"
    return "info"


def worse_by(metric: str, base, cur) -> float | None:
    """How much worse `cur` is than `base` beyond the tolerance (> 0 means a
    regression, < 0 an improvement beyond it, 0 within it); None when the
    metric is not compared."""
    if base is None or cur is None:
        return None
    kind = _kind(metric)
    if kind == "error":
        tol = max(0.05 * abs(base), 0.005)
        d = cur - base
    elif kind == "bias":
        tol = 0.01
        d = abs(cur) - abs(base)
    elif kind == "rate":
        tol = 0.05
        d = base - cur
    elif kind == "days":
        tol = max(0.05 * abs(base), 1.0)
        d = cur - base
    else:
        return None
    if d > tol:
        return d - tol
    if d < -tol:
        return d + tol
    return 0.0


# reported, never held: one line's own warning (the household sees the
# combined alarm, alarm_*), the shortfall flags of the headline alone
INFO_PREFIXES = ("sf_",)


def compare(base: dict, cur: dict) -> dict[str, float]:
    """{metric: worse_by} for every metric both runs measured and the rule
    holds. A bias counts as worse only when its size grew beyond the
    tolerance AND the error at the same horizon did not improve beyond
    its own: a forecast that moves from a little gloomy to a little rosy
    while getting closer to the truth has not regressed."""
    out = {}
    for k in sorted(set(base) | set(cur)):
        if k.startswith(INFO_PREFIXES):
            continue
        w = worse_by(k, base.get(k), cur.get(k))
        if w is None:
            continue
        if k.startswith("bias_") and w > 0:
            e = worse_by("nmae_" + k[5:], base.get("nmae_" + k[5:]),
                         cur.get("nmae_" + k[5:]))
            if e is not None and e < 0:
                w = 0.0
        out[k] = w
    return out


def ceiling_failures(persona, m: dict) -> list[str]:
    """The hard ceilings for a persona the design claims to handle."""
    if not persona.claimed or persona.known_weak:
        return []
    out = []
    if m.get("nmae_30") is not None and m["nmae_30"] > CEILING_NMAE_30:
        out.append(f"nmae_30 {m['nmae_30']} > {CEILING_NMAE_30}")
    if m.get("bias_30") is not None and abs(m["bias_30"]) > CEILING_BIAS_30:
        out.append(f"|bias_30| {abs(m['bias_30'])} > {CEILING_BIAS_30}")
    # the shortfall ceiling judges the alarm the household sees (usual, now
    # or the heavier usual line below $0 within 30 days); an engine that
    # does not report it is judged on its usual line
    pos_k, rec_k = (("alarm_positives", "alarm_recall")
                    if "alarm_recall" in m else ("sf_positives", "sf_recall"))
    if (m.get(pos_k) or 0) >= CEILING_RECALL_MIN_POSITIVES \
            and m.get(rec_k) is not None and m[rec_k] < CEILING_RECALL:
        out.append(f"{rec_k} {m[rec_k]} < {CEILING_RECALL}")
    return out
