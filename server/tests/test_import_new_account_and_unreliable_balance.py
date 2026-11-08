"""Importing into an account that does not exist yet, and what a $0
balance may not be used to claim.

The single-file import page creates its destination account inline, the
same way the bulk importer does. And while imported accounts sit at their
$0 default (runway.balance_unreliable) the forecast is suppressed
everywhere — a $0-seeded trajectory is noise presented as truth."""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, write_config

CSV = b"Date,Description,Amount\n2026-07-01,COFFEE SHOP,-4.50\n"


class ImportCreateAccountTests(unittest.TestCase):
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
            "email": f"imp-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            write_config(conn)
        finally:
            conn.close()

    def test_the_sync_lock_is_let_go_only_after_the_rows_commit(self):
        """The import's session sync lock must outlive its push
        transaction: a sync queued on release must find the file's rows
        committed, or it pulls again what the file already covers."""
        from unittest import mock
        from psycopg import pq
        seen = []

        def answer(conn, tenant_id):
            seen.append(conn.info.transaction_status)
            return False
        with mock.patch("oikonome.jobs.worker.answer_sync_nudge", answer):
            r = self.client.post(
                "/api/import",
                data={"account_id": "", "amount_sign": "bank",
                      "new_account_name": "Lock Order Checking"},
                files={"file": ("lock.csv", CSV, "text/csv")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(seen, [pq.TransactionStatus.IDLE])

    def test_a_typed_number_two_moved_cards_share_asks_which_one(self):
        """Two cards an earlier release named by their full numbers, both
        moved onto ids keeping only the last four they share: a file
        whose new-account name spells one of the numbers is answered
        with a choice to make, not landed on either and not a 500."""
        from oikonome.sync import base
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute(
                "INSERT INTO items (id, aggregator, institution_name)"
                " VALUES ('manual','manual','Manual')"
                " ON CONFLICT (tenant_id, id) DO NOTHING")
            for n in ("4111111111111111", "5500000000001111"):
                conn.execute(
                    "INSERT INTO accounts (id, item_id, name, type, subtype)"
                    " VALUES (%s,'manual',%s,'credit','credit card')",
                    (f"manual:shared-{n}", f"Shared {n}"))
            moved = list(base.rekey_numbered_accounts(conn).values())
            self.assertEqual(len(moved), 2)
            before = conn.execute(
                "SELECT (SELECT count(*) FROM accounts) AS a,"
                " (SELECT count(*) FROM transactions) AS t").fetchone()
            r = self.client.post(
                "/api/import",
                data={"account_id": "", "amount_sign": "card",
                      "new_account_name": "Shared 4111111111111111"},
                files={"file": ("shared.csv", CSV, "text/csv")})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("choose one", r.json()["result"]["error"])
            self.assertEqual(conn.execute(
                "SELECT (SELECT count(*) FROM accounts) AS a,"
                " (SELECT count(*) FROM transactions) AS t").fetchone(),
                before)
        finally:
            # the class shares one household
            conn.execute("DELETE FROM account_adoptions"
                         " WHERE account_id LIKE 'manual:shared-%%'")
            conn.execute("DELETE FROM accounts"
                         " WHERE id LIKE 'manual:shared-%%'")
            conn.close()

    def test_a_script_naming_a_moved_numbered_id_reaches_the_moved_account(
            self):
        """A script that pushes by the id an earlier release gave an
        account named by its full number keeps working after the account
        moves to an id keeping the last four: the move's record leads the
        import and the balance door to it."""
        from oikonome.sync import base
        old = "manual:legacy-12345678901234"        # an invented number
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute(
                "INSERT INTO items (id, aggregator, institution_name)"
                " VALUES ('manual','manual','Manual')"
                " ON CONFLICT (tenant_id, id) DO NOTHING")
            conn.execute(
                "INSERT INTO accounts (id, item_id, name, type, subtype)"
                " VALUES (%s,'manual','Legacy …1234','depository',"
                "'checking')", (old,))
            moved = base.rekey_numbered_accounts(conn)[old]
            r = self.client.post(
                "/api/import",
                data={"account_id": old, "amount_sign": "bank"},
                files={"file": ("legacy.csv", CSV, "text/csv")})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertNotIn("error", r.json()["result"], r.text)
            r = self.client.post("/api/accounts/balance",
                                 json={"account_id": old, "balance": "12.5"})
            self.assertEqual(r.status_code, 200, r.text)
            row = conn.execute(
                "SELECT balance_current, (SELECT count(*) FROM transactions"
                " WHERE account_id = %s) AS n FROM accounts WHERE id = %s",
                (moved, moved)).fetchone()
            self.assertEqual((float(row["balance_current"]), row["n"]),
                             (12.5, 1))
        finally:
            conn.execute("DELETE FROM transactions WHERE account_id LIKE"
                         " 'manual:legacy-%%'")
            conn.execute("DELETE FROM account_adoptions"
                         " WHERE account_id LIKE 'manual:legacy-%%'")
            conn.execute("DELETE FROM accounts"
                         " WHERE id LIKE 'manual:legacy-%%'")
            conn.close()

    def test_a_huge_new_account_name_is_cut_like_the_bulk_importer(self):
        # the name becomes the account's primary-key id; uncut, a few
        # thousand characters overflow the index and the import was a 500
        name = "Spare Checking " + "x" * 5000
        r = self.client.post(
            "/api/import",
            data={"account_id": "", "amount_sign": "bank",
                  "new_account_name": name},
            files={"file": ("spare.csv", CSV, "text/csv")})
        self.assertEqual(r.status_code, 200, r.text)
        conn = tenancy.tenant_connect(self.tid)
        try:
            acct = conn.execute(
                "SELECT id, name FROM accounts"
                " WHERE name LIKE 'Spare Checking%%'").fetchone()
            # the class shares one household: leave the other tests the
            # single manual account they expect
            conn.execute("DELETE FROM transactions WHERE account_id=%s",
                         (acct["id"],))
            conn.execute("DELETE FROM accounts WHERE id=%s", (acct["id"],))
        finally:
            conn.close()
        self.assertEqual(acct["name"], name[:120])

    def test_import_into_a_new_account(self):
        r = self.client.post(
            "/api/import",
            data={"account_id": "", "amount_sign": "bank",
                  "new_account_name": "Chase Checking"},
            files={"file": ("chase.csv", CSV, "text/csv")})
        self.assertEqual(r.status_code, 200, r.text)
        res = r.json()["result"]
        self.assertFalse(res.get("error"), res)
        self.assertEqual(res.get("imported"), 1)
        conn = tenancy.tenant_connect(self.tid)
        try:
            acct = conn.execute(
                "SELECT id, name FROM accounts WHERE id LIKE 'manual:%'"
            ).fetchone()
            self.assertEqual(acct["name"], "Chase Checking")
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM transactions WHERE account_id=%s",
                (acct["id"],)).fetchone()["n"]
            self.assertEqual(n, 1)
        finally:
            conn.close()


class ForecastSuppressedTests(unittest.TestCase):
    def test_unreliable_balance_suppresses_the_forecast(self):
        from oikonome.web import report, todayview
        from .util import add_txn, make_db
        import datetime as dt
        conn = make_db()
        try:
            write_config(conn)
            add_txn(conn, dt.date.today(), 12.0, "COFFEE", account="chk")
            # imported/manual account still at the $0 default —
            # balance_unreliable = no LIVE depository account + rows exist
            conn.execute(
                """INSERT INTO items (id, aggregator, institution_name)
                   VALUES ('manual','manual','Manual')
                   ON CONFLICT (tenant_id, id) DO NOTHING""")
            conn.execute("UPDATE accounts SET balance_current=0, "
                         "balance_available=NULL, item_id='manual'")
            st = report.gather(conn, dt.date.today())
            if not (st.get("runway") or {}).get("balance_unreliable"):
                self.skipTest("fixture did not trip balance_unreliable")
            ctx = todayview.build_context(st)
            self.assertIsNone(ctx["fc"])          # no $0-as-truth forecast
            self.assertEqual(ctx["fc_rows"], [])
            self.assertEqual(ctx["fc_lows"], [])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
