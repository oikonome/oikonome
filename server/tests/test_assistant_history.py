"""The assistant keeps what it was asked and answered — per household,
newest first, the owner and every member reading the same log, a member
able to tidy it, a viewer shown only their own questions (and able to
continue only their own conversations), and never another household's
rows."""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.engine import assistant

from .test_assistant import _BE, _seq_transport
from .util import _ensure_db, make_db

PW = "correct-horse-battery"


def _reply(text):
    return _seq_transport([{"content": text, "tool_calls": []}])


class HistoryEngineTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _ask(self, q, text):
        out = assistant.ask(self.conn, q, transport=_reply(text), backend=_BE)
        return assistant.record_turn(self.conn, {"user_id": None,
                                                 "email": "A@Example.dev"},
                                     q, out)

    def test_answered_questions_are_kept_newest_first(self):
        a = self._ask("net worth?", "About $0.")
        b = self._ask("bills?", "None.\n```chart\n"
                      '{"type": "bar", "title": "t", "labels": ["x"], '
                      '"series": [{"name": "s", "values": [1]}]}\n```')
        h = assistant.history(self.conn)
        self.assertEqual([t["id"] for t in h["turns"]], [b, a])
        self.assertFalse(h["more"])
        top = h["turns"][0]
        self.assertEqual(top["question"], "bills?")
        self.assertEqual(top["answer"], "None.")
        self.assertEqual([x["kind"] for x in top["blocks"]], ["text", "bar"])
        self.assertEqual(top["asked_by"], "a@example.dev")
        self.assertTrue(top["asked_at"])

    def test_an_empty_answer_is_not_kept(self):
        out = {"answer": "", "blocks": [], "tools_used": []}
        self.assertIsNone(assistant.record_turn(
            self.conn, {"email": "a@example.dev"}, "?", out))
        self.assertEqual(assistant.history(self.conn)["turns"], [])

    def test_forget_one_and_all(self):
        a = self._ask("one", "1")
        b = self._ask("two", "2")
        self.assertEqual(assistant.forget(self.conn, a), 1)
        self.assertEqual([t["id"] for t in assistant.history(self.conn)["turns"]], [b])
        self.assertEqual(assistant.forget(self.conn, str(uuid.uuid4())), 0)
        self._ask("three", "3")
        self.assertEqual(assistant.forget(self.conn), 2)
        self.assertEqual(assistant.history(self.conn)["turns"], [])

    def test_the_log_is_trimmed_to_the_newest(self):
        with mock.patch.object(assistant, "HISTORY_KEEP", 3):
            for i in range(5):
                self._ask(f"q{i}", f"a{i}")
        qs = [t["question"] for t in assistant.history(self.conn)["turns"]]
        self.assertEqual(qs, ["q4", "q3", "q2"])

    def _member(self):
        """A real member of this household: the log names only people
        who still have a users row."""
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        return str(self.conn.execute(
            "INSERT INTO users (tenant_id, email, password_hash, role) "
            "VALUES (%s, %s, 'x', 'viewer') RETURNING id",
            (tid, f"m-{uuid.uuid4().hex[:8]}@example.dev")).fetchone()["id"])

    def _turn(self, uid, q):
        return assistant.record_turn(
            self.conn, {"user_id": uid, "email": f"{uid[:6]}@example.dev"},
            q, {"answer": "ok", "blocks": [], "tools_used": []})

    def test_one_asker_cannot_evict_another_s_questions(self):
        # anyone who may ask — a viewer included — fills only their own
        # share of the log; the trim never reaches another member's rows
        a, b = self._member(), self._member()
        with mock.patch.object(assistant, "HISTORY_KEEP", 3):
            self._turn(a, "a0")
            self._turn(a, "a1")
            for i in range(6):
                self._turn(b, f"b{i}")
        qs = [t["question"] for t in assistant.history(self.conn)["turns"]]
        self.assertEqual(qs, ["b5", "b4", "b3", "a1", "a0"])

    def test_a_malformed_cursor_is_refused_not_a_database_error(self):
        self._ask("q", "a")
        for bad in ("yesterday", "2026-13-45T00:00:00", "'; --"):
            with self.assertRaises(ValueError):
                assistant.history(self.conn, before=bad)
        with self.assertRaises(ValueError):
            assistant.history(self.conn, before="2026-01-01T00:00:00+00:00",
                              before_id="not-a-uuid")
        # the connection is still usable: nothing reached Postgres
        self.assertEqual(len(assistant.history(self.conn)["turns"]), 1)

    def test_rows_sharing_the_cursor_time_are_not_skipped(self):
        for i in range(3):
            self.conn.execute(
                "INSERT INTO assistant_turns (asked_at, question, answer) "
                "VALUES ('2026-05-01T12:00:00+00:00', %s, 'x')", (f"q{i}",))
        self.conn.commit()
        seen, page = [], assistant.history(self.conn, limit=1)
        seen += page["turns"]
        while page["more"]:
            page = assistant.history(self.conn, limit=1, **page["next"])
            seen += page["turns"]
        self.assertEqual(sorted(t["question"] for t in seen),
                         ["q0", "q1", "q2"])
        self.assertEqual(len({t["id"] for t in seen}), 3)

    def test_forgetting_an_asker_keeps_the_answers_but_not_the_name(self):
        a, b = self._member(), self._member()
        self._turn(a, "mine")
        self._turn(b, "theirs")
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.assertEqual(assistant.forget_asker(self.conn, tid, a), 1)
        self.conn.commit()
        rows = {t["question"]: t["asked_by"]
                for t in assistant.history(self.conn)["turns"]}
        self.assertEqual(rows["mine"], "")
        self.assertTrue(rows["theirs"])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM assistant_turns WHERE asked_by_id = %s",
            (a,)).fetchone()["n"], 0)

    def test_an_ask_finishing_after_its_asker_was_removed_is_not_named(self):
        # the removal blanks the asker's rows; an answer still in flight
        # then lands afterwards and must not bring their name back
        gone = str(uuid.uuid4())   # no users row: removed mid-answer
        stays = self._member()
        self._turn(gone, "late")
        self._turn(stays, "kept")
        rows = {t["question"]: t["asked_by"]
                for t in assistant.history(self.conn)["turns"]}
        self.assertEqual(rows["late"], "")
        self.assertTrue(rows["kept"])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM assistant_turns WHERE asked_by_id = %s",
            (gone,)).fetchone()["n"], 0)

    def test_paging(self):
        for i in range(4):
            self._ask(f"q{i}", f"a{i}")
        first = assistant.history(self.conn, limit=3)
        self.assertTrue(first["more"])
        self.assertEqual(len(first["turns"]), 3)
        rest = assistant.history(self.conn, limit=3,
                                 before=first["turns"][-1]["asked_at"])
        self.assertFalse(rest["more"])
        self.assertEqual([t["question"] for t in rest["turns"]], ["q0"])


class HistoryRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.app = app
        cls.owner = TestClient(app)
        r = cls.owner.post("/api/signup", data={
            "email": f"hist-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        assert r.status_code in (200, 303), r.text
        cls.other = TestClient(app)
        r = cls.other.post("/api/signup", data={
            "email": f"hist2-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        assert r.status_code in (200, 303), r.text

    def _claim(self, role):
        r = self.owner.post("/api/invites",
                            json={"label": role, "role": role,
                                  "password": PW})
        assert r.status_code == 200, r.text
        token = r.json()["url"].rsplit("token=", 1)[1]
        client = TestClient(self.app)
        got = client.post("/api/invite/claim", json={
            "token": token,
            "email": f"{role}-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "family-member-pass"})
        assert got.status_code == 200, got.text
        return client

    def _ask(self, client, q, text="Fine."):
        with mock.patch.object(assistant, "_llm") as llm:
            llm._backend.return_value = _BE
            llm.chat_tools.return_value = {"content": text, "tool_calls": []}
            llm.configured.return_value = True
            return client.post("/api/assistant", json={"question": q})

    def test_ask_records_and_the_household_reads_it_but_not_others(self):
        r = self._ask(self.owner, "how am I doing?", "Fine, thanks.")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["id"])
        h = self.owner.get("/api/assistant/history").json()
        self.assertEqual([t["question"] for t in h["turns"]][:1],
                         ["how am I doing?"])
        self.assertEqual(h["turns"][0]["answer"], "Fine, thanks.")
        # another household sees nothing of it
        self.assertEqual(self.other.get("/api/assistant/history").json()["turns"], [])
        # the viewer is shown its own questions only — the owner's is not
        # in its log, and a follow-up on the owner's thread reads as no
        # such conversation
        viewer = self._claim("viewer")
        self.assertEqual(viewer.get("/api/assistant/history").json()["turns"], [])
        owner_thread = h["turns"][0]["thread_id"]
        with mock.patch.object(assistant, "_llm") as llm:
            llm._backend.return_value = _BE
            llm.chat_tools.return_value = {"content": "x", "tool_calls": []}
            llm.configured.return_value = True
            r = viewer.post("/api/assistant", json={
                "question": "and last month?", "thread_id": owner_thread})
        self.assertEqual(r.status_code, 404, r.text)
        r = self._ask(viewer, "what do I spend on coffee?", "Little.")
        self.assertEqual(r.status_code, 200, r.text)
        v = viewer.get("/api/assistant/history").json()
        self.assertEqual([t["question"] for t in v["turns"]],
                         ["what do I spend on coffee?"])
        # ...and may continue its own conversation
        with mock.patch.object(assistant, "_llm") as llm:
            llm._backend.return_value = _BE
            llm.chat_tools.return_value = {"content": "Less.", "tool_calls": []}
            llm.configured.return_value = True
            r = viewer.post("/api/assistant", json={
                "question": "and tea?", "thread_id": v["turns"][0]["thread_id"]})
        self.assertEqual(r.status_code, 200, r.text)
        # the owner's log holds everyone's, the viewer's questions named
        o = self.owner.get("/api/assistant/history").json()
        self.assertIn("and tea?", [t["question"] for t in o["turns"]])
        self.assertIn("how am I doing?", [t["question"] for t in o["turns"]])
        self.assertEqual(viewer.delete(
            f"/api/assistant/history/{h['turns'][0]['id']}").status_code, 403)
        self.assertEqual(viewer.delete("/api/assistant/history").status_code, 403)
        # a member reads the whole log and may tidy it
        member = self._claim("member")
        m = member.get("/api/assistant/history").json()
        self.assertIn("and tea?", [t["question"] for t in m["turns"]])
        self.assertEqual(member.delete(
            f"/api/assistant/history/{h['turns'][0]['id']}").json()["deleted"], 1)
        self._ask(self.owner, "again?")
        self.assertEqual(self.owner.delete("/api/assistant/history/not-a-uuid")
                         .status_code, 400)
        self.assertGreaterEqual(
            member.delete("/api/assistant/history").json()["deleted"], 1)
        self.assertEqual(self.owner.get("/api/assistant/history").json()["turns"], [])

    def _follow_up(self, client, thread_id, q, text="Fine."):
        """Ask a follow-up; returns (response, every message list the
        model was handed)."""
        with mock.patch.object(assistant, "_llm") as llm:
            llm._backend.return_value = _BE
            llm.chat_tools.return_value = {"content": text, "tool_calls": []}
            llm.configured.return_value = True
            r = client.post("/api/assistant", json={
                "question": q, "thread_id": thread_id})
            sent = repr([c.args + tuple(c.kwargs.values())
                         for c in llm.chat_tools.call_args_list])
        return r, sent

    def test_a_viewer_s_follow_up_never_carries_another_person_s_turn(self):
        """The model's context for a view-only login's follow-up holds
        only what that login asked. The owner reads the viewer's
        conversation and may continue it; if the viewer's next follow-up
        then sent the whole thread, the owner's question and answer — not
        in the viewer's log — would reach the model, and the viewer could
        ask it to repeat them."""
        self.addCleanup(self.owner.delete, "/api/assistant/history")
        viewer = self._claim("viewer")
        r = self._ask(viewer, "viewer-opening-q", "viewer-opening-a")
        self.assertEqual(r.status_code, 200, r.text)
        thread = viewer.get("/api/assistant/history").json()["turns"][0][
            "thread_id"]
        r, _ = self._follow_up(self.owner, thread, "owner-private-q",
                               "owner-private-a")
        self.assertEqual(r.status_code, 200, r.text)
        # the viewer's log still shows only what it asked
        self.assertEqual(
            [t["question"] for t in
             viewer.get("/api/assistant/history").json()["turns"]],
            ["viewer-opening-q"])
        r, sent = self._follow_up(viewer, thread, "what was asked last?")
        self.assertNotIn("owner-private-q", sent)
        self.assertNotIn("owner-private-a", sent)
        # refused exactly as a thread that does not exist
        self.assertEqual(r.status_code, 404, r.text)

    def test_a_turn_landing_mid_follow_up_never_reaches_a_viewer(self):
        """The ownership check and the context a viewer's follow-up hands
        the model are one read: an owner turn committed between them must
        not reach the model. The turn is written right after the first
        statement of the follow-up that reads the thread."""
        from oikonome.web import api as api_mod
        self.addCleanup(self.owner.delete, "/api/assistant/history")
        viewer = self._claim("viewer")
        r = self._ask(viewer, "viewer-race-q", "viewer-race-a")
        self.assertEqual(r.status_code, 200, r.text)
        thread = viewer.get("/api/assistant/history").json()["turns"][0][
            "thread_id"]
        real_conn = api_mod._conn

        class _Racing:
            def __init__(self, conn):
                self._c, self._fired = conn, False

            def __getattr__(self, name):
                return getattr(self._c, name)

            def execute(self, sql, *a, **k):
                cur = self._c.execute(sql, *a, **k)
                if not self._fired and "assistant_turns" in str(sql):
                    self._fired = True
                    self._c.execute(
                        "INSERT INTO assistant_turns (id, thread_id, "
                        "asked_by_id, asked_by, question, answer, blocks, "
                        "tools_used) VALUES (%s, %s, NULL, 'owner', "
                        "'owner-race-q', 'owner-race-a', '[]'::jsonb, '{}')",
                        (str(uuid.uuid4()), thread))
                return cur

        with mock.patch.object(api_mod, "_conn",
                               lambda user: _Racing(real_conn(user))):
            r, sent = self._follow_up(viewer, thread, "and then?")
        self.assertNotIn("owner-race-q", sent)
        self.assertNotIn("owner-race-a", sent)

    def test_a_viewer_cannot_tell_a_household_thread_from_a_missing_one(self):
        """A view-only login's follow-up on a conversation someone else
        opened gets the same status and body as one on an id that names
        nothing — otherwise the refusal would confirm which ids exist."""
        self.addCleanup(self.owner.delete, "/api/assistant/history")
        r = self._ask(self.owner, "owner-only-q")
        self.assertEqual(r.status_code, 200, r.text)
        thread = self.owner.get("/api/assistant/history").json()["turns"][0][
            "thread_id"]
        viewer = self._claim("viewer")
        theirs, sent = self._follow_up(viewer, thread, "and then?")
        self.assertNotIn("owner-only-q", sent)
        missing, _ = self._follow_up(viewer, str(uuid.uuid4()), "and then?")
        self.assertEqual(theirs.status_code, 404, theirs.text)
        self.assertEqual((theirs.status_code, theirs.json()),
                         (missing.status_code, missing.json()))

    def _rows_by_question(self):
        return {t["question"]: t for t in
                self.owner.get("/api/assistant/history").json()["turns"]}

    def test_a_bad_cursor_is_a_400_not_a_500(self):
        for bad in ("garbage", "2026-02-30T00:00:00"):
            r = self.owner.get("/api/assistant/history",
                               params={"before": bad})
            self.assertEqual(r.status_code, 400, bad)
        r = self.owner.get("/api/assistant/history", params={
            "before": "2026-01-01T00:00:00+00:00", "before_id": "x"})
        self.assertEqual(r.status_code, 400)

    def test_a_removed_member_s_questions_stay_without_their_name(self):
        member = self._claim("member")
        member_id = next(u["id"] for u in member.get("/api/users")
                         .json()["users"] if u.get("me"))
        self.assertEqual(self._ask(member, "removed-q").status_code, 200)
        self.assertEqual(self._ask(self.owner, "owner-q").status_code, 200)
        r = self.owner.request("DELETE", f"/api/users/{member_id}",
                               json={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        rows = self._rows_by_question()
        self.assertEqual(rows["removed-q"]["asked_by"], "")
        self.assertEqual(rows["removed-q"]["answer"], "Fine.")
        self.assertTrue(rows["owner-q"]["asked_by"])
        self.owner.delete("/api/assistant/history")

    def test_a_member_who_leaves_takes_their_name_off_the_log(self):
        member = self._claim("member")
        self.assertEqual(self._ask(member, "leaver-q").status_code, 200)
        r = member.post("/api/account/leave",
                        data={"password": "family-member-pass"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._rows_by_question()["leaver-q"]["asked_by"], "")
        self.owner.delete("/api/assistant/history")

    def test_a_member_removed_by_an_owner_s_rotation_loses_their_name(self):
        # the recovery and rotation doors remove members through their own
        # path; it must leave the log the way the roster's removal does
        from oikonome.auth import reset
        from oikonome.db import tenancy
        member = self._claim("member")
        me = next(u for u in member.get("/api/users").json()["users"]
                  if u.get("me"))
        self.assertEqual(self._ask(member, "rotated-q").status_code, 200)
        conn = tenancy.control_connect()
        try:
            row = conn.execute("SELECT id, tenant_id, email FROM users "
                               "WHERE id = %s", (me["id"],)).fetchone()
            with conn.transaction():
                gone = reset._remove_members(conn, [dict(row)])
        finally:
            conn.close()
        self.assertEqual(len(gone), 1)
        self.assertEqual(self._rows_by_question()["rotated-q"]["asked_by"],
                         "")
        self.owner.delete("/api/assistant/history")

    def test_another_household_cannot_delete_by_id(self):
        r = self._ask(self.owner, "mine")
        tid = r.json()["id"]
        # the row policy hides it, so nothing is deleted: the door answers
        # as for a turn already gone (deleted 0), and the row stays
        d = self.other.delete(f"/api/assistant/history/{tid}")
        self.assertEqual((d.status_code, d.json()["deleted"]), (200, 0))
        self.assertEqual(len(self.owner.get("/api/assistant/history").json()["turns"]), 1)
        self.owner.delete("/api/assistant/history")


class SharedLoginHistoryTests(unittest.TestCase):
    """The shared demonstration login reads the log with every other
    member's address withheld, and cannot delete what other visitors
    asked — one visitor clearing the log would empty it for the rest."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def test_addresses_are_withheld_and_the_deletes_refused(self):
        from oikonome.db import tenancy
        c = TestClient(self.app)
        email = f"shared-{uuid.uuid4().hex[:8]}@example.dev"
        self.assertEqual(c.post("/api/signup", data={
            "email": email, "password": PW}).status_code, 200)
        HistoryRouteTests._ask(self, c, "mine")
        admin = tenancy.admin_connect()
        try:
            tid = admin.execute("SELECT tenant_id FROM users WHERE email=%s",
                                (email,)).fetchone()["tenant_id"]
            admin.execute(
                "INSERT INTO assistant_turns (tenant_id, asked_by_id, "
                "asked_by, question, answer) VALUES (%s, %s, "
                "'someone-else@example.dev', 'theirs', 'x')",
                (tid, str(uuid.uuid4())))
            admin.execute("UPDATE users SET second_factor_waived=TRUE "
                          "WHERE email=%s", (email,))
        finally:
            admin.close()
        turns = c.get("/api/assistant/history").json()["turns"]
        who = {t["question"]: t["asked_by"] for t in turns}
        self.assertEqual(who["mine"], email)
        self.assertNotIn("someone-else", who["theirs"])
        self.assertTrue(who["theirs"])
        self.assertEqual(c.delete(
            f"/api/assistant/history/{turns[0]['id']}").status_code, 403)
        self.assertEqual(c.delete("/api/assistant/history").status_code, 403)
        self.assertEqual(c.delete(
            f"/api/assistant/threads/{turns[0]['thread_id']}").status_code,
            403)
        self.assertEqual(len(c.get("/api/assistant/history").json()["turns"]),
                         2)


if __name__ == "__main__":
    unittest.main()
