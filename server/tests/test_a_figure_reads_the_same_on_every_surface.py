"""A figure has one value on every surface that shows it
(specs/invariants-and-fuzzing.md, M10; the figures and their deciding
functions are the ones specs/counted-figures.md lists).

Property: for a generated household with a plan (tests/money_households.py,
one per seed of tests/props.py), each figure in FIGURES reads the same,
to the cent, at every payload path that carries it — Today (the Budget
page draws the same payload), the Month and Year lenses, Cash Flow,
Spending, Net Worth, Accounts, Retirement, Debt, the daily email's context
and the integrations export. A figure the email prints is also found, as
the email rounds it, in the email's own text. A failure names the figure,
the surfaces and the seed.

Not in the table, because they are different figures by design: the Year
lens's excess is the standing one (`forecast.standing_surplus`, the
month's income swapped for the series' average), Cash Flow dates spending
by the day it was paid while the lenses date a bill by its month, and the
Today bills row is this month's schedule while the plan line's bills are
the evened average.
"""

from __future__ import annotations

import unittest

from oikonome.engine.reporting import whole_dollars
from oikonome.web import report

from . import props
from .money_surfaces import Surfaces
from .util import TODAY

MONTH = TODAY.strftime("%Y-%m")


def _cards(p) -> float:
    # the Debt page's cards: those in the plan and those it lists as paid
    # in full every month (specs/debt-plan.md)
    return round(sum(d["balance"] for d in p["debt"]["debts"]
                     if d["kind"] == "card")
                 + sum(c["balance"] for c in p["debt"]["paid_in_full"]), 2)


def _spend_month(p) -> float | None:
    return next((v for ym, v in p["spending"]["by_month"] if ym == MONTH), None)


def _export_spend_month(p) -> float | None:
    return next((m["total"] for m in p["export_spending"]["months"]
                 if m["month"] == MONTH), None)


def _cashflow_income_month(p) -> float | None:
    return next((v for ym, v in p["cashflow"]["income_by_month"]
                 if ym == MONTH), None)


def _bd(p) -> dict:
    """A reply's Bills row's breakdown."""
    row = next((r for r in p["plan_rows"] if r["label"] == "Bills"), None)
    # a month with no dated bill sends no Bills row: nothing planned or due
    return row["breakdown"] if row is not None else {"plan": 0,
                                                     "still_due": 0}


def _set_aside(p) -> int:
    """A reply's Envelopes row's set-aside (0 without a pool)."""
    row = next((r for r in p["plan_rows"] if r["label"] == "Envelopes"),
               None)
    return row["breakdown"]["set_aside"] if row else 0


def _row(p, label: str) -> float | None:
    return next((r["budget"] for r in p["today"]["plan_rows"]
                 if r["label"] == label), None)


