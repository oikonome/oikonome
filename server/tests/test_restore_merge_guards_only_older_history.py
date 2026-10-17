"""A restore merged onto a live account guards only what is older than it.

When the archive is restored after the same bank was linked, the live
account already holds the link's first rows — pending holds included — and
the days both lineages cover are paired one to one at the merge. What the
backfill guard is still needed for is the older history the link has yet
to replay. If it treated the live lineage's own rows as restored ones, a
second, distinct same-price charge would be dropped as a copy of the
first (and its bank id written onto it), and a hold's settlement would be
dropped as a copy of the hold — which the bank then retires, so the charge
would exist nowhere.

The overlap pairing itself is one to one by DAY: a charge recurring every
morning on both lineages pairs morning for morning, and a restored row
older than the link's first row is never paired with a live row on a
different day.
"""

import datetime as dt
import unittest

from oikonome.sync import adopt, export, restore

from .test_restore_after_link_merges_onto_the_live_account import (
    _account, _fresh_household, _item, _txn)
from .util import make_db

D = dt.date


def _archive(rows, pending=(), carried=()):
    a = make_db()
    try:
        _item(a, "old-item", token="tok-old")
        _account(a, "old-acct", "old-item", "1234")
        for tid, day, amount, name in rows:
            _txn(a, tid, "old-acct", day, amount, name)
        if pending:
            a.execute("UPDATE transactions SET pending = 1 WHERE id = ANY(%s)",
                      (list(pending),))
        if carried:
            # what an open guard on an older install noted in these rows'
            # raw records
            a.execute("UPDATE transactions SET raw = COALESCE(raw, '{}')"
                      " || jsonb_build_object('_carried_from', 'gone-id')"
                      " WHERE id = ANY(%s)", (list(carried),))
        a.execute("INSERT INTO transaction_notes (txn_id, note)"
                  " SELECT id, 'note on ' || id FROM transactions")
        return export.build_zip(a)
    finally:
        a.close()


class _Merged(unittest.TestCase):
    archive_rows = [("old-rent", D(2024, 3, 1), 500.0, "LANDLORD RENT"),
                    ("old-last", D(2026, 6, 1), 9.99, "STREAMCO")]
    archive_pending: tuple = ()
    archive_carried: tuple = ()
    update_status = "INITIAL_UPDATE_COMPLETE"

    def setUp(self):
        # the archive first: make_db points the household helpers at the
        # test database
        data = _archive(self.archive_rows, self.archive_pending,
                        self.archive_carried)
        self.b = _fresh_household(type(self).__name__.lower())
        self.addCleanup(self.b.close)
        _item(self.b, "new-item", token="tok-new",
              update_status=self.update_status)
        _account(self.b, "new-acct", "new-item", "1234")
        self.live_rows()
        restore.restore_zip(self.b, data)

    def live_rows(self):
        pass

    def held(self):
        return {r["id"]: dict(r) for r in self.b.execute(
            "SELECT id, date, amount, name FROM transactions"
            " WHERE account_id = 'new-acct' AND removed = 0").fetchall()}


class TheLinksOwnRowsAbsorbNothing(_Merged):

    def live_rows(self):
        _txn(self.b, "live-coffee", "new-acct", D(2026, 5, 28), 5.25,
             "Beanhouse")
        _txn(self.b, "live-hold", "new-acct", D(2026, 5, 29), 30.0,
             "Fuel Stop")
        self.b.execute("UPDATE transactions SET pending = 1"
                       " WHERE id = 'live-hold'")

    def test_the_guard_is_still_armed_for_the_older_history(self):
        self.assertIsNotNone(adopt.pending(self.b, "new-acct"))
        kept, _ = adopt.filter_incoming(self.b, "new-acct", [
            {"id": "plaid-rent", "date": D(2024, 3, 1), "amount": 500.0,
             "name": "Landlord Rent", "merchant_name": None}])
        self.assertEqual(kept, [])

    def test_a_second_coffee_keeps_its_own_id(self):
        second = {"id": "plaid-coffee-2", "date": D(2026, 5, 30),
                  "amount": 5.25, "name": "Beanhouse",
                  "merchant_name": "Beanhouse"}
        kept, stats = adopt.filter_incoming(self.b, "new-acct", [second])
        self.assertEqual(kept, [second])
        self.assertFalse(stats.get("carried_plaid_ids"))
        self.assertIn("live-coffee", self.held())

    def test_a_holds_settlement_is_kept_and_the_hold_left_to_retire(self):
        posted = {"id": "plaid-posted", "date": D(2026, 5, 30),
                  "amount": 30.0, "name": "Fuel Stop",
                  "merchant_name": "Fuel Stop"}
        kept, _ = adopt.filter_incoming(self.b, "new-acct", [posted])
        self.assertEqual(kept, [posted])
        # the hold still wears its own id, so the bank's removal of it
        # retires the hold and nothing else
        self.assertIn("live-hold", self.held())


