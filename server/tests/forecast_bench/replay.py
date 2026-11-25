"""The benchmark's nightly replay (specs/forecast-benchmark.md, long mode):
a simulated household stepped night by night through what the nightly
sweep does — the feed's pull, the month's snapshot, the bills, then the
forecast's own record (forecast_score.record) — so the forecast grades
itself on a household whose truth is known.

    replay(key, seed, nights=REPLAY_NIGHTS) -> dict

returns, per persona, whether the grader is right (every graded miss is the
world's true balance less the forecast's), the band's coverage (the band
learned from the first BAND_LEARN_NIGHTS nights against the next
BAND_TEST_NIGHTS nights' truth, at each horizon), and whether the
track-record sentence's figure is the 80th percentile of the 30-day misses.
"""

from __future__ import annotations

import datetime as dt
import time

from oikonome.engine import forecast_score as fs

from . import personas, world

# nights of forecasts graded: the band learns from the first, and is tested
# on the next; the replay runs on until the last tested forecast's 30-day
# target night has been kept
BAND_LEARN_NIGHTS = 100
BAND_TEST_NIGHTS = 50
REPLAY_NIGHTS = BAND_LEARN_NIGHTS + BAND_TEST_NIGHTS + 30
# the first replayed night: a year and more of history behind it, and the
# last target inside the long world
REPLAY_START = dt.date(2026, 3, 2)
# the band is meant to hold 8 times in 10. Over 50 test nights the share
# a perfectly calibrated band holds is itself noisy (overlapping 30-day
# misses are two or three separate results), so the replay holds the mean
# over the claimed personas and seeds at each horizon to this range, each
# claimed persona's mean over horizons and seeds to COVERAGE_FLOOR, and
# the band's median half-width at each horizon to WIDTH_MAX of a month's
# money (about one and a half times what the chosen band measured)
COVERAGE_RANGE = (0.70, 0.92)
COVERAGE_FLOOR = 0.55
WIDTH_MAX = {14: 0.20, 30: 0.40, 45: 0.55, 60: 0.75}
GATED_HORIZONS = (14, 30, 45, 60)


def band_at(qs, walks, i: int, h: int, history_days: int):
    """The band's offsets at horizon h for a night's walks, as Today draws
    them (forecast_score.band): the pooled misses, floored at the heavier
    line's distance while the horizon has fewer than
    BAND_FLOOR_INDEPENDENT separate stretches of history."""
    lo, hi = qs
    if (history_days - h) / h < fs.BAND_FLOOR_INDEPENDENT:
        u = (walks.get("usual") or {}).get("at") or []
        low = (walks.get("low") or {}).get("at") or []
        if i < len(u) and i < len(low) and None not in (u[i], low[i]):
            g = max(0.0, (u[i] - low[i]) / 100)
            lo, hi = min(lo, -g), max(hi, g)
    return lo, hi


