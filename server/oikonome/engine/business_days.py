"""The US banking calendar the cash forecast lands money on.

Money moved by ACH or direct deposit moves on business days only: a
paycheck scheduled on a Saturday or a bank holiday is usually deposited
the business day before, and a bill debited by ACH on one posts the
business day after (specs/today-figures.md, "Paydays and bills land on
business days").

The calendar is the Federal Reserve's, the one ACH settles on: weekends
and the eleven federal holidays. A holiday that falls on a Sunday is
observed on the Monday after; one that falls on a Saturday is not moved
(the Federal Reserve Banks open the Friday before, so that Friday is a
business day). There is no household country setting: every household is
read on this calendar, and a household outside the US sees its paydays
and bills moved off weekends and US holidays only.
"""

from __future__ import annotations

import calendar
import datetime as dt
import functools

# The holidays as data: ("fixed", month, day) or ("nth", month, weekday,
# n) — the nth such weekday of the month, n = -1 for the last.
US_BANK_HOLIDAYS: tuple[tuple, ...] = (
    ("fixed", 1, 1),                      # New Year's Day
    ("nth", 1, calendar.MONDAY, 3),       # Birthday of Martin Luther King, Jr.
    ("nth", 2, calendar.MONDAY, 3),       # Washington's Birthday
    ("nth", 5, calendar.MONDAY, -1),      # Memorial Day
    ("fixed", 6, 19),                     # Juneteenth
    ("fixed", 7, 4),                      # Independence Day
    ("nth", 9, calendar.MONDAY, 1),       # Labor Day
    ("nth", 10, calendar.MONDAY, 2),      # Columbus Day
    ("fixed", 11, 11),                    # Veterans Day
    ("nth", 11, calendar.THURSDAY, 4),    # Thanksgiving Day
    ("fixed", 12, 25),                    # Christmas Day
)
# A holiday the calendar has only from a year on: Juneteenth became a
# federal holiday in 2021, so a June 19 before it was a business day.
HOLIDAY_FIRST_YEAR: dict[tuple, int] = {("fixed", 6, 19): 2021}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    days = [d for d in range(1, calendar.monthrange(year, month)[1] + 1)
            if dt.date(year, month, d).weekday() == weekday]
    return dt.date(year, month, days[n - 1 if n > 0 else n])


@functools.lru_cache(maxsize=64)
def bank_holidays(year: int) -> frozenset[dt.date]:
    """The days in `year` the banks are closed for a holiday, observed
    days included (a Sunday holiday moves to the Monday after)."""
    out = set()
    for rule in US_BANK_HOLIDAYS:
        if year < HOLIDAY_FIRST_YEAR.get(rule, year):
            continue
        if rule[0] == "fixed":
            d = dt.date(year, rule[1], rule[2])
            if d.weekday() == calendar.SUNDAY:
                d += dt.timedelta(days=1)
        else:
            d = _nth_weekday(year, rule[1], rule[2], rule[3])
        out.add(d)
    return frozenset(out)


def is_business_day(d: dt.date) -> bool:
    return d.weekday() < calendar.SATURDAY and d not in bank_holidays(d.year)


# The rollers stop at the ends of the calendar instead of overflowing: a
# restored date can sit on the last (or first) representable day.


def previous_business_day(d: dt.date) -> dt.date:
    """`d` when it is a business day, else the last one before it."""
    while not is_business_day(d) and d > dt.date.min:
        d -= dt.timedelta(days=1)
    return d


def next_business_day(d: dt.date) -> dt.date:
    """`d` when it is a business day, else the first one after it."""
    while not is_business_day(d) and d < dt.date.max:
        d += dt.timedelta(days=1)
    return d


def add_business_days(d: dt.date, n: int) -> dt.date:
    """The `n`th business day after `d` (n >= 0; 0 is `d` itself), never
    past the last representable day."""
    while n > 0 and d < dt.date.max:
        d += dt.timedelta(days=1)
        if is_business_day(d):
            n -= 1
    return d
