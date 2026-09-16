"""JSON management APIs the SPA Accounts/Settings pages consume."""

import datetime as dt
import unittest
import uuid

from oikonome.web import data

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import (TODAY, _ensure_db, add_txn, make_db,
                   seed_accounts, write_config)


class ManageApiTests(unittest.TestCase):
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
            "email": f"mng-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def test_classify_sets_type_and_user_flag(self):
        r = self.client.post("/api/accounts/classify", json={
            "account_id": "chk", "kind": "depository/savings",
            "primary_checking": False})
        self.assertEqual(r.status_code, 200)
        conn = tenancy.tenant_connect(self.tid)
        try:
            row = conn.execute(
                "SELECT type, subtype, type_user_set FROM accounts "
                "WHERE id='chk'").fetchone()
        finally:
            conn.close()
        self.assertEqual((row["type"], row["subtype"]), ("depository", "savings"))
        self.assertTrue(row["type_user_set"])
        # restore + set primary
        r = self.client.post("/api/accounts/classify", json={
            "account_id": "chk", "kind": "depository/checking",
            "primary_checking": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/settings").json()
                         ["checking_account_id"], "chk")

    def test_editing_an_account_that_is_gone_is_not_reported_as_done(self):
        """A reconnect adoption renames account ids, so an edit can race
        one and address an id that no longer exists. Answering ok for a
        write that matched no row is how a lost edit stays invisible: the
        person is told it saved and nothing ever shows it."""
        r = self.client.post("/api/accounts/rename", json={
            "account_id": "no-such-account", "name": "Ghost"})
        self.assertEqual(r.status_code, 404, r.text)
        r = self.client.post("/api/accounts/classify", json={
            "account_id": "no-such-account", "kind": "depository/checking"})
        self.assertEqual(r.status_code, 404, r.text)
        # and a real account still edits
        r = self.client.post("/api/accounts/rename", json={
            "account_id": "chk", "name": "Everyday"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_manual_account_add_and_balance(self):
        r = self.client.post("/api/accounts/add", json={
            "name": "Shoebox Cash", "kind": "depository/savings"})
        self.assertEqual(r.status_code, 200)
        aid = r.json()["account_id"]
        r = self.client.post("/api/accounts/balance", json={
            "account_id": aid, "balance": "$1,234.56"})
        self.assertEqual(r.status_code, 200)
        accounts = self.client.get("/api/accounts").json()["accounts"]
        me = next(a for a in accounts if a["id"] == aid)
        self.assertEqual(me["balance_current"], 1234.56)

    def test_balance_rejected_for_synced_account(self):
        r = self.client.post("/api/accounts/balance", json={
            "account_id": "chk", "balance": "10"})
        self.assertEqual(r.status_code, 400)

    def test_settings_roundtrip_and_validation(self):
        r = self.client.post("/api/settings", json={
            "food_monthly": 700, "other_monthly": 1800,
            "budgeted_income_monthly": 9000,
            "email_recipients": "a@x.dev, b@x.dev",
            "email_send_hour_utc": 13})
        self.assertEqual(r.status_code, 200)
        got = self.client.get("/api/settings").json()
        self.assertEqual(got["food_monthly"], 700)
        self.assertEqual(got["email_recipients"], ["a@x.dev", "b@x.dev"])
        self.assertEqual(got["email_send_hour_utc"], 13)
        # income of 0 clears the key
        self.client.post("/api/settings", json={"budgeted_income_monthly": 0})
        self.assertIsNone(self.client.get("/api/settings").json()
                          ["budgeted_income_monthly"])
        # junk rejected without corrupting config
        r = self.client.post("/api/settings", json={"food_monthly": "lots"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.get("/api/settings").json()
                         ["food_monthly"], 700)

    def test_simplefin_bad_token_is_400_not_500(self):
        r = self.client.post("/api/accounts/simplefin",
                             json={"token": "!!!broken!!!"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("connection failed", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()


class AlertsApiTests(unittest.TestCase):
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
            "email": f"alrt-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]

    def test_dismiss_and_restore_roundtrip(self):
        import datetime as dt

        from oikonome.engine import alerts as alerts_mod
        conn = tenancy.tenant_connect(self.tid)
        try:
            alerts_mod.log(conn, [{"kind": "price_creep", "severity": "warn",
                                   "message": "Internet bill crept up $70 -> $80"}],
                           dt.date(2026, 7, 15))
        finally:
            conn.close()
        r = self.client.post("/api/alerts/dismiss", json={
            "kind": "price_creep",
            "message": "Internet bill crept up $70 -> $80"})
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(r.json()["dismissed"], 1)
        r = self.client.post("/api/alerts/restore", json={
            "kind": "price_creep",
            "message": "Internet bill crept up $70 -> $80"})
        self.assertEqual(r.status_code, 200)

    def test_history_shape_and_dismissed_roundtrip(self):
        """GET /api/alerts/history serializes the log rows (dates as ISO
        strings) and reflects dismiss/restore in the dismissed flag."""
        import datetime as dt

        from oikonome.engine import alerts as alerts_mod
        msg = "Gym plan updated $30.00 -> $35.00"
        conn = tenancy.tenant_connect(self.tid)
        try:
            alerts_mod.log(conn, [{"kind": "drift", "severity": "info",
                                   "message": msg}], dt.date(2026, 7, 16))
        finally:
            conn.close()
        r = self.client.get("/api/alerts/history")
        self.assertEqual(r.status_code, 200)
        rows = r.json()["alerts"]
        row = next(x for x in rows if x["message"] == msg)
        self.assertEqual(set(row), {"kind", "severity", "message",
                                    "first_seen", "last_seen", "active",
                                    "dismissed"})
        self.assertEqual(row["kind"], "drift")
        self.assertEqual(row["severity"], "info")
        self.assertEqual(row["first_seen"], "2026-07-16")
        self.assertEqual(row["last_seen"], "2026-07-16")
        self.assertEqual(row["active"], 1)
        self.assertEqual(row["dismissed"], 0)
        # dismiss via the API → flag flips in history
        self.client.post("/api/alerts/dismiss",
                         json={"kind": "drift", "message": msg})
        rows = self.client.get("/api/alerts/history").json()["alerts"]
        row = next(x for x in rows if x["message"] == msg)
        self.assertEqual(row["dismissed"], 1)
        # restore → back to 0
        self.client.post("/api/alerts/restore",
                         json={"kind": "drift", "message": msg})
        rows = self.client.get("/api/alerts/history").json()["alerts"]
        row = next(x for x in rows if x["message"] == msg)
        self.assertEqual(row["dismissed"], 0)


class CalendarApiTests(unittest.TestCase):
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
            "email": f"cal-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def test_calendar_shape_and_bounds(self):
        r = self.client.get("/api/calendar?days=35")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["days"], 35)
        for day in body["calendar"]:
            self.assertGreaterEqual(day["date"], body["start"])
            self.assertEqual(day["total"],
                             round(sum(e["amount"] for e in day["events"]), 2))
        # days clamped to sane range
        self.assertEqual(self.client.get("/api/calendar?days=900").json()["days"], 90)
        self.assertEqual(self.client.get("/api/calendar?days=1").json()["days"], 7)


class NumberParsingAndClassifyTests(unittest.TestCase):
    """Settings number parsing, manual-account slug collisions, and the
    classify door's primary-checking anchor."""
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
            "email": f"cfx-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def test_settings_accepts_comma_and_dollar_numbers(self):
        r = self.client.post("/api/settings", json={
            "food_monthly": "1,250.50", "budgeted_income_monthly": "$9,000"})
        self.assertEqual(r.status_code, 200)
        got = self.client.get("/api/settings").json()
        self.assertEqual(got["food_monthly"], 1250.5)
        self.assertEqual(got["budgeted_income_monthly"], 9000.0)

    def test_manual_slug_collision_creates_distinct_accounts(self):
        a = self.client.post("/api/accounts/add",
                             json={"name": "Wallet!", "kind": "depository/checking"})
        b = self.client.post("/api/accounts/add",
                             json={"name": "Wallet?", "kind": "depository/savings"})
        self.assertNotEqual(a.json()["account_id"], b.json()["account_id"])
        accounts = self.client.get("/api/accounts").json()["accounts"]
        kinds = {x["id"]: x["kind"] for x in accounts
                 if x["id"].startswith("manual:wallet")}
        self.assertEqual(len(kinds), 2)
        self.assertIn("savings", kinds[b.json()["account_id"]])

    def test_classify_without_primary_key_leaves_anchor_alone(self):
        self.client.post("/api/accounts/classify", json={
            "account_id": "chk", "kind": "depository/checking",
            "primary_checking": True})
        # a type-only edit (no primary_checking key) must not unset it
        r = self.client.post("/api/accounts/classify", json={
            "account_id": "chk", "kind": "depository/checking"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/settings").json()
                         ["checking_account_id"], "chk")

    def test_card_convention_rejected_for_csv(self):
        r = self.client.post(
            "/import",
            files={"file": ("card.csv",
                            b"Date,Amount,Description\n07/01/2026,5.00,X\n",
                            "text/csv")},
            data={"account_id": "chk", "amount_sign": "card"})
        self.assertEqual(r.status_code, 200)
        # PDF-only conventions (card/statement) are refused for CSV
        self.assertIn("PDF statement conventions", r.text)


class ImportApiTests(unittest.TestCase):
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
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def test_csv_upload_batches_and_rollback(self):
        csv = (b"Date,Amount,Description\n"
               b"07/01/2026,-42.50,GROCERY MART\n"
               b"07/02/2026,-9.99,STREAMFLIX\n")
        r = self.client.post(
            "/api/import",
            files={"file": ("bank.csv", csv, "text/csv")},
            data={"account_id": "chk", "amount_sign": "bank"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIsNone(body["mapping_needed"])
        self.assertEqual(body["result"]["imported"], 2)
        batches = self.client.get("/api/import/batches").json()["batches"]
        self.assertTrue(batches and batches[0]["row_count"] == 2)
        r = self.client.post("/api/import/rollback",
                             json={"batch_id": batches[0]["id"]})
        self.assertEqual(r.json()["deleted"], 2)

    def test_unmapped_csv_returns_mapping_then_completes(self):
        csv = (b"When,How Much,What\n"
               b"07/01/2026,-5.00,COFFEE\n")
        r = self.client.post(
            "/api/import",
            files={"file": ("odd.csv", csv, "text/csv")},
            data={"account_id": "chk", "amount_sign": "bank"})
        need = r.json()["mapping_needed"]
        self.assertIsNotNone(need)
        self.assertIn("When", need["header"])
        r = self.client.post("/api/import/mapped", json={
            "token": need["token"], "date_col": "When",
            "amount_col": "How Much", "name_col": "What"})
        self.assertEqual(r.json()["result"]["imported"], 1)

    def test_mapped_import_dual_debit_credit(self):
        # no single signed Amount — mapping a Debit/Credit pair
        # must reach the engine's dual-column path (debit = money out = +)
        csv = (b"When,Out Flow,In Flow,What\n"
               b"07/01/2026,42.50,,GROCERY MART DUAL\n"
               b"07/02/2026,,1000.00,PAYCHECK DUAL\n")
        r = self.client.post(
            "/api/import",
            files={"file": ("dual.csv", csv, "text/csv")},
            data={"account_id": "chk", "amount_sign": "bank"})
        need = r.json()["mapping_needed"]
        self.assertIsNotNone(need)
        r = self.client.post("/api/import/mapped", json={
            "token": need["token"], "date_col": "When", "name_col": "What",
            "debit_col": "Out Flow", "credit_col": "In Flow"})
        self.assertEqual(r.json()["result"]["imported"], 2)
        conn = tenancy.tenant_connect(self.tid)
        try:
            rows = conn.execute(
                "SELECT name, amount FROM transactions "
                "WHERE name LIKE %s", ("%DUAL",)).fetchall()
        finally:
            conn.close()
        amounts = {r["name"]: float(r["amount"]) for r in rows}
        self.assertEqual(amounts["GROCERY MART DUAL"], 42.50)   # out = +
        self.assertEqual(amounts["PAYCHECK DUAL"], -1000.00)    # in = −

    def test_mapped_import_needs_one_money_column_not_a_whole_pair(self):
        # Mapping NOTHING that names the money is refused, and the refusal
        # lands BEFORE the stash is consumed so the same upload can be
        # remapped. But one side of the pair alone is a real bank export
        # (a Withdrawals-only file) and the engine has always taken it —
        # the wrapper must not demand the half the file does not have.
        csv = (b"When,Out Flow,In Flow,What\n"
               b"07/01/2026,1.00,,X\n")
        r = self.client.post(
            "/api/import",
            files={"file": ("dual2.csv", csv, "text/csv")},
            data={"account_id": "chk", "amount_sign": "bank"})
        need = r.json()["mapping_needed"]
        r = self.client.post("/api/import/mapped", json={
            "token": need["token"], "date_col": "When", "name_col": "What"})
        self.assertIn("Amount", r.json()["result"]["error"])
        # the stash survived the bad request — mapping one side now works
        r = self.client.post("/api/import/mapped", json={
            "token": need["token"], "date_col": "When", "name_col": "What",
            "debit_col": "Out Flow"})
        self.assertEqual(r.json()["result"]["imported"], 1)

    def test_a_wrong_date_column_is_corrected_without_re_uploading(self):
        # The mapping card exists because the columns were guessed wrong,
        # and the commonest wrong guess is the date. That one is only
        # caught while the rows are parsed — after the stashed upload has
        # been consumed — so the refusal has to hand back a live claim
        # token, or the card sits there submitting a dead one forever.
        csv = (b"When,How Much,What\n"
               b"07/06/2026,-5.00,COFFEE REMAP\n")
        r = self.client.post(
            "/api/import",
            files={"file": ("remap.csv", csv, "text/csv")},
            data={"account_id": "chk", "amount_sign": "bank"})
        need = r.json()["mapping_needed"]
        before = len(self.client.get("/api/import/batches").json()["batches"])

        # date pointed at the description column: no row parses
        bad = self.client.post("/api/import/mapped", json={
            "token": need["token"], "date_col": "What",
            "amount_col": "How Much", "name_col": "What"}).json()["result"]
        self.assertIn("unrecognized date", bad["error"])
        self.assertFalse(bad.get("expired"),
                         "a correctable mapping mistake told the client the "
                         "upload was gone")
        self.assertTrue(bad.get("token"),
                        "the refusal left the person with no token to "
                        "resubmit — the card can only say 'upload expired'")
        self.assertNotEqual(bad["token"], need["token"])
        # nothing was imported, so nothing is left in the batch list either
        self.assertEqual(
            len(self.client.get("/api/import/batches").json()["batches"]),
            before, "a refused mapping left an empty batch behind")

        # the SAME file, remapped, on the token the refusal handed back
        good = self.client.post("/api/import/mapped", json={
            "token": bad["token"], "date_col": "When",
            "amount_col": "How Much", "name_col": "What"}).json()["result"]
        self.assertEqual(good["imported"], 1, good)
        self.assertEqual(self._amounts("%REMAP"), {"COFFEE REMAP": 5.00})

    def test_an_upload_that_is_gone_tells_the_client_to_upload_again(self):
        # The other half of the contract: when the stash really cannot be
        # recovered, the reply must SAY so, because the clients drop the
        # mapping card on `expired`. Leaving it up with a dead token is
        # what produced the same message on every click.
        gone = self.client.post("/api/import/mapped", json={
            "token": "no-such-token", "date_col": "When",
            "amount_col": "How Much", "name_col": "What"}).json()["result"]
        self.assertTrue(gone.get("expired"))
        self.assertIn("again", gone["error"])
        self.assertIsNone(gone.get("token"))

    def test_a_spent_token_is_dead_once_the_refusal_replaces_it(self):
        # The re-stash is a NEW claim; a client that keeps submitting the
        # old token must be told the upload is gone rather than looping.
        csv = (b"When,How Much,What\n"
               b"07/07/2026,-5.00,COFFEE STALE\n")
        r = self.client.post(
            "/api/import",
            files={"file": ("stale.csv", csv, "text/csv")},
            data={"account_id": "chk", "amount_sign": "bank"})
        need = r.json()["mapping_needed"]
        first = self.client.post("/api/import/mapped", json={
            "token": need["token"], "date_col": "What",
            "amount_col": "How Much", "name_col": "What"}).json()["result"]
        self.assertTrue(first.get("token"))
        again = self.client.post("/api/import/mapped", json={
            "token": need["token"], "date_col": "When",
            "amount_col": "How Much", "name_col": "What"}).json()["result"]
        self.assertTrue(again.get("expired"))

    def test_mapped_import_takes_a_withdrawals_only_file(self):
        # no Amount, no Deposits column at all — the shape the mapping
        # screen offers Finish on. Debit = money out = positive.
        csv = (b"When,Withdrawals,What\n"
               b"07/03/2026,42.50,GROCERY MART DEBITONLY\n"
               b"07/04/2026,9.99,STREAMFLIX DEBITONLY\n")
        r = self.client.post(
            "/api/import",
            files={"file": ("debitonly.csv", csv, "text/csv")},
            data={"account_id": "chk", "amount_sign": "bank"})
        need = r.json()["mapping_needed"]
        self.assertIsNotNone(need)
        r = self.client.post("/api/import/mapped", json={
            "token": need["token"], "date_col": "When", "name_col": "What",
            "debit_col": "Withdrawals"})
        self.assertEqual(r.json()["result"]["imported"], 2, r.text)
        self.assertEqual(self._amounts("%DEBITONLY"),
                         {"GROCERY MART DEBITONLY": 42.50,
                          "STREAMFLIX DEBITONLY": 9.99})

    def test_mapped_import_takes_a_deposits_only_file(self):
        # the mirror shape — money in only, which must land negative
        csv = (b"When,Deposits,What\n"
               b"07/05/2026,1000.00,PAYCHECK CREDITONLY\n")
        r = self.client.post(
            "/api/import",
            files={"file": ("creditonly.csv", csv, "text/csv")},
            data={"account_id": "chk", "amount_sign": "bank"})
        need = r.json()["mapping_needed"]
        self.assertIsNotNone(need)
        r = self.client.post("/api/import/mapped", json={
            "token": need["token"], "date_col": "When", "name_col": "What",
            "credit_col": "Deposits"})
        self.assertEqual(r.json()["result"]["imported"], 1, r.text)
        self.assertEqual(self._amounts("%CREDITONLY"),
                         {"PAYCHECK CREDITONLY": -1000.00})

    def _amounts(self, like: str) -> dict:
        conn = tenancy.tenant_connect(self.tid)
        try:
            rows = conn.execute(
                "SELECT name, amount FROM transactions WHERE name LIKE %s",
                (like,)).fetchall()
        finally:
            conn.close()
        return {r["name"]: float(r["amount"]) for r in rows}

    def test_error_shape_is_json_not_500(self):
        r = self.client.post(
            "/api/import",
            files={"file": ("x.csv", b"Date,Amount,Description\n", "text/csv")},
            data={"account_id": "", "amount_sign": "bank"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("pick the account", r.json()["result"]["error"])


class OnboardingApiTests(unittest.TestCase):
    def test_fresh_tenant_counts(self):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        c = TestClient(app)
        c.post("/api/signup", data={
            "email": f"onb-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        ob = c.get("/api/onboarding").json()
        self.assertEqual((ob["accounts"], ob["transactions"], ob["bills"]),
                         (0, 0, 0))
        self.assertFalse(ob["budgets_set"])
        conn = tenancy.tenant_connect(c.get("/api/me").json()["tenant_id"])
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()
        ob = c.get("/api/onboarding").json()
        self.assertEqual(ob["accounts"], 2)
        self.assertTrue(ob["budgets_set"])
        # the guided wizard is offered until dismissed…
        self.assertFalse(ob["wizard_done"])
        # …and the dismissal round-trips through settings
        r = c.post("/api/settings", json={"wizard_done": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(c.get("/api/onboarding").json()["wizard_done"])
        # the household's zone round-trips, shows up on /api/me as the
        # zone the hours are kept in, clears back to the instance, and a
        # name this host cannot load is refused rather than stored
        c.post("/api/settings", json={"timezone": "America/Los_Angeles"})
        self.assertEqual(c.get("/api/settings").json()["timezone"],
                         "America/Los_Angeles")
        me = c.get("/api/me").json()
        self.assertEqual(me["timezone"], "America/Los_Angeles")
        self.assertEqual(me["timezone_source"], "household")
        self.assertTrue(me["instance_timezone"])
        c.post("/api/settings", json={"timezone": ""})
        self.assertIsNone(c.get("/api/settings").json()["timezone"])
        self.assertEqual(c.get("/api/me").json()["timezone_source"],
                         "instance")
        r = c.post("/api/settings", json={"timezone": "Mars/Olympus_Mons"})
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(c.get("/api/settings").json()["timezone"])
        # theme pin round-trips; auto clears; junk rejected
        c.post("/api/settings", json={"theme": "light"})
        self.assertEqual(c.get("/api/settings").json()["theme"], "light")
        c.post("/api/settings", json={"theme": "auto"})
        self.assertIsNone(c.get("/api/settings").json()["theme"])
        self.assertEqual(
            c.post("/api/settings", json={"theme": "neon"}).status_code, 400)
        # the Today hero face round-trips the same way: advanced pins,
        # simple clears back to the default, junk rejected — and the
        # /api/today payload follows it, which is what the page toggle
        # and the daily email actually read
        c.post("/api/settings", json={"today_view": "detail"})
        self.assertEqual(c.get("/api/settings").json()["today_view"],
                         "detail")
        self.assertEqual(c.get("/api/today/full").json()["today_view"],
                         "detail")
        c.post("/api/settings", json={"today_view": "summary"})
        self.assertIsNone(c.get("/api/settings").json()["today_view"])
        t = c.get("/api/today/full").json()
        self.assertEqual(t["today_view"], "summary")
        # the simple face's composed pieces ride the payload for the SPA
        self.assertIn("left_today", t["simple"])
        self.assertIsInstance(t["simple"]["chips"], list)
        self.assertEqual(
            c.post("/api/settings",
                   json={"today_view": "fancy"}).status_code, 400)


class CategoryScopeTests(unittest.TestCase):
    """A correction on ONE row must not silently claim the merchant.

    Without a scope, a single edit on a generic payee — a bank's "check
    paid" descriptor, a transfer line — writes a merchant-wide rule and
    repaints every other row that shares the string. Those payees are not
    one thing, so `scope` decides, and it defaults to the narrow reading.
    """

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.a = add_txn(self.conn, TODAY, 40.0, "CHECK PAID", account="chk")
        self.b = add_txn(self.conn, TODAY - dt.timedelta(days=3), 55.0,
                         "CHECK PAID", account="chk")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _rules(self):
        return [r["merchant"] for r in self.conn.execute(
            "SELECT merchant FROM merchant_categories").fetchall()]

    def _cat(self, tid):
        return self.conn.execute(
            "SELECT category_override FROM transactions WHERE id=%s",
            (tid,)).fetchone()["category_override"]

    def test_scope_one_writes_no_rule_and_leaves_siblings_alone(self):
        data.set_category(self.conn, self.a, "MEDICAL", scope="one")
        self.conn.commit()
        self.assertEqual("MEDICAL", self._cat(self.a))
        self.assertIsNone(self._cat(self.b),
                          "a one-off edit changed a sibling transaction")
        self.assertEqual([], self._rules(),
                         "a one-off edit wrote a merchant rule — it would "
                         "show on the Rules page and claim every CHECK PAID")

    def test_scope_all_writes_the_rule(self):
        data.set_category(self.conn, self.a, "MEDICAL", scope="all")
        self.conn.commit()
        self.assertTrue(self._rules(), "scope=all wrote no rule")

    def test_default_is_the_narrow_one(self):
        """The default is load-bearing: every existing caller inherits it."""
        data.set_category(self.conn, self.a, "MEDICAL")
        self.conn.commit()
        self.assertEqual([], self._rules())

    def test_bad_scope_rejected(self):
        with self.assertRaises(ValueError):
            data.set_category(self.conn, self.a, "MEDICAL", scope="everything")


class RetWizardStepsTests(unittest.TestCase):
    """Retirement setup's persisted marks — third flow, third key.

    A tenant can finish onboarding, never run a business, and be halfway
    through retirement planning; one shared key could not express that.
    """

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        r = cls.client.post("/api/signup", data={
            "email": f"rw-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text

    def setUp(self):
        self.client.post("/api/settings", json={"ret_wizard_steps": {
            k: None for k in
            ("age", "spend", "saving", "assumptions", "done")}})

    def _steps(self):
        return self.client.get("/api/settings").json().get("ret_wizard_steps")

    def test_marks_round_trip(self):
        self.assertEqual({}, self._steps())
        self.client.post("/api/settings",
                         json={"ret_wizard_steps": {"done": "done"}})
        self.assertEqual({"done": "done"}, self._steps())

    def test_unknown_step_rejected(self):
        r = self.client.post("/api/settings",
                             json={"ret_wizard_steps": {"nope": "done"}})
        self.assertEqual(400, r.status_code)

    def test_independent_of_the_other_wizards(self):
        self.client.post("/api/settings",
                         json={"biz_wizard_steps": {"what": "done"}})
        self.client.post("/api/settings",
                         json={"wizard_steps": {"finish": "done"}})
        self.assertEqual({}, self._steps())


class BizWizardStepsTests(unittest.TestCase):
    """Persisted marks for the BUSINESS setup wizard.

    Completion held in React state is forgotten on a refresh: the wizard
    cannot resume where you left off, and it prompts "continue setup"
    forever because nothing records that it finished. Separate key from
    wizard_steps — different flows, different steps, and finishing
    onboarding says nothing about whether a business was ever set up.
    """

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        r = cls.client.post("/api/signup", data={
            "email": f"bw-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text

    def setUp(self):
        # class-level client → these tests share a tenant, so each starts
        # from a known-empty mark set rather than inheriting the last one
        self.client.post("/api/settings", json={"biz_wizard_steps": {
            k: None for k in
            ("what", "when", "accounts", "startup", "finish")}})

    def _steps(self):
        return self.client.get("/api/settings").json().get("biz_wizard_steps")

    def test_marks_round_trip_and_merge(self):
        self.assertEqual({}, self._steps())
        r = self.client.post("/api/settings",
                             json={"biz_wizard_steps": {"what": "done"}})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual({"what": "done"}, self._steps())
        # each step posts only its OWN mark
        self.client.post("/api/settings",
                         json={"biz_wizard_steps": {"accounts": "skipped"}})
        self.assertEqual({"what": "done", "accounts": "skipped"}, self._steps())

    def test_null_forgets_a_mark(self):
        self.client.post("/api/settings",
                         json={"biz_wizard_steps": {"what": "done"}})
        self.client.post("/api/settings",
                         json={"biz_wizard_steps": {"what": None}})
        self.assertEqual({}, self._steps())

    def test_unknown_step_and_state_rejected(self):
        r = self.client.post("/api/settings",
                             json={"biz_wizard_steps": {"nope": "done"}})
        self.assertEqual(400, r.status_code)
        r = self.client.post("/api/settings",
                             json={"biz_wizard_steps": {"what": "maybe"}})
        self.assertEqual(400, r.status_code)

    def test_independent_of_the_welcome_wizard(self):
        """Finishing onboarding must not imply a business was set up."""
        self.client.post("/api/settings",
                         json={"wizard_steps": {"finish": "done"}})
        self.assertEqual({}, self._steps())
