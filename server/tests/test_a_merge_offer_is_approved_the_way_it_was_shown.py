"""Approving a merge offer merges the way the person was shown it.

A pending offer is turned in place when the ledger grows and the other
side becomes the survivor. An approve that names the survivor it was
shown is refused as a conflict when the stored offer now points the other
way, so the client lists it again instead of renaming the long history
after the name the person was asked to retire. That holds for "Merge all"
as much as for a single approve.
"""

import datetime as dt
import os
import pathlib
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import merchant_dedup, merchant_identity, merchant_merge

from .util import _ensure_db, add_txn, make_db, seed_accounts, write_config

APP = pathlib.Path(__file__).resolve().parents[2]

LEFT = "Fernwood Bakery"
RIGHT = "Fernwood Bakehouse"


class AnApproveNamesTheSurvivorItWasShown(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        for n, name in enumerate((LEFT, LEFT, RIGHT)):
            add_txn(self.conn, dt.date(2026, 5, 3 + n), 12.0 + n,
                    name.upper(), merchant=name, txn_id=f"f{n}")
        merchant_dedup.apply(self.conn)
        merchant_identity.resolve(self.conn)
        self.ids = {r["name"]: r["id"] for r in self.conn.execute(
            "SELECT id::text AS id, name FROM merchants "
            "WHERE merged_into IS NULL").fetchall()}
        self.conn.execute(
            "INSERT INTO merchant_merge_proposals (id, from_merchant_id, "
            " into_merchant_id) VALUES ('mm:fern', %s, %s)",
            (self.ids[RIGHT], self.ids[LEFT]))

    def _turn(self):
        """What a later sync does when the other side outgrows it."""
        self.conn.execute(
            "UPDATE merchant_merge_proposals SET from_merchant_id = %s, "
            "into_merchant_id = %s WHERE id = 'mm:fern'",
            (self.ids[LEFT], self.ids[RIGHT]))

    def _live(self):
        return {r["name"] for r in self.conn.execute(
            "SELECT name FROM merchants WHERE merged_into IS NULL")}

    def test_an_offer_turned_since_it_was_shown_is_a_conflict(self):
        shown = self.ids[LEFT]
        self._turn()
        for bulk in (False, True):
            got = merchant_merge.decide(self.conn, "mm:fern", "approve",
                                        bulk=bulk, shown_into=shown)
            self.assertTrue(got.get("conflict"), got)
        self.assertEqual(self._live(), {LEFT, RIGHT})
        self.assertEqual(self.conn.execute(
            "SELECT status FROM merchant_merge_proposals WHERE id='mm:fern'"
        ).fetchone()["status"], "pending")

    def test_the_survivor_it_was_shown_is_the_one_kept(self):
        got = merchant_merge.decide(self.conn, "mm:fern", "approve",
                                    shown_into=self.ids[LEFT])
        self.assertNotIn("error", got)
        self.assertEqual(self._live(), {LEFT})


class TheMergeDoorTakesTheShownSurvivor(unittest.TestCase):
    """POST /api/merchants/merge with the `into_id` the list showed."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        r = cls.client.post("/api/signup", data={
            "email": f"mergeshown-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        tag = uuid.uuid4().hex[:6].upper()
        self.left, self.right = f"Wren {tag} Bakery", f"Wren {tag} Bakehouse"
        self.pid = f"mm:{tag}"
        conn = tenancy.tenant_connect(self.tid)
        try:
            self.ids = {}
            for name in (self.left, self.right):
                self.ids[name] = str(conn.execute(
                    "INSERT INTO merchants (name, name_source) VALUES "
                    "(%s, 'plaid') RETURNING id", (name,)).fetchone()["id"])
                t = add_txn(conn, dt.date(2026, 5, 3), 12.0, name.upper())
                conn.execute("UPDATE transactions SET merchant_id = %s "
                             "WHERE id = %s", (self.ids[name], t))
            conn.execute(
                "INSERT INTO merchant_merge_proposals (id, from_merchant_id,"
                " into_merchant_id) VALUES (%s, %s, %s)",
                (self.pid, self.ids[self.right], self.ids[self.left]))
        finally:
            conn.close()

    def _turn(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute(
                "UPDATE merchant_merge_proposals SET from_merchant_id = %s, "
                "into_merchant_id = %s WHERE id = %s",
                (self.ids[self.left], self.ids[self.right], self.pid))
        finally:
            conn.close()

    def _status(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return conn.execute(
                "SELECT status FROM merchant_merge_proposals WHERE id = %s",
                (self.pid,)).fetchone()["status"]
        finally:
            conn.close()

    def _answer(self, **extra):
        return self.client.post("/api/merchants/merge", json={
            "pid": self.pid, "action": "approve", **extra})

    def test_a_turned_offer_answered_with_the_survivor_shown_is_409(self):
        shown = self.ids[self.left]
        self._turn()
        for bulk in (False, True):
            r = self._answer(into_id=shown, bulk=bulk)
            self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self._status(), "pending")

    def test_the_current_survivor_merges(self):
        self._turn()
        r = self._answer(into_id=self.ids[self.right])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["from"], r.json()["into"]),
                         (self.left, self.right))

    def test_an_older_client_approves_an_offer_never_turned(self):
        r = self._answer()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["from"], r.json()["into"]),
                         (self.right, self.left))

    def test_an_older_client_cannot_approve_an_offer_a_sync_turned(self):
        """An approve that does not say which survivor it was shown can
        not tell a listing from before the turn from one after it, so it
        is refused once a sync has turned the offer: never merged the
        other way round from the one on screen."""
        tag = uuid.uuid4().hex[:6].upper()
        big_name, new_name = f"Juniper {tag} Market", f"Junipers {tag} Market"
        conn = tenancy.tenant_connect(self.tid)
        try:
            ids = {}
            for name, n, start, ent in (
                    (big_name, 12, dt.date(2025, 1, 6), None),
                    (new_name, 2, dt.date(2026, 4, 2), f"ent-{tag}")):
                ids[name] = str(conn.execute(
                    "INSERT INTO merchants (name, name_source, "
                    "plaid_entity_id) VALUES (%s, %s, %s) RETURNING id",
                    (name, "plaid" if ent else "layer1", ent)
                ).fetchone()["id"])
                for d in range(n):
                    t = add_txn(conn, start + dt.timedelta(days=7 * d), 20.0,
                                name.upper(), merchant=name)
                    conn.execute("UPDATE transactions SET merchant_id = %s "
                                 "WHERE id = %s", (ids[name], t))
            pid = merchant_merge._sig(ids[big_name], ids[new_name])
            # listed while stored the wrong way round: the big side goes
            conn.execute(
                "INSERT INTO merchant_merge_proposals (id, from_merchant_id,"
                " into_merchant_id, evidence) VALUES (%s, %s, %s,"
                " '{\"signals\": [\"one name\"]}'::jsonb)",
                (pid, ids[big_name], ids[new_name]))
            self.assertGreaterEqual(
                merchant_merge.run(conn).get("turned", 0), 1)
        finally:
            conn.close()
        for bulk in (False, True):
            r = self.client.post("/api/merchants/merge", json={
                "pid": pid, "action": "approve", "bulk": bulk})
            self.assertEqual(r.status_code, 409, r.text)
        conn = tenancy.tenant_connect(self.tid)
        try:
            live = {r["name"] for r in conn.execute(
                "SELECT name FROM merchants WHERE merged_into IS NULL "
                "AND id::text = ANY(%s)", (list(ids.values()),))}
        finally:
            conn.close()
        self.assertEqual(live, {big_name, new_name})
        # the survivor the list shows now still merges
        r = self.client.post("/api/merchants/merge", json={
            "pid": pid, "action": "approve", "into_id": ids[big_name]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["into"], big_name)

    def test_an_older_client_merges_all_of_a_family_it_listed(self):
        """The family below, approved by a phone that sends no survivor:
        the second offer followed the first approve's fold, which keeps
        the merge it was listed as, so it still merges."""
        tag = uuid.uuid4().hex[:6].upper()
        full, cut, mid = (f"Quince {tag} Toyworks Market",
                          f"Quince {tag} Toyw", f"Quince {tag} Toywor")
        conn = tenancy.tenant_connect(self.tid)
        try:
            ids = {}
            for name, n in ((full, 5), (cut, 8), (mid, 2)):
                ids[name] = str(conn.execute(
                    "INSERT INTO merchants (name, name_source) VALUES "
                    "(%s, 'layer1') RETURNING id", (name,)).fetchone()["id"])
                for i in range(n):
                    t = add_txn(conn, dt.date(2026, 4, 1 + i), 20.0,
                                name.upper())
                    conn.execute("UPDATE transactions SET merchant_id = %s "
                                 "WHERE id = %s", (ids[name], t))
            for pid, frm in ((f"g1:{tag}", full), (f"g2:{tag}", mid)):
                conn.execute(
                    "INSERT INTO merchant_merge_proposals (id, "
                    "from_merchant_id, into_merchant_id, evidence) VALUES "
                    "(%s, %s, %s, '{\"signals\": [\"a reason\"]}'::jsonb)",
                    (pid, ids[frm], ids[cut]))
        finally:
            conn.close()
        for pid in (f"g1:{tag}", f"g2:{tag}"):
            r = self.client.post("/api/merchants/merge", json={
                "pid": pid, "action": "approve", "bulk": True})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["into"], full)

    def test_merge_all_of_a_family_merges_every_offer_it_listed(self):
        """Three spellings of one name, both offers stored onto the cut
        one: approving the first folds the cut spelling into the full
        name, and the second offer — re-pointed onto it — still merges
        with the survivor it was listed with."""
        tag = uuid.uuid4().hex[:6].upper()
        full, cut, mid = (f"Quince {tag} Toyworks Market",
                          f"Quince {tag} Toyw", f"Quince {tag} Toywor")
        conn = tenancy.tenant_connect(self.tid)
        try:
            ids = {}
            for name, n in ((full, 5), (cut, 8), (mid, 2)):
                ids[name] = str(conn.execute(
                    "INSERT INTO merchants (name, name_source) VALUES "
                    "(%s, 'layer1') RETURNING id", (name,)).fetchone()["id"])
                for i in range(n):
                    t = add_txn(conn, dt.date(2026, 4, 1 + i), 20.0,
                                name.upper())
                    conn.execute("UPDATE transactions SET merchant_id = %s "
                                 "WHERE id = %s", (ids[name], t))
            for pid, frm in ((f"f1:{tag}", full), (f"f2:{tag}", mid)):
                conn.execute(
                    "INSERT INTO merchant_merge_proposals (id, "
                    "from_merchant_id, into_merchant_id, evidence) VALUES "
                    "(%s, %s, %s, '{\"signals\": [\"a reason\"]}'::jsonb)",
                    (pid, ids[frm], ids[cut]))
            listed = {o["id"]: o for o in merchant_merge.pending(conn)}
        finally:
            conn.close()
        for pid in (f"f1:{tag}", f"f2:{tag}"):
            self.assertTrue(listed[pid]["bulk"])
            r = self.client.post("/api/merchants/merge", json={
                "pid": pid, "action": "approve", "bulk": True,
                "into_id": listed[pid]["into_id"]})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["into"], full)


class BothClientsSendTheShownSurvivor(unittest.TestCase):
    def test_web_and_phone_send_into_id_on_every_approve(self):
        for path in ("webapp/src/api/client.ts", "mobile/src/lib/api.ts"):
            src = (APP / path).read_text()
            call = src[src.index("merchantMerge:"):]
            call = call[:call.index("/api/merchants/merge") + 400]
            self.assertIn("into_id", call, path)
        for path in ("webapp/src/pages/Merchants.tsx",
                     "mobile/src/app/merchants.tsx"):
            src = (APP / path).read_text()
            for chunk in src.split("merchantMerge(")[1:]:
                self.assertIn("into_id", chunk[:chunk.index(")")], path)


if __name__ == "__main__":
    unittest.main()
