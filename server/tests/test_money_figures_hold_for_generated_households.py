"""The money figures the pages show hold their rules for generated
households (specs/invariants-and-fuzzing.md, section 1; households from
tests/money_households.py, seeds from tests/props.py).

* Accounts: the headline is Net Worth's "Financial accounts" figure, and
  the sections beneath it, as shown, add up to it exactly; within a
  section, the institution subtotals add up to the section's total.
* Net Worth: the institution and asset-class rows, as shown, each add up
  to the financial total, and the whole is that plus the property layer.
* Cash Flow: a year's Saved is the sum of its months' income less spend
  over the months the feed measured, and its monthly bars add up to it.
* An own transfer between two accounts changes no spending, income or
  saved figure, and moves no net worth.
* A linked second source of an account (its rows mirrored) changes no
  figure.
* Excluding an account takes out exactly its own rows: the figures equal
  those of the same ledger without them.
* Splitting a charge moves money between categories and changes no total.

Parts shown under a total add up to it by largest-remainder rounding
(the product decision recorded in the note as D1).
"""

from __future__ import annotations

import datetime as dt
import unittest

from oikonome.engine import budget, links, reporting, splits

from . import props
from .money_households import Household
from .util import TODAY


def _cents(x) -> int:
    return int(round(float(x) * 100))


def cashflow(conn) -> dict:
    cf = reporting.compute_cashflow(conn, TODAY)
    return {"savings_by_year": cf["savings_by_year"],
            "monthly_by_year": cf["monthly_by_year"],
            "income_by_month": cf["income_by_month"],
            "savings_by_year_months": cf["savings_by_year_months"]}


def networth_total(conn) -> float:
    return reporting.compute_networth(conn, trend=False)["current_total"]


class _Seeded(unittest.TestCase):
    """Each test runs once per seed, as a subtest naming it."""

    def households(self, seeds=None, **kw):
        for seed in seeds or props.seeds():
            yield seed, (lambda s=seed: _Opened(s, **kw))


class _Opened:
    def __init__(self, seed, **kw):
        self.seed, self.kw = seed, kw

    def __enter__(self) -> Household:
        self.hh = Household(self.seed, **self.kw)
        return self.hh

    def __exit__(self, *exc):
        self.hh.close()


# balances only (no ledger) build in milliseconds, so the parts-and-totals
# rules get many more households than the ledger rules
BALANCE_SEEDS = tuple(range(1, 41))


class PartsAddUpToTheirShownTotal(_Seeded):

    def test_accounts_sections_and_subtotals_add_up(self):
        for seed, opened in self.households(BALANCE_SEEDS, ledger=False):
            with self.subTest(seed=seed), opened() as hh:
                at = reporting.account_totals(hh.conn)
                self.assertEqual(at["financial_total"], networth_total(hh.conn))
                counted = [c["counted_total"] for pane in at["sections"].values()
                           for c in pane.values()]
                self.assertEqual(sum(_cents(c) for c in counted),
                                 _cents(at["financial_total"]), at["sections"])
                for pane in at["sections"].values():
                    for cat in pane.values():
                        self.assertEqual(
                            sum(_cents(v) for v in cat["by_item"].values()),
                            _cents(cat["total"]), cat)

    def test_net_worth_rows_add_up_to_the_total(self):
        for seed, opened in self.households(BALANCE_SEEDS, ledger=False):
            with self.subTest(seed=seed), opened() as hh:
                nw = reporting.compute_networth(hh.conn, trend=False)
                total = _cents(nw["current_total"])
                for key in ("by_institution", "by_asset_class"):
                    self.assertEqual(sum(_cents(v) for _, v in nw[key]), total,
                                     (key, nw[key]))
                self.assertEqual(_cents(nw["full_total"]),
                                 total + _cents(nw["property_net"]))


class CashFlowSavedIsIncomeLessSpend(_Seeded):

    def test_a_years_saved_is_the_sum_of_its_measured_months(self):
        measured = 0
        for seed, opened in self.households():
            with self.subTest(seed=seed), opened() as hh:
                cf = reporting.compute_cashflow(hh.conn, TODAY)
                for y, inc, sp, net, rate in cf["savings_by_year"]:
                    known, total = cf["savings_by_year_months"][y]
                    self.assertLessEqual(known, total)
                    bars = cf["monthly_by_year"].get(y, [])
                    # a year with an incomplete month says so, and no
                    # other year does
                    self.assertEqual(y in cf["savings_by_year_notes"],
                                     known < total, y)
                    if net is None:
                        # a year with no measured month prints no saved
                        # figure
                        self.assertEqual(known, 0, y)
                        self.assertIsNone(inc)
                        self.assertIsNone(rate)
                        continue
                    self.assertGreater(known, 0, y)
                    self.assertEqual(len(bars), known, (y, bars))
                    # the slots are the same bars, an incomplete month null
                    slots = cf["months_by_year"][y]
                    self.assertEqual(len(slots), total, (y, slots))
                    self.assertEqual([v for _m, v in slots if v is not None],
                                     [v for _m, v in bars], y)
                    # each month's income and spend are whole cents, so
                    # the bars add up to the year exactly
                    self.assertEqual(sum(_cents(v) for _, v in bars),
                                     _cents(net), (y, bars))
                    measured += 1
        # a household whose feed measures no month proves nothing here
        self.assertGreater(measured, 0)


