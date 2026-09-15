"""Guards on the push doors and the file importers: what a restatement may
delete, how an account created at import time is typed, which aggregator a
pushed item claims, what a script token may write, and how multi-account
exports split. Synthetic fixtures only.
"""

import datetime as dt
import unittest

from oikonome.sync import coinbase_push

from .util import make_db, write_config

TODAY = dt.date.today()


def _payload(**over) -> dict:
    """A legit collector payload (mirrors community-scripts/coinbase)."""
    p = {
        "items": [{"id": "coinbase-test", "institution_name": "Coinbase"}],
        "accounts": [{"id": "coinbase-test", "item_id": "coinbase-test",
                      "name": "Coinbase (test)", "type": "investment",
                      "subtype": "crypto", "balance_current": 1234.56}],
        "transactions": [
            {"id": "coinbase:t-old", "account_id": "coinbase-test",
             "date": (TODAY - dt.timedelta(days=90)).isoformat(),
             "amount": 100.0, "name": "BUY BTC"},
            {"id": "coinbase:t-new", "account_id": "coinbase-test",
             "date": TODAY.isoformat(), "amount": -25.0, "name": "SELL ETH"},
        ],
    }
    p.update(over)
    return p


class CoinbaseEmptyRestatementTests(unittest.TestCase):
    """An empty or partial restatement must never soft-delete history
    outside the restated window; a whole-account restatement needs an
    explicit full_replace flag."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        # seed history through the door itself
        out = coinbase_push.import_payload(self.conn, _payload())
        self.assertEqual(out["accounts"]["coinbase-test"]["after"], 2)

    def tearDown(self):
        self.conn.close()

    def _live(self):
        return {r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions WHERE account_id='coinbase-test' "
            "AND removed=0").fetchall()}

    def test_empty_transactions_removes_nothing(self):
        # the wipe: valid accounts + transactions[] == [] used to retire
        # ALL coinbase: rows on those accounts
        out = coinbase_push.import_payload(
            self.conn, _payload(transactions=[]))
        self.assertEqual(out["stale_removed"], 0)
        self.assertEqual(self._live(), {"coinbase:t-old", "coinbase:t-new"})

    def test_partial_window_only_prunes_inside_the_window(self):
        # restate only the recent window (one new row today): the 90-day-old
        # row is OUTSIDE the pushed window and must survive; the pushed-window
        # row that disappeared (t-new) is retired.
        p = _payload(transactions=[
            {"id": "coinbase:t-new2", "account_id": "coinbase-test",
             "date": TODAY.isoformat(), "amount": 5.0, "name": "BUY SOL"}])
        out = coinbase_push.import_payload(self.conn, p)
        self.assertEqual(out["stale_removed"], 1)
        self.assertEqual(self._live(), {"coinbase:t-old", "coinbase:t-new2"})

    def test_full_replace_retires_everything_absent(self):
        # the collector always pushes its complete current view and says so
        p = _payload(full_replace=True, transactions=[
            {"id": "coinbase:t-new", "account_id": "coinbase-test",
             "date": TODAY.isoformat(), "amount": -25.0, "name": "SELL ETH"}])
        out = coinbase_push.import_payload(self.conn, p)
        self.assertEqual(out["stale_removed"], 1)
        self.assertEqual(self._live(), {"coinbase:t-new"})

    def test_full_replace_empty_is_an_explicit_wipe(self):
        out = coinbase_push.import_payload(
            self.conn, _payload(full_replace=True, transactions=[]))
        self.assertEqual(out["stale_removed"], 2)
        self.assertEqual(self._live(), set())

    def test_window_is_per_account(self):
        # two accounts in one push; only account A restates transactions —
        # account B's history must be untouched even inside A's window
        p = _payload()
        p["items"].append({"id": "coinbase-b", "institution_name": "Coinbase"})
        p["accounts"].append({"id": "coinbase-b", "item_id": "coinbase-b",
                              "name": "Coinbase (b)", "type": "investment",
                              "balance_current": 10.0})
        p["transactions"].append(
            {"id": "coinbase:b1", "account_id": "coinbase-b",
             "date": TODAY.isoformat(), "amount": 1.0, "name": "BUY DOGE"})
        coinbase_push.import_payload(self.conn, p)
        # restate ONLY account coinbase-test's window (same day as b1)
        p2 = _payload(transactions=[
            {"id": "coinbase:t-new", "account_id": "coinbase-test",
             "date": TODAY.isoformat(), "amount": -25.0, "name": "SELL ETH"}])
        p2["items"] = p["items"]
        p2["accounts"] = p["accounts"]
        out = coinbase_push.import_payload(self.conn, p2)
        self.assertEqual(out["stale_removed"], 0)
        b_live = {r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions WHERE account_id='coinbase-b' "
            "AND removed=0").fetchall()}
        self.assertEqual(b_live, {"coinbase:b1"})


class BulkEnsureAccountTypeTests(unittest.TestCase):
    """Accounts created at import time infer their type from the name
    instead of always depository/checking — a card account typed depository
    breaks the credit sign/exclusion math."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _type(self, aid):
        r = self.conn.execute(
            "SELECT type, subtype FROM accounts WHERE id=%s",
            (aid,)).fetchone()
        return r["type"], r["subtype"]

    def test_card_named_account_types_credit(self):
        from oikonome.web.bulk_import import _ensure_account
        aid = _ensure_account(self.conn, "Chase Sapphire Card")
        self.assertEqual(self._type(aid), ("credit", "credit card"))

    def test_checking_wins_over_credit_union(self):
        from oikonome.web.bulk_import import _ensure_account
        aid = _ensure_account(self.conn, "First Credit Union Checking")
        self.assertEqual(self._type(aid), ("depository", "checking"))

    def test_savings_and_default(self):
        from oikonome.web.bulk_import import _ensure_account
        aid = _ensure_account(self.conn, "Acme Savings")
        self.assertEqual(self._type(aid), ("depository", "savings"))
        aid = _ensure_account(self.conn, "Mystery Import")
        self.assertEqual(self._type(aid), ("depository", "checking"))


