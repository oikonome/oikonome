"""Restoring the history / reference tables from an export ZIP —
holdings, crypto_holdings, networth_*, income_annual/documents, merchant
maps, amazon_*, liabilities. Fixture CSVs use the archive's encoding
(header row, '' for NULL, 'YYYY-MM-DD HH:MM:SS' timestamps, JSON strings
for raw)."""

import csv
import io
import unittest
import zipfile
from unittest import mock

from oikonome.sync import restore

from .util import _ensure_db, make_db


def _zip(tables: dict[str, tuple[list[str], list[dict]]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for tbl, (cols, rows) in tables.items():
            s = io.StringIO()
            w = csv.DictWriter(s, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({c: ("" if r.get(c) is None else r.get(c))
                            for c in cols})
            z.writestr(f"{tbl}.csv", s.getvalue())
    return buf.getvalue()


V2_ZIP = _zip({
    # the transaction amazon_matches points at. Migration 067 gave the
    # transaction-child tables real FKs, so a backup whose annotation has no
    # transaction is exactly the orphan restore now skips — include it, or
    # this fixture is testing the skip path rather than the restore path.
    "transactions": (["id", "account_id", "date", "amount", "name"], [
        {"id": "tx-1", "account_id": "chk", "date": "2026-07-02",
         "amount": 12.34, "name": "AMAZON"}]),
    "liabilities": (["account_id", "as_of", "raw"], [
        {"account_id": "card", "as_of": "2026-07-01 02:00:00",
         "raw": '{"apr": 24.99}'}]),
    "holdings": (["account_id", "symbol", "name", "quantity", "price",
                  "value", "as_of", "raw"], [
        {"account_id": "chk", "symbol": "VTI", "name": "Vanguard Total",
         "quantity": 10.5, "price": 250.0, "value": 2625.0,
         "as_of": "2026-07-01", "raw": "{}"},
        {"account_id": "chk", "symbol": "FXAIX", "name": "Fidelity 500",
         "quantity": 2.0, "price": 100.0, "value": 200.0,
         "as_of": None, "raw": "{}"}]),
    "crypto_holdings": (["account_id", "currency", "quantity", "native_usd",
                         "as_of", "raw"], [
        {"account_id": "chk", "currency": "BTC", "quantity": 0.25,
         "native_usd": 15000.0, "as_of": "2026-07-01 02:30:00",
         "raw": "{}"}]),
    "networth_recorded": (["month", "total", "source"], [
        {"month": "2020-01", "total": 123456.78, "source": "import"}]),
    "networth_snapshot": (["date", "total", "by_class"], [
        {"date": "2026-07-01", "total": 200000.0,
         "by_class": '{"cash": 5000}'}]),
    "income_annual": (["year", "total_income", "agi", "taxable_income",
                       "tax_paid", "wages", "primary_wages",
                       "investment_income", "capital_gain", "spouse_wages",
                       "ss_earnings", "medicare_earnings", "filing_status",
                       "joint", "source", "note"], [
        {"year": 2020, "total_income": 100000, "agi": 95000,
         "wages": 90000, "ss_earnings": 90000, "capital_gain": -500.0,
         "filing_status": "MFJ", "joint": 1, "source": "return"}]),
    "income_documents": (["id", "year", "form", "owner", "payer", "ein",
                          "primary_amount", "amounts", "notes"], [
        {"id": 7, "year": 2020, "form": "W-2", "owner": "primary",
         "payer": "ACME", "primary_amount": 90000,
         "amounts": '{"box1": 90000}'}]),
    "merchant_canonical": (["raw_merchant", "canonical", "method", "as_of"], [
        {"raw_merchant": "SQ *COFFEE 123", "canonical": "Coffee Shop",
         "method": "layer1", "as_of": "2026-07-08 12:00:00"}]),
    "merchant_categories": (["merchant", "category_primary", "source",
                             "classified_at"], [
        {"merchant": "Coffee Shop", "category_primary": "FOOD_AND_DRINK",
         "source": "llm", "classified_at": None}]),
    "amazon_orders": (["dedup_key", "account", "date", "amount", "payee",
                       "seller", "memo", "category", "category_source",
                       "order_number", "is_refund", "payment_method",
                       "items_json", "inserted_at"], [
        {"dedup_key": "ord-1", "account": "main", "date": "2026-07-01",
         "amount": 25.99, "payee": "Amazon", "seller": "", "memo": "",
         "category": "Household", "category_source": "rules",
         "order_number": "111-222", "is_refund": 0, "payment_method": "",
         "items_json": '[{"name": "widget"}]',
         "inserted_at": "2026-07-02 03:00:00"}]),
    "amazon_matches": (["transaction_id", "dedup_key", "matched_at"], [
        {"transaction_id": "tx-1", "dedup_key": "ord-1",
         "matched_at": "2026-07-02 03:05:00"}]),
    "amazon_summaries": (["dedup_key", "summary", "created_at"], [
        {"dedup_key": "ord-1", "summary": "a widget",
         "created_at": "2026-07-02 03:10:00"}]),
})

EXPECT = {"liabilities": 1, "holdings": 2, "crypto_holdings": 1,
          "networth_recorded": 1, "networth_snapshot": 1,
          "income_annual": 1, "income_documents": 1,
          "merchant_canonical": 1, "merchant_categories": 1,
          "amazon_orders": 1, "amazon_matches": 1, "amazon_summaries": 1}


class RestoreV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _count(self, tbl):
        # merchant_canonical: the post-restore merchant resolve adds layer1
        # alias rows for the restored transactions' raw strings — those are
        # derived, not restored, so the round-trip count excludes them
        # (restored rows keep the ZIP's as_of; derived ones are stamped now())
        where = " WHERE as_of < now() - interval '1 hour'" \
            if tbl == "merchant_canonical" else ""
        return self.conn.execute(
            f"SELECT COUNT(*) AS n FROM {tbl}{where}").fetchone()["n"]

    def test_v2_tables_restore_and_are_idempotent(self):
        counts = restore.restore_zip(self.conn, V2_ZIP)
        for tbl, n in EXPECT.items():
            self.assertEqual(counts.get(tbl), n, tbl)
            self.assertEqual(self._count(tbl), n, tbl)
        # re-restore: ON CONFLICT DO NOTHING on the original keys
        restore.restore_zip(self.conn, V2_ZIP)
        for tbl, n in EXPECT.items():
            self.assertEqual(self._count(tbl), n, tbl)

    def test_values_survive(self):
        restore.restore_zip(self.conn, V2_ZIP)
        h = self.conn.execute("SELECT * FROM holdings WHERE symbol='VTI'"
                              ).fetchone()
        self.assertEqual(h["quantity"], 10.5)
        self.assertEqual(h["value"], 2625.0)
        self.assertEqual(str(h["as_of"])[:10], "2026-07-01")
        # NULL as_of stays NULL on the nullable column
        self.assertIsNone(self.conn.execute(
            "SELECT as_of FROM holdings WHERE symbol='FXAIX'"
        ).fetchone()["as_of"])
        ns = self.conn.execute("SELECT * FROM networth_snapshot").fetchone()
        self.assertEqual(ns["by_class"], {"cash": 5000})
        self.assertEqual(str(ns["date"]), "2026-07-01")
        ia = self.conn.execute("SELECT * FROM income_annual").fetchone()
        self.assertEqual((ia["year"], ia["joint"], ia["filing_status"]),
                         (2020, 1, "MFJ"))
        self.assertEqual(ia["capital_gain"], -500.0)
        doc = self.conn.execute("SELECT * FROM income_documents").fetchone()
        self.assertEqual(doc["id"], 7)                  # original id kept
        self.assertEqual(doc["amounts"], {"box1": 90000})
        o = self.conn.execute("SELECT * FROM amazon_orders").fetchone()
        self.assertEqual(o["amount"], 25.99)
        self.assertEqual(o["items_json"], [{"name": "widget"}])
        self.assertEqual(o["is_refund"], 0)
        # empty classified_at cell falls back to now(), never NULL
        self.assertIsNotNone(self.conn.execute(
            "SELECT classified_at FROM merchant_categories").fetchone()
            ["classified_at"])
        li = self.conn.execute("SELECT * FROM liabilities").fetchone()
        self.assertEqual(li["raw"], {"apr": 24.99})

    def test_tenant_isolation(self):
        restore.restore_zip(self.conn, V2_ZIP)
        other = make_db()
        try:
            for tbl in EXPECT:
                n = other.execute(
                    f"SELECT COUNT(*) AS n FROM {tbl}").fetchone()["n"]
                self.assertEqual(n, 0, f"{tbl} leaked across tenants")
        finally:
            other.close()

    def test_partial_reimbursement_columns_survive(self):
        """A $50-back-of-$200 pair must keep partial=1 / amount=50;
        otherwise NET_AMOUNT treats the charge as full personal spend."""
        z = _zip({
            "transactions": (["id", "account_id", "date", "amount", "name"], [
                {"id": "e1", "account_id": "chk", "date": "2026-07-01",
                 "amount": 200, "name": "HOTEL"},
                {"id": "r1", "account_id": "chk", "date": "2026-07-05",
                 "amount": -50, "name": "EMPLOYER"}]),
            "reimbursements": (["expense_id", "reimburse_id", "partial",
                                "amount"], [
                {"expense_id": "e1", "reimburse_id": "r1",
                 "partial": 1, "amount": 50}]),
            "reimburse_flags": (["txn_id", "partial", "expected"], [
                {"txn_id": "e1", "partial": 1, "expected": 50}]),
        })
        restore.restore_zip(self.conn, z)
        row = self.conn.execute(
            "SELECT partial, amount FROM reimbursements").fetchone()
        self.assertEqual((row["partial"], row["amount"]), (1, 50.0))
        flag = self.conn.execute(
            "SELECT partial, expected FROM reimburse_flags").fetchone()
        self.assertEqual((flag["partial"], flag["expected"]), (1, 50.0))

    def _override(self, conn, tid):
        return conn.execute(
            "SELECT category_override FROM transactions WHERE id=%s",
            (tid,)).fetchone()["category_override"]

    def test_hand_set_transfer_survives_unlink_after_a_restore(self):
        """A transfer the person set by hand before linking stays after the
        link is undone — on the restored copy too. Unlink tells a hand pin
        from a link-made one by comparing when each was made, so restore
        must carry both times rather than stamp them alike."""
        from oikonome.sync import export
        from oikonome.web import data

        from .util import TODAY, add_txn
        exp = add_txn(self.conn, TODAY, 200.0, "OFFICE SUPPLY CO")
        dep = add_txn(self.conn, TODAY, -200.0, "EMPLOYER REIMB",
                      account="chk", primary="INCOME")
        data.set_category(self.conn, dep, "TRANSFER_IN")
        data.link_reimbursement(self.conn, exp, dep)
        dest = make_db()
        try:
            restore.restore_zip(dest, export.build_zip(self.conn))
            self.assertEqual(self._override(dest, dep), "TRANSFER_IN")
            data.unlink_reimbursement(dest, exp, dep)
            self.assertEqual(self._override(dest, dep), "TRANSFER_IN")
            self.assertIsNone(self._override(dest, exp))
        finally:
            dest.close()

    def test_undated_archive_never_makes_a_hand_pin_look_link_made(self):
        """An archive whose pins and links carry no times: the links are
        dated after the pins, so an unlink keeps the pin."""
        from oikonome.web import data
        z = _zip({
            "transactions": (["id", "account_id", "date", "amount", "name",
                              "category_primary", "category_override"], [
                {"id": "e1", "account_id": "card", "date": "2026-07-01",
                 "amount": 200, "name": "HOTEL",
                 "category_primary": "TRAVEL",
                 "category_override": "TRANSFER_OUT"},
                {"id": "r1", "account_id": "chk", "date": "2026-07-05",
                 "amount": -200, "name": "EMPLOYER",
                 "category_primary": "INCOME",
                 "category_override": "TRANSFER_IN"}]),
            "manual_categories": (["transaction_id", "category"], [
                {"transaction_id": "r1", "category": "TRANSFER_IN"}]),
            "reimbursements": (["expense_id", "reimburse_id", "partial",
                                "amount"], [
                {"expense_id": "e1", "reimburse_id": "r1", "partial": 0}]),
        })
        restore.restore_zip(self.conn, z)
        data.unlink_reimbursement(self.conn, "e1", "r1")
        self.assertEqual(self._override(self.conn, "r1"), "TRANSFER_IN")

    def test_disabled_merchant_rule_stays_disabled(self):
        z = _zip({
            "merchant_categories": (
                ["merchant", "category_primary", "source", "disabled"], [
                    {"merchant": "Netflix",
                     "category_primary": "ENTERTAINMENT",
                     "source": "user", "disabled": "true"}]),
        })
        restore.restore_zip(self.conn, z)
        row = self.conn.execute(
            "SELECT disabled FROM merchant_categories "
            "WHERE merchant='Netflix'").fetchone()
        self.assertTrue(row["disabled"])

    def test_row_cap_blocks_oom_zip(self):
        """A member with an absurd row count (the OOM-DoS vector) is
        refused before it can materialize — streamed + hard-capped. Patch the
        cap low so we don't have to build millions of rows."""
        cols = ["id", "account_id", "date", "amount", "name"]
        rows = [{"id": str(i), "account_id": "a", "date": "2025-01-01",
                 "amount": "1", "name": "x"} for i in range(6)]
        blob = _zip({"transactions": (cols, rows)})
        # cap 0 → the generator raises before yielding ANY row, so nothing is
        # inserted (proves the cap fires before materialization/DB work)
        with mock.patch.object(restore, "MAX_ROWS_PER_MEMBER", 0):
            with self.assertRaises(ValueError):
                restore.restore_zip(self.conn, blob)

    def test_rows_streams_without_materializing(self):
        """_rows is a lazy generator now, not a list — proves we don't
        list()-materialize a whole member."""
        import types
        z = zipfile.ZipFile(io.BytesIO(V2_ZIP))
        self.assertIsInstance(restore._rows(z, "accounts.csv"),
                              types.GeneratorType)


if __name__ == "__main__":
    unittest.main()
