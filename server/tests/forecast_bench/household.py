"""One simulated household as a tenant, stepped night by night.

`BenchHousehold(world)` is a fresh tenant with one Plaid item served by
`feed.SimPlaid`. `night(day)` runs what the nightly sweep runs before a
forecast is read, in the same order:

1. `sync.plaid.sync(conn, item, transport=…)` — balances and the
   incremental transaction pull, through the production code (merchant
   identity, pending settlement, the ledger writes);
2. `sync.plaid.sync_liabilities(conn, item, transport=…)` — each card's
   statement, minimum, due date and last payment;
3. `budget.snapshot_month(conn, day)` — the month's budget snapshot;
4. the bill schedule: `planted` enters every true series a household could
   know (seen twice before the day) with its last real amount and its next
   date, through `bills.save_bill`; `detected` runs `bills.run(conn,
   today=day)` and approves every new-bill and income offer (a diligent
   household), rolling a passed due date on the bench's day rather than
   through the approve door's own roll, which reads the real day.

`forecast(day)` is `forecast.build(conn, today=day)`, the entry point Today
reads. Both run under `ClockWatch`: a read of the wall clock inside the
product (`date.today()`, `datetime.now()`, `localtime.now_local`, SQL's
`CURRENT_DATE`/`now()` inside the forecast) is recorded with its place, so
an engine that reads the clock when it was given `today` is caught.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import os
import re
import sys
import uuid

from oikonome import localtime
from oikonome.db import tenancy
from oikonome.engine import bills as bills_mod
from oikonome.engine import budget, forecast
from oikonome.sync import plaid

from tests import util

from . import feed as feed_mod

ITEM = "bench-item"
_PKG = os.path.dirname(os.path.abspath(localtime.__file__))

_SQL_CLOCK = re.compile(
    r"\b(current_date|current_timestamp|localtimestamp|now\s*\(|"
    r"clock_timestamp|statement_timestamp|transaction_timestamp)",
    re.I)


class ClockWatch:
    """Record every wall-clock read the product makes inside the block.

    Built on `sys.monitoring` CALL events, every call watched: a call
    site is never switched off, since the same site may call a clock on a
    later call. With `sql`, a query text naming the database clock counts
    too — not in the nightly writes, where `now()` stamps when a row was
    written, but inside the forecast, which must be a function of `today`.
    """

    def __init__(self, *, sql: bool):
        self.sql = sql
        self.reads: dict[str, int] = {}
        self._py = {localtime.now_local, localtime.household_day}

    def _where(self, code, offset) -> str | None:
        f = code.co_filename
        # a read inside localtime itself is how now_local works; its
        # caller is the place that read the clock
        if not f.startswith(_PKG) or f == localtime.__file__:
            return None
        # the function, not the line: lines move with every edit
        rel = f[len(_PKG):].lstrip(os.sep)
        return f"oikonome/{rel}:{code.co_qualname}"

    def _on_call(self, code, offset, fn, arg0):
        try:
            name = getattr(fn, "__name__", "")
            clock = (fn in self._py if callable(fn) and not isinstance(
                fn, type) else False)
            if not clock and name in ("today", "now", "utcnow"):
                clock = getattr(fn, "__self__", None) in (dt.date,
                                                          dt.datetime)
            if clock:
                where = self._where(code, offset)
                if where:
                    key = f"{where} {name}()"
                    self.reads[key] = self.reads.get(key, 0) + 1
                return None
            if self.sql and name == "execute":
                text = arg0 if isinstance(arg0, str) else str(arg0 or "")
                if _SQL_CLOCK.search(text):
                    where = self._where(code, offset)
                    if where:
                        hit = _SQL_CLOCK.search(text).group(1).lower()
                        key = f"{where} SQL {hit.strip()}"
                        self.reads[key] = self.reads.get(key, 0) + 1
                return None
        except Exception:                                # noqa: BLE001
            pass
        # never DISABLE a call site: the same site can call another
        # function next time (a callable passed in, a method looked up on
        # what it was handed), and one that is a clock must still be seen
        return None

    def __enter__(self):
        mon = sys.monitoring
        for tool in (5, 4, 3, 2):
            if mon.get_tool(tool) is None:
                break
        else:
            raise RuntimeError("no free sys.monitoring tool id")
        self._tool = tool
        mon.use_tool_id(tool, "forecast-bench-clock")
        mon.register_callback(tool, mon.events.CALL, self._on_call)
        mon.set_events(tool, mon.events.CALL)
        mon.restart_events()
        return self

    def __exit__(self, *exc):
        mon = sys.monitoring
        mon.set_events(self._tool, 0)
        mon.register_callback(self._tool, mon.events.CALL, None)
        mon.free_tool_id(self._tool)
        return False


def _next_nominal(series, last: dt.date) -> dt.date:
    """The next date a series' own rule names after `last` — what a
    household entering the series by hand would type."""
    from .world import _add_months_clamped, _nth
    spec, cad = series.spec, series.cadence
    if cad == "weekly":
        return last + dt.timedelta(days=7)
    if cad == "biweekly":
        return last + dt.timedelta(days=14)
    if cad == "semimonthly":
        if last.day < 20:
            return _add_months_clamped(last, 0, 31)
        return _add_months_clamped(last, 1, 15)
    if cad == "weekday_rule":
        y, m = (last.year + 1, 1) if last.month == 12 else (last.year,
                                                           last.month + 1)
        return _nth(y, m, spec["weekday"], spec["nth"])
    months = {"monthly": 1, "quarterly": 3, "semiannual": 6,
              "annual": 12}[cad]
    return _add_months_clamped(last, months, spec["day"])


_FREQ = {"weekly": ("WEEKLY", 1), "biweekly": ("WEEKLY", 2),
         "semimonthly": ("MONTHLY", 1), "monthly": ("MONTHLY", 1),
         "weekday_rule": ("MONTHLY", 1), "quarterly": ("MONTHLY", 3),
         "semiannual": ("MONTHLY", 6), "annual": ("YEARLY", 1)}


class BenchHousehold:
    def __init__(self, world, *, bills: str = "planted",
                 link: dt.date | None = None):
        # nothing a previous household or seed left in the worker
        getattr(forecast, "forget_learned", lambda: None)()
        if bills not in ("planted", "detected"):
            raise ValueError(bills)
        self.world = world
        self.mode = bills
        util._ensure_db()
        admin = tenancy.admin_connect(util._admin_dsn(util.TEST_DB))
        try:
            self.tenant_id = tenancy.create_tenant(
                admin, f"bench-{world.persona.key[:12]}-"
                       f"{uuid.uuid4().hex[:8]}")
        finally:
            admin.close()
        self.conn = tenancy.tenant_connect(self.tenant_id)
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, "
            "access_token) VALUES (%s, 'plaid', 'Bench Bank', "
            "'access-bench')", (ITEM,))
        p = world.persona
        # the household's budget: its true variable spend, give or take a
        # tenth (a fixed draw per world, never the clock)
        from .world import rng_for
        rng = rng_for(p.key, world.seed, "budget")
        cfg = {"food_monthly": round(p.food_monthly
                                     * rng.uniform(0.9, 1.1)),
               "other_monthly": round(p.other_monthly
                                      * rng.uniform(0.9, 1.1)),
               "dynamic_variable_budget": False,
               "plaid_client_id": "bench", "plaid_secret": "bench",
               "plaid_env": "sandbox"}
        if p.pin:
            cfg["checking_account_id"] = world.checking_id
        util.write_config(self.conn, **cfg)
        self.feed = feed_mod.SimPlaid(world, link=link)
        self.transport = self.feed.transport()
        # where the product read the wall clock: {phase: {place: count}}
        self.clock_reads: dict[str, dict[str, int]] = {"night": {},
                                                      "forecast": {}}
        self.nights = 0
        # build each forecast twice, the second with the worker's kept
        # facts dropped, and record a day the two differ
        self.verify_cache = True
        self.cache_mismatches: list[str] = []

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:                                # noqa: BLE001
            pass

    def _merge(self, phase: str, reads: dict) -> None:
        got = self.clock_reads[phase]
        for k, v in reads.items():
            got[k] = got.get(k, 0) + v

    # ---- the night -------------------------------------------------------

    def night(self, day: dt.date) -> None:
        if day < self.feed.day and self.nights:
            raise ValueError("a household only steps forward")
        self.feed.day = day
        with ClockWatch(sql=False) as cw:
            plaid.sync(self.conn, ITEM, transport=self.transport)
            plaid.sync_liabilities(self.conn, ITEM,
                                   transport=self.transport)
            budget.snapshot_month(self.conn, day)
            if self.mode == "planted":
                self._plant(day)
            else:
                self._detect(day)
        self._merge("night", cw.reads)
        self.nights += 1

    def _plant(self, day: dt.date) -> None:
        for s in self.world.schedule:
            if not s.plantable:
                continue
            seen = [o for o in s.occurrences
                    if o.day <= day and o.day >= self.feed.link]
            if len(seen) < 2:
                continue
            last = seen[-1]
            freq, interval = _FREQ[s.cadence]
            kw = {}
            if s.cadence == "semimonthly":
                kw["by_month_day"] = [15, 31]
            bills_mod.save_bill(
                self.conn, payee=s.payee, amount=last.amount,
                frequency=freq, interval=interval,
                next_due=_next_nominal(s, last.nominal),
                income=(s.kind == "income"), last_seen=last.day,
                source="detected", **kw)

    def _detect(self, day: dt.date) -> None:
        bills_mod.run(self.conn, today=day)
        for r in self.conn.execute(
                "SELECT id, kind, payee FROM bill_proposals "
                "WHERE status='pending' ORDER BY id"
                ).fetchall():
            # a new bill or a new income series is offered as kind 'add'
            if (r["kind"] or "") != "add":
                continue
            # approved without the door's own roll of a passed due date,
            # which reads the household's real day (roll_passed_due);
            # the same roll is then made on the bench's day
            try:
                got = bills_mod.apply_proposal(self.conn, r["id"],
                                               "approve", roll=False)
            except Exception:                            # noqa: BLE001
                continue
            payee = r["payee"]
            if payee and not (got or {}).get("error"):
                bills_mod._advance_due_dates(
                    self.conn, day, collections.Counter(), payee=payee)

    # ---- the forecast ---------------------------------------------------

    def forecast(self, day: dt.date) -> dict:
        """forecast.build, then (`verify_cache`) the same day built twice
        more and compared as JSON: once in a cold worker that can only
        read the day's stored row (forecast_learned), and once with
        everything this process learned or stored set aside
        (forecast.forget_learned), learning it all again. A difference is
        a stale kept fact: the day is recorded in `cache_mismatches`
        ("<day> row" or "<day> fresh")."""
        with ClockWatch(sql=True) as cw:
            fc = forecast.build(self.conn, today=day)
        self._merge("forecast", cw.reads)
        if self.verify_cache:
            a = json.dumps(fc, sort_keys=True, default=str)
            # a cold worker: nothing in memory, the stored row as another
            # process wrote it
            with forecast._LEARNED_LOCK:
                for store in (forecast._HISTORY, forecast._CLOSED_ROWS,
                              forecast._BUILT):
                    store.clear()
            self.conn.execute(
                "UPDATE forecast_learned SET written_by = '' "
                "WHERE as_of = %s", (day,))
            self.conn._memo = None
            from_row = json.dumps(forecast.build(self.conn, today=day),
                                  sort_keys=True, default=str)
            # learned again from the ledger (the row back as this
            # process's own, which forget_learned sets aside)
            self.conn.execute(
                "UPDATE forecast_learned SET written_by = %s "
                "WHERE as_of = %s", (forecast._writer(), day))
            forecast.forget_learned()
            self.conn._memo = None
            fresh = json.dumps(forecast.build(self.conn, today=day),
                               sort_keys=True, default=str)
            if from_row != a:
                self.cache_mismatches.append(f"{day.isoformat()} row")
            if fresh != a:
                self.cache_mismatches.append(f"{day.isoformat()} fresh")
        return fc
