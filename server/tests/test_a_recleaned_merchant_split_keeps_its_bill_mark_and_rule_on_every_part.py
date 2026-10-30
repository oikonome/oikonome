"""A string clean that splits a merchant never changes what a bill, a
category rule or a 1099 vendor mark covers.

An older clean folded two businesses into one merchant (both bank lines
came out as one shared word). A better clean tells them apart, and the
nightly pass moves each line's rows to its own merchant. Retiring the old
merchant into whichever part sorts first would hand the bill, the rule and
the mark to that part alone: the other part's charges would stop matching
the bill, lose the household's category and drop off the year-end 1099
list, silently. So every part gets a copy, each moved line is journalled so
the Merchants page can undo it, and undoing re-joins them.

A clean that moves only some lines away (the old merchant keeps rows) is
the same case with the old merchant as one of the parts.

Names and figures are invented.
"""
import unittest

from oikonome.engine import (bills, budget, entities, merchant_dedup,
                             merchant_identity, selfemploy)

from .util import add_txn, make_db

A_RAW = "A&A PLUMBING INC"
B_RAW = "B&B PLUMBING"
KEEP_RAW = "PLUMBING SVC"
OLD = "Plumbing"
A = "A&A Plumbing"
B = "B&B Plumbing"
YEAR = 2026


class _Split(unittest.TestCase):
    raws = (A_RAW, B_RAW)

    def setUp(self):
        self.conn = make_db()
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('biz','it1','Biz Checking',"
            "'depository','checking',4000)")
        self.eid = entities.create_entity(
            self.conn, name="Fabrikam Renovations", structure="sole_prop",
            business_start_date="2025-01-01")["id"]
        entities.assign_account(self.conn, "biz", self.eid)
        amounts = {A_RAW: 400.0, B_RAW: 250.0, KEEP_RAW: 90.0}
        for i, raw in enumerate(self.raws):
            add_txn(self.conn, f"{YEAR}-0{i + 3}-10", amounts[raw], raw,
                    account="biz", txn_id=f"p{i}")
            # the old clean's answer: every line one shared name
            self.conn.execute(
                "INSERT INTO merchant_canonical (raw_merchant, canonical, "
                "method) VALUES (%s,%s,%s)",
                (raw, OLD, "manual" if raw == KEEP_RAW else "layer1"))
        merchant_identity.resolve(self.conn)
        self.old = self._id(OLD)
        self.conn.execute(
            "INSERT INTO merchant_categories (merchant, category_primary, "
            "source) VALUES (%s,'HOME_IMPROVEMENT','user')", (OLD,))
        selfemploy.mark_vendor(self.conn, self.eid, OLD, tin_last4="4321")
        bills.save_bill(self.conn, payee="Plumber", amount=300,
                        frequency="monthly", merchants=[OLD])
        self.total = self._paid_total()

    def tearDown(self):
        self.conn.close()

    def _id(self, name):
        row = self.conn.execute(
            "SELECT id::text AS id FROM merchants WHERE name = %s "
            "AND merged_into IS NULL", (name,)).fetchone()
        return row and row["id"]

    def _bill_ids(self):
        row = self.conn.execute(
            "SELECT payee, raw FROM bills WHERE payee = 'Plumber'").fetchone()
        return set(budget.bill_merchant_ids(self.conn, [row])["Plumber"])

    def _rule(self, name):
        return self.conn.execute(
            "SELECT category_primary, source FROM merchant_categories "
            "WHERE merchant = %s", (name,)).fetchone()

    def _vendors(self):
        return {v["merchant"]: v for v in
                selfemploy.vendor_totals(self.conn, self.eid, YEAR)}

    def _paid_total(self):
        return round(sum(v["paid"] for v in self._vendors().values()), 2)

    def _state(self):
        return (
            sorted(self.conn.execute(
                "SELECT id::text, name, merged_into::text FROM merchants "
                "ORDER BY id").fetchall(), key=str),
            self.conn.execute("SELECT raw FROM bills ORDER BY id").fetchall(),
            self.conn.execute(
                "SELECT merchant, category_primary, source FROM "
                "merchant_categories ORDER BY merchant").fetchall(),
            self.conn.execute(
                "SELECT merchant, merchant_id::text, reportable, tin_last4, "
                "note FROM vendor_1099 ORDER BY merchant").fetchall(),
            self.conn.execute(
                "SELECT raw_merchant, from_canonical, to_canonical "
                "FROM merchant_renames ORDER BY id").fetchall())


