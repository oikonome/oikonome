"""Pools and unpaid bills are listed in one order, whatever order their
rows were written in (specs/today-figures.md, "Lists print in one
order"): the daily email's "Pinned:" line, the monthly email's
"without …", a closed month's "not paid:" notes and the pool list, by
the name shown, then the stable id. Two builds of the same household,
with the bills and pools written in opposite orders, print the same
text. Every figure and name is invented.
"""

from __future__ import annotations

import datetime as dt
import unittest

from oikonome.web import lens_email, lenses, report

from .util import (TODAY, add_bill, add_txn, freeze_month, make_db,
                   write_config)

POOLS = (("Zinnia Garden", 600.0, True), ("Apple Orchard", 480.0, True),
         ("Maple Books", 360.0, False))
BILLS = (("Water Works", 60.0), ("Bright Power", 90.0),
         ("Net Provider", 70.0))


def _build(order):
    conn = make_db()
    write_config(conn)
    y = TODAY.year
    for name, pool, pinned in order(POOLS):
        add_bill(conn, name, pool, bill_type="envelope", frequency=None,
                 interval=12, merchant=name.lower(), show_today=pinned)
    for name, amount in order(BILLS):
        # due on the 5th, never paid in June: each is "not paid"
        add_bill(conn, name, amount, next_due=dt.date(y, 6, 5),
                 last_seen=dt.date(y, 5, 5))
    for m in range(1, TODAY.month + 1):
        for name, pool, _p in order(POOLS):
            add_txn(conn, dt.date(y, m, 12), pool / 4, name.upper())
        if m < TODAY.month:
            freeze_month(conn, y, m)
    return conn


def _printed(conn) -> dict:
    y = TODAY.year
    st = report.gather(conn, TODAY)
    _s, plain, _h = report.build(st)
    june = lenses.month_summary(conn, y, 6, today=TODAY)
    yr = lenses.year_summary(conn, y, today=TODAY)
    mail = lens_email.month_mail_context(june, yr, y, 6)
    env = next((r for r in june["plan_rows"] if r["label"] == "Envelopes"),
               None)
    return {"pinned": [ln for ln in plain.splitlines()
                       if ln.startswith("Pinned:")],
            "notes": june["bills"]["notes"],
            "mail": [(r["label"], r["note"]) for r in mail["rows"]],
            "pools": [r["label"] for r in
                      (env or {}).get("breakdown", {}).get("table", {})
                      .get("rows", [])]}


class ListsPrintInOneOrder(unittest.TestCase):
    def test_two_builds_in_opposite_orders_print_the_same(self):
        a = _build(lambda xs: list(xs))
        b = _build(lambda xs: list(reversed(xs)))
        try:
            pa, pb = _printed(a), _printed(b)
        finally:
            a.close()
            b.close()
        self.assertEqual(pa, pb)
        self.assertTrue(pa["pinned"])
        self.assertTrue(pa["pinned"][0].index("Apple Orchard")
                        < pa["pinned"][0].index("Zinnia Garden"))
        self.assertEqual([n.split(" $")[0] for n in pa["notes"]],
                         ["not paid: Bright Power", "not paid: Net Provider",
                          "not paid: Water Works"])
        self.assertEqual(pa["pools"], sorted(pa["pools"]))
        without = [n for _l, n in pa["mail"] if "without" in (n or "")]
        self.assertTrue(any("without Apple Orchard, Zinnia Garden" in n
                            for n in without), pa["mail"])


if __name__ == "__main__":
    unittest.main()
