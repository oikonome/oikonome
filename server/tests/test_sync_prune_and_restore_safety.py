"""Sync invariants: a non-full-replace prune must not retire rows it never
saw, and neither must a restore leave half a ZIP behind.

A prune scoped to each account's min..max pushed date retires every row
between them, so a sparse push (two anchors years apart — a collector
glitch, a filtered portal view) mass-wipes the history in between
(coinbase soft-removes, the plan door hard-DELETEd). Prune scope is the DATES
actually restated, per account.

assert_token_importable must refuse push-fed aggregators as well as live
pull ones: a script token that can CSV-import into a plan account or a tokenless
coinbase account pollutes rows those push scripts restate.

MX's default 30-day window on the very first pull silently discards the
deeper history MX serves. A first sync (no mx: rows) reaches back 2 years;
refreshes stay 30d.

restore_zip run row-by-row on an autocommit connection leaves a partial
restore behind when the ZIP fails deep in. The write section is one
transaction — all or nothing.
"""

import csv
import datetime as dt
import io
import unittest
import zipfile

import httpx

from oikonome.db import crypto
from oikonome.engine import budget
from oikonome.sync import base as sync_base
from oikonome.sync import coinbase_push, mx, plan_csv, restore

from .util import make_db, write_config

TODAY = dt.date.today()
OLD = TODAY - dt.timedelta(days=700)
MID = TODAY - dt.timedelta(days=350)


def _payload(transactions):
    return {
        "items": [{"id": "coinbase-test", "institution_name": "Coinbase"}],
        "accounts": [{"id": "coinbase-test", "item_id": "coinbase-test",
                      "name": "Coinbase (test)", "type": "investment",
                      "subtype": "crypto", "balance_current": 10.0}],
        "transactions": transactions,
    }


def _t(tid, date, amount=1.0):
    return {"id": tid, "account_id": "coinbase-test",
            "date": date.isoformat(), "amount": amount, "name": tid}


class CoinbaseSparsePushTests(unittest.TestCase):
    """A sparse push (anchors years apart) must not retire the history
    between them."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        coinbase_push.import_payload(self.conn, _payload(
            [_t("coinbase:t-old", OLD), _t("coinbase:t-mid", MID),
             _t("coinbase:t-new", TODAY)]))

    def tearDown(self):
        self.conn.close()

    def _live(self):
        return {r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions WHERE account_id='coinbase-test' "
            "AND removed=0").fetchall()}

    def test_sparse_anchors_keep_history_between(self):
        """Restating two far-apart dates must not wipe everything in
        between."""
        out = coinbase_push.import_payload(self.conn, _payload(
            [_t("coinbase:t-old", OLD), _t("coinbase:t-new", TODAY)]))
        self.assertEqual(out["stale_removed"], 0)
        self.assertIn("coinbase:t-mid", self._live())

    def test_restated_date_still_prunes(self):
        # a row that vanished from a date the push DOES restate is stale
        out = coinbase_push.import_payload(self.conn, _payload(
            [_t("coinbase:t-mid2", MID)]))
        self.assertEqual(out["stale_removed"], 1)
        live = self._live()
        self.assertNotIn("coinbase:t-mid", live)
        self.assertIn("coinbase:t-old", live)
        self.assertIn("coinbase:t-new", live)


def _plan_csv(rows):
    head = ('"Trade Date","Investments","Ticker","Transaction",'
            '"Transaction Amount","Share Price","Total shares"\n')
    return head + "".join(
        f'"{d}","Fund X","FX","{typ}","{amt}","10.00","{sh}"\n'
        for d, typ, amt, sh in rows)


class PlanSparseCsvTests(unittest.TestCase):
    """A sparse plan CSV must not hard-delete the history between its
    anchor dates."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        plan_csv.import_csv(self.conn, "403B", _plan_csv([
            ("01/15/2026", "Contribution", "100.00", "10.000"),
            ("03/15/2026", "Contribution", "100.00", "10.000"),
            ("07/01/2026", "Contribution", "100.00", "10.000")]))

    def tearDown(self):
        self.conn.close()

    def _dates(self):
        return [str(r["date"]) for r in self.conn.execute(
            "SELECT date FROM transactions WHERE account_id='plan-403b' "
            "AND removed=0 ORDER BY date").fetchall()]

    def test_sparse_csv_keeps_history_between(self):
        """The plan path hard-DELETEd, so the loss was permanent."""
        plan_csv.import_csv(self.conn, "403B", _plan_csv([
            ("01/15/2026", "Contribution", "100.00", "10.000"),
            ("07/01/2026", "Contribution", "100.00", "10.000")]))
        self.assertEqual(self._dates(),
                         ["2026-01-15", "2026-03-15", "2026-07-01"])

    def test_restated_date_still_replaced(self):
        plan_csv.import_csv(self.conn, "403B", _plan_csv([
            ("03/15/2026", "Dividend", "5.00", "0.500")]))
        # live view: only the restated row (the stale one is soft-retired
        # since, not hard-deleted)
        rows = self.conn.execute(
            "SELECT name FROM transactions WHERE account_id='plan-403b' "
            "AND date='2026-03-15' AND removed=0").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("Dividend", rows[0]["name"])


