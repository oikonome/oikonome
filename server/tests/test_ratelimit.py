"""rate limits: real client IP + shared store.

Behind a reverse proxy `request.client.host` is the PROXY, so every
visitor shared one bucket (one guesser could lock the whole household
out of login) — and trusting X-Forwarded-For blind is worse, because
then any client picks its own bucket with a forged header. Policy:
the header only counts when the socket peer is listed in
OIKONOME_TRUSTED_PROXIES, and the client is the rightmost UNTRUSTED
hop (the proxy appends the real client at the right; the left of the
chain is attacker-controlled).

Store: in-memory windows multiply the limit by the worker count and
reset on restart. With REDIS_URL (compose already has it for arq) the
window lives in Redis, shared; Redis failure degrades to the in-memory
window instead of failing closed.
"""

import os
import types
import unittest
from unittest import mock

from oikonome.web import security


def _req(peer="203.0.113.9", xff=None):
    headers = {}
    if xff is not None:
        headers["x-forwarded-for"] = xff
    return types.SimpleNamespace(
        client=types.SimpleNamespace(host=peer),
        headers=headers)


class ClientIpTests(unittest.TestCase):
    def test_no_trusted_proxies_ignores_the_header(self):
        with mock.patch.dict(os.environ, {"OIKONOME_TRUSTED_PROXIES": ""}):
            ip = security.client_ip(_req(peer="203.0.113.9",
                                         xff="6.6.6.6"))
        self.assertEqual(ip, "203.0.113.9")

    def test_untrusted_peer_ignores_the_header(self):
        with mock.patch.dict(os.environ,
                             {"OIKONOME_TRUSTED_PROXIES": "10.0.0.1"}):
            ip = security.client_ip(_req(peer="203.0.113.9",
                                         xff="6.6.6.6"))
        self.assertEqual(ip, "203.0.113.9")

    def test_trusted_peer_takes_rightmost_untrusted_hop(self):
        with mock.patch.dict(os.environ,
                             {"OIKONOME_TRUSTED_PROXIES": "10.0.0.0/24"}):
            # forged left entries don't matter — the proxy appended the
            # real client at the right
            ip = security.client_ip(
                _req(peer="10.0.0.1", xff="6.6.6.6, 198.51.100.7"))
        self.assertEqual(ip, "198.51.100.7")

    def test_chained_trusted_proxies_are_skipped(self):
        with mock.patch.dict(
                os.environ,
                {"OIKONOME_TRUSTED_PROXIES": "10.0.0.0/24, 172.16.0.5"}):
            ip = security.client_ip(
                _req(peer="10.0.0.1", xff="198.51.100.7, 172.16.0.5"))
        self.assertEqual(ip, "198.51.100.7")

    def test_trusted_peer_without_header_stays_the_peer(self):
        with mock.patch.dict(os.environ,
                             {"OIKONOME_TRUSTED_PROXIES": "10.0.0.1"}):
            ip = security.client_ip(_req(peer="10.0.0.1"))
        self.assertEqual(ip, "10.0.0.1")

    def test_garbage_cidr_entries_are_ignored(self):
        with mock.patch.dict(
                os.environ,
                {"OIKONOME_TRUSTED_PROXIES": "not-an-ip, 10.0.0.1"}):
            ip = security.client_ip(_req(peer="10.0.0.1", xff="9.9.9.9"))
        self.assertEqual(ip, "9.9.9.9")


class SeparateBucketsTests(unittest.TestCase):
    def test_two_clients_behind_one_proxy_get_separate_buckets(self):
        security._limiter._hits.clear()
        dep = security.limit(f"bkt-{id(self)}", 2, 60)
        with mock.patch.dict(os.environ,
                             {"OIKONOME_TRUSTED_PROXIES": "10.0.0.1"}), \
             mock.patch.object(security, "_redis_limiter", False):
            a = _req(peer="10.0.0.1", xff="198.51.100.7")
            b = _req(peer="10.0.0.1", xff="198.51.100.8")
            dep(a), dep(a)                       # client A uses its 2
            from fastapi import HTTPException
            with self.assertRaises(HTTPException):
                dep(a)                           # A is limited…
            dep(b)                               # …B is not


class _FakePipe:
    def __init__(self, store):
        self.store, self.ops = store, []

    def zremrangebyscore(self, k, lo, hi):
        self.ops.append(("zrem", k, lo, hi))

    def zcard(self, k):
        self.ops.append(("zcard", k))

    def zadd(self, k, mapping):
        self.ops.append(("zadd", k, mapping))

    def expire(self, k, s):
        self.ops.append(("expire", k, s))

    def execute(self):
        out = []
        for op in self.ops:
            if op[0] == "zrem":
                _, k, lo, hi = op
                kept = {m: s for m, s in self.store.get(k, {}).items()
                        if not (lo <= s <= hi)}
                self.store[k] = kept
                out.append(0)
            elif op[0] == "zcard":
                out.append(len(self.store.get(op[1], {})))
            elif op[0] == "zadd":
                self.store.setdefault(op[1], {}).update(op[2])
                out.append(1)
            else:
                out.append(True)
        return out


