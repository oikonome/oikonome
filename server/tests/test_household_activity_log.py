"""The household activity log: who changed which category, bill, note,
split or rule, and when.

What breaks if these fail: a member's edit is recorded under the member's
address (not the owner's, not nobody's); the sentence names the thing and
the change; a viewer can read the log and nothing else; a removed member
keeps their name on their rows; the log survives an export/restore round
trip; the nightly prune keeps two years and no more; the settings entry
names keys and never a value.
"""

import datetime as dt
import io
import unittest
import uuid
import zipfile

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import activity

from .util import (TODAY, _ensure_db, add_txn, make_db,
                   seed_accounts, write_config)

PW = "correct-horse-battery"


class SentenceTests(unittest.TestCase):
    """The words are composed once, at write time."""

    def test_category_change_names_before_and_after(self):
        s = activity.sentence("category", "set", "Big Box $80.00 on 01/15/26",
                              {"before": "GENERAL_MERCHANDISE",
                               "after": "FOOD_AND_DRINK", "scope": "one"})
        self.assertEqual(s, "changed the category of Big Box $80.00 on "
                            "01/15/26 from GENERAL MERCHANDISE to FOOD AND DRINK")

    def test_teaching_the_merchant_is_said(self):
        s = activity.sentence("category", "set", "X $1.00 on 01/01/26",
                              {"before": None, "after": "FOOD_AND_DRINK",
                               "scope": "all", "merchant": "Corner Market"})
        self.assertIn("taught the merchant Corner Market", s)
        self.assertTrue(s.startswith("filed X $1.00 on 01/01/26 under"))

    def test_note_and_split_and_bill_shapes(self):
        self.assertEqual(
            activity.sentence("note", "added", "T", {"after": "call the bank"}),
            "added a note on T: “call the bank”")
        self.assertEqual(activity.sentence("note", "cleared", "T", None),
                         "removed the note on T")
        s = activity.sentence("split", "set", "T", {"parts": [
            {"category": "FOOD_AND_DRINK", "amount": 30},
            {"category": "GENERAL_MERCHANDISE", "amount": 50}]})
        self.assertEqual(s, "split T 2 ways: FOOD AND DRINK $30.00, "
                            "GENERAL MERCHANDISE $50.00")
        s = activity.sentence("bill", "edited", "Power", {
            "before": {"amount": 100, "frequency": "MONTHLY", "interval": 1},
            "after": {"amount": 120, "frequency": "MONTHLY", "interval": 1}})
        self.assertEqual(s, "edited the bill Power: amount $100.00 → $120.00")
        s = activity.sentence("bill", "added", "Gym", {"after": {
            "amount": 40, "frequency": "MONTHLY", "interval": 1,
            "due_on": "2026-08-01"}})
        self.assertEqual(s, "added the bill Gym ($40.00, monthly, due 08/01/26)")

    def test_settings_entry_names_keys_only(self):
        s = activity.sentence("settings", "changed", "", {"keys": [
            "budget", "smtp_password", "savings_goals"]})
        self.assertEqual(s, "changed settings: budget, smtp password, "
                            "savings goals")

    def test_a_long_note_is_clipped(self):
        s = activity.sentence("note", "added", "T", {"after": "x" * 500})
        self.assertLess(len(s), 120)
        self.assertTrue(s.endswith("…”"))

    def test_unknown_shape_still_reads(self):
        self.assertEqual(activity.sentence("widget", "frobbed", "W", None),
                         "frobbed widget W")


