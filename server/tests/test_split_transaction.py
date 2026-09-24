"""One charge, several categories: a hand split of a ledger row.

The parts a person writes must add up to the charge, be spending categories,
and then BE the row for every per-category reader — the month verdict's
buckets, the Spending and Cash Flow category totals, the year lens, the
ledger's category filter — while every reader that counts whole rows
(totals by month, the anomaly pass) sees the charge unchanged. Removing the
split hands the row back whole.
"""
import datetime as dt
import io
import threading
import time
import unittest
import uuid
import zipfile

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import anomalies, budget, reporting, splits
from oikonome.sync import adopt, batches, reanchor, restore
from oikonome.sync import base as sync_base
from oikonome.sync.base import Transaction
from oikonome.web import data, lenses

from .util import (TODAY, _ensure_db, add_txn, make_db, seed_accounts,
                   write_config)

FOOD, OTHER, HOME = "FOOD_AND_DRINK", "GENERAL_MERCHANDISE", "HOME_IMPROVEMENT"


class ValidationTests(unittest.TestCase):
    def test_parts_must_add_up_to_the_charge(self):
        ok = splits.validate(120.0, [{"category": FOOD, "amount": 80},
                                     {"category": OTHER, "amount": 40}])
        self.assertEqual(ok, [{"category": FOOD, "amount": 80.0},
                              {"category": OTHER, "amount": 40.0}])
        with self.assertRaises(splits.SplitError) as e:
            splits.validate(120.0, [{"category": FOOD, "amount": 80},
                                    {"category": OTHER, "amount": 30}])
        self.assertIn("10.00 left over", str(e.exception))
        with self.assertRaises(splits.SplitError):
            splits.validate(120.0, [{"category": FOOD, "amount": 80},
                                    {"category": OTHER, "amount": 50}])

    def test_cent_arithmetic_is_exact(self):
        # 0.1 + 0.2 != 0.3 in a double; the parts are compared in cents
        splits.validate(0.3, [{"category": FOOD, "amount": 0.1},
                              {"category": OTHER, "amount": 0.2}])

    def test_a_split_needs_two_distinct_spending_parts(self):
        with self.assertRaises(splits.SplitError):
            splits.validate(50.0, [{"category": FOOD, "amount": 50}])
        with self.assertRaises(splits.SplitError):
            splits.validate(50.0, [{"category": FOOD, "amount": 25},
                                   {"category": FOOD, "amount": 25}])
        for flow in ("TRANSFER_OUT", "INCOME", "LOAN_PAYMENTS"):
            with self.assertRaises(splits.SplitError):
                splits.validate(50.0, [{"category": FOOD, "amount": 25},
                                       {"category": flow, "amount": 25}])
        # a custom name is a spending category like any other
        splits.validate(50.0, [{"category": FOOD, "amount": 25},
                               {"category": "Kids", "amount": 25}])

    def test_every_part_carries_the_charge_s_sign(self):
        with self.assertRaises(splits.SplitError):
            splits.validate(50.0, [{"category": FOOD, "amount": 75},
                                   {"category": OTHER, "amount": -25}])
        with self.assertRaises(splits.SplitError):
            splits.validate(50.0, [{"category": FOOD, "amount": 0},
                                   {"category": OTHER, "amount": 50}])
        # a refund splits the same way, in its own direction
        splits.validate(-50.0, [{"category": FOOD, "amount": -20},
                                {"category": OTHER, "amount": -30}])

    def test_malformed_input_is_refused_not_crashed(self):
        for bad in (None, "x", [], [1, 2], [{"category": 3, "amount": 1}] * 2,
                    [{"category": FOOD, "amount": "1"}] * 2,
                    [{"category": FOOD, "amount": True},
                     {"category": OTHER, "amount": 1}]):
            with self.assertRaises(splits.SplitError):
                splits.validate(2.0, bad)

    def test_a_non_finite_or_absurd_amount_is_a_refusal_not_a_crash(self):
        """A part amount of NaN, infinity or a number past any real charge
        is a bad part like any other. Rounding one to cents raises
        ValueError / OverflowError, which is not a SplitError, so the split
        door answered a bare 500 instead of saying which part was wrong."""
        for amt in (float("nan"), float("inf"), float("-inf"), 1e308, 1e400,
                    10 ** 400):
            with self.assertRaises(splits.SplitError) as e:
                splits.validate(-10.0, [{"category": FOOD, "amount": amt},
                                        {"category": OTHER, "amount": 1}])
            self.assertIn("part 1", str(e.exception))
        for row in (float("nan"), float("inf")):
            with self.assertRaises(splits.SplitError):
                splits.validate(row, [{"category": FOOD, "amount": 1},
                                      {"category": OTHER, "amount": 1}])