# (figure, [(surface, read)]) — `read(payloads, email context)`; a None
# read means the surface does not show the figure for this household,
# which is allowed only when every surface reads None
FIGURES = [
    ("paychecks still due (income_expected)", [
        ("Today", lambda p, e: p["today"]["income_expected"]),
        ("Today cash tile", lambda p, e: p["today"]["income_due_total"]),
        ("email", lambda p, e: e["income_expected"]),
        ("email cash tile", lambda p, e: e["income_due_total"]),
    ]),
    ("the plan's income", [
        ("Today", lambda p, e: p["today"]["income"]),
        ("email", lambda p, e: e["income"]),
    ]),
    ("planned spending (plan_variable)", [
        ("Today", lambda p, e: p["today"]["plan_variable"]),
        ("email", lambda p, e: e["plan_variable"]),
    ]),
    ("the plan's bills (avg_bills)", [
        ("Today", lambda p, e: p["today"]["avg_bills"]),
        ("email", lambda p, e: e["avg_bills"]),
    ]),
    ("the savings plan", [
        ("Today", lambda p, e: p["today"]["savings_plan"]),
        ("Today savings row", lambda p, e: (
            _row(p, "Savings / Investment")
            if (p["today"]["savings_plan"] or 0) > 0.005 else 0)),
        ("email", lambda p, e: e["savings_plan"]),
    ]),
    ("the month's excess (plan_surplus)", [
        ("Today", lambda p, e: p["today"]["plan_surplus"]),
        ("Month lens", lambda p, e: p["lens_month"]["plan_surplus"]),
        ("email", lambda p, e: e["plan_surplus"]),
    ]),
    ("left at month end after cards & bills", [
        ("Today", lambda p, e: p["today"]["after_cards_bills"]),
        ("email", lambda p, e: e["after_cards_bills"]),
    ]),
    ("… bills & paychecks", [
        ("Today", lambda p, e: p["today"]["after_cards_bills_paychecks"]),
        ("email", lambda p, e: e["after_cards_bills_paychecks"]),
    ]),
    ("the verdict", [
        ("Today", lambda p, e: p["today"]["verdict"]),
        ("Month lens", lambda p, e: p["lens_month"]["verdict"]),
        ("email", lambda p, e: e["verdict"]),
        ("export", lambda p, e: p["export_summary"]["verdict"]),
    ]),
    ("variable spending this month", [
        ("Today", lambda p, e: p["today"]["variable_actual"]),
        ("Month lens", lambda p, e: p["lens_month"]["variable_actual"]),
        ("email", lambda p, e: e["variable_actual"]),
        ("export", lambda p, e: p["export_summary"]["month"]["spent"]),
    ]),
    ("the month's variable budget, paced", [
        ("Today", lambda p, e: p["today"]["variable_budget_paced"]),
        ("Today variable bar", lambda p, e: p["today"]["plan_total"]["budget"]),
        ("export", lambda p, e: p["export_summary"]["month"]["budget"]),
    ]),
    ("this month's bills budget", [
        ("Today", lambda p, e: p["today"]["buckets"]["fixed"]["month_budget"]),
        ("export", lambda p, e: p["export_summary"]["month"]["fixed_budget"]),
    ]),
    # the plan the Bills row's parts add up to, in the dollars printed
    ("this month's bills plan, as printed", [
        # the plan less the pools' set-aside (the Envelopes row's
        # caption): the bills plan and the set-aside add up to it
        ("Today", lambda p, e: float(whole_dollars(
            p["today"]["buckets"]["fixed"]["month_budget"])
            - _set_aside(p["today"]))),
        ("Today bills row", lambda p, e: float(_bd(p["today"])["plan"])),
        ("Month lens bills row", lambda p, e: float(_bd(p["lens_month"])["plan"])),
        ("Month lens card",
         # in cents (it adds up to the plan with the set-aside to the
         # cent); a client prints it in whole dollars
         lambda p, e: float(whole_dollars(
             p["lens_month"]["bills"]["bills_planned"]))),
    ]),
    ("this month's bills still due, as printed", [
        ("Today bills row", lambda p, e: _bd(p["today"])["still_due"]),
        ("Month lens bills row", lambda p, e: _bd(p["lens_month"])["still_due"]),
        ("email", lambda p, e: e["bills_breakdown"]["still_due"]),
    ]),
    ("spent today / today's allowance", [
        ("Today", lambda p, e: (p["today"]["day_meter"]["spent"],
                                p["today"]["day_meter"]["allow"])),
        ("export", lambda p, e: (p["export_summary"]["today"]["spent"],
                                 p["export_summary"]["today"]["allowance"])),
    ]),
    ("checking", [
        ("Today", lambda p, e: p["today"]["runway"]["checking"]),
        ("email", lambda p, e: e["runway"]["checking"]),
        ("export", lambda p, e: p["export_summary"]["cash"]["checking"]),
    ]),
    ("card debt", [
        ("Today", lambda p, e: p["today"]["runway"]["card_debt"]),
        ("email", lambda p, e: e["runway"]["card_debt"]),
        ("Retirement", lambda p, e: p["retirement"]["buckets"]["debt"]),
        ("Debt (its cards)", lambda p, e: _cards(p)),
        ("export", lambda p, e: p["export_summary"]["cash"]["card_debt"]),
    ]),
    ("Financial accounts", [
        ("Net Worth", lambda p, e: p["networth"]["current_total"]),
        ("Accounts", lambda p, e: p["accounts"]["totals"]["financial_total"]),
        ("Retirement (what it projects from)",
         lambda p, e: p["retirement"]["buckets"]["total"]),
        ("export summary", lambda p, e: p["export_summary"]["net_worth"]["total"]),
        ("export net worth", lambda p, e: p["export_networth"]["total"]),
    ]),
    ("Financial accounts in whole dollars", [
        ("Net Worth by institution",
         lambda p, e: p["networth"]["rings"]["by_institution"]["shown"]["net"]),
        ("Net Worth by asset class",
         lambda p, e: p["networth"]["rings"]["by_asset_class"]["shown"]["net"]),
        ("Accounts sections", lambda p, e: sum(
            c["shown"]["counted_total"]
            for pane in p["accounts"]["totals"]["sections"].values()
            for c in pane.values())),
    ]),
    ("net worth with property", [
        ("Net Worth", lambda p, e: p["networth"]["full_total"]),
        ("export summary",
         lambda p, e: p["export_summary"]["net_worth"]["with_property"]),
        ("export net worth", lambda p, e: p["export_networth"]["with_property"]),
    ]),
    ("net worth by institution", [
        ("Net Worth", lambda p, e: [list(x) for x in p["networth"]["by_institution"]]),
        ("export", lambda p, e: [[x["institution"], x["total"]]
                                 for x in p["export_networth"]["by_institution"]]),
    ]),
    ("net worth by asset class", [
        ("Net Worth", lambda p, e: [list(x) for x in p["networth"]["by_asset_class"]]),
        ("export", lambda p, e: [[x["asset_class"], x["total"]]
                                 for x in p["export_networth"]["by_asset_class"]]),
    ]),
    ("this month's spending", [
        ("Spending", lambda p, e: _spend_month(p)),
        ("export", lambda p, e: _export_spend_month(p)),
    ]),
    ("this month's income so far", [
        ("Cash Flow", lambda p, e: _cashflow_income_month(p)),
        ("Month lens", lambda p, e: p["lens_month"]["income"]["actual"]),
    ]),
]

