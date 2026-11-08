"""From when the ledger measures a household's money, as one value.

Every report that totals a window has to know where the household's history
really begins: which day a feed's first row stands for, which months have
both their income and their spend on file, and whether a previous window is
covered well enough to compare against. Answered separately at each site,
every copy handled the edge cases its own way — a feed opening mid-month,
a first row that posts after a weekend, a card linked days before checking
— and the pages disagreed about the same month. `MeasuredWindow` is the one
answer: built once per checkout from the ledger, frozen, and read by every
reporting KPI, comparison, exemption and cache key. Consumers call its
methods; none re-derives a rule.

Pacing on the Today page keeps its own, looser anchor
(`budget.ledger_start`, see specs/short-ledger-pacing.md): it is a
different question — from when to pace a budget — and counts rows the
reports never total.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib

from . import budget
from .compat import as_date

SPEND = "spend"
INCOME = "income"


def posting_grace_day(start: dt.date) -> dt.date:
    """The latest first-row date at which a feed still counts as covering
    from `start`: the SECOND weekday on or after it. Nothing posts on a
    weekend, and a weekday may be a bank holiday (New Year's Day; Labor Day
    when September opens on a weekend) whose rows post the next business
    day, so a feed that opened on `start` shows its first row no later than
    that. One weekday of allowance, never more: for a 1st the second weekday
    is never later than the 4th, and a first row past it is a feed that
    really opened later."""
    day, weekdays = start, 0
    while True:
        if day.weekday() < 5:
            weekdays += 1
            if weekdays == 2:
                return day
        day += dt.timedelta(days=1)


def covers(first_row: dt.date | None, from_day: dt.date) -> bool:
    """Whether a feed whose first row is `first_row` was on file from
    `from_day`. The one posting-lag rule: every "did the feed start by X"
    question goes through here."""
    return first_row is not None and first_row <= posting_grace_day(from_day)


def month_known(years, start: str | None, key: str) -> bool:
    """`key` 'YYYY-MM' has measured income: on or after the first whole
    month `start`, in a year with bank rows. For a whole year pass
    'YYYY-01' (a year is known only when its January is)."""
    return start is not None and key >= start and key[:4] in years


def known_months(years, start: str | None, through: str) -> list[str]:
    """Every 'YYYY-MM' from `start` through `through` (inclusive) whose
    income is known, in order — a month with no deposit included, since
    its income is a measured $0, not a missing one."""
    if start is None:
        return []
    return [f"{y}-{mo:02d}" for y in sorted(years) for mo in range(1, 13)
            if start <= f"{y}-{mo:02d}" <= through]


def _month(ym: str) -> dt.date:
    return dt.date(int(ym[:4]), int(ym[5:7]), 1)


def _next_month(d: dt.date) -> dt.date:
    return (d.replace(day=1) + dt.timedelta(days=32)).replace(day=1)


@dataclasses.dataclass(frozen=True)
class Clip:
    """A window cut to the months whose income is known (MeasuredWindow.clip).
    `runs` are [start, stop) stretches of known months — the whole window
    when `income_unknown`; `gap_months` the 'YYYY-MM' left out on both
    sides; `clipped` says the start moved to the first measured month."""
    start: dt.date
    clipped: bool
    runs: tuple
    gap_months: tuple
    income_unknown: bool


@dataclasses.dataclass(frozen=True)
class MeasuredWindow:
    """One household's measured history (see the module docstring).

    spend_first         first row a spend window totals: opens 'All' and
                        caps the `/mo` divisor
    spend_covered_from  first live row of any kind on an account that
                        carries spend: decides whether a spend comparison
                        window is covered (a card imported from Sep 1 whose
                        first purchase is on Sep 2 covers September)
    income_first_row    first row an income window totals, on the income
                        anchor accounts (bank accounts that are not a
                        brokerage's sweep-only cash, or every bank account
                        when that is all there is)
    income_years        years with bank rows the income total counts
    income_from         first month 'YYYY-MM' whose income is measured
                        whole, or None when no bank feed counts at all
    """
    spend_first: dt.date | None
    spend_covered_from: dt.date | None
    income_first_row: dt.date | None
    income_years: frozenset
    income_from: str | None

    @property
    def fingerprint(self) -> str:
        """Short digest of every field: a cache entry built under one
        measured window is never served under another."""
        return hashlib.sha1(repr(dataclasses.astuple(self)).encode(),
                            usedforsecurity=False).hexdigest()[:12]

    # -- windows -------------------------------------------------------------

    def first(self, kind: str) -> dt.date | None:
        return self.spend_first if kind == SPEND else self.income_first_row

    def first_month(self, kind: str) -> dt.date | None:
        f = self.first(kind)
        return f.replace(day=1) if f else None

    def covered_from(self, kind: str) -> dt.date | None:
        return (self.spend_covered_from if kind == SPEND
                else self.income_first_row)

    def prev_covered(self, prev_start: dt.date, kind: str) -> bool:
        """A comparison window opening on `prev_start` is covered: the feed
        was on file from that day (posting lag allowed, `covers`). One that
        opens before the ledger would total days nobody imported as $0, and
        a partial overlap reads as a fake "+150%"."""
        return covers(self.covered_from(kind), prev_start)

    def pace_start(self, start: dt.date, kind: str) -> dt.date:
        """Where a window's `/mo` divisor starts counting: the window's own
        start, or the month the rows it totals begin in when that is later
        — a household three weeks old on a 1y range divides by one month,
        not twelve."""
        f = self.first_month(kind)
        return max(start, f) if f else start

    # -- income coverage -----------------------------------------------------

    def month_known(self, ym: str) -> bool:
        return month_known(self.income_years, self.income_from, ym)

    def year_known(self, y) -> bool:
        return self.month_known(f"{int(y):04d}-01")

    def known_months(self, through: str) -> list[str]:
        return known_months(self.income_years, self.income_from, through)

    def clip(self, start: dt.date, last: dt.date, end: dt.date) -> Clip:
        """`start`..`last` (end exclusive at `end`) cut to the months whose
        income is known. A window opening before the first measured month
        starts there; months inside it without measured income (cards only,
        or a gap in the checking history) are left out on BOTH sides, so
        none of their income is set against all of their spend. A window
        with no known month is `income_unknown`, and spend is still measured
        over all of it."""
        unknown = self.income_from is None
        clipped = False
        if not unknown:
            first = _month(self.income_from)
            if start < first <= last:
                start, clipped = first, True
            elif first > last:
                unknown = True
        runs: list[tuple[dt.date, dt.date]] = []
        gaps: list[str] = []
        if not unknown:
            cur = start
            while cur <= last:
                nxt = _next_month(cur)
                stop = min(nxt, end)
                if self.month_known(cur.strftime("%Y-%m")):
                    if runs and runs[-1][1] == cur:
                        runs[-1] = (runs[-1][0], stop)
                    else:
                        runs.append((cur, stop))
                else:
                    gaps.append(cur.strftime("%Y-%m"))
                cur = nxt
            if not runs:
                unknown, gaps = True, []
        if unknown:
            runs = [(start, end)]
        return Clip(start, clipped, tuple(runs), tuple(gaps), unknown)


def _derive_income_from(first_row: dt.date | None,
                        spend_covered_from: dt.date | None) -> str | None:
    """The first month whose income is measured whole. A feed's first month
    counts when the feed was on file from its 1st (`covers`), or when no
    spend predates the feed's first day: a household whose whole ledger
    opens on Oct 20 measures both sides from that day, while a card from
    Oct 2 beside it sums spend for days whose paychecks were never imported.
    By DAY, with the same posting allowance: a card and checking linked
    together whose first rows land on a Friday and the Monday after started
    together. Otherwise income is measured from the month after."""
    if first_row is None:
        return None
    d = first_row
    if not covers(d, d.replace(day=1)):
        if spend_covered_from is not None and not covers(d, spend_covered_from):
            d = _next_month(d)
    return f"{d.year:04d}-{d.month:02d}"


def _spend_anchors(conn, excl: str, ex_p: tuple
                   ) -> tuple[dt.date | None, dt.date | None]:
    """(spend_first, spend_covered_from), read as three short probes rather
    than an aggregate over every spend row: a whole-ledger scan costs most
    page loads more than the rest of their SQL together.

    The coverage day is the first live row of any kind on an account that
    carries spend, under the exclusions spend applies to its rows: an
    excluded, hidden or shadow account and business-entity money never
    decide coverage, and an account with no spend at all (a plan's
    contribution history, a loan) cannot open the window years before the
    cards were imported. spend_first is spend's own predicate: 401k plan
    history, loan rows and business-entity rows are never spend, and a plan
    CSV back to 2016 would otherwise divide two years of spend by ten."""
    from . import reporting as r
    row = conn.execute(
        f"""SELECT t.date FROM transactions t
             WHERE {r.SPEND_WHERE} {excl}
             ORDER BY t.date LIMIT 1""", ex_p or None).fetchone()
    if row is None:
        return None, None
    # a dateless spend row still marks its account as carrying spend
    first = as_date(row["date"]) if row["date"] else None
    carrying = [a["id"] for a in conn.execute(
        f"""SELECT a.id FROM accounts a
             WHERE EXISTS (SELECT 1 FROM transactions t
                            WHERE t.account_id = a.id
                              AND {r.SPEND_WHERE} {excl})""",
        ex_p or None).fetchall()]
    row = conn.execute(
        f"""SELECT t.date FROM transactions t
             WHERE t.removed = 0 {excl}
               {r._NOT_SHADOW.format(col="t.account_id")}
               {budget.PERSONAL_ONLY_SQL}
               AND t.account_id = ANY(%s)
             ORDER BY t.date LIMIT 1""", (*ex_p, carrying)).fetchone()
    return first, (as_date(row["date"]) if row and row["date"] else None)


def build(conn) -> MeasuredWindow:
    """Read the ledger and derive the window (no memo; see `window`)."""
    from . import reporting as r
    excl, ex_p = r.excluded_accounts_sql(conn)
    spend_first, spend_cov = _spend_anchors(conn, excl, ex_p)
    # Income is measured on the bank accounts that are not a brokerage's
    # cash: one that only ever received sweeps would call years known that
    # no paycheck was ever seen in. A household whose only bank is such an
    # account still anchors its income windows on it — there is nothing
    # else to open them at — but its income is never called measured.
    nocash = r._bank_account_ids(conn, brokerage_cash=False)
    anchor = nocash or r._bank_account_ids(conn)
    years: frozenset = frozenset()
    first_row = income_from = None
    if anchor:
        # The rows the income total counts (_counted_rows_where): business
        # and shadow rows never reach it, so they must not anchor it either.
        where, params = r._counted_rows_where(conn, anchor)
        rows = conn.execute(
            f"""SELECT to_char(t.date,'YYYY') yr, MIN(t.date) AS d
                FROM transactions t WHERE {where}
                GROUP BY yr""", params).fetchall()
        rows = [x for x in rows if x["d"]]
        if rows:
            first_row = min(as_date(x["d"]) for x in rows)
            if nocash:
                years = frozenset(x["yr"] for x in rows)
                income_from = _derive_income_from(first_row, spend_cov)
    return MeasuredWindow(spend_first, spend_cov, first_row, years,
                          income_from)


def window(conn) -> MeasuredWindow:
    """The household's MeasuredWindow, built once per checkout: the
    connection's memo keeps it until any ledger input is written on it
    (budget._LEDGER_TABLES, which names the config), so the several
    reports one page load runs share one read of the ledger."""
    from . import reporting as r
    _excl, ex_p = r.excluded_accounts_sql(conn)
    key = ("coverage.measured_window", repr(ex_p))
    hit = getattr(conn, "memo_get", lambda k: None)(key)
    if hit is not None:
        return hit
    out = build(conn)
    if hasattr(conn, "memo_put"):
        conn.memo_put(key, out, *budget._LEDGER_TABLES)
    return out
