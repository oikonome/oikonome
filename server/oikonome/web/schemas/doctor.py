"""The Doctor page: the named health checks, the diagnostic bundle and
the script collectors' heartbeats (web/app.py, web/doctor.py)."""

from __future__ import annotations

from typing import Literal

from . import Shape


class DoctorCheck(Shape):
    section: str
    name: str
    ok: bool
    severity: Literal["ok", "warn", "bad"]
    detail: str
    progress: float = 0
    """Percent done, on a check that is under way."""
    busy: bool = False


class DoctorReport(Shape):
    """GET /api/doctor: every check, and whether all passed."""
    ok: bool
    checks: list[DoctorCheck]


class DoctorBundle(Shape):
    """GET /api/doctor/bundle: the checks as a public issue may carry
    them (no host, bank or household-typed names), with versions."""
    oikonome_version: str
    python: str
    checks: list[DoctorCheck]
    redacted: str
    """What the bundle leaves out, said in the bundle itself."""
    generated_at: str


class ScriptHeartbeat(Shape):
    source: str
    label: str | None
    first_push: str
    last_push: str
    last_rows: int | None
    expected_hours: int | None
    """Null undecided, 0 never warn, N hours."""
    alerts: int
    """1 when a stale source is mailed about."""
    via: str | None
    kind: str | None
    age_hours: float
    status: Literal["fresh", "stale", "off"]


class ScriptHeartbeats(Shape):
    """GET /api/doctor/scripts: one row per script-fed source."""
    scripts: list[ScriptHeartbeat]


class ScriptWatchSet(Shape):
    """POST /api/doctor/scripts/{source}: either or both. Digits in a
    string read the same as a number."""
    expected_hours: int | str = 0
    alerts: bool = False
