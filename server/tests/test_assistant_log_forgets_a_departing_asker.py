"""An answer still in flight when its asker leaves the household must not
put their name back on the household's question log.

Asking takes as long as the model takes, and a member can be removed — or
moved out into a household of their own — in that time. The removal blanks
the asker's rows; the late answer then lands afterwards. Recording the
answer checks the asker still belongs to THIS household and holds that
answer until the row is written, so a departure either finishes first (the
answer is kept without a name) or waits for the row and then blanks it.
Either order ends with the question kept and the name gone."""

import os
import threading
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import assistant

from .util import _ensure_db, make_db

OUT = {"answer": "ok", "blocks": [], "tools_used": []}


class _PausedAfterCheck:
    """A tenant connection whose record_turn stops between the membership
    check and the insert, until the test lets it go."""

    def __init__(self, conn):
        self._c = conn
        self.checked = threading.Event()
        self.go = threading.Event()

    def __getattr__(self, name):
        return getattr(self._c, name)

    def execute(self, sql, *a, **k):
        if sql.lstrip().startswith("INSERT INTO assistant_turns"):
            self.checked.set()
            self.go.wait(15)
        return self._c.execute(sql, *a, **k)


def _in_flight(conn, uid, question):
    """Start an ask's recording on a thread; it parks after its check."""
    paused = _PausedAfterCheck(conn)
    errs: list[BaseException] = []

    def run():
        try:
            assistant.record_turn(
                paused, {"user_id": uid, "email": "asker@example.dev"},
                question, OUT)
        except BaseException as e:                       # noqa: BLE001
            errs.append(e)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert paused.checked.wait(15), "the ask never reached its insert"
    return paused, t, errs


def _row(tid, question):
    admin = tenancy.admin_connect()
    try:
        return admin.execute(
            "SELECT asked_by_id, asked_by FROM assistant_turns "
            "WHERE tenant_id = %s AND question = %s",
            (tid, question)).fetchone()
    finally:
        admin.close()


class RecordTurnHoldsTheAskerTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]

    def tearDown(self):
        self.conn.close()

    def _member(self, tid=None):
        admin = tenancy.admin_connect()
        try:
            return str(admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s, %s, 'x', 'member') RETURNING id",
                (tid or self.tid,
                 f"m-{uuid.uuid4().hex[:8]}@example.dev")).fetchone()["id"])
        finally:
            admin.close()

    def test_a_present_member_s_ask_keeps_their_name(self):
        uid = self._member()
        assistant.record_turn(self.conn, {"user_id": uid,
                                          "email": "Me@Example.dev"},
                              "mine", OUT)
        r = _row(self.tid, "mine")
        self.assertEqual(str(r["asked_by_id"]), uid)
        self.assertEqual(r["asked_by"], "me@example.dev")

    def test_a_removal_during_the_recording_waits_and_then_blanks_it(self):
        uid = self._member()
        paused, asker, errs = _in_flight(self.conn, uid, "late")
        deleted = threading.Event()

        def remove():
            admin = tenancy.admin_connect()
            try:
                admin.execute("SET lock_timeout = '20s'")
                with admin.transaction():
                    admin.execute("DELETE FROM users WHERE id = %s", (uid,))
                    assistant.forget_asker(admin, self.tid, uid)
                deleted.set()
            finally:
                admin.close()

        remover = threading.Thread(target=remove, daemon=True)
        remover.start()
        # the check holds the member's row until the answer is written
        waited = not deleted.wait(1.0)
        paused.go.set()
        asker.join(15)
        remover.join(25)
        self.assertEqual(errs, [])
        self.assertTrue(waited, "the removal did not wait for the answer")
        self.assertTrue(deleted.is_set())
        r = _row(self.tid, "late")
        self.assertIsNotNone(r, "the question itself must be kept")
        self.assertIsNone(r["asked_by_id"])
        self.assertEqual(r["asked_by"], "")

    def test_a_member_now_in_another_household_is_not_named_here(self):
        admin = tenancy.admin_connect()
        try:
            other = tenancy.create_tenant(admin,
                                          f"other-{uuid.uuid4().hex[:8]}")
        finally:
            admin.close()
        uid = self._member(other)       # their row exists — elsewhere
        assistant.record_turn(self.conn, {"user_id": uid,
                                          "email": "gone@example.dev"},
                              "after the move", OUT)
        r = _row(self.tid, "after the move")
        self.assertIsNone(r["asked_by_id"])
        self.assertEqual(r["asked_by"], "")


