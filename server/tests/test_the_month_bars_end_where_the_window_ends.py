"""The bars under "Left over, by month" end where the chosen timeframe
ends (specs/counted-figures.md, "The month bars end where the window
ends"). The server names the months each timeframe draws; a timeframe of
complete months carries neither the month in progress nor the forecast,
which both clients used to add after every window.
"""
import datetime as dt
import pathlib
import unittest

from oikonome.engine import reporting
from oikonome.web.schemas.reports import CashGraph

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 11, 17)
_ROOT = pathlib.Path(__file__).resolve().parents[2]
CLOSED = ("left over = income − spending; spending is dated by the day "
          "it was paid")
OPEN = ("left over = income − spending, this month so far included; "
        "spending is dated by the day it was paid")


def _month(conn, month: int, income: float, spend: float) -> None:
    add_txn(conn, dt.date(2026, month, 2), -income, "CLIENT DEPOSIT",
            account="chk", primary="INCOME")
    add_txn(conn, dt.date(2026, month, 10), spend, "Grocer",
            account="chk", primary="FOOD_AND_DRINK")


def _drawn(graph: dict, key: str) -> list[str]:
    """The month keys a client draws for `key`: every slot of the graph
    from the window's first month through its last."""
    w = graph["windows"][key]
    slots = [p[0] for p in graph["points"]]
    if graph["current"]:
        slots.append(graph["current"][0])
    return sorted({k for k in slots if w["first"] <= k <= w["last"]})


class AFinishedWindowStopsAtItsLastMonth(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        for month in (6, 7, 8, 9, 10):
            _month(self.conn, month, 2000.0, 1600.0)
        _month(self.conn, 11, 100.0, 1000.0)
        self.graph = reporting.compute_cashflow(self.conn,
                                                TODAY)["cash_graph"]

    def tearDown(self):
        self.conn.close()

    def test_the_graph_holds_this_month_and_a_forecast(self):
        self.assertEqual(self.graph["current"][0], "2026-11")
        self.assertGreaterEqual(self.graph["points"][-1][0], "2026-12")

    def test_complete_months_draw_no_month_in_progress_and_no_forecast(self):
        self.assertEqual(_drawn(self.graph, "1m"), ["2026-10"])
        self.assertEqual(_drawn(self.graph, "3m"),
                         ["2026-08", "2026-09", "2026-10"])
        for key in ("1m", "3m", "6m", "1y", "3y", "5y"):
            w = self.graph["windows"][key]
            self.assertEqual(w["last"], "2026-10", key)
            self.assertEqual(w["note"], CLOSED, key)

    def test_this_month_and_all_run_through_the_forecast(self):
        end = self.graph["points"][-1][0]
        self.assertEqual(self.graph["windows"]["cur"],
                         {"first": "2026-11", "last": end, "note": OPEN})
        self.assertEqual(self.graph["windows"]["all"],
                         {"first": "2026-06", "last": end, "note": OPEN})
        self.assertEqual(_drawn(self.graph, "cur")[0], "2026-11")
        self.assertEqual(_drawn(self.graph, "all")[0], "2026-06")
        self.assertEqual(_drawn(self.graph, "all")[-1], end)

    def test_the_report_carries_a_window_for_every_timeframe(self):
        sent = CashGraph.model_validate(self.graph).model_dump()
        self.assertEqual(set(sent["windows"]), set(reporting.FLOW_RANGES))
        self.assertEqual(sent["windows"], self.graph["windows"])


class AFirstMonthHouseholdSeesItsMonth(unittest.TestCase):
    """With no complete month yet every timeframe's window is the month
    so far, so every one of them draws it."""

    def test_every_timeframe_reaches_this_month(self):
        conn = make_db()
        try:
            write_config(conn)
            _month(conn, 11, 900.0, 400.0)
            graph = reporting.compute_cashflow(conn, TODAY)["cash_graph"]
        finally:
            conn.close()
        for key, w in graph["windows"].items():
            self.assertEqual(w["first"], "2026-11", key)
            self.assertGreaterEqual(w["last"], "2026-11", key)
            self.assertEqual(w["note"], OPEN, key)


class NeitherClientWorksOutTheWindow(unittest.TestCase):
    def test_the_clients_draw_the_servers_window(self):
        web = (_ROOT / "webapp/src/pages/CashFlow.tsx").read_text()
        mob = (_ROOT / "mobile/src/app/cashflow.tsx").read_text()
        self.assertIn("const w = graph.windows[range];", web)
        self.assertIn("const w = graphWindow(graph, range);", mob)
        for rel, src in (("web", web), ("phone", mob)):
            self.assertIn("key >= w.first && key <= w.last", src, rel)
            # the line under the chart is the server's
            self.assertIn("{series.note}", src, rel)
            self.assertNotIn("this month so far included", src, rel)
        # the web is served with its server: no month table of its own
        self.assertNotIn("cashflowCutoffMonth", web)
        self.assertNotIn("cashflowCutoffMonth", (
            _ROOT / "webapp/src/components/RangePicker.tsx").read_text())


if __name__ == "__main__":
    unittest.main()
