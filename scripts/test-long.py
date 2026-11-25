#!/usr/bin/env python3
"""The property tests in long mode (specs/invariants-and-fuzzing.md,
"Runtime budget"): every test module that states a rule and lets a
generator look for the case that breaks it — the money properties, the
seeded households, the body fuzz, the restore invariants, the older-archive
eras and the concurrency scenarios — run with OIKONOME_PROPERTY=long.
Never part of `make test`: it takes from tens of minutes to hours.

    make test-long                       every such module, one process
    make test-long JOBS=4                dealt over four test databases
    make test-long ONLY=restore          only modules whose name matches
    make test-long SEED=123              replay one run's random choices

The run's seed is printed first and last; every failure names it, prints
the Hypothesis @reproduce_failure blob, or names the household's seed.
"""

# every command run here is this checkout's own interpreter and scripts
# ruff: noqa: S603

from __future__ import annotations

import os
import random
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PY = str(SERVER / ".venv" / "bin" / "python")

# a module is in when it imports one of the generators or harnesses
HELPERS = ("props", "concurrency", "body_fuzz", "fuzz_checks",
           "restore_invariants", "roundtrip_builders", "archive_eras")
USES = re.compile(
    r"^from \. import [^#\n]*\b(%s)\b|^from \.(%s) import|^from hypothesis"
    % ("|".join(HELPERS), "|".join(HELPERS)), re.M)


def modules(only: str) -> list[str]:
    out = []
    for path in sorted((SERVER / "tests").glob("test_*.py")):
        if USES.search(path.read_text(errors="replace")) \
                and re.search(only or ".", path.stem):
            out.append(path)
    return out


def deal(paths: list[Path], jobs: int) -> list[list[str]]:
    """Largest file first onto the lightest share: size is the only cost
    known before the run."""
    shares: list[list[str]] = [[] for _ in range(jobs)]
    load = [0] * jobs
    for p in sorted(paths, key=lambda p: -p.stat().st_size):
        i = load.index(min(load))
        shares[i].append(f"tests.{p.stem}")
        load[i] += p.stat().st_size
    return [s for s in shares if s]


def main() -> int:
    jobs = max(1, int(os.environ.get("JOBS") or 1))
    env = dict(os.environ)
    env["OIKONOME_PROPERTY"] = "long"
    env.setdefault("OIKONOME_PROPERTY_SEEDS", "12")
    env.setdefault("OIKONOME_RT_SEEDS", "24")
    seed = os.environ.get("SEED") or env.get("OIKONOME_PROPERTY_SEED") \
        or str(random.SystemRandom().randrange(1, 10 ** 9))
    env["OIKONOME_PROPERTY_SEED"] = seed
    paths = modules(os.environ.get("ONLY", ""))
    if not paths:
        print("test-long: no module matches")
        return 1
    print(f"test-long: {len(paths)} modules, OIKONOME_PROPERTY_SEED={seed}, "
          f"OIKONOME_PROPERTY_SEEDS={env['OIKONOME_PROPERTY_SEEDS']}, "
          f"OIKONOME_RT_SEEDS={env['OIKONOME_RT_SEEDS']}, JOBS={jobs}",
          flush=True)
    started = time.monotonic()
    base = env.get("OIKONOME_TEST_DB", "oikonome_test").lower()
    if jobs == 1:
        if subprocess.call([str(ROOT / "scripts" / "test-db-guard.sh")],
                           env=env):
            return 1
        subprocess.call([str(ROOT / "scripts" / "test-db-fresh.sh")], env=env)
        rc = subprocess.call([PY, "-m", "unittest",
                              *(f"tests.{p.stem}" for p in paths)],
                             cwd=SERVER, env=env)
    else:
        logs = Path(tempfile.mkdtemp(prefix="oikonome-test-long-"))
        running = []
        for i, share in enumerate(deal(paths, jobs)):
            db_env = dict(env, OIKONOME_TEST_DB=f"{base}_long{i}",
                          OIKONOME_TEST_ALLOW_CONCURRENT="1")
            subprocess.call([str(ROOT / "scripts" / "test-db-fresh.sh")],
                            env=db_env)
            # migrated here, one database after another: the role
            # statements a migration runs are cluster-wide and would race
            subprocess.check_call(
                [PY, "-c", "from tests.util import _ensure_db; _ensure_db()"],
                cwd=SERVER, env=db_env, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            log = logs / f"job-{i}.log"
            fh = log.open("w")
            running.append((log, fh, subprocess.Popen(
                [PY, "-m", "unittest", *share], cwd=SERVER,
                env=dict(db_env, OIKONOME_TEST_PREPARED="1"),
                stdout=fh, stderr=subprocess.STDOUT)))
        rc = 0
        for log, fh, proc in running:
            rc |= proc.wait()
            fh.close()
            text = log.read_text(errors="replace")
            tail = [ln for ln in text.splitlines()
                    if ln.startswith(("Ran ", "OK", "FAILED"))]
            print(f"== {log.name}: {' '.join(tail)}")
            if "FAILED" in text:
                print(text)
        print(f"test-long: logs in {logs}")
    print(f"test-long: {time.monotonic() - started:.0f}s, "
          f"OIKONOME_PROPERTY_SEED={seed}")
    return 1 if rc else 0


if __name__ == "__main__":
    sys.exit(main())
