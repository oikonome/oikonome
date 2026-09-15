"""Whose clock a household runs on.

Every scheduled hour in the product — "daily verdict at 9", the weekly
report's weekday, the date the nightly month-snapshot freezes — is a LOCAL
hour. The instance has one: ``OIKONOME_TZ``, seeded from the host by the
installer. That is the right default for a self-hosted box in somebody's
closet, and wrong on its own for a hosted instance, where every household
in the world would share the instance's clock and a 9am email would reach
the far side of the continent at 2am.

So a household may carry its own zone in its settings (``timezone``, an
IANA name); the instance zone is the fallback for every household that has
not said otherwise. One resolver, used by the worker and the API alike, so
the hour the page promises is the hour the sweep keeps.
"""

from __future__ import annotations

import datetime as dt
import os
import zoneinfo


def valid_zone(name) -> bool:
    """True for an IANA zone this host can load."""
    if not isinstance(name, str) or not name or len(name) > 64:
        return False
    try:
        zoneinfo.ZoneInfo(name)
    except Exception:                                # noqa: BLE001
        return False
    return True


def instance_tz():
    """The instance's zone: ``OIKONOME_TZ`` when set, else the PROCESS's
    own local zone — the one ``date.today()`` has always used. In the
    container the two coincide (compose sets TZ from OIKONOME_TZ); for a
    bare process they must too, or a household with no zone of its own
    would see the live day flip at UTC midnight while the clock on the
    wall still said yesterday."""
    name = os.environ.get("OIKONOME_TZ")
    if name:
        try:
            return zoneinfo.ZoneInfo(name)
        except Exception:                            # noqa: BLE001
            pass
    return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


def instance_tz_name() -> str:
    tz = instance_tz()
    return getattr(tz, "key", None) or (tz.tzname(dt.datetime.now()) or "UTC")


def tenant_tz(cfg: dict | None):
    """The zone this household's hours are kept in: its own setting when
    it has a valid one, the instance's otherwise."""
    name = (cfg or {}).get("timezone")
    if valid_zone(name):
        return zoneinfo.ZoneInfo(name)
    return instance_tz()


def tenant_tz_name(cfg: dict | None) -> str:
    return getattr(tenant_tz(cfg), "key", "UTC")


def now_local(cfg: dict | None) -> dt.datetime:
    return dt.datetime.now(tenant_tz(cfg))


def household_day(conn) -> dt.date:
    """The date it is where this household lives.

    The process clock is the container's, which is UTC on every hosted
    instance: `date.today()` in a job or an engine pass flips the day at
    5pm Pacific, so a bill due "today", a savings pace "this month" and a
    forecast that starts "now" all move a day early for most of the
    continent. Anything whose answer is a CALENDAR DAY asks this instead.

    The config load is the same one the caller almost always does a line
    later; where the caller already holds a config (or a `today`), use
    `now_local(cfg).date()` and skip the query.
    """
    from .engine import budget
    return now_local(budget.load_config(conn)).date()