class HonestAggregatorStampTests(unittest.TestCase):
    """Push doors stamp their real source — coinbase/plan_csv, not 'csv' —
    without ever landing in the worker's hourly pull loop."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_coinbase_push_items_stamp_coinbase(self):
        coinbase_push.import_payload(self.conn, _payload())
        row = self.conn.execute(
            "SELECT aggregator, status, access_token FROM items "
            "WHERE id='coinbase-test'").fetchone()
        self.assertEqual(row["aggregator"], "coinbase")
        # push-fed: out of the hourly pull loop + stale-connection checks
        # (freshness is the script heartbeat's job), like the plan door
        self.assertEqual(row["status"], "archived")
        self.assertIsNone(row["access_token"])

    def test_plan_import_stamps_plan_csv(self):
        from oikonome.sync import plan_csv
        csv_text = (
            '"Trade Date","Investments","Ticker","Transaction",'
            '"Transaction Amount","Share Price","Total shares"\n'
            '"07/01/2026","Fund X","FX","Contribution","100.00",'
            '"10.00","10.000"\n')
        plan_csv.import_csv(self.conn, "403B", csv_text)
        row = self.conn.execute(
            "SELECT aggregator, status FROM items "
            "WHERE id='plan'").fetchone()
        self.assertEqual(row["aggregator"], "plan_csv")
        self.assertEqual(row["status"], "archived")

    def test_worker_never_pulls_tokenless_coinbase_items(self):
        # a restored export recreates a push-fed coinbase item WITHOUT the
        # archived status ('restored') — the hourly loop must still skip
        # it (there are no credentials to pull with)
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, status) "
            "VALUES ('coinbase-main','coinbase','Coinbase','restored')")
        tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        from oikonome.jobs import worker
        results = worker.sync_tenant(tid)
        self.assertNotIn("coinbase-main", results)
        status = self.conn.execute(
            "SELECT status FROM items WHERE id='coinbase-main'"
        ).fetchone()["status"]
        self.assertEqual(status, "restored")   # untouched, no error stamp

    def test_migration_rewrites_existing_push_items(self):
        # the data migration re-stamps rows the old doors created
        from pathlib import Path

        from oikonome.db import migrate
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, status, "
            "                   access_token) "
            "VALUES ('coinbase-old','csv','Coinbase','restored',NULL),"
            # the plan door marked its item with a '<provider>:scrape' token
            "       ('plan','csv','Workplace plan','archived','plan:scrape'),"
            "       ('filebank','csv','File Bank','restored',NULL)")
        sql = (Path(migrate.HERE) / "migrations"
               / "022_honest_push_aggregators.sql").read_text()
        self.conn.execute(sql)
        rows = {r["id"]: (r["aggregator"], r["status"]) for r in
                self.conn.execute(
                    "SELECT id, aggregator, status FROM items").fetchall()}
        self.assertEqual(rows["coinbase-old"], ("coinbase", "archived"))
        self.assertEqual(rows["plan"], ("plan_csv", "archived"))
        # a genuine file-import item is untouched
        self.assertEqual(rows["filebank"], ("csv", "restored"))


PLAN_CSV = (
    '"Trade Date","Investments","Ticker","Transaction",'
    '"Transaction Amount","Share Price","Total shares"\n'
    '"07/01/2026","Fund X","FX","Contribution","100.00","10.00","10.000"\n'
    '"07/03/2026","Fund X","FX","Fee","-1.50","10.00","-0.150"\n')


class PlanOverrideSurvivesReplaceTests(unittest.TestCase):
    """A window-replace that hard-DELETEs and re-inserts every row in the
    CSV's span kills a user's category_override on those rows with every
    nightly push. The replace must be a restatement, not a rewrite."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_override_survives_same_window_repush(self):
        from oikonome.sync import plan_csv
        plan_csv.import_csv(self.conn, "403B", PLAN_CSV)
        tid = self.conn.execute(
            "SELECT id FROM transactions WHERE account_id='plan-403b' "
            "ORDER BY date LIMIT 1").fetchone()["id"]
        # override the way web/data.py does (column + manual_categories)
        self.conn.execute(
            "UPDATE transactions SET category_override='INCOME' WHERE id=%s",
            (tid,))
        self.conn.execute(
            "INSERT INTO manual_categories (transaction_id, category) "
            "VALUES (%s,'INCOME')", (tid,))
        out = plan_csv.import_csv(self.conn, "403B", PLAN_CSV)
        self.assertEqual(out["inserted"], 2)
        row = self.conn.execute(
            "SELECT category_override FROM transactions WHERE id=%s",
            (tid,)).fetchone()
        self.assertIsNotNone(row, "row must survive the re-push")
        self.assertEqual(row["category_override"], "INCOME")

    def test_stale_rows_inside_the_window_still_replaced(self):
        from oikonome.sync import plan_csv
        plan_csv.import_csv(self.conn, "403B", PLAN_CSV)
        # restate the window without the fee row: it must go away
        one_row = "\n".join(PLAN_CSV.splitlines()[:2]) + "\n"
        # widen the window to cover the old span so the fee row is stale
        two_dates = one_row + (
            '"07/03/2026","Fund X","FX","Contribution","5.00",'
            '"10.00","0.500"\n')
        plan_csv.import_csv(self.conn, "403B", two_dates)
        # live view: the fee row is gone (soft-retired since —
        # removed=1, still on disk so a later CSV can revive it)
        names = [r["name"] for r in self.conn.execute(
            "SELECT name FROM transactions WHERE account_id='plan-403b' "
            "AND removed=0 ORDER BY date, name").fetchall()]
        self.assertEqual(len(names), 2)
        self.assertFalse([n for n in names if "Fee" in n])
        retired = self.conn.execute(
            "SELECT name FROM transactions WHERE account_id='plan-403b' "
            "AND removed=1").fetchall()
        self.assertEqual(len(retired), 1)
        self.assertIn("Fee", retired[0]["name"])