class RecordAndListTests(unittest.TestCase):
    """The engine writes and reads under RLS."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.txn = add_txn(self.conn, TODAY, 42.5, "CORNER MARKET")

    def tearDown(self):
        # a pooled tenant connection: left open, six of the pool's ten
        # slots stay checked out for the rest of the suite, and a later
        # test that needs three at once (a background sync under a held
        # lock) times out on the pool
        self.conn.close()

    def _user(self, email="pat@example.dev", role="member"):
        return {"user_id": str(uuid.uuid4()), "email": email, "role": role,
                "tenant_id": "x"}

    def test_record_then_list_newest_first(self):
        activity.record(self.conn, self._user(), "note", "added",
                        target=self.txn, label="T", detail={"after": "hi"})
        activity.record(self.conn, self._user("lee@example.dev"), "category",
                        "set", target=self.txn, label="T",
                        detail={"before": None, "after": "FOOD_AND_DRINK"})
        out = activity.list_activity(self.conn)
        self.assertEqual([r["kind"] for r in out["rows"]], ["category", "note"])
        self.assertEqual(out["rows"][0]["actor"], "lee@example.dev")
        self.assertEqual(out["rows"][1]["summary"], "added a note on T: “hi”")
        self.assertFalse(out["more"])
        self.assertEqual(out["actors"], ["lee@example.dev", "pat@example.dev"])

    def test_filters_by_kind_actor_and_target(self):
        other = add_txn(self.conn, TODAY, 5, "OTHER")
        activity.record(self.conn, self._user(), "note", "added",
                        target=self.txn, label="T", detail={"after": "a"})
        activity.record(self.conn, self._user(), "category", "set",
                        target=other, label="O", detail={"after": "X"})
        activity.record(self.conn, self._user("lee@example.dev"), "category",
                        "set", target=self.txn, label="T", detail={"after": "Y"})
        self.assertEqual(
            len(activity.list_activity(self.conn, kind="category")["rows"]), 2)
        self.assertEqual(
            len(activity.list_activity(self.conn, actor="LEE@example.dev")["rows"]), 1)
        self.assertEqual(
            len(activity.for_target(self.conn, self.txn)), 2)

    def test_paging_by_timestamp(self):
        base = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.timezone.utc)
        for i in range(5):
            self.conn.execute(
                "INSERT INTO activity_log (at, actor, kind, action, summary) "
                "VALUES (%s,'a@x','note','added','n')",
                (base + dt.timedelta(minutes=i),))
        p1 = activity.list_activity(self.conn, limit=2)
        self.assertTrue(p1["more"])
        self.assertEqual(len(p1["rows"]), 2)
        p2 = activity.list_activity(self.conn, limit=2, before=p1["next"])
        self.assertEqual(len(p2["rows"]), 2)
        p3 = activity.list_activity(self.conn, limit=2, before=p2["next"])
        self.assertEqual(len(p3["rows"]), 1)
        self.assertFalse(p3["more"])
        seen = {r["id"] for p in (p1, p2, p3) for r in p["rows"]}
        self.assertEqual(len(seen), 5)

    def test_script_token_and_bare_address_actors(self):
        activity.record(self.conn, {"email": "script:importer", "user_id": "u",
                                    "script_token": True}, "note", "added",
                        label="T", detail={"after": "x"})
        activity.record(self.conn, "Someone@Example.dev", "note", "added",
                        label="T", detail={"after": "x"})
        rows = activity.list_activity(self.conn)["rows"]
        self.assertEqual({r["actor"] for r in rows},
                         {"script:importer", "someone@example.dev"})
        self.assertTrue(all(r["actor_user_id"] is None for r in rows))

    def test_prune_keeps_two_years(self):
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=800)
        self.conn.execute(
            "INSERT INTO activity_log (at, actor, kind, action, summary) "
            "VALUES (%s,'a@x','note','added','old')", (old,))
        activity.record(self.conn, self._user(), "note", "added", label="T",
                        detail={"after": "new"})
        self.assertEqual(activity.prune(self.conn), 1)
        rows = activity.list_activity(self.conn)["rows"]
        self.assertEqual([r["summary"] for r in rows],
                         ["added a note on T: “new”"])

    def test_a_failed_insert_never_raises_into_the_door(self):
        # a kind longer than any column would take is still just a warning
        rid = activity.record(self.conn, self._user(), "note", "added",
                              label="T", detail={"after": "x"})
        self.assertIsNotNone(rid)
        # detail must be an object: a list is refused by the CHECK, and
        # the door that called record() must not see that
        self.assertIsNone(activity.record(
            self.conn, self._user(), "note", "added", label="T",
            detail=[1, 2]))  # type: ignore[arg-type]
        self.assertEqual(len(activity.list_activity(self.conn)["rows"]), 1)


class ApiTests(unittest.TestCase):
    """The doors write the log; the log reads back to everyone."""

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.app = app
        cls.owner = TestClient(app)
        cls.owner_email = f"owner-{uuid.uuid4().hex[:8]}@example.dev"
        cls.owner.post("/api/signup", data={"email": cls.owner_email,
                                            "password": PW})
        cls.tid = cls.owner.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()
        cls.member, cls.member_email = cls._claim(cls, "member")
        cls.viewer, cls.viewer_email = cls._claim(cls, "viewer")

    def _claim(self, role):
        r = self.owner.post("/api/invites",
                            json={"label": role, "role": role, "password": PW})
        assert r.status_code == 200, r.text
        token = r.json()["url"].rsplit("token=", 1)[1]
        client = TestClient(self.app)
        email = f"{role}-{uuid.uuid4().hex[:8]}@example.dev"
        got = client.post("/api/invite/claim", json={
            "token": token, "email": email, "password": "family-member-pass"})
        assert got.status_code == 200, got.text
        return client, email

    def _conn(self):
        return tenancy.tenant_connect(self.tid)

    def _txn(self, amount=12.5, name="CORNER MARKET"):
        conn = self._conn()
        try:
            return add_txn(conn, TODAY, amount, name)
        finally:
            conn.close()

    def _rows(self, **q):
        r = self.owner.get("/api/activity", params=q)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["rows"]

    def test_member_category_change_is_recorded_under_the_member(self):
        txn = self._txn(80, "BIG BOX STORE")
        r = self.member.post(f"/api/transactions/{txn}/category",
                             json={"category": "FOOD_AND_DRINK"})
        self.assertEqual(r.status_code, 200, r.text)
        row = self._rows(target=txn)[0]
        self.assertEqual(row["actor"], self.member_email)
        self.assertEqual(row["kind"], "category")
        self.assertIn("BIG BOX", row["label"].upper())
        self.assertIn("$80.00", row["summary"])
        self.assertIn("FOOD AND DRINK", row["summary"])
        self.assertEqual(row["detail"]["before"], "GENERAL_MERCHANDISE")
        # and clearing it is a second row that names what it was
        r = self.member.post(f"/api/transactions/{txn}/category",
                             json={"category": ""})
        self.assertEqual(r.status_code, 200, r.text)
        rows = self._rows(target=txn)
        self.assertEqual(rows[0]["action"], "cleared")
        self.assertIn("was FOOD AND DRINK", rows[0]["summary"])

    def test_note_set_edit_clear(self):
        txn = self._txn()
        self.owner.post(f"/api/transactions/{txn}/note", json={"note": "first"})
        self.owner.post(f"/api/transactions/{txn}/note", json={"note": "first"})
        self.owner.post(f"/api/transactions/{txn}/note", json={"note": "second"})
        self.owner.post(f"/api/transactions/{txn}/note", json={"note": ""})
        acts = [r["action"] for r in self._rows(target=txn, kind="note")]
        # the unchanged re-save is not an act
        self.assertEqual(acts, ["cleared", "edited", "added"])

    def test_split_set_and_clear(self):
        txn = self._txn(100, "BIG BOX")
        r = self.member.put(f"/api/transactions/{txn}/split", json={"parts": [
            {"category": "FOOD_AND_DRINK", "amount": 60},
            {"category": "GENERAL_MERCHANDISE", "amount": 40}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.member.delete(f"/api/transactions/{txn}/split")
        # a second delete of nothing is not an act
        self.member.delete(f"/api/transactions/{txn}/split")
        rows = self._rows(target=txn, kind="split")
        self.assertEqual([r["action"] for r in rows], ["cleared", "set"])
        self.assertIn("2 ways", rows[1]["summary"])

    def test_bill_lifecycle(self):
        payee = f"Gym {uuid.uuid4().hex[:4]}"
        r = self.member.post("/api/bills/save", json={
            "payee": payee, "amount": 40, "cadence": "MONTHLY:1"})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.member.post("/api/bills/save", json={
            "payee": payee, "amount": 45, "cadence": "MONTHLY:1"})
        self.assertEqual(r.status_code, 200, r.text)
        self.member.post("/api/bills/toggle", json={"payee": payee,
                                                    "disabled": True})
        self.member.post("/api/bills/toggle", json={"payee": payee,
                                                    "disabled": True})
        self.member.post("/api/bills/delete", json={"payee": payee})
        self.member.post("/api/bills/restore", json={"payee": payee})
        rows = self._rows(target=payee, kind="bill")
        self.assertEqual([r["action"] for r in rows],
                         ["restored", "archived", "paused", "edited", "added"])
        self.assertIn("$40.00 → $45.00", rows[3]["summary"])
        self.assertIn("$40.00, monthly", rows[4]["summary"])
        self.assertTrue(all(r["actor"] == self.member_email for r in rows))

    def test_proposal_decision(self):
        from oikonome.engine import bills
        conn = self._conn()
        try:
            pid = f"prop:{uuid.uuid4().hex}"
            bills._insert_proposal(
                conn, pid=pid, kind="add", payee="StreamBox",
                amount=12.99, frequency="MONTHLY", interval=1,
                next_due=TODAY + dt.timedelta(days=5),
                evidence={"txn_ids": []}, bill_type="occurrence")
        finally:
            conn.close()
        r = self.owner.post("/api/bills/proposal",
                            json={"pid": pid, "action": "reject"})
        self.assertEqual(r.status_code, 200, r.text)
        row = self._rows(target="StreamBox")[0]
        self.assertEqual(row["action"], "dismissed")
        self.assertEqual(row["summary"], "dismissed the offer to track StreamBox")

    def test_rule_set_and_undo(self):
        for _ in range(3):
            self._txn(9, "GAS N GO")
        r = self.member.post("/api/rules/set", json={"merchant": "Gas N Go",
                                                    "category": "TRANSPORTATION"})
        self.assertEqual(r.status_code, 200, r.text)
        rows = self._rows(kind="rule")
        self.assertEqual(rows[0]["action"], "set")
        self.assertIn("→ TRANSPORTATION", rows[0]["summary"])
        self.assertEqual(rows[0]["detail"]["count"], 3)

    def test_settings_save_names_keys_never_values(self):
        body = {"timezone": "America/Chicago"}
        r = self.owner.post("/api/settings", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        row = self._rows(kind="settings")[0]
        self.assertIn("time zone", row["summary"])
        self.assertNotIn("Chicago", row["summary"])
        self.assertNotIn("Chicago", str(row["detail"]))
        # an unchanged re-save is not an act
        n = len(self._rows(kind="settings"))
        self.owner.post("/api/settings", json=body)
        self.assertEqual(len(self._rows(kind="settings")), n)

    def test_viewer_reads_the_log_and_writes_nothing(self):
        r = self.viewer.get("/api/activity")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("rows", r.json())
        txn = self._txn()
        r = self.viewer.post(f"/api/transactions/{txn}/note",
                             json={"note": "x"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self._rows(target=txn), [])

    def test_unknown_kind_is_refused(self):
        self.assertEqual(self.owner.get("/api/activity?kind=nope").status_code,
                         400)

    def test_a_removed_member_keeps_their_name_on_their_rows(self):
        client, email = self._claim("member")
        txn = self._txn()
        client.post(f"/api/transactions/{txn}/note", json={"note": "mine"})
        uid = next(u["id"] for u in self.owner.get("/api/users").json()["users"]
                   if u["email"] == email)
        r = self.owner.request("DELETE", f"/api/users/{uid}",
                               json={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(email, [u["email"] for u in
                                 self.owner.get("/api/users").json()["users"]])
        row = self._rows(target=txn)[0]
        self.assertEqual(row["actor"], email)

    def test_export_restore_round_trip(self):
        from oikonome.sync import export, restore
        txn = self._txn(33, "ROUND TRIP")
        self.owner.post(f"/api/transactions/{txn}/note", json={"note": "keep"})
        conn = self._conn()
        try:
            z = export.build_zip(conn)
        finally:
            conn.close()
        data = z if isinstance(z, (bytes, bytearray)) else z.getvalue()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            self.assertIn("activity_log.csv", zf.namelist())
            self.assertIn("activity_log.csv", zf.read("README.txt").decode())
        dest = make_db()
        try:
            add_txn(dest, TODAY, 33, "ROUND TRIP")
            restore.restore_zip(dest, data)
            rows = activity.list_activity(dest)["rows"]
            self.assertTrue(any(r["summary"].startswith("added a note on")
                                and "keep" in r["summary"] for r in rows))
            self.assertTrue(all(r["actor_user_id"] is None for r in rows))
            n = len(rows)
            restore.restore_zip(dest, data)
            self.assertEqual(len(activity.list_activity(dest)["rows"]), n)
        finally:
            dest.close()
