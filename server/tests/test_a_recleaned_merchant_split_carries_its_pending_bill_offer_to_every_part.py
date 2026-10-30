"""A pending offer to add a merchant to a bill follows that merchant when
a better string clean splits it, the way the bill itself does.

The offer says "this bill may also be paid by <merchant>". When the clean
tells two businesses apart and moves each line's rows to its own merchant,
an offer left naming the old merchant names nothing any more: approving it
would add a ghost, and the two real parts would never be offered. So every
part is offered in its place (unless the bill already pays it); the old
offer stands while the old merchant keeps rows and is superseded once it
has none. Running the pass again changes nothing.

Names and figures are invented.
"""
import unittest

from oikonome.engine import bills, merchant_dedup, merchant_identity

from .util import add_txn, make_db

A_RAW = "A&A PLUMBING INC"
B_RAW = "B&B PLUMBING"
KEEP_RAW = "PLUMBING SVC"
OLD = "Plumbing"
A = "A&A Plumbing"
B = "B&B Plumbing"
OTHER_RAW = "NORTHWIND HEATING"
BILL = "Home upkeep"


class _SplitOffer(unittest.TestCase):
    raws = (A_RAW, B_RAW)

    def setUp(self):
        self.conn = make_db()
        amounts = {A_RAW: 400.0, B_RAW: 250.0, KEEP_RAW: 90.0}
        for i, raw in enumerate(self.raws):
            add_txn(self.conn, f"2026-0{i + 3}-10", amounts[raw], raw,
                    txn_id=f"p{i}")
            # the old clean's answer: every line one shared name
            self.conn.execute(
                "INSERT INTO merchant_canonical (raw_merchant, canonical, "
                "method) VALUES (%s,%s,%s)",
                (raw, OLD, "manual" if raw == KEEP_RAW else "layer1"))
        add_txn(self.conn, "2026-03-02", 120.0, OTHER_RAW, txn_id="h1")
        merchant_identity.resolve(self.conn)
        heating = self.conn.execute(
            "SELECT name FROM merchants WHERE id = (SELECT merchant_id "
            "FROM transactions WHERE id = 'h1')").fetchone()["name"]
        bills.save_bill(self.conn, payee=BILL, amount=300,
                        frequency="monthly", merchants=[heating])
        self.offer = bills._sig("merchant", BILL, OLD)
        bills._insert_proposal(
            self.conn, pid=self.offer, kind="merchant", payee=OLD,
            amount=300, evidence={"bill_payee": BILL, "merchant_name": OLD})

    def tearDown(self):
        self.conn.close()

    def _offers(self):
        return {(r["payee"], r["status"], r["evidence"]["merchant_name"],
                 r["evidence"]["bill_payee"])
                for r in self.conn.execute(
                    "SELECT payee, status, evidence FROM bill_proposals "
                    "WHERE kind = 'merchant'").fetchall()}

    def _state(self):
        return self.conn.execute(
            "SELECT id, payee, amount, status, evidence, decided_at "
            "FROM bill_proposals ORDER BY id").fetchall()


class WholeSplitOfferTests(_SplitOffer):
    def setUp(self):
        super().setUp()
        merchant_dedup.apply(self.conn)

    def test_each_part_is_offered_and_the_old_offer_is_superseded(self):
        self.assertEqual(self._offers(), {
            (OLD, "superseded", OLD, BILL),
            (A, "pending", A, BILL),
            (B, "pending", B, BILL)})

    def test_approving_a_parts_offer_adds_that_part_to_the_bill(self):
        pid = bills._sig("merchant", BILL, A)
        res = bills.apply_proposal(self.conn, pid, "approve")
        self.assertNotIn("error", res or {})
        raw = self.conn.execute("SELECT raw FROM bills WHERE payee = %s",
                                (BILL,)).fetchone()["raw"]
        self.assertIn(A, raw["merchant_names"])

    def test_running_it_again_changes_nothing(self):
        before = self._state()
        merchant_dedup.apply(self.conn)
        merchant_dedup.apply(self.conn)
        self.assertEqual(self._state(), before)


class PartialSplitOfferTests(_SplitOffer):
    """One line moves away; the old merchant keeps a line a person pinned
    to its name, so its own offer still names something real."""
    raws = (A_RAW, KEEP_RAW)

    def setUp(self):
        super().setUp()
        merchant_dedup.apply(self.conn)

    def test_the_old_offer_stands_and_the_part_is_offered_beside_it(self):
        self.assertEqual(self._offers(), {
            (OLD, "pending", OLD, BILL),
            (A, "pending", A, BILL)})

    def test_running_it_again_changes_nothing(self):
        before = self._state()
        merchant_dedup.apply(self.conn)
        merchant_dedup.apply(self.conn)
        self.assertEqual(self._state(), before)


class SplitIsAppliedTwiceTests(_SplitOffer):
    """The split's own follow-through, run a second time over the same
    parts, finds every offer already in place."""

    def test_following_the_same_split_twice_changes_nothing(self):
        merchant_dedup.apply(self.conn)
        before = self._state()
        old = self.conn.execute(
            "SELECT id FROM merchants WHERE name = %s", (OLD,)).fetchone()
        parts = [(r["id"], r["name"], []) for r in self.conn.execute(
            "SELECT id::text AS id, name FROM merchants WHERE name = ANY(%s) "
            "AND merged_into IS NULL ORDER BY id", ([A, B],)).fetchall()]
        with self.conn.transaction():
            bills.follow_merchant_split(
                self.conn, old["id"] if old else "0", OLD, parts,
                keep_old=False)
        self.assertEqual(self._state(), before)


class AlreadyPaidPartTests(_SplitOffer):
    def test_a_part_the_bill_already_pays_is_not_offered(self):
        bills.save_bill(self.conn, payee=BILL, amount=300,
                        frequency="monthly", merchants=[OLD])
        merchant_dedup.apply(self.conn)
        offered = {o[0] for o in self._offers() if o[1] == "pending"}
        self.assertEqual(offered, set())


if __name__ == "__main__":
    unittest.main()