class WholeSplitTests(_Split):
    def setUp(self):
        super().setUp()
        merchant_dedup.apply(self.conn)
        self.a, self.b = self._id(A), self._id(B)

    def test_both_parts_exist_and_the_old_merchant_is_not_merged_into_one(self):
        self.assertIsNotNone(self.a)
        self.assertIsNotNone(self.b)
        row = self.conn.execute(
            "SELECT merged_into FROM merchants WHERE id::text = %s",
            (self.old,)).fetchone()
        self.assertTrue(row is None or row["merged_into"] is None)

    def test_the_bill_matches_both_parts(self):
        self.assertEqual(self._bill_ids(), {self.a, self.b})

    def test_both_parts_carry_the_persons_rule(self):
        for name in (A, B):
            self.assertEqual(self._rule(name),
                             {"category_primary": "HOME_IMPROVEMENT",
                              "source": "user"})
        self.assertIsNone(self._rule(OLD))

    def test_both_parts_carry_the_mark_and_the_total_is_unchanged(self):
        v = self._vendors()
        for name in (A, B):
            self.assertTrue(v[name]["reportable"])
            self.assertEqual(v[name]["tin_last4"], "4321")
        self.assertEqual(round(v[A]["paid"] + v[B]["paid"], 2), self.total)
        notes = {r["note"] for r in self.conn.execute(
            "SELECT note FROM vendor_1099").fetchall()}
        self.assertEqual(notes, {f"carried from {OLD} on split"})

    def test_each_moved_line_is_journalled(self):
        rows = {(r["raw_merchant"], r["from_canonical"], r["to_canonical"])
                for r in self.conn.execute(
                    "SELECT raw_merchant, from_canonical, to_canonical "
                    "FROM merchant_renames").fetchall()}
        self.assertEqual(rows, {(A_RAW, OLD, A), (B_RAW, OLD, B)})

    def test_undoing_both_lines_rejoins_them_with_their_bill_rule_and_mark(self):
        for r in self.conn.execute(
                "SELECT id FROM merchant_renames ORDER BY id").fetchall():
            with self.conn.transaction():
                merchant_dedup.undo(self.conn, r["id"])
        merchant_identity.resolve(self.conn)
        back = self._id(OLD)
        self.assertIsNotNone(back)
        self.assertEqual({r["merchant_id"] for r in self.conn.execute(
            "SELECT merchant_id::text FROM transactions").fetchall()}, {back})
        self.assertEqual(self._bill_ids(), {back})
        self.assertEqual(self._rule(OLD), {"category_primary": "HOME_IMPROVEMENT",
                                           "source": "user"})
        self.assertIsNone(self._rule(A))
        self.assertIsNone(self._rule(B))
        marks = self.conn.execute(
            "SELECT merchant_id::text AS mid, tin_last4 FROM vendor_1099"
        ).fetchall()
        self.assertEqual(marks, [{"mid": back, "tin_last4": "4321"}])
        self.assertEqual(self._paid_total(), self.total)

    def test_running_it_again_changes_nothing(self):
        before = self._state()
        merchant_dedup.apply(self.conn)
        merchant_dedup.apply(self.conn)
        self.assertEqual(self._state(), before)


class PartialSplitTests(_Split):
    """One line moves away; the old merchant keeps a line a person pinned
    to its name."""
    raws = (A_RAW, KEEP_RAW)

    def setUp(self):
        super().setUp()
        merchant_dedup.apply(self.conn)
        self.a = self._id(A)

    def test_the_old_merchant_keeps_its_rows_and_the_part_joins_the_bill(self):
        self.assertEqual(self._id(OLD), self.old)
        self.assertEqual(self._bill_ids(), {self.old, self.a})

    def test_both_keep_the_rule_and_the_mark(self):
        for name in (OLD, A):
            self.assertEqual(self._rule(name)["category_primary"],
                             "HOME_IMPROVEMENT")
        v = self._vendors()
        self.assertTrue(v[OLD]["reportable"] and v[A]["reportable"])
        self.assertEqual(self._paid_total(), self.total)

    def test_undo_drops_the_copies_and_leaves_the_old_merchant_whole(self):
        jid = self.conn.execute(
            "SELECT id FROM merchant_renames").fetchone()["id"]
        with self.conn.transaction():
            merchant_dedup.undo(self.conn, jid)
        self.assertIsNone(self._rule(A))
        self.assertEqual(self._bill_ids(), {self.old})
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM vendor_1099").fetchone()["n"], 1)

    def test_running_it_again_changes_nothing(self):
        before = self._state()
        merchant_dedup.apply(self.conn)
        self.assertEqual(self._state(), before)


class SingleSurvivorTests(_Split):
    """Every line moves to one new merchant: the old one is retired into
    it as before, and nothing is journalled."""
    raws = (A_RAW,)

    def test_the_old_merchant_is_retired_into_the_one_part(self):
        merchant_dedup.apply(self.conn)
        a = self._id(A)
        self.assertEqual(self._bill_ids(), {a})
        self.assertEqual(self._rule(A)["category_primary"], "HOME_IMPROVEMENT")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM merchant_renames").fetchone()["n"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT merchant_id::text AS m FROM vendor_1099").fetchone()["m"],
            a)


if __name__ == "__main__":
    unittest.main()
