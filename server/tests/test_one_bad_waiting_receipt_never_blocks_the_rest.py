"""One unreadable waiting receipt never blocks the waiting list or the
matcher, and a pile of old receipts never hides or starves a new one.

A total no purchase carries (1e307: its cents overflow) or a date off the
calendar (year 9999: a day either side overflows) used to fail the whole
waiting list with a 500 and stop every matcher pass for the household. The
restore door, which accepts a crafted ZIP, now drops such values at the
source; the matcher reads them as unknown; and each receipt is handled on
its own, so a bad one is logged and passed over while the rest carry on.

The list reports how many receipts really wait and pages through all of
them, and the matcher reads the newest first, so a fresh snap meets its
charge even behind hundreds of receipts nothing will ever match.
"""
import csv
import datetime as dt
import io
import unittest
import zipfile
from unittest import mock

from oikonome.engine import receipt_match, receipts
from oikonome.sync import export, restore

from .util import add_txn, make_db, write_config

PNG = b"\x89PNG-bad-row"
DAY = dt.date.today() - dt.timedelta(days=1)


def _waiting(conn, merchant="Maple Leaf Bistro", total=42.17, date=DAY,
             created_at=None):
    rid = receipts.add(conn, None, PNG, "image/png")
    conn.execute(
        "UPDATE receipts SET status = 'parsed', parsed = %s::jsonb,"
        " created_at = COALESCE(%s, created_at) WHERE id = %s",
        ('{"merchant": "%s", "total": %s, "date": "%s"}'
         % (merchant, total, date.isoformat()), created_at, rid))
    return rid


def _attached(conn, rid):
    return conn.execute("SELECT txn_id FROM receipts WHERE id = %s",
                        (rid,)).fetchone()["txn_id"]


class AbsurdValuesTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_facts_read_absurd_totals_and_dates_as_unknown(self):
        f = receipt_match.facts
        self.assertIsNone(f({"parsed": {"total": 1e307}})["total"])
        self.assertIsNone(f({"parsed": {"total": "1e999"}})["total"])
        self.assertIsNone(f({"created_at": dt.datetime(9999, 12, 31)})["date"])
        self.assertIsNone(f({"created_at": dt.datetime(1, 1, 1)})["date"])
        self.assertIsNone(f({"parsed": {"date": "9999-12-31"}})["date"])

    def test_a_crafted_row_leaves_the_list_and_the_matcher_working(self):
        good = _waiting(self.conn)
        huge = _waiting(self.conn, total="1e307")
        far = _waiting(self.conn, date=dt.date(9999, 12, 30),
                       created_at=dt.datetime(9999, 12, 31,
                                              tzinfo=dt.timezone.utc))
        early = _waiting(self.conn, date=dt.date(1, 1, 2),
                         created_at=dt.datetime(1, 1, 1,
                                                tzinfo=dt.timezone.utc))
        add_txn(self.conn, DAY + dt.timedelta(days=1), 42.17,
                "MAPLE LEAF BISTRO", txn_id="p-1")
        listed = receipt_match.waiting(self.conn)
        self.assertEqual(listed["count"], 4)
        self.assertEqual({w["id"] for w in listed["waiting"]},
                         {good, huge, far, early})
        receipt_match.match_waiting(self.conn)
        self.assertEqual(_attached(self.conn, good), "p-1")
        for rid in (huge, far, early):
            self.assertIsNone(_attached(self.conn, rid))
        self.assertEqual(receipt_match.waiting(self.conn)["count"], 3)

    def test_a_row_that_raises_is_passed_over_not_fatal(self):
        first = _waiting(self.conn, merchant="Harbor Coffee", total=6.25)
        second = _waiting(self.conn)
        add_txn(self.conn, DAY + dt.timedelta(days=1), 6.25,
                "HARBOR COFFEE", txn_id="c-1")
        add_txn(self.conn, DAY + dt.timedelta(days=1), 42.17,
                "MAPLE LEAF BISTRO", txn_id="p-1")
        real = receipt_match.decide
        calls = []

        def flaky(cands, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise OverflowError("an unreadable row")
            return real(cands, **kw)
        with mock.patch.object(receipt_match, "decide", flaky):
            receipt_match.match_waiting(self.conn)
        # newest first: the first row tried is `second`, and it raised
        self.assertIsNone(_attached(self.conn, second))
        self.assertEqual(_attached(self.conn, first), "c-1")
        with mock.patch.object(receipt_match, "decide",
                               side_effect=OverflowError("unreadable")):
            listed = receipt_match.waiting(self.conn)
        self.assertEqual([w["id"] for w in listed["waiting"]], [second])


class RestoreDropsAbsurdValuesTests(unittest.TestCase):
    def setUp(self):
        self.src = make_db()
        write_config(self.src)

    def tearDown(self):
        self.src.close()

    @staticmethod
    def _tamper(data: bytes, **cells) -> bytes:
        zin = zipfile.ZipFile(io.BytesIO(data))
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                body = zin.read(info)
                if info.filename == "receipts.csv":
                    rows = list(csv.DictReader(io.StringIO(body.decode())))
                    for r in rows:
                        r.update(cells)
                    buf = io.StringIO()
                    w = csv.DictWriter(buf, fieldnames=list(rows[0]))
                    w.writeheader()
                    w.writerows(rows)
                    body = buf.getvalue().encode()
                zout.writestr(info, body)
        return out.getvalue()

    def test_a_crafted_total_and_dates_do_not_survive_a_restore(self):
        rid = _waiting(self.src)
        data = self._tamper(
            export.build_zip(self.src),
            parsed='{"merchant": "Maple Leaf Bistro", "total": 1e307,'
                   ' "date": "2026-01-01"}',
            created_at="9999-12-31 00:00:00",
            parsed_at="0001-01-01 00:00:00")
        dest = make_db()
        try:
            self.assertEqual(restore.restore_zip(dest, data)["receipts"], 1)
            r = dest.execute("SELECT parsed, created_at, parsed_at"
                             " FROM receipts WHERE id = %s",
                             (rid,)).fetchone()
            self.assertIsNone(r["parsed"]["total"])
            self.assertEqual(r["parsed"]["merchant"], "Maple Leaf Bistro")
            self.assertLessEqual(r["created_at"].year,
                                 dt.date.today().year + 1)
            self.assertIsNone(r["parsed_at"])
            self.assertEqual(receipt_match.waiting(dest)["count"], 1)
            receipt_match.match_waiting(dest)
        finally:
            dest.close()


class NothingHidesANewSnapTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_the_matcher_reads_the_newest_receipts_first(self):
        # receipts nothing will match, snapped long ago, fill the pass
        for i in range(3):
            _waiting(self.conn, merchant="Quill and Ink", total=90 + i,
                     date=DAY - dt.timedelta(days=200),
                     created_at=dt.datetime.now(dt.timezone.utc)
                     - dt.timedelta(days=200 - i))
        fresh = _waiting(self.conn)
        add_txn(self.conn, DAY + dt.timedelta(days=1), 42.17,
                "MAPLE LEAF BISTRO", txn_id="p-1")
        with mock.patch.object(receipt_match, "SWEEP_LIMIT", 3):
            receipt_match.match_waiting(self.conn)
        self.assertEqual(_attached(self.conn, fresh), "p-1")

    def test_the_list_counts_every_waiting_receipt_and_pages_through_them(self):
        ids = [_waiting(self.conn, total=10 + i,
                        created_at=dt.datetime.now(dt.timezone.utc)
                        - dt.timedelta(minutes=10 - i)) for i in range(5)]
        first = receipt_match.waiting(self.conn, limit=2)
        self.assertEqual(first["count"], 5)
        self.assertEqual([w["id"] for w in first["waiting"]],
                         ids[::-1][:2])
        seen = []
        for off in range(0, 5, 2):
            seen += [w["id"] for w in receipt_match.waiting(
                self.conn, limit=2, offset=off)["waiting"]]
        self.assertEqual(seen, ids[::-1])

    def test_one_query_reads_the_same_candidates_as_one_per_receipt(self):
        a = _waiting(self.conn, total=40.00)
        b = _waiting(self.conn, merchant="Harbor Coffee", total=6.25)
        add_txn(self.conn, DAY, 48.00, "MAPLE LEAF BISTRO", txn_id="n-1")
        add_txn(self.conn, DAY, 40.00, "QUILL AND INK", txn_id="x-1")
        add_txn(self.conn, DAY, 6.25, "HARBOR COFFEE", txn_id="c-1")
        self.conn.execute("UPDATE receipts SET unmatched_txn_ids = '{x-1}'"
                          " WHERE id = %s", (a,))
        rows = self.conn.execute(
            "SELECT id, kind, status, parsed, created_at, unmatched_txn_ids"
            " FROM receipts WHERE id = ANY(%s::uuid[]) ORDER BY created_at",
            ([a, b],)).fetchall()
        together = receipt_match.candidates_for(self.conn, rows)
        alone = [receipt_match.candidates(self.conn, r) for r in rows]
        self.assertEqual(together, alone)
        self.assertEqual([c["txn_id"] for c in together[0]], ["n-1"])
        self.assertEqual([c["txn_id"] for c in together[1]], ["c-1"])


if __name__ == "__main__":
    unittest.main()
