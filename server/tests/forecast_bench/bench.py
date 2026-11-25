"""Run the benchmark: simulate, step each household, score, compare.

    python -m tests.forecast_bench.bench [--mode default|long]
        [--only REGEX] [--seeds N] [--bills planted,detected]
        [--out FILE] [--jobs N]
    python -m tests.forecast_bench.bench --write-baseline [--why TEXT]

`--write-baseline` (what `make forecast-baseline` runs) records the
default run as `baseline.json`'s `default` section, and refuses a value
worse than the one it replaces unless `--why` says why; the reason is
kept in the file's `history`. `--jobs N` deals the personas over N
processes, each on its own test database (`<OIKONOME_TEST_DB>_j<n>`).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import metrics, personas, world

HERE = Path(__file__).resolve().parent
BASELINE = HERE / "baseline.json"
RUNS = HERE / "runs"

# The places the product reads the wall clock while the bench drives it
# with a day of its own, and why each is wrong there. The clock-guard test
# is an expected failure while this is not empty; a read not listed here
# fails outright. Remove an entry in the commit that passes the day down.
KNOWN_CLOCK_READS = {
    "forecast": {},
    "night": {
        "oikonome/engine/bills.py:save_bill household_day()":
            "save_bill dates its grace and drift stamps by the household's "
            "real day; it takes no today",
        "oikonome/engine/budget.py:bill_displays household_day()":
            "bill_displays falls back to the household's real day when a "
            "caller on the save path passes no today",
        # detected mode only: bills.run(today=…) still reaches these
        "oikonome/engine/bills.py:pending_proposals now_local()":
            "bills.run calls pending_proposals without the today it was "
            "given",
        "oikonome/engine/bills.py:_insert_proposal now()":
            "an offer's created stamp is the wall clock",
    },
}


def asof_days(persona, mode: str) -> tuple[tuple[dt.date, ...],
                                           dt.date | None]:
    """The forecast days, and the link day of a household that linked late
    (its days are counted from the link: `history_days` of ledger, then
    one, two and four weeks more — 2, 3, 4 and 6 weeks for a household
    that linked two weeks before its first forecast)."""
    days = world.DEFAULT_ASOF if mode == "default" else world.long_asof()
    if persona.history_days:
        link = days[0] - dt.timedelta(days=persona.history_days)
        return tuple(link + dt.timedelta(days=persona.history_days + k)
                     for k in (0, 7, 14, 28)), link
    return days, None


def world_end(mode: str) -> dt.date:
    return world.DEFAULT_END if mode == "default" else world.LONG_END


def run_persona(key: str, seed: int, *, mode: str = "default",
                bills: str = "planted") -> dict:
    """Simulate one household and forecast it on its as-of days."""
    from .household import BenchHousehold
    p = personas.PERSONAS[key]
    t0 = time.monotonic()
    w = world.simulate(p, seed, end=world_end(mode))
    days, link = asof_days(p, mode)
    h = BenchHousehold(w, bills=bills, link=link)
    samples, problems = [], []
    try:
        for d in days:
            h.night(d)
            fc = h.forecast(d)
            problems += [f"{d.isoformat()}: {x}"
                         for x in metrics.horizon_problems(fc, d)]
            samples.append(metrics.sample(w, fc, d))
    finally:
        h.close()
    return {"persona": key, "seed": seed, "bills": bills,
            "scale": round(w.scale, 2), "samples": samples,
            "clock_reads": h.clock_reads, "horizon_problems": problems,
            "cache_mismatches": h.cache_mismatches,
            "seconds": round(time.monotonic() - t0, 2)}


def label(key: str, bills: str) -> str:
    return key if bills == "planted" else f"{key}@{bills}"


def persona_of(label_: str):
    return personas.PERSONAS[label_.split("@")[0]]


def summarise(runs: list[dict]) -> dict:
    """Pool every seed's samples per persona (and bill mode)."""
    by: dict[str, list] = {}
    for r in runs:
        by.setdefault(label(r["persona"], r["bills"]), []).append(r)
    out = {}
    for k in sorted(by):
        rs = by[k]
        agg = metrics.aggregate([s for r in rs for s in r["samples"]])
        clocks: dict[str, set] = {"night": set(), "forecast": set()}
        for r in rs:
            for phase, reads in r["clock_reads"].items():
                clocks[phase] |= set(reads)
        agg.update({
            "seeds": sorted(r["seed"] for r in rs),
            "scale": [r["scale"] for r in rs],
            "clock_reads": {k2: sorted(v) for k2, v in clocks.items()},
            "horizon_problems": sorted({x for r in rs
                                        for x in r["horizon_problems"]}),
            "cache_mismatches": sorted(
                f"seed {r['seed']} {d}" for r in rs
                for d in r.get("cache_mismatches", [])),
            "seconds": round(sum(r["seconds"] for r in rs), 2)})
        out[k] = agg
    return out


