"""A collector push whose JSON is nested thousands of levels deep is a bad
payload (400), never a server error.

Python's JSON reader takes such a document; the walk that drops NUL
characters from every pushed string recursed into it until the interpreter
gave up, and the RecursionError answered 500 at every push door that walks
its payload (crypto, store orders, store receipts).
"""

import unittest

from oikonome.engine.compat import strip_nul

from . import body_fuzz as fuzz

DEEP = '{"a":' * 5000 + "1" + "}" * 5000


class ADeepPushIsABadPayload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.hh = fuzz.Household(appmod.app)

    def test_every_push_door_answers_400(self):
        from oikonome.web import security
        bodies = {
            "/api/import/coinbase": '{"accounts": [{"raw": %s}]}' % DEEP,
            "/api/import/amazon": '{"orders": [{"items": %s}]}' % DEEP,
            "/api/import/costco": '{"receipts": [{"items": %s}]}' % DEEP,
        }
        for path, raw in bodies.items():
            security._limiter._hits.clear()
            r = self.hh.client.post(path, content=raw, headers={
                "content-type": "application/json"})
            self.assertEqual(r.status_code, 400, (path, r.text[:120]))

    def test_the_walk_refuses_a_document_past_its_reach(self):
        deep = cur = {}
        for _ in range(5000):
            cur["a"] = {}
            cur = cur["a"]
        with self.assertRaises(ValueError):
            strip_nul(deep)
        self.assertEqual(strip_nul({"a": ["x\x00y", {"b": "\x00"}]}),
                         {"a": ["xy", {"b": ""}]})


if __name__ == "__main__":
    unittest.main()
