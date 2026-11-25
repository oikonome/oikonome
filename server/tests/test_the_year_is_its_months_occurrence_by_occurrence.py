"""The Year is its months, occurrence by occurrence (specs/today-figures.md,
"The Year is its months, occurrence by occurrence").

A bill occurrence that straddles a month line — paid on the 2nd of the
month after, paid early in the month before, one of two dates a month
paid late — is decided by more than one month's pass. The Year page must
not decide it again. For every household of tests/year_households.py
(the straddling shapes, and ordinary generated households):

1. every occurrence a budgeted month of the year plans has exactly one
   owning month: no two months plan it, and no month hands it to a
   budgeted month that does not plan it;
2. the Year's cell for a month is that month's own page, field by
   field;
3. the Year's Bills row is the sum of its months: its plan and posted
   are the months', its parts add up to its plan, it calls nothing still
   due that a month counted paid, and nothing not paid that a later
   month carries as late; its Savings figure is the months' saved;
4. a payment pays at most one occurrence across the whole year.
"""

from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import budget
from oikonome.web import bills_breakdown, lenses

from . import props, year_households as yh
from .util import TODAY, make_db

Y = yh.Y
CELL = ("status", "verdict", "variance", "variable_actual", "variable_budget",
        "income", "spend", "net")


def _key(o) -> tuple:
    b = o["bill"]
    return (b.get("uid") or b.get("id") or b["payee"], o["due"].isoformat())


def _planned(o) -> bool:
    """month_status's own test: paid, or unpaid and not floored."""
    return o["txn"] is not None or not o["floored"]


class _Passes:
    """Each budgeted month's occurrence pass, as its page reads it."""

    def __init__(self, conn, months, today):
        self.by_month = {}
        real = budget.month_occurrence_pass
        for m in months:
            seen = []

            def spy(*a, **kw):
                out = real(*a, **kw)
                seen.append(out)
                return out
            with mock.patch.object(budget, "month_occurrence_pass", spy):
                lenses.month_state(conn, Y, m, today, memo={})
            self.by_month[m] = seen[0]


def _bills(rows):
    return next((r for r in rows or () if r["label"] == "Bills"), None)


def violations(conn, today: dt.date = TODAY) -> list[str]:
    """Every broken invariant of the household, in words."""
    out: list[str] = []
    noon = dt.datetime.combine(today, dt.time(12))
    folds = []
    real_year = bills_breakdown.year

    def year_spy(months, **kw):
        res = real_year(months, **kw)
        folds.append((months, res))
        return res
    with mock.patch("oikonome.localtime.now_local", return_value=noon):
        with mock.patch.object(bills_breakdown, "year", year_spy):
            yr = lenses.year_summary(conn, Y, today=today)
        budgeted = [c["m"] for c in yr["cells"]
                    if c["status"] in ("final", "projected")]
        pages = {m: lenses.month_summary(conn, Y, m, today=today)
                 for m in budgeted}
        passes = _Passes(conn, budgeted, today).by_month

    # 1. one owner
    owners: dict[tuple, list[int]] = {}
    handed: dict[tuple, set] = {}
    for m, p in passes.items():
        for o in p["occs"]:
            if _planned(o) and o["bill"].get("bill_type") != "envelope":
                owners.setdefault(_key(o), []).append(m)
        for o in p["candidates"]:
            # a month decides an occurrence it is due in, or one paid by
            # a payment dated in it; any other it reads only as a decoy,
            # through a window that may not reach the payment
            if _planned(o) and (Y, m) in (
                    (o["due"].year, o["due"].month),
                    budget.occurrence_home(o)):
                h = budget.occurrence_home(o)
                if h[0] == Y and h[1] in passes and h[1] != m:
                    handed.setdefault(_key(o), set()).add((m, h[1]))
    for k, ms in sorted(owners.items()):
        if len(ms) > 1:
            out.append(f"1: {k} planned in months {ms}")
    for k, hs in sorted(handed.items()):
        for frm, to in sorted(hs):
            if to not in owners.get(k, []):
                out.append(f"1: month {frm} hands {k} to month {to}, "
                           f"which plans it in {owners.get(k, [])}")

    # 4. a payment pays one occurrence
    paid_by: dict[str, list] = {}
    for m, p in passes.items():
        for o in p["occs"]:
            if o["txn"] is not None and o["txn"].get("txn_id"):
                paid_by.setdefault(o["txn"]["txn_id"], []).append((m, _key(o)))
    for t, uses in sorted(paid_by.items()):
        if len(uses) > 1:
            out.append(f"4: payment {t} pays {uses}")

    # 2. the cell is the month's page
    for c in yr["cells"]:
        if c["m"] not in pages:
            continue
        pg = pages[c["m"]]
        hero = dict(pg, spend=pg["spend_total"],
                    income=pg["income"]["actual"],
                    status=("final" if pg["status"] == "final"
                            else "projected"))
        if pg["status"] == "in_progress" and pg.get("projection"):
            hero.update(verdict=pg["projection"]["verdict"],
                        variance=pg["projection"]["variance"])
        for f in CELL:
            if c.get(f) != hero.get(f):
                out.append(f"2: month {c['m']} {f}: year {c.get(f)!r}, "
                           f"month page {hero.get(f)!r}")

    # 3. the Year's rows are the sums of the months'
    yrow = _bills(yr.get("plan_rows"))
    # a month with no dated bill has no Bills row, and adds nothing
    mbds = {m: b for m, pg in pages.items()
            if (b := (_bills(pg["plan_rows"]) or {}).get("breakdown"))}
    if yrow is None and mbds:
        out.append(f"3: no Bills row on the year, months {sorted(mbds)} "
                   "have one")
    if yrow is not None:
        ybd = yrow["breakdown"]
        plan = sum(b["plan"] for b in mbds.values())
        net = sum(b["posted"] - b["refunds"] for b in mbds.values())
        if ybd["plan"] != plan:
            out.append(f"3: year plan {ybd['plan']}, months {plan}")
        if ybd["posted"] - ybd["refunds"] != net:
            out.append(f"3: year posted {ybd['posted'] - ybd['refunds']}, "
                       f"months {net}")
        inside = sum(i["amount"] for i in ybd["late"] if i["inside_plan"])
        parts = (ybd["posted"] - ybd["refunds"] + ybd["due"] + inside
                 + ybd["not_paid"] + ybd["moved"] + ybd["under"])
        if parts != ybd["plan"]:
            out.append(f"3: year parts {parts}, plan {ybd['plan']}")
        # the Year's own breakdown and the months it folded, with the
        # ids the reply strips
        months_in, full = folds[-1] if folds else ([], ybd)
        paid = [o for p in passes.values() for o in p["occs"]
                if o["txn"] is not None]

        def paid_here(i):
            return [o for o in paid if o["due"].isoformat() == i["due"]
                    and (o["bill"].get("uid") == i.get("uid")
                         if o["bill"].get("uid") and i.get("uid")
                         else i.get("bill") in (o["bill"].get("id"),)
                         or o["bill"]["payee"] == i["payee"])]
        for i in full["late"] + full["unpaid"]:
            if paid_here(i):
                out.append(f"4: year carries {i['payee']} {i['due']} "
                           "unpaid, a month counted it paid")
        # every closed month's not-paid row is the year's not paid, or
        # taken by a late or moved occurrence of the SAME bill
        taken = [i for i in full["late"] if i.get("inside_plan")] \
            + full["moved_items"]
        for mo in months_in:
            for r in mo.get("rows") or ():
                if r["state"] != "not_paid" or r in full["unpaid"]:
                    continue
                if not any(bills_breakdown.same_bill(r, i) for i in taken):
                    out.append(f"3: month {mo['m']}'s not paid {r['payee']} "
                               f"{r['due']} is gone from the year")
        run = [b for b in mbds.values() if b["kind"] == "running"]
        # the running month's still due, less what a closed month counted
        # paid (the Year takes the closed month's word for its own month)
        dropped = sum(i["amount"] for i in full.get("paid_late") or ())
        if run and ybd["still_due"] + dropped != run[0]["still_due"]:
            out.append(f"3: year still due {ybd['still_due']}, running "
                       f"month {run[0]['still_due']}")
        late_due = {(i["payee"], i["due"]) for i in ybd["late"]}
        for i in ybd["unpaid"]:
            if (i["payee"], i["due"]) in late_due:
                out.append(f"3: {i['payee']} {i['due']} both not paid and "
                           "late")
    if yr.get("saved_ytd") is not None:
        months = round(sum(pg["saved_month"] or 0 for pg in pages.values()),
                       2)
        if round(yr["saved_ytd"], 2) != months:
            out.append(f"3: saved {yr['saved_ytd']}, months {months}")
    return out