def _window_eval(store, args):
    """The window script the limiter EVALs, in Python. Mirrors the Lua:
    trim, count, and admit-plus-record as ONE indivisible step."""
    key, cutoff, limit, now, _ttl, member = args
    kept = {m: s for m, s in store.get(key, {}).items() if s > float(cutoff)}
    store[key] = kept
    if len(kept) >= int(limit):
        return 0
    kept[member] = float(now)
    return 1


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def pipeline(self):
        return _FakePipe(self.store)

    def eval(self, script, numkeys, *args):
        return _window_eval(self.store, args)


class _InterleavingRedis(_FakeRedis):
    """Lets a second caller run at the END of the first round trip — the
    only place a concurrent worker can slip in. A one-round-trip check has
    no such place inside it; a check-then-add has one right in the middle."""

    def __init__(self):
        super().__init__()
        self.on_boundary = None

    def _boundary(self):
        cb, self.on_boundary = self.on_boundary, None   # fires once
        if cb:
            cb()

    def pipeline(self):
        outer = self

        class _Pipe(_FakePipe):
            def execute(self):
                out = super().execute()
                outer._boundary()
                return out

        return _Pipe(self.store)

    def eval(self, script, numkeys, *args):
        out = super().eval(script, numkeys, *args)
        self._boundary()
        return out


class RedisWindowTests(unittest.TestCase):
    def _limiter(self):
        lim = security.RedisRateLimiter.__new__(security.RedisRateLimiter)
        lim._r = _FakeRedis()
        return lim

    def test_window_admits_then_blocks(self):
        lim = self._limiter()
        self.assertTrue(lim.check(("login", "1.2.3.4"), 3, 60))
        self.assertTrue(lim.check(("login", "1.2.3.4"), 3, 60))
        self.assertTrue(lim.check(("login", "1.2.3.4"), 3, 60))
        self.assertFalse(lim.check(("login", "1.2.3.4"), 3, 60))
        # a different IP is a different bucket
        self.assertTrue(lim.check(("login", "5.6.7.8"), 3, 60))

    def test_old_entries_expire_out_of_the_window(self):
        lim = self._limiter()
        for _ in range(3):
            self.assertTrue(lim.check(("k",), 3, 60))
        self.assertFalse(lim.check(("k",), 3, 60))
        # age everything past the window → admits again
        key = next(iter(lim._r.store))
        lim._r.store[key] = {m: s - 61 for m, s in lim._r.store[key].items()}
        self.assertTrue(lim.check(("k",), 3, 60))

    def test_two_callers_cannot_both_take_the_last_slot(self):
        """The limit is the whole defense, so it has to hold when requests
        overlap — which, in a multi-worker deployment, is the normal case
        and exactly what a brute-force or an SMS toll-fraud run produces.

        `_InterleavingRedis` runs a second caller at the first round-trip
        BOUNDARY. A limiter that reads the count in one round trip and
        records the hit in another leaves that boundary open, and both
        callers pass a limit of one."""
        fake = _InterleavingRedis()
        lim = security.RedisRateLimiter.__new__(security.RedisRateLimiter)
        lim._r = fake
        verdicts = []
        fake.on_boundary = lambda: verdicts.append(lim.check(("race",), 1, 60))
        verdicts.append(lim.check(("race",), 1, 60))
        self.assertEqual(
            1, sum(1 for v in verdicts if v),
            f"a limit of 1 admitted {sum(1 for v in verdicts if v)} "
            f"overlapping callers: {verdicts}")

    def test_the_in_process_window_holds_under_real_threads(self):
        """The fallback store must enforce the same ceiling — a Redis
        outage degrades the limiter's SCOPE, never its arithmetic."""
        import threading
        lim = security.RateLimiter()
        verdicts, lock = [], threading.Lock()
        start = threading.Barrier(24)

        def hit():
            start.wait()
            v = lim.check(("threads",), 5, 60)
            with lock:
                verdicts.append(v)

        threads = [threading.Thread(target=hit) for _ in range(24)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(5, sum(1 for v in verdicts if v))

    def test_redis_error_returns_none_and_dep_falls_back(self):
        import redis as redis_mod

        class _Down:
            def pipeline(self):
                raise redis_mod.RedisError("down")

            def eval(self, *a, **kw):
                raise redis_mod.RedisError("down")

        lim = security.RedisRateLimiter.__new__(security.RedisRateLimiter)
        lim._r = _Down()
        self.assertIsNone(lim.check(("k",), 3, 60))
        # the dependency degrades to the in-memory window, not a lockout
        security._limiter._hits.clear()
        dep = security.limit(f"fb-{id(self)}", 1, 60)
        with mock.patch.object(security, "_redis_limiter", lim):
            dep(_req())                           # allowed via memory
            from fastapi import HTTPException
            with self.assertRaises(HTTPException):
                dep(_req())                       # memory window enforces


if __name__ == "__main__":
    unittest.main()
