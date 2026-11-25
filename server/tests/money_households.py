"""Seeded synthetic households for the database-backed money properties
(specs/invariants-and-fuzzing.md, section 1).

Lighter than tests/roundtrip_builders.py on purpose: that builder fills
every table the export carries; these properties read only accounts and
the ledger, and a household here builds in tens of milliseconds, so a
property can afford one per seed per test.

A household holds a checking and a savings account, one or two cards, an
investment account and sometimes a loan; balances to the cent or, for the
investment and some bank accounts, to three or four places (feeds report
them so); and up to a year of ledger ending at TODAY: paychecks, spending
in several categories on the cards and checking, card payments as paired
transfers, savings transfers, and months the bank feed missed.

With `plan=True` it also carries a plan: a few monthly and yearly bills
and sometimes an envelope, budgets as set, a typed income or none (then
the paycheck series is the income), and up to three savings goals in
either mode. `conn=` builds into an existing household (a signed-up
tenant whose session the caller holds) instead of a fresh one.
"""

from __future__ import annotations

import datetime as dt
import random

from .util import TODAY, add_bill, add_txn, make_db, write_config

SPEND_CATS = ("FOOD_AND_DRINK", "GENERAL_MERCHANDISE", "TRANSPORTATION",
              "ENTERTAINMENT", "PERSONAL_CARE", "MEDICAL")
MERCHANTS = ("Corner Grocer", "Fuel Stop", "Book Nook", "Cinema Nine",
             "Pharmacy Plus", "Hardware Hub", "Noodle Bar")


def _bal(rng: random.Random, lo: float, hi: float, places: int = 2) -> float:
    return round(rng.uniform(lo, hi), places)


def _months_back(n: int) -> list[dt.date]:
    out, d = [], TODAY.replace(day=1)
    for _ in range(n):
        out.append(d)
        d = (d - dt.timedelta(days=1)).replace(day=1)
    return out[::-1]


