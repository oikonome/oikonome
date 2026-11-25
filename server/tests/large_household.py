"""A large generated household for the forecast's time budget, shaped like
a long-lived real one: a long sparse history tail, many bills with years
of payments (some switched off), and many card accounts, most of them
closed or idle: tens of
thousands of transactions over three years on a checking account and two
cards, biweekly paychecks, monthly bills, card payments after payday.
Figures and names are invented."""

from __future__ import annotations

import datetime as dt
import random

from oikonome.engine.compat import jsonb

from .util import TODAY, add_bill, make_db, write_config

SHOPS = [f"SHOP {n}" for n in range(1, 400)]


BILLS = 40          # bills with years of payments
CARDS = 16          # card accounts, most closed or idle
TAIL_YEARS = 20     # a sparse history before the busy years


def build(seed: int = 7, years: int = 3, per_day: int = 36,
          shaped: bool = True):
    rng = random.Random(seed)
    conn = make_db()
    write_config(conn, food_monthly=1500, other_monthly=2500)
    conn.execute("UPDATE accounts SET balance_available=8000, "
                 "balance_current=8000 WHERE id='chk'")
    conn.execute("INSERT INTO accounts (id,item_id,name,type,subtype,"
                 "balance_current) VALUES ('card2','it1','Harbor card',"
                 "'credit','credit card',0)")
    rows = []
    n = 0
    start = TODAY - dt.timedelta(days=365 * years)
    d = start
    bal = {"card": 0.0, "card2": 0.0}
    while d < TODAY:
        for _ in range(rng.randint(per_day - 8, per_day + 8)):
            acct = rng.choice(["card", "card", "card2", "chk"])
            amt = round(rng.uniform(3, 90), 2)
            n += 1
            rows.append((f"x{n}", acct, d, amt, rng.choice(SHOPS),
                         rng.choice(["FOOD_AND_DRINK", "GENERAL_MERCHANDISE",
                                     "GENERAL_SERVICES"]), None))
            if acct in bal:
                bal[acct] += amt
        if (d - start).days % 14 == 0:
            n += 1
            rows.append((f"x{n}", "chk", d, -4200.0, "NORTHSTAR PAYROLL",
                         "INCOME", "INCOME_WAGES"))
        if (d - start).days % 14 == 1:
            for acct in ("card", "card2"):
                if bal[acct] > 0:
                    for leg, amt in ((acct, -bal[acct]), ("chk", bal[acct])):
                        n += 1
                        rows.append((f"x{n}", leg, d, round(amt, 2),
                                     "CARD PAYMENT", "LOAN_PAYMENTS",
                                     "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"))
                    bal[acct] = 0.0
        if d.day == 3:
            n += 1
            rows.append((f"x{n}", "chk", d, 1850.0, "MAPLE RENTALS",
                         "RENT_AND_UTILITIES", None))
        if d.day == 20:
            n += 1
            rows.append((f"x{n}", "card", d, round(rng.uniform(90, 160), 2),
                         "ACME POWER", "RENT_AND_UTILITIES", None))
        d += dt.timedelta(days=1)
    amounts: list[float] = []
    if shaped:
        # idle and closed cards: charges years ago, nothing owed now
        for k in range(CARDS - 2):
            cid = f"old{k}"
            conn.execute("INSERT INTO accounts (id,item_id,name,type,subtype,"
                         "balance_current) VALUES (%s,'it1',%s,'credit',"
                         "'credit card',0)", (cid, f"Old card {k}"))
            for j in range(rng.randint(20, 120)):
                n += 1
                rows.append((f"x{n}", cid, start - dt.timedelta(
                    days=rng.randint(30, 365 * 8)),
                    round(rng.uniform(5, 80), 2), rng.choice(SHOPS),
                    "GENERAL_MERCHANDISE", None))
        # a long sparse tail before the busy years
        d = start - dt.timedelta(days=365 * TAIL_YEARS)
        while d < start:
            n += 1
            rows.append((f"x{n}", "chk", d, round(rng.uniform(5, 200), 2),
                         rng.choice(SHOPS), "GENERAL_MERCHANDISE", None))
            d += dt.timedelta(days=rng.randint(2, 6))
        # many bills, each paid monthly for the busy years
        for b in range(BILLS):
            amt = round(rng.uniform(9, 400), 2)
            amounts.append(amt)
            acct = rng.choice(["chk", "card", "card2"])
            day = 1 + (b * 7) % 28
            d = start.replace(day=day)
            while d < TODAY:
                n += 1
                rows.append((f"x{n}", acct, d,
                             round(amt * rng.uniform(0.9, 1.1), 2),
                             f"UTILITY CO {b}", "RENT_AND_UTILITIES", None))
                if acct in bal:
                    bal[acct] += amt
                y, m = (d.year, d.month + 1) if d.month < 12 else (
                    d.year + 1, 1)
                d = dt.date(y, m, day)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO transactions (id, account_id, date, amount, name, "
            "merchant_name, category_primary, category_detailed, pending, "
            "removed, raw) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,0,'{}')",
            [(tid, acct, day, amt, name, name, prim, det)
             for tid, acct, day, amt, name, prim, det in rows])
    for acct, b in bal.items():
        conn.execute("UPDATE accounts SET balance_current=%s WHERE id=%s",
                     (round(b, 2), acct))
        conn.execute(
            "INSERT INTO liabilities (account_id, raw) VALUES (%s, %s)",
            (acct, jsonb({"last_statement_issue_date": "2026-07-08",
                          "next_payment_due_date": "2026-08-03",
                          "last_statement_balance": round(b * 0.7, 2)})))
    add_bill(conn, "NORTHSTAR PAYROLL", 4200.0, income=True,
             frequency="WEEKLY", interval=2,
             next_due=TODAY + dt.timedelta(days=(14 - (TODAY - start).days
                                                 % 14) % 14 or 14),
             last_seen=TODAY - dt.timedelta(days=(TODAY - start).days % 14))
    add_bill(conn, "MAPLE RENTALS", 1850.0, next_due=dt.date(2026, 8, 3),
             last_seen=dt.date(2026, 7, 3))
    add_bill(conn, "ACME POWER", 120.0, next_due=dt.date(2026, 7, 20),
             last_seen=dt.date(2026, 6, 20))
    if shaped:
        off = []
        for b in range(BILLS):
            day = 1 + (b * 7) % 28
            add_bill(conn, f"UTILITY CO {b}", amounts[b],
                     next_due=dt.date(2026, 7, day) if day > 15
                     else dt.date(2026, 8, day),
                     last_seen=dt.date(2026, 6, day))
            if b % 8 == 0:
                off.append(f"UTILITY CO {b}")
        write_config(conn, food_monthly=1500, other_monthly=2500,
                     disabled_bills=off)
    return conn, len(rows)