class NothingOlderThanTheLinkLeavesNothingToGuard(_Merged):
    archive_rows = [("old-last", D(2026, 6, 1), 9.99, "STREAMCO")]

    def live_rows(self):
        _txn(self.b, "live-first", "new-acct", D(2026, 5, 20), 42.5,
             "Grocerymart")

    def test_the_adoption_is_recorded_reconciled(self):
        self.assertIsNone(adopt.pending(self.b, "new-acct"))
        self.assertIsNotNone(self.b.execute(
            "SELECT reconciled_at FROM account_adoptions"
            " WHERE account_id = 'new-acct'").fetchone()["reconciled_at"])


class ADailyChargePairsDayForDay(_Merged):
    archive_rows = [(f"old-{d:02d}", D(2026, 5, d), 4.0, "BEANHOUSE 0042")
                    for d in range(1, 11)]
    update_status = "HISTORICAL_UPDATE_COMPLETE"

    def live_rows(self):
        for d in range(5, 13):
            _txn(self.b, f"live-{d:02d}", "new-acct", D(2026, 5, d), 4.0,
                 "Beanhouse")

    def test_each_morning_pairs_with_its_own_morning(self):
        held = self.held()
        # every live row, plus the restored mornings before the link began
        self.assertEqual(
            set(held), {f"live-{d:02d}" for d in range(5, 13)}
            | {f"old-{d:02d}" for d in range(1, 5)})
        notes = {r["txn_id"]: r["note"] for r in self.b.execute(
            "SELECT txn_id, note FROM transaction_notes").fetchall()}
        for d in range(5, 11):
            self.assertEqual(notes.get(f"live-{d:02d}"), f"note on old-{d:02d}")
        self.assertNotIn("live-11", notes)


class NothingFromTheLinksFirstDayOnIsMatched(_Merged):
    """The guard's matcher reaches a few days either side of a row, so a
    seam that ran on past the cutoff would test the link's own first days
    against the restored days just before them: a hold's settlement posted
    the next day is dropped as a copy of yesterday's restored coffee (and
    the bank then retires the hold, so the charge is nowhere), and a fresh
    coffee two days in is dropped with its bank id written onto a restored
    row. A merge ends the seam at the day before the live lineage began."""
    archive_rows = ([("old-rent", D(2024, 3, 1), 500.0, "LANDLORD RENT")]
                    + [(f"old-{d}", D(2026, 5, d), 4.0, "BEANHOUSE 0042")
                       for d in (25, 26, 27)])

    def live_rows(self):
        _txn(self.b, "live-hold", "new-acct", D(2026, 5, 28), 4.0,
             "Beanhouse")
        self.b.execute("UPDATE transactions SET pending = 1"
                       " WHERE id = 'live-hold'")

    def test_the_settlement_the_day_after_the_link_began_is_kept(self):
        posted = {"id": "plaid-posted", "date": D(2026, 5, 29), "amount": 4.0,
                  "name": "Beanhouse", "merchant_name": "Beanhouse"}
        kept, stats = adopt.filter_incoming(self.b, "new-acct", [posted])
        self.assertEqual(kept, [posted])
        self.assertFalse(stats.get("deduped_overlap"))

    def test_a_coffee_two_days_in_keeps_its_own_id(self):
        replay = {"id": "plaid-27", "date": D(2026, 5, 27), "amount": 4.0,
                  "name": "Beanhouse", "merchant_name": "Beanhouse"}
        kept, _ = adopt.filter_incoming(self.b, "new-acct", [replay])
        self.assertEqual(kept, [])          # a restored day: still guarded
        coffee = {"id": "plaid-new-30", "date": D(2026, 5, 30), "amount": 4.0,
                  "name": "Beanhouse", "merchant_name": "Beanhouse"}
        kept, stats = adopt.filter_incoming(self.b, "new-acct", [coffee])
        self.assertEqual(kept, [coffee])
        self.assertFalse(stats.get("carried_plaid_ids"))
        self.assertNotIn("plaid-new-30", self.held())
        self.assertEqual({"old-25", "old-26"} & set(self.held()),
                         {"old-25", "old-26"})


