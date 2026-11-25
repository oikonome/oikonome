"""Estimator experiments run on the bench without touching the engine.

    python -m tests.forecast_bench.experiments lump [--only REGEX]
        [--seeds N] [--scales 0.5,1,2] [--jobs N] [--out FILE]

`lump` — a lump allowance for one-offs. The pace leaves one-offs out
(forecast.window_one_offs) and nothing stands in for them, so a household
that has a repair or a trip every so often is forecast rosy. The allowance
is the household's own trailing-12-month one-off dollars per day: the
dollars the engine's one-off rule removed from six back-to-back 60-day pace
windows (forecast.forecast_pace at the day, 60, 120 … 300 days before),
divided by 360. Each scenario's path is post-processed — the allowance
times the days ahead subtracted from it, its low point and first negative
day recomputed — and scored with none, half and all of it. `--scales`
re-runs the worlds with the simulator's one-off frequency multiplied, to
show whether a gain is the engine's or only the simulator's assumption.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from . import bench, metrics, personas, world

FACTORS = (0.0, 0.5, 1.0)


def lump_allowance(conn, day: dt.date) -> float:
    """Trailing-12-month one-off dollars per day, as the engine's own
    one-off rule found them."""
    from oikonome.engine import forecast
    total = 0.0
    for k in range(6):
        fp = forecast.forecast_pace(conn, day - dt.timedelta(days=60 * k))
        total += sum(float(o["amount"]) for o in fp.get("one_offs") or [])
    return total / 360.0


def with_allowance(fc: dict, per_day: float) -> dict:
    """The reply with `per_day` dollars a day taken from every walk."""
    if per_day <= 0:
        return fc
    out = copy.copy(fc)
    walk_names = set(metrics.scenario_keys()) | {
        k for k, v in fc.items() if isinstance(v, dict)
        and isinstance(v.get("series"), list)}
    for name in walk_names:
        w = fc.get(name)
        if not isinstance(w, dict) or not isinstance(w.get("series"), list):
            continue
        series = [(d, round(v - per_day * i, 2))
                  for i, (d, v) in enumerate(w["series"])]
        lo = min(range(len(series)), key=lambda i: series[i][1])
        neg = next((d for d, v in series if v < 0), None)
        out[name] = dict(w, series=series, min=series[lo][1],
                         min_date=series[lo][0], negative_date=neg)
    return out


def run_persona(key: str, seed: int, scale: float) -> dict:
    from .household import BenchHousehold
    p = personas.PERSONAS[key]
    w = world.simulate(p, seed, end=world.LONG_END, one_off_scale=scale)
    days, link = bench.asof_days(p, "long")
    h = BenchHousehold(w, link=link)
    samples = {f: [] for f in FACTORS}
    allowances = []
    try:
        for d in days:
            h.night(d)
            fc = h.forecast(d)
            a = lump_allowance(h.conn, d)
            allowances.append(a)
            for f in FACTORS:
                samples[f].append(metrics.sample(
                    w, with_allowance(fc, a * f), d))
    finally:
        h.close()
    return {"persona": key, "seed": seed, "scale": scale,
            "allowance_per_day": round(sum(allowances) / len(allowances), 2),
            "income_monthly": w.scale, "samples": samples}


def summarise(runs: list[dict]) -> dict:
    out: dict = {}
    for r in runs:
        out.setdefault(f"{r['persona']}@x{r['scale']}", []).append(r)
    res = {}
    for k, rs in sorted(out.items()):
        res[k] = {"allowance_per_day": round(sum(
            r["allowance_per_day"] for r in rs) / len(rs), 2),
            "income_monthly": round(sum(r["income_monthly"] for r in rs)
                                    / len(rs), 2)}
        for f in FACTORS:
            agg = metrics.aggregate([s for r in rs
                                     for s in r["samples"][f]])
            res[k][str(f)] = agg["metrics"]
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("experiment", choices=("lump",))
    ap.add_argument("--only")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--scales", default="1")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    keys = bench.selected(args.only)
    scales = [float(x) for x in args.scales.split(",")]
    if args.jobs > 1 and len(keys) > 1:
        jobs = min(args.jobs, len(keys))
        shares = [keys[i::jobs] for i in range(jobs)]
        base_db = os.environ.get("OIKONOME_TEST_DB", "oikonome_test").lower()
        for i in range(jobs):
            bench._prepare(f"{base_db}_j{i}")
        tmp = Path(tempfile.mkdtemp(prefix="forecast-experiment-"))
        procs, outs = [], []
        for i, share in enumerate(shares):
            out = tmp / f"part{i}.json"
            env = dict(os.environ, OIKONOME_TEST_DB=f"{base_db}_j{i}",
                       OIKONOME_TEST_PREPARED="1")
            procs.append(subprocess.Popen(  # noqa: S603
                [sys.executable, "-W", "ignore", "-m",
                 __package__ + ".experiments", "lump", "--seeds",
                 str(args.seeds), "--scales", args.scales, "--out", str(out),
                 "--only", "^(" + "|".join(map(re.escape, share)) + ")$"],
                env=env))
            outs.append(out)
        if any(p.wait() for p in procs):
            raise SystemExit("an experiment job failed")
        result = {}
        for out in outs:
            result.update(json.loads(out.read_text()))
        result = dict(sorted(result.items()))
    else:
        runs = [run_persona(k, s, sc) for k in keys
                for s in range(1, args.seeds + 1) for sc in scales]
        result = summarise(runs)
    text = bench.dump(result)
    if args.out:
        Path(args.out).write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
