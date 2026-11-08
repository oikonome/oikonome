"""Account names and owner labels are bounded at the door.

An account's display name and owner label are not stored once and read
once: they are copied onto every ledger and spend row of that account, so
a megabyte label turns a month of rows into hundreds of megabytes per
request on a process every household shares. And the name given to a new
manual account becomes its primary-key id, where a few thousand characters
overflow the index and surfaced as a bare 500. Every door that writes one
of these labels refuses an oversized or control-character value with a
400, and a label at the cap still saves.
"""

import os
import json
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, add_txn, seed_accounts


def _client():
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    client = TestClient(appmod.app)
    r = client.post("/api/signup", data={
        "email": f"labels-{uuid.uuid4().hex[:10]}@example.dev",
        "password": "correct-horse-battery"})
    assert r.status_code == 200, r.text
    return client, client.get("/api/me").json()["tenant_id"]


class AccountLabelsAreBounded(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client, cls.tid = _client()
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            add_txn(conn, "2026-08-01", 20, "Cedar Lane Cleaners",
                    txn_id="lbl-t1")
        finally:
            conn.close()

    def _stored(self, sql, *args):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return conn.execute(sql, args).fetchone()
        finally:
            conn.close()

    def test_an_oversized_account_rename_is_refused_and_not_stored(self):
        r = self.client.post("/api/accounts/rename",
                             json={"account_id": "card", "name": "A" * 5000})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("too long", r.text)
        row = self._stored("SELECT display_name FROM accounts WHERE id=%s",
                           "card")
        self.assertNotEqual(row["display_name"], "A" * 5000)

    def test_an_oversized_account_owner_is_refused(self):
        r = self.client.post("/api/accounts/card/owner",
                             json={"owner": "B" * 5000})
        self.assertEqual(r.status_code, 400, r.text)
        row = self._stored("SELECT owner FROM accounts WHERE id=%s", "card")
        self.assertIsNone(row["owner"])

    def test_an_oversized_transaction_owner_is_refused(self):
        r = self.client.post("/api/transactions/lbl-t1/owner",
                             json={"owner": "C" * 5000})
        self.assertEqual(r.status_code, 400, r.text)
        row = self._stored(
            "SELECT owner_override FROM transactions WHERE id=%s", "lbl-t1")
        self.assertIsNone(row["owner_override"])

    def test_a_huge_manual_account_name_is_a_400_not_a_500(self):
        import secrets
        r = self.client.post("/api/accounts/add",
                             json={"name": secrets.token_hex(2000)})
        self.assertEqual(r.status_code, 400, r.text)

    def test_the_form_door_holds_a_new_account_name_to_the_same_rule(self):
        import secrets
        for name in (secrets.token_hex(2000), "Bad\x1bName"):
            r = self.client.post("/accounts/add", data={"name": name},
                                 follow_redirects=False)
            self.assertEqual(r.status_code, 400, r.text[:200])

    def test_a_new_account_keeps_no_full_number_in_its_id_or_name(self):
        # the id is permanent and rides every exported row, URL and log
        # line; both doors that make a manual account cut the number first
        for door, kwargs in (
                ("/api/accounts/add",
                 {"json": {"name": "Visa 4111111111111111"}}),
                ("/accounts/add",
                 {"data": {"name": "Amex 371449635398431"},
                  "follow_redirects": False})):
            r = self.client.post(door, **kwargs)
            self.assertIn(r.status_code, (200, 303), f"{door}: {r.text}")
        conn = tenancy.tenant_connect(self.tid)
        try:
            rows = conn.execute(
                "SELECT id, name FROM accounts WHERE item_id='manual'"
                " AND (name LIKE 'Visa%%' OR name LIKE 'Amex%%')").fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 2, rows)
        for row in rows:
            self.assertNotRegex(row["id"], r"\d{8,}")
            self.assertNotRegex(row["name"], r"\d{8,}")
        self.assertEqual(sorted(r["name"] for r in rows),
                         ["Amex \u20268431", "Visa \u20261111"])

    def test_a_card_number_typed_in_groups_is_cut_like_an_unbroken_one(self):
        # people type a card number the way the card prints it, in groups
        # of four split by spaces or dashes; the grouping must not carry
        # the full number past the cut into the id or the stored name
        for door, kwargs in (
                ("/api/accounts/add",
                 {"json": {"name": "Debit 4111 1111 1111 1111"}}),
                ("/accounts/add",
                 {"data": {"name": "Debit 5500-0000-0000-0004"},
                  "follow_redirects": False})):
            r = self.client.post(door, **kwargs)
            self.assertIn(r.status_code, (200, 303), f"{door}: {r.text}")
        conn = tenancy.tenant_connect(self.tid)
        try:
            rows = conn.execute(
                "SELECT id, name FROM accounts WHERE item_id='manual'"
                " AND name LIKE 'Debit%%'").fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 2, rows)
        for row in rows:
            self.assertNotRegex(row["id"], r"\d(?:[ -]?\d){7,}")
            self.assertNotRegex(row["name"], r"\d(?:[ -]?\d){7,}")
        self.assertEqual(sorted(r["name"] for r in rows),
                         ["Debit \u20260004", "Debit \u20261111"])

    def test_a_rename_spelling_a_card_number_keeps_only_the_last_four(self):
        # a rename is shown on every page and rides every export, as the
        # name a new account is given does
        r = self.client.post("/api/accounts/rename",
                             json={"account_id": "card",
                                   "name": "Visa 4111 1111 1111 1111"})
        self.assertEqual(r.status_code, 200, r.text)
        row = self._stored("SELECT display_name FROM accounts WHERE id=%s",
                           "card")
        self.assertEqual(row["display_name"], "Visa \u20261111")
        # the client shows and caches what was stored, not what was typed
        self.assertEqual(r.json().get("name"), "Visa \u20261111")

    def test_a_rename_never_copies_a_full_number_into_the_activity_log(self):
        # the label before a rename may have been stored whole by an
        # older door; the activity log rides every export, so the
        # record of the rename carries it cut
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute("UPDATE accounts SET display_name = %s"
                         " WHERE id = 'chk'", ("Visa 4111 1111 1111 1111",))
        finally:
            conn.close()
        r = self.client.post("/api/accounts/rename",
                             json={"account_id": "chk", "name": "Joint"})
        self.assertEqual(r.status_code, 200, r.text)
        row = self._stored(
            "SELECT label, detail FROM activity_log WHERE kind = 'account'"
            " AND action = 'renamed' AND target = 'chk'"
            " ORDER BY at DESC LIMIT 1")
        self.assertIsNotNone(row)
        self.assertEqual(row["label"], "Visa \u20261111")
        detail = row["detail"] if isinstance(row["detail"], dict) \
            else json.loads(row["detail"])
        self.assertEqual(detail["before"], "Visa \u20261111")
        self.assertEqual(detail["after"], "Joint")

    def test_control_characters_are_refused_on_every_label_door(self):
        for path, body in (
                ("/api/accounts/rename",
                 {"account_id": "chk", "name": "Bad\x07Name"}),
                ("/api/accounts/chk/owner", {"owner": "Bad\x85Name"}),
                ("/api/transactions/lbl-t1/owner", {"owner": "Bad\x00"}),
                ("/api/accounts/add", {"name": "Bad\x1bName"})):
            r = self.client.post(path, json=body)
            self.assertEqual(r.status_code, 400, f"{path}: {r.text}")

    def test_labels_at_the_cap_still_save(self):
        r = self.client.post("/api/accounts/rename",
                             json={"account_id": "chk", "name": "D" * 200})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post("/api/accounts/chk/owner",
                             json={"owner": "E" * 200})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post("/api/transactions/lbl-t1/owner",
                             json={"owner": "F" * 200})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post("/api/accounts/add", json={"name": "G" * 200})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            self._stored("SELECT display_name FROM accounts WHERE id=%s",
                         "chk")["display_name"], "D" * 200)


if __name__ == "__main__":
    unittest.main()
