#!/usr/bin/env python3
"""Judge a cash-forecast change on evidence (specs/forecast-benchmark.md).

    make forecast-scoreboard BASE=<git ref> [ONLY=<persona regex>]
                             [SEEDS=n] [MODE=default|long] [JOBS=n]

Runs THIS checkout's benchmark (server/tests/forecast_bench) against two
engines on identical simulated households: the engine at BASE, checked out
to a temporary detached worktree that gets a copy of this checkout's
benchmark, and this checkout's own engine (its working tree, uncommitted
changes included). Each side runs in its own process from its own
server/ with its own test database (`oikonome_bench_<sha>_base|_head`,
migrated by that side's code), dropped afterwards. Writes
`server/tests/forecast_bench/runs/<sha>-<mode>.json` for each side (not
tracked), prints a per-persona table of base -> head with the change, and
exits 1 when a persona not marked known_weak got worse beyond tolerance.
"""

# every command run here is git or this checkout's own interpreter
# ruff: noqa: S603, S607

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
BENCH = SERVER / "tests" / "forecast_bench"
RUNS = BENCH / "runs"
ADMIN = os.environ.get("OIKONOME_TEST_ADMIN_DSN",
                       "postgresql://postgres:devpass@127.0.0.1:5433")
SHOWN = ("nmae_7", "nmae_14", "nmae_30", "nmae_45", "nmae_60", "bias_30",
         "low_amt", "low_days", "sf_precision", "sf_recall",
         "heavy_below_usual_30", "heavy_below_usual_60")


def git(*args, cwd=ROOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def drop_dbs(prefix: str) -> None:
    import psycopg
    with psycopg.connect(f"{ADMIN}/postgres", autocommit=True) as c:
        for row in c.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE %s",
                (prefix + "%",)).fetchall():
            c.execute(f'DROP DATABASE IF EXISTS "{row[0]}" WITH (FORCE)')


def run_side(server: Path, db: str, args, out: Path) -> dict:
    env = dict(os.environ, OIKONOME_TEST_DB=db)
    env.pop("OIKONOME_TEST_PREPARED", None)
    cmd = [sys.executable, "-W", "ignore", "-m",
           "tests.forecast_bench.bench", "--mode", args.mode,
           "--out", str(out), "--jobs", str(args.jobs)]
    if args.seeds:
        cmd += ["--seeds", str(args.seeds)]
    if args.only:
        cmd += ["--only", args.only]
    subprocess.run(cmd, cwd=server, env=env, check=True)
    got = json.loads(out.read_text())
    want = (server / "oikonome" / "engine" / "forecast.py").resolve()
    if Path(got["engine"]["path"]).resolve() != want:
        raise SystemExit(f"ran {got['engine']['path']}, expected {want}")
    return got


def fmt(x) -> str:
    if x is None:
        return "—"
    return f"{x:.3f}" if isinstance(x, float) else str(x)


def table(base: dict, head: dict, metrics, personas) -> tuple[str, list]:
    lines, regressed = [], []
    for k in sorted(set(base["personas"]) | set(head["personas"])):
        b = (base["personas"].get(k) or {}).get("metrics") or {}
        h = (head["personas"].get(k) or {}).get("metrics") or {}
        diff = metrics.compare(b, h)
        worse = sorted(m for m, w in diff.items() if w > 0)
        better = sorted(m for m, w in diff.items() if w < 0)
        weak = personas.PERSONAS[k.split("@")[0]].known_weak
        verdict = ("WORSE: " + ", ".join(worse) if worse
                   else "better: " + ", ".join(better) if better
                   else "unchanged within tolerance")
        lines.append(f"\n{k}{'  (known weak)' if weak else ''} — {verdict}")
        for m in SHOWN:
            if m not in b and m not in h:
                continue
            bv, hv = b.get(m), h.get(m)
            delta = (f"{hv - bv:+.3f}" if bv is not None and hv is not None
                     else "")
            w = diff.get(m) or 0
            mark = " ▼ worse" if w > 0 else " ▲ better" if w < 0 else ""
            lines.append(f"  {m:13s} {fmt(bv):>9s} -> {fmt(hv):>9s}"
                         f"  {delta:>8s}{mark}")
        if worse and not weak:
            regressed.append(k)
    return "\n".join(lines), regressed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--only")
    ap.add_argument("--seeds", type=int, default=0)
    ap.add_argument("--mode", choices=("default", "long"), default="default")
    ap.add_argument("--jobs", type=int, default=1)
    args = ap.parse_args()
    base_sha = git("rev-parse", "--verify", args.base + "^{commit}")
    head_sha = git("rev-parse", "HEAD")
    if git("status", "--porcelain", "--", "server/oikonome"):
        head_sha += "-dirty"
    tmp = Path(tempfile.mkdtemp(prefix="forecast-scoreboard-"))
    wt = tmp / "base"
    prefix = f"oikonome_bench_{base_sha[:8]}"
    try:
        git("worktree", "add", "--detach", str(wt), base_sha)
        # the bench is this checkout's, whichever engine it drives
        dest = wt / "server" / "tests" / "forecast_bench"
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(BENCH, dest, ignore=shutil.ignore_patterns(
            "runs", "__pycache__"))
        RUNS.mkdir(exist_ok=True)
        print(f"base {base_sha[:12]} ({args.base}) vs head {head_sha[:18]}"
              f", {args.mode} mode", flush=True)
        base = run_side(wt / "server", f"{prefix}_base", args,
                        RUNS / f"{base_sha[:12]}-{args.mode}.json")
        head = run_side(SERVER, f"{prefix}_head", args,
                        RUNS / f"{head_sha[:18]}-{args.mode}.json")
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)],
                       cwd=ROOT, check=False, capture_output=True)
        try:
            drop_dbs(prefix)
        except Exception as e:                           # noqa: BLE001
            print(f"! could not drop {prefix}*: {e}", file=sys.stderr)
        shutil.rmtree(tmp, ignore_errors=True)
    sys.path.insert(0, str(SERVER))
    from tests.forecast_bench import metrics, personas
    text, regressed = table(base, head, metrics, personas)
    print(text)
    if regressed:
        print(f"\nregressed beyond tolerance: {', '.join(regressed)}")
        return 1
    print("\nno persona outside known_weak regressed beyond tolerance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
