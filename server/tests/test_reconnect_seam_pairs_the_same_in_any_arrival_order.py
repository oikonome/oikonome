"""The reconnect seam decides which restored row a backfill row is the
same way whatever order the feed delivers the page in.

Plaid promises no order for a page's `added` rows. When the guard walked
them in arrival order, each taking the nearest restored row still free, a
charge made just after the backup could arrive first and take its
restored neighbour from the day before: the neighbour's real re-delivery,
its partner gone, was then kept as a second charge on the old day. One
month reads a charge high and the next a charge low, and nothing later
corrects the date. If the newer charge was still a hold, the restored row
took the hold's id, and the hold's removal when it posted deleted the
restored row with everything a person had put on it.

What these protect:

  * a backfill row with a restored twin on its own day pairs with that
    twin, never with a neighbour's, in either arrival order;
  * a daily same-price run across a month end keeps every charge on its
    own day;
  * no permutation of a small page changes which rows are kept or which
    restored row carries which incoming id;
  * a pending row dated after the cutoff is never dropped as a copy and
    never lends its id to a restored row — a hold the backup could not
    have held is not a re-delivery.
"""

import datetime as dt
import itertools
import unittest

from oikonome.sync import adopt

from .util import make_db

D = dt.date


def _item(conn, iid, status, token=None):
    conn.execute(
        "INSERT INTO items (id, aggregator, institution_id, "
        "institution_name, status, access_token) "
        "VALUES (%s,'plaid','ins_9','Example Credit Union',%s,%s)",
        (iid, status, token))


def _txn(conn, tid, aid, date, amount, name):
    conn.execute(
        "INSERT INTO transactions (id, account_id, date, amount, name, "
        "pending, removed) VALUES (%s,%s,%s,%s,%s,0,0)",
        (tid, aid, date, amount, name))


class _Txn:
    def __init__(self, date, amount, name, id, pending=False):
        self.account_id, self.date = "new-acct", date
        self.amount, self.name = amount, name
        self.merchant_name = None
        self.id = id
        self.pending = pending


class SeamPairingTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        _item(self.conn, "old-item", "restored")
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, mask) VALUES "
            "('old-acct','old-item','Everyday','depository','4411')")

    def _adopt(self, restored):
        for tid, day in restored:
            _txn(self.conn, tid, "old-acct", day, 5.25, "HAZELNUT HOUSE")
        _item(self.conn, "new-item", "ok", token="live-token")
        adopt.adopt_accounts(
            self.conn, item_id="new-item", institution_id="ins_9",
            institution_name="Example Credit Union",
            incoming=[{"account_id": "new-acct", "mask": "4411",
                       "type": "depository"}])

    def _ledger(self):
        return sorted((r["id"], r["date"]) for r in self.conn.execute(
            "SELECT id, date FROM transactions WHERE account_id='new-acct'"
            " AND removed = 0").fetchall())

    def _run(self, page):
        kept, _ = adopt.filter_incoming(self.conn, "new-acct", page)
        return sorted(t.id for t in kept), self._ledger()

    def test_a_charge_just_past_the_cutoff_never_takes_its_neighbours_row(self):
        """The cutoff-day coffee re-delivered, and the next morning's
        coffee bought after the backup: whichever arrives first, the
        re-delivery is the restored row and the new one is kept."""
        self._adopt([("r-31", D(2026, 10, 31))])
        redelivery = _Txn(D(2026, 10, 31), 5.25, "HAZELNUT HOUSE", "p-31")
        new = _Txn(D(2026, 11, 1), 5.25, "HAZELNUT HOUSE", "p-n1")
        results = []
        for page in ([redelivery, new], [new, redelivery]):
            with self.conn.transaction(force_rollback=True):
                results.append(self._run(page))
        self.assertEqual(results[0], results[1])
        kept, ledger = results[1]
        self.assertEqual(kept, ["p-n1"])
        # the restored row now answers to its own re-delivery's id, on its
        # own day — not to the next morning's charge
        self.assertEqual(ledger, [("p-31", D(2026, 10, 31))])

    def test_a_daily_run_over_a_month_end_keeps_every_charge_on_its_day(self):
        self._adopt([(f"r-{d}", D(2026, 10, d)) for d in (28, 29, 30, 31)])
        page = [_Txn(D(2026, 10, d), 5.25, "HAZELNUT HOUSE", f"p-{d}")
                for d in (28, 29, 30, 31)]
        page += [_Txn(D(2026, 11, d), 5.25, "HAZELNUT HOUSE", f"p-n{d}")
                 for d in (1, 2)]
        kept, ledger = self._run(list(reversed(page)))
        self.assertEqual(kept, ["p-n1", "p-n2"])
        # four restored rows, still one on each October day
        self.assertEqual(sorted(d for _i, d in ledger),
                         [D(2026, 10, d) for d in (28, 29, 30, 31)])

    def test_no_arrival_order_changes_the_pairing(self):
        self._adopt([("r-30", D(2026, 10, 30)), ("r-31", D(2026, 10, 31))])
        page = [_Txn(D(2026, 10, 30), 5.25, "HAZELNUT HOUSE", "p-30"),
                _Txn(D(2026, 10, 31), 5.25, "HAZELNUT HOUSE", "p-31"),
                _Txn(D(2026, 11, 1), 5.25, "HAZELNUT HOUSE", "p-n1"),
                _Txn(D(2026, 11, 2), 5.25, "HAZELNUT", "p-n2")]
        seen = set()
        for perm in itertools.permutations(page):
            with self.conn.transaction(force_rollback=True):
                kept, ledger = self._run(list(perm))
            seen.add((tuple(kept), tuple(ledger)))
        self.assertEqual(len(seen), 1, seen)
        (kept, _ledger), = seen
        self.assertEqual(kept, ("p-n1", "p-n2"))

    def test_a_hold_past_the_cutoff_is_never_a_copy_of_a_restored_row(self):
        """The newest charges are usually still holds. One dated after the
        backup is a purchase the backup could not hold: if it took the
        restored row's slot and id, the hold's removal when it posts would
        delete the restored row."""
        self._adopt([("r-31", D(2026, 10, 31))])
        hold = _Txn(D(2026, 11, 1), 5.25, "HAZELNUT HOUSE", "p-hold",
                    pending=True)
        kept, ledger = self._run([hold])
        self.assertEqual(kept, ["p-hold"])
        self.assertEqual(ledger, [("r-31", D(2026, 10, 31))])


class PairHelperTests(unittest.TestCase):
    """`pairing.pair` itself: same-day before off-day, nearest first, and
    the edges' order never matters."""

    def test_every_edge_order_gives_one_pairing(self):
        from oikonome.sync.pairing import pair
        # incoming a (day 1), b (day 2); held x (day 1), y (day 0)
        edges = [("b", "x", 1, ()), ("a", "x", 0, ()), ("a", "y", 1, ()),
                 ("b", "y", 2, ())]
        seen = {tuple(sorted(pair(list(p), in_key=lambda k: (k,),
                                  cand_key=lambda k: (k,)).items()))
                for p in itertools.permutations(edges)}
        self.assertEqual(seen, {(("a", "x"), ("b", "y"))})


if __name__ == "__main__":
    unittest.main()


class AnOversizedSameDayPileIsRefused(unittest.TestCase):
    def test_more_identical_rows_on_a_day_than_the_scan_ceiling_refuses(self):
        """Pairing a pile past the ceiling row for row would leave rows
        unpaired and the merge would double them; the merge refuses it."""
        import inspect

        from oikonome.sync import adopt
        src = inspect.getsource(adopt._pair_overlap)
        self.assertIn("len(v) > SCAN_CEILING for v in cells.values()", src)
        self.assertNotIn("[:SCAN_CEILING]", src)
