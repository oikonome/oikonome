"""A YNAB register export can hold several YNAB accounts — a checking
account and a card in one file, each row naming its own in the Account
column. Imported into the one account the person picked, every card
purchase landed on checking: counted there beside the card's own feed,
deduplicated against the wrong account's rows, and never matched to the
card's.

The person now says where each YNAB account goes. Nothing is inferred
from a name or from an id that happens to exist. A row's id depends only
on the file and the account it lands on, so a re-import of the account
that stays where it was keeps every id it had. Rows an earlier import put
on the picked account are removed only when the person asks, and only
when both the exact id that import gave them and their own recorded
Account match."""

import csv
import datetime as dt
import io
import os
import unittest
import uuid

from fastapi import HTTPException
from fastapi.testclient import TestClient

from oikonome.db import staging, tenancy
from oikonome.sync import heartbeat
from oikonome.web import bulk_import, pages

from .bulk_job_util import submit_and_run
from .util import _ensure_db, add_txn, make_db, seed_accounts, write_config

HEAD = ('"Account","Flag","Date","Payee","Category Group/Category",'
        '"Category Group","Category","Memo","Outflow","Inflow","Cleared"\n')
CHECKING_ROWS = (
    '"Checking","","2026-07-10","Safeway","Everyday: Groceries","Everyday",'
    '"Groceries","","$42.50","$0.00","Cleared"\n'
    '"Checking","","2026-07-12","Transfer : Visa","","","","","$100.00",'
    '"$0.00","Cleared"\n')
REGISTER = (
    HEAD
    + '"Checking","","2026-07-10","Safeway","Everyday: Groceries",'
      '"Everyday","Groceries","","$42.50","$0.00","Cleared"\n'
    + '"Visa","","2026-07-10","Gas Station","Everyday: Fuel","Everyday",'
      '"Fuel","","$35.00","$0.00","Cleared"\n'
    + '"Checking","","2026-07-12","Transfer : Visa","","","","","$100.00",'
      '"$0.00","Cleared"\n'
    + '"Visa","","2026-07-12","Transfer : Checking","","","","","$0.00",'
      '"$100.00","Cleared"\n'
    # an identical line on both accounts, the card's first: the checking
    # line is the second occurrence of its date, cents and name in the file
    + '"Visa","","2026-07-31","Interest","","","","","$0.00","$0.01",'
      '"Cleared"\n'
    + '"Checking","","2026-07-31","Interest","","","","","$0.00","$0.01",'
      '"Cleared"\n')
ONE_ACCOUNT = HEAD + CHECKING_ROWS
BANK_CSV = (b"Date,Description,Amount\n"
            b"2026-07-01,HARDWARE STORE,-40.00\n"
            b"2026-07-02,PAYCHECK,1500.00\n")


def seed_whole_register_on(conn, account_id: str, text: str) -> list[str]:
    """The rows an earlier import of the whole register into one account
    stored there: every line under that account's ids, numbered over the
    whole file, raw the scrubbed source row. Returns the ids in file
    order."""
    from oikonome.sync import batches
    from oikonome.sync.base import (FileImportIds, Transaction,
                                    upsert_transactions)
    from oikonome.sync.csvimport import scrub_identifiers

    def money(s):
        s = (s or "").replace("$", "").replace(",", "").strip()
        return float(s) if s else 0.0
    ids = FileImportIds("ynab", account_id)
    txns = []
    for row in csv.DictReader(io.StringIO(text)):
        row = {k: v for k, v in row.items() if k is not None}
        amount = abs(money(row["Outflow"])) - abs(money(row["Inflow"]))
        date = dt.date.fromisoformat(row["Date"])
        txns.append(Transaction(
            id=ids.id_for(date, amount, row["Payee"]),
            account_id=account_id, date=date, amount=amount,
            name=row["Payee"], merchant_name=row["Payee"],
            raw=scrub_identifiers(row)))
    bid = batches.create(conn, "ynab", account_id, "register.csv")
    batches.tag(conn, txns, bid)
    upsert_transactions(conn, txns)
    batches.finish(conn, bid, len(txns))
    return [t.id for t in txns]


