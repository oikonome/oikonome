"""Every report reads one answer to "from when is this household measured".

Which day a feed's history begins, which months have both their income and
their spend on file, and whether a previous window is covered well enough
to compare against are one value per household (engine/coverage.py,
MeasuredWindow). Each row below is a synthetic ledger shape with the answer
it must give, and the answer is asserted through EVERY consumer at once —
the income coverage, the Month and Year lenses, the month's income and its
deposit rows, the Cash Flow years and monthly bars, the cumulative-savings
line, the flow picture's clip, the Spending and Income tabs' previous
window, and the closed-month cache key — so a site that drifts from the
rule fails the shared row, not a test of its own.

Amounts are invented.
"""

import datetime as dt
import unittest

from oikonome.db import tenancy
from oikonome.engine import budget, coverage, entities, links, reporting
from oikonome.web import lenses

from .test_reporting import add_account, add_holding, add_item, add_raw_txn
from .util import add_txn, make_db, write_config

_MO = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
       "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _spend(conn, day, amount=120.0, account="card"):
    add_txn(conn, day, amount, "CORNER GROCER", primary="FOOD_AND_DRINK",
            account=account)


def _pay(conn, day, amount=2400.0, account="chk"):
    add_txn(conn, day, -amount, "NORTHWIND PAYROLL", primary="INCOME",
            detailed="INCOME_WAGES", account=account)


def _monthly(conn, first: dt.date, last: dt.date, fn, day=None):
    d = first
    while d <= last:
        fn(conn, d if day is None else d.replace(day=day))
        d = (d.replace(day=1) + dt.timedelta(days=32)).replace(day=1)


# ---- the shapes -------------------------------------------------------------

def _checking_mid_month_beside_old_card(conn):
    _monthly(conn, dt.date(2024, 6, 12), dt.date(2025, 4, 12), _spend)
    _pay(conn, dt.date(2025, 3, 20))
    _pay(conn, dt.date(2025, 4, 3))


def _whole_ledger_opens_mid_month(conn):
    _spend(conn, dt.date(2025, 10, 20))
    _pay(conn, dt.date(2025, 10, 20))
    _spend(conn, dt.date(2025, 11, 12))
    _pay(conn, dt.date(2025, 11, 14))


def _card_before_checking_same_month(conn):
    _spend(conn, dt.date(2025, 10, 2))
    _pay(conn, dt.date(2025, 10, 20))
    _spend(conn, dt.date(2025, 11, 12))
    _pay(conn, dt.date(2025, 11, 14))


def _feed_opens_saturday_first_row_monday(conn):
    # Mar 1 2025 is a Saturday: a feed from that day shows Monday Mar 3
    _spend(conn, dt.date(2025, 3, 3))
    _pay(conn, dt.date(2025, 3, 3))
    _spend(conn, dt.date(2025, 4, 9))
    _pay(conn, dt.date(2025, 4, 15))


def _feed_opens_on_a_holiday(conn):
    # Jan 1 is a bank holiday: the first row posts on Jan 2
    _spend(conn, dt.date(2026, 1, 2))
    _pay(conn, dt.date(2026, 1, 2))
    _spend(conn, dt.date(2026, 2, 9))
    _pay(conn, dt.date(2026, 2, 13))


def _card_friday_checking_monday(conn):
    _spend(conn, dt.date(2025, 5, 16))
    _pay(conn, dt.date(2025, 5, 19))
    _spend(conn, dt.date(2025, 6, 9))
    _pay(conn, dt.date(2025, 6, 13))


def _checking_two_weekdays_after_the_card(conn):
    _spend(conn, dt.date(2025, 5, 16))
    _pay(conn, dt.date(2025, 5, 20))
    _spend(conn, dt.date(2025, 6, 9))
    _pay(conn, dt.date(2025, 6, 13))


def _card_imported_a_day_before_its_first_purchase(conn):
    # the card's history opens Sep 1 with a payment; its first purchase is
    # Sep 2 — September is covered, the 1st was a day with no spend
    add_txn(conn, dt.date(2025, 9, 1), -300.0, "CARD PAYMENT THANK YOU",
            primary="TRANSFER_IN", account="card")
    _spend(conn, dt.date(2025, 9, 2))
    _pay(conn, dt.date(2025, 9, 1))
    _spend(conn, dt.date(2025, 10, 8))
    _pay(conn, dt.date(2025, 10, 15))


