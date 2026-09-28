"""The email schedule a household actually has, saved or not.

A household that never saved a schedule still gets the daily verdict, and
every surface that describes when — web Settings, the welcome wizard, the
member card on /api/me, mobile Notifications — must describe the same send
the worker makes. They all read this instead of each inventing a default.

- a saved `email_schedule` object is the schedule;
- none saved: the daily verdict ON at 07:00 in the household's own zone;
- an old settings document that only carries `email_send_hour_utc` keeps
  that UTC hour, published as the local hour it falls on today so the
  clients show the time the mail really arrives.
"""

from __future__ import annotations

import datetime as dt

DEFAULT_DAILY_HOUR = 7


def effective_email_schedule(cfg: dict, now_utc: dt.datetime | None = None) -> dict:
    sched = cfg.get("email_schedule")
    if isinstance(sched, dict):
        return sched
    hour = DEFAULT_DAILY_HOUR
    if "email_send_hour_utc" in cfg:
        try:
            utc_hour = int(cfg["email_send_hour_utc"])
        except (TypeError, ValueError):
            utc_hour = None
        if utc_hour is not None and 0 <= utc_hour <= 23:
            from .. import localtime
            now = now_utc or dt.datetime.now(dt.timezone.utc)
            at = dt.datetime.combine(now.date(), dt.time(utc_hour),
                                     dt.timezone.utc)
            hour = at.astimezone(localtime.tenant_tz(cfg)).hour
    return {"daily": {"on": True, "hour": hour}}
