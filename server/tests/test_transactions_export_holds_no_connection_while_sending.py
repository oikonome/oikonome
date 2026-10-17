"""The ledger download gives its database connection and its download slot
back before the first byte goes to the client.

The connection is a pooled tenant connection every household shares, and a
client decides how fast it reads: a phone on a bad link, or one that reads
slowly on purpose, would otherwise keep that connection checked out for as
long as the transfer lasts, and a few such downloads empty the pool for
everyone. So the file is built first, on the server, and only then sent.

The built file is the one thing a send still holds, so files built and not
yet sent are bounded on their own — readers that stall after the headers
cannot pile them up in the process — and a client that leaves mid-send
closes its file, which removes it from disk.
"""
import asyncio
import csv
import datetime as dt
import io
import os
import tempfile
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import TODAY, _ensure_db, add_txn, seed_accounts, write_config

LOCAL_NOW = dt.datetime.combine(TODAY, dt.time(9, 0))
N_ROWS = 9


class ExportHoldsNothingWhileSendingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"txsend-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            for i in range(N_ROWS):
                add_txn(conn, TODAY.replace(day=1) - dt.timedelta(days=i),
                        10.0 + i, f"CORNER GROCER {i}",
                        primary="FOOD_AND_DRINK")
        finally:
            conn.close()

    def _drive(self, fmt: str, on_body=None):
        """Run the door the way a server would and look, at every body
        message the client is sent, whether the connection the door
        opened is back in the pool and the household's slot is free.
        `on_body(n)` runs at the n-th body message, mid-send."""
        from oikonome.web import api
        from oikonome.web.app import app
        opened, seen, bodies = [], [], []
        real_conn = api._conn

        def spy(user):
            c = real_conn(user)
            opened.append(c)
            return c
        cookie = "; ".join(f"{k}={v}" for k, v in self.client.cookies.items())
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": "/api/transactions/export",
            "raw_path": b"/api/transactions/export",
            "query_string": f"q=corner%20grocer&format={fmt}".encode(),
            "root_path": "",
            "headers": [(b"host", b"testserver"), (b"cookie", cookie.encode())],
            "client": ("127.0.0.1", 50000), "server": ("testserver", 80)}

        async def run():
            done = asyncio.Event()

            async def receive():
                await done.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] != "http.response.body":
                    return
                bodies.append(message.get("body", b""))
                seen.append((
                    [c._returned for c in opened],
                    any(t == str(self.tid)
                        for t, _b in api._EXPORT_SLOTS.values())))
                if on_body is not None:
                    on_body(len(bodies))
                if not message.get("more_body"):
                    done.set()
            await app(scope, receive, send)
        with mock.patch.object(api, "_EXPORT_CHUNK", 2), \
                mock.patch.object(api, "_conn", spy), \
                mock.patch("oikonome.localtime.now_local", return_value=LOCAL_NOW):
            asyncio.run(run())
        return opened, seen, b"".join(bodies)

    def test_the_connection_and_slot_are_back_before_the_first_byte(self):
        for fmt in ("csv", "xlsx"):
            with self.subTest(format=fmt):
                opened, seen, body = self._drive(fmt)
                self.assertEqual(len(opened), 1)
                self.assertTrue(seen, "nothing was sent")
                for returned, slot_taken in seen:
                    self.assertEqual(returned, [True],
                                     "the connection is held while sending")
                    self.assertFalse(slot_taken,
                                     "the slot is held while sending")
                if fmt == "csv":
                    rows = list(csv.reader(io.StringIO(body.decode())))
                    self.assertEqual(len(rows), 1 + N_ROWS)

    def _get(self):
        with mock.patch("oikonome.localtime.now_local", return_value=LOCAL_NOW):
            return self.client.get(
                "/api/transactions/export?q=corner%20grocer&format=csv")

    def test_files_still_being_sent_are_bounded(self):
        from oikonome.web import api
        for knob, status in (("_EXPORT_HELD_MAX", 503),
                             ("_EXPORT_HELD_PER_HOUSEHOLD", 429)):
            with self.subTest(bound=knob):
                answers = []

                def mid_send(n):
                    if n == 1:
                        answers.append(self._get())
                with mock.patch.object(api, knob, 1), \
                        mock.patch.object(api, "_EXPORT_SEND_PIECE", 64):
                    _opened, _seen, body = self._drive("csv", mid_send)
                    self.assertIn(b"CORNER GROCER", body)
                    self.assertEqual(len(answers), 1)
                    self.assertEqual(answers[0].status_code, status,
                                     answers[0].text)
                    self.assertTrue(answers[0].headers.get("retry-after"))
                    # the send that held the place has given it back
                    self.assertEqual(api._EXPORT_HELD, {})
                    self.assertEqual(self._get().status_code, 200)
                self.assertEqual(api._EXPORT_HELD, {})

    def test_a_client_that_leaves_mid_send_closes_and_removes_the_file(self):
        from oikonome.web import api
        made, links = [], []
        real = tempfile.SpooledTemporaryFile

        def spool(*a, **kw):
            f = real(*a, **kw)
            made.append(f)
            return f

        def leave(n):
            f = made[0]
            if n == 1:
                # the file spilled to disk; note what its descriptor names
                links.append(os.readlink(f"/proc/self/fd/{f.fileno()}"))
                raise OSError("client went away")
        with mock.patch.object(api, "_EXPORT_SPOOL_MEMORY", 64), \
                mock.patch.object(api, "_EXPORT_SEND_PIECE", 64), \
                mock.patch("tempfile.SpooledTemporaryFile", spool):
            try:
                self._drive("csv", leave)
            except Exception:
                pass
        self.assertEqual(len(made), 1)
        self.assertTrue(made[0]._rolled, "the file never reached disk")
        self.assertTrue(made[0].closed, "the file is still open")
        still = []
        for fd in os.listdir("/proc/self/fd"):
            try:
                still.append(os.readlink(f"/proc/self/fd/{fd}"))
            except OSError:
                pass  # closed while listing
        self.assertNotIn(links[0], still, "the spilled file is still on disk")
        self.assertEqual(api._EXPORT_HELD, {})
        self.assertEqual(self._get().status_code, 200)


if __name__ == "__main__":
    unittest.main()
