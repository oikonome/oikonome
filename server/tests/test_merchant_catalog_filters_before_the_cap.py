"""The merchant catalog filters and sorts before it caps.

The catalog is capped, and a household's long tail runs past the cap. A
filter ("one-off", "unnamed", "seen this month") or a sort ("biggest
spend", "recently seen") cut from the capped page on the client would lose
every match beyond it and call the result complete. So both run in the
query, before the limit, and `offset` pages through the rest.
"""

import datetime as dt
import re
import unittest

from oikonome.engine import merchant_dedup

from .util import TODAY, add_txn, make_db, write_config

LIMIT = 4


def _looks_unnamed(name: str) -> bool:
    # the merchant screens' own test, restated: a long number or a
    # terminal's * or #; a shorter number in a line with no lower case;
    # several words all in capitals. One capitalised word is a brand.
    return bool(re.search(r"\d{5,}|[*#]", name)) or (
        bool(re.search(r"\d{3,}", name)) and not re.search(r"[a-z]", name)
    ) or (bool(re.search(r"[A-Z]{4,}", name)) and " " in name
          and name == name.upper())


def _name(r):
    # a → z as people read it: case does not order names
    return (r["display"].lower(), r["display"])


class CatalogBeforeTheCapTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        first = TODAY.replace(day=1)
        named = ["Amber Bakery", "Birch Books", "Cedar Cafe", "Dune Deli",
                 "Ember Eatery", "Fern Florist", "Grove Grocer", "Harbor Hats"]
        unnamed = ["POS 48213 KIOSK", "SQ *LANTERNWAY", "TERMINAL ORCHARD",
                   "CHK 9917 PAYEE"]
        # busy, large, long ago at the front; one-off, small, recent at the
        # back — so every filter and sort reaches past the first page
        for i, name in enumerate(named + unnamed):
            n = 1 if i % 3 == 0 else 2 + i % 4
            for k in range(n):
                if i < 6:
                    day = TODAY - dt.timedelta(days=40 + i * 9 + k)
                else:
                    day = first + dt.timedelta(days=min(k, TODAY.day - 1))
                add_txn(self.conn, day, 10.0 * (i + 1), name, merchant=name,
                        account="chk")
        self.all = merchant_dedup.listing(self.conn, limit=500)
        self.assertGreater(len(self.all), LIMIT * 2)

    def _pages(self, **kw):
        out, off = [], 0
        while True:
            page = merchant_dedup.listing(self.conn, limit=LIMIT,
                                          offset=off, today=TODAY, **kw)
            out.extend(r["display"] for r in page)
            if len(page) < LIMIT:
                return out
            off += LIMIT

    def _expect(self, rows, key):
        return [r["display"] for r in sorted(rows, key=key)]

    def test_each_sort_orders_the_whole_catalog_before_the_limit(self):
        cases = {
            "count": lambda r: (-r["rows"], _name(r)),
            "alpha": _name,
            "spend": lambda r: (-float(r["total"]), _name(r)),
            "recent": lambda r: (-(r["last"].toordinal()), _name(r)),
        }
        for sort, key in cases.items():
            want = self._expect(self.all, key)
            first = merchant_dedup.listing(self.conn, limit=LIMIT, sort=sort)
            self.assertEqual([r["display"] for r in first], want[:LIMIT], sort)
            self.assertEqual(self._pages(sort=sort), want, sort)

    def test_each_filter_reaches_past_the_limit(self):
        m0 = TODAY.replace(day=1)
        cases = {
            "oneoff": lambda r: r["rows"] == 1,
            "unnamed": lambda r: _looks_unnamed(r["display"]),
            "month": lambda r: r["last"] >= m0,
        }
        for flt, keep in cases.items():
            want = self._expect([r for r in self.all if keep(r)],
                                lambda r: (-r["rows"], _name(r)))
            self.assertTrue(want, flt)
            got = self._pages(filter=flt)
            self.assertEqual(got, want, flt)
            # and a filter with a sort: the cut is the sorted filtered set
            spend = self._expect([r for r in self.all if keep(r)],
                                 lambda r: (-float(r["total"]), _name(r)))
            self.assertEqual(self._pages(filter=flt, sort="spend"), spend,
                             flt)

    def test_the_old_order_parameter_still_works(self):
        self.assertEqual(
            [r["display"] for r in merchant_dedup.listing(
                self.conn, limit=LIMIT, order="alpha")],
            [r["display"] for r in sorted(self.all, key=_name)][:LIMIT])

    def test_unknown_values_are_refused(self):
        with self.assertRaises(ValueError):
            merchant_dedup.listing(self.conn, sort="loudest")
        with self.assertRaises(ValueError):
            merchant_dedup.listing(self.conn, filter="everything")

    def test_a_negative_or_huge_offset_is_bounded(self):
        self.assertEqual(
            [r["display"] for r in merchant_dedup.listing(
                self.conn, limit=LIMIT, offset=-5)],
            [r["display"] for r in self.all[:LIMIT]])
        self.assertEqual(merchant_dedup.listing(
            self.conn, limit=LIMIT, offset=10 ** 30), [])



class TheListReadsAsPeopleRead(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        for name in ("AZ Bakery", "Abbey Diner", "ZEFO", "KRAV",
                     "412 Kitchenette", "TERMINAL ORCHARD", "POS 48213 KIOSK",
                     "Shellfuel 57442653", "SQ *LANTERNWAY"):
            add_txn(self.conn, TODAY - dt.timedelta(days=3), 10.0, name,
                    merchant=name, account="chk")
        # an all-capitals name the person gave a merchant themselves
        mid = self.conn.execute(
            "INSERT INTO merchants (name, name_source) VALUES "
            "('HOME RENT POOL', 'manual') RETURNING id").fetchone()["id"]
        tid = add_txn(self.conn, TODAY - dt.timedelta(days=3), 900.0,
                      "ACH DEBIT 0099", merchant="ACH DEBIT 0099",
                      account="chk")
        self.conn.execute("UPDATE transactions SET merchant_id = %s "
                          "WHERE id = %s", (mid, tid))

    def _names(self, **kw):
        return [r["display"] for r in merchant_dedup.listing(
            self.conn, limit=500, today=TODAY, **kw)]

    def test_a_to_z_ignores_capitals(self):
        names = self._names(sort="alpha")
        self.assertLess(names.index("Abbey Diner"), names.index("AZ Bakery"))

    def test_unnamed_lists_bank_strings_and_no_real_name(self):
        self.assertEqual(sorted(self._names(filter="unnamed")),
                         sorted(["TERMINAL ORCHARD", "POS 48213 KIOSK",
                                 "Shellfuel 57442653", "SQ *LANTERNWAY"]))


if __name__ == "__main__":
    unittest.main()
