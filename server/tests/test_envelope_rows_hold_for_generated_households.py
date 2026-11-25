"""The Envelopes row holds its rules on every surface for generated
households (tests/envelope_households.py; specs/today-figures.md,
"Envelopes are their own row"):

* never red, never "over" unless a pool's own period spent past it: on
  Today, each Month lens month (closed, running, future) and the Year,
  the row's left figure is never past its right one, and its tone is
  neg exactly when it names a spill;
* the row is its list: its figures are its items' printed figures
  summed;
* the plan splits to the cent and as printed: the Month card's Bills
  plan and the pools' set-aside add up to its plan in cents, and their
  whole dollars to its whole dollars;
* the Year reads the pool: each yearly pool's drawn + left + outside is
  the pool, its drawn the months' draws summed once;
* nothing in two rows: in cents the Bills row's posted and the pools'
  draws are the month's fixed spend, and the map's rows add up to the
  month's out;
* closed months are stable: resizing, renaming or deleting a live pool
  moves no closed month's Bills or Envelopes row.

Each seed runs twice: with pools whose day is not known (stored before
the day was kept) and with pools dated on the day they were set up.
"""

from __future__ import annotations

import unittest

from oikonome.engine import bills as bills_engine
from oikonome.engine.reporting import whole_dollars
from oikonome.web import lenses, report, todayview

from . import props
from .envelope_households import EnvelopeHousehold
from .util import TODAY, add_bill


def _row(rows, label):
    return next((r for r in rows or () if r["label"] == label), None)


class EnvelopeRowsHoldTests(unittest.TestCase):

    def _envelope_row(self, row, where):
        if row is None:
            return
        bd = row["breakdown"]
        with self.subTest(where=where):
            self.assertLessEqual(row["actual"], row["budget"] + 0.5)
            st = row["status"] or {}
            self.assertEqual(st.get("tone") == "neg", bd["overflow"] > 0)
            self.assertEqual(row["actual"],
                             sum(i["drawn"] for i in bd["items"]))
            self.assertEqual(row["budget"],
                             sum(i["cap"] for i in bd["items"]))
            for word in ("under plan", "over plan", "ahead"):
                self.assertNotIn(word, st.get("text") or "")
            if bd["kind"] == "year":
                for it in bd["items"]:
                    if it["period_months"] > 1:
                        self.assertEqual(
                            it["drawn"] + it["left"] + it["outside"],
                            it["pool"])

    def _closed(self, conn):
        out = {}
        for m in range(1, TODAY.month):
            mo = lenses.month_summary(conn, TODAY.year, m, today=TODAY)
            out[m] = (_row(mo["plan_rows"], "Bills"),
                      _row(mo["plan_rows"], "Envelopes"))
        return out

    def test_generated_households(self):
        for seed, dated in ((s, d) for s in props.seeds(default=12)
                            for d in (False, True)):
            # dated: every pool carries the day it was set up, so a pool
            # set up mid-year counts from its month and is in no plan
            # before its day
            h = EnvelopeHousehold(seed, dated=dated)
            conn = h.conn
            try:
                st = report.gather(conn, TODAY)
                rows, _t = todayview.today_map(st)
                rows = todayview.sent_plan_rows(rows)
                env = _row(rows, "Envelopes")
                self._envelope_row(env, ("today", seed))
                fx = st["buckets"]["fixed"]
                self.assertAlmostEqual(fx["occ_actual"] + sum(
                    e["used"] for e in fx["envelopes"]), fx["actual"],
                    places=6)
                bills = _row(rows, "Bills")
                out = sum(x["actual"] for x in rows
                          if x["judged"] and x.get("actual_label") != "—")
                bd = st["bills_breakdown"]
                # a month with no dated bill sends no Bills row
                from oikonome.web import bills_breakdown
                self.assertEqual(bills is None,
                                 bills_breakdown.nothing_dated(bd), seed)
                out += bd["posted"] - bd["refunds"]
                out += env["actual"] if env else 0.0
                # as printed: Bills under a dollar off its cents, each
                # pool's draw half a dollar
                pools = len(env["breakdown"]["items"]) if env else 0
                self.assertLess(abs(out - st["total_actual"]),
                                1 + 0.5 * pools + 1e-6, seed)
                if env is not None:
                    self.assertEqual(env["breakdown"]["reserve"],
                                     st["month_end_parts"]["envelopes"])
                before = self._closed(conn)
                for m, (b, e) in before.items():
                    self._envelope_row(e, ("month", seed, m))
                    mo = lenses.month_summary(conn, TODAY.year, m,
                                              today=TODAY)
                    card = mo["bills"]
                    if card.get("set_aside") is not None:
                        # the two parts add up to the plan in cents AND
                        # as printed, the bills part the Bills row's plan
                        bp, es = (card["bills_planned"],
                                  card["envelopes_set_aside"])
                        self.assertEqual(
                            int(round(bp * 100)) + int(round(es * 100)),
                            int(round(card["planned"] * 100)),
                            (seed, m, card["planned"], bp, es))
                        self.assertEqual(
                            whole_dollars(bp) + whole_dollars(es),
                            whole_dollars(card["planned"]), (seed, m))
                        if b is not None:
                            self.assertEqual(whole_dollars(bp),
                                             b["breakdown"]["plan"])
                yr = lenses.year_summary(conn, TODAY.year, today=TODAY)
                # the monthly email's Envelopes bar is the row, its note
                # the row's words; the daily email's plain text prints it
                from oikonome.web import lens_email
                for m, (_b, e) in before.items():
                    mo = lenses.month_summary(conn, TODAY.year, m,
                                              today=TODAY)
                    bars = {x["label"]: x for x in lens_email
                            .month_mail_context(mo, yr, TODAY.year, m)["rows"]}
                    if e is None:
                        self.assertNotIn("Envelopes", bars)
                        continue
                    bar = bars["Envelopes"]
                    self.assertEqual((bar["actual"], bar["plan"]),
                                     (e["actual"], e["budget"]))
                    if e["status"]:
                        self.assertEqual(bar["note"], e["status"]["text"])
                    self.assertEqual(bar["bar"] == "#e0695d",
                                     e["breakdown"]["overflow"] > 0)
                if env is not None:
                    _s, plain, _h = report.build(st)
                    self.assertIn(f"Envelopes ${int(env['actual']):,} / "
                                  f"${int(env['budget']):,}", plain)
                self._envelope_row(_row(yr["plan_rows"], "Envelopes"),
                                   ("year", seed))
                nxt = lenses.month_summary(conn, TODAY.year,
                                           TODAY.month + 1, today=TODAY)
                self._envelope_row(_row(nxt["plan_rows"], "Envelopes"),
                                   ("future", seed))
                # edit the live pools: closed months do not move
                for p in h.pools:
                    live = conn.execute(
                        "SELECT 1 FROM bills WHERE payee=%s",
                        (p["payee"],)).fetchone()
                    if live is None:
                        continue
                    pick = h.rng.random()
                    if pick < 0.4:
                        add_bill(conn, p["payee"], p["amount"] * 1.7,
                                 bill_type="envelope", frequency=None,
                                 interval=p["months"],
                                 merchant=p["payee"].lower())
                    elif pick < 0.7:
                        bills_engine.rename_bill(conn, p["payee"],
                                                 p["payee"] + " Two")
                    else:
                        bills_engine.delete_bill(conn, p["payee"])
                conn.commit()
                self.assertEqual(self._closed(conn), before, seed)
            finally:
                h.close()


if __name__ == "__main__":
    unittest.main()
