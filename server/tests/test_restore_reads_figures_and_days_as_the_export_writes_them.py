"""A restored figure or day is read as the export writes it, and a spelling
no export of this app writes is not quietly turned into some other value.

Python's own readers are more lenient than the export: float() takes
"1_000" and fullwidth digits, and a day read from the first ten
characters of its cell took "2026-03-02T25:00" (an hour no clock has) as
that day, and "20260302" (the compact ISO spelling) as it too. A person's
export never holds those, so each is unreadable: the row is refused as
malformed where the figure or day is required, the value is left empty
where it is optional. What the export itself can write still reads: an
exponent ("1e-05", "1E+3"), a day before 1900 within the years the app
keeps, a moment with its zone.
"""

import datetime as dt
import unittest

from oikonome.sync import restore

from .test_restore_v2 import _zip
from .util import make_db, write_config


class AFigure(unittest.TestCase):
    def test_a_spelling_no_export_writes_is_not_a_number(self):
        for v in ("1_000", "１２", "1 000", "0x10", "١٢"):
            with self.subTest(v=v):
                with self.assertRaises(restore._NotANumber):
                    restore._f(v)

    def test_what_the_export_can_write_still_reads(self):
        for v, want in (("12", 12.0), ("-0.5", -0.5), ("1e-05", 1e-05),
                        ("1E+3", 1000.0), (".25", 0.25), ("3.", 3.0),
                        ("1e-400", 0.0)):
            with self.subTest(v=v):
                self.assertEqual(restore._f(v), want)

    def test_a_non_finite_number_still_refuses_the_archive(self):
        for v in ("nan", "-Infinity", "INF", "1e999"):
            with self.subTest(v=v):
                with self.assertRaises(restore._ArchiveRefused):
                    restore._f(v)


class ADay(unittest.TestCase):
    def test_a_spelling_no_export_writes_is_unreadable(self):
        for v in ("20260302", "2026-03-02T25:00", "2026-03-02 10:61:00",
                  "2026-0３-02", "2026-03-02garbage"):
            with self.subTest(v=v):
                self.assertIsNone(restore._opt_date(v))
                self.assertIsNone(restore._ts(v))
                with self.assertRaises(restore._Unreadable):
                    restore._req_date(v)

    def test_what_the_export_can_write_still_reads(self):
        self.assertEqual(restore._opt_date("2026-03-02"), dt.date(2026, 3, 2))
        self.assertEqual(restore._opt_date("1899-12-31"),
                         dt.date(1899, 12, 31))
        self.assertEqual(restore._opt_date("2026-03-02 10:00:00+00:00"),
                         dt.date(2026, 3, 2))
        self.assertEqual(restore._ts("2026-03-02"), dt.datetime(2026, 3, 2))
        self.assertEqual(restore._ts("2026-03-02 10:00:00"),
                         dt.datetime(2026, 3, 2, 10))
        self.assertEqual(
            restore._ts("2026-03-02T10:00:00+00:00"),
            dt.datetime(2026, 3, 2, 10, tzinfo=dt.timezone.utc))


class InARestore(unittest.TestCase):
    def test_such_a_charge_is_refused_and_the_rest_lands(self):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        cols = ["id", "account_id", "date", "amount", "name"]
        rows = [{"id": "t-ok", "account_id": "chk", "date": "2026-03-02",
                 "amount": "-10", "name": "INVENTED SHOP"},
                {"id": "t-under", "account_id": "chk", "date": "2026-03-02",
                 "amount": "-1_000", "name": "INVENTED SHOP"},
                {"id": "t-compact", "account_id": "chk", "date": "20260302",
                 "amount": "-10", "name": "INVENTED SHOP"}]
        _r, out = restore.restore_outcomes(conn, _zip({"transactions": (
            cols, rows)}))
        self.assertEqual(sorted(r["id"] for r in conn.execute(
            "SELECT id FROM transactions WHERE id LIKE 't-%'").fetchall()),
            ["t-ok"])


if __name__ == "__main__":
    unittest.main()