def _hidden_and_business_checking_older_than_the_feed(conn):
    conn.execute(
        "INSERT INTO accounts (id, item_id, name, type, subtype) VALUES "
        "('old', 'it1', 'Old Checking', 'depository', 'checking'),"
        "('llc', 'it1', 'Shop Checking', 'depository', 'checking')")
    _pay(conn, dt.date(2023, 2, 10), account="old")
    _pay(conn, dt.date(2023, 2, 10), account="llc")
    _monthly(conn, dt.date(2023, 2, 12), dt.date(2025, 8, 12), _spend)
    _pay(conn, dt.date(2025, 6, 2))
    _pay(conn, dt.date(2025, 7, 2))
    conn.execute("UPDATE accounts SET user_removed_at = now() "
                 "WHERE id = 'old'")
    ent = entities.create_entity(conn, name="Shop LLC",
                                 structure="sole_prop")
    entities.assign_account(conn, "llc", ent["id"])
    tenancy.set_entity_scope(conn)


def _sweep_only_brokerage_cash(conn):
    add_item(conn, "itb", "Contoso Brokerage")
    add_account(conn, "bcash", "Contoso Cash", "depository",
                "cash management", 0, item="itb")
    add_account(conn, "bport", "Contoso Portfolio", "investment",
                "brokerage", 10000, item="itb")
    add_txn(conn, dt.date(2023, 3, 10), -400.0, "SWEEP FROM PORTFOLIO",
            primary="TRANSFER_IN", account="bcash")
    _monthly(conn, dt.date(2025, 4, 1), dt.date(2025, 6, 1), _pay, day=1)
    _monthly(conn, dt.date(2025, 4, 1), dt.date(2025, 6, 1), _spend, day=8)


def _feed_that_stopped(conn):
    # checking stops after June 2025; the card carries on. The rest of 2025
    # still has bank rows that year, and 2026 has none: unknown
    _monthly(conn, dt.date(2025, 1, 2), dt.date(2025, 6, 2), _pay, day=2)
    _monthly(conn, dt.date(2025, 1, 9), dt.date(2026, 3, 9), _spend)


def _managed_account_funded_before_its_buys(conn):
    _spend(conn, dt.date(2025, 1, 2))
    _pay(conn, dt.date(2025, 1, 2))
    add_item(conn, "rb", "Evergreen Robo")
    add_account(conn, "rba", "Managed Portfolio", "investment", "brokerage",
                80000, item="rb")
    add_raw_txn(conn, dt.date(2025, 6, 1), -80000, "ACH DEPOSIT", "rba",
                {"type": "deposit"})
    add_raw_txn(conn, dt.date(2025, 6, 5), 80000, "BUY VTI", "rba",
                {"units": 800, "ticker": "VTI", "unitprice": 100,
                 "type": "buy"})
    add_holding(conn, "rba", "VTI", 800, 100, 80000)


def _managed_account_opened_mid_year(conn):
    _spend(conn, dt.date(2025, 1, 2))
    _pay(conn, dt.date(2025, 1, 2))
    add_item(conn, "rb", "Evergreen Robo")
    add_account(conn, "rba", "Managed Portfolio", "investment", "brokerage",
                60000, item="rb")
    add_raw_txn(conn, dt.date(2025, 9, 1), 60000, "BUY VTI", "rba",
                {"units": 600, "ticker": "VTI", "unitprice": 100,
                 "type": "buy"})
    add_holding(conn, "rba", "VTI", 600, 100, 60000)