class ARestoredChargePairsWithThePostedTwinNotTheHold(_Merged):
    """On a day the live lineage holds both a bank hold and the posted
    charge it became, the restored copy of that charge pairs with the
    posted row. Paired with the hold, everything a person put on it would
    follow the hold, which the bank then removes."""
    archive_rows = [("old-rent", D(2024, 3, 1), 500.0, "LANDLORD RENT"),
                    ("old-coffee", D(2026, 5, 28), 4.0, "BEANHOUSE 0042")]
    update_status = "HISTORICAL_UPDATE_COMPLETE"

    def live_rows(self):
        _txn(self.b, "live-a-hold", "new-acct", D(2026, 5, 28), 4.0,
             "Beanhouse")
        _txn(self.b, "live-b-posted", "new-acct", D(2026, 5, 28), 4.0,
             "Beanhouse")
        self.b.execute("UPDATE transactions SET pending = 1"
                       " WHERE id = 'live-a-hold'")

    def test_the_note_lands_on_the_posted_row(self):
        notes = {r["txn_id"]: r["note"] for r in self.b.execute(
            "SELECT txn_id, note FROM transaction_notes").fetchall()}
        self.assertEqual(notes.get("live-b-posted"), "note on old-coffee")
        self.assertNotIn("live-a-hold", notes)
        self.assertNotIn("old-coffee", self.held())



class NothingPastTheRestoredHistoryIsMatched(_Merged):
    """Both lineages are the same bank's Plaid feed, which dates a posted
    charge the same way whichever link delivers it, and the restore's own
    holds are retired at the merge. So a row the replay delivers for a day
    after the newest restored one is a charge the backup never held — the
    settlement of a hold it was waiting on, or the next morning's coffee.
    Matched against the restored days just before it, it is dropped and
    its bank id written onto a restored row; when that row's own
    re-delivery arrives on a later page it is claimed a second time, and
    two charges end as one row."""
    archive_rows = [("old-rent", D(2024, 3, 1), 500.0, "LANDLORD RENT"),
                    ("old-coffee", D(2026, 5, 20), 4.0, "BEANHOUSE 0042"),
                    ("old-hold", D(2026, 5, 21), 4.0, "BEANHOUSE 0042")]
    archive_pending = ("old-hold",)

    def live_rows(self):
        _txn(self.b, "live-first", "new-acct", D(2026, 5, 28), 42.5,
             "Grocerymart")

    def _land(self, page):
        kept, _ = adopt.filter_incoming(self.b, "new-acct", page)
        for t in kept:
            _txn(self.b, t["id"], "new-acct", t["date"], t["amount"],
                 t["name"])

    def test_the_holds_settlement_and_the_replay_are_two_charges(self):
        self.assertIsNotNone(adopt.pending(self.b, "new-acct"))
        self._land([{"id": "plaid-settled", "date": D(2026, 5, 22),
                     "amount": 4.0, "name": "Beanhouse",
                     "merchant_name": "Beanhouse"}])
        self._land([{"id": "plaid-coffee", "date": D(2026, 5, 20),
                     "amount": 4.0, "name": "Beanhouse",
                     "merchant_name": "Beanhouse"}])
        coffees = [r for r in self.held().values() if r["amount"] == 4.0]
        self.assertEqual(len(coffees), 2)
        self.assertEqual(sorted(r["date"] for r in coffees),
                         [D(2026, 5, 20), D(2026, 5, 22)])


class AHoldsSettlementIsNotAnOlderChargesCopy(_Merged):
    """A restored hold is seldom the newest restored row: charges keep
    posting while it waits. Its settlement then falls inside the restored
    span, where the matcher would take it for a re-delivery of an earlier
    same-price charge and put its bank id on that row — and the earlier
    charge's own re-delivery, on a later page, would be claimed against
    the same row again. The settlement is recognized as the retired
    hold's, and kept."""
    archive_rows = [("old-rent", D(2024, 3, 1), 500.0, "LANDLORD RENT"),
                    ("old-coffee", D(2026, 5, 17), 4.0, "BEANHOUSE 0042"),
                    ("old-hold", D(2026, 5, 18), 4.0, "BEANHOUSE 0042"),
                    ("old-gas", D(2026, 5, 21), 31.0, "FUEL STOP")]
    archive_pending = ("old-hold",)

    def live_rows(self):
        _txn(self.b, "live-first", "new-acct", D(2026, 5, 28), 42.5,
             "Grocerymart")

    def _land(self, page):
        kept, _ = adopt.filter_incoming(self.b, "new-acct", page)
        for t in kept:
            _txn(self.b, t["id"], "new-acct", t["date"], t["amount"],
                 t["name"])

    def test_the_settlement_and_the_older_charge_are_two_rows(self):
        self.assertIsNotNone(adopt.pending(self.b, "new-acct"))
        self._land([{"id": "plaid-settled", "date": D(2026, 5, 19),
                     "amount": 4.0, "name": "Beanhouse",
                     "merchant_name": "Beanhouse"}])
        self._land([{"id": "plaid-coffee", "date": D(2026, 5, 17),
                     "amount": 4.0, "name": "Beanhouse",
                     "merchant_name": "Beanhouse"}])
        coffees = {i: r["date"] for i, r in self.held().items()
                   if r["amount"] == 4.0}
        self.assertEqual(coffees, {"plaid-settled": D(2026, 5, 19),
                                   "plaid-coffee": D(2026, 5, 17)})
        notes = {r["txn_id"]: r["note"] for r in self.b.execute(
            "SELECT txn_id, note FROM transaction_notes").fetchall()}
        # what a person wrote on the older charge stays on that charge
        self.assertEqual(notes.get("plaid-coffee"), "note on old-coffee")
        self.assertNotIn("plaid-settled", notes)