CSV = b"Date,Description,Amount\n2026-07-01,CORNER GROCER,-42.50\n"


class TokenImportGuardTests(unittest.TestCase):
    """A script token may push files only into manual or import-fed
    accounts — never into one owned by a live pull aggregator."""

    @classmethod
    def setUpClass(cls):
        import os
        import uuid

        from fastapi.testclient import TestClient

        from oikonome.db import tenancy

        from .util import _ensure_db, seed_accounts

        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"tig-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            conn.execute(
                "INSERT INTO items (id, aggregator, institution_name) "
                "VALUES ('sfin-main','simplefin','SFIN Bank'),"
                "       ('manual','manual','Manual') "
                "ON CONFLICT (tenant_id, id) DO NOTHING")
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type) VALUES "
                "('sfin:acct1','sfin-main','SFIN Checking','depository'),"
                "('manual:box','manual','Shoebox','depository') "
                "ON CONFLICT (tenant_id, id) DO NOTHING")
        finally:
            conn.close()
        minted = cls.client.post("/api/tokens",
                                 json={"name": "import-guard", "password": "correct-horse-battery"}).json()
        cls.hdr = {"Authorization": f"Bearer {minted['token']}"}

    def _post(self, client, account_id, **kw):
        return client.post(
            "/api/import", data={"account_id": account_id,
                                 "amount_sign": "bank"},
            files={"file": ("box.csv", CSV, "text/csv")}, **kw)

    def test_token_import_into_live_aggregator_account_is_403(self):
        from fastapi.testclient import TestClient
        bare = TestClient(self.client.app)
        r = self._post(bare, "sfin:acct1", headers=self.hdr)
        self.assertEqual(r.status_code, 403, r.text)
        from oikonome.db import tenancy
        conn = tenancy.tenant_connect(self.tid)
        try:
            n = conn.execute(
                "SELECT count(*) n FROM transactions "
                "WHERE account_id='sfin:acct1'").fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(n, 0)

    def test_token_import_into_manual_account_still_works(self):
        from fastapi.testclient import TestClient
        bare = TestClient(self.client.app)
        r = self._post(bare, "manual:box", headers=self.hdr)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse((r.json().get("result") or {}).get("error"))

    def test_zsession_owner_can_still_import_anywhere(self):
        # (z-ordered after the 403 test so its legit row into sfin:acct1
        # can't mask a guard failure — same trick as HubZipGuardTests)
        r = self._post(self.client, "sfin:acct1")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse((r.json().get("result") or {}).get("error"))