# Each row: the ledger, the day it is read on, the expected income coverage,
# months whose income must read known / unknown, and the previous-window
# verdict of `prev_range` for each tab (True = a comparison is shown).
SHAPES = [
    dict(name="checking from Mar 20 beside a card from last year",
         seed=_checking_mid_month_beside_old_card, today=dt.date(2025, 5, 10),
         income=({"2025"}, "2025-04"), known=["2025-04"],
         unknown=["2025-03", "2024-12"], unknown_years=["2024", "2025"],
         prev_range="1m", prev={"spend": True, "income": False},
         flow_all=("2025-04-01", False)),
    dict(name="whole ledger opens Oct 20",
         seed=_whole_ledger_opens_mid_month, today=dt.date(2025, 12, 10),
         income=({"2025"}, "2025-10"), known=["2025-10", "2025-11"],
         unknown=["2025-09"],
         # Oct is a measured month (both sides from the 20th) but not a
         # whole one, so it is no comparison for November
         prev_range="1m", prev={"spend": False, "income": False},
         flow_all=("2025-10-01", False)),
    dict(name="card Oct 2, checking Oct 20",
         seed=_card_before_checking_same_month, today=dt.date(2025, 12, 10),
         income=({"2025"}, "2025-11"), known=["2025-11"],
         unknown=["2025-10"],
         prev_range="1m", prev={"spend": True, "income": False},
         flow_all=("2025-11-01", False)),
    dict(name="feed from Saturday Mar 1, first row Monday Mar 3",
         seed=_feed_opens_saturday_first_row_monday,
         today=dt.date(2025, 5, 10),
         income=({"2025"}, "2025-03"), known=["2025-03", "2025-04"],
         unknown=["2025-02"],
         prev_range="1m", prev={"spend": True, "income": True},
         flow_all=("2025-03-01", False)),
    dict(name="feed from the Jan 1 holiday, first row Jan 2",
         seed=_feed_opens_on_a_holiday, today=dt.date(2026, 3, 10),
         income=({"2026"}, "2026-01"), known=["2026-01", "2026-02"],
         unknown=["2025-12"], known_years=["2026"],
         prev_range="1m", prev={"spend": True, "income": True},
         flow_all=("2026-01-01", False)),
    dict(name="card's first row Friday, checking's Monday",
         seed=_card_friday_checking_monday, today=dt.date(2025, 7, 10),
         income=({"2025"}, "2025-05"), known=["2025-05", "2025-06"],
         unknown=["2025-04"],
         prev_range="1m", prev={"spend": False, "income": False},
         flow_all=("2025-05-01", False)),
    dict(name="checking's first row two weekdays after the card's",
         seed=_checking_two_weekdays_after_the_card,
         today=dt.date(2025, 7, 10),
         income=({"2025"}, "2025-06"), known=["2025-06"],
         unknown=["2025-05"],
         prev_range="1m", prev={"spend": False, "income": False},
         flow_all=("2025-06-01", False)),
    dict(name="card imported from Sep 1, first purchase Sep 2",
         seed=_card_imported_a_day_before_its_first_purchase,
         today=dt.date(2025, 11, 10),
         income=({"2025"}, "2025-09"), known=["2025-09", "2025-10"],
         unknown=["2025-08"],
         prev_range="1m", prev={"spend": True, "income": True},
         flow_all=("2025-09-01", False)),
    dict(name="hidden and business checking older than the personal feed",
         seed=_hidden_and_business_checking_older_than_the_feed,
         today=dt.date(2025, 9, 10),
         income=({"2025"}, "2025-06"), known=["2025-06", "2025-07"],
         unknown=["2023-02", "2025-05"], unknown_years=["2023", "2024"],
         prev_range="1m", prev={"spend": True, "income": True},
         flow_all=("2025-06-01", False)),
    dict(name="sweep-only brokerage cash older than the paychecks",
         seed=_sweep_only_brokerage_cash, today=dt.date(2025, 7, 10),
         income=({"2025"}, "2025-04"), known=["2025-04", "2025-06"],
         unknown=["2023-03", "2025-03"], unknown_years=["2023"],
         prev_range="1m", prev={"spend": True, "income": True},
         flow_all=("2025-04-01", False)),
    dict(name="a bank feed that stopped",
         seed=_feed_that_stopped, today=dt.date(2026, 4, 10),
         income=({"2025"}, "2025-01"), known=["2025-01", "2025-09"],
         unknown=["2026-01", "2026-03"], known_years=["2025"],
         unknown_years=["2026"],
         prev_range="1m", prev={"spend": True, "income": True},
         flow_all=("2025-01-01", False)),
    dict(name="managed account funded Jun 1, first buy Jun 5",
         seed=_managed_account_funded_before_its_buys,
         today=dt.date(2025, 12, 31),
         income=({"2025"}, "2025-01"), known=["2025-01"], unknown=[],
         prev_range="1m", prev={"spend": True, "income": True},
         flow_all=("2025-01-01", False),
         fee=("2025", 0.0025 * 80000 * 210 / 365)),
    dict(name="managed account opened mid-year",
         seed=_managed_account_opened_mid_year, today=dt.date(2025, 12, 31),
         income=({"2025"}, "2025-01"), known=["2025-01"], unknown=[],
         prev_range="1m", prev={"spend": True, "income": True},
         flow_all=("2025-01-01", False),
         fee=("2025", 0.0025 * 60000 * 122 / 365)),
]


