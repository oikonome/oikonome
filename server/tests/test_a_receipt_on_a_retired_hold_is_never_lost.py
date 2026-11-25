"""A receipt put on a pending charge is never left on a row no screen shows,
and is never taken off a row that may still come back.

A person opens a pending charge and attaches the slip. The hold is then
retired. Where the feed names the posted charge, or the re-anchor finds the
same charge under a new id, the receipt moves with everything else on the
row; a row the feed reports again under its own id simply has its receipt
still. Only when the row has been gone longer than the reconcile window and
the re-anchor says it has no twin does the nightly pass put the receipt
back on the waiting list, where the matcher pairs it with the store's
tipped charge by its own rule or a person pairs it by hand (read or not).
Two look-alikes are left for the person, no sync ever releases one, and
the Receipts page lists a receipt's lines whatever became of its charge.
"""
import datetime as dt
import json
import unittest

import httpx

from oikonome.engine import budget, receipt_match, receipts
from oikonome.sync import base as sync_base
from oikonome.sync import adopt, reanchor
from oikonome.sync.base import Transaction

from .util import add_txn, make_db, write_config

PNG = b"\x89PNG..."
# long enough ago that the row has waited out the reconcile window
DAY = dt.date.today() - dt.timedelta(days=adopt.RECONCILE_DAYS + 6)
SLIP = {"merchant": "Harbor Noodle House", "date": DAY.isoformat(),
        "total": 40.00, "tax": 3.00, "tip": None,
        "items": [{"description": "ramen bowl", "qty": 2, "amount": 37.00}]}


def _vision(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps(payload)}}]})
    return httpx.MockTransport(handler)


def _where(conn, rid):
    r = conn.execute("SELECT txn_id, match_method FROM receipts "
                     "WHERE id = %s::uuid", (rid,)).fetchone()
    return r["txn_id"], r["match_method"]


class ReceiptOnARetiredHoldTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        cfg = budget.load_config(self.conn)
        cfg["llm_url"] = "http://llm.test"
        cfg["llm_model"] = "vision-model"
        budget.save_config(self.conn, cfg)
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", pending=1,
                txn_id="hold-1")
        # attached from the charge itself: no matcher, no waiting list
        self.rid = receipts.add(self.conn, "hold-1", PNG, "image/png")
        out = receipts.parse_one(self.conn, self.rid, transport=_vision(SLIP))
        assert out["status"] == "parsed", out
        assert _where(self.conn, self.rid) == ("hold-1", None)

    def tearDown(self):
        self.conn.close()

    def _retire_hold(self, *, days_ago=adopt.RECONCILE_DAYS + 2):
        """The hold left the ledger `days_ago`, and the slip was on it at
        least that long."""
        self.conn.execute(
            "UPDATE transactions SET removed = 1 WHERE id = 'hold-1'")
        self.conn.execute(
            "UPDATE transactions SET removed_at = now() - make_interval("
            "days => %s) WHERE id = 'hold-1'", (days_ago,))
        self.conn.execute(
            "UPDATE receipts SET created_at = now() - make_interval(days => %s)",
            (days_ago,))

    def _nightly(self):
        """The steps of the nightly pass that touch a retired row's
        receipt, in the order the worker runs them."""
        reanchor.reanchor_stranded(self.conn)
        freed = receipt_match.release_unplaced(self.conn)
        return {**receipt_match.match_waiting(self.conn), "freed": freed}

    def _posted(self, tid, amount, days=2, raw=None):
        sync_base.upsert_transactions(self.conn, [Transaction(
            id=tid, account_id="card", date=DAY + dt.timedelta(days=days),
            amount=amount, name="HARBOR NOODLE HOUSE", pending=False,
            raw=raw or {})])

    def test_a_hold_posting_with_a_tip_and_no_link_hands_its_receipt_on(self):
        self._retire_hold()
        # the posted charge: a new id, the tip on top, nothing naming the hold
        self._posted("posted-1", 47.00)
        # no sync releases a receipt: only the nightly, after the wait
        self.assertEqual(_where(self.conn, self.rid), ("hold-1", None))
        self._nightly()
        self.assertEqual(_where(self.conn, self.rid), ("posted-1", "auto"))
        self.assertEqual(receipt_match.waiting(self.conn)["count"], 0)

    def test_a_hold_withdrawn_with_no_successor_sends_its_receipt_to_waiting(self):
        self._retire_hold()
        self.assertEqual(self._nightly()["freed"], 1)
        self.assertEqual(_where(self.conn, self.rid), (None, None))
        self.assertEqual(
            [w["id"] for w in receipt_match.waiting(self.conn)["waiting"]],
            [self.rid])
        # being released is not a refusal: a person may pair it with any
        # charge, the withdrawn one's later twin included
        self.assertEqual(self.conn.execute(
            "SELECT unmatched_txn_ids FROM receipts WHERE id = %s::uuid",
            (self.rid,)).fetchone()["unmatched_txn_ids"] or [], [])

    def test_the_nightly_pass_repairs_a_stranded_receipt_once(self):
        self._retire_hold()
        self.assertEqual(self._nightly()["freed"], 1)
        again = self._nightly()
        self.assertEqual((again["freed"], again["matched"]), (0, 0))
        self.assertEqual(_where(self.conn, self.rid), (None, None))

    def test_a_row_gone_only_a_few_days_keeps_its_receipt(self):
        self._retire_hold(days_ago=3)
        self.assertEqual(self._nightly()["freed"], 0)
        self.assertEqual(_where(self.conn, self.rid), ("hold-1", None))

    def test_the_wait_counts_from_the_retirement_not_the_charge(self):
        # an old charge, its slip on it for weeks, retired yesterday: it
        # has not waited yet
        self._retire_hold()
        self.conn.execute(
            "UPDATE transactions SET removed_at = now() - interval '1 day'"
            " WHERE id = 'hold-1'")
        self.assertEqual(self._nightly()["freed"], 0)
        self.conn.execute(
            "UPDATE transactions SET removed_at = now() - make_interval("
            "days => %s) WHERE id = 'hold-1'", (adopt.RECONCILE_DAYS + 1,))
        self.assertEqual(self._nightly()["freed"], 1)

    def test_a_row_that_returns_under_its_own_id_still_has_its_receipt(self):
        self._retire_hold(days_ago=3)
        self._nightly()
        # the feed reports the row again
        self.conn.execute(
            "UPDATE transactions SET removed = 0 WHERE id = 'hold-1'")
        self._nightly()
        self.assertEqual(_where(self.conn, self.rid), ("hold-1", None))
        self.assertEqual([r["id"] for r in receipts.for_txn(
            self.conn, "hold-1")], [self.rid])

    def test_a_posted_twin_arriving_a_sync_later_takes_the_note_too(self):
        self.conn.execute("INSERT INTO transaction_notes (txn_id, note) "
                          "VALUES ('hold-1', 'team lunch')")
        self.conn.execute("INSERT INTO manual_categories (transaction_id,"
                          " category) VALUES ('hold-1', 'FOOD_AND_DRINK')")
        # the hold is dropped in one sync…
        self._retire_hold(days_ago=1)
        receipt_match.match_waiting(self.conn)
        self.assertEqual(_where(self.conn, self.rid), ("hold-1", None))
        # …and the posted charge, tip and all, names it in the next
        self._posted("posted-1", 47.00, raw={"pending_transaction_id": "hold-1"})
        self.assertEqual(_where(self.conn, self.rid), ("posted-1", None))
        self.assertEqual(self.conn.execute(
            "SELECT note FROM transaction_notes WHERE txn_id = 'posted-1'"
        ).fetchone()["note"], "team lunch")
        self.assertEqual(self.conn.execute(
            "SELECT transaction_id FROM manual_categories"
        ).fetchone()["transaction_id"], "posted-1")

    def test_two_look_alikes_are_left_for_the_person(self):
        self._retire_hold()
        for n in (1, 2):
            add_txn(self.conn, DAY + dt.timedelta(days=n), 40.00,
                    "HARBOR NOODLE HOUSE", txn_id=f"alike-{n}")
        self.assertEqual(self._nightly()["freed"], 0)
        self.assertEqual(_where(self.conn, self.rid), ("hold-1", None))

    def test_a_receipt_nothing_could_read_is_released_where_a_person_can_reach_it(self):
        self.conn.execute(
            "UPDATE receipts SET status = 'failed', parsed = NULL")
        self.conn.execute("DELETE FROM receipt_items")
        self._retire_hold()
        self.assertEqual(self._nightly()["freed"], 1)
        listed = receipt_match.waiting(self.conn)["waiting"]
        self.assertEqual([w["id"] for w in listed], [self.rid])
        # and it pairs by hand with any live charge
        add_txn(self.conn, DAY + dt.timedelta(days=30), 55.00, "ELSEWHERE",
                txn_id="by-hand")
        self.assertTrue(receipt_match.match_by_hand(
            self.conn, self.rid, "by-hand"))

    def test_a_receipt_the_re_anchor_can_place_is_left_for_it(self):
        # the same charge under a new id: every child of the row moves
        # together, so nothing takes the receipt out from under the
        # re-anchor and leaves the note behind
        self.conn.execute("INSERT INTO transaction_notes (txn_id, note) "
                          "VALUES ('hold-1', 'team lunch')")
        self._retire_hold()
        add_txn(self.conn, DAY + dt.timedelta(days=1), 40.00,
                "HARBOR NOODLE HOUSE", txn_id="renumbered-1")
        self.assertEqual(receipt_match.release_unplaced(self.conn), 0)
        self.assertEqual(_where(self.conn, self.rid), ("hold-1", None))
        self._nightly()
        self.assertEqual(_where(self.conn, self.rid), ("renumbered-1", None))
        self.assertEqual(self.conn.execute(
            "SELECT txn_id FROM transaction_notes").fetchone()["txn_id"],
            "renumbered-1")

    def test_a_hold_a_person_retired_by_hand_keeps_its_receipt(self):
        self._retire_hold()
        self.conn.execute("UPDATE transactions SET retired_at = now() "
                          "WHERE id = 'hold-1'")
        self.assertEqual(self._nightly()["freed"], 0)
        self.assertEqual(_where(self.conn, self.rid), ("hold-1", None))

    def test_the_receipts_page_lists_the_lines_whatever_became_of_the_charge(self):
        self.assertEqual(
            len(receipts.search_items(self.conn, q="ramen")["rows"]), 1)
        self._retire_hold()
        # before any pass has run
        self.assertEqual(
            len(receipts.search_items(self.conn, q="ramen")["rows"]), 1)
        self._nightly()
        self.assertEqual(
            len(receipts.search_items(self.conn, q="ramen")["rows"]), 1)


