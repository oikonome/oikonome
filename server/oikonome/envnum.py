"""Environment values that compose materialises as empty strings.

compose declares every key it forwards, so a knob nobody set arrives as
`""` rather than absent — and `os.environ.get(name, default)` returns the
default only when the name is MISSING. Reading a numeric knob that way is
`int("")` at import time, i.e. a container that crash-loops on startup.

Forward a handful of numeric knobs so they can be tuned at all and every
app container dies at import. The same trap is separately documented in
`turnstile.enabled`, `spamdefense._flag` and `db/tenancy`. It lives in ONE
place here, because independent restatements of a rule are how the next
caller reintroduces the bug.
"""

from __future__ import annotations

import os


def env_num(name: str, default: str) -> str:
    """An env NUMBER as a string, treating empty and whitespace as unset.

    Returns a string so the caller keeps its own `int()`/`float()` — the
    conversion is where the caller's own bounds and units live.
    """
    return (os.environ.get(name) or "").strip() or default


def env_flag(name: str) -> bool:
    """An env BOOLEAN. Same empty-is-unset rule, one spelling of truthy.

    `os.environ.get(name)` alone treats `"0"` and `"false"` as ON, which is
    the other half of this trap and has bitten separately.
    """
    return (os.environ.get(name) or "").strip().lower() in (
        "1", "true", "yes", "on")
