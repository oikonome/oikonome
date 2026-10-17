"""The ledger download is written out a slice at a time and one at a time
per household. A whole history must never be held in memory at once — the
filters are matched once, then rows are read, decorated and written in
slices on one connection — and a second download for the same household
while the first is still running is refused instead of building the same
file twice beside it."""
import csv
import datetime as dt
import io
import os
import tracemalloc
import unittest
import uuid
import zipfile
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import TODAY, _ensure_db, add_txn, seed_accounts, write_config

LOCAL_NOW = dt.datetime.combine(TODAY, dt.time(9, 0))
N_ROWS = 7


class TransactionsExportStreamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"txstream-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn, food_monthly=1000, other_monthly=1000)
            for i in range(N_ROWS):
                add_txn(conn, dt.date(TODAY.year, TODAY.month, 1) - dt.timedelta(days=i),
                        10.0 + i, f"CORNER GROCER {i}", primary="FOOD_AND_DRINK",
                        txn_id=f"stream-{uuid.uuid4().hex[:6]}-{i}")
        finally:
            conn.close()

    def get(self, path: str, **headers):
        with mock.patch("oikonome.localtime.now_local", return_value=LOCAL_NOW):
            return self.client.get(path, headers=headers)

    def _csv(self, r):
        rows = list(csv.reader(io.StringIO(r.text)))
        return rows[0], rows[1:]

    def test_transactions_export_streams_in_chunks(self):
        from oikonome.web import api, data
        seen = []
        real = data.search_transactions

        def spy(*a, **kw):
            seen.append(kw.get("chunk"))
            return real(*a, **kw)
        page = self.get("/api/transactions?q=corner%20grocer").json()
        with mock.patch.object(api, "_EXPORT_CHUNK", 2), \
                mock.patch.object(data, "search_transactions", spy):
            r = self.get("/api/transactions/export?q=corner%20grocer&format=csv")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(seen, [2])
        header, rows = self._csv(r)
        # every row, in the page's own order, across slice boundaries
        self.assertEqual([row[header.index("id")] for row in rows],
                         [x["id"] for x in page["rows"]])
        self.assertEqual(len(rows), N_ROWS)

    def test_a_chunked_xlsx_is_one_valid_sheet(self):
        from oikonome.web import api
        with mock.patch.object(api, "_EXPORT_CHUNK", 3):
            r = self.get("/api/transactions/export?q=corner%20grocer&format=xlsx")
        self.assertEqual(r.status_code, 200, r.text)
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            self.assertIsNone(z.testzip())
            sheet = z.read("xl/worksheets/sheet1.xml").decode()
        self.assertTrue(sheet.endswith("</sheetData></worksheet>"))
        self.assertEqual(sheet.count("<row "), 1 + N_ROWS)
        self.assertIn(f'<row r="{1 + N_ROWS}">', sheet)

    def test_rows_past_the_cap_are_named_in_a_last_line(self):
        from oikonome.web import api
        with mock.patch.object(api, "TRANSACTIONS_EXPORT_CAP", 3), \
                mock.patch.object(api, "_EXPORT_CHUNK", 2):
            r = self.get("/api/transactions/export?q=corner%20grocer")
        self.assertEqual(r.status_code, 200, r.text)
        _header, rows = self._csv(r)
        self.assertEqual(len(rows), 4)
        self.assertIn(f"{N_ROWS - 3} more rows were not included", rows[-1][0])

    def test_transactions_export_one_at_a_time_per_tenant(self):
        from oikonome.web import api
        release = api._export_slot(self.tid)
        try:
            r = self.get("/api/transactions/export?q=corner%20grocer")
            self.assertEqual(r.status_code, 429)
            self.assertIn("already running", r.json()["detail"])
        finally:
            release()
        # a finished download gives its slot back: two in a row both work
        for _ in range(2):
            self.assertEqual(
                self.get("/api/transactions/export?q=corner%20grocer").status_code, 200)
        self.assertFalse(any(t == str(self.tid) for t, _b in api._EXPORT_SLOTS.values()))

    def test_the_process_takes_only_a_few_downloads_at_once(self):
        from oikonome.web import api
        others = [api._export_slot(f"other-{i}") for i in range(api._EXPORT_MAX)]
        try:
            r = self.get("/api/transactions/export?q=corner%20grocer")
            self.assertEqual(r.status_code, 429)
        finally:
            for rel in others:
                rel()
        self.assertEqual(
            self.get("/api/transactions/export?q=corner%20grocer").status_code, 200)

    def test_a_refused_filter_gives_the_slot_back(self):
        from oikonome.web import api
        r = self.get("/api/transactions/export?date_from=not-a-date")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(any(t == str(self.tid) for t, _b in api._EXPORT_SLOTS.values()))

    def test_an_abandoned_slot_expires(self):
        from oikonome.web import api
        api._export_slot(self.tid)          # never released, like a lost client
        with mock.patch.object(api, "_EXPORT_SLOT_TTL", -1):
            self.assertEqual(
                self.get("/api/transactions/export?q=corner%20grocer").status_code, 200)

    def _leave_after_first_chunk(self, spec_version: str):
        """Drive the download door the way a server would and walk away
        after the first chunk: on ASGI 2.3 the server reports a
        disconnect, on 2.4 its send raises. Returns the connections the
        door opened and, taken the moment the app returned, whether each
        was back and whether the household's slot was still taken.

        The response object, and the body generator inside it, are held
        until after that look: a real server keeps them alive past the
        app's return, so a door that leaves the release to whoever drops
        the last reference (here, refcounting) must show up as a leak."""
        import asyncio
        from oikonome.web import api
        from oikonome.web.app import app
        opened = []
        real_conn = api._conn

        def spy(user):
            c = real_conn(user)
            opened.append(c)
            return c
        cookie = "; ".join(f"{k}={v}" for k, v in self.client.cookies.items())
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": spec_version},
            "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": "/api/transactions/export",
            "raw_path": b"/api/transactions/export",
            "query_string": b"q=corner%20grocer&format=csv", "root_path": "",
            "headers": [(b"host", b"testserver"), (b"cookie", cookie.encode())],
            "client": ("127.0.0.1", 50000), "server": ("testserver", 80)}
        bodies = []
        held = []
        at_return = {}
        from starlette.responses import StreamingResponse
        real_init = StreamingResponse.__init__

        def holding_init(resp, *a, **kw):
            real_init(resp, *a, **kw)
            held.append(resp)

        async def run():
            left = asyncio.Event()
            asked = []

            async def receive():
                if not asked:
                    asked.append(True)
                    return {"type": "http.request", "body": b"", "more_body": False}
                await left.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] != "http.response.body":
                    return
                if bodies and spec_version >= "2.4":
                    raise OSError("the client has gone")
                bodies.append(message.get("body", b""))
                left.set()
                # let the disconnect land before the next slice is asked for
                await asyncio.sleep(0.05)
            try:
                await app(scope, receive, send)
            except Exception:                               # noqa: BLE001
                pass        # a vanished client may surface as an error
            at_return["returned"] = [c._returned for c in opened]
            at_return["slot_taken"] = any(
                t == str(self.tid) for t, _b in api._EXPORT_SLOTS.values())
        with mock.patch.object(api, "_EXPORT_CHUNK", 1), \
                mock.patch.object(api, "_conn", spy), \
                mock.patch.object(StreamingResponse, "__init__", holding_init), \
                mock.patch("oikonome.localtime.now_local", return_value=LOCAL_NOW):
            asyncio.run(run())
        self.assertTrue(bodies, "the door never started the file")
        self.assertTrue(held, "the door never built its response")
        held.clear()
        return opened, at_return

    def test_export_releases_its_connection_when_the_client_leaves(self):
        import gc
        for spec in ("2.3", "2.4"):
            with self.subTest(asgi=spec):
                gc.disable()        # released by the door itself, not the collector
                try:
                    opened, at_return = self._leave_after_first_chunk(spec)
                    self.assertEqual(len(opened), 1)
                    self.assertEqual(at_return["returned"], [True],
                                     "the connection is still checked out")
                    self.assertFalse(at_return["slot_taken"],
                                     "the household's download slot is still taken")
                finally:
                    gc.enable()
        # and the household can download again straight away
        self.assertEqual(
            self.get("/api/transactions/export?q=corner%20grocer").status_code, 200)


