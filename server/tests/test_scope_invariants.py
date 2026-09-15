"""Every merchant-wide write touches exactly the rows its page shows.

A class of bug, not a case: a merchant page that matches the display name
exactly, while its "Apply to whole merchant" write matches substrings,
rewrites far more rows than the page ever showed. The fix for that door is
in data.py; THIS file is what stops the next door from drifting the same
way. Each test seeds a ledger where a naive match
(substring, shared word, shared token) would over-reach, snapshots every
row, runs the door, and asserts the CHANGED set equals the DISPLAYED set.

Add every new merchant-wide door here. The helper is the contract."""
import unittest

from oikonome.engine import bills, merchant_dedup
from oikonome.web import data
from .util import TODAY, _ensure_db, add_txn, make_db, write_config


def _snapshot(conn) -> dict:
    return {r["id"]: (r["category_primary"], r["category_override"],
                      r["disp"])
            for r in conn.execute(
                f"SELECT t.id, t.category_primary, t.category_override, "
                f"       {merchant_dedup.DISPLAY_MERCHANT} AS disp "
                f"  FROM transactions t {merchant_dedup.MC_JOIN}").fetchall()}


def changed_ids(conn, door) -> set:
    """Run a write door; return the ids whose category or display changed."""
    before = _snapshot(conn)
    door()
    after = _snapshot(conn)
    return {i for i in after if after[i] != before.get(i)}


def displayed_ids(conn, display: str) -> set:
    """What the merchant's page shows — the ledger rows displaying under it."""
    return {r["id"] for r in conn.execute(
        f"SELECT t.id FROM transactions t {merchant_dedup.MC_JOIN} "
        f" WHERE t.removed = 0 AND {merchant_dedup.DISPLAY_MERCHANT} = %s",
        (display,)).fetchall()}


class ScopeInvariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        # the merchant under test, and three neighbours a loose match grabs:
        # a substring ("care" in "healthcare"), a shared word, a shared token
        self.care = [
            add_txn(self.conn, TODAY, 59.0, "CAREWELL.COM 555-010-311SPRINGFIELD",
                    merchant="Care", primary="GENERAL_SERVICES"),
            add_txn(self.conn, TODAY, 39.0, "CAREWELL.COM", merchant="Care",
                    primary="GENERAL_SERVICES"),
        ]
        self.others = [
            add_txn(self.conn, TODAY, -5250.00, "NORTHWIND MFG PAYROLL DDIR",
                    merchant="Northwind Manufacturing", primary="INCOME"),
            add_txn(self.conn, TODAY, 80.0, "BOREALIS HEALTH CARE",
                    merchant="Borealis Health Care", primary="MEDICAL"),
            add_txn(self.conn, TODAY, 120.0, "CAREVANE SYSTEMS DIRECT PAY",
                    merchant="CAREVANE SYSTEMS DIRECT PAY",
                    primary="GENERAL_SERVICES"),
            add_txn(self.conn, TODAY, 12.0, "CARE BEAR TOYS",
                    merchant="Care Bear Toys", primary="GENERAL_MERCHANDISE"),
        ]

    def tearDown(self):
        self.conn.close()

    def test_history_page_bulk_category_writes_the_page_set(self):
        shown = {t["txn_id"] for t in
                 bills.merchant_history(self.conn, "Care")["txns"]}
        self.assertEqual(shown, set(self.care), "the page itself over-reaches")
        preview = data.merchant_category_preview(self.conn, "Care", "CHILD CARE")
        changed = changed_ids(
            self.conn,
            lambda: data.set_merchant_category(self.conn, "Care", "CHILD CARE"))
        self.assertEqual(changed, shown)
        self.assertEqual(preview["count"], len(changed),
                         "the confirm's count must equal what the write did")

    def test_row_apply_to_whole_merchant_writes_the_display_bucket(self):
        changed = changed_ids(
            self.conn,
            lambda: data.set_category(self.conn, self.care[0], "CHILD CARE",
                                      scope="all"))
        self.assertEqual(changed, displayed_ids(self.conn, "Care"))

    def test_row_one_writes_one(self):
        changed = changed_ids(
            self.conn,
            lambda: data.set_category(self.conn, self.care[0], "CHILD CARE",
                                      scope="one"))
        self.assertEqual(changed, {self.care[0]})

    def test_merchant_rename_moves_the_display_bucket_only(self):
        shown = displayed_ids(self.conn, "Care")
        changed = changed_ids(
            self.conn, lambda: merchant_dedup.rename(self.conn, "Care", "Carewell.com"))
        self.assertEqual(changed, shown)

    def test_explicit_selection_writes_exactly_the_selection(self):
        pick = [self.care[1], self.others[3]]
        changed = changed_ids(
            self.conn,
            lambda: data.bulk_apply(self.conn, pick, "category",
                                    category="CHILD CARE"))
        self.assertEqual(changed, set(pick))


if __name__ == "__main__":
    unittest.main()
