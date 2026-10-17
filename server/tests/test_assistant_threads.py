"""Every question opens a conversation, and a follow-up continues it with
that thread — and only that thread — as the model's context.

Asked "coffee by month?" and then "and last year?", the second question
means nothing on its own; it is answered as the next turn of the first.
Pinned here: a follow-up's model request carries the thread's earlier
questions and prose answers before the new one, oldest first, bounded;
another thread's turns never appear in it; a new question is a thread of
its own (thread_id = its id) and a follow-up joins the thread it names;
removing a conversation drops every turn of it and nothing else; the
route refuses a thread the household does not have.

Removing ONE turn by its id still removes that turn alone: the apps
already installed send a turn's id to /assistant/history/{id} meaning
"this answer", and a whole conversation has its own door,
/assistant/threads/{thread_id}. A follow-up whose conversation was deleted
while the model was answering is answered but not kept — the deleted
conversation never comes back as its one late turn — and the trim that
bounds the log takes only the asker's own turns, never a conversation's
opening while any follow-up of it stays, and leaves no orphan when it
races a follow-up.
"""

import json
import os
import threading
import unittest
import uuid
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import assistant

from .util import TEST_DB, _ensure_db, make_db

_BE = {"url": "http://fake-llm.local", "model": "m", "api_key": "",
       "extra_body": "", "source": "env"}
PW = "a-long-enough-password-1"
_OUT = {"blocks": [], "tools_used": []}


def _recording_transport(seen, answer="Here you go."):
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {
            "content": answer, "tool_calls": []}}]})
    return httpx.MockTransport(handler)


class ConversationLogNeverLeavesTheInstanceTests(unittest.TestCase):
    """The question log is written by record_turn alone: neither the
    household's export nor the operator's archive carries it, so no
    restore writes a turn without its thread (one that did would take a
    fresh thread id per row and split each conversation into threads of
    one)."""

    def test_both_exports_skip_the_log(self):
        from oikonome import tenant_export
        from oikonome.web import pages
        self.assertIn("assistant_turns", pages.EXPORT_SKIP)
        self.assertIn("assistant_turns", tenant_export.ARCHIVE_TABLE_SKIP)


class ThreadContextTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.user = {"user_id": None, "email": "me@example.dev"}

    def tearDown(self):
        self.conn.close()

    def _keep(self, q, a, thread=None):
        return assistant.record_turn(
            self.conn, self.user, q, {**_OUT, "answer": a}, thread_id=thread)

    def test_a_new_question_is_a_thread_of_its_own(self):
        first = self._keep("coffee by month?", "About $40 a month.")
        rows = assistant.history(self.conn)["turns"]
        self.assertEqual(rows[0]["thread_id"], first)
        self.assertEqual(rows[0]["id"], first)

    def test_a_follow_up_joins_its_thread_and_the_model_sees_that_thread_alone(self):
        first = self._keep("coffee by month?", "About $40 a month.")
        other = self._keep("net worth?", "Up this year.")
        self.assertNotEqual(first, other)
        seen = []
        ctx = assistant.thread_context(self.conn, first)
        r = assistant.ask(self.conn, "and last year?",
                          transport=_recording_transport(seen),
                          backend=_BE, context=ctx)
        self.assertEqual(r["answer"], "Here you go.")
        msgs = seen[-1]["messages"]
        roles = [m["role"] for m in msgs]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])
        self.assertEqual(msgs[1]["content"], "coffee by month?")
        self.assertEqual(msgs[2]["content"], "About $40 a month.")
        self.assertEqual(msgs[3]["content"], "and last year?")
        self.assertNotIn("net worth", json.dumps(msgs))
        second = self._keep("and last year?", r["answer"], thread=first)
        rows = {t["id"]: t["thread_id"] for t in assistant.history(self.conn)["turns"]}
        self.assertEqual(rows[second], first)
        self.assertEqual(rows[other], other)

    def test_the_context_is_the_prose_oldest_first_and_bounded(self):
        ctx = [{"question": f"q{i}", "answer": "a" * 5000} for i in range(12)]
        msgs = assistant._context_messages(ctx)
        self.assertEqual(len(msgs), 2 * assistant.THREAD_CONTEXT_TURNS)
        self.assertEqual(msgs[0]["content"], f"q{12 - assistant.THREAD_CONTEXT_TURNS}")
        self.assertLessEqual(len(msgs[1]["content"]), assistant.THREAD_CONTEXT_CHARS + 2)
        self.assertTrue(msgs[1]["content"].endswith("…"))
        # a turn with no answer is skipped, never sent as a bare user line
        self.assertEqual(assistant._context_messages([{"question": "q", "answer": ""}]), [])

    def test_removing_a_conversation_drops_its_turns_and_nothing_else(self):
        first = self._keep("coffee?", "Some.")
        self._keep("and tea?", "Less.", thread=first)
        other = self._keep("net worth?", "Up.")
        self.assertEqual(assistant.forget_thread(self.conn, first), 2)
        left = [t["id"] for t in assistant.history(self.conn)["turns"]]
        self.assertEqual(left, [other])
        self.assertEqual(assistant.forget_thread(self.conn, first), 0)

    def _ids(self):
        return [t["id"] for t in assistant.history(self.conn)["turns"]]

    def test_removing_one_turn_by_its_id_leaves_the_rest_of_its_conversation(self):
        root = self._keep("coffee?", "Some.")
        f1 = self._keep("and tea?", "Less.", thread=root)
        f2 = self._keep("and juice?", "None.", thread=root)
        # a follow-up by its own id: that turn alone
        self.assertEqual(assistant.forget(self.conn, f1), 1)
        self.assertEqual(self._ids(), [f2, root])
        # the opening turn by its id — which is also the thread's id, the
        # case the installed apps hit — is kept while a follow-up remains:
        # dropped, it would leave that follow-up answering nothing
        self.assertEqual(assistant.forget_turn(self.conn, root),
                         {"deleted": 0, "reason": assistant.OPENING_KEPT})
        self.assertEqual(assistant.forget(self.conn, root), 0)
        self.assertEqual(self._ids(), [f2, root])
        # and goes as the conversation's last row
        self.assertEqual(assistant.forget(self.conn, f2), 1)
        self.assertEqual(assistant.forget_turn(self.conn, root), {"deleted": 1})
        self.assertEqual(self._ids(), [])

    def _race_a_follow_up(self, removal):
        """Pause a follow-up to a fresh conversation after it has taken its
        share lock, run `removal(conn)` on a second connection, let the
        follow-up go. Returns (removal's result, whether it waited)."""
        root = self._keep("coffee?", "Some.")
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        paused = _PausedBeforeInsert(self.conn)
        errs: list[BaseException] = []

        def follow_up():
            try:
                assistant.record_turn(paused, self.user, "and tea?",
                                      {**_OUT, "answer": "Less."},
                                      thread_id=root)
            except BaseException as e:                   # noqa: BLE001
                errs.append(e)

        asker = threading.Thread(target=follow_up, daemon=True)
        asker.start()
        self.assertTrue(paused.checked.wait(15))
        done, got = threading.Event(), []

        def remove():
            c = tenancy.tenant_connect(tid, os.environ.get(
                "OIKONOME_TEST_DSN",
                f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}"))
            try:
                c.execute("SET lock_timeout = '20s'")
                got.append(removal(c, root))
                done.set()
            finally:
                c.close()

        remover = threading.Thread(target=remove, daemon=True)
        remover.start()
        waited = not done.wait(1.0)
        paused.go.set()
        asker.join(15)
        remover.join(25)
        self.assertEqual(errs, [])
        return root, got, waited

    def test_clearing_the_log_while_a_follow_up_is_written_leaves_nothing(self):
        """The whole-log delete waits for the follow-up's lock; its first
        pass cannot see the row the follow-up wrote meanwhile, so a single
        pass left that follow-up behind, headless, in an emptied log."""
        _root, got, waited = self._race_a_follow_up(
            lambda c, _root: assistant.forget(c))
        self.assertTrue(waited, "the clear did not wait for the follow-up")
        self.assertEqual(got, [2])
        self.assertEqual(self._ids(), [])

    def test_removing_an_opening_while_a_follow_up_is_written_keeps_both(self):
        """The one-turn delete of an opening waits for the follow-up's lock
        and then sees the follow-up it wrote: the opening is kept, never
        dropped from under its new follow-up."""
        root, got, waited = self._race_a_follow_up(assistant.forget_turn)
        self.assertTrue(waited, "the removal did not wait for the follow-up")
        self.assertEqual(got, [{"deleted": 0, "reason": assistant.OPENING_KEPT}])
        ids = self._ids()
        self.assertEqual(len(ids), 2)
        self.assertIn(root, ids)

    def test_a_follow_up_to_a_deleted_conversation_is_not_kept(self):
        root = self._keep("coffee?", "Some.")
        assistant.forget_thread(self.conn, root)
        with self.assertRaises(assistant.ThreadRemoved):
            self._keep("and tea?", "Less.", thread=root)
        self.assertEqual(self._ids(), [])

    def test_a_conversation_deleted_while_a_follow_up_is_written_leaves_nothing(self):
        """The removal and the late follow-up race: whichever lands first,
        the conversation ends up gone — no turn of it outlives the delete."""
        root = self._keep("coffee?", "Some.")
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        paused = _PausedBeforeInsert(self.conn)
        errs: list[BaseException] = []

        def follow_up():
            try:
                assistant.record_turn(paused, self.user, "and tea?",
                                      {**_OUT, "answer": "Less."},
                                      thread_id=root)
            except BaseException as e:                   # noqa: BLE001
                errs.append(e)

        asker = threading.Thread(target=follow_up, daemon=True)
        asker.start()
        self.assertTrue(paused.checked.wait(15))
        done, gone = threading.Event(), []

        def remove():
            c = tenancy.tenant_connect(tid, os.environ.get(
                "OIKONOME_TEST_DSN",
                f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}"))
            try:
                c.execute("SET lock_timeout = '20s'")
                gone.append(assistant.forget_thread(c, root))
                done.set()
            finally:
                c.close()

        remover = threading.Thread(target=remove, daemon=True)
        remover.start()
        waited = not done.wait(1.0)
        paused.go.set()
        asker.join(15)
        remover.join(25)
        self.assertEqual(errs, [])
        self.assertTrue(waited, "the removal did not wait for the follow-up")
        self.assertEqual(gone, [2])
        self.assertEqual(self._ids(), [])

    def test_the_trim_never_takes_a_conversation_s_opening_and_keeps_its_follow_ups(self):
        with mock.patch.object(assistant, "HISTORY_KEEP", 3):
            root = self._keep("coffee?", "Some.")
            x1 = self._keep("rent?", "Paid.")
            x2 = self._keep("bills?", "Two.")
            f = self._keep("and tea?", "Less.", thread=root)
            x3 = self._keep("net worth?", "Up.")
        # the conversation last active least recently goes whole; the one
        # revived by a follow-up keeps its opening
        self.assertEqual(self._ids(), [x3, f, root])
        self.assertNotIn(x1, self._ids())
        self.assertNotIn(x2, self._ids())
        self.assertEqual([t["question"] for t in assistant.thread_context(self.conn, root)],
                         ["coffee?", "and tea?"])

    def test_another_asker_s_trim_never_takes_a_conversation_they_did_not_open(self):
        a = {"user_id": None, "email": "a@example.dev"}
        b = {"user_id": None, "email": "b@example.dev"}
        with mock.patch.object(assistant, "HISTORY_KEEP", 3):
            root = assistant.record_turn(self.conn, a, "coffee?",
                                         {**_OUT, "answer": "Some."})
            assistant.record_turn(self.conn, b, "and tea?",
                                  {**_OUT, "answer": "Less."}, thread_id=root)
            for i in range(4):
                assistant.record_turn(self.conn, b, f"b{i}?",
                                      {**_OUT, "answer": "ok"})
        qs = [t["question"] for t in assistant.history(self.conn)["turns"]]
        self.assertIn("coffee?", qs)
        self.assertEqual(qs, ["b3?", "b2?", "b1?", "coffee?"])

    def test_one_conversation_longer_than_the_share_keeps_its_opening_and_newest_turns(self):
        # the opening is the anchor; the oldest follow-ups go
        with mock.patch.object(assistant, "HISTORY_KEEP", 3):
            root = self._keep("q0", "a")
            for i in range(1, 5):
                last = self._keep(f"q{i}", "a", thread=root)
        rows = assistant.history(self.conn)["turns"]
        self.assertEqual([t["question"] for t in rows], ["q4", "q3", "q2", "q0"])
        self.assertEqual(rows[0]["id"], last)
        self.assertEqual(rows[-1]["id"], root)

    def test_an_asker_s_trim_never_takes_another_member_s_follow_up_or_its_opening(self):
        a = {"user_id": None, "email": "a@example.dev"}
        b = {"user_id": None, "email": "b@example.dev"}
        with mock.patch.object(assistant, "HISTORY_KEEP", 3):
            root = assistant.record_turn(self.conn, a, "coffee?",
                                         {**_OUT, "answer": "Some."})
            assistant.record_turn(self.conn, b, "and tea?",
                                  {**_OUT, "answer": "Less."}, thread_id=root)
            for i in range(4):
                assistant.record_turn(self.conn, a, f"a{i}?",
                                      {**_OUT, "answer": "ok"})
        qs = [t["question"] for t in assistant.history(self.conn)["turns"]]
        # a's oldest own turn goes; b's follow-up stays, and with it the
        # opening it continues
        self.assertEqual(qs, ["a3?", "a2?", "a1?", "and tea?", "coffee?"])
        self.assertEqual([t["question"] for t in assistant.thread_context(self.conn, root)],
                         ["coffee?", "and tea?"])

    def test_a_trim_racing_a_follow_up_leaves_no_orphan(self):
        """A follow-up holds its conversation's opening while it is
        written; a trim that would take that opening meanwhile must not
        end with the follow-up kept and the opening gone."""
        a = {"user_id": None, "email": "a@example.dev"}
        b = {"user_id": None, "email": "b@example.dev"}
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        with mock.patch.object(assistant, "HISTORY_KEEP", 1):
            root = assistant.record_turn(self.conn, a, "coffee?",
                                         {**_OUT, "answer": "Some."})
            paused = _PausedBeforeInsert(self.conn)
            errs: list[BaseException] = []

            def follow_up():
                try:
                    assistant.record_turn(paused, b, "and tea?",
                                          {**_OUT, "answer": "Less."},
                                          thread_id=root)
                except BaseException as e:               # noqa: BLE001
                    errs.append(e)

            asker = threading.Thread(target=follow_up, daemon=True)
            asker.start()
            self.assertTrue(paused.checked.wait(15))
            done = threading.Event()

            def ask_again():
                # a's next question pushes the opening past a's share
                c = tenancy.tenant_connect(tid, os.environ.get(
                    "OIKONOME_TEST_DSN",
                    f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}"))
                try:
                    c.execute("SET lock_timeout = '20s'")
                    assistant.record_turn(c, a, "rent?",
                                          {**_OUT, "answer": "Paid."})
                    done.set()
                except BaseException as e:               # noqa: BLE001
                    errs.append(e)
                finally:
                    c.close()

            trimmer = threading.Thread(target=ask_again, daemon=True)
            trimmer.start()
            done.wait(1.0)
            paused.go.set()
            asker.join(15)
            trimmer.join(25)
        self.assertEqual(errs, [])
        rows = assistant.history(self.conn)["turns"]
        ids = {t["id"] for t in rows}
        for t in rows:
            self.assertIn(t["thread_id"], ids,
                          f"{t['question']!r} outlived its conversation's opening")
        self.assertIn("rent?", [t["question"] for t in rows])

    def test_another_households_thread_reads_as_none(self):
        theirs = make_db()
        try:
            tid = assistant.record_turn(theirs, self.user, "theirs?",
                                        {**_OUT, "answer": "yes"})
        finally:
            theirs.close()
        self.assertEqual(assistant.thread_context(self.conn, tid), [])


class _PausedBeforeInsert:
    """A tenant connection whose record_turn stops after its checks (the
    thread's share lock taken) and before its insert, until let go."""

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


class ThreadRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.owner = TestClient(app)
        r = cls.owner.post("/api/signup", data={
            "email": f"thr-{uuid.uuid4().hex[:8]}@example.dev", "password": PW})
        assert r.status_code in (200, 303), r.text
        cls.other = TestClient(app)
        r = cls.other.post("/api/signup", data={
            "email": f"thr2-{uuid.uuid4().hex[:8]}@example.dev", "password": PW})
        assert r.status_code in (200, 303), r.text

    def _ask(self, client, q, text="Fine.", thread=None, seen=None):
        with mock.patch.object(assistant, "_llm") as llm:
            llm._backend.return_value = _BE
            llm.configured.return_value = True

            def chat(messages, *a, **k):
                if seen is not None:
                    seen.append(messages)
                return {"content": text, "tool_calls": []}
            llm.chat_tools.side_effect = chat
            body = {"question": q}
            if thread:
                body["thread_id"] = thread
            return client.post("/api/assistant", json=body)

    def test_a_follow_up_continues_the_conversation(self):
        r = self._ask(self.owner, "coffee by month?", "About $40.")
        self.assertEqual(r.status_code, 200, r.text)
        first = r.json()["thread_id"]
        self.assertEqual(first, r.json()["id"])
        seen = []
        r2 = self._ask(self.owner, "and last year?", "About $35.", thread=first, seen=seen)
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["thread_id"], first)
        self.assertNotEqual(r2.json()["id"], first)
        self.assertEqual([m["role"] for m in seen[-1]],
                         ["system", "user", "assistant", "user"])
        self.assertEqual(seen[-1][2]["content"], "About $40.")
        self.assertTrue(r2.json()["saved"])
        self.assertFalse(r2.json()["removed"])
        rows = self.owner.get("/api/assistant/history").json()["turns"]
        self.assertEqual({t["thread_id"] for t in rows}, {first})
        self.assertEqual(len(rows), 2)
        # removing the conversation takes both turns
        d = self.owner.delete(f"/api/assistant/threads/{first}")
        self.assertEqual(d.json()["deleted"], 2)
        self.assertEqual(self.owner.get("/api/assistant/history").json()["turns"], [])

    def test_the_history_door_removes_one_turn_by_its_id(self):
        root = self._ask(self.owner, "coffee?").json()["id"]
        f1 = self._ask(self.owner, "and tea?", thread=root).json()["id"]
        f2 = self._ask(self.owner, "and juice?", thread=root).json()["id"]
        try:
            d = self.owner.delete(f"/api/assistant/history/{f1}")
            self.assertEqual((d.status_code, d.json()["deleted"]), (200, 1))
            # the opening turn's id is the thread's id too; the installed
            # apps mean the one turn by it, and while a follow-up remains
            # the opening stays — a 200 they accept, with the reason
            d = self.owner.delete(f"/api/assistant/history/{root}")
            self.assertEqual((d.status_code, d.json()),
                             (200, {"ok": True, "deleted": 0,
                                    "reason": assistant.OPENING_KEPT}))
            ids = [t["id"] for t in
                   self.owner.get("/api/assistant/history").json()["turns"]]
            self.assertEqual(ids, [f2, root])
            d = self.owner.delete(f"/api/assistant/history/{f2}")
            self.assertEqual((d.status_code, d.json()["deleted"]), (200, 1))
            d = self.owner.delete(f"/api/assistant/history/{root}")
            self.assertEqual((d.status_code, d.json()),
                             (200, {"ok": True, "deleted": 1}))
            self.assertEqual(self.owner.get(
                "/api/assistant/history").json()["turns"], [])
            # a turn already gone is a success that deleted nothing: the
            # installed apps leave the row on screen on a 404
            d = self.owner.delete(f"/api/assistant/history/{root}")
            self.assertEqual((d.status_code, d.json()), (200, {"ok": True, "deleted": 0}))
            self.assertEqual(self.owner.delete(
                "/api/assistant/history/not-a-uuid").status_code, 400)
        finally:
            self.owner.delete("/api/assistant/history")

    def test_the_threads_door_removes_one_whole_conversation(self):
        root = self._ask(self.owner, "coffee?").json()["id"]
        self._ask(self.owner, "and tea?", thread=root)
        other = self._ask(self.owner, "net worth?").json()["id"]
        try:
            d = self.owner.delete(f"/api/assistant/threads/{root}")
            self.assertEqual((d.status_code, d.json()["deleted"]), (200, 2))
            ids = [t["id"] for t in
                   self.owner.get("/api/assistant/history").json()["turns"]]
            self.assertEqual(ids, [other])
            self.assertEqual(self.owner.delete(
                f"/api/assistant/threads/{root}").status_code, 404)
            self.assertEqual(self.owner.delete(
                "/api/assistant/threads/not-a-uuid").status_code, 400)
            # another household's conversation is none of this one's
            self.assertEqual(self.other.delete(
                f"/api/assistant/threads/{other}").status_code, 404)
        finally:
            self.owner.delete("/api/assistant/history")

    def test_a_viewer_may_not_remove_a_conversation_and_a_member_may(self):
        root = self._ask(self.owner, "coffee?").json()["id"]
        try:
            viewer = self._claim("viewer")
            self.assertEqual(viewer.delete(
                f"/api/assistant/threads/{root}").status_code, 403)
            member = self._claim("member")
            self.assertEqual(member.delete(
                f"/api/assistant/threads/{root}").status_code, 200)
        finally:
            self.owner.delete("/api/assistant/history")

    def _claim(self, role):
        from oikonome.web.app import app
        r = self.owner.post("/api/invites", json={
            "label": role, "role": role, "password": PW})
        assert r.status_code == 200, r.text
        token = r.json()["url"].rsplit("token=", 1)[1]
        client = TestClient(app)
        got = client.post("/api/invite/claim", json={
            "token": token,
            "email": f"{role}-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "family-member-pass"})
        assert got.status_code == 200, got.text
        return client

    def test_a_follow_up_whose_conversation_was_deleted_still_answers(self):
        root = self._ask(self.owner, "coffee?").json()["id"]

        def chat(messages, *a, **k):
            # the conversation is deleted while the model is answering
            admin = tenancy.admin_connect()
            try:
                admin.execute("DELETE FROM assistant_turns WHERE thread_id = %s",
                              (root,))
            finally:
                admin.close()
            return {"content": "About $35.", "tool_calls": []}

        with mock.patch.object(assistant, "_llm") as llm:
            llm._backend.return_value = _BE
            llm.configured.return_value = True
            llm.chat_tools.side_effect = chat
            r = self.owner.post("/api/assistant", json={
                "question": "and last year?", "thread_id": root})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["answer"], "About $35.")
        self.assertEqual((body["id"], body["thread_id"], body["saved"],
                          body["removed"]), (None, None, False, True))
        self.assertEqual(self.owner.get("/api/assistant/history").json()["turns"], [])

    def test_an_answer_that_could_not_be_kept_says_so(self):
        with mock.patch.object(assistant, "record_turn",
                               side_effect=RuntimeError("db down")):
            r = self._ask(self.owner, "coffee?", "Some.")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["answer"], "Some.")
        self.assertEqual((body["id"], body["thread_id"], body["saved"],
                          body["removed"]), (None, None, False, False))
        # a follow-up whose save failed still names its conversation,
        # which is there to follow up on
        root = self._ask(self.owner, "rent?").json()["id"]
        try:
            with mock.patch.object(assistant, "record_turn",
                                   side_effect=RuntimeError("db down")):
                r = self._ask(self.owner, "and last month?", thread=root)
            self.assertEqual((r.json()["thread_id"], r.json()["saved"]),
                             (root, False))
        finally:
            self.owner.delete("/api/assistant/history")

    def test_a_thread_the_household_does_not_have_is_refused(self):
        r = self._ask(self.other, "mine?", "Yes.")
        theirs = r.json()["thread_id"]
        r2 = self._ask(self.owner, "a follow-up on theirs?", thread=theirs)
        self.assertEqual(r2.status_code, 404, r2.text)
        r3 = self._ask(self.owner, "x?", thread="not-a-uuid")
        self.assertEqual(r3.status_code, 400, r3.text)


if __name__ == "__main__":
    unittest.main()
