"""A pool's line names its period and says only the spill that period
counted (specs/today-figures.md, "A pool named on a month surface prints
only what that month's own status holds", "The Year's pool line").

* A yearly pool cut mid-year: $1,000 cut to $300 in May, $150 a month at
  its merchant, read on July 15. Every month surface prints main's pair
  and July's own $150 spill; the Year prints its months — $1,050 spent,
  $450 counted in spending — and the resize as its own line.
* The same pool on a closed June card names June in both phrases.
* A pool deleted and made again is its own line on the Year.
* A yearly pool exhausted earlier in the year and pinned to Today, with
  nothing drawn this month: the monthly email prints its own bar from its
  line (row or no row), the yearly email too, and the Bills bar names no
  pool.
Every figure and name is invented.
"""

from __future__ import annotations

import datetime as dt
import unittest

from oikonome.engine import bills as bills_engine
from oikonome.engine import budget
from oikonome.web import api_bills, lens_email, lenses, report, todayview

from .util import (TODAY, add_bill, add_txn, freeze_month, make_db,
                   write_config)

JULY = ("used $900 of $300 this year · $150 past the pool this month, "
        "counted in spending")
YEAR = ("$1,050 spent in 2026 · pool $300 · $450 past the pool in 2026, "
        "counted in spending")


def _row(rows, label):
    return next((r for r in rows or () if r["label"] == label), None)


