"""Shared switches for the property tests (specs/invariants-and-fuzzing.md).

Two Hypothesis profiles. `default` runs in every suite: derandomized (the
same examples every run, so a failure is the same failure on every
machine), no example database written to the tree, no per-example
deadline (a loaded shard is not a slow function), and few examples. `long`
is run by hand with `OIKONOME_PROPERTY=long`: random, many
examples, and it prints the blob that reproduces a failure.

Seeded generators (the household builders) read `seeds()`: a fixed few by
default, `OIKONOME_PROPERTY_SEEDS=<n>` for more.

Whatever else long mode draws at random (the concurrency harness's
schedules, the fuzz's choice of field) comes from `run_seed()`: one seed per
run, printed when first drawn and named in every failure it causes;
`OIKONOME_PROPERTY_SEED=<n>` replays it.
"""

from __future__ import annotations

import os
import random
import sys

from hypothesis import HealthCheck, settings

LONG = os.environ.get("OIKONOME_PROPERTY", "").strip().lower() == "long"

settings.register_profile(
    "default", derandomize=True, database=None, deadline=None,
    max_examples=40, print_blob=True,
    suppress_health_check=[HealthCheck.too_slow])
settings.register_profile(
    "long", derandomize=False, database=None, deadline=None,
    max_examples=2000, print_blob=True,
    suppress_health_check=[HealthCheck.too_slow])
settings.load_profile("long" if LONG else "default")

DEFAULT_SEEDS = (1, 2, 3)


def seeds(default: int | None = None) -> tuple[int, ...]:
    """The seeds a seeded generator runs: the fixed defaults (or the first
    `default` when a property needs more households to reach every case),
    or the first N when OIKONOME_PROPERTY_SEEDS=N."""
    raw = os.environ.get("OIKONOME_PROPERTY_SEEDS", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return tuple(range(1, int(raw) + 1))
    return tuple(range(1, default + 1)) if default else DEFAULT_SEEDS


def examples(n: int) -> int:
    """`n` examples by default; the long profile's count in long mode."""
    return settings().max_examples if LONG else n


_RUN_SEED: int | None = None


def run_seed() -> int:
    """The run's one seed for long mode's own random choices: 0 by default
    (nothing is random then), else OIKONOME_PROPERTY_SEED or a fresh one,
    printed the first time it is drawn so any failure can be replayed."""
    global _RUN_SEED
    if not LONG:
        return 0
    if _RUN_SEED is None:
        raw = os.environ.get("OIKONOME_PROPERTY_SEED", "").strip()
        _RUN_SEED = int(raw) if raw.isdigit() else random.SystemRandom(
        ).randrange(1, 10 ** 9)
        print(f"\n[long mode] OIKONOME_PROPERTY_SEED={_RUN_SEED}",
              file=sys.stderr, flush=True)
    return _RUN_SEED


def replay() -> str:
    """How to replay the run's random choices, for a failure message."""
    return f"OIKONOME_PROPERTY=long OIKONOME_PROPERTY_SEED={run_seed()}"