def ynab_rows(conn, account_id):
    return {r["id"]: (r["name"], float(r["amount"]), r["acct"])
            for r in conn.execute(
                "SELECT id, name, amount, raw->>'Account' AS acct"
                "  FROM transactions WHERE account_id = %s AND removed = 0"
                "   AND id LIKE 'ynab:%%'", (account_id,))}


def accounts_of(rows):
    return {a for _, _, a in rows.values()}


class YnabRegisterAccountsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _ask(self, text=REGISTER, pick="chk"):
        return pages._dispatch_import_unlocked(
            self.conn, pick, "bank", "register.csv", text.encode(),
            role="owner", ynab_choice=True)

    def _answer(self, token, accounts, remove_earlier=None):
        return pages.run_ynab_import(self.conn, token, accounts,
                                     remove_earlier or {})

    def test_a_multi_account_register_asks_where_each_account_goes(self):
        result, need = self._ask()
        self.assertFalse((result or {}).get("imported"))
        self.assertIsNotNone(need)
        self.assertEqual(need["kind"], "ynab_accounts")
        self.assertEqual([a["name"] for a in need["accounts"]],
                         ["Checking", "Visa"])
        by = {a["name"]: a for a in need["accounts"]}
        self.assertEqual(by["Checking"]["rows"], 3)
        self.assertEqual(by["Visa"]["rows"], 3)
        self.assertEqual(by["Visa"]["first_date"], "2026-07-10")
        self.assertEqual(by["Visa"]["last_date"], "2026-07-31")
        self.assertEqual(ynab_rows(self.conn, "chk"), {})
        self.assertEqual(ynab_rows(self.conn, "card"), {})

    def test_each_account_lands_where_the_person_sent_it(self):
        _, need = self._ask()
        r = self._answer(need["token"], {"Checking": "chk", "Visa": "card"})
        self.assertNotIn("error", r)
        self.assertEqual(accounts_of(ynab_rows(self.conn, "chk")),
                         {"Checking"})
        self.assertEqual(accounts_of(ynab_rows(self.conn, "card")), {"Visa"})
        self.assertEqual(len(ynab_rows(self.conn, "card")), 3)

    def test_rows_of_the_kept_ynab_account_keep_their_earlier_ids(self):
        legacy = seed_whole_register_on(self.conn, "chk", REGISTER)
        before = set(ynab_rows(self.conn, "chk"))
        self.assertEqual(before, set(legacy))
        _, need = self._ask()
        r = self._answer(need["token"], {"Checking": "chk", "Visa": "card"})
        self.assertNotIn("error", r)
        # the checking lines updated the rows they made before — the
        # interest line too, the second of its kind in the file — and the
        # card lines left there stay until the person asks them gone
        self.assertEqual(set(ynab_rows(self.conn, "chk")), before)

    def test_a_register_card_purchase_dedups_against_the_cards_own_feed(self):
        add_txn(self.conn, "2026-07-10", 35.00, "GAS STATION",
                account="card", txn_id="feed-gas")
        _, need = self._ask()
        r = self._answer(need["token"], {"Checking": "chk", "Visa": "card"})
        self.assertEqual(r["skipped_duplicates"], 1)
        total = self.conn.execute(
            "SELECT sum(amount) AS s FROM transactions WHERE account_id='card'"
            " AND date='2026-07-10' AND removed=0").fetchone()["s"]
        self.assertEqual(float(total), 35.0)

    def test_rows_an_earlier_import_put_on_the_pick_are_removed_when_asked(self):
        seed_whole_register_on(self.conn, "chk", REGISTER)
        _, need = self._ask()
        by = {a["name"]: a for a in need["accounts"]}
        self.assertEqual(by["Visa"]["stored_on_pick"], 3)
        r = self._answer(need["token"], {"Checking": "chk", "Visa": "card"},
                         {"Visa": True})
        self.assertEqual(r["removed_earlier"], 3)
        self.assertEqual(accounts_of(ynab_rows(self.conn, "chk")),
                         {"Checking"})
        self.assertEqual(len(ynab_rows(self.conn, "card")), 3)

    def test_rows_an_earlier_import_put_on_the_pick_stay_unless_asked(self):
        seed_whole_register_on(self.conn, "chk", REGISTER)
        _, need = self._ask()
        r = self._answer(need["token"], {"Checking": "chk", "Visa": "card"},
                         {"Visa": False})
        self.assertFalse(r.get("removed_earlier"))
        self.assertEqual(sum(1 for _, _, a in ynab_rows(self.conn,
                                                        "chk").values()
                             if a == "Visa"), 3)

    def test_an_earlier_row_with_a_note_is_kept_and_counted(self):
        legacy = seed_whole_register_on(self.conn, "chk", REGISTER)
        gas = legacy[1]
        self.conn.execute(
            "INSERT INTO transaction_notes (txn_id, note) VALUES (%s,%s)",
            (gas, "fuel for the trip"))
        _, need = self._ask()
        r = self._answer(need["token"], {"Checking": "chk", "Visa": "card"},
                         {"Visa": True})
        self.assertEqual(r["kept_annotated"], 1)
        self.assertEqual(r["removed_earlier"], 2)
        self.assertIn(gas, ynab_rows(self.conn, "chk"))

    def test_a_foreign_row_from_another_export_is_left_and_counted(self):
        seed_whole_register_on(self.conn, "chk", REGISTER)
        # a Visa row another export put here: its id is no id this file
        # gives any line, so nothing proves it came from this register
        other = seed_whole_register_on(
            self.conn, "chk",
            HEAD + '"Visa","","2026-06-02","Bookshop","","","","","$20.00",'
                   '"$0.00","Cleared"\n')
        _, need = self._ask()
        r = self._answer(need["token"], {"Checking": "chk", "Visa": "card"},
                         {"Visa": True})
        self.assertEqual(r["foreign_left"], 1)
        self.assertIn(other[0], ynab_rows(self.conn, "chk"))

    def test_every_ynab_account_must_be_answered_and_the_upload_survives(self):
        _, need = self._ask()
        r = self._answer(need["token"], {"Checking": "chk"})
        self.assertIn("Visa", r["error"])
        self.assertNotIn("token", r)
        self.assertIsNotNone(staging.peek(self.conn, "ynab", need["token"]))
        self.assertEqual(ynab_rows(self.conn, "chk"), {})
        r = self._answer(need["token"], {"Checking": "skip", "Visa": "skip"})
        self.assertIn("error", r)
        self.assertIsNotNone(staging.peek(self.conn, "ynab", need["token"]))
        r = self._answer(need["token"], {"Checking": "chk", "Visa": "skip"})
        self.assertNotIn("error", r)
        self.assertEqual(len(ynab_rows(self.conn, "chk")), 3)
        self.assertEqual(ynab_rows(self.conn, "card"), {})

    def test_a_hidden_account_is_not_a_destination(self):
        self.conn.execute(
            "UPDATE accounts SET user_removed_at = now() WHERE id='card'")
        _, need = self._ask()
        r = self._answer(need["token"], {"Checking": "chk", "Visa": "card"})
        self.assertIn("error", r)
        self.assertIsNotNone(staging.peek(self.conn, "ynab", need["token"]))
        r = self._answer(need["token"], {"Checking": "chk",
                                         "Visa": "nonexistent"})
        self.assertIn("error", r)
        self.assertEqual(ynab_rows(self.conn, "chk"), {})

    def test_a_new_account_can_be_the_destination(self):
        _, need = self._ask()
        r = self._answer(need["token"], {"Checking": "chk",
                                         "Visa": {"new": "Rewards Visa"}})
        self.assertNotIn("error", r)
        acct = self.conn.execute(
            "SELECT id FROM accounts WHERE name='Rewards Visa'").fetchone()
        self.assertIsNotNone(acct)
        self.assertEqual(len(ynab_rows(self.conn, acct["id"])), 3)

    def test_a_script_token_cannot_push_a_multi_account_register(self):
        via = heartbeat.PUSH_VIA.set("token")
        try:
            with self.assertRaises(HTTPException) as caught:
                pages._dispatch_import_unlocked(
                    self.conn, "chk", "bank", "register.csv",
                    REGISTER.encode(), role="owner")
            self.assertEqual(caught.exception.status_code, 403)
            self.assertEqual(ynab_rows(self.conn, "chk"), {})
            # one account's register still lands where the token sent it
            result, need = pages._dispatch_import_unlocked(
                self.conn, "chk", "bank", "register.csv",
                ONE_ACCOUNT.encode(), role="owner")
        finally:
            heartbeat.PUSH_VIA.reset(via)
        self.assertIsNone(need)
        self.assertEqual(result["imported"], 2)

    def test_an_older_client_gets_a_refusal_naming_the_accounts(self):
        result, need = pages._dispatch_import_unlocked(
            self.conn, "chk", "bank", "register.csv", REGISTER.encode(),
            role="owner")
        self.assertIsNone(need)
        self.assertIn("Checking", result["error"])
        self.assertIn("Visa", result["error"])
        self.assertEqual(ynab_rows(self.conn, "chk"), {})
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM import_batches WHERE source='ynab'").fetchone())

    def test_bulk_refuses_only_the_multi_account_register(self):
        plan = bulk_import.analyze(self.conn, [
            ("register.csv", REGISTER.encode()), ("bank.csv", BANK_CSV)])
        f = {p["name"]: p for p in plan["files"]}
        self.assertIn("2 YNAB accounts", f["register.csv"]["note"])
        decisions = [dict(p, account_id="chk") for p in plan["files"]]
        results = {r["name"]: r for r in
                   submit_and_run(self.conn, plan["token"], decisions)}
        self.assertFalse(results["register.csv"]["ok"])
        self.assertIn("YNAB accounts", results["register.csv"]["error"])
        self.assertTrue(results["bank.csv"]["ok"])
        self.assertEqual(ynab_rows(self.conn, "chk"), {})

    def test_a_single_account_register_still_goes_to_the_pick_with_the_same_ids(self):
        from oikonome.sync.base import FileImportIds
        result, need = self._ask(ONE_ACCOUNT)
        self.assertIsNone(need)
        self.assertEqual(result["imported"], 2)
        ids = FileImportIds("ynab", "chk")
        want = {ids.id_for(dt.date(2026, 7, 10), 42.5, "Safeway"),
                ids.id_for(dt.date(2026, 7, 12), 100.0, "Transfer : Visa")}
        self.assertEqual(set(ynab_rows(self.conn, "chk")), want)


