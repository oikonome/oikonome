"""Two waiting receipts that could both take one charge: the one from the
charge's own day takes it, whichever was snapped first.

The matcher pass walks the waiting receipts newest first, and each took
the one charge it could see. With the same coffee bought two mornings
running and only the first morning's charge posted, the second morning's
receipt — the newer — took the first morning's charge, and its own charge
then went to the first morning's receipt when it posted: both receipts on
the wrong day. A charge another waiting receipt has a strictly closer
claim on is not this receipt's to take.
"""
import datetime as dt
import unittest


from .test_a_receipt_snapped_early_meets_its_charge import (
    _attached, _enable_llm, _post, _snap)
from .util import make_db, write_config

DAY = dt.date.today() - dt.timedelta(days=2)
NEXT = DAY + dt.timedelta(days=1)


class WaitingReceiptsPairByTheirOwnDayTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _enable_llm(self.conn)

    def tearDown(self):
        self.conn.close()

    def _two_mornings(self, newer_first):
        dates = [NEXT, DAY] if newer_first else [DAY, NEXT]
        got = {d: _snap(self.conn, merchant="Juniper Cafe", total=4.80,
                        date=d) for d in dates}
        _post(self.conn, "c-day", 4.80, "JUNIPER CAFE 31", date=DAY)
        return got[DAY], got[NEXT]

    def test_the_older_receipt_snapped_first_takes_its_own_morning(self):
        old, new = self._two_mornings(newer_first=False)
        self.assertEqual(_attached(self.conn, old), ("c-day", "auto"))
        self.assertEqual(_attached(self.conn, new), (None, None))

    def test_the_newer_receipt_snapped_first_leaves_it_to_its_own_morning(self):
        old, new = self._two_mornings(newer_first=True)
        self.assertEqual(_attached(self.conn, old), ("c-day", "auto"))
        self.assertEqual(_attached(self.conn, new), (None, None))
        # and the second morning's charge, when it posts, is its own
        _post(self.conn, "c-next", 4.80, "JUNIPER CAFE 31", date=NEXT)
        self.assertEqual(_attached(self.conn, new), ("c-next", "auto"))


class StoresTheirLeadLetterKeepsApart(unittest.TestCase):
    def test_two_stores_differing_by_their_lead_letter_are_different(self):
        from oikonome.engine import receipt_match
        self.assertEqual(receipt_match.merchant_match(
            "Q DEPOT", ["V DEPOT 0042"]), "different")
        self.assertEqual(receipt_match.merchant_match(
            "Q DEPOT", ["Q DEPOT #12"]), "same")


class TheRightStoreOutranksANearerStranger(unittest.TestCase):
    def test_a_stranger_dated_on_the_posting_day_does_not_take_the_charge(self):
        """Two receipts of the same total: the right store's, two days
        before the charge posted, and another store's dated on the posting
        day. The right store keeps its claim."""
        import datetime as dt

        from oikonome.engine import receipt_match
        d = dt.date(2026, 3, 3)
        own = {"txn_id": "X", "exact": True, "merchant_match": "same",
               "day_gap": 2, "date": d}
        stranger = dict(own, merchant_match="different", day_gap=0)
        theirs = receipt_match._closer_claims([[own], [stranger]])
        self.assertEqual(theirs, [set(), {"X"}])


if __name__ == "__main__":
    unittest.main()