MULTI_QIF = """!Option:AutoSwitch
!Account
NEveryday Checking
TBank
^
NRewards Card
TCCard
^
!Clear:AutoSwitch
!Account
NEveryday Checking
TBank
^
!Type:Bank
D01/05'26
T-42.50
PSAFEWAY STORE
^
D01/06'26
T1200.00
PPAYROLL
^
!Account
NRewards Card
TCCard
^
!Type:CCard
D01/07'26
T-19.99
PNETFLIX
^
"""

SINGLE_QIF = """!Type:Bank
D01/05'26
T-42.50
PSAFEWAY STORE
^
"""


class QifMultiAccountTests(unittest.TestCase):
    """Quicken multi-account exports carry !Account headers. Unhandled they
    crash the parser (the account block's T-for-type line hits float) and,
    at best, merge every account into one ledger."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_multi_account_file_splits_per_account(self):
        from oikonome.sync import qifimport
        out = qifimport.import_qif(self.conn, "chk", MULTI_QIF)
        self.assertEqual(out["imported"], 3)
        self.assertIn("split_accounts", out)
        rows = self.conn.execute(
            "SELECT a.id AS aid, a.name, a.type, t.name AS txn "
            "FROM transactions t JOIN accounts a ON a.id=t.account_id "
            "WHERE t.id LIKE 'qif:%' ORDER BY t.date").fetchall()
        by_acct = {}
        for r in rows:
            by_acct.setdefault(r["name"], []).append(r["txn"])
        self.assertEqual(set(by_acct), {"Everyday Checking", "Rewards Card"})
        self.assertEqual(by_acct["Rewards Card"], ["NETFLIX"])
        # nothing landed on the user-picked account
        self.assertFalse([r for r in rows if r["aid"] == "chk"])
        # name-based typing applies to the split accounts
        types = {r["name"]: r["type"] for r in rows}
        self.assertEqual(types["Rewards Card"], "credit")
        self.assertEqual(types["Everyday Checking"], "depository")

    def test_single_account_file_uses_the_picked_account(self):
        from oikonome.sync import qifimport
        out = qifimport.import_qif(self.conn, "chk", SINGLE_QIF)
        self.assertEqual(out["imported"], 1)
        self.assertNotIn("split_accounts", out)
        r = self.conn.execute(
            "SELECT account_id FROM transactions WHERE id LIKE 'qif:%'"
        ).fetchone()
        self.assertEqual(r["account_id"], "chk")

    def test_single_account_header_still_uses_the_picked_account(self):
        # ONE !Account header names the source account, but the user's
        # pick wins (mint doctrine: only >1 distinct accounts split)
        from oikonome.sync import qifimport
        text = ("!Account\nNEveryday Checking\nTBank\n^\n" + SINGLE_QIF)
        out = qifimport.import_qif(self.conn, "chk", text)
        self.assertEqual(out["imported"], 1)
        r = self.conn.execute(
            "SELECT account_id FROM transactions WHERE id LIKE 'qif:%'"
        ).fetchone()
        self.assertEqual(r["account_id"], "chk")

    def test_reimport_is_idempotent(self):
        from oikonome.sync import qifimport
        qifimport.import_qif(self.conn, "chk", MULTI_QIF)
        qifimport.import_qif(self.conn, "chk", MULTI_QIF)  # upserts, no dupes
        n = self.conn.execute(
            "SELECT count(*) n FROM transactions WHERE id LIKE 'qif:%'"
        ).fetchone()["n"]
        self.assertEqual(n, 3)


class DualColumnCsvTests(unittest.TestCase):
    """Bank exports with separate Debit/Credit columns instead of one
    signed Amount column must auto-map: the manual mapper cannot express
    two columns, so a failure there is a dead end."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    DUAL = ("Date,Description,Debit,Credit\n"
            "2026-07-01,SAFEWAY STORE,42.50,\n"
            "2026-07-02,PAYROLL,,1200.00\n"
            "2026-07-03,NO AMOUNTS,,\n")

    def test_import_with_debit_credit_mapping(self):
        from oikonome.sync import csvimport
        out = csvimport.import_csv(
            self.conn, "chk", self.DUAL,
            {"date": "Date", "name": "Description",
             "debit": "Debit", "credit": "Credit"})
        self.assertEqual(out["imported"], 2)   # the empty row is skipped
        rows = {r["name"]: r["amount"] for r in self.conn.execute(
            "SELECT name, amount FROM transactions WHERE id LIKE 'csv:%'"
        ).fetchall()}
        # engine sign: positive = money out, regardless of amount_sign
        self.assertEqual(rows["SAFEWAY STORE"], 42.50)
        self.assertEqual(rows["PAYROLL"], -1200.00)

    def test_auto_map_detects_dual_columns(self):
        from oikonome.web.pages import _auto_map
        m = _auto_map(["Date", "Description", "Debit", "Credit"])
        self.assertIsNotNone(m)
        self.assertNotIn("amount", m)
        self.assertEqual(m["debit"], "Debit")
        self.assertEqual(m["credit"], "Credit")
        # a single signed Amount column still wins over debit/credit
        m2 = _auto_map(["Date", "Description", "Amount", "Credit"])
        self.assertEqual(m2.get("amount"), "Amount")

    def test_missing_amount_and_dual_columns_still_raises(self):
        from oikonome.sync import csvimport
        with self.assertRaises(ValueError):
            csvimport.import_csv(
                self.conn, "chk", self.DUAL,
                {"date": "Date", "name": "Description"})


