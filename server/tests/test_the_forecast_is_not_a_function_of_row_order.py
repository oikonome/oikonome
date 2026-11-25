"""The forecast is a function of the stored data and the as-of day, not
of the order the database returns rows in (specs/today-figures.md, "Row
order"). A query without ORDER BY may return its rows in any order — a
no-op UPDATE moves a row in the heap — so every build here runs twice:
once as read, once with every multi-row SELECT that names no order
returned reversed (in long mode also shuffled, several seeds). The
forecast, the Today context, the running month and the nightly record
must be the same bytes. Figures and names are invented."""

import datetime as dt
import json
import random
import re
import unittest

from oikonome.engine import budget, forecast, forecast_score

from . import props
from .test_the_cash_walk_adds_up_for_generated_households import WalkAddsUp
from .util import TODAY, add_bill, add_txn, make_db, write_config

D = dt.date
_ORDERED = re.compile(r"\border\s+by\b", re.I)


class _Rows:
    def __init__(self, cur, q, mode):
        self._c, self._q, self._mode = cur, q, mode

    def fetchall(self):
        rows = self._c.fetchall()
        if len(rows) < 2 or _ORDERED.search(self._q):
            return rows
        rows = list(rows)
        if self._mode == "reversed":
            rows.reverse()
        else:
            random.Random(self._mode).shuffle(rows)
        return rows

    def fetchone(self):
        return self._c.fetchone()

    def __iter__(self):
        return iter(self.fetchall())

    def __getattr__(self, k):
        return getattr(self._c, k)


class Reordered:
    """The connection, returning every unordered multi-row read in another
    order (`mode`: "reversed" or a shuffle seed)."""

    def __init__(self, conn, mode):
        self._conn, self._mode = conn, mode

    def execute(self, q, *a, **k):
        return _Rows(self._conn.execute(q, *a, **k),
                     q if isinstance(q, str) else str(q), self._mode)

    def __getattr__(self, k):
        return getattr(self._conn, k)


def _js(v) -> str:
    return json.dumps(v, sort_keys=True, default=str)


def _surfaces(conn, day=TODAY) -> dict:
    """Everything a household reads that the forecast or the month feeds,
    built from nothing kept."""
    from oikonome.web import report, todayview
    forecast.forget_learned()
    conn._memo = None
    fc = forecast.build(conn, today=day, memo={})
    st = report.gather(conn, day, live=True)
    ctx = todayview.build_context(st)
    ctx = {k: v for k, v in ctx.items() if k not in ("st", "fc")}
    ms = budget.month_status(conn, day, memo={})
    return {"forecast": _js(fc), "today": _js(ctx),
            "month": _js({k: ms[k] for k in ("verdict", "variance",
                                              "buckets", "total_actual")}),
            "record": _js(forecast_score.compact(fc, day)),
            "transactions": _transactions(conn, day),
            "proposals": _proposals(conn, day)}


def _transactions(conn, day) -> str:
    """The Transactions page's rows (and a download's, which stamps the
    same way) and Today's recent pane, each with its recurring pill."""
    from oikonome.web import api_common, data
    rows = [api_common._ser(r) for r in data.transactions(
        conn, day.year, day.month)]
    api_common._stamp_recurring(conn, rows)
    return _js([(r["id"], r.get("recurring_bill")) for r in rows])


def _proposals(conn, day) -> str:
    """What the nightly detection would offer (bills.run), rolled back."""
    import psycopg
    from oikonome.engine import bills
    out = []
    try:
        with conn.transaction():
            bills.run(conn, today=day)
            out = [dict(r) for r in conn.execute(
                "SELECT id, kind, payee, amount, frequency, interval, "
                "next_due FROM bill_proposals ORDER BY id").fetchall()]
            raise psycopg.Rollback()
    except psycopg.Rollback:
        pass
    conn._memo = None
    return _js(out)