class RollupTests(unittest.TestCase):
    """A $120 charge split $80 food / $40 other, beside a plain $50 food
    row and a plain $30 other row."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.mid = TODAY.replace(day=5)
        self.big = add_txn(self.conn, self.mid, 120.0, "ACME MARKET",
                           primary=OTHER, account="chk")
        add_txn(self.conn, self.mid, 50.0, "SAFEWAY", primary=FOOD, account="chk")
        add_txn(self.conn, self.mid, 30.0, "TARGET", primary=OTHER, account="chk")

    def tearDown(self):
        self.conn.close()

    def _split(self):
        return splits.set_split(self.conn, self.big, [
            {"category": FOOD, "amount": 80}, {"category": OTHER, "amount": 40}])

    def test_the_verdict_buckets_see_the_parts(self):
        before = budget.month_status(self.conn, TODAY)
        self.assertAlmostEqual(before["buckets"]["food"]["actual"], 50.0)
        self.assertAlmostEqual(before["buckets"]["other"]["actual"], 150.0)
        self._split()
        after = budget.month_status(self.conn, TODAY)
        self.assertAlmostEqual(after["buckets"]["food"]["actual"], 130.0)
        self.assertAlmostEqual(after["buckets"]["other"]["actual"], 70.0)
        # the same money, twice named, never double-counted
        self.assertAlmostEqual(
            after["buckets"]["food"]["actual"] + after["buckets"]["other"]["actual"],
            before["buckets"]["food"]["actual"] + before["buckets"]["other"]["actual"])
        # the rows behind each bucket name the split charge in both
        food_ids = budget.bucket_ledger(after, "food")["txn_ids"]
        other_ids = budget.bucket_ledger(after, "other")["txn_ids"]
        self.assertIn(self.big, food_ids)
        self.assertIn(self.big, other_ids)
        # …and removing the split hands the row back whole
        splits.clear_split(self.conn, self.big)
        again = budget.month_status(self.conn, TODAY)
        self.assertAlmostEqual(again["buckets"]["food"]["actual"], 50.0)
        self.assertAlmostEqual(again["buckets"]["other"]["actual"], 150.0)

    def test_the_spend_rows_fan_out_and_carry_the_line(self):
        self._split()
        rows = budget._spend_rows(self.conn, self.mid, TODAY + dt.timedelta(days=1))
        parts = [r for r in rows if r["txn_id"] == self.big]
        self.assertEqual([(r["stored_category"], r["amount"], r["split_line"])
                          for r in parts],
                         [(FOOD, 80.0, 1), (OTHER, 40.0, 2)])
        self.assertTrue(parts[0]["is_food"])
        self.assertFalse(parts[1]["is_food"])
        whole = [r for r in budget._spend_rows(
            self.conn, self.mid, TODAY + dt.timedelta(days=1), parts=False)
            if r["txn_id"] == self.big]
        self.assertEqual([(r["stored_category"], r["amount"], r["split_line"])
                          for r in whole], [(OTHER, 120.0, None)])

    def test_a_partial_reimbursement_is_shared_pro_rata(self):
        self._split()
        dep = add_txn(self.conn, self.mid, -30.0, "VENMO FROM SAM", account="chk",
                      primary="TRANSFER_IN")
        self.conn.execute(
            "INSERT INTO reimbursements (expense_id, reimburse_id, amount, partial)"
            " VALUES (%s, %s, 30, 1)", (self.big, dep))
        rows = [r for r in budget._spend_rows(
            self.conn, self.mid, TODAY + dt.timedelta(days=1))
            if r["txn_id"] == self.big]
        # $90 net of the $120, split 2:1 like the parts
        self.assertEqual([r["amount"] for r in rows], [60.0, 30.0])
        st = budget.month_status(self.conn, TODAY)
        self.assertAlmostEqual(st["buckets"]["food"]["actual"], 110.0)
        self.assertAlmostEqual(st["buckets"]["other"]["actual"], 60.0)

    def test_the_spending_reports_count_each_part_under_its_category(self):
        self._split()
        sp = reporting.compute_spending(self.conn, TODAY)
        tot = dict(sp["category_totals"])
        self.assertAlmostEqual(tot["FOOD AND DRINK"], 130.0)
        self.assertAlmostEqual(tot["GENERAL MERCHANDISE"], 70.0)
        # the year's total is the charges, not the parts twice
        self.assertAlmostEqual(sum(y[1] for y in sp["by_year"]), 200.0)
        self.assertAlmostEqual(sum(m[1] for m in sp["by_month"]), 200.0)
        win = reporting.spending_window(self.conn, TODAY, "cur")
        cats = {c[0]: c[1] for c in win["categories"]}
        self.assertAlmostEqual(cats["FOOD AND DRINK"], 130.0)
        self.assertAlmostEqual(cats["GENERAL MERCHANDISE"], 70.0)
        self.assertAlmostEqual(win["total"], 200.0)

    def test_the_year_lens_counts_the_parts_and_opens_the_row_under_each(self):
        self._split()
        door = lenses.year_category_ledger(self.conn, FOOD, TODAY.year)
        self.assertIn(self.big, door["txn_ids"])
        self.assertAlmostEqual(door["amount"], 130.0)
        door = lenses.year_category_ledger(self.conn, OTHER, TODAY.year)
        self.assertEqual(door["txn_ids"].count(self.big), 1)
        self.assertAlmostEqual(door["amount"], 70.0)
        st = budget.month_status(self.conn, TODAY)
        month_door = lenses.month_category_ledger(st, FOOD)
        self.assertIn(self.big, month_door["txn_ids"])
        self.assertAlmostEqual(month_door["amount"], 130.0)

    def test_the_ledger_row_carries_its_parts_and_answers_a_part_s_filter(self):
        self._split()
        rows = data.transactions(self.conn, TODAY.year, TODAY.month)
        row = next(r for r in rows if r["id"] == self.big)
        self.assertEqual(row["split"], [{"category": FOOD, "amount": 80.0},
                                        {"category": OTHER, "amount": 40.0}])
        self.assertEqual(row["category"], "GENERAL MERCHANDISE")
        plain = next(r for r in rows if r["id"] != self.big)
        self.assertIsNone(plain["split"])
        found, *_ = data.search_transactions(self.conn, "", category=FOOD)
        self.assertIn(self.big, {r["id"] for r in found})
        found, *_ = data.search_transactions(self.conn, "", category=HOME)
        self.assertNotIn(self.big, {r["id"] for r in found})

    def test_the_anomaly_pass_judges_the_whole_charge(self):
        self._split()
        # ten $12 charges, then the $120 one: unusually large as a CHARGE,
        # which the parts alone would hide
        for i in range(10):
            add_txn(self.conn, self.mid - dt.timedelta(days=40 + i), 12.0,
                    "ACME MARKET", primary=OTHER, account="chk")
        rows = budget._spend_rows(self.conn, self.mid, TODAY + dt.timedelta(days=1),
                                  parts=False)
        self.assertEqual([r["amount"] for r in rows if r["txn_id"] == self.big],
                         [120.0])
        anomalies.detect(self.conn, TODAY)   # runs whole, never raises

    def test_a_charge_that_posts_for_another_amount_drops_its_split(self):
        self._split()
        self.conn.execute("UPDATE transactions SET amount = 125 WHERE id = %s",
                          (self.big,))
        self.assertEqual(splits.stale(self.conn), [self.big])
        self.assertEqual(splits.drop_stale(self.conn), 2)
        self.assertEqual(splits.for_txn(self.conn, self.big), [])

    def test_a_split_survives_an_export_and_restore(self):
        self._split()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for tbl in ("items", "accounts", "transactions", "transaction_splits"):
                rows = self.conn.execute(f"SELECT * FROM {tbl}").fetchall()
                cols = [c for c in rows[0].keys()
                        if c not in ("tenant_id", "access_token")]
                lines = [",".join(cols)]
                for r in rows:
                    lines.append(",".join(
                        "" if r[c] is None else
                        ('"' + str(r[c]).replace('"', '""') + '"')
                        for c in cols))
                z.writestr(f"{tbl}.csv", "\n".join(lines))
        dest = make_db()
        try:
            restore.restore_zip(dest, buf.getvalue())
            self.assertEqual(splits.for_txn(dest, self.big),
                             [{"category": FOOD, "amount": 80.0},
                              {"category": OTHER, "amount": 40.0}])
        finally:
            dest.close()


def _parts(conn, tid):
    return splits.for_txn(conn, tid)


PARTS = [{"category": FOOD, "amount": 80.0}, {"category": OTHER, "amount": 40.0}]


class SplitFollowsTheChargeTests(unittest.TestCase):
    """A split is something a person wrote on a charge, like a note: when
    the row it hangs on is retired and the charge lives on under another
    id, the parts go with it. Every rollup filters removed rows, so parts
    left behind vanish from Spending while the charge counts whole again;
    and the stale-split sweep never notices, because the parts still add
    up to the retired row."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _hold(self, tid, amount=120.0):
        sync_base.upsert_transactions(self.conn, [Transaction(
            id=tid, account_id="chk", date=TODAY, amount=amount,
            name="ACME MARKET", pending=True, category_primary=OTHER)])

    def _post(self, tid, old, amount=120.0):
        sync_base.upsert_transactions(self.conn, [Transaction(
            id=tid, account_id="chk", date=TODAY, amount=amount,
            name="ACME MARKET", pending=False, category_primary=OTHER,
            raw={"pending_transaction_id": old})])

    def test_a_split_made_while_pending_survives_the_charge_posting(self):
        self._hold("sp:p1")
        splits.set_split(self.conn, "sp:p1", PARTS)
        self._post("sp:s1", "sp:p1")
        self.assertEqual(_parts(self.conn, "sp:s1"), PARTS)
        self.assertEqual(_parts(self.conn, "sp:p1"), [])
        tot = dict(reporting.compute_spending(self.conn, TODAY)["category_totals"])
        self.assertAlmostEqual(tot["FOOD AND DRINK"], 80.0)
        self.assertAlmostEqual(tot["GENERAL MERCHANDISE"], 40.0)

    def test_a_posted_row_keeps_its_own_split_over_the_pending_one(self):
        """All or nothing per charge: the posted row's own parts are never
        mixed with, or replaced by, the pending row's."""
        self._hold("sp:p2")
        splits.set_split(self.conn, "sp:p2", PARTS)
        sync_base.upsert_transactions(self.conn, [Transaction(
            id="sp:s2", account_id="chk", date=TODAY, amount=120.0,
            name="ACME MARKET", pending=False, category_primary=OTHER)])
        mine = [{"category": FOOD, "amount": 100.0},
                {"category": HOME, "amount": 20.0}]
        splits.set_split(self.conn, "sp:s2", mine)
        self._post("sp:s2", "sp:p2")
        self.assertEqual(_parts(self.conn, "sp:s2"), mine)

    def test_a_charge_that_posts_for_another_amount_still_drops_the_parts(self):
        """The carry hands the parts over; the stale sweep then judges them
        against the POSTED amount, which is the row that counts."""
        self._hold("sp:p3")
        splits.set_split(self.conn, "sp:p3", PARTS)
        self._post("sp:s3", "sp:p3", amount=125.0)
        self.assertEqual(splits.stale(self.conn), ["sp:s3"])
        splits.drop_stale(self.conn)
        self.assertEqual(_parts(self.conn, "sp:s3"), [])

    def test_a_re_numbered_charge_takes_the_split_on_re_anchor(self):
        old = add_txn(self.conn, TODAY - dt.timedelta(days=3), 120.0,
                      "ACME MARKET", primary=OTHER, txn_id="ra_old")
        new = add_txn(self.conn, TODAY - dt.timedelta(days=2), 120.0,
                      "ACME MARKET", primary=OTHER, txn_id="ra_new")
        splits.set_split(self.conn, old, PARTS)
        self.assertEqual(sync_base.mark_removed(self.conn, [old]), 1)
        self.assertEqual(_parts(self.conn, new), PARTS)
        self.assertEqual(_parts(self.conn, old), [])
        self.assertEqual(reanchor.stranded(self.conn), [])

    def test_re_anchor_never_mixes_or_overwrites_the_live_row_s_split(self):
        old = add_txn(self.conn, TODAY - dt.timedelta(days=3), 120.0,
                      "ACME MARKET", primary=OTHER, txn_id="rb_old")
        new = add_txn(self.conn, TODAY - dt.timedelta(days=2), 120.0,
                      "ACME MARKET", primary=OTHER, txn_id="rb_new")
        splits.set_split(self.conn, old, PARTS)
        mine = [{"category": FOOD, "amount": 100.0},
                {"category": HOME, "amount": 20.0}]
        splits.set_split(self.conn, new, mine)
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s", (old,))
        out = reanchor.reanchor_stranded(self.conn)
        self.assertEqual(_parts(self.conn, new), mine)
        # a differing answer stays where it is, counted, rather than deleted
        self.assertEqual(_parts(self.conn, old), PARTS)
        self.assertEqual(out["left"], 1)
        # an identical copy is let go once the live row demonstrably has it
        splits.set_split(self.conn, new, PARTS)
        out = reanchor.reanchor_stranded(self.conn)
        self.assertEqual(out["moved"], 1)
        self.assertEqual(_parts(self.conn, old), [])

    def test_a_split_on_a_retired_reconnect_twin_moves_to_the_survivor(self):
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_id, "
            "institution_name, status, access_token) VALUES "
            "('cu-item','plaid','ins_9','Credit Union','ok','tok')")
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, mask) VALUES "
            "('biz','cu-item','Business Checking','depository','1111')")
        d = dt.date(2026, 8, 14)
        for tid, name, merchant in (
                ("raw", "REF000000000042 NORTHWIND HOSTING.COM NORTHWINDHOST.US",
                 "Northwindhost.us"),
                ("clean", "Northwind Hosting", "Northwind Hosting")):
            self.conn.execute(
                "INSERT INTO transactions (id, account_id, date, amount, name, "
                "merchant_name, pending, removed) VALUES (%s,'biz',%s,120,%s,%s,0,0)",
                (tid, d, name, merchant))
        splits.set_split(self.conn, "raw", PARTS)
        out = adopt.dedupe_twins(self.conn, "biz")
        self.assertEqual([(o["kept"], o["removed"]) for o in out],
                         [("clean", "raw")])
        self.assertEqual(_parts(self.conn, "clean"), PARTS)
        self.assertEqual(_parts(self.conn, "raw"), [])

    def test_an_import_rollback_confirm_counts_the_split_it_would_delete(self):
        seed_accounts(self.conn)
        acct = self.conn.execute("SELECT id FROM accounts LIMIT 1").fetchone()["id"]
        bid = batches.create(self.conn, "csv", acct, "c.csv")
        txns = batches.tag(self.conn, [Transaction(
            id="bk-split", account_id=acct, date=dt.date(2024, 1, 5),
            name="ACME MARKET", amount=120.0)], bid)
        sync_base.upsert_transactions(self.conn, txns)
        batches.finish(self.conn, bid, 1)
        splits.set_split(self.conn, "bk-split", PARTS)
        row = [b for b in batches.recent(self.conn) if b["id"] == bid][0]
        # one split is one thing a person did, however many parts it has
        self.assertEqual(row["annotations"], 1)


