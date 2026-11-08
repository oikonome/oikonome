"""Environment values that compose materialises as empty strings.

compose declares every key it forwards, so a knob nobody set arrives as
`""` rather than absent — and `os.environ.get(name, default)` returns the
default only when the name is MISSING. Reading a numeric knob that way is
`int("")` at import time, i.e. a container that crash-loops on startup.

Forwarding numeric knobs so they can be tuned would otherwise make every
app container die at import. The rule lives in ONE place here so every
caller reads empty-as-unset the same way.
"""

from __future__ import annotations

import logging
import math
import os

_log = logging.getLogger(__name__)


def env_num(name: str, default: str) -> str:
    """An env NUMBER as a string, treating empty and whitespace as unset.

    Returns a string so the caller keeps its own `int()`/`float()` — the
    conversion is where the caller's own bounds and units live.
    """
    return (os.environ.get(name) or "").strip() or default


def env_flag(name: str) -> bool:
    """An env BOOLEAN. Same empty-is-unset rule, one spelling of truthy.

    `os.environ.get(name)` alone treats `"0"` and `"false"` as ON, which is
    the other half of this trap.
    """
    return (os.environ.get(name) or "").strip().lower() in (
        "1", "true", "yes", "on")


def _bounded(name: str, v, lo, hi):
    if lo is not None and v < lo:
        _log.warning("%s=%r is below %r — using %r", name, v, lo, lo)
        return lo
    if hi is not None and v > hi:
        _log.warning("%s=%r is above %r — using %r", name, v, hi, hi)
        return hi
    return v


def env_float(name: str, default: float, *, lo: float | None = None,
              hi: float | None = None) -> float:
    """A numeric knob as a float that can never stop a process starting.

    Empty is unset (env_num). A value that is not a finite number — a typo,
    a unit suffix like `60s`, `nan` — is logged by name and the documented
    `default` is used; a number outside [lo, hi] is held to the bound."""
    raw = env_num(name, "")
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        v = math.nan
    if not math.isfinite(v):
        _log.warning("%s=%r is not a number — using the default %r",
                     name, raw, default)
        return default
    return _bounded(name, v, lo, hi)


def env_int(name: str, default: int, *, lo: int | None = None,
            hi: int | None = None) -> int:
    """A whole-number knob, on env_float's terms. `12e6` is a whole number
    and reads as 12000000; `2.5` is not, and falls back to `default`."""
    raw = env_num(name, "")
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        try:
            f = float(raw)
        except ValueError:
            f = math.nan
        if not (math.isfinite(f) and f.is_integer()):
            _log.warning("%s=%r is not a whole number — using the default %r",
                         name, raw, default)
            return default
        v = int(f)
    return _bounded(name, v, lo, hi)