class AnOwnTransferMovesNoFigure(_Seeded):

    def test_a_transfer_between_own_accounts_changes_nothing(self):
        for seed, opened in self.households():
            with self.subTest(seed=seed), opened() as hh:
                before = cashflow(hh.conn)
                nw = networth_total(hh.conn)
                amount = round(hh.rng.uniform(1, 2000), 2)
                day = hh.months[-1] + dt.timedelta(days=hh.rng.randint(0, 13))
                hh.transfer(day, amount)
                hh.conn.execute("UPDATE accounts SET balance_current = "
                                "balance_current - %s WHERE id = 'chk'", (amount,))
                hh.conn.execute("UPDATE accounts SET balance_current = "
                                "balance_current + %s WHERE id = 'sav'", (amount,))
                self.assertEqual(cashflow(hh.conn), before)
                self.assertAlmostEqual(networth_total(hh.conn), nw, places=2)


class ASecondSourceOfAnAccountChangesNoFigure(_Seeded):

    def test_a_linked_twin_with_mirrored_rows_counts_once(self):
        for seed, opened in self.households():
            with self.subTest(seed=seed), opened() as hh:
                c = hh.conn
                before = (cashflow(c), reporting.account_totals(c),
                          reporting.compute_networth(c, trend=False)["by_asset_class"])
                c.execute("INSERT INTO accounts (id,item_id,name,type,subtype,"
                          "balance_current,balance_available) SELECT 'chk-twin',"
                          "item_id,name,type,subtype,balance_current,"
                          "balance_available FROM accounts WHERE id='chk'")
                c.execute("""INSERT INTO transactions (id,account_id,date,amount,
                               name,merchant_name,category_primary,
                               category_detailed,pending,removed,raw)
                             SELECT 'tw-' || id,'chk-twin',date,amount,name,
                               merchant_name,category_primary,category_detailed,
                               pending,removed,raw
                               FROM transactions WHERE account_id='chk'""")
                links.create(c, ["chk", "chk-twin"])
                links.set_shadow_scope(c)
                after = (cashflow(c), reporting.account_totals(c),
                         reporting.compute_networth(c, trend=False)["by_asset_class"])
                self.assertEqual(after[0], before[0])
                self.assertEqual(after[1]["financial_total"],
                                 before[1]["financial_total"])
                self.assertEqual(after[2], before[2])


class ExcludingAnAccountTakesOutOnlyItsRows(_Seeded):

    def test_excluded_equals_absent(self):
        for seed, opened in self.households():
            with self.subTest(seed=seed), opened() as hh:
                c = hh.conn
                victim = hh.rng.choice(hh.cards)
                with budget.config_txn(c) as cfg:
                    cfg["excluded_accounts"] = [victim]
                excluded = cashflow(c)
                with budget.config_txn(c) as cfg:
                    cfg["excluded_accounts"] = []
                c.execute("UPDATE transactions SET removed = 1 "
                          "WHERE account_id = %s", (victim,))
                self.assertEqual(excluded, cashflow(c))


class ASplitChangesNoTotal(_Seeded):

    def test_splitting_a_charge_keeps_every_total(self):
        for seed, opened in self.households():
            with self.subTest(seed=seed), opened() as hh:
                c = hh.conn
                row = c.execute(
                    "SELECT id, amount FROM transactions WHERE amount > 1 "
                    "AND category_primary NOT IN ('INCOME','LOAN_PAYMENTS',"
                    "'TRANSFER_OUT','TRANSFER_IN') ORDER BY id LIMIT 1"
                ).fetchone()
                if row is None:
                    continue
                before = cashflow(c)
                spend = reporting.compute_spending(c, TODAY)
                cents = _cents(row["amount"])
                first = hh.rng.randint(1, cents - 1)
                with c.transaction():
                    splits.set_split(c, row["id"], [
                        {"category": "FOOD_AND_DRINK", "amount": first / 100},
                        {"category": "MEDICAL", "amount": (cents - first) / 100}])
                self.assertEqual(cashflow(c), before)
                self.assertEqual(_totals(reporting.compute_spending(c, TODAY)),
                                 _totals(spend))


def _totals(sp: dict) -> dict:
    """The spending page's totals, without the per-category breakdown a
    split is meant to change."""
    return {k: v for k, v in sp.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)}


if __name__ == "__main__":
    unittest.main()
