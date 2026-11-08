"""A restore makes one bill of two only when the household already held
the archived bill under its cut name, and counts a stored-name row only
when it is true of the account it names.

A detected bill whose payee spells a card number lands under the cut
name. Two cards can share a last four, so two archived bills can cut to
the same name; and a person can have typed that name into a bill of
their own. Neither is the archived bill: the second detected bill and the
hand-made one stay bills of their own, and what names each (a pin) keeps
naming it. Only a detected bill that was in the household before the
restore, which an upgrade moved to the cut id, is the same bill.

An account's stored-name row (c-row) counts only while it is the digest
of the cut name its account stores, and whitespace of any kind between
the groups of a number is still a number: it is cut, and no digest is
ever taken over the whole of it.

Every number here is invented (test-card shapes).
"""

import hashlib
import json
import unittest

from oikonome.sync import account_sources, restore

from .test_restore_cuts_numbers_whatever_the_archive_says_of_itself import (
    _zip)
from .util import make_db


def _bill(number: str, amount: int) -> dict:
    return {"id": f"rec:invented-{number}", "type": "bill",
            "payee": f"Invented {number}", "amount": amount,
            "frequency": "MONTHLY", "active": 1, "raw": "{}"}


def _digest(text: str) -> str:
    return "c:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


class TwoBillsStayTwo(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def _bills(self):
        return sorted((r["id"], r["payee"], float(r["amount"])) for r in
                      self.conn.execute(
                          "SELECT id, payee, amount FROM bills").fetchall())

    def test_two_cards_sharing_a_last_four_restore_as_two_bills(self):
        for flip in (False, True):
            with self.subTest(flip=flip):
                self.conn = make_db()
                self.addCleanup(self.conn.close)
                self._two_whole_cards(flip)

    def _two_whole_cards(self, flip: bool):
        two = [_bill("4000000000000002", 25), _bill("4111111111110002", 70)]
        data = _zip({
            "bills.csv": two[::-1] if flip else two,
            "transactions.csv": [
                {"id": "t2", "account_id": "chk", "date": "2026-07-02",
                 "amount": 70, "name": "INVENTED CARD"}],
            "manual_categories.csv": [
                {"transaction_id": "t2", "category": "BILLS",
                 "bill_id": "rec:invented-4111111111110002",
                 "set_at": "2026-07-02T00:00:00+00:00"}]})
        counts = restore.restore_zip(self.conn, data)
        bills = self._bills()
        self.assertEqual([b[2] for b in bills], [25.0, 70.0], bills)
        self.assertEqual(counts.get("already_present", 0), 0)
        # the pin stays on the $70 bill
        pin = self.conn.execute(
            "SELECT b.amount FROM manual_categories c"
            "  JOIN bills b ON b.id = c.bill_id").fetchone()
        self.assertEqual(float(pin["amount"]), 70.0)

    def _restore_cut_and_uncut(self, order: str) -> dict:
        # what the upgrade leaves for two cards sharing a last four: the
        # first under the cut id, the second whole because the cut id was
        # taken. The archive may list them either way round.
        cut = {"id": "rec:invented-0002", "type": "bill",
               "payee": "Invented …0002", "amount": 25,
               "frequency": "MONTHLY", "active": 1, "raw": "{}"}
        whole = _bill("4111111111110002", 70)
        rows = [cut, whole] if order == "cut first" else [whole, cut]
        return restore.restore_zip(self.conn, _zip({
            "bills.csv": rows,
            "transactions.csv": [
                {"id": "t1", "account_id": "chk", "date": "2026-07-01",
                 "amount": 25, "name": "INVENTED CARD ONE"},
                {"id": "t2", "account_id": "chk", "date": "2026-07-02",
                 "amount": 70, "name": "INVENTED CARD TWO"}],
            "manual_categories.csv": [
                {"transaction_id": "t1", "category": "BILLS",
                 "bill_id": "rec:invented-0002",
                 "set_at": "2026-07-01T00:00:00+00:00"},
                {"transaction_id": "t2", "category": "BILLS",
                 "bill_id": "rec:invented-4111111111110002",
                 "set_at": "2026-07-02T00:00:00+00:00"}],
            "tenant_settings.csv": [{"config": json.dumps({
                "disabled_bills": ["Invented 4111111111110002"],
                "occurrence_caps": {"Invented …0002": 3,
                                    "Invented 4111111111110002": 9}})}]}))

    def test_a_cut_bill_and_the_uncut_one_beside_it_restore_in_any_order(self):
        for order in ("cut first", "uncut first"):
            with self.subTest(order=order):
                self.conn = make_db()
                self.addCleanup(self.conn.close)
                counts = self._restore_cut_and_uncut(order)
                self.assertEqual(self._bills(), [
                    ("rec:invented-0002", "Invented …0002", 25.0),
                    ("rec:invented-4111111111110002",
                     "Invented 4111111111110002", 70.0)])
                self.assertEqual(counts.get("already_present", 0), 0,
                                 counts)
                pins = dict((r["transaction_id"], float(r["amount"]))
                            for r in self.conn.execute(
                    "SELECT c.transaction_id, b.amount"
                    "  FROM manual_categories c"
                    "  JOIN bills b ON b.id = c.bill_id").fetchall())
                self.assertEqual(pins, {"t1": 25.0, "t2": 70.0})
                cfg = self.conn.execute(
                    "SELECT config FROM tenant_settings").fetchone()["config"]
                self.assertEqual(cfg["disabled_bills"],
                                 ["Invented 4111111111110002"])
                self.assertEqual(cfg["occurrence_caps"],
                                 {"Invented …0002": 3,
                                  "Invented 4111111111110002": 9})

    def test_an_older_archive_of_the_same_household_keeps_each_card_on_its_bill(
            self):
        # after the upgrade: the first card's bill under the cut id, the
        # second whole beside it (the cut id was taken)
        for bid, payee, amount in (
                ("rec:invented-0002", "Invented …0002", 25),
                ("rec:invented-4111111111110002",
                 "Invented 4111111111110002", 70)):
            self.conn.execute(
                "INSERT INTO bills (id, type, payee, amount, frequency,"
                "                   active, raw)"
                " VALUES (%s, 'bill', %s, %s, 'MONTHLY', 1, '{}')",
                (bid, payee, amount))
        # its own backup from before the upgrade: both whole
        restore.restore_zip(self.conn, _zip({
            "bills.csv": [_bill("4000000000000002", 25),
                          _bill("4111111111110002", 70)],
            "tenant_settings.csv": [{"config": json.dumps({
                "disabled_bills": ["Invented 4111111111110002"],
                "occurrence_caps": {"Invented 4000000000000002": 3,
                                    "Invented 4111111111110002": 9},
                "dismissed_hints": {"Invented 4000000000000002":
                                    {"kind": "fixed"}},
                "dismissed_health": {"Invented 4000000000000002":
                                     {"health": "stale"}}})}]}))
        self.assertEqual([b[2] for b in self._bills()], [25.0, 70.0])
        cfg = self.conn.execute(
            "SELECT config FROM tenant_settings").fetchone()["config"]
        self.assertEqual(cfg["disabled_bills"],
                         ["Invented 4111111111110002"])
        self.assertEqual(cfg["occurrence_caps"],
                         {"Invented …0002": 3,
                          "Invented 4111111111110002": 9})
        # a dismissed hint or health flag is keyed on the payee too, and
        # follows the bill, or the flag the person dismissed comes back
        self.assertEqual(cfg["dismissed_hints"],
                         {"Invented …0002": {"kind": "fixed"}})
        self.assertEqual(cfg["dismissed_health"],
                         {"Invented …0002": {"health": "stale"}})

    def test_a_bill_a_person_made_never_takes_in_a_detected_one(self):
        self.conn.execute(
            "INSERT INTO bills (id, type, payee, amount, frequency, active,"
            "                   raw)"
            " VALUES ('rec:invented-0002', 'bill', 'Invented …0002', 5,"
            "         'MONTHLY', 1, %s)", (json.dumps({"source": "manual"}),))
        data = _zip({"bills.csv": [_bill("4000000000000002", 25)]})
        restore.restore_zip(self.conn, data)
        self.assertEqual([b[2] for b in self._bills()], [5.0, 25.0])
        mine = self.conn.execute(
            "SELECT amount FROM bills WHERE id = 'rec:invented-0002'"
        ).fetchone()
        self.assertEqual(float(mine["amount"]), 5.0)


class NameRowsAreTrue(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def _restore_account(self, name: str, sources: list) -> dict:
        return restore.restore_zip(self.conn, _zip({
            "items.csv": [{"id": "manual", "aggregator": "manual",
                           "institution_name": "Manual"}],
            "accounts.csv": [{"id": "manual:abcdefghijklmnop",
                              "item_id": "manual", "name": name,
                              "type": "credit", "balance_current": 1}],
            "account_sources.csv": [
                {"door": "manual", "source_hmac": d,
                 "account_id": "manual:abcdefghijklmnop"}
                for d in sources]}))

    def _rows(self):
        return [r["source_hmac"] for r in self.conn.execute(
            "SELECT source_hmac FROM account_sources").fetchall()]

    def test_only_the_row_true_of_its_account_is_counted(self):
        counts = self._restore_account(
            "Invented Brokerage", [_digest("Invented Brokerage"),
                                   _digest("Some Other Name"),
                                   _digest("Third")])
        self.assertEqual(self._rows(), [_digest("Invented Brokerage")])
        self.assertEqual(counts.get("account_sources"), 1, counts)

    def test_a_number_spaced_by_tabs_is_cut_and_never_digested(self):
        tabbed = "Visa 4000\t0000\t0000\t0002"
        self.assertEqual(account_sources._name_digest(tabbed),
                         account_sources._name_digest("Visa …0002"))
        self.assertEqual(account_sources.stored_account_name(tabbed),
                         "Visa …0002")
        self._restore_account(tabbed, [])
        name = self.conn.execute(
            "SELECT name FROM accounts WHERE id = 'manual:abcdefghijklmnop'"
        ).fetchone()["name"]
        self.assertEqual(name, "Visa …0002")
        self.assertEqual(self._rows(), [_digest("Visa …0002")])

    def test_the_sql_backfill_never_digests_a_tab_spaced_number(self):
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name)"
            " VALUES ('manual', 'manual', 'Manual')"
            " ON CONFLICT DO NOTHING")
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type)"
            " VALUES ('manual:abcdefghijklmnop', 'manual',"
            "         'Visa 4000\t0000\t0000\t0002', 'credit')")
        self.conn.execute(
            "INSERT INTO account_sources (door, source_hmac, account_id)"
            " VALUES ('manual', %s, 'manual:abcdefghijklmnop')",
            (_digest("Visa 4000 0000 0000 0002"),))
        account_sources.backfill(self.conn)
        self.assertEqual(self._rows(), [])

    def test_a_number_the_slug_reads_is_cut_and_never_digested(self):
        for shape in ("Visa 4000_0000_0000_0002",
                      "Visa 4000 \u2013 0000 \u2013 0000 \u2013 0002",
                      "Visa 4000,0000,0000,0002",
                      "Visa 4000:0000:0000:0002"):
            with self.subTest(shape=shape):
                self.conn = make_db()
                self.addCleanup(self.conn.close)
                self.assertEqual(account_sources.stored_account_name(shape),
                                 "Visa …0002")
                self._restore_account(shape, [])
                name = self.conn.execute(
                    "SELECT name FROM accounts"
                    " WHERE id = 'manual:abcdefghijklmnop'").fetchone()["name"]
                self.assertEqual(name, "Visa …0002")
                self.assertEqual(self._rows(), [_digest("Visa …0002")])
                # stored whole by another door: no digest is ever taken,
                # and one already there is dropped
                self.conn.execute("UPDATE accounts SET name = %s", (shape,))
                self.conn.execute("DELETE FROM account_sources")
                self.conn.execute(
                    "INSERT INTO account_sources (door, source_hmac,"
                    "                             account_id)"
                    " VALUES ('manual', %s, 'manual:abcdefghijklmnop')",
                    (_digest(shape),))
                account_sources.backfill(self.conn)
                self.assertEqual(self._rows(), [])


if __name__ == "__main__":
    unittest.main()