class AmazonRematchShortCircuitTests(unittest.TestCase):
    """A push that changes no orders skips the full-table order↔txn matcher
    (the nightly sweep still rematches for late-arriving transactions)."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    ORDERS = [{"dedup_key": "ord-1", "date": "2026-07-01", "amount": 25.0,
               "order_number": "111-222", "items": ["Widget"]}]

    def test_unchanged_repush_skips_the_matcher(self):
        from oikonome.sync import amazon_orders
        out1 = amazon_orders.import_orders(self.conn, self.ORDERS)
        self.assertEqual(out1["new"], 1)
        self.assertNotIn("skipped", out1["amazon_match"])
        out2 = amazon_orders.import_orders(self.conn, self.ORDERS)
        self.assertEqual(out2["new"], 0)
        self.assertIn("skipped", out2["amazon_match"])

    def test_drift_refresh_still_rematches(self):
        from oikonome.sync import amazon_orders
        amazon_orders.import_orders(self.conn, self.ORDERS)
        drifted = [dict(self.ORDERS[0], date="2026-07-03",
                        dedup_key="ord-1b")]
        out = amazon_orders.import_orders(self.conn, drifted)
        self.assertEqual(out["date_drift_refreshed"], 1)
        self.assertNotIn("skipped", out["amazon_match"])


class RestoreHonestCountsTests(unittest.TestCase):
    """Restore counts are actual inserts — re-restoring the same ZIP
    reports 0 restored (+ already_present), not the ZIP's size."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    @staticmethod
    def _zip() -> bytes:
        import csv as csvmod
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            s = io.StringIO()
            w = csvmod.DictWriter(
                s, fieldnames=["id", "account_id", "date", "amount", "name"])
            w.writeheader()
            w.writerow({"id": "hr-1", "account_id": "chk",
                        "date": "2026-07-01", "amount": "12.5",
                        "name": "ROW ONE"})
            w.writerow({"id": "hr-2", "account_id": "chk",
                        "date": "2026-07-02", "amount": "3.25",
                        "name": "ROW TWO"})
            z.writestr("transactions.csv", s.getvalue())
        return buf.getvalue()

    def test_rerestore_reports_zero_not_zip_size(self):
        from oikonome.sync import restore
        first = restore.restore_zip(self.conn, self._zip())
        self.assertEqual(first["transactions"], 2)
        self.assertNotIn("already_present", first)
        second = restore.restore_zip(self.conn, self._zip())
        self.assertEqual(second.get("transactions", 0), 0)
        self.assertEqual(second["already_present"], 2)

    def test_restored_archived_items_stay_archived(self):
        # follow-through: a push-fed item travels through
        # export/restore with status archived — 'restored' would re-enter
        # it into freshness checks and the pull loop it was kept out of
        import csv as csvmod
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            s = io.StringIO()
            w = csvmod.DictWriter(
                s, fieldnames=["id", "aggregator", "institution_name",
                               "status"])
            w.writeheader()
            w.writerow({"id": "plan", "aggregator": "plan_csv",
                        "institution_name": "Workplace plan",
                        "status": "archived"})
            w.writerow({"id": "sfin-live", "aggregator": "simplefin",
                        "institution_name": "SFIN Bank", "status": "ok"})
            z.writestr("items.csv", s.getvalue())
        from oikonome.sync import restore
        restore.restore_zip(self.conn, buf.getvalue())
        rows = {r["id"]: r["status"] for r in self.conn.execute(
            "SELECT id, status FROM items "
            "WHERE id IN ('plan','sfin-live')").fetchall()}
        self.assertEqual(rows["plan"], "archived")
        self.assertEqual(rows["sfin-live"], "restored")


if __name__ == "__main__":
    unittest.main()