class ReclaimMoveOutForgetsTheAskerTests(unittest.TestCase):
    """The reclaim's member road moves a person out into a household of
    their own: the household they leave keeps their questions, not their
    name — the same as every other way of leaving."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()
        import oikonome.web.app as appmod
        cls.appmod = appmod

    def setUp(self):
        self._p = [
            mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1",
                                         "OIKONOME_OPEN_SIGNUP": "1"}),
            mock.patch.object(self.appmod, "DEV_MODE", False),
            mock.patch.object(self.appmod, "_deliver_verification"),
        ]
        self.sent: list[tuple[str, str]] = []
        self._p.append(mock.patch.object(
            self.appmod, "_deliver_reclaim",
            side_effect=lambda email, token: self.sent.append((email, token))))
        for p in self._p:
            p.start()
        from oikonome.web import security
        security._limiter._hits.clear()

    def tearDown(self):
        for p in reversed(self._p):
            p.stop()

    def _signup(self, email):
        return TestClient(self.appmod.app).post(
            "/api/signup", data={"email": email, "password": "correct-horse-battery",
                                 "invite": "", "ref": ""})

    def _squatted_member(self):
        host = f"ahost-{uuid.uuid4().hex[:8]}@x.dev"
        member = f"amem-{uuid.uuid4().hex[:8]}@x.dev"
        self.assertEqual(self._signup(host).status_code, 200)
        admin = tenancy.admin_connect()
        try:
            tid = str(admin.execute("SELECT tenant_id FROM users WHERE email=%s",
                                    (host,)).fetchone()["tenant_id"])
            uid = str(admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s, %s, 'x', 'member') RETURNING id",
                (tid, member)).fetchone()["id"])
        finally:
            admin.close()
        self.assertEqual(self._signup(member).status_code, 409)
        return tid, uid, self.sent[-1][1]

    def _finish(self, token):
        return TestClient(self.appmod.app).post(
            "/signup/reclaim",
            data={"token": token, "password": "clicker-chosen-password"},
            follow_redirects=False)

    def test_the_move_out_blanks_the_questions_they_already_asked(self):
        tid, uid, token = self._squatted_member()
        conn = tenancy.tenant_connect(tid)
        try:
            assistant.record_turn(conn, {"user_id": uid,
                                         "email": "asker@example.dev"},
                                  "before", OUT)
        finally:
            conn.close()
        self.assertEqual(str(_row(tid, "before")["asked_by_id"]), uid)
        self.assertEqual(self._finish(token).status_code, 303)
        r = _row(tid, "before")
        self.assertIsNone(r["asked_by_id"])
        self.assertEqual(r["asked_by"], "")

    def test_an_ask_in_flight_during_the_move_out_ends_up_blank(self):
        tid, uid, token = self._squatted_member()
        conn = tenancy.tenant_connect(tid)
        try:
            paused, asker, errs = _in_flight(conn, uid, "in flight")
            result: dict = {}
            mover = threading.Thread(
                target=lambda: result.setdefault("r", self._finish(token)),
                daemon=True)
            mover.start()
            mover.join(1.0)
            paused.go.set()
            asker.join(15)
            mover.join(30)
        finally:
            conn.close()
        self.assertEqual(errs, [])
        self.assertEqual(result["r"].status_code, 303, result["r"].text)
        r = _row(tid, "in flight")
        self.assertIsNotNone(r, "the question itself must be kept")
        self.assertIsNone(r["asked_by_id"])
        self.assertEqual(r["asked_by"], "")


if __name__ == "__main__":
    unittest.main()
