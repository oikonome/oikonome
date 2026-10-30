"""A merchant-wide category write is undoable however big the merchant is.

The write's undo used to be a snapshot handed to the client and posted
back, so the undo door validated it like any other body, with caps on how
many rows and how long a name it could carry. A merchant past those caps
answered its write with an undo the door then refused: the write landed,
the toast offered Undo, and Undo said 400. Capping the write instead made
the Rules page refuse to edit a big merchant's rule at all.

The server now holds the snapshot and hands back an opaque reference, so
nothing about the merchant's size travels. Pinned here: the big and the
long-named merchant undo whole from every door that writes (history page,
Rules page, the row's "whole merchant" correction); a reference is spent
once and is only ever this household's; a snapshot posted inline by an
older client is still accepted through the capped path; the held
snapshots are pruned as the writes go.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web import data

from .util import _ensure_db, add_txn, make_db, seed_accounts

PASSWORD = "correct-horse-battery"

# one row past what an inline snapshot may carry
BIG = data.UNDO_MAX_AFFECTED + 1


def _bulk_rows(conn, name, n, primary="GENERAL_MERCHANDISE", prefix="b"):
    """`n` ledger rows for one payee, in one statement (row-by-row inserts
    of thousands would be most of the test's runtime)."""
    conn.execute(
        """INSERT INTO transactions (id, account_id, date, amount, name,
               merchant_name, category_primary, pending, removed, raw)
           SELECT %s || g, 'card', DATE '2026-01-01' + (g %% 300),
                  10 + g %% 7, %s, %s, %s, 0, 0, '{}'::jsonb
             FROM generate_series(1, %s) AS g""",
        (prefix, name, name, primary, n))
    return [f"{prefix}{i}" for i in range(1, n + 1)]


def _cats(conn, name):
    return {r["category_primary"] for r in conn.execute(
        "SELECT category_primary FROM transactions "
        "WHERE name=%s AND category_override IS NULL", (name,)).fetchall()}


class HeldUndoTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def test_the_write_hands_back_a_reference_not_the_snapshot(self):
        _bulk_rows(self.conn, "Contoso Lumber", 3)
        res = data.set_merchant_category(self.conn, "Contoso Lumber",
                                         "ENTERTAINMENT")
        self.assertEqual(set(res["undo"]), {"ref", "category"})
        self.assertEqual(res["undo"]["category"], "ENTERTAINMENT")

    def test_a_write_past_the_inline_cap_is_undone_whole(self):
        _bulk_rows(self.conn, "Fabrikam Hardware", BIG)
        res = data.set_merchant_category(self.conn, "Fabrikam Hardware",
                                         "ENTERTAINMENT")
        self.assertEqual(res["count"], BIG)
        self.assertEqual(_cats(self.conn, "Fabrikam Hardware"),
                         {"ENTERTAINMENT"})
        n = data.undo_merchant_category(self.conn, res["undo"])
        self.assertEqual(n, BIG)
        self.assertEqual(_cats(self.conn, "Fabrikam Hardware"),
                         {"GENERAL_MERCHANDISE"})
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM merchant_categories WHERE merchant=%s "
            "AND source='user'", ("Fabrikam Hardware",)).fetchone())

    def test_a_merchant_with_a_long_name_is_undone(self):
        name = ("Northwind " + "Springfield Outfitters " * 11)[:250]
        self.assertGreater(len(name), data._NAME_MAX)
        for i in range(3):
            add_txn(self.conn, f"2026-03-0{i + 1}", 12.0, name,
                    primary="SHOPPING", txn_id=f"long{i}")
        res = data.set_merchant_category(self.conn, name, "ENTERTAINMENT")
        self.assertEqual(_cats(self.conn, name), {"ENTERTAINMENT"})
        data.undo_merchant_category(self.conn, res["undo"])
        self.assertEqual(_cats(self.conn, name), {"SHOPPING"})

    def test_a_rules_page_edit_on_a_big_merchant_succeeds_and_undoes(self):
        n = BIG + 999
        _bulk_rows(self.conn, "Contoso Grocers", n, primary="SHOPPING")
        res = data.set_merchant_category(self.conn, "Contoso Grocers",
                                         "FOOD_AND_DRINK", family=False)
        self.assertEqual(res["count"], n)
        self.assertEqual(_cats(self.conn, "Contoso Grocers"),
                         {"FOOD_AND_DRINK"})
        self.assertEqual(
            data.undo_merchant_category(self.conn, res["undo"]), n)
        self.assertEqual(_cats(self.conn, "Contoso Grocers"), {"SHOPPING"})

    def test_a_rows_whole_merchant_correction_on_a_big_merchant_teaches(self):
        ids = _bulk_rows(self.conn, "Fabrikam Fuel", BIG + 1,
                         primary="TRAVEL")
        with self.conn.transaction():
            res = data.set_category(self.conn, ids[0], "TRANSPORTATION",
                                    scope="all")
        self.assertIsNotNone(res, "the teach was skipped")
        self.assertEqual(_cats(self.conn, "Fabrikam Fuel"),
                         {"TRANSPORTATION"})
        # every row but the corrected one, which carries its own answer
        self.assertEqual(
            data.undo_merchant_category(self.conn, res["undo"]), BIG)
        self.assertEqual(_cats(self.conn, "Fabrikam Fuel"), {"TRAVEL"})
        self.assertEqual(self.conn.execute(
            "SELECT category_override FROM transactions WHERE id=%s",
            (ids[0],)).fetchone()["category_override"], "TRANSPORTATION")

    def test_a_reference_is_spent_once(self):
        _bulk_rows(self.conn, "Contoso Lumber", 3)
        res = data.set_merchant_category(self.conn, "Contoso Lumber",
                                         "ENTERTAINMENT")
        data.undo_merchant_category(self.conn, res["undo"])
        data.set_merchant_category(self.conn, "Contoso Lumber", "TRAVEL")
        with self.assertRaises(ValueError):
            data.undo_merchant_category(self.conn, res["undo"])
        # the later write was not touched by the refused replay
        self.assertEqual(_cats(self.conn, "Contoso Lumber"), {"TRAVEL"})

    def test_another_households_reference_is_not_found(self):
        _bulk_rows(self.conn, "Contoso Lumber", 3)
        res = data.set_merchant_category(self.conn, "Contoso Lumber",
                                         "ENTERTAINMENT")
        other = make_db()
        self.addCleanup(other.close)
        with self.assertRaises(data.UndoNotFound):
            data.undo_merchant_category(other, res["undo"])
        # still this household's to use
        self.assertEqual(data.undo_merchant_category(self.conn, res["undo"]),
                         3)

    def test_a_malformed_reference_is_a_value_error(self):
        for ref in ("not-a-uuid", 7, None, ["x"]):
            with self.assertRaises(ValueError, msg=repr(ref)):
                data.undo_merchant_category(self.conn, {"ref": ref})

    def test_an_inline_snapshot_from_an_older_client_is_still_accepted(self):
        _bulk_rows(self.conn, "Contoso Lumber", 3)
        res = data.set_merchant_category(self.conn, "Contoso Lumber",
                                         "ENTERTAINMENT")
        inline = data.held_undo(self.conn, res["undo"])
        self.assertEqual(data.undo_merchant_category(self.conn, inline), 3)
        self.assertEqual(_cats(self.conn, "Contoso Lumber"),
                         {"GENERAL_MERCHANDISE"})

    def test_an_inline_snapshot_stays_capped(self):
        too_many = {"canons": [{"canon": "Contoso Lumber"}],
                    "affected": [{"id": f"x{i}", "prev": "TRAVEL"}
                                 for i in range(BIG)]}
        with self.assertRaises(ValueError):
            data.undo_merchant_category(self.conn, too_many)

    def test_held_snapshots_are_pruned_by_age_and_count(self):
        _bulk_rows(self.conn, "Contoso Lumber", 2)
        self.conn.execute(
            "INSERT INTO category_undo (created_at, payload) "
            "VALUES (now() - interval '8 days', '{}'::jsonb)")
        refs = []
        for i in range(data.UNDO_KEEP_NEWEST + 3):
            refs.append(data.set_merchant_category(
                self.conn, "Contoso Lumber",
                "ENTERTAINMENT" if i % 2 else "TRAVEL")["undo"])
        held = self.conn.execute(
            "SELECT count(*) AS n FROM category_undo").fetchone()["n"]
        self.assertEqual(held, data.UNDO_KEEP_NEWEST)
        self.assertEqual(data.held_undo(self.conn, refs[-1])["category"],
                         refs[-1]["category"])
        with self.assertRaises(data.UndoNotFound):
            data.held_undo(self.conn, refs[0])