class YnabRegisterAccountsApiTests(unittest.TestCase):
    """The two doors a signed-in client walks: the upload answers with the
    accounts to place, the answer imports them."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"ynab-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def _rows(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return ynab_rows(conn, "chk"), ynab_rows(conn, "card")
        finally:
            conn.close()

    def test_an_upload_without_the_choice_is_refused_naming_the_accounts(self):
        r = self.client.post(
            "/api/import",
            files={"file": ("register.csv", REGISTER.encode(), "text/csv")},
            data={"account_id": "chk", "amount_sign": "bank"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIsNone(body["mapping_needed"])
        self.assertIn("Checking", body["result"]["error"])
        self.assertIn("Visa", body["result"]["error"])

    def test_the_choice_round_trip_places_each_account(self):
        r = self.client.post(
            "/api/import",
            files={"file": ("register.csv", REGISTER.encode(), "text/csv")},
            data={"account_id": "chk", "amount_sign": "bank",
                  "ynab_choice": "1"})
        self.assertEqual(r.status_code, 200, r.text)
        need = r.json()["mapping_needed"]
        self.assertEqual(need["kind"], "ynab_accounts")
        r = self.client.post("/api/import/ynab-accounts", json={
            "token": need["token"],
            "accounts": {"Checking": "chk", "Visa": "card"},
            "remove_earlier": {}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("error", r.json()["result"])
        chk, card = self._rows()
        self.assertEqual(accounts_of(chk), {"Checking"})
        self.assertEqual(accounts_of(card), {"Visa"})
        # a malformed answer is refused before anything is read
        r = self.client.post("/api/import/ynab-accounts", json={
            "token": need["token"], "accounts": ["chk"]})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