def _line(e, key, raw):
    """A plan-line term as the email prints it: the whole dollars
    budget.plan_line rounded to the printed excess, when there is one."""
    return e["plan_line"][key] if e.get("plan_line") else e[raw]


# figures the email's plain text prints, as the email rounds them
EMAIL_TEXT = (
    ("the plan's income", lambda e: _line(e, "income", "income"), "{} in"),
    ("the plan's bills", lambda e: _line(e, "bills", "avg_bills"), "{} bills"),
    ("planned spending", lambda e: _line(e, "to_spend", "plan_variable"),
     "{} to spend"),
    # the plan line's excess in the Budget summary's words
    ("the month's excess", lambda e: _line(e, "excess", "plan_surplus"),
     "{} planned excess"),
)


def _norm(v):
    """Money to the cent; lists and tuples element by element."""
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 2) + 0.0
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    return v


class AFigureReadsTheSameOnEverySurface(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # eight households: fewer leave some figure (a paycheck still due)
        # unshown by every one of them
        cls.households = [Surfaces(s) for s in props.seeds(8)]

    @classmethod
    def tearDownClass(cls):
        for s in cls.households:
            s.close()

    def test_every_surface_shows_the_same_value(self):
        """For each figure, every payload path carrying it holds one
        value, to the cent."""
        shown = {name: 0 for name, _ in FIGURES}
        for s in self.households:
            for name, reads in FIGURES:
                values = {where: _norm(read(s.payload, s.email))
                          for where, read in reads}
                with self.subTest(figure=name, seed=s.seed):
                    self.assertEqual(len(set(map(repr, values.values()))), 1,
                                     values)
                if any(v is not None for v in values.values()):
                    shown[name] += 1
        # a figure no generated household ever shows proves nothing
        self.assertEqual([n for n, k in shown.items() if not k], [])

    def test_the_email_prints_the_figures_it_carries(self):
        """The email's plain text prints its own context's figures, as
        the email rounds them (whole dollars)."""
        for s in self.households:
            for name, read, fmt in EMAIL_TEXT:
                v = read(s.email)
                if v is None:
                    continue
                with self.subTest(figure=name, seed=s.seed):
                    self.assertIn(fmt.format(report._money(v)), s.email_text)


if __name__ == "__main__":
    unittest.main()