class APoolCutMidYear(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        y = TODAY.year
        for m in range(1, TODAY.month + 1):
            if m in (1, 5):
                add_bill(self.conn, "Gifts", 1000.0 if m == 1 else 300.0,
                         bill_type="envelope", frequency=None, interval=12,
                         merchant="gift nook", show_today=True)
            add_txn(self.conn, dt.date(y, m, 10), 150.0, "GIFT NOOK")
            if m < TODAY.month:
                freeze_month(self.conn, y, m)

    def test_every_month_surface_says_this_months_spill(self):
        mo = lenses.month_summary(self.conn, TODAY.year, TODAY.month,
                                  today=TODAY)
        (e,) = mo["bills"]["envelopes"]
        self.assertEqual((e["pair_text"], e["pair_past"]), (JULY, 150.0))
        row = _row(mo["plan_rows"], "Envelopes")
        self.assertEqual(row["status"]["text"],
                         "$150 past the pool this month, counted in spending")
        self.assertTrue(row["breakdown"]["table"]["rows"][0]["text"]
                        .startswith(JULY))
        st = report.gather(self.conn, TODAY)
        (card,) = [c for c in todayview._bill_cards(st)
                   if c["kind"] == "envelope"]
        self.assertEqual((card["sub"], card["status"]), (
            "used $900 of $300 this year",
            "$150 past the pool this month, counted in spending"))
        stats = api_bills._month_bill_stats(
            self.conn, budget.load_config(self.conn), TODAY)
        self.assertEqual(stats["Gifts"]["pool_text"], JULY)

    def test_a_closed_june_card_names_june(self):
        mo = lenses.month_summary(self.conn, TODAY.year, 6, today=TODAY)
        (e,) = mo["bills"]["envelopes"]
        self.assertEqual(e["pair_text"],
                         "used $750 of $300 by the end of June · $150 past "
                         "the pool in June, counted in spending")
        self.assertEqual(_row(mo["plan_rows"], "Envelopes")["status"]["text"],
                         "$150 past the pool in June, counted in spending")

    def test_the_year_reads_its_months_and_says_the_resize(self):
        yr = lenses.year_summary(self.conn, TODAY.year, today=TODAY)
        row = _row(yr["plan_rows"], "Envelopes")
        (it,) = row["breakdown"]["items"]
        self.assertEqual(it["pair_text"], YEAR)
        self.assertEqual(row["status"]["text"],
                         "$450 past the pool in 2026, counted in spending")
        self.assertIn("Gifts: pool resized during the year",
                      [o["label"] for o in row["breakdown"]["table"]
                       ["outside"]])


class APoolMadeAgain(unittest.TestCase):
    def test_a_recreated_pool_is_a_line_of_its_own(self):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        y = TODAY.year
        for m in range(1, TODAY.month + 1):
            if m == 1:
                add_bill(conn, "Gifts", 400.0, bill_type="envelope",
                         frequency=None, interval=12, merchant="gift nook")
            if m == 4:
                bills_engine.delete_bill(conn, "Gifts")
            if m == 5:
                # made again on May 1, as the save door dates a new pool
                add_bill(conn, "Gifts", 500.0, bill_type="envelope",
                         frequency=None, interval=12, merchant="gift nook",
                         created_on=dt.date(y, 5, 1))
            if m != 4:
                add_txn(conn, dt.date(y, m, 10), 130.0, "GIFT NOOK")
            if m < TODAY.month:
                freeze_month(conn, y, m)
        yr = lenses.year_summary(conn, y, today=TODAY)
        items = _row(yr["plan_rows"], "Envelopes")["breakdown"]["items"]
        self.assertEqual(sorted(it["pair_text"] for it in items), [
            "$390 spent in 2026 · pool $400",
            "$390 spent in 2026 · pool $500"])
        # May to July's charges are the new pool's alone, counted once
        self.assertEqual(sorted(it["drawn"] for it in items), [390, 390])
        self.assertNotIn("was", " ".join(it["pair_text"] for it in items))


class AnExhaustedPinnedPool(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        add_bill(self.conn, "Rent Sample", 900.0,
                 next_due=dt.date(TODAY.year, TODAY.month, 1),
                 last_seen=dt.date(TODAY.year, TODAY.month - 1, 1))
        add_bill(self.conn, "Gifts", 1000.0, bill_type="envelope",
                 frequency=None, interval=12, merchant="gift nook",
                 show_today=True)
        y = TODAY.year
        add_txn(self.conn, dt.date(y, 2, 12), 900.0, "GIFT NOOK")
        add_txn(self.conn, dt.date(y, 3, 12), 748.0, "GIFT NOOK")
        for m in range(1, TODAY.month):
            add_txn(self.conn, dt.date(y, m, 1), 900.0, "RENT SAMPLE")
            freeze_month(self.conn, y, m)

    def test_the_monthly_email_prints_the_pools_own_line(self):
        y, m = TODAY.year, TODAY.month
        mo = lenses.month_summary(self.conn, y, m, today=TODAY)
        # nothing drawn and nothing to say this month: no Envelopes row
        self.assertIsNone(_row(mo["plan_rows"], "Envelopes"))
        (e,) = mo["bills"]["envelopes"]
        self.assertEqual(e["pair_text"], "used $1,648 of $1,000 this year")
        yr = lenses.year_summary(self.conn, y, today=TODAY)
        rows = {r["label"]: r for r in
                lens_email.month_mail_context(mo, yr, y, m)["rows"]}
        # the card's own figures, not an older reply's legacy pair
        self.assertEqual((rows["Gifts"]["actual_text"],
                          rows["Gifts"]["plan_text"],
                          rows["Gifts"]["note"]),
                         ("$1,648", "$1,000", "yearly pool · used in full"))
        self.assertNotIn("Gifts", rows["Bills"]["note"])
        self.assertNotIn("drawn on its own", rows["Bills"]["note"])

    def test_the_yearly_email_prints_the_pools_year_line(self):
        yr = lenses.year_summary(self.conn, TODAY.year, today=TODAY,
                                 with_detail=True)
        rows = {r["label"]: r for r in
                lens_email.year_mail_context(yr, TODAY.year)["rows"]}
        self.assertEqual(rows["Gifts"]["note"],
                         "yearly pool · $648 past the pool in 2026, "
                         "counted in spending")
        self.assertNotIn("drawn on its own", rows["Bills"]["note"])


if __name__ == "__main__":
    unittest.main()