class OnlySpendRowsSplitTests(unittest.TestCase):
    """A split re-buckets SPENDING, so the door takes only a row the spend
    rollups read: money out, not a transfer, income or loan payment, not a
    card payment, not on a loan account. A split on any other row is
    stored, shown on the ledger row, and counted by no report — the person
    is told it worked and nothing changes. A row recategorized out of
    spending afterwards has its split dropped by the stale sweep for the
    same reason."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _refused(self, tid, amount):
        with self.assertRaises(splits.SplitError):
            splits.set_split(self.conn, tid, [
                {"category": FOOD, "amount": amount / 2},
                {"category": OTHER, "amount": amount / 2}])
        self.assertEqual(splits.for_txn(self.conn, tid), [])

    def test_a_refund_is_refused(self):
        self._refused(add_txn(self.conn, TODAY, -50.0, "RETURN", account="chk"),
                      -50.0)

    def test_a_loan_payment_from_checking_splits_and_keeps_its_split(self):
        """The spend rollups count a loan payment made from checking, so it
        is a row a person may split — and the stale sweep must not take the
        split back on the next sync."""
        tid = add_txn(self.conn, TODAY, 120.0, "AUTO LOAN PMT",
                      primary="LOAN_PAYMENTS", account="chk")
        splits.set_split(self.conn, tid, PARTS)
        self.assertTrue(splits.splittable(self.conn, tid))
        self.assertEqual(splits.drop_stale(self.conn), 0)
        self.assertEqual(splits.for_txn(self.conn, tid), PARTS)

    def test_a_transfer_is_refused(self):
        self._refused(add_txn(self.conn, TODAY, 50.0, "TO SAVINGS",
                              primary="TRANSFER_OUT", account="chk"), 50.0)
        tid = add_txn(self.conn, TODAY, 60.0, "ACME MARKET", account="chk")
        self.conn.execute("UPDATE transactions SET category_override="
                          "'TRANSFER_OUT' WHERE id=%s", (tid,))
        self._refused(tid, 60.0)

    def test_a_card_payment_is_refused(self):
        self._refused(add_txn(self.conn, TODAY, 50.0, "CARD AUTOPAY",
                              primary="LOAN_PAYMENTS",
                              detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
                              account="chk"), 50.0)

    def test_a_loan_account_row_is_refused(self):
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
            " VALUES ('mort','it1','Test Mortgage','loan','mortgage',100000)")
        self._refused(add_txn(self.conn, TODAY, 50.0, "ESCROW ITEM",
                              account="mort"), 50.0)

    def test_a_row_recategorized_to_a_transfer_loses_its_split(self):
        tid = add_txn(self.conn, TODAY, 120.0, "ACME MARKET", account="chk")
        splits.set_split(self.conn, tid, PARTS)
        keep = add_txn(self.conn, TODAY, 120.0, "ACME MARKET", account="chk")
        splits.set_split(self.conn, keep, PARTS)
        data.set_category(self.conn, tid, "TRANSFER_OUT")
        self.assertEqual(splits.stale(self.conn), [tid])
        self.assertEqual(splits.drop_stale(self.conn), 2)
        self.assertEqual(splits.for_txn(self.conn, tid), [])
        self.assertEqual(splits.for_txn(self.conn, keep), PARTS)

    def test_a_split_row_a_machine_pass_recategorizes_keeps_its_split(self):
        """The bill pass and the categorizer know nothing of splits: a pin
        they stamp on a split row, flow category or not, must not cost the
        person their parts while the rollups still read the row."""
        tid = add_txn(self.conn, TODAY, 120.0, "ACME MARKET", account="chk")
        splits.set_split(self.conn, tid, PARTS)
        self.conn.execute("UPDATE transactions SET category_override="
                          "'LOAN_PAYMENTS', override_source='bill' WHERE id=%s",
                          (tid,))
        self.assertEqual(splits.drop_stale(self.conn), 0)
        self.conn.execute("UPDATE transactions SET category_override=NULL, "
                          "override_source=NULL, category_primary='INCOME' "
                          "WHERE id=%s", (tid,))
        self.assertEqual(splits.drop_stale(self.conn), 0)
        self.assertEqual(splits.for_txn(self.conn, tid), PARTS)

    def test_a_split_written_while_the_sweep_runs_is_not_deleted(self):
        """The sweep lists stale rows, then deletes. A person re-splitting
        a row in between — to the new amount — wrote a split that holds;
        deleting by id alone threw it away with the stale one."""
        tid = add_txn(self.conn, TODAY, 120.0, "ACME MARKET", account="chk")
        splits.set_split(self.conn, tid, PARTS)
        self.conn.execute("UPDATE transactions SET amount=125 WHERE id=%s", (tid,))
        fresh = [{"category": FOOD, "amount": 85.0},
                 {"category": OTHER, "amount": 40.0}]
        real = splits.stale

        def listed_then_re_split(conn):
            ids = real(conn)
            splits.set_split(conn, tid, fresh)
            return ids

        splits.stale = listed_then_re_split
        try:
            splits.drop_stale(self.conn)
        finally:
            splits.stale = real
        self.assertEqual(splits.for_txn(self.conn, tid), fresh)

    def test_a_retired_row_keeps_its_split_for_the_carry(self):
        """The spend test is about what the row IS, not whether it is live:
        a retired row's split is waiting to follow the charge."""
        tid = add_txn(self.conn, TODAY, 120.0, "ACME MARKET", account="chk")
        splits.set_split(self.conn, tid, PARTS)
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s", (tid,))
        self.assertEqual(splits.stale(self.conn), [])


