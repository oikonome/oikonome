"""A settings save is refused for its shape before it costs anything.

The AI backend list is a body field, and every entry's URL is resolved
before the save takes its row lock. If the size of that list were checked
only inside the lock, a body of tens of thousands of entries would be
looked up name by name first, each lookup up to the resolver deadline,
while the request held a pooled tenant connection and a worker thread —
a few such requests would take the instance down. So the list's shape is
judged before any connection or lookup, and the lookups a single request
may start are bounded by a per-request budget that the address check
itself consults, whichever door calls it. The resolver's caches are
bounded too, so no stream of fresh names can grow them without end.
"""
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from .util import _ensure_db

PW = "correct-horse-battery"


class SettingsSaveRefusesOversizedListsBeforeWork(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import netguard, security
        security._limiter._hits.clear()
        netguard._DNS_CACHE.clear()
        netguard._DNS_LOCKS.clear()
        self.client = TestClient(self.app)
        r = self.client.post("/api/signup", data={
            "email": f"bklookups-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        self.assertEqual(r.status_code, 200, r.text)

    def _counting(self):
        from oikonome.web import api, netguard
        lookups, conns = [], []
        real_conn = api._conn

        def _resolve(host):
            lookups.append(host)
            return ["93.184.216.34"]

        def _conn(user):
            conns.append(1)
            return real_conn(user)
        return lookups, conns, (
            mock.patch.object(netguard, "_getaddrinfo", _resolve),
            mock.patch.object(api, "_conn", _conn))

    def test_a_huge_backend_list_is_refused_with_no_lookup_and_no_connection(self):
        lookups, conns, (p1, p2) = self._counting()
        # compact, so the body stays under the request size limit and
        # reaches the handler
        import json
        body = json.dumps({"llm_backends": [
            {"url": f"http://a{i}.e"} for i in range(40000)]},
            separators=(",", ":"))
        with p1, p2:
            r = self.client.post("/api/settings", content=body,
                                 headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("12", r.text)
        self.assertEqual(lookups, [])
        self.assertEqual(conns, [])

    def test_a_backend_list_that_is_not_a_list_is_refused_before_work(self):
        lookups, conns, (p1, p2) = self._counting()
        with p1, p2:
            r = self.client.post("/api/settings", json={
                "llm_backends": {"url": "http://x.example"}})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual((lookups, conns), ([], []))

    def test_a_backend_entry_that_is_not_an_object_is_refused_before_work(self):
        lookups, conns, (p1, p2) = self._counting()
        with p1, p2:
            r = self.client.post("/api/settings", json={
                "llm_backends": ["http://x.example"]})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual((lookups, conns), ([], []))

    def test_a_full_backend_list_still_saves(self):
        lookups, _conns, (p1, p2) = self._counting()
        body = {"llm_backends": [
            {"id": f"b{i}", "url": f"http://h{i}.llm.example/v1",
             "model": "m"} for i in range(12)], "password": PW}
        with p1, p2:
            r = self.client.post("/api/settings", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(set(lookups)), 12)

    def test_settings_saves_are_rate_limited(self):
        from oikonome.web import api
        from fastapi.routing import APIRoute
        route = next(r for r in api.router.routes
                     if isinstance(r, APIRoute) and r.path.endswith("/settings")
                     and "POST" in r.methods)
        names = [getattr(d.dependency, "__qualname__", "")
                 for d in route.dependencies]
        self.assertTrue(any("limit" in n for n in names), names)


class LookupBudgetBoundsEveryDoor(unittest.TestCase):
    def setUp(self):
        from oikonome.web import netguard
        netguard._DNS_CACHE.clear()
        netguard._DNS_LOCKS.clear()

    def test_fresh_lookups_past_the_budget_are_refused(self):
        from oikonome.web import netguard
        calls = []
        with mock.patch.object(netguard, "_getaddrinfo",
                               lambda h: calls.append(h) or ["1.1.1.1"]):
            with netguard.lookup_budget(3):
                for i in range(3):
                    netguard.check_url(f"https://n{i}.example/")
                # a name already answered costs nothing
                netguard.check_url("https://n0.example/")
                with self.assertRaises(netguard.BlockedURL):
                    netguard.check_url("https://n9.example/")
        self.assertEqual(len(calls), 3)

    def test_no_budget_means_no_limit(self):
        from oikonome.web import netguard
        with mock.patch.object(netguard, "_getaddrinfo",
                               lambda h: ["1.1.1.1"]):
            for i in range(30):
                netguard.check_url(f"https://free{i}.example/")

    def test_a_spent_deadline_stops_further_nameserver_calls(self):
        from oikonome.web import netguard
        calls = []
        with mock.patch.object(netguard, "_getaddrinfo",
                               lambda h: calls.append(h) or ["1.1.1.1"]):
            with netguard.lookup_budget(10, seconds=0.0):
                ips = netguard._resolved_ips("late.example")
        self.assertEqual((calls, ips), ([], []))
        # nothing about the name was learned, so nothing is cached for it
        self.assertNotIn("late.example", netguard._DNS_CACHE)

    def test_dns_caches_stay_bounded(self):
        from oikonome.web import netguard
        with mock.patch.object(netguard, "_getaddrinfo",
                               lambda h: ["1.1.1.1"]):
            for i in range(netguard._DNS_CACHE_MAX + 500):
                netguard._resolved_ips(f"c{i}.example")
        self.assertLessEqual(len(netguard._DNS_CACHE),
                             netguard._DNS_CACHE_MAX)
        # a host's lock lives only while a lookup for it is in flight
        self.assertEqual(len(netguard._DNS_LOCKS), 0)
        # the most recent answers are the ones kept
        last = f"c{netguard._DNS_CACHE_MAX + 499}.example"
        self.assertIn(last, netguard._DNS_CACHE)


if __name__ == "__main__":
    unittest.main()