class MeasuredWindowFeedShapes(unittest.TestCase):
    def _ledger(self, shape):
        conn = make_db()
        write_config(conn)
        shape["seed"](conn)
        links.set_shadow_scope(conn)
        conn.commit()
        conn._memo = None
        return conn

    def test_every_consumer_reads_the_same_window(self):
        for shape in SHAPES:
            with self.subTest(shape["name"]):
                conn = self._ledger(shape)
                try:
                    self._check(conn, shape)
                finally:
                    conn.close()

    def _check(self, conn, shape):
        today = shape["today"]
        years, start = shape["income"]
        self.assertEqual(reporting._income_coverage(conn), (years, start),
                         "income coverage")
        cov = lenses._Coverage(conn)
        cf = reporting.compute_cashflow(conn, today=today)
        bars = {(y, m[0]) for y, ms in cf["monthly_by_year"].items()
                for m in ms}
        for ym, want in ([(k, True) for k in shape["known"]]
                         + [(k, False) for k in shape["unknown"]]):
            y, m = int(ym[:4]), int(ym[5:])
            self.assertEqual(cov.month_known(ym), want, f"lens month {ym}")
            self.assertEqual(reporting.month_income(conn, y, m) is not None,
                             want, f"month_income {ym}")
            nxt = (dt.date(y, m, 1) + dt.timedelta(days=32)).replace(day=1)
            _rows, known = reporting.month_deposit_rows(
                conn, y, m, dt.date(y, m, 1), nxt, excluded=[])
            self.assertEqual(known, want, f"month_deposit_rows {ym}")
            self.assertEqual((ym[:4], _MO[m - 1]) in bars, want,
                             f"Cash Flow monthly bar {ym}")
        saved = {r[0]: r for r in cf["savings_by_year"]}
        line = reporting._cumulative_savings(conn)
        for y, want in ([(k, True) for k in shape.get("known_years", [])]
                        + [(k, False) for k in shape.get("unknown_years", [])]):
            self.assertEqual(cov.year_known(int(y)), want, f"lens year {y}")
            if y in saved:
                self.assertEqual(saved[y][1] is not None, want,
                                 f"Cash Flow year {y}")
            i = next((n for n, (yy, _v) in enumerate(line) if yy == y), None)
            if i is not None and not want:
                before = line[i - 1][1] if i else 0.0
                self.assertEqual(line[i][1], before,
                                 f"cumulative savings flat through {y}")
        # the flow picture's "All" starts at the first measured month
        frm, unknown = shape["flow_all"]
        fb = reporting.flow_breakdown(conn, today, "all")
        self.assertEqual(fb["from"], frm, "flow picture start")
        self.assertEqual(fb["income_unknown"], unknown, "flow picture income")
        # the previous window, on both tabs
        rk = shape["prev_range"]
        sp = reporting.spending_window(conn, today, rk)
        inc = reporting.income_window(conn, today, rk)
        self.assertEqual(sp["prev_total"] is not None, shape["prev"]["spend"],
                         "Spending tab previous window")
        self.assertEqual(inc["prev_total"] is not None,
                         shape["prev"]["income"], "Income tab previous window")
        # the closed-month cache key carries the window it was built under
        w = coverage.window(conn)
        tid = conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        runs = [k for k in reporting._FIXED_MONTH_CACHE
                if k[0] == tid and str(k[2]).startswith("run:")]
        self.assertTrue(runs, "closed months were cached")
        self.assertTrue(all(k[2].endswith(w.fingerprint) for k in runs),
                        "run cache key carries the window fingerprint")
        # Today's pacing anchor is a looser, separate rule: it counts every
        # live row (shadow and business rows included), so it never opens
        # after the reports' coverage day
        ls = budget.ledger_start(conn)
        if ls is not None and w.spend_covered_from is not None:
            self.assertLessEqual(ls, w.spend_covered_from, "ledger_start")
        if "fee" in shape:
            y, want = shape["fee"]
            est = reporting._managed_fee_by_year(conn, ["rba"], 0.0025, today)
            self.assertAlmostEqual(est[y], want, delta=0.01)


class PostingAllowance(unittest.TestCase):
    """The one posting-lag rule every "did the feed start by X" asks."""

    def test_the_second_weekday_is_the_last_day_that_still_covers(self):
        cases = [
            (dt.date(2025, 3, 1), dt.date(2025, 3, 4), True),    # Sat → Tue
            (dt.date(2025, 3, 1), dt.date(2025, 3, 5), False),
            (dt.date(2026, 1, 1), dt.date(2026, 1, 2), True),    # Thu → Fri
            (dt.date(2025, 5, 16), dt.date(2025, 5, 19), True),  # Fri → Mon
            (dt.date(2025, 5, 16), dt.date(2025, 5, 20), False),
            (dt.date(2025, 9, 1), dt.date(2025, 8, 30), True),   # earlier
        ]
        for start, first, want in cases:
            self.assertEqual(coverage.covers(first, start), want,
                             (start, first))
        self.assertFalse(coverage.covers(None, dt.date(2025, 3, 1)))


class OneReadPerCheckout(unittest.TestCase):
    def test_a_page_of_reports_reads_the_window_once(self):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        _checking_mid_month_beside_old_card(conn)
        conn._memo = None
        today = dt.date(2025, 5, 10)
        seen = []
        real = coverage.build

        def spy(c):
            seen.append(1)
            return real(c)
        coverage.build = spy
        try:
            reporting.compute_cashflow(conn, today=today)
            reporting.flow_breakdown(conn, today, "1y")
            reporting.spending_window(conn, today, "1y")
            reporting.income_window(conn, today, "1y")
            lenses._Coverage(conn).month_known("2025-04")
        finally:
            coverage.build = real
        self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