class ARestoredRowAbsorbsOneIncomingChargeAcrossPages(_Merged):
    """Once an incoming id has been carried onto a restored row, the row
    wears the bank's id, and only that id's own re-delivery can be it —
    which the guard passes through as a correction. A later page's
    same-price charge matched against it would be dropped as a second copy
    of a row that already stands for one charge."""
    archive_rows = [("old-rent", D(2024, 3, 1), 500.0, "LANDLORD RENT"),
                    ("old-coffee", D(2026, 5, 17), 4.0, "BEANHOUSE 0042"),
                    ("old-gas", D(2026, 5, 21), 31.0, "FUEL STOP")]

    def live_rows(self):
        _txn(self.b, "live-first", "new-acct", D(2026, 5, 28), 42.5,
             "Grocerymart")

    def _land(self, page):
        kept, stats = adopt.filter_incoming(self.b, "new-acct", page)
        for t in kept:
            _txn(self.b, t["id"], "new-acct", t["date"], t["amount"],
                 t["name"])
        return kept, stats

    def test_the_second_page_charge_is_kept(self):
        # a charge the backup never saw, posted under an earlier date
        kept, stats = self._land([{"id": "plaid-late", "date": D(2026, 5, 18),
                                   "amount": 4.0, "name": "Beanhouse",
                                   "merchant_name": "Beanhouse"}])
        self.assertEqual(kept, [])
        self.assertEqual(stats.get("carried_plaid_ids"), 1)
        page = [{"id": "plaid-coffee", "date": D(2026, 5, 17), "amount": 4.0,
                 "name": "Beanhouse", "merchant_name": "Beanhouse"}]
        kept, stats = self._land(page)
        self.assertEqual(kept, page)
        self.assertFalse(stats.get("carried_plaid_ids"))
        coffees = {i for i, r in self.held().items() if r["amount"] == 4.0}
        # the carried id is not stripped off the row Plaid now addresses
        self.assertEqual(coffees, {"plaid-late", "plaid-coffee"})


def _coffee(tid, day):
    return {"id": tid, "date": day, "amount": 4.0, "name": "Beanhouse",
            "merchant_name": "Beanhouse"}


class _DailyCoffeeAndARetiredHold(_Merged):
    """A coffee every morning, and one morning's hold still pending when
    the backup was taken. Every restored morning looks alike, so no
    incoming copy can be pinned to one of them and no bank id is carried
    across — the guard only drops the copy. What it dropped it against has
    to be remembered for the next page all the same: each page builds a
    fresh matcher, which would otherwise offer the same restored morning
    again, and the settlement of the hold would go as a second copy of it."""
    archive_rows = [("old-rent", D(2024, 3, 1), 500.0, "LANDLORD RENT"),
                    ("old-15", D(2026, 5, 15), 4.0, "BEANHOUSE 0042"),
                    ("old-16", D(2026, 5, 16), 4.0, "BEANHOUSE 0042"),
                    ("old-hold", D(2026, 5, 16), 4.0, "BEANHOUSE 0042"),
                    ("old-17", D(2026, 5, 17), 4.0, "BEANHOUSE 0042"),
                    ("old-gas", D(2026, 5, 21), 31.0, "FUEL STOP")]
    archive_pending = ("old-hold",)

    def live_rows(self):
        _txn(self.b, "live-first", "new-acct", D(2026, 5, 28), 42.5,
             "Grocerymart")

    def land(self, page):
        kept, stats = adopt.filter_incoming(self.b, "new-acct", page)
        for t in kept:
            _txn(self.b, t["id"], "new-acct", t["date"], t["amount"],
                 t["name"])
        return kept, stats

    def coffees(self):
        return sorted(r["date"] for r in self.held().values()
                      if r["amount"] == 4.0)

    # three restored mornings plus the hold's posted charge
    every_charge = [D(2026, 5, 15), D(2026, 5, 16), D(2026, 5, 17),
                    D(2026, 5, 17)]


