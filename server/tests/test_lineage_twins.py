"""One transaction must not sit on an account twice under two ids.

A restore followed by a fresh link can leave the restored history and the
link's own backfill side by side on the same account — the raw bank
descriptor under one id, Plaid's cleaned name under another — whenever the
two reached the account without crossing the incoming guard. The twin
dedup retires the second lineage: posted beats pending, the cleaner name
beats the descriptor, user state survives on whichever row stays, and two
genuinely different same-day charges are left alone."""
import datetime as dt
import unittest

from oikonome.sync import adopt
from .util import make_db


def _item(conn, iid):
    conn.execute("INSERT INTO items (id, aggregator, institution_id, "
                 "institution_name, status, access_token) VALUES "
                 "(%s,'plaid','ins_9','Credit Union','ok','tok')", (iid,))


def _account(conn, aid, iid):
    conn.execute("INSERT INTO accounts (id, item_id, name, type, mask) "
                 "VALUES (%s,%s,'Business Checking','depository','1111')",
                 (aid, iid))


def _txn(conn, tid, aid, date, amount, name, *, merchant=None, pending=0,
         override=None):
    conn.execute(
        "INSERT INTO transactions (id, account_id, date, amount, name, "
        "merchant_name, pending, removed, category_override) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,0,%s)",
        (tid, aid, date, amount, name, merchant, pending, override))


class LineageTwinTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        _item(self.conn, "cu-item")
        _account(self.conn, "biz", "cu-item")
        self.d = dt.date(2026, 8, 14)

    def _live(self):
        return {r["id"]: dict(r) for r in self.conn.execute(
            "SELECT id, removed, category_override FROM transactions "
            "WHERE account_id='biz'").fetchall()}

    def test_raw_descriptor_twin_is_retired_and_override_carried(self):
        _txn(self.conn, "raw", "biz", self.d, 20.00,
             "REF000000000042 NORTHWIND HOSTING.COM NORTHWINDHOST.US",
             merchant="Northwindhost.us", override="SOFTWARE")
        _txn(self.conn, "clean", "biz", self.d, 20.00, "Northwind Hosting",
             merchant="Northwind Hosting")
        out = adopt.dedupe_twins(self.conn, "biz")
        self.assertEqual([(o["kept"], o["removed"]) for o in out],
                         [("clean", "raw")])
        live = self._live()
        self.assertEqual(live["raw"]["removed"], 1)
        self.assertEqual(live["clean"]["removed"], 0)
        # the user's category moved with the transaction, not with the row
        self.assertEqual(live["clean"]["category_override"], "SOFTWARE")
        # idempotent: nothing left to retire
        self.assertEqual(adopt.dedupe_twins(self.conn, "biz"), [])

    def test_posted_charge_beats_its_stale_card_hold(self):
        _txn(self.conn, "hold", "biz", self.d, 10.00,
             "Card Hold; 000000000000001 NORTHWIND*STREAMING", pending=1)
        _txn(self.conn, "posted", "biz", self.d, 10.00,
             "XX1111 Northwind*Streaming Northwindstream.Com")
        out = adopt.dedupe_twins(self.conn, "biz")
        self.assertEqual([(o["kept"], o["removed"]) for o in out],
                         [("posted", "hold")])

    def test_two_different_merchants_same_day_same_amount_both_stay(self):
        _txn(self.conn, "coffee", "biz", self.d, 4.50, "NORTHWIND COFFEE 1234",
             merchant="Northwind Coffee")
        _txn(self.conn, "fuel", "biz", self.d, 4.50, "KESTREL FUEL 5678",
             merchant="Kestrel Fuel")
        self.assertEqual(adopt.dedupe_twins(self.conn, "biz"), [])
        self.assertEqual({v["removed"] for v in self._live().values()}, {0})

    def test_dry_run_reports_without_touching_rows(self):
        _txn(self.conn, "a", "biz", self.d, 99.0, "Fabrikam",
             merchant="Fabrikam")
        _txn(self.conn, "b", "biz", self.d, 99.0,
             "000000000000042 FABRIKAM.COM/US")
        out = adopt.dedupe_twins(self.conn, "biz", apply=False)
        self.assertEqual(len(out), 1)
        self.assertEqual({v["removed"] for v in self._live().values()}, {0})

    def test_twins_left_by_a_link_that_synced_first_are_retired_on_request(self):
        # the link synced first (new id already holds the charge), then the
        # restore's copy of the same charge is adopted onto that id
        self.conn.execute("INSERT INTO items (id, aggregator, institution_id, "
                          "institution_name, status, access_token) VALUES "
                          "('shell','plaid','ins_9','Credit Union','restored','')")
        _account(self.conn, "old-biz", "shell")
        _txn(self.conn, "restored", "old-biz", self.d, 20.00,
             "REF000000000042 NORTHWIND HOSTING.COM")
        # the restored copy is adopted onto the new Plaid id, the link's own
        # copy arrives beside it; the operator tool retires the second one
        adopt.adopt_accounts(self.conn, item_id="cu-item", institution_id="ins_9",
                             institution_name="Credit Union",
                             incoming=[{"account_id": "biz2", "mask": "1111",
                                        "type": "depository"}])
        _txn(self.conn, "fresh", "biz2", self.d, 20.00, "Northwind Hosting",
             merchant="Northwind Hosting")
        out = adopt.dedupe_twins(self.conn, "biz2")
        self.assertEqual([(o["kept"], o["removed"]) for o in out],
                         [("fresh", "restored")])

    def _note(self, tid, text):
        self.conn.execute(
            "INSERT INTO transaction_notes (txn_id, note) VALUES (%s,%s)",
            (tid, text))

    def _notes(self):
        return {r["txn_id"]: r["note"] for r in self.conn.execute(
            "SELECT txn_id, note FROM transaction_notes").fetchall()}

    def test_losing_note_moves_when_survivor_has_none(self):
        _txn(self.conn, "raw", "biz", self.d, 20.00,
             "REF000042 NORTHWINDHOST.US", merchant="Northwindhost.us")
        _txn(self.conn, "clean", "biz", self.d, 20.00, "Northwind Hosting",
             merchant="Northwind Hosting")
        self._note("raw", "annual plan")
        adopt.dedupe_twins(self.conn, "biz")
        notes = self._notes()
        # the note followed the transaction to the survivor, not the row id
        self.assertEqual(notes, {"clean": "annual plan"})

    def test_both_notes_are_kept_when_survivor_already_has_one(self):
        """A dedup must never silently drop a user's written words. When
        both twins carry a note, the retired one's text is appended to the
        survivor's rather than stranded on a removed=1 row every read hides."""
        _txn(self.conn, "raw", "biz", self.d, 20.00,
             "REF000042 NORTHWINDHOST.US", merchant="Northwindhost.us")
        _txn(self.conn, "clean", "biz", self.d, 20.00, "Northwind Hosting",
             merchant="Northwind Hosting")
        self._note("clean", "survivor note")
        self._note("raw", "loser note")
        adopt.dedupe_twins(self.conn, "biz")
        notes = self._notes()
        self.assertEqual(set(notes), {"clean"})          # one row, survivor's
        self.assertIn("survivor note", notes["clean"])
        self.assertIn("loser note", notes["clean"])      # nothing lost


if __name__ == "__main__":
    unittest.main()
