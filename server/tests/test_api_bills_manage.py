"""JSON APIs for recurring bill management.

The /bills/* form routes and the /bills/history page, mirrored as
/api/bills/* JSON endpoints over engine/bills.py's CRUD. Amount
convention: positive = money out; income bills store positive amounts in
the bills table.
"""

import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget

from .util import _ensure_db, add_txn, seed_accounts, write_config


def _make_client() -> TestClient:
    import os
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    from oikonome.web.app import app
    return TestClient(app)


class RecurringManageApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = _make_client()
        cls.client.post("/api/signup", data={
            "email": f"rcm-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    # -- helpers ------------------------------------------------------------

    def _cfg(self) -> dict:
        conn = tenancy.tenant_connect(self.tid)
        try:
            return budget.load_config(conn)
        finally:
            conn.close()

    def _row(self, payee):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return conn.execute(
                "SELECT * FROM bills WHERE payee=%s", (payee,)).fetchone()
        finally:
            conn.close()

    # -- save + bill --------------------------------------------------------

    def test_save_creates_bill_with_cap(self):
        r = self.client.post("/api/bills/save", json={
            "payee": "Streamflix", "amount": "15.99", "cadence": "MONTHLY:1",
            "next_due": "2026-08-01", "cap": 2})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True, "payee": "Streamflix"})
        b = self.client.get("/api/bills/bill?payee=Streamflix").json()
        self.assertEqual(b["amount"], 15.99)
        self.assertFalse(b["income"])
        self.assertEqual(b["cadence"], "MONTHLY:1")
        self.assertEqual(b["next_due"], "2026-08-01")
        self.assertEqual(b["bill_type"], "occurrence")
        self.assertEqual(b["source"], "manual")
        self.assertEqual(b["cap"], 2)
        self.assertFalse(b["disabled"])
        self.assertTrue(b["active"])
        self.assertTrue(b["cadences"])           # cadence options for the UI
        # bills store negative amounts (money out) in the bills table
        row = self._row("Streamflix")
        self.assertEqual(row["amount"], -15.99)
        self.assertEqual(row["type"], "BILL")
        # editing with cap="" clears the occurrence cap
        self.client.post("/api/bills/save", json={
            "payee": "Streamflix", "amount": 17.99, "cadence": "MONTHLY:1",
            "cap": ""})
        b = self.client.get("/api/bills/bill?payee=Streamflix").json()
        self.assertEqual(b["amount"], 17.99)
        self.assertIsNone(b["cap"])

    def test_save_income_stores_positive(self):
        self.client.post("/api/bills/save", json={
            "payee": "Acme Payroll", "amount": 2500, "cadence": "WEEKLY:2",
            "income": True})
        row = self._row("Acme Payroll")
        self.assertEqual(row["amount"], 2500)
        self.assertEqual(row["type"], "INCOME")
        b = self.client.get("/api/bills/bill?payee=Acme Payroll").json()
        self.assertTrue(b["income"])
        self.assertEqual(b["cadence"], "WEEKLY:2")

    def test_save_envelope_and_one_time(self):
        self.client.post("/api/bills/save", json={
            "payee": "Coffee Pool", "amount": 80, "cadence": "ENVELOPE:1"})
        b = self.client.get("/api/bills/bill?payee=Coffee Pool").json()
        self.assertEqual(b["bill_type"], "envelope")
        self.assertEqual(b["cadence"], "ENVELOPE:1")
        self.client.post("/api/bills/save", json={
            "payee": "Annual Dues", "amount": 100, "cadence": "ONE_TIME:1",
            "next_due": "2026-12-01"})
        b = self.client.get("/api/bills/bill?payee=Annual Dues").json()
        self.assertEqual(b["cadence"], "ONE_TIME:1")
        self.assertEqual(b["next_due"], "2026-12-01")

    def test_save_validation(self):
        cases = [
            {"payee": "", "amount": 5},                       # no payee
            {"payee": "Junk Amt", "amount": "lots"},          # NaN amount
            {"payee": "Zero Bill", "amount": 0},              # $0 invisible
            {"payee": "Bad Cad", "amount": 5,
             "cadence": "MONTHLY:x"},                         # bad interval
            {"payee": "Bad Freq", "amount": 5,
             "cadence": "HOURLY:1"},                          # unknown freq
            {"payee": "Big Iv", "amount": 5,
             "cadence": "MONTHLY:999999999999"},              # interval range
            {"payee": "Zero Iv", "amount": 5,
             "cadence": "MONTHLY:0"},                         # interval < 1
            {"payee": "Bad Cap", "amount": 5, "cap": "two"},  # NaN cap
            {"payee": "Neg Cap", "amount": 5, "cap": -1},     # negative cap
            {"payee": "Bad Due", "amount": 5,
             "next_due": "garbage"},                          # bad date
        ]
        for body in cases:
            r = self.client.post("/api/bills/save", json=body)
            self.assertEqual(r.status_code, 400, body)
        # a rejected cap must not have half-saved the bill
        self.assertEqual(
            self.client.get("/api/bills/bill?payee=Bad Cap").status_code,
            404)
        # an unknown frequency reaches PER_YEAR[freq] (KeyError=500) only if
        # unvalidated; it must be rejected before the bill is created
        self.assertEqual(
            self.client.get("/api/bills/bill?payee=Bad Freq").status_code,
            404)

    def test_bad_input_on_rename_leaves_original_intact(self):
        # the rename + config migration commit BEFORE save_bill (autocommit,
        # no transaction); every save_bill-validated input must be rejected up
        # front so a failing edit never half-applies the rename (original
        # keeps its name, no partial mutation). Covers freq/amount/next_due.
        bad_edits = [
            {"cadence": "HOURLY:1", "amount": 12},          # unknown freq
            {"cadence": "MONTHLY:1", "amount": -5},         # negative amount
            {"cadence": "MONTHLY:1", "amount": 12,
             "next_due": "not-a-date"},                     # malformed date
        ]
        for i, extra in enumerate(bad_edits):
            orig = f"Keep Me {i}"
            self.client.post("/api/bills/save", json={
                "payee": orig, "amount": 12, "cadence": "MONTHLY:1"})
            r = self.client.post("/api/bills/save", json={
                "payee": f"Renamed {i}", "orig_payee": orig, **extra})
            self.assertEqual(r.status_code, 400, extra)
            # original still there under its old name; no orphaned rename
            self.assertEqual(self.client.get(
                f"/api/bills/bill?payee={orig}").status_code, 200, extra)
            self.assertEqual(self.client.get(
                f"/api/bills/bill?payee=Renamed {i}").status_code,
                404, extra)
        # unknown payee lookups 404
        self.assertEqual(
            self.client.get("/api/bills/bill?payee=Nope").status_code,
            404)

    # -- rename -------------------------------------------------------------

    def test_rename_migrates_payee_keyed_config(self):
        self.client.post("/api/bills/save", json={
            "payee": "Old Gym", "amount": 30, "cadence": "MONTHLY:1",
            "cap": 1})
        self.client.post("/api/bills/toggle", json={"payee": "Old Gym"})
        r = self.client.post("/api/bills/save", json={
            "payee": "New Gym", "orig_payee": "Old Gym", "amount": 30,
            "cadence": "MONTHLY:1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            self.client.get("/api/bills/bill?payee=Old Gym").status_code,
            404)
        b = self.client.get("/api/bills/bill?payee=New Gym").json()
        self.assertEqual(b["cap"], 1)            # cap followed the rename
        self.assertTrue(b["disabled"])           # disabled flag followed too

    def test_rename_onto_existing_bill_is_409(self):
        self.client.post("/api/bills/save", json={
            "payee": "Bill A", "amount": 10, "cadence": "MONTHLY:1"})
        self.client.post("/api/bills/save", json={
            "payee": "Bill B", "amount": 20, "cadence": "MONTHLY:1"})
        r = self.client.post("/api/bills/save", json={
            "payee": "Bill B", "orig_payee": "Bill A", "amount": 99,
            "cadence": "MONTHLY:1"})
        self.assertEqual(r.status_code, 409)
        # the whole save aborted: A intact, B untouched
        self.assertEqual(self.client.get(
            "/api/bills/bill?payee=Bill A").json()["amount"], 10)
        self.assertEqual(self.client.get(
            "/api/bills/bill?payee=Bill B").json()["amount"], 20)

    # -- archive / restore / purge / toggle ----------------------------------

    def test_archive_restore_purge_lifecycle(self):
        self.client.post("/api/bills/save", json={
            "payee": "Temp Sub", "amount": 9.99, "cadence": "MONTHLY:1",
            "cap": 3})
        self.client.post("/api/bills/toggle", json={"payee": "Temp Sub"})
        r = self.client.post("/api/bills/delete",
                             json={"payee": "Temp Sub"})
        self.assertEqual(r.json(), {"ok": True, "mode": "archive",
                                    "affected": 1})
        b = self.client.get("/api/bills/bill?payee=Temp Sub").json()
        self.assertFalse(b["active"])            # soft-removed, restorable
        r = self.client.post("/api/bills/restore",
                             json={"payee": "Temp Sub"})
        self.assertEqual(r.json()["affected"], 1)
        self.assertTrue(self.client.get(
            "/api/bills/bill?payee=Temp Sub").json()["active"])
        # archive keeps the payee-keyed config (restorable) …
        cfg = self._cfg()
        self.assertEqual((cfg.get("occurrence_caps") or {}).get("Temp Sub"), 3)
        # … purge drops the row AND the config leftovers
        self.client.post("/api/bills/delete", json={"payee": "Temp Sub"})
        r = self.client.post("/api/bills/delete",
                             json={"payee": "Temp Sub", "mode": "purge"})
        self.assertEqual(r.json()["affected"], 1)
        self.assertEqual(
            self.client.get("/api/bills/bill?payee=Temp Sub").status_code,
            404)
        cfg = self._cfg()
        self.assertNotIn("Temp Sub", cfg.get("occurrence_caps") or {})
        self.assertNotIn("Temp Sub", cfg.get("disabled_bills") or [])

    def test_toggle_roundtrip(self):
        self.client.post("/api/bills/save", json={
            "payee": "Spotify Duo", "amount": 14.99, "cadence": "MONTHLY:1"})
        r = self.client.post("/api/bills/toggle",
                             json={"payee": "Spotify Duo"})
        self.assertEqual(r.json(), {"ok": True, "payee": "Spotify Duo",
                                    "disabled": True})
        self.assertIn("Spotify Duo", self._cfg().get("disabled_bills") or [])
        r = self.client.post("/api/bills/toggle",
                             json={"payee": "Spotify Duo"})
        self.assertFalse(r.json()["disabled"])
        self.assertNotIn("Spotify Duo",
                         self._cfg().get("disabled_bills") or [])

    def test_payee_required_everywhere(self):
        for path in ("/api/bills/delete", "/api/bills/restore",
                     "/api/bills/toggle", "/api/bills/hint"):
            r = self.client.post(path, json={})
            self.assertEqual(r.status_code, 400, path)
        r = self.client.post("/api/bills/save", json={"amount": 5})
        self.assertEqual(r.status_code, 400)

    # -- history + ledger fit + hint dismissal --------------------------------

    def test_history_hits_fit_and_hint(self):
        today = dt.date.today()
        conn = tenancy.tenant_connect(self.tid)
        try:
            for days in (95, 65, 35, 5):        # clean 30-day cycle
                add_txn(conn, today - dt.timedelta(days=days), 45.0,
                        "GYMBLUE FITNESS", account="chk",
                        merchant="Gymblue Fitness")
            # an off-amount charge from the same merchant (annual fee)
            add_txn(conn, today - dt.timedelta(days=12), 200.0,
                    "GYMBLUE FITNESS", account="chk",
                    merchant="Gymblue Fitness")
        finally:
            conn.close()
        self.client.post("/api/bills/save", json={
            "payee": "Gymblue Fitness", "amount": 45, "cadence": "MONTHLY:1",
            "next_due": (today + dt.timedelta(days=25)).isoformat()})
        r = self.client.get("/api/bills/history",
                            params={"payee": "Gymblue Fitness"})
        self.assertEqual(r.status_code, 200)
        h = r.json()
        self.assertEqual(h["bill"]["amount"], 45)
        self.assertEqual(h["bill"]["cadence"], "MONTHLY:1")
        self.assertFalse(h["bill"]["income"])
        # a quarter of the bill, floored at min($30, half the bill): $22.50
        self.assertEqual(h["bill"]["tol"], 22.5)
        self.assertIsNone(h["covered_by"])
        self.assertEqual(len(h["txns"]), 5)
        # hit = the bill matcher would count it: the $45 rows yes, $200 no
        hits = {t["amount"]: t["hit"] for t in h["txns"]}
        self.assertTrue(hits[45.0])
        self.assertFalse(hits[200.0])
        # history rows are the Transactions-page shape (the SPA
        # renders both through one TxnTable) + the legacy id alias
        for t in h["txns"]:
            for field in ("id", "txn_id", "payee", "category", "account",
                          "reimb", "reimb_flag", "biz_flag", "has_receipt",
                          "recurring_bill", "pending"):
                self.assertIn(field, t)
            self.assertEqual(t["id"], t["txn_id"])
        # the $45 rows match the bill's matcher → stamped with the bill
        by_amt = {t["amount"]: t for t in h["txns"]}
        self.assertEqual(by_amt[45.0]["recurring_bill"], "Gymblue Fitness")
        self.assertIsNone(by_amt[200.0]["recurring_bill"])
        # lifetime aggregate covers ALL matched spend
        self.assertEqual(h["lifetime"]["total"], 380.0)
        self.assertEqual(h["lifetime"]["count"], 5)
        self.assertFalse(h["inflow"])
        # avg/mo over ACTIVE months only, never watered down by empty
        # months in the span. DERIVED from the same offsets the fixture
        # uses rather than pinned to a range: how many distinct months
        # 95/65/35/12/5 days back fall into depends on TODAY, so a
        # date-dependent constant passes most of the year and indicts an
        # innocent diff the rest of it.
        expect_months = len({(today - dt.timedelta(days=d)).strftime("%Y-%m")
                             for d in (95, 65, 35, 12, 5)})
        act = h["lifetime"]["active_months"]
        self.assertEqual(act, expect_months)
        self.assertAlmostEqual(h["lifetime"]["avg_monthly_active"],
                               round(380.0 / act, 2))
        # the ledger fit found the 30-day cycle at the current amount
        self.assertIsNotNone(h["fit"])
        self.assertEqual(h["fit"]["kind"], "occurrence")
        self.assertEqual(h["fit"]["cycle_days"], 30)
        self.assertEqual(h["fit"]["amount"], 45.0)
        self.assertEqual(h["fit"]["cadence"], "MONTHLY:1")
        self.assertEqual(h["health"]["health"], "ok")
        for row in h["monthly"]:
            self.assertEqual(set(row), {"month", "total", "count"})
        # hint dismissal stores the fit signature; restore clears it
        self.assertFalse(h["hint_dismissed"])
        r = self.client.post("/api/bills/hint",
                             json={"payee": "Gymblue Fitness"})
        self.assertEqual(r.json()["dismissed"], True)
        stored = (self._cfg().get("dismissed_hints") or {})["Gymblue Fitness"]
        self.assertEqual(stored["kind"], "occurrence")
        self.assertEqual(stored["amount"], 45.0)
        self.assertTrue(self.client.get(
            "/api/bills/history", params={"payee": "Gymblue Fitness"})
            .json()["hint_dismissed"])
        self.client.post("/api/bills/hint", json={
            "payee": "Gymblue Fitness", "action": "restore"})
        self.assertNotIn("Gymblue Fitness",
                         self._cfg().get("dismissed_hints") or {})
        self.assertFalse(self.client.get(
            "/api/bills/history", params={"payee": "Gymblue Fitness"})
            .json()["hint_dismissed"])

    def test_pending_charge_in_tolerance_is_evidence_not_a_mismatch(self):
        """A yearly bill whose only POSTED charge is last year's different
        price, with this year's charge inside tolerance still PENDING: the
        pending charge counts for health (it settles in days), and the row
        says what the ledger last showed. A renewal pending beside a single
        posted charge at last year's price otherwise reads 'mismatch' with
        no suggestion — the fit needs two posted charges."""
        today = dt.date.today()
        conn = tenancy.tenant_connect(self.tid)
        try:
            add_txn(conn, today - dt.timedelta(days=364), 36.00,
                    "NORTHWIND SOFTWARE", account="chk", merchant="Northwind Software")
        finally:
            conn.close()
        self.client.post("/api/bills/save", json={
            "payee": "Northwind Software", "amount": 84.00, "cadence": "YEARLY:1",
            "next_due": today.isoformat()})
        row = {b["payee"]: b for b in self.client.get("/api/bills").json()["bills"]}["Northwind Software"]
        self.assertEqual(row["health"], "mismatch")
        # …but it says what it saw
        self.assertEqual(row["last_seen"]["amount"], 36.00)
        conn = tenancy.tenant_connect(self.tid)
        try:
            add_txn(conn, today, 84.00, "NORTHWIND SOFTWARE REDMOND",
                    account="chk", merchant="Northwind Software", pending=1)
        finally:
            conn.close()
        row = {b["payee"]: b for b in self.client.get("/api/bills").json()["bills"]}["Northwind Software"]
        self.assertEqual(row["health"], "ok")

    def test_health_flag_dismissal(self):
        # a merchant that is in the ledger but never near the configured
        # amount → mismatch. Dismissing the flag must hide the pill AND
        # take it out of the attention count; restore brings both back.
        today = dt.date.today()
        conn = tenancy.tenant_connect(self.tid)
        try:
            for days in (65, 35, 5):
                add_txn(conn, today - dt.timedelta(days=days), 200.0,
                        "ACME POWER", account="chk", merchant="Acme Power")
        finally:
            conn.close()
        self.client.post("/api/bills/save", json={
            "payee": "Acme Power", "amount": 45, "cadence": "MONTHLY:1",
            "next_due": (today + dt.timedelta(days=25)).isoformat()})
        data = self.client.get("/api/bills").json()
        row = {b["payee"]: b for b in data["bills"]}["Acme Power"]
        self.assertEqual(row["health"], "mismatch")
        self.assertFalse(row["health_dismissed"])
        base_attn = data["totals"]["attention"]
        self.assertGreaterEqual(base_attn, 1)
        r = self.client.post("/api/bills/hint",
                             json={"payee": "Acme Power", "target": "health"})
        self.assertTrue(r.json()["dismissed"])
        self.assertEqual(
            (self._cfg().get("dismissed_health") or {})
            ["Acme Power"]["health"], "mismatch")
        data = self.client.get("/api/bills").json()
        row = {b["payee"]: b for b in data["bills"]}["Acme Power"]
        self.assertTrue(row["health_dismissed"])
        self.assertEqual(data["totals"]["attention"], base_attn - 1)
        self.client.post("/api/bills/hint", json={
            "payee": "Acme Power", "action": "restore", "target": "health"})
        data = self.client.get("/api/bills").json()
        row = {b["payee"]: b for b in data["bills"]}["Acme Power"]
        self.assertFalse(row["health_dismissed"])
        self.assertEqual(data["totals"]["attention"], base_attn)

    def test_disabled_bill_is_not_flagged_as_needing_attention(self):
        """A budget-disabled bill renders no health pill and no dismiss
        control, so counting it in `attention` sends the owner to a row
        with nothing to act on."""
        today = dt.date.today()
        conn = tenancy.tenant_connect(self.tid)
        try:
            for days in (65, 35, 5):
                add_txn(conn, today - dt.timedelta(days=days), 200.0,
                        "ZENITH GAS", account="chk", merchant="Zenith Gas")
        finally:
            conn.close()
        self.client.post("/api/bills/save", json={
            "payee": "Zenith Gas", "amount": 45, "cadence": "MONTHLY:1",
            "next_due": (today + dt.timedelta(days=25)).isoformat()})
        data = self.client.get("/api/bills").json()
        row = {b["payee"]: b for b in data["bills"]}["Zenith Gas"]
        self.assertEqual(row["health"], "mismatch")
        self.assertFalse(row["disabled"])
        base_attn = data["totals"]["attention"]
        self.client.post("/api/bills/toggle", json={"payee": "Zenith Gas"})
        data = self.client.get("/api/bills").json()
        row = {b["payee"]: b for b in data["bills"]}["Zenith Gas"]
        self.assertTrue(row["disabled"])
        self.assertEqual(data["totals"]["attention"], base_attn - 1)

    def test_a_chip_no_merchant_answers_to_is_refused(self):
        """The identity a person types must name a merchant this ledger
        HAS. A name nothing answers to is stored as a ref with no id, and
        an identity whose refs resolve to nothing matches NOTHING — the
        bill would stop being paid while the form showed the chip
        accepted."""
        conn = tenancy.tenant_connect(self.tid)
        try:
            mid = conn.execute(
                "INSERT INTO merchants (name, name_source) VALUES "
                "('Zenith Fibre','manual') RETURNING id::text AS id"
            ).fetchone()["id"]
        finally:
            conn.close()
        r = self.client.post("/api/bills/save", json={
            "payee": "Chip Bill", "amount": 12, "cadence": "MONTHLY:1",
            "merchants": ["Zenith Fibre", "Nowhere Cable"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Nowhere Cable", r.json()["detail"])
        # refused before any mutation, as for a bad cap or date
        self.assertEqual(
            self.client.get("/api/bills/bill?payee=Chip Bill").status_code, 404)
        # the picker's own name in any casing resolves, and the saved bill
        # carries the merchant's id — an identity that matches its rows
        r = self.client.post("/api/bills/save", json={
            "payee": "Chip Bill", "amount": 12, "cadence": "MONTHLY:1",
            "merchants": ["zenith fibre"]})
        self.assertEqual(r.status_code, 200)
        conn = tenancy.tenant_connect(self.tid)
        try:
            row = conn.execute("SELECT payee, merchant, raw FROM bills "
                               "WHERE payee='Chip Bill'").fetchone()
            self.assertEqual(budget.bill_merchant_ids(conn, [row])["Chip Bill"],
                             frozenset({mid}))
        finally:
            conn.close()

    def test_a_mirrored_name_this_instance_lacks_can_still_be_saved(self):
        """A bill that arrived from another instance can carry a ref naming
        a merchant this one has never seen. Editing the amount must not be
        refused because of it — the
        door is shut on chips being ADDED, not on what the bill already
        carries."""
        self.client.post("/api/bills/save", json={
            "payee": "Mirror Bill", "amount": 30, "cadence": "MONTHLY:1"})
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute(
                """UPDATE bills SET raw = raw || %s::jsonb
                    WHERE payee='Mirror Bill'""",
                ('{"merchant_refs": [{"id": null, "name": "Far Instance Co"}],'
                 ' "merchant_names": ["Far Instance Co"]}',))
        finally:
            conn.close()
        r = self.client.post("/api/bills/save", json={
            "payee": "Mirror Bill", "amount": 31, "cadence": "MONTHLY:1",
            "merchants": ["Far Instance Co"]})
        self.assertEqual(r.status_code, 200, r.text)
        b = self.client.get("/api/bills/bill?payee=Mirror Bill").json()
        self.assertEqual(b["merchants"], ["Far Instance Co"])
        # adding a stranger beside it is still refused
        r = self.client.post("/api/bills/save", json={
            "payee": "Mirror Bill", "amount": 31, "cadence": "MONTHLY:1",
            "merchants": ["Far Instance Co", "Nowhere Cable"]})
        self.assertEqual(r.status_code, 400)

    def test_history_covered_by_and_arbitrary_merchant(self):
        self.client.post("/api/bills/save", json={
            "payee": "Zenith Coffee", "amount": 25, "cadence": "WEEKLY:1"})
        # a merchant string already covered by an existing bill's matcher
        h = self.client.get("/api/bills/history",
                            params={"payee": "Zenith Coffee. Cafe"}).json()
        self.assertIsNone(h["bill"])
        self.assertEqual(h["covered_by"], "Zenith Coffee")
        # a totally unknown merchant: empty history, no fit, no bill
        h = self.client.get("/api/bills/history",
                            params={"payee": "Totally Unknown Vendor"}).json()
        self.assertIsNone(h["bill"])
        self.assertIsNone(h["covered_by"])
        self.assertEqual(h["txns"], [])
        self.assertIsNone(h["fit"])
        self.assertEqual(h["lifetime"]["total"], 0.0)


if __name__ == "__main__":
    unittest.main()


class SparseMerchantAvgTests(unittest.TestCase):
    def test_avg_per_month_counts_active_months_only(self):
        # two $60 renewals six months apart
        # average $60/mo over 2 ACTIVE months — a mean over the 7-month
        # span ($17/mo) understates what the merchant costs when it bills
        import datetime as dt

        from oikonome.engine import bills
        from .util import add_txn, make_db, write_config
        conn = make_db()
        try:
            write_config(conn)
            today = dt.date(2026, 7, 15)
            add_txn(conn, dt.date(2026, 1, 10), 60.0, "SPARSE DOMAINS LLC",
                    account="chk")
            add_txn(conn, dt.date(2026, 7, 10), 60.0, "SPARSE DOMAINS LLC",
                    account="chk")
            life = bills.merchant_history(
                conn, "SPARSE DOMAINS LLC", today=today)["lifetime"]
            self.assertEqual(life["total"], 120.0)
            self.assertEqual(life["active_months"], 2)
            self.assertEqual(life["avg_monthly_active"], 60.0)
        finally:
            conn.close()


class HistoryCanonicalGroupingTests(unittest.TestCase):
    """One payee can reach the ledger under three strings — a clean name,
    a feed-truncated one, and (when the aggregator sends NO merchant at
    all) the raw bank descriptor with the store's street address. The
    dedup map already knows they are the same business; matching raw text
    only would show the same merchant as three separate histories, with the
    newest charge looking as though it had stopped grouping with its own
    recurring bill."""

    def test_history_groups_by_canonical_merchant(self):
        import datetime as dt

        from oikonome.engine import bills, merchant_dedup
        from .util import add_txn, make_db, write_config
        conn = make_db()
        try:
            write_config(conn)
            today = dt.date(2026, 7, 25)
            clean = "ZENITH SERVICES"
            trunc = "ZENITH SERVICE"
            raw = "ZENITH SERVICES 100 MAIN ST SPRINGFIELD, IL, US"
            add_txn(conn, dt.date(2026, 5, 10), 32.0, clean, account="chk")
            add_txn(conn, dt.date(2026, 6, 10), 32.0, trunc, account="chk")
            add_txn(conn, dt.date(2026, 7, 10), 32.0, raw, account="chk")
            merchant_dedup.apply(conn)                     # layer1 for all 3
            # the truncated variant is a reviewed merge, as a feed produces
            merchant_dedup.load_llm_map(conn, {trunc: "Zenith Services"})

            # every entry point lands on the SAME 3 charges
            for payee in (clean, raw, "Zenith Services"):
                h = bills.merchant_history(conn, payee, today=today)
                self.assertEqual(len(h["txns"]), 3, payee)
                self.assertEqual(h["lifetime"]["total"], 96.0, payee)
        finally:
            conn.close()

    def test_category_pool_envelope_history_lists_category_rows(self):
        """A match_category envelope owns rows by effective category — its
        history page must show them even though the merchants share no
        tokens with the payee. An occurrence bill's own charge stays off the
        page, but a charge outside that bill's amount tolerance falls
        through to the pool exactly as the engine spends it."""
        import datetime as dt

        from oikonome.engine import bills
        from .util import add_bill, add_txn, make_db, write_config
        conn = make_db()
        try:
            write_config(conn)
            today = dt.date(2026, 7, 25)
            add_bill(conn, "Coffee Pool", 400, bill_type="envelope",
                     category="EDUCATION", match_category=True)
            add_bill(conn, "Zenith Education", 200)
            add_txn(conn, dt.date(2026, 7, 5), 120.0, "FITNESS CLUB DUES",
                    primary="EDUCATION", account="chk")
            add_txn(conn, dt.date(2026, 7, 12), 80.0, "ATM WITHDRAWAL",
                    override="EDUCATION", account="chk")
            # same category, and it IS the Zenith Education bill's own $200 payment
            add_txn(conn, dt.date(2026, 7, 15), 200.0, "ZENITH EDUCATION",
                    primary="EDUCATION", account="chk")
            # a one-off far outside that bill's tolerance: the occurrence
            # matcher never claims it, so the pool pays for it and the pool's
            # history must say so
            add_txn(conn, dt.date(2026, 7, 16), 600.0, "ZENITH EDUCATION",
                    primary="EDUCATION", account="chk")
            # different category — not the envelope's
            add_txn(conn, dt.date(2026, 7, 18), 50.0, "GROCERY MART",
                    primary="FOOD_AND_DRINK", account="chk")
            h = bills.merchant_history(conn, "Coffee Pool", today=today)
            self.assertEqual(sorted(t["payee"] for t in h["txns"]),
                             ["ATM WITHDRAWAL", "FITNESS CLUB DUES",
                              "ZENITH EDUCATION"])
            self.assertEqual([t["amount"] for t in h["txns"]
                              if t["payee"] == "ZENITH EDUCATION"], [600.0])
            # the lifetime figure must keep the category-only rows the
            # list includes — a merchant-token-only match drops them
            self.assertEqual(h["lifetime"]["count"], 3)
            self.assertEqual(h["lifetime"]["total"], 800.0)
        finally:
            conn.close()

    def test_unmapped_merchant_still_matches_on_tokens(self):
        """Fallback intact: a merchant with no canonical row still works."""
        import datetime as dt

        from oikonome.engine import bills
        from .util import add_txn, make_db, write_config
        conn = make_db()
        try:
            write_config(conn)
            today = dt.date(2026, 7, 25)
            add_txn(conn, dt.date(2026, 7, 1), 20.0, "ZZQ UNIQUE PAYEE",
                    account="chk")
            h = bills.merchant_history(conn, "ZZQ UNIQUE PAYEE", today=today)
            self.assertEqual(len(h["txns"]), 1)
        finally:
            conn.close()

    def test_different_merchants_do_not_fuse(self):
        """The grouping must not drag in a genuinely different business."""
        import datetime as dt

        from oikonome.engine import bills, merchant_dedup
        from .util import add_txn, make_db, write_config
        conn = make_db()
        try:
            write_config(conn)
            today = dt.date(2026, 7, 25)
            add_txn(conn, dt.date(2026, 7, 2), 30.0,
                    "ZENITH SERVICES 100 MAIN ST SPRINGFIELD, IL, US",
                    account="chk")
            add_txn(conn, dt.date(2026, 7, 3), 40.0,
                    "ACME PLUMBING 200 OAK AVE SPRINGFIELD, IL, US", account="chk")
            merchant_dedup.apply(conn)
            h = bills.merchant_history(conn, "ZENITH SERVICES", today=today)
            self.assertEqual(len(h["txns"]), 1)
            self.assertEqual(h["txns"][0]["amount"], 30.0)
        finally:
            conn.close()


class HistoryCurrentMonthTests(unittest.TestCase):
    def test_monthly_totals_include_current_month(self):
        # a bill paid THIS month must reach the history page's monthly
        # table, not just the lifetime total and the txn list
        import datetime as dt

        from oikonome.engine import bills
        from .util import add_txn, make_db, write_config
        conn = make_db()
        try:
            write_config(conn)
            today = dt.date(2026, 7, 15)
            add_txn(conn, dt.date(2026, 6, 29), 150.0, "FAMILY CLINIC",
                    account="chk")
            add_txn(conn, dt.date(2026, 7, 8), 150.0, "FAMILY CLINIC",
                    account="chk")
            hist = bills.merchant_history(conn, "FAMILY CLINIC",
                                              today=today)
            months = {m["month"]: m for m in hist["monthly"]}
            self.assertIn("2026-07", months)
            self.assertEqual(months["2026-07"]["total"], 150.0)
            self.assertIn("2026-06", months)
        finally:
            conn.close()
