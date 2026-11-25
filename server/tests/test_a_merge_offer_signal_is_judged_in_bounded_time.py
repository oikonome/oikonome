"""A stored merge offer's signals are judged in bounded time, whatever a
restored archive put in them.

The one-shared-word rule reads every signal of every pending offer on each
Today load, each daily email and each Merchants page. A signal comes back
from an archive as whatever text the file held, up to megabytes, and the
pattern that told a pointer sentence ('and "A" is one merchant with "B"')
from evidence backtracked on a crafted one while holding the interpreter
lock of the shared web process. A signal longer than any the proposer
writes is not judged at all, and the pointer sentences are recognised by
their fixed words, not by a pattern.
"""
import time
import unittest

from oikonome.engine import merchant_merge as mm

from .test_merchant_merge_proposals import CITIES

_BOUND = 0.25


class ASignal(unittest.TestCase):
    def _quick(self, signals, a="Juniper Pub", b="Juniper Tavern"):
        t = time.perf_counter()
        out = mm.one_word_only(signals, a, b, CITIES)
        took = time.perf_counter() - t
        self.assertLess(took, _BOUND, f"took {took:.3f} s")
        return out

    def test_a_hostile_restored_signal_is_judged_quickly(self):
        for n in (8_000, 100_000):
            hostile = 'and "' + '" is one merchant with "' * n + "x"
            with self.subTest(n=n):
                self._quick([hostile])
                self._quick(['all are outlets of "' + '"' * n + "x"])

    def test_a_signal_longer_than_the_proposer_writes_is_not_evidence(self):
        long_pointer = 'and "' + "a" * 5000 + '" is one merchant with "b"'
        # only an over-long signal: nothing stands, the offer is no
        # one-word offer (it is judged by its other reads)
        self.assertFalse(self._quick([long_pointer]))
        # beside a one-word line of business it changes nothing
        line = "same registered line of business (MCC 5813) in Fernvale"
        self.assertEqual(self._quick([line, long_pointer]),
                         self._quick([line]))

    def test_the_longest_signal_the_proposer_writes_is_still_read(self):
        from oikonome.sync.base import MAX_DESCRIPTOR
        self.assertGreaterEqual(mm.MAX_SIGNAL, 2 * MAX_DESCRIPTOR + len(
            mm._POINTER_HEAD + mm._POINTER_JOIN + '"'))
        line = "same registered line of business (MCC 5813) in Fernvale"
        longest = (mm._POINTER_HEAD + "a" * MAX_DESCRIPTOR + mm._POINTER_JOIN
                   + "b" * MAX_DESCRIPTOR + '"')
        self.assertTrue(self._quick([line, longest]))

    def test_the_pointer_sentences_still_are_not_evidence(self):
        line = "same registered line of business (MCC 5813) in Fernvale"
        for ptr in ('and "Juniper Bar" is one merchant with "Juniper Pub"',
                    'all are outlets of "Juniper"',
                    'and "Say "hi"" is one merchant with "X"'):
            with self.subTest(ptr=ptr):
                # a pointer is no proof: the offer rests on its one line
                self.assertTrue(self._quick([line, ptr]))
                self.assertFalse(self._quick([ptr]))
        # a sentence that is not quite a pointer is name evidence
        for s in ('and "A" is one merchant with B', 'all are outlets of "',
                  'and "A"\n" is one merchant with "B"'):
            with self.subTest(s=s):
                self.assertFalse(self._quick([line, s]))


class ARestoredOffer(unittest.TestCase):
    def test_a_reason_longer_than_the_proposer_writes_does_not_land(self):
        import json
        import uuid

        from oikonome.sync import restore

        from .test_merchant_restore_pointers import M_COLS, _anchors, _zip
        from .util import make_db
        conn = make_db()
        self.addCleanup(conn.close)
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        rows = [{"id": a, "name": "Juniper Pub", "name_source": "layer1",
                 "kind": "merchant"},
                {"id": b, "name": "Juniper Tavern", "name_source": "layer1",
                 "kind": "merchant"}]
        kept = "same registered line of business (MCC 5813) in Fernvale"
        hostile = 'and "' + '" is one merchant with "' * 50_000 + "x"
        data = _zip({
            "merchants": (M_COLS, rows),
            "merchant_canonical": _anchors(rows),
            "merchant_merge_proposals": (
                ["id", "from_merchant_id", "into_merchant_id", "evidence",
                 "status", "created_at", "decided_at"],
                [{"id": "mm:x", "from_merchant_id": a, "into_merchant_id": b,
                  "evidence": json.dumps({"signals": [kept, hostile],
                                          "supports": [hostile]}),
                  "status": "pending"}])})
        restore.restore_zip(conn, data)
        ev = conn.execute("SELECT evidence FROM merchant_merge_proposals"
                          ).fetchone()["evidence"]
        self.assertEqual(ev["signals"], [kept])
        self.assertEqual(ev["supports"], [])


if __name__ == "__main__":
    unittest.main()