def _app():
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    return appmod.app


def _owner_client(app):
    client = TestClient(app)
    email = f"undo-ref-{uuid.uuid4().hex[:10]}@example.dev"
    r = client.post("/api/signup", data={"email": email,
                                         "password": PASSWORD})
    assert r.status_code == 200, r.text
    return client, client.get("/api/me").json()["tenant_id"]


class HeldUndoDoorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = _app()
        cls.client, cls.tid = _owner_client(app)
        cls.other, _ = _owner_client(app)
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            _bulk_rows(conn, "Fabrikam Hardware", BIG, prefix="api")
            _bulk_rows(conn, "Contoso Rail", 4, primary="SHOPPING",
                       prefix="rail")
        finally:
            conn.close()

    def _undo(self, client, undo):
        return client.post("/api/bills/merchant-category/undo",
                           json={"undo": undo})

    def test_the_undo_door_restores_a_big_merchant_and_refuses_a_replay(self):
        r = self.client.post("/api/rules/set",
                             json={"merchant": "Fabrikam Hardware",
                                   "category": "ENTERTAINMENT"})
        self.assertEqual(r.status_code, 200, r.text)
        undo = r.json()["undo"]
        # another household cannot see it
        self.assertEqual(self._undo(self.other, undo).status_code, 404)
        u = self._undo(self.client, undo)
        self.assertEqual(u.status_code, 200, u.text)
        self.assertEqual(u.json()["restored"], BIG)
        self.assertEqual(self._undo(self.client, undo).status_code, 400)
        self.assertEqual(
            self._undo(self.client, {"ref": "nope"}).status_code, 400)
        self.assertEqual(
            self._undo(self.client, {"ref": str(uuid.uuid4())}).status_code,
            404)

    def test_the_rows_whole_merchant_door_reports_its_teach(self):
        r = self.client.post("/api/transactions/rail1/category",
                             json={"category": "TRAVEL", "scope": "all"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["rule_written"])
        u = self._undo(self.client, r.json()["undo"])
        self.assertEqual(u.status_code, 200, u.text)
        self.assertEqual(u.json()["restored"], 3)


if __name__ == "__main__":
    unittest.main()
