"""Two receipts are counted once only when they are one slip, and the
copy counted is one that can stand for the purchase.

The same image, byte for byte, is one slip. A second photo (same store,
day and total as read) is never one slip by itself: it is put to the
person, and a stranded first copy is only held back on its retired row
while it reads like a copy on a live charge of the same card. The counted
copy has lines (and, for a tag report, the tag) or the purchase would
count nothing. A search reads only the slips of the receipts it hit.
"""
import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import receipt_identity, receipt_match, receipts
from oikonome.sync import reanchor

from . import test_a_receipt_on_a_retired_hold_is_never_lost as base

DAY, PNG, _where = base.DAY, base.PNG, base._where


class SameSlipNeverHidesAPurchaseTests(unittest.TestCase):
    setUp = base.OneSlipCountsOnceTests.setUp
    _slip = base.OneSlipCountsOnceTests._slip
    _retire = base.OneSlipCountsOnceTests._retire
    _totals = base.OneSlipCountsOnceTests._totals

    def _wait(self):
        self.conn.execute("UPDATE receipts SET txn_id = NULL")

    def _reports(self, tag):
        later = DAY + dt.timedelta(days=2)
        months = {(DAY.year, DAY.month), (later.year, later.month)}
        reps = [receipts.report(self.conn, tag, y, m) for y, m in months]
        return (sum(r["count"] for r in reps),
                round(sum(r["total"] for r in reps), 2))

    def test_a_tag_only_the_waiting_copy_carries_reaches_the_report(self):
        self._wait()
        self.assertEqual(receipts.set_tag(self.conn, self.first, 0, "work"), 1)
        self._slip("posted-1", PNG)
        self.assertEqual(self._reports("work"), (1, 37.0))
        # the item totals still count the slip once
        self.assertEqual(self._totals(), (1, [37.0]))

    def test_a_live_copy_never_read_does_not_hide_the_lines(self):
        self._wait()
        receipts.add(self.conn, "posted-1", PNG, "image/png")
        self.assertEqual(self._totals(), (1, [37.0]))

    def test_a_second_purchase_waiting_beside_a_live_one_counts(self):
        # two of the same meal, same store, day and price: one paid in
        # cash, its receipt waiting; the other on the posted charge
        self._wait()
        self._slip("posted-1", PNG + b"the other slip")
        n, totals = self._totals()
        self.assertEqual((n, sum(totals)), (2, 74.0))

    def test_a_second_photo_on_the_posted_charge_keeps_the_hold_copy_once(self):
        # photo A stranded on the retired hold, photo B (other bytes, the
        # same reading) on the posted charge: one purchase, before and
        # after the nightly, and A stays where it is — listed, not counted
        self.assertEqual(receipts.set_tag(self.conn, self.first, 0, "work"), 1)
        self._retire()
        second = self._slip("posted-1", PNG + b"the other slip")
        self.assertEqual(receipts.set_tag(self.conn, second, 0, "work"), 1)
        reanchor.reanchor_stranded(self.conn)
        self.assertEqual(receipt_match.release_unplaced(self.conn), 0)
        self.assertEqual(_where(self.conn, self.first), ("hold-1", None))
        self.assertEqual(self._totals(), (1, [37.0]))
        self.assertEqual(self._reports("work"), (1, 37.0))

    def test_a_retired_receipt_no_live_reading_matches_is_released(self):
        self._retire()
        self._slip("posted-1", PNG + b"the other slip")
        self.conn.execute(
            "UPDATE receipts SET parsed = jsonb_set(parsed, '{total}', '52')"
            " WHERE txn_id = 'posted-1'")
        reanchor.reanchor_stranded(self.conn)
        self.assertEqual(receipt_match.release_unplaced(self.conn), 1)
        self.assertEqual(_where(self.conn, self.first), (None, None))

    def test_a_second_photo_read_with_stray_spaces_counts_once(self):
        self._retire()
        second = self._slip("posted-1", PNG + b"another photo")
        self.conn.execute(
            "UPDATE receipts SET parsed = jsonb_set(parsed, '{merchant}',"
            " to_jsonb(' ' || (parsed->>'merchant') || '  '))"
            " WHERE id = %s::uuid", (second,))
        self.assertEqual(self._totals(), (1, [37.0]))

    def test_a_search_reads_only_its_own_slips(self):
        self._wait()
        self._slip("posted-1", PNG)
        real = receipt_identity.slips
        with mock.patch.object(receipt_identity, "slips",
                               wraps=real) as spy:
            receipts.search_items(self.conn, q="nothing bought like this")
            self.assertEqual(spy.call_count, 0)
            receipts.search_items(self.conn, q="ramen")
            self.assertEqual(spy.call_count, 1)
            self.assertEqual(len(spy.call_args.args[1]), 2)


if __name__ == "__main__":
    unittest.main()
