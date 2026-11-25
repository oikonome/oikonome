"""Seeded households with envelopes, for the Envelopes-row properties
(specs/today-figures.md, "Envelopes are their own row").

A household here holds the accounts and ledger of money_households, plus
a few due-dated bills paid (or not) through the year, and 0–4 pools —
monthly and yearly, merchant-matched — with charges spread across the
months, some of them past the pool. The months January to June of the
fixed TODAY's year are frozen in order, and between two freezes a pool
may be set up, resized or deleted, so the closed months carry the
schedule as it stood.

Only the fixtures' public helpers (tests.util) and the engine's save and
delete doors are used, so the same seed builds the same household under
the code before the Envelopes row (`figures`, run there by
test_envelopes_move_nothing_but_the_split).

`figures(conn)` is what the change must not move: the running month's
plan, verdict, totals, month-end figure and cash tiles, the cash
timeline, and each closed month's verdict and totals.
"""

from __future__ import annotations

import datetime as dt
import json
import random

from oikonome.engine import bills as bills_engine

from .money_households import Household
from .util import TODAY, add_bill, add_txn, freeze_month, write_config

POOL_MERCHANTS = ("Garden Supply", "Pet Clinic", "Bike Workshop",
                  "Music Studio", "Craft Corner", "Tailor Lane")
BILL_PAYEES = ("Rent Sample", "Water Works", "Net Provider",
               "Auto Cover", "Phone Line")


def _amt(rng: random.Random, lo: float, hi: float) -> float:
    return round(rng.uniform(lo, hi), 2)


def _month_days(y: int, m: int) -> int:
    import calendar
    return calendar.monthrange(y, m)[1]


class EnvelopeHousehold:
    """`conn` and what was built: `pools` [{payee, months, amount,
    created, resized, deleted}], `bills` [payee]."""

    def __init__(self, seed: int, *, dated: bool = False):
        """`dated`: each pool carries the day it was set up, in its month
        (bills.created_on) — the households of a pool made after the
        upgrade, which only this checkout can build; without it a pool's
        day is not known, except that a pool there from January may be
        dated before the ledger begins when the column exists (it reads
        the same either way)."""
        self.seed = seed
        self.dated = dated
        self.rng = rng = random.Random(seed * 7919 + 17)
        # the draws added after the first households were recorded, apart,
        # so the earlier draws stay as they were
        self.xrng = xrng = random.Random(seed * 104729 + 3)
        self.base = Household(seed, ledger=True, plan=False)
        conn = self.conn = self.base.conn
        y = TODAY.year
        self.n = 0
        # the budget: some households scale, some type an income
        over = {"food_monthly": _amt(rng, 150, 1200),
                "other_monthly": _amt(rng, 150, 1800),
                "dynamic_variable_budget": rng.random() < 0.5}
        if rng.random() < 0.7:
            over["budgeted_income_monthly"] = _amt(rng, 2500, 9000)
        write_config(conn, **over)
        # due-dated bills, paid in most months on their due day
        payees = list(BILL_PAYEES)
        rng.shuffle(payees)
        self.bills = []
        for payee in payees[:rng.randint(0, 3)]:
            amount = _amt(rng, 20, 1600)
            day = rng.randint(1, 28)
            yearly = rng.random() < 0.2
            if yearly:
                month = rng.randint(1, 12)
                due = dt.date(y if month >= TODAY.month else y + 1, month,
                              day)
                add_bill(conn, payee, amount, next_due=due,
                         frequency="YEARLY",
                         last_seen=due.replace(year=due.year - 1))
                paid = [dt.date(y, month, day)] if month < TODAY.month else []
            else:
                due = dt.date(y, TODAY.month, day)
                if due <= TODAY:
                    nm = TODAY.month % 12 + 1
                    due = dt.date(y + (TODAY.month == 12), nm, day)
                add_bill(conn, payee, amount, next_due=due,
                         last_seen=dt.date(y - 1, 12, day))
                paid = [dt.date(y, m, day) for m in range(1, TODAY.month + 1)
                        if dt.date(y, m, day) <= TODAY
                        and rng.random() < 0.8]
            for d in paid:
                self._txn(d, amount if rng.random() < 0.8
                          else round(amount * rng.uniform(0.9, 1.1), 2),
                          payee.upper(), "RENT_AND_UTILITIES")
            self.bills.append(payee)
        # pools: monthly or yearly, set up at the start or later in the
        # year, sometimes resized or deleted between two closed months
        merchants = list(POOL_MERCHANTS)
        rng.shuffle(merchants)
        self.pools = []
        for name in merchants[:rng.randint(0, 4)]:
            months = 12 if rng.random() < 0.55 else 1
            amount = (_amt(rng, 200, 2400) if months == 12
                      else _amt(rng, 25, 400))
            created = 1 if rng.random() < 0.65 else rng.randint(2, 6)
            resized = (rng.randint(created + 1, 7)
                       if created < 6 and rng.random() < 0.2 else None)
            deleted = (rng.randint(created + 1, 7)
                       if created < 6 and resized is None
                       and rng.random() < 0.15 else None)
            self.pools.append({"payee": name, "months": months,
                               "amount": amount, "created": created,
                               "resized": resized, "deleted": deleted,
                               "new_amount": (round(amount * rng.uniform(
                                   0.3, 1.6), 2) if resized else None)})
            # charges: last December (a yearly pool's year starts fresh)
            # and every month to TODAY; now and then past the pool
            for m in [0] + list(range(1, TODAY.month + 1)):
                d0 = (dt.date(y - 1, 12, 1) if m == 0
                      else dt.date(y, m, 1))
                last = (dt.date(y - 1, 12, 31) if m == 0
                        else min(TODAY, d0.replace(day=_month_days(y, m))))
                for _ in range(rng.choice((0, 0, 1, 1, 2, 3))):
                    d = d0 + dt.timedelta(days=rng.randint(
                        0, (last - d0).days))
                    big = rng.random() < 0.15
                    cap = amount if months == 1 else amount / 3
                    v = _amt(rng, 3, cap * (1.6 if big else 0.6))
                    self._txn(d, v, name.upper(), "GENERAL_MERCHANDISE")
        # now and then a twin: a second pool of the same size and period
        # on the same merchant — which of two pools claims a shared
        # charge is the one thing the claim order decides
        if self.pools and xrng.random() < 0.2 \
                and not (self.pools[0]["resized"] or self.pools[0]["deleted"]):
            p0 = self.pools[0]
            self.pools.append(dict(p0, payee=p0["payee"] + " Spare",
                                   merchant=p0["payee"].lower(),
                                   resized=None, deleted=None))
        # the day each pool was set up (dated), or, where the column
        # exists, a day before the ledger for a pool there from January
        has_day = conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name ="
            " 'bills' AND column_name = 'created_on'").fetchone()
        for p in self.pools:
            p["day"] = None
            if dated:
                p["day"] = dt.date(y, p["created"], xrng.randint(1, 28))
            elif has_day and p["created"] == 1 and xrng.random() < 0.3:
                # before any day the ledger is read on: such a pool reads
                # as one whose day is not known (a pool dated inside the
                # ledger's span is absent from the days before it, by
                # design, and the forecast's history reads those days)
                p["day"] = dt.date(y - 2, xrng.randint(1, 12),
                                   xrng.randint(1, 28))
        # the year's months in order: each pool's change lands before the
        # month it belongs to is frozen
        for m in range(1, TODAY.month + 1):
            for p in self.pools:
                if p["created"] == m:
                    add_bill(conn, p["payee"], p["amount"],
                             bill_type="envelope", frequency=None,
                             interval=p["months"],
                             merchant=p.get("merchant")
                             or p["payee"].lower())
                    if p["day"] is not None:
                        conn.execute("UPDATE bills SET created_on = %s "
                                     "WHERE payee = %s",
                                     (p["day"], p["payee"]))
                if p["resized"] == m:
                    add_bill(conn, p["payee"], p["new_amount"],
                             bill_type="envelope", frequency=None,
                             interval=p["months"],
                             merchant=p.get("merchant")
                             or p["payee"].lower())
                if p["deleted"] == m:
                    bills_engine.delete_bill(conn, p["payee"])
            if m < TODAY.month:
                freeze_month(conn, y, m)

    def _txn(self, d: dt.date, amount: float, name: str, primary: str):
        self.n += 1
        add_txn(self.conn, d, amount, name, primary=primary,
                account=self.rng.choice(["chk", "card"]),
                txn_id=f"e{self.seed}-{self.n}")

    def close(self):
        self.base.close()