def _household_ties():
    """Two bills that tie exactly for one charge, two paychecks on one
    day, two same-day bills at one merchant."""
    conn = make_db()
    write_config(conn, food_monthly=600, other_monthly=600)
    d = TODAY - dt.timedelta(days=130)
    k = 0
    while d < TODAY:
        k += 1
        add_txn(conn, d, 11.0 + k % 13, f"SHOP {k % 4}",
                primary="FOOD_AND_DRINK")
        d += dt.timedelta(days=1)
    for m in (4, 5, 6):
        # $102 sits exactly between the $100 and $104 bills
        add_txn(conn, D(2026, m, 10), 102.0, "RIDGE UTILITIES",
                account="chk")
        for payer in ("CEDAR PAYROLL", "BIRCH PAYROLL"):
            add_txn(conn, D(2026, m, 1), -1500.0, payer, account="chk",
                    primary="INCOME", detailed="INCOME_WAGES")
        for amt in (40.0, 40.0):
            add_txn(conn, D(2026, m, 20), amt, "HILLTOP SERVICES",
                    account="chk")
    add_bill(conn, "RIDGE UTILITIES", 100.0, next_due=D(2026, 7, 10),
             last_seen=D(2026, 6, 10), merchant="RIDGE UTILITIES")
    add_bill(conn, "RIDGE WATER", 104.0, next_due=D(2026, 7, 10),
             last_seen=D(2026, 6, 10), merchant="RIDGE UTILITIES")
    for payer in ("CEDAR PAYROLL", "BIRCH PAYROLL"):
        add_bill(conn, payer, 1500.0, income=True, next_due=D(2026, 8, 1),
                 last_seen=D(2026, 7, 1))
    add_bill(conn, "HILLTOP SERVICES", 40.0, next_due=D(2026, 7, 20),
             last_seen=D(2026, 6, 20))
    add_bill(conn, "HILLTOP LAWN", 40.0, next_due=D(2026, 7, 20),
             last_seen=D(2026, 6, 20), merchant="HILLTOP SERVICES")
    add_txn(conn, D(2026, 7, 10), 102.0, "RIDGE UTILITIES", account="chk")
    add_txn(conn, D(2026, 7, 1), -1500.0, "CEDAR PAYROLL", account="chk",
            primary="INCOME", detailed="INCOME_WAGES")
    return conn


def _modes():
    yield "reversed"
    if props.LONG:
        for seed in props.seeds(4):
            yield seed


class TheForecastIgnoresRowOrder(unittest.TestCase):
    maxDiff = 2000

    def _same(self, conn, what):
        want = _surfaces(conn)
        for mode in _modes():
            got = _surfaces(Reordered(conn, mode))
            for k in want:
                self.assertEqual(got[k], want[k], f"{what}: {k} ({mode})")

    def test_ties_paychecks_and_same_day_bills(self):
        conn = _household_ties()
        try:
            self._same(conn, "ties")
            # a no-op write moves rows in the heap; nothing moves
            before = _surfaces(conn)
            conn.execute("UPDATE bills SET payee = payee")
            conn.execute("UPDATE transactions SET name = name "
                         "WHERE name LIKE 'RIDGE%%'")
            self.assertEqual(_surfaces(conn), before)
        finally:
            conn.close()

    def test_generated_households(self):
        w = WalkAddsUp()
        for seed in props.seeds(4):
            conn, _h = w._household(seed)
            try:
                self._same(conn, f"seed {seed}")
            finally:
                conn.close()

    def test_the_large_household(self):
        from . import large_household
        conn, _n = large_household.build(years=1)
        try:
            self._same(conn, "large")
        finally:
            conn.close()

    def test_bench_households_for_a_few_nights(self):
        from .forecast_bench import bench, personas, world
        from .forecast_bench.household import BenchHousehold
        for key in ("biweekly_hand_payer", "two_earners", "annual_bills"):
            p = personas.PERSONAS[key]
            wd = world.simulate(p, 1, end=bench.world_end("default"))
            days, link = bench.asof_days(p, "default")
            h = BenchHousehold(wd, link=link)
            h.verify_cache = False
            try:
                for d in days[:2]:
                    h.night(d)
                    want = _surfaces(h.conn, d)
                    got = _surfaces(Reordered(h.conn, "reversed"), d)
                    for k in want:
                        self.assertEqual(got[k], want[k], f"{key} {d}: {k}")
            finally:
                h.close()


if __name__ == "__main__":
    unittest.main()