class SplitAgainstAMovingAmountTests(unittest.TestCase):
    """The door validates the parts against the row's amount and writes
    them in one locked step. Read outside the lock, a sync posting a new
    amount in between leaves parts that add up to a total the bank never
    charged — stored after the stale sweep already ran."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        import os
        from .util import TEST_DB
        self.other = tenancy.tenant_connect(tid, os.environ.get(
            "OIKONOME_TEST_DSN",
            f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}"))

    def tearDown(self):
        self.other.close()
        self.conn.close()

    def test_a_split_waits_for_an_amount_being_changed_and_sees_the_new_one(self):
        tid = add_txn(self.conn, TODAY, 120.0, "ACME MARKET", account="chk")
        out: dict = {}

        def door():
            try:
                out["ok"] = splits.set_split(self.conn, tid, PARTS)
            except Exception as e:                       # noqa: BLE001
                out["err"] = e

        with self.other.transaction():
            self.other.execute(
                "SELECT 1 FROM transactions WHERE id=%s FOR UPDATE", (tid,))
            th = threading.Thread(target=door)
            th.start()
            time.sleep(0.5)
            # the door is queued behind the sync's lock, not already done
            self.assertTrue(th.is_alive())
            self.other.execute(
                "UPDATE transactions SET amount=125 WHERE id=%s", (tid,))
        th.join(10)
        self.assertFalse(th.is_alive())
        self.assertIsInstance(out.get("err"), splits.SplitError)
        self.assertEqual(splits.for_txn(self.conn, tid), [])


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"split-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            cls.d = dt.date.today().replace(day=10)
            cls.txn = add_txn(conn, cls.d, 120.0, "ACME MARKET", account="chk")
        finally:
            conn.close()

    def _row(self):
        r = self.client.get("/api/transactions",
                            params={"y": self.d.year, "m": self.d.month}).json()
        return next(x for x in r["rows"] if x["id"] == self.txn)

    def test_put_replaces_delete_removes_and_the_row_says_so(self):
        r = self.client.put(f"/api/transactions/{self.txn}/split", json={
            "parts": [{"category": FOOD, "amount": 80},
                      {"category": OTHER, "amount": 40}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["split"],
                         [{"category": FOOD, "amount": 80.0},
                          {"category": OTHER, "amount": 40.0}])
        self.assertEqual(self._row()["split"],
                         [{"category": FOOD, "amount": 80.0},
                          {"category": OTHER, "amount": 40.0}])
        r = self.client.put(f"/api/transactions/{self.txn}/split", json={
            "parts": [{"category": FOOD, "amount": 100},
                      {"category": HOME, "amount": 20}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([p["category"] for p in self._row()["split"]],
                         [FOOD, HOME])
        r = self.client.delete(f"/api/transactions/{self.txn}/split")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(self._row()["split"])

    def test_the_row_says_whether_it_can_be_split(self):
        """The clients offer Split only where the door will take it; the
        row carries the server's answer so no client re-derives the rule."""
        conn = tenancy.tenant_connect(self.tid)
        try:
            refund = add_txn(conn, self.d, -15.0, "ACME MARKET REFUND", account="chk")
        finally:
            conn.close()
        rows = {x["id"]: x for x in self.client.get(
            "/api/transactions",
            params={"y": self.d.year, "m": self.d.month}).json()["rows"]}
        self.assertIs(rows[self.txn]["splittable"], True)
        self.assertIs(rows[refund]["splittable"], False)

    def test_a_bad_split_is_a_400_with_the_reason(self):
        r = self.client.put(f"/api/transactions/{self.txn}/split", json={
            "parts": [{"category": FOOD, "amount": 80},
                      {"category": OTHER, "amount": 30}]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("add up", r.json()["detail"])
        r = self.client.put(f"/api/transactions/{self.txn}/split", json={
            "parts": [{"category": FOOD, "amount": 80},
                      {"category": "TRANSFER_OUT", "amount": 40}]})
        self.assertEqual(r.status_code, 400)
        # a number no charge can be is a bad part too, not a server error
        r = self.client.put(f"/api/transactions/{self.txn}/split", json={
            "parts": [{"category": FOOD, "amount": 1e308},
                      {"category": OTHER, "amount": 1}]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("part 1", r.json()["detail"])
        r = self.client.put(f"/api/transactions/{self.txn}/split",
                            content='{"parts": [{"category": "%s", "amount": NaN},'
                                    ' {"category": "%s", "amount": 1}]}' % (FOOD, OTHER),
                            headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 400)
        r = self.client.put("/api/transactions/nope/split", json={
            "parts": [{"category": FOOD, "amount": 1},
                      {"category": OTHER, "amount": 1}]})
        self.assertEqual(r.status_code, 404)
        r = self.client.delete("/api/transactions/nope/split")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