class StreamedWorkbookMemoryTests(unittest.TestCase):
    def test_a_streamed_sheet_holds_one_batch_not_the_whole_table(self):
        from oikonome.web import xlsx
        header = [f"col{i}" for i in range(19)]

        def batches(n_batches=40, size=500):
            for b in range(n_batches):
                yield [[f"2020-01-{(i % 28) + 1:02d}", f"PAYEE {b}-{i} " * 3,
                        -12.34 - i, "FOOD_AND_DRINK", "Everyday card", "1234",
                        "", "a note", "", "", "", "online", None, "City",
                        "ST", "items " * 5, None, "BANK TEXT " * 3,
                        uuid.uuid4().hex] for i in range(size)]
        tracemalloc.start()
        try:
            size = 0
            for part in xlsx.stream_workbook("Transactions", header, batches()):
                size += len(part)
            _cur, streamed_peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        # 20,000 rows; the peak stays near one 500-row batch plus the
        # compressor, an order of magnitude under the table itself
        self.assertGreater(size, 0)
        self.assertLess(streamed_peak, 8 * 1024 * 1024)

        buf = io.BytesIO()
        for part in xlsx.stream_workbook("Transactions", header,
                                         batches(n_batches=2, size=3)):
            buf.write(part)
        with zipfile.ZipFile(buf) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(
                z.read("xl/worksheets/sheet1.xml").decode().count("<row "), 7)


if __name__ == "__main__":
    unittest.main()