def replay(key: str, seed: int, *, nights: int = REPLAY_NIGHTS,
           start: dt.date = REPLAY_START, drop: int = 2) -> dict:
    from .household import BenchHousehold
    p = personas.PERSONAS[key]
    t0 = time.monotonic()
    w = world.simulate(p, seed, end=world.LONG_END)
    link = (start - dt.timedelta(days=p.history_days)
            if p.history_days else None)
    h = BenchHousehold(w, bills="planted", link=link)
    h.verify_cache = False
    built: dict[dt.date, dict] = {}
    rng = world.rng_for(key, seed, "dropped nights")
    dropped = {start + dt.timedelta(days=rng.randrange(
        BAND_LEARN_NIGHTS + BAND_TEST_NIGHTS, nights - 1))
        for _ in range(drop)}
    try:
        for k in range(nights):
            d = start + dt.timedelta(days=k)
            h.night(d)
            # a night the sweep did not run: nothing is kept (the replay
            # drops `drop` nights at random after the band's learning)
            if d not in dropped:
                fs.record(h.conn, d)
            row = h.conn.execute(
                "SELECT walks FROM forecast_nights WHERE as_of = %s",
                (d,)).fetchone()
            if row is not None:
                built[d] = row["walks"]
        last = start + dt.timedelta(days=nights - 1)
        nights_kept = fs.rows(h.conn)
        today = last + dt.timedelta(days=1)
        pairs, skips = fs.graded_pairs(nights_kept, today)
        # "why it missed" adds up to the miss to the cent, every 30-day
        # miss whose night still holds its inputs (one in three, for time)
        drills = drill_wrong = 0
        for pr in [x for x in pairs if x.h == 30][::3]:
            m = fs.miss(h.conn, pr.target, 30, today)
            if m is None:
                continue
            drills += 1
            if (round(sum(x["diff"] for x in m["parts"]) * 100)
                    != round(m["error"] * 100)
                    or abs(m["error"] - pr.error) > 0.005):
                drill_wrong += 1
    finally:
        h.close()
    truth = w.checking
    # the grader is right: every graded miss is the world's balance on the
    # target day less what the forecast said for it
    wrong = []
    for pr in pairs:
        want = round(truth[pr.target] - pr.predicted, 2)
        if abs(pr.error - want) > 0.005:
            wrong.append((pr.as_of.isoformat(), pr.h, pr.error, want))
    cut = start + dt.timedelta(days=BAND_LEARN_NIGHTS)
    learned = [pr for pr in pairs if pr.target < cut]
    q = fs.band_quantiles(learned, cut)
    coverage, width = {}, {}
    for hz in fs.HORIZONS:
        qs = q.get(str(hz))
        if not qs:
            coverage[str(hz)] = width[str(hz)] = None
            continue
        # the band's half-width as a share of the household's money in a
        # month (world.scale): a band that holds by being enormous fails
        width[str(hz)] = round((qs[1] - qs[0]) / 2 / (w.scale or 1.0), 3)
        i = fs.HORIZONS.index(hz)
        held = n = 0
        for k in range(BAND_TEST_NIGHTS):
            d = cut + dt.timedelta(days=k)
            t = d + dt.timedelta(days=hz)
            walks = built.get(d)
            if walks is None or t not in truth:
                continue
            at = (walks.get("usual") or {}).get("at") or []
            if i >= len(at) or at[i] is None:
                continue
            pred = at[i] / 100
            lo_, hi_ = band_at(qs, walks, i, hz,
                               (cut - start).days)
            n += 1
            held += pred + lo_ <= truth[t] <= pred + hi_
        coverage[str(hz)] = round(held / n, 3) if n else None
    # the sentence's figure is the 80th percentile of the misses of the
    # window at the horizon it states a frequency for, rounded up
    sc = fs.summarise(pairs, today)
    win = [abs(pr.error) for pr in pairs if pr.h == sc["horizon"]
           and pr.target > today - dt.timedelta(days=fs.WINDOW_DAYS)]
    want_within = (fs.plain_up(fs.weighted_quantile(win, [1.0] * len(win),
                                                    0.80))
                   if len(win) >= fs.MIN_PAIRS else None)
    return {"persona": key, "seed": seed, "known_weak": p.known_weak,
            "nights": nights, "kept": len(nights_kept), "pairs": len(pairs),
            "skips": skips, "grader_wrong": wrong[:10],
            "grader_wrong_n": len(wrong), "coverage": coverage,
            "width": width,
            "drills": drills, "drill_wrong": drill_wrong,
            "dropped": sorted(d.isoformat() for d in dropped),
            "quantiles": q, "sentence_horizon": sc["horizon"],
            "within": sc["within"], "within_want": want_within,
            "scale": round(w.scale, 2),
            "seconds": round(time.monotonic() - t0, 1)}


def replay_raw(key: str, seed: int, *, nights: int = REPLAY_NIGHTS,
               start: dt.date = REPLAY_START) -> dict:
    """The replay's raw record for studying a band: every kept night's
    predictions (the usual walk and the heavier line, per horizon) and the
    world's true balance on each target day. Pure data, no grading."""
    from .household import BenchHousehold
    p = personas.PERSONAS[key]
    w = world.simulate(p, seed, end=world.LONG_END)
    link = (start - dt.timedelta(days=p.history_days)
            if p.history_days else None)
    h = BenchHousehold(w, bills="planted", link=link)
    h.verify_cache = False
    out = []
    try:
        for k in range(nights):
            d = start + dt.timedelta(days=k)
            h.night(d)
            fs.record(h.conn, d)
            row = h.conn.execute(
                "SELECT walks, start_checking FROM forecast_nights "
                "WHERE as_of = %s", (d,)).fetchone()
            if row is None:
                continue
            truth = [w.checking.get(d + dt.timedelta(days=hz))
                     for hz in fs.HORIZONS]
            out.append({"day": d.isoformat(),
                        "start": row["start_checking"],
                        "usual": row["walks"]["usual"]["at"],
                        "low": row["walks"]["low"]["at"],
                        "truth": truth})
    finally:
        h.close()
    return {"persona": key, "seed": seed, "known_weak": p.known_weak,
            "income": round(w.income_monthly, 2), "scale": round(w.scale, 2),
            "nights": out}