class TheSettlementOnItsOwnPageFirst(_DailyCoffeeAndARetiredHold):

    def test_four_charges_stay_four(self):
        self.land([_coffee("p-settle", D(2026, 5, 17))])
        self.land([_coffee("p-17", D(2026, 5, 17))])
        self.land([_coffee("p-15", D(2026, 5, 15)),
                   _coffee("p-16", D(2026, 5, 16))])
        self.assertEqual(self.coffees(), self.every_charge)


class TheRestoredMorningOnItsOwnPageFirst(_DailyCoffeeAndARetiredHold):

    def test_four_charges_stay_four(self):
        self.land([_coffee("p-17", D(2026, 5, 17))])
        self.land([_coffee("p-settle", D(2026, 5, 17))])
        self.land([_coffee("p-15", D(2026, 5, 15)),
                   _coffee("p-16", D(2026, 5, 16))])
        self.assertEqual(self.coffees(), self.every_charge)

    def test_a_correction_to_a_dropped_copy_is_dropped_again(self):
        # Plaid's `modified` re-delivers a row under the id it first gave
        # it. The guard dropped that id as a copy; matched afresh it could
        # take a different restored morning, or the hold's settlement.
        self.land([_coffee("p-17", D(2026, 5, 17))])
        kept, stats = self.land([_coffee("p-17", D(2026, 5, 17))])
        self.assertEqual(kept, [])
        self.assertEqual(stats.get("deduped_overlap"), 1)
        self.land([_coffee("p-settle", D(2026, 5, 17))])
        self.land([_coffee("p-15", D(2026, 5, 15)),
                   _coffee("p-16", D(2026, 5, 16))])
        self.assertEqual(self.coffees(), self.every_charge)


class AllOnOnePage(_DailyCoffeeAndARetiredHold):

    def test_four_charges_stay_four(self):
        self.land([_coffee("p-settle", D(2026, 5, 17)),
                   _coffee("p-17", D(2026, 5, 17)),
                   _coffee("p-15", D(2026, 5, 15)),
                   _coffee("p-16", D(2026, 5, 16))])
        self.assertEqual(self.coffees(), self.every_charge)


class ACorrectionToACarriedRowKeepsItSpokenFor(
        ARestoredRowAbsorbsOneIncomingChargeAcrossPages):
    """The bank's `modified` restates a row's whole raw record, so what the
    guard knows about a row it carried an id onto cannot live there: once
    restated, the row would be back in the matcher's pool and absorb the
    next same-price charge."""

    def test_the_second_page_charge_is_kept(self):
        self._land([{"id": "plaid-late", "date": D(2026, 5, 18),
                     "amount": 4.0, "name": "Beanhouse",
                     "merchant_name": "Beanhouse"}])
        # what the upsert of a `modified` leaves: the feed's raw, nothing
        # of ours
        self.b.execute("UPDATE transactions SET raw = '{}'::jsonb"
                       " WHERE id = 'plaid-late'")
        page = [{"id": "plaid-coffee", "date": D(2026, 5, 17), "amount": 4.0,
                 "name": "Beanhouse", "merchant_name": "Beanhouse"}]
        kept, _ = self._land(page)
        self.assertEqual(kept, page)


class WhatAnEarlierGuardNotedDoesNotComeBack(_Merged):
    """An older guard kept its notes in the row's raw record, which an
    export carries. Whatever an archive brings back there, a restored row
    still matches its own re-delivery — otherwise the charge would be held
    twice."""
    archive_rows = [("old-rent", D(2024, 3, 1), 500.0, "LANDLORD RENT"),
                    ("old-coffee", D(2026, 5, 17), 4.0, "BEANHOUSE 0042")]
    archive_carried = ("old-coffee",)

    def live_rows(self):
        _txn(self.b, "live-first", "new-acct", D(2026, 5, 28), 42.5,
             "Grocerymart")

    def test_the_restored_row_still_absorbs_its_re_delivery(self):
        kept, stats = adopt.filter_incoming(self.b, "new-acct", [
            {"id": "plaid-coffee", "date": D(2026, 5, 17), "amount": 4.0,
             "name": "Beanhouse", "merchant_name": "Beanhouse"}])
        self.assertEqual(kept, [])
        self.assertEqual(stats.get("carried_plaid_ids"), 1)


if __name__ == "__main__":
    unittest.main()