class OneSlipCountsOnceTests(unittest.TestCase):
    """The person attached the same slip again to the posted charge."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        cfg = budget.load_config(self.conn)
        cfg["llm_url"] = "http://llm.test"
        cfg["llm_model"] = "vision-model"
        budget.save_config(self.conn, cfg)
        add_txn(self.conn, DAY, 40.00, "HARBOR NOODLE HOUSE", pending=1,
                txn_id="hold-1")
        add_txn(self.conn, DAY + dt.timedelta(days=2), 47.00,
                "HARBOR NOODLE HOUSE", txn_id="posted-1")
        self.first = self._slip("hold-1", PNG)

    def _slip(self, txn_id, image):
        rid = receipts.add(self.conn, txn_id, image, "image/png")
        out = receipts.parse_one(self.conn, rid, transport=_vision(SLIP))
        assert out["status"] == "parsed", out
        return rid

    def _retire(self):
        """The hold left the ledger past the reconcile window ago."""
        self.conn.execute(
            "UPDATE transactions SET removed = 1 WHERE id = 'hold-1'")
        self.conn.execute(
            "UPDATE transactions SET removed_at = now() - make_interval("
            "days => %s) WHERE id = 'hold-1'", (adopt.RECONCILE_DAYS + 2,))
        self.conn.execute(
            "UPDATE receipts SET created_at = now() - make_interval(days => %s)"
            " WHERE id = %s::uuid", (adopt.RECONCILE_DAYS + 2, self.first))

    def _totals(self):
        got = receipts.search_items(self.conn, q="ramen", group="merchant")
        # the lines that count; a copy's lines are listed apart
        return (len([r for r in got["rows"] if r["counts"]]),
                [g["total"] for g in got["groups"]])

    def test_the_same_image_on_the_posted_charge_is_one_purchase(self):
        self._retire()
        self._slip("posted-1", PNG)
        self.assertEqual(self._totals(), (1, [37.0]))
        # the stranded copy is not released to wait beside it
        reanchor.reanchor_stranded(self.conn)
        self.assertEqual(receipt_match.release_unplaced(self.conn), 0)
        self.assertEqual(_where(self.conn, self.first), ("hold-1", None))
        self.assertEqual(self._totals(), (1, [37.0]))

    def test_a_second_photo_of_the_slip_counts_once_and_holds_the_first(self):
        self._retire()
        self._slip("posted-1", PNG + b"another photo")
        self.assertEqual(self._totals(), (1, [37.0]))
        # the stranded copy reads like the posted charge's: held back
        reanchor.reanchor_stranded(self.conn)
        self.assertEqual(receipt_match.release_unplaced(self.conn), 0)
        self.assertEqual(self._totals(), (1, [37.0]))

    def test_a_copy_already_waiting_is_not_counted_again(self):
        # released before the charge had its own copy
        self.conn.execute("UPDATE receipts SET txn_id = NULL")
        self._slip("posted-1", PNG)
        self.assertEqual(self._totals(), (1, [37.0]))
        # still on the waiting list, for the person to remove
        self.assertEqual(receipt_match.waiting(self.conn)["count"], 1)

    def test_the_tag_report_counts_the_slip_once(self):
        self.conn.execute("UPDATE receipts SET txn_id = NULL")
        second = self._slip("posted-1", PNG)
        for rid in (self.first, second):
            self.assertEqual(receipts.set_tag(self.conn, rid, 0, "work"), 1)
        day = DAY + dt.timedelta(days=2)
        months = {(DAY.year, DAY.month), (day.year, day.month)}
        reps = [receipts.report(self.conn, "work", y, m) for y, m in months]
        self.assertEqual((sum(r["count"] for r in reps),
                          sum(r["total"] for r in reps)), (1, 37.0))

    def _listed(self):
        got = receipts.search_items(self.conn, q="ramen", group="merchant")
        return [(r["counts"], r["why"]) for r in got["rows"]]

    def test_lines_of_a_charge_off_the_ledger_are_listed_not_added_up(self):
        self._retire()
        self.assertEqual(self._totals(), (0, []))
        self.assertEqual(self._listed(), [(False, "off_ledger")])
        # a hold a person retired by hand, the same
        self.conn.execute("UPDATE transactions SET retired_at = now() "
                          "WHERE id = 'hold-1'")
        self.assertEqual(self._totals(), (0, []))
        self.assertEqual(self._listed(), [(False, "off_ledger")])

    def test_two_purchases_on_live_charges_are_two(self):
        # two photos read alike on two live charges, nothing stranded: two
        self._slip("posted-1", PNG + b"another meal")
        self.assertEqual(self._totals(), (2, [74.0]))


if __name__ == "__main__":
    unittest.main()