# What today's code breaks, by shape (none, when the shape holds).
KNOWN: dict[str, list[str]] = {}


class TheYearIsItsMonths(unittest.TestCase):
    maxDiff = None

    def _shape(self, name, days=()):
        for day in (0,) + tuple(days):
            with self.subTest(day=day or None):
                conn, today, close = yh.build(name, day)
                try:
                    got = violations(conn, today)
                finally:
                    close()
                if name in KNOWN:
                    self.assertTrue(got, f"{name} now holds: drop it")
                    continue
                self.assertEqual(got, [], (name, today))

    def test_a_bill_paid_on_the_2nd_of_the_month_after(self):
        self._shape("late_on_the_2nd", yh.EARLY_DAYS)

    def test_a_bill_paid_early_in_the_month_before(self):
        self._shape("early_in_the_month_before", yh.EARLY_DAYS)

    def test_a_bill_of_two_dates_paid_late_on_one(self):
        self._shape("two_dates_one_late", yh.EARLY_DAYS)

    def test_a_bill_missed_in_a_closed_month(self):
        self._shape("missed_in_a_closed_month", yh.EARLY_DAYS)

    def test_a_goal_partly_taken_back(self):
        self._shape("partial_withdrawal", yh.EARLY_DAYS)

    def test_a_bill_still_late(self):
        self._shape("still_late", yh.EARLY_DAYS)

    def test_a_late_bill_takes_its_own_bills_row_on_a_shared_date(self):
        self._shape("same_date_legacy_archived")

    def test_an_occurrence_a_closed_month_paid_is_never_carried_late(self):
        self._shape("paid_then_merchant_changed")

    def test_a_bill_given_a_second_date_after_its_month_closed(self):
        conn, today, close = yh.build("one_to_two_dates", 0)
        self.addCleanup(close)
        self.assertEqual(violations(conn, today), [])
        yr = lenses.year_summary(conn, Y, today=today)
        late = _bills(yr["plan_rows"])["breakdown"]["late"]
        self.assertEqual(sorted((i["due"], i["inside_plan"]) for i in late),
                         [(f"{Y}-06-27", False), (f"{Y}-06-29", True)])

    def test_straddling_generated_households(self):
        for seed in props.seeds(default=12):
            with self.subTest(seed=seed):
                conn = make_db()
                try:
                    today = yh.straddling(conn, seed)
                    self.assertEqual(violations(conn, today), [], seed)
                finally:
                    conn.close()

    def test_ordinary_generated_households(self):
        for seed in props.seeds(default=6):
            with self.subTest(seed=seed):
                h = yh.ordinary(seed)
                try:
                    self.assertEqual(violations(h.conn), [], seed)
                finally:
                    h.close()


if __name__ == "__main__":
    unittest.main()