class TokenImportGuardTests(unittest.TestCase):
    """Script tokens may not file-import into push-fed accounts."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, "
            "access_token) VALUES "
            "('plan','plan_csv','Workplace plan','plan:csv'),"
            "('coinbase-main','coinbase','Coinbase',NULL),"
            "('manual','manual','Manual',NULL) "
            "ON CONFLICT (tenant_id, id) DO NOTHING")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type) VALUES "
            "('plan-403b','plan','Workplace plan 403(b)','investment'),"
            "('coinbase-test','coinbase-main','Coinbase','investment'),"
            "('manual:box','manual','Shoebox','depository') "
            "ON CONFLICT (tenant_id, id) DO NOTHING")

    def tearDown(self):
        self.conn.close()

    def test_plan_account_refused(self):
        with self.assertRaises(PermissionError):
            sync_base.assert_token_importable(self.conn, "plan-403b")

    def test_tokenless_coinbase_refused(self):
        """Push-fed coinbase (no CDP token) must be refused too."""
        with self.assertRaises(PermissionError):
            sync_base.assert_token_importable(self.conn, "coinbase-test")

    def test_manual_still_allowed(self):
        sync_base.assert_token_importable(self.conn, "manual:box")


class MxDeepFirstSyncTests(unittest.TestCase):
    """First MX pull reaches deep; refreshes stay 30 days."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        cfg = budget.load_config(self.conn)
        cfg["mx_client_id"] = "cid"
        cfg["mx_api_key"] = crypto.encrypt(self.conn, "key")
        cfg["mx_env"] = "sandbox"
        budget.save_config(self.conn, cfg)
        self.from_dates: list[str] = []
        payload = {
            "GET /users": {"users": [], "pagination": {"total_pages": 1}},
            "POST /users": {"user": {"guid": "USR-1"}},
            "GET /users/USR-1/members": {
                "members": [{"guid": "MBR-1", "name": "Demo CU"}],
                "pagination": {"total_pages": 1}},
            "GET /users/USR-1/accounts": {"accounts": [
                {"guid": "ACT-1", "member_guid": "MBR-1", "name": "Checking",
                 "type": "CHECKING", "balance": 1000.0,
                 "currency_code": "USD"}],
                "pagination": {"total_pages": 1}},
            "GET /users/USR-1/transactions": {"transactions": [
                {"guid": "TRN-1", "account_guid": "ACT-1", "amount": 12.5,
                 "type": "DEBIT", "transacted_at": "2026-07-10T12:00:00Z",
                 "description": "COFFEE", "status": "POSTED"}],
                "pagination": {"total_pages": 1}},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            key = f"{request.method} {request.url.path}"
            if key == "GET /users/USR-1/transactions":
                self.from_dates.append(request.url.params["from_date"])
            body = payload.get(key)
            if body is None:
                return httpx.Response(404, json={"missing": key})
            return httpx.Response(200, json=body)

        self.transport = httpx.MockTransport(handler)

    def tearDown(self):
        self.conn.close()

    def test_first_sync_deep_then_30d_refresh(self):
        mx.sync(self.conn, transport=self.transport)
        first = dt.date.fromisoformat(self.from_dates[0])
        self.assertLess(first, dt.date.today() - dt.timedelta(days=600))
        mx.sync(self.conn, transport=self.transport)   # mx: rows exist now
        second = dt.date.fromisoformat(self.from_dates[1])
        self.assertEqual(second, dt.date.today() - dt.timedelta(days=30))


class AtomicRestoreTests(unittest.TestCase):
    """A failure deep in the ZIP must leave NOTHING behind."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_partial_restore_rolls_back(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            s = io.StringIO()
            w = csv.DictWriter(s, fieldnames=["id", "account_id", "date",
                                              "amount", "name"])
            w.writeheader()
            w.writerow({"id": "zt1", "account_id": "chk",
                        "date": "2026-07-01", "amount": "10", "name": "ok"})
            w.writerow({"id": "zt2", "account_id": "chk",
                        "date": "not-a-date", "amount": "5", "name": "bad"})
            z.writestr("transactions.csv", s.getvalue())
        with self.assertRaises(Exception):
            restore.restore_zip(self.conn, buf.getvalue())
        row = self.conn.execute(
            "SELECT 1 FROM transactions WHERE id='zt1'").fetchone()
        self.assertIsNone(row, "partial restore leaked past the failure")


if __name__ == "__main__":
    unittest.main()
