"""An upload slot is a shared resource; a client must not decide how long it
holds one.

The ingest cap exists so that only a bounded number of multipart bodies are
resident at once. It is a process-wide counter, which makes it something every
tenant depends on — and therefore something an attacker wants. Two properties
keep it from becoming the denial-of-service it was meant to prevent:

  * a slot is reclaimed when the body stops arriving, so a connection that
    opens a request and then dribbles cannot park one forever;
  * a slot is only ever taken for a route that actually accepts a file, so a
    slow POST to a Form(...) door like /login spends none of the capacity.

Both are driven here through the raw ASGI interface, because that is the only
place a body can be fed one chunk at a time. A test client hands the whole
body over at once and would prove neither.
"""

import asyncio
import unittest

from oikonome.web import security

MULTIPART = "multipart/form-data; boundary=x"


def _scope(path, app=None, ip="192.0.2.44", ctype=MULTIPART):
    return {"type": "http", "method": "POST", "path": path,
            "headers": [(b"content-type", ctype.encode())],
            "client": (ip, 40000), "app": app}


class _Reader:
    """The inner app: drains the body, then answers 200. It records the
    in-flight count seen while the body was arriving — which is the number
    the tests are actually about."""

    def __init__(self):
        self.peak_inflight = 0
        self.chunks = 0

    async def __call__(self, scope, receive, send):
        while True:
            msg = await receive()
            if msg["type"] != "http.request":
                break
            self.chunks += 1
            self.peak_inflight = max(self.peak_inflight,
                                     security._ingest_inflight)
            if not msg.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-length", b"2")]})
        await send({"type": "http.response.body", "body": b"ok"})


def _two_chunks(body=b"--x--\r\n"):
    """A body in two parts, so the slot is observably held while the second
    is still outstanding — a one-chunk body is already complete by the time
    the app sees it, and the slot is correctly gone."""
    state = {"n": 0}

    async def receive():
        state["n"] += 1
        return {"type": "http.request", "body": body,
                "more_body": state["n"] == 1}
    return receive


def _stops_after_first(body=b"--x\r\n"):
    """One chunk, then silence — the shape of the attack: the request is
    open, the parser is waiting, and nothing more is ever sent."""
    state = {"n": 0}

    async def receive():
        state["n"] += 1
        if state["n"] == 1:
            return {"type": "http.request", "body": body, "more_body": True}
        await asyncio.Event().wait()
    return receive


def _dribbles(gap=0.01, body=b"a"):
    """Never stalls, never finishes — one byte at a time, forever."""
    async def receive():
        await asyncio.sleep(gap)
        return {"type": "http.request", "body": body, "more_body": True}
    return receive


def _drive(scope, receive, inner, timeout=5.0):
    """Run the middleware to completion and return the messages it sent.

    The outer timeout is the point: an unbounded slot means this never
    returns, and a test that hangs is a test that has failed.
    """
    sent = []

    async def send(msg):
        sent.append(msg)

    async def go():
        mw = security.BodySizeLimitMiddleware(inner)
        await asyncio.wait_for(mw(scope, receive, send), timeout)

    asyncio.run(go())
    return sent


def _status(sent):
    for msg in sent:
        if msg["type"] == "http.response.start":
            return msg["status"]
    return None


class AnUploadSlotIsReclaimedFromAStalledClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault("OIKONOME_DEV", "1")
        import oikonome.web.app as appmod
        cls.app = appmod.app

    def setUp(self):
        security._reset_ingest_slots()
        security._limiter._hits.clear()
        self._clocks = (security.UPLOAD_STALL_TIMEOUT,
                        security.UPLOAD_INGEST_DEADLINE)

    def tearDown(self):
        (security.UPLOAD_STALL_TIMEOUT,
         security.UPLOAD_INGEST_DEADLINE) = self._clocks
        security._reset_ingest_slots()
        security._limiter._hits.clear()

    def test_an_upload_route_takes_a_slot_while_its_body_arrives(self):
        """The control: every assertion below is meaningless on a server
        that never takes a slot at all."""
        reader = _Reader()
        sent = _drive(_scope("/api/import", self.app), _two_chunks(), reader)
        self.assertEqual(_status(sent), 200)
        self.assertEqual(reader.peak_inflight, 1)
        self.assertEqual(security._ingest_inflight, 0)

    def test_a_client_that_stops_sending_loses_its_slot_and_gets_408(self):
        security.UPLOAD_STALL_TIMEOUT = 0.05
        reader = _Reader()
        sent = _drive(_scope("/api/import", self.app),
                      _stops_after_first(), reader)
        self.assertEqual(reader.peak_inflight, 1)   # it was held…
        self.assertEqual(_status(sent), 408)                 # …then reclaimed
        self.assertEqual(security._ingest_inflight, 0)

    def test_dribbling_forever_does_not_beat_the_stall_timer(self):
        """A byte every so often keeps a stall timer happy indefinitely, so
        the slot also carries an absolute ceiling."""
        security.UPLOAD_STALL_TIMEOUT = 30.0
        security.UPLOAD_INGEST_DEADLINE = 0.1
        reader = _Reader()
        sent = _drive(_scope("/api/import", self.app), _dribbles(), reader)
        self.assertEqual(_status(sent), 408)
        self.assertEqual(security._ingest_inflight, 0)

    def test_a_reclaimed_slot_is_usable_by_the_next_client(self):
        """A slot that is 'released' but never handed back would still lock
        the instance out, one stalled connection at a time."""
        security.UPLOAD_STALL_TIMEOUT = 0.05
        for _ in range(security.UPLOAD_INGEST_CONCURRENCY + 2):
            _drive(_scope("/api/import", self.app),
                   _stops_after_first(), _Reader())
        reader = _Reader()
        sent = _drive(_scope("/api/import", self.app), _two_chunks(), reader)
        self.assertEqual(_status(sent), 200)


