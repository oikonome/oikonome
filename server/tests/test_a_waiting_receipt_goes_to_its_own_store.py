"""A waiting receipt goes to the charge from its own store, on the
household's own calendar, and a person's pairing stays theirs.

The matcher's rules, seen from the cases that used to go wrong: an
exact-cents charge at another business must not beat the receipt's own
store off by a tip; a same-store charge a little off the total must not be
taken while the receipt's exact charge can still arrive (the same coffee
every morning always has yesterday's charge sitting in the band); the
window closes on the household's day, not the server's; and a hand match
whose charge vanishes waits for the person again instead of being
re-decided by the rules.
"""
import datetime as dt
import unittest
import zoneinfo

from oikonome import localtime
from oikonome.engine import budget, receipt_match, receipts
from oikonome.sync import base as sync_base

from .util import add_txn, make_db, write_config

PNG = b"\x89PNG-own-store"
TODAY = dt.date.today()


def _waiting(conn, merchant, total, date):
    """A waiting receipt with its total, date and store already read."""
    rid = receipts.add(conn, None, PNG, "image/png")
    conn.execute(
        "UPDATE receipts SET status = 'parsed', parsed = %s::jsonb"
        " WHERE id = %s",
        ('{"merchant": "%s", "total": %s, "date": "%s"}'
         % (merchant, total, date.isoformat()), rid))
    return rid


def _attached(conn, rid):
    r = conn.execute("SELECT txn_id, match_method FROM receipts"
                     " WHERE id = %s", (rid,)).fetchone()
    return r["txn_id"], r["match_method"]


def _c(tid, exact=True, merchant="unknown", gap=1):
    return {"txn_id": tid, "exact": exact, "merchant_match": merchant,
            "day_gap": gap}


class TheRulesTests(unittest.TestCase):
    """decide() on its own, without a database."""

    def test_the_own_store_off_by_a_tip_beats_a_stranger_at_the_exact_price(self):
        d = receipt_match.decide
        cands = [_c("shell", merchant="different"),
                 _c("bistro", exact=False, merchant="same")]
        self.assertEqual(d(cands), ("bistro", "near+merchant"))
        # while the window is open, neither is taken yet
        self.assertEqual(d(cands, closed=False), (None, "early"))
        # two same-store near charges beside the stranger: a person picks
        self.assertEqual(d(cands + [_c("bistro2", exact=False,
                                       merchant="same", gap=2)]),
                         (None, "tie"))
        # with no same-store charge at all the stranger still matches once
        # the window has closed
        self.assertEqual(d([_c("shell", merchant="different")]),
                         ("shell", "exact"))

    def test_two_strangers_at_the_exact_price_do_not_hide_the_own_store(self):
        d = receipt_match.decide
        cands = [_c("shell", merchant="different"),
                 _c("target", merchant="different"),
                 _c("starbucks", exact=False, merchant="same")]
        self.assertEqual(d(cands), ("starbucks", "near+merchant"))
        self.assertEqual(d(cands, closed=False), (None, "early"))
        # with no own-store charge the strangers are still a tie
        self.assertEqual(d(cands[:2]), (None, "tie"))

    def test_the_offer_puts_the_charge_the_rules_would_take_first(self):
        # the rules' pick must be inside the few charges a person is shown
        cands = [_c(f"stranger{i}", merchant="different") for i in range(4)]
        cands.append(_c("own", exact=False, merchant="same"))
        cands.append(_c("exactown", merchant="unknown"))
        for c in cands:
            c["amount"] = 5.0
        cands.sort(key=receipt_match._precedence)
        self.assertEqual([c["txn_id"] for c in cands[:2]],
                         ["exactown", "own"])

    def test_a_near_same_store_charge_waits_while_the_window_is_open(self):
        d = receipt_match.decide
        near = [_c("yesterday", exact=False, merchant="same", gap=-1)]
        self.assertEqual(d(near, closed=False), (None, "early"))
        self.assertEqual(d(near, closed=True),
                         ("yesterday", "near+merchant"))
        # an exact same-store charge is still taken at once
        self.assertEqual(d([_c("own", merchant="same")], closed=False),
                         ("own", "exact"))


class OwnStoreTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_the_tipped_charge_at_the_bistro_takes_the_receipt_not_the_gas_station(self):
        day = TODAY - dt.timedelta(days=receipt_match.AUTO_DAYS_AFTER + 3)
        rid = _waiting(self.conn, "Corner Bistro", 40.00, day)
        add_txn(self.conn, day + dt.timedelta(days=1), 48.00,
                "CORNER BISTRO", txn_id="bistro")
        add_txn(self.conn, day + dt.timedelta(days=1), 40.00,
                "SHELL OIL 5741", txn_id="shell")
        receipt_match.match_waiting(self.conn)
        self.assertEqual(_attached(self.conn, rid), ("bistro", "auto"))

    def test_the_tipped_coffee_beats_two_strangers_at_the_printed_price(self):
        day = TODAY - dt.timedelta(days=receipt_match.AUTO_DAYS_AFTER + 3)
        rid = _waiting(self.conn, "Starbucks", 5.00, day)
        for tid, name in (("shell", "SHELL OIL 5741"),
                          ("target", "TARGET T-1234")):
            add_txn(self.conn, day + dt.timedelta(days=1), 5.00, name,
                    txn_id=tid)
        add_txn(self.conn, day + dt.timedelta(days=1), 5.75,
                "STARBUCKS STORE 6161", txn_id="sbux")
        offered = [w for w in receipt_match.waiting(self.conn)["waiting"]
                   if w["id"] == str(rid)][0]["candidates"]
        self.assertEqual(offered[0]["txn_id"], "sbux")
        receipt_match.match_waiting(self.conn)
        self.assertEqual(_attached(self.conn, rid), ("sbux", "auto"))

    def test_the_daily_coffee_waits_for_its_own_charge(self):
        rid = _waiting(self.conn, "Harbor Coffee", 4.75, TODAY)
        # yesterday's visit, a different drink, is already in the ledger
        add_txn(self.conn, TODAY - dt.timedelta(days=1), 5.25,
                "HARBOR COFFEE", txn_id="yesterday")
        receipt_match.match_waiting(self.conn)
        self.assertEqual(_attached(self.conn, rid), (None, None))
        # today's own charge posts, and is the one taken
        sync_base.upsert_transactions(self.conn, [sync_base.Transaction(
            id="today", account_id="card",
            date=TODAY + dt.timedelta(days=1), amount=4.75,
            name="HARBOR COFFEE", pending=False, raw={})])
        self.assertEqual(_attached(self.conn, rid), ("today", "auto"))


class HouseholdDayTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_the_window_closes_on_the_households_day_not_the_servers(self):
        # a zone whose date differs from this process's right now: of two
        # zones 26 hours apart, at least one always does
        for name in ("Pacific/Kiritimati", "Etc/GMT+12"):
            here = dt.datetime.now(zoneinfo.ZoneInfo(name)).date()
            if here != dt.date.today():
                break
        cfg = budget.load_config(self.conn)
        cfg["timezone"] = name
        budget.save_config(self.conn, cfg)
        household = localtime.household_day(self.conn)
        self.assertNotEqual(household, dt.date.today())
        ahead = household > dt.date.today()
        # closed on the household's day exactly when the household is ahead
        # of the server; open on it when the household is behind
        receipt_day = household - dt.timedelta(
            days=receipt_match.AUTO_DAYS_AFTER + (1 if ahead else 0))
        rid = _waiting(self.conn, "Maple Leaf Bistro", 42.17, receipt_day)
        add_txn(self.conn, receipt_day + dt.timedelta(days=1), 42.17,
                "TST* NORTHWIND HOSPITALITY", txn_id="legal-name")
        receipt_match.match_waiting(self.conn)
        want = ("legal-name", "auto") if ahead else (None, None)
        self.assertEqual(_attached(self.conn, rid), want)
        # the waiting list and a single-receipt pass agree with the sweep
        if not ahead:
            self.assertIsNone(receipt_match.match_receipt(self.conn, rid))
            listed = receipt_match.waiting(self.conn)["waiting"]
            self.assertEqual([w["id"] for w in listed], [rid])


class AHandMatchStaysTheirsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_hand_match_whose_charge_goes_waits_for_the_person(self):
        day = TODAY - dt.timedelta(days=1)
        rid = _waiting(self.conn, "Quill and Ink", 23.00, day)
        add_txn(self.conn, day, 23.00, "QUILL AND INK", txn_id="hold",
                pending=1)
        self.assertTrue(receipt_match.match_by_hand(self.conn, rid, "hold"))
        # the hold vanishes with no successor; a later sync brings a charge
        # the rules would happily take on their own
        sync_base.mark_removed(self.conn, ["hold"])
        sync_base.upsert_transactions(self.conn, [sync_base.Transaction(
            id="posted", account_id="card", date=day, amount=23.00,
            name="QUILL AND INK", pending=False, raw={})])
        self.assertEqual(_attached(self.conn, rid), (None, "manual"))
        # later passes and a re-parse's match leave it alone too
        receipt_match.match_waiting(self.conn)
        self.assertIsNone(receipt_match.match_receipt(self.conn, rid))
        self.assertEqual(_attached(self.conn, rid), (None, "manual"))
        listed = receipt_match.waiting(self.conn)["waiting"]
        self.assertEqual(len(listed), 1)
        self.assertTrue(listed[0]["released"])
        self.assertEqual([c["txn_id"] for c in listed[0]["candidates"]],
                         ["posted"])
        # and the person's next choice is theirs again
        self.assertTrue(receipt_match.match_by_hand(self.conn, rid, "posted"))
        self.assertEqual(_attached(self.conn, rid), ("posted", "manual"))

    def test_an_automatic_match_whose_charge_goes_is_matched_again(self):
        day = TODAY - dt.timedelta(days=1)
        rid = _waiting(self.conn, "Quill and Ink", 23.00, day)
        sync_base.upsert_transactions(self.conn, [sync_base.Transaction(
            id="hold", account_id="card", date=day, amount=23.00,
            name="QUILL AND INK", pending=True, raw={})])
        self.assertEqual(_attached(self.conn, rid), ("hold", "auto"))
        sync_base.mark_removed(self.conn, ["hold"])
        sync_base.upsert_transactions(self.conn, [sync_base.Transaction(
            id="posted", account_id="card", date=day, amount=23.00,
            name="QUILL AND INK", pending=False, raw={})])
        self.assertEqual(_attached(self.conn, rid), ("posted", "auto"))


if __name__ == "__main__":
    unittest.main()
