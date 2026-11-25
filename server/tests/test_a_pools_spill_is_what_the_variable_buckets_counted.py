"""A pool's "past the pool" is what the variable buckets counted
(specs/today-figures.md, "A pool printed by name prints its own line,
from the engine's own overflow").

For generated households (tests/envelope_households.py: pools set up,
resized or deleted between frozen months, charges past the pool), every
pool's line on a month surface — the Month card of every month this
year, Today's Envelopes row, the Bills page — prints as its spill that
month's overflow rows for the pool in the variable buckets, and the
Year's line a yearly pool's overflow rows over the Year's months: each
read independently, month by month, from each month's own state
(lenses.month_state: food and other rows labelled as the pool's spill).
Never the spent less the pool. Each row's figure is the sum of its
pools' figures for the row's period, as printed.
"""

from __future__ import annotations

import datetime as dt
import unittest

from oikonome.engine.reporting import whole_dollars
from oikonome.web import api_bills, lenses, report
from oikonome.engine import budget

from . import props
from .envelope_households import EnvelopeHousehold
from .util import TODAY


def _spills(conn) -> dict[int, dict[str, float]]:
    """month → pool payee → the dollars its spill rows put in Food and
    Everything else that month, from the month's own state."""
    out: dict[int, dict[str, float]] = {}
    memo: dict = {}
    for m in range(1, TODAY.month + 1):
        st = lenses.month_state(conn, TODAY.year, m, TODAY, memo=memo)
        got: dict[str, float] = {}
        for k in ("food", "other"):
            for r in st["buckets"][k].get("rows") or []:
                if r.get("category") == "envelope overflow":
                    got[r["name"]] = got.get(r["name"], 0.0) + r["amount"]
        out[m] = got
    return out


def _want(spills, m: int, payee: str) -> int:
    return whole_dollars(spills[m].get(payee, 0.0))


def _past_words(text: str, past: int, period: str) -> None:
    if past > 0:
        assert f"${past:,} past the pool {period}, counted in spending" \
            in text, (text, past, period)
    else:
        assert "past the pool" not in text, text


class ThePoolsSpillIsTheBucketsSpill(unittest.TestCase):

    def test_generated_households(self):
        for seed, dated in ((s, d) for s in props.seeds(default=8)
                            for d in (False, True)):
            h = EnvelopeHousehold(seed, dated=dated)
            conn = h.conn
            try:
                spills = _spills(conn)
                for m in range(1, TODAY.month + 1):
                    mo = lenses.month_summary(conn, TODAY.year, m,
                                              today=TODAY)
                    when = ("this month" if m == TODAY.month
                            else f"in {dt.date(TODAY.year, m, 1):%B}")
                    for e in mo["bills"]["envelopes"]:
                        want = _want(spills, m, e["payee"])
                        with self.subTest(seed=seed, dated=dated, m=m,
                                          pool=e["payee"]):
                            self.assertEqual(e["pair_past"], want)
                            _past_words(e["pair_text"], want, when)
                            self.assertEqual(e["overflow"], round(
                                spills[m].get(e["payee"], 0.0), 2))
                    row = next((r for r in mo["plan_rows"]
                                if r["label"] == "Envelopes"), None)
                    if row is not None:
                        bd = row["breakdown"]
                        self.assertEqual(
                            bd["overflow"],
                            sum(it["overflow"] for it in bd["items"]))
                        for it in bd["items"]:
                            self.assertEqual(it["overflow"], whole_dollars(
                                spills[m].get(it["payee"], 0.0)))
                # Today's row and the Bills page read the running month
                st = report.gather(conn, TODAY)
                for it in st["envelopes_breakdown"]["items"]:
                    with self.subTest(seed=seed, dated=dated, at="today",
                                      pool=it["payee"]):
                        self.assertEqual(it["pair_past"], _want(
                            spills, TODAY.month, it["payee"]))
                stats = api_bills._month_bill_stats(
                    conn, budget.load_config(conn), TODAY)
                for e in st["buckets"]["fixed"]["envelopes"]:
                    with self.subTest(seed=seed, dated=dated, at="bills",
                                      pool=e["payee"]):
                        _past_words(stats[e["payee"]]["pool_text"],
                                    _want(spills, TODAY.month, e["payee"]),
                                    "this month")
                # the Year: the row is its lines
                yr = lenses.year_summary(conn, TODAY.year, today=TODAY)
                row = next((r for r in yr["plan_rows"]
                            if r["label"] == "Envelopes"), None)
                if row is not None:
                    bd = row["breakdown"]
                    self.assertEqual(bd["overflow"], sum(
                        it["pair_past"] for it in bd["items"]))
                    names = [it["payee"] for it in bd["items"]]
                    for it in bd["items"]:
                        if it["period_months"] > 1 \
                                and names.count(it["payee"]) == 1:
                            want = whole_dollars(sum(
                                spills[k].get(it["payee"], 0.0)
                                for k in range(1, TODAY.month + 1)))
                            self.assertEqual(it["pair_past"], want)
                            _past_words(it["pair_text"], want,
                                        f"in {TODAY.year}")
            finally:
                h.close()


if __name__ == "__main__":
    unittest.main()