# what the change must not move, by name (specs/today-figures.md,
# "Envelopes are their own row": nothing moves but the printed split)
RUNNING_KEYS = ("verdict", "variance", "tolerance", "variable_actual",
                "variable_expected", "plan_surplus", "scheduled_month",
                "variable_scale", "total_actual", "avg_bills",
                "bills_remaining_month", "after_cards_bills",
                "after_cards_bills_paychecks", "income_due_total")
PLAN_KEYS = ("income", "bills", "to_spend", "saved", "excess", "shares")
FIXED_KEYS = ("actual", "expected", "month_budget")


def _pick(d: dict | None, keys) -> dict | None:
    if d is None:
        return None
    return {k: d.get(k) for k in keys}


# fields the forecast has gained since the reference was recorded: new
# figures, not moved ones (each walk's card charges, kept for the
# forecast's nightly record)
ADDED_SINCE_REFERENCE = ("charges",)


def _as_recorded(fc: dict) -> dict:
    return {k: ({kk: vv for kk, vv in v.items()
                 if kk not in ADDED_SINCE_REFERENCE}
                if isinstance(v, dict) else v) for k, v in fc.items()}


def figures(conn) -> dict:
    """The household's unmoving figures, as plain JSON (floats to the
    cent): the running month (report.gather on TODAY), the cash timeline
    (forecast.build) and each closed month of the year (lenses.month_state
    under its snapshot)."""
    from oikonome.engine import forecast
    from oikonome.web import lenses, report
    st = report.gather(conn, TODAY)
    out = {"running": _pick(st, RUNNING_KEYS),
           "plan_line": _pick(st.get("plan_line"), PLAN_KEYS),
           "plan_legs": _pick(st.get("plan_legs"), PLAN_KEYS[:5]),
           "fixed": _pick(st["buckets"]["fixed"], FIXED_KEYS),
           "food": _pick(st["buckets"]["food"], FIXED_KEYS),
           "other": _pick(st["buckets"]["other"], FIXED_KEYS),
           "forecast": _as_recorded(forecast.build(conn, TODAY)),
           "months": {}}
    for m in range(1, TODAY.month):
        ms = lenses.month_state(conn, TODAY.year, m, TODAY)
        out["months"][str(m)] = {
            **_pick(ms, ("verdict", "variance", "total_actual",
                         "variable_actual", "plan_surplus",
                         "scheduled_month")),
            "fixed": _pick(ms["buckets"]["fixed"], FIXED_KEYS),
            "food": _pick(ms["buckets"]["food"], FIXED_KEYS),
            "other": _pick(ms["buckets"]["other"], FIXED_KEYS)}
    return json.loads(json.dumps(out, default=str))
