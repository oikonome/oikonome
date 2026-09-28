"""A receipt snapped before its charge reaches the ledger meets the charge.

The receipt is stored WAITING — no transaction — and parsed as usual. When a
sync brings the charge (every importer and aggregator passes through
`upsert_transactions`), or when the parse finishes after the charge is
already here, the matcher pairs the two: exact cents inside the date window
first, then a near amount (a tip, a currency conversion) only when the
merchant also matches. Two equally good charges are a tie, and a tie waits
for a person rather than guessing. A pairing made while the charge is still
pending follows it when it posts, and a person's unmatch is remembered.
"""
import datetime as dt
import json
import unittest

import httpx

from oikonome.engine import budget, receipt_match, receipts
from oikonome.sync import base as sync_base
from oikonome.sync.base import Transaction

from .util import add_txn, make_db, write_config

PNG = b"\x89PNG..."
# the purchase happened yesterday, so its window is still open: a charge
# can still arrive inside it
DAY = dt.date.today() - dt.timedelta(days=1)
# a household day after DAY's automatic window has closed
CLOSED = DAY + dt.timedelta(days=receipt_match.AUTO_DAYS_AFTER + 1)


def _vision(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps(payload)}}]})
    return httpx.MockTransport(handler)


def _enable_llm(conn):
    cfg = budget.load_config(conn)
    cfg["llm_url"] = "http://llm.test"
    cfg["llm_model"] = "vision-model"
    budget.save_config(conn, cfg)


def _snap(conn, merchant="Maple Leaf Bistro", total=42.17, date=DAY):
    """A waiting receipt, parsed the way the vision model reads one."""
    rid = receipts.add(conn, None, PNG, "image/png")
    out = receipts.parse_one(conn, rid, transport=_vision({
        "merchant": merchant, "date": date.isoformat(), "total": total,
        "tax": None, "tip": None, "items": []}))
    assert out["status"] == "parsed", out
    return rid


def _txn(tid, amount, name, date=DAY + dt.timedelta(days=2),
         pending=False, raw=None, account="card"):
    return Transaction(id=tid, account_id=account, date=date, amount=amount,
                       name=name, pending=pending, raw=raw or {})


def _post(conn, *args, **kw):
    """One sync delivering one charge."""
    sync_base.upsert_transactions(conn, [_txn(*args, **kw)])


def _sync(conn, *txns):
    """One sync delivering several charges at once."""
    sync_base.upsert_transactions(conn, list(txns))


def _attached(conn, rid):
    r = conn.execute("SELECT txn_id, match_method FROM receipts "
                     "WHERE id = %s", (rid,)).fetchone()
    return r["txn_id"], r["match_method"]


class ReceiptWaitsForItsChargeTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _enable_llm(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_receipt_can_be_stored_with_no_transaction(self):
        rid = receipts.add(self.conn, None, PNG, "image/png")
        self.assertEqual(_attached(self.conn, rid), (None, None))
        listed = receipt_match.waiting(self.conn)
        self.assertEqual([w["id"] for w in listed["waiting"]], [rid])
        # a check image is read against the transaction it sits on, so it
        # cannot wait
        with self.assertRaises(ValueError):
            receipts.add(self.conn, None, PNG, "image/png", kind="check")
        # and the same limits hold as an attached upload
        with self.assertRaises(ValueError):
            receipts.add(self.conn, None, b"x", "text/html")

    def test_the_charge_arriving_later_takes_the_receipt(self):
        rid = _snap(self.conn)
        self.assertEqual(_attached(self.conn, rid), (None, None))
        _post(self.conn, "p-1", 42.17, "MAPLE LEAF BISTRO 0917")
        self.assertEqual(_attached(self.conn, rid), ("p-1", "auto"))
        self.assertEqual(receipt_match.waiting(self.conn)["count"], 0)

    def test_a_charge_already_here_is_found_when_the_parse_finishes(self):
        add_txn(self.conn, DAY + dt.timedelta(days=1), 42.17,
                "MAPLE LEAF BISTRO", txn_id="early-1")
        rid = _snap(self.conn)
        self.assertEqual(_attached(self.conn, rid), ("early-1", "auto"))

    def test_a_lone_exact_charge_under_another_name_matches_once_the_window_closes(self):
        # a descriptor that names the business's legal entity, not the
        # name on the receipt, is still the charge when the cents agree —
        # but not while a charge from the right store could still arrive
        rid = _snap(self.conn)
        _post(self.conn, "p-2", 42.17, "TST* NORTHWIND HOSPITALITY")
        self.assertEqual(_attached(self.conn, rid), (None, None))
        old = _snap(self.conn, date=DAY - dt.timedelta(days=20))
        _post(self.conn, "p-3", 42.17, "TST* NORTHWIND HOSPITALITY",
              date=DAY - dt.timedelta(days=18))
        self.assertEqual(_attached(self.conn, old), ("p-3", "auto"))
        # the open one closes, and the nightly pass pairs it
        receipt_match.match_waiting(self.conn)
        self.assertEqual(_attached(self.conn, rid), (None, None))
        self.conn.execute(
            "UPDATE receipts SET parsed = jsonb_set(parsed, '{date}', %s)"
            " WHERE id = %s",
            (f'"{(DAY - dt.timedelta(days=9)).isoformat()}"', rid))
        self.conn.execute("UPDATE transactions SET date = %s WHERE id = 'p-2'",
                          (DAY - dt.timedelta(days=8),))
        receipt_match.match_waiting(self.conn)
        self.assertEqual(_attached(self.conn, rid), ("p-2", "auto"))

    def test_outside_the_date_window_nothing_matches(self):
        rid = _snap(self.conn)
        _post(self.conn, "late", 42.17, "MAPLE LEAF BISTRO",
              date=DAY + dt.timedelta(days=receipt_match.AUTO_DAYS_AFTER + 1))
        _post(self.conn, "early", 42.17, "MAPLE LEAF BISTRO",
              date=DAY - dt.timedelta(days=receipt_match.AUTO_DAYS_BEFORE + 1))
        self.assertEqual(_attached(self.conn, rid), (None, None))
        # …but both are offered for a hand match
        w = receipt_match.waiting(self.conn)["waiting"][0]
        self.assertEqual(sorted(c["txn_id"] for c in w["candidates"]),
                         ["early", "late"])

    def test_money_in_never_takes_a_receipt(self):
        rid = _snap(self.conn)
        _post(self.conn, "refund", -42.17, "MAPLE LEAF BISTRO")
        self.assertEqual(_attached(self.conn, rid), (None, None))


class ToleranceAndMerchantTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _enable_llm(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_tip_added_after_printing_matches_by_merchant(self):
        rid = _snap(self.conn, total=40.00)
        # a charge in the band at another business does not take it …
        _post(self.conn, "other", 47.50, "QUILL AND INK STATIONERS")
        self.assertEqual(_attached(self.conn, rid), (None, None))
        # … the same business with the tip on top does — once the window
        # has closed, since an exact charge could still arrive inside it
        _post(self.conn, "tipped", 48.00, "MAPLE LEAF BISTRO")
        self.assertEqual(_attached(self.conn, rid), (None, None))
        receipt_match.match_waiting(self.conn, today=CLOSED)
        self.assertEqual(_attached(self.conn, rid), ("tipped", "auto"))

    def test_beyond_the_tip_band_is_not_a_match(self):
        lo, hi = receipt_match.band(40.00)
        self.assertEqual((lo, hi), (38.8, 50.0))
        rid = _snap(self.conn, total=40.00)
        _post(self.conn, "too-much", 50.01, "MAPLE LEAF BISTRO")
        self.assertEqual(_attached(self.conn, rid), (None, None))

    def test_the_merchant_breaks_an_exact_amount_tie(self):
        rid = _snap(self.conn, merchant="Corner Deli", total=12.50)
        # a stranger at the same price arrives first and is not taken …
        _post(self.conn, "a", 12.50, "QUILL AND INK STATIONERS")
        self.assertEqual(_attached(self.conn, rid), (None, None))
        # … the store's own charge, the next day, is
        _post(self.conn, "b", 12.50, "SQ *CORNERDELI 0099",
              date=DAY + dt.timedelta(days=3))
        self.assertEqual(_attached(self.conn, rid), ("b", "auto"))

    def test_the_merchant_breaks_a_tie_inside_one_sync(self):
        rid = _snap(self.conn, merchant="Corner Deli", total=12.50)
        _sync(self.conn, _txn("a", 12.50, "QUILL AND INK STATIONERS"),
              _txn("b", 12.50, "SQ *CORNERDELI 0099"))
        self.assertEqual(_attached(self.conn, rid), ("b", "auto"))

    def test_two_near_charges_at_the_same_store_wait(self):
        rid = _snap(self.conn, total=40.00)
        _sync(self.conn, _txn("n1", 46.00, "MAPLE LEAF BISTRO"),
              _txn("n2", 47.00, "MAPLE LEAF BISTRO",
                   date=DAY + dt.timedelta(days=3)))
        self.assertEqual(_attached(self.conn, rid), (None, None))


class ATieWaitsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _enable_llm(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_two_identical_charges_leave_the_receipt_waiting(self):
        rid = _snap(self.conn, total=6.25, merchant="Harbor Coffee")
        _sync(self.conn, _txn("c1", 6.25, "HARBOR COFFEE"),
              _txn("c2", 6.25, "HARBOR COFFEE",
                   date=DAY + dt.timedelta(days=1)))
        self.assertEqual(_attached(self.conn, rid), (None, None))
        w = receipt_match.waiting(self.conn)["waiting"][0]
        self.assertTrue(w["tie"])
        self.assertEqual(sorted(c["txn_id"] for c in w["candidates"]),
                         ["c1", "c2"])
        # the person picks one, and the other stays free for its own receipt
        self.assertTrue(receipt_match.match_by_hand(self.conn, rid, "c2"))
        self.assertEqual(_attached(self.conn, rid), ("c2", "manual"))

    def test_a_charge_with_a_receipt_is_not_offered_to_another(self):
        first = _snap(self.conn, total=6.25, merchant="Harbor Coffee")
        _post(self.conn, "c1", 6.25, "HARBOR COFFEE")
        self.assertEqual(_attached(self.conn, first), ("c1", "auto"))
        second = _snap(self.conn, total=6.25, merchant="Harbor Coffee")
        self.assertEqual(_attached(self.conn, second), (None, None))
        w = receipt_match.waiting(self.conn)["waiting"][0]
        self.assertEqual(w["candidates"], [])


class PendingToPostedKeepsTheMatchTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _enable_llm(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_receipt_on_a_hold_follows_it_when_it_posts(self):
        rid = _snap(self.conn)
        _post(self.conn, "hold-1", 42.17, "MAPLE LEAF BISTRO", pending=True,
              date=DAY)
        self.assertEqual(_attached(self.conn, rid), ("hold-1", "auto"))
        _post(self.conn, "post-1", 42.17, "MAPLE LEAF BISTRO",
              raw={"pending_transaction_id": "hold-1"})
        self.assertEqual(_attached(self.conn, rid), ("post-1", "auto"))

    def test_a_hand_match_on_a_hold_stays_a_hand_match_when_it_posts(self):
        # the settlement carries the receipt itself — it is not released
        # and re-guessed, so a person's choice is still theirs afterwards
        rid = receipts.add(self.conn, None, PNG, "image/png")
        _post(self.conn, "hold-4", 23.00, "QUILL AND INK", pending=True,
              date=DAY)
        self.assertTrue(receipt_match.match_by_hand(self.conn, rid, "hold-4"))
        _post(self.conn, "post-4", 23.00, "QUILL AND INK",
              raw={"pending_transaction_id": "hold-4"})
        self.assertEqual(_attached(self.conn, rid), ("post-4", "manual"))

    def test_a_hold_that_posts_with_a_tip_keeps_the_receipt(self):
        rid = _snap(self.conn, total=40.00)
        _post(self.conn, "hold-2", 40.00, "MAPLE LEAF BISTRO", pending=True,
              date=DAY)
        self.assertEqual(_attached(self.conn, rid), ("hold-2", "auto"))
        _post(self.conn, "post-2", 48.00, "MAPLE LEAF BISTRO",
              raw={"pending_transaction_id": "hold-2"})
        self.assertEqual(_attached(self.conn, rid), ("post-2", "auto"))

    def test_a_hold_retired_without_a_twin_puts_the_receipt_back(self):
        rid = _snap(self.conn, total=40.00)
        _post(self.conn, "hold-3", 40.00, "MAPLE LEAF BISTRO", pending=True,
              date=DAY)
        self.assertEqual(_attached(self.conn, rid), ("hold-3", "auto"))
        # the aggregator drops the hold without naming a successor, and the
        # charge posts for a different amount, so no re-anchor finds it
        sync_base.mark_removed(self.conn, ["hold-3"])
        _post(self.conn, "post-3", 48.00, "MAPLE LEAF BISTRO")
        # back to waiting, and paired with the tipped charge once the
        # window has closed with no exact charge arriving
        self.assertEqual(_attached(self.conn, rid), (None, None))
        receipt_match.match_waiting(self.conn, today=CLOSED)
        self.assertEqual(_attached(self.conn, rid), ("post-3", "auto"))


class HandMatchAndUnmatchTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _enable_llm(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_unmatch_puts_it_back_and_the_matcher_does_not_repeat_itself(self):
        rid = _snap(self.conn)
        _post(self.conn, "p-9", 42.17, "MAPLE LEAF BISTRO")
        self.assertEqual(_attached(self.conn, rid), ("p-9", "auto"))
        self.assertEqual(receipt_match.unmatch(self.conn, rid), "p-9")
        self.assertEqual(_attached(self.conn, rid), (None, None))
        # the next sync does not pair it straight back …
        _post(self.conn, "p-9", 42.17, "MAPLE LEAF BISTRO")
        receipt_match.match_waiting(self.conn)
        self.assertEqual(_attached(self.conn, rid), (None, None))
        # … and it is not offered that charge again
        w = receipt_match.waiting(self.conn)["waiting"][0]
        self.assertNotIn("p-9", [c["txn_id"] for c in w["candidates"]])
        # a person can still put it there by hand
        self.assertTrue(receipt_match.match_by_hand(self.conn, rid, "p-9"))
        self.assertEqual(_attached(self.conn, rid), ("p-9", "manual"))

    def test_a_hand_match_is_refused_for_a_gone_charge_or_a_matched_receipt(self):
        rid = receipts.add(self.conn, None, PNG, "image/png")
        self.assertFalse(receipt_match.match_by_hand(self.conn, rid, "nope"))
        add_txn(self.conn, DAY, 9.99, "QUILL AND INK", txn_id="q-1")
        add_txn(self.conn, DAY, 19.99, "QUILL AND INK", txn_id="q-2")
        self.assertTrue(receipt_match.match_by_hand(self.conn, rid, "q-1"))
        # already attached: a second hand match does not move it
        self.assertFalse(receipt_match.match_by_hand(self.conn, rid, "q-2"))
        self.assertEqual(_attached(self.conn, rid), ("q-1", "manual"))

    def test_matching_is_idempotent(self):
        rid = _snap(self.conn)
        _post(self.conn, "p-5", 42.17, "MAPLE LEAF BISTRO")
        for _ in range(3):
            receipt_match.match_waiting(self.conn)
        self.assertEqual(_attached(self.conn, rid), ("p-5", "auto"))
        n = self.conn.execute("SELECT count(*) AS n FROM receipts "
                              "WHERE txn_id = 'p-5'").fetchone()["n"]
        self.assertEqual(n, 1)


class DecideIsPureTests(unittest.TestCase):
    """The rules on their own, without a database."""

    @staticmethod
    def _c(tid, exact=True, merchant="unknown", gap=1):
        return {"txn_id": tid, "exact": exact, "merchant_match": merchant,
                "day_gap": gap}

    def test_rules(self):
        d = receipt_match.decide
        self.assertEqual(d([self._c("a")]), ("a", "exact"))
        self.assertEqual(d([self._c("a"), self._c("b")]), (None, "tie"))
        self.assertEqual(d([self._c("a", merchant="different")],
                           closed=False), (None, "early"))
        self.assertEqual(d([self._c("a", merchant="different")]),
                         ("a", "exact"))
        self.assertEqual(d([self._c("a", merchant="different"),
                            self._c("b", merchant="same")]),
                         ("b", "exact+merchant"))
        self.assertEqual(d([self._c("a", exact=False)]), (None, "none"))
        self.assertEqual(d([self._c("a", exact=False, merchant="same")]),
                         ("a", "near+merchant"))
        self.assertEqual(d([self._c("a", gap=receipt_match.AUTO_DAYS_AFTER + 1)]),
                         (None, "none"))

    def test_merchant_evidence(self):
        m = receipt_match.merchant_match
        self.assertEqual(m("Corner Deli", ["SQ *CORNERDELI 0099"]), "same")
        self.assertEqual(m("Maple Leaf Bistro", ["MAPLE LEAF BISTRO #12"]),
                         "same")
        self.assertEqual(m("Maple Leaf Bistro", ["QUILL AND INK"]),
                         "different")
        self.assertEqual(m(None, ["QUILL AND INK"]), "unknown")


if __name__ == "__main__":
    unittest.main()