class OnlyRoutesThatAcceptAFileSpendUploadCapacity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault("OIKONOME_DEV", "1")
        import oikonome.web.app as appmod
        cls.app = appmod.app

    def setUp(self):
        security._reset_ingest_slots()
        security._limiter._hits.clear()

    tearDown = setUp

    def test_a_slow_multipart_at_a_form_route_takes_no_upload_slot(self):
        """/login parses multipart too, and it is reachable with no account,
        no cookie and no Origin header. Charging it to the ingest cap would
        let anyone refuse every upload on the instance from one host."""
        reader = _Reader()
        sent = _drive(_scope("/login", self.app), _two_chunks(), reader)
        self.assertEqual(reader.peak_inflight, 0)
        self.assertEqual(_status(sent), 200)
        self.assertEqual(security._ingest_inflight, 0)

    def test_the_upload_paths_are_read_off_the_router(self):
        """Hand-maintaining this list is how it rots. Derivation is only
        worth anything if it actually finds the routes."""
        paths = set(security.upload_route_paths(self.app))
        for expected in ("/api/import", "/api/transactions/{txn_id}/receipt",
                         "/api/import/bulk/analyze",
                         "/api/import/taxdoc/analyze",
                         "/api/connections/import", "/import"):
            self.assertIn(expected, paths)
        self.assertNotIn("/login", paths)
        self.assertNotIn("/signup", paths)
        # an empty derivation falls back to the content-type gate, so the cap
        # would still work — but the scoping would be gone and nothing would
        # say so. This is that alarm.
        self.assertTrue(paths)

    def test_every_upload_route_is_recognised_from_its_request_path(self):
        """The templates have to survive the trip through path matching —
        /api/transactions/{txn_id}/receipt is the one with a parameter."""
        for template in security.upload_route_paths(self.app):
            concrete = template.replace("{txn_id}", "abc123")
            self.assertTrue(
                security._accepts_upload({"app": self.app, "path": concrete}),
                template)

    def test_a_collector_push_door_is_not_an_upload_route(self):
        """The host-side collectors POST JSON to /api/import/<source>. They
        must never land in the multipart budget."""
        for path in ("/api/import/coinbase", "/api/import/amazon",
                     "/api/import/plan-activity"):
            self.assertFalse(
                security._accepts_upload({"app": self.app, "path": path}),
                path)


class NoSingleClientCanHoldEveryUploadSlot(unittest.TestCase):
    def setUp(self):
        security._reset_ingest_slots()

    tearDown = setUp

    def test_one_address_is_capped_below_the_process_ceiling(self):
        mine = [security._take_ingest_slot("198.51.100.7")
                for _ in range(security.UPLOAD_INGEST_CONCURRENCY)]
        self.assertEqual(sum(1 for t in mine if t),
                         security.UPLOAD_INGEST_PER_IP)
        self.assertLess(security.UPLOAD_INGEST_PER_IP,
                        security.UPLOAD_INGEST_CONCURRENCY)

    def test_another_client_still_gets_in(self):
        for _ in range(security.UPLOAD_INGEST_CONCURRENCY):
            security._take_ingest_slot("198.51.100.7")
        self.assertTrue(security._take_ingest_slot("203.0.113.19"))

    def test_the_process_ceiling_still_holds_across_clients(self):
        taken = 0
        for n in range(20):
            if security._take_ingest_slot(f"203.0.113.{n}"):
                taken += 1
        self.assertEqual(taken, security.UPLOAD_INGEST_CONCURRENCY)

    def test_releasing_frees_both_the_process_and_the_per_client_count(self):
        ip = "198.51.100.7"
        for _ in range(security.UPLOAD_INGEST_PER_IP):
            self.assertTrue(security._take_ingest_slot(ip))
        self.assertFalse(security._take_ingest_slot(ip))
        security._free_ingest_slot(ip)
        self.assertTrue(security._take_ingest_slot(ip))
        for _ in range(security.UPLOAD_INGEST_PER_IP):
            security._free_ingest_slot(ip)
        self.assertEqual(security._ingest_inflight, 0)
        self.assertEqual(security._ingest_by_ip, {})


if __name__ == "__main__":
    unittest.main()