class Household:
    """`conn` (tenant-scoped, the caller closes it) and what was built."""

    def __init__(self, seed: int, *, ledger: bool = True, plan: bool = False,
                 conn=None):
        self.rng = rng = random.Random(seed)
        self.owned = conn is None
        self.conn = conn = make_db() if conn is None else conn
        write_config(conn)
        sub = rng.random() < 0.6           # sub-cent balances on some feeds
        conn.execute("UPDATE accounts SET balance_current=%s, "
                     "balance_available=%s WHERE id='chk'",
                     (b := _bal(rng, 200, 9000, 3 if sub else 2), b))
        conn.execute("UPDATE accounts SET balance_current=%s WHERE id='card'",
                     (_bal(rng, 0, 2500),))
        rows = [("sav", "Test Savings", "depository", "savings",
                 _bal(rng, 0, 40000, 4 if sub else 2)),
                ("inv", "Test Brokerage", "investment", "brokerage",
                 _bal(rng, 0, 90000, rng.choice((2, 3, 4)))),
                ]
        if rng.random() < 0.5:
            rows.append(("card2", "Test Card Two", "credit", "credit card",
                         _bal(rng, 0, 1500)))
        if rng.random() < 0.5:
            rows.append(("loan", "Test Mortgage", "loan", "mortgage",
                         _bal(rng, 50000, 300000)))
        # the brokerage is its own login: a bank login that also holds an
        # investment account is a brokerage's cash, whose deposits never
        # say from when income is measured, so with every account on one
        # login no month's Saved figure would be known at all
        conn.execute("INSERT INTO items (id, aggregator, institution_name, "
                     "access_token) VALUES ('it-brk','test','Test Brokerage',"
                     "'tok-test-2') ON CONFLICT (tenant_id, id) DO NOTHING")
        for aid, name, typ, subtype, bal in rows:
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,"
                "balance_current) VALUES (%s,%s,%s,%s,%s,%s)",
                (aid, "it-brk" if typ == "investment" else "it1", name, typ,
                 subtype, bal))
        self.cards = ["card"] + (["card2"] if any(r[0] == "card2"
                                                  for r in rows) else [])
        self.months = _months_back(rng.randint(3, 12)) if ledger else []
        self.missed = {m for m in self.months[:-1] if rng.random() < 0.15}
        n = self.n = 0
        for m in self.months:
            last = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1) \
                - dt.timedelta(days=1)
            end = min(last, TODAY)

            def day(lo=1):
                return m + dt.timedelta(days=rng.randint(lo - 1,
                                                         (end - m).days))
            if m in self.missed:
                # the bank feed missed the month: only the cards posted
                for _ in range(rng.randint(1, 4)):
                    n += 1
                    add_txn(conn, day(), _bal(rng, 3, 120), rng.choice(
                        MERCHANTS).upper(), primary=rng.choice(SPEND_CATS),
                        account=rng.choice(self.cards), txn_id=f"h{n}")
                continue
            for _ in range(rng.randint(1, 2)):
                n += 1
                add_txn(conn, day(), -_bal(rng, 900, 4200), "PAYROLL ACME",
                        primary="INCOME", account="chk", txn_id=f"h{n}")
            for _ in range(rng.randint(3, 12)):
                n += 1
                add_txn(conn, day(), _bal(rng, 2, 260), rng.choice(
                    MERCHANTS).upper(), primary=rng.choice(SPEND_CATS),
                    account=rng.choice(self.cards + ["chk"]), txn_id=f"h{n}")
            if rng.random() < 0.7:                 # a card payment, paired
                amt, d, card = _bal(rng, 50, 900), day(), rng.choice(self.cards)
                n += 1
                add_txn(conn, d, amt, "CARD PAYMENT", primary="LOAN_PAYMENTS",
                        detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
                        account="chk", txn_id=f"h{n}")
                n += 1
                add_txn(conn, d, -amt, "PAYMENT THANK YOU",
                        primary="LOAN_PAYMENTS",
                        detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
                        account=card, txn_id=f"h{n}")
            if rng.random() < 0.5:
                self.transfer(day(), _bal(rng, 20, 800))
        self.n = n
        if plan:
            self.plan()

    def plan(self):
        """Bills, budgets, an income and savings goals (see the module
        note). Every amount is whole cents."""
        rng, conn = self.rng, self.conn
        payees = ["Rent Example", "Power Co", "Phone Plan", "Gym Club",
                  "Insurance Mutual", "Streaming Box"]
        rng.shuffle(payees)
        for payee in payees[:rng.randint(1, 4)]:
            yearly = rng.random() < 0.25
            due = TODAY + dt.timedelta(days=rng.randint(-20, 40))
            add_bill(conn, payee, _bal(rng, 15, 1800), next_due=due,
                     frequency="YEARLY" if yearly else "MONTHLY",
                     last_seen=due - dt.timedelta(days=365 if yearly else 31))
        if rng.random() < 0.4:
            add_bill(conn, "Hobby Pool", _bal(rng, 40, 300),
                     bill_type="envelope", frequency=None,
                     category="ENTERTAINMENT", match_category=True)
        goals = []
        for i in range(rng.randint(0, 3)):
            g = {"name": f"Goal {i + 1}", "target": _bal(rng, 500, 20000),
                 "monthly_plan": _bal(rng, 0, 600), "tokens": [],
                 "start_balance": 0}
            if rng.random() < 0.5:
                g["account_id"] = "sav"
            if rng.random() < 0.25:
                g["mode"] = "sweep"
            goals.append(g)
        self.goals = goals
        over = {"today_view": "detail",
                "food_monthly": _bal(rng, 100, 1500),
                "other_monthly": _bal(rng, 100, 2500),
                "savings_goals": goals}
        if rng.random() < 0.6:
            over["budgeted_income_monthly"] = _bal(rng, 1500, 9000)
        else:
            # no typed income: the paycheck series is the plan's income,
            # with a check still due this month or not
            due = TODAY + dt.timedelta(days=rng.randint(-10, 14))
            add_bill(conn, "Payroll Acme", _bal(rng, 900, 4200), income=True,
                     next_due=due, last_seen=due - dt.timedelta(days=31))
        write_config(conn, **over)

    def transfer(self, day: dt.date, amount: float, src="chk", dst="sav"):
        """An own transfer: out of `src`, into `dst`, the same day."""
        self.n = getattr(self, "n", 0) + 1
        add_txn(self.conn, day, amount, "TRANSFER TO SAVINGS",
                primary="TRANSFER_OUT", account=src, txn_id=f"x{self.n}a")
        add_txn(self.conn, day, -amount, "TRANSFER FROM CHECKING",
                primary="TRANSFER_IN", account=dst, txn_id=f"x{self.n}b")

    def close(self):
        if self.owned:
            self.conn.close()