def engine_info() -> dict:
    """Which engine ran: its checkout's commit (`-dirty` when its server
    code has changes), its FORECAST_MODEL when it names one, its path."""
    from oikonome.engine import forecast
    root = Path(forecast.__file__).resolve().parents[3]
    sha = ""
    try:
        sha = subprocess.run(  # noqa: S603
            ["git", "-C", str(root), "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True, text=True, check=False).stdout.strip()
        dirty = subprocess.run(  # noqa: S603
            ["git", "-C", str(root), "status", "--porcelain",  # noqa: S607
             "--", "server/oikonome"],
            capture_output=True, text=True, check=False).stdout.strip()
        if dirty:
            sha += "-dirty"
    except OSError:
        pass
    return {"sha": sha, "model": getattr(forecast, "FORECAST_MODEL", None),
            "path": str(Path(forecast.__file__).resolve())}


def run(keys, seeds, *, mode: str, bills_modes=("planted",)) -> dict:
    runs = [run_persona(k, s, mode=mode, bills=b)
            for k in keys for s in seeds for b in bills_modes]
    return {"engine": engine_info(), "mode": mode, "seeds": list(seeds),
            "bills": list(bills_modes), "personas": summarise(runs)}


def default_run(keys) -> dict:
    return run(keys, (1,), mode="default")


def selected(only: str | None) -> list[str]:
    return [k for k in personas.PERSONAS if re.search(only or ".", k)]


def _prepare(db: str) -> None:
    """Create and migrate one job's test database."""
    import psycopg
    from oikonome.db import migrate

    from tests import util
    with psycopg.connect(util._admin_dsn("postgres"), autocommit=True) as c:
        if not c.execute("SELECT 1 FROM pg_database WHERE datname=%s",
                         (db,)).fetchone():
            c.execute(f"CREATE DATABASE {db}")
    migrate.run(util._admin_dsn(db))


def _run_jobs(args, keys) -> dict:
    """Deal the personas over `jobs` processes, each on its own test
    database, and merge their results."""
    jobs = min(args.jobs, len(keys))
    shares = [keys[i::jobs] for i in range(jobs)]
    base_db = os.environ.get("OIKONOME_TEST_DB", "oikonome_test").lower()
    # migrated here one at a time: the role statements are cluster-wide
    # and race when several processes run them at once
    for i in range(len(shares)):
        _prepare(f"{base_db}_j{i}")
    procs, outs = [], []
    tmp = Path(tempfile.mkdtemp(prefix="forecast-bench-"))
    for i, share in enumerate(shares):
        out = tmp / f"part{i}.json"
        env = dict(os.environ, OIKONOME_TEST_DB=f"{base_db}_j{i}",
                   OIKONOME_TEST_PREPARED="1")
        cmd = [sys.executable, "-W", "ignore", "-m",
               __package__ + ".bench", "--mode", args.mode,
               "--seeds", str(args.seeds), "--bills", args.bills,
               "--out", str(out),
               "--only", "^(" + "|".join(map(re.escape, share)) + ")$"]
        procs.append(subprocess.Popen(cmd, env=env))  # noqa: S603
        outs.append(out)
    rc = [p.wait() for p in procs]
    if any(rc):
        raise SystemExit(f"a benchmark job failed: {rc}")
    merged = None
    for out in outs:
        part = json.loads(out.read_text())
        if merged is None:
            merged = part
        else:
            merged["personas"].update(part["personas"])
    merged["personas"] = dict(sorted(merged["personas"].items()))
    return merged


def dump(obj) -> str:
    return json.dumps(obj, indent=1, sort_keys=True) + "\n"


def load_baseline() -> dict:
    try:
        return json.loads(BASELINE.read_text())
    except FileNotFoundError:
        return {"default": {}, "long": {}, "history": []}


def write_baseline(result: dict, why: str | None) -> int:
    old = load_baseline()
    worse = []
    for k, cur in result["personas"].items():
        base = (old.get("default") or {}).get(k)
        if not base:
            continue
        for metric, w in metrics.compare(base, cur["metrics"]).items():
            if w > 0:
                worse.append(f"{k} {metric}: {base.get(metric)} -> "
                             f"{cur['metrics'].get(metric)}")
    if worse and not why:
        print("refusing: the baseline would get worse:\n  "
              + "\n  ".join(worse)
              + '\nrun again with WHY="..." to record the reason')
        return 1
    new = dict(old)
    new["default"] = {k: v["metrics"] for k, v in result["personas"].items()}
    new["model"] = result["engine"]["model"]
    # the file is tracked and published: it records no commit, only the
    # model and the reason each re-recording gives
    new.pop("recorded_at_sha", None)
    hist = [{k: v for k, v in h.items() if k != "sha"}
            for h in old.get("history") or []]
    hist.append({"model": result["engine"]["model"],
                 "why": why or ("first baseline" if not old.get("default")
                                else "ratchet"),
                 "worse": worse})
    new["history"] = hist
    BASELINE.write_text(dump(new))
    print(f"baseline written: {len(new['default'])} personas"
          + (f", {len(worse)} worse values accepted" if worse else ""))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("default", "long"), default="default")
    ap.add_argument("--only")
    ap.add_argument("--seeds", type=int, default=0)
    ap.add_argument("--bills", default="planted")
    ap.add_argument("--out")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--why")
    args = ap.parse_args(argv)
    if args.write_baseline:
        return write_baseline(default_run(list(personas.DEFAULT)), args.why)
    if args.mode == "default":
        keys = [k for k in personas.DEFAULT if k in selected(args.only)]
        seeds = range(1, (args.seeds or 1) + 1)
    else:
        keys = selected(args.only)
        seeds = range(1, (args.seeds or 3) + 1)
    args.seeds = len(seeds)
    bills_modes = tuple(args.bills.split(","))
    if args.jobs > 1 and len(keys) > 1:
        result = _run_jobs(args, keys)
    else:
        result = run(keys, tuple(seeds), mode=args.mode,
                     bills_modes=bills_modes)
    text = dump(result)
    if args.out:
        Path(args.out).write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
