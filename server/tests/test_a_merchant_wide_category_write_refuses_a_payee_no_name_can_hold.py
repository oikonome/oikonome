"""The merchant-wide category doors refuse a payee longer than any stored
name with a 400, and still take the longest name the ledger can hold.

Their payee becomes a token pattern the database compiles. Unbounded, one
long word in it was a pattern Postgres refuses ("regular expression is too
complex"), which answered 500 — on the preview a person opens before the
write, and on the write itself.
"""

import json
import unittest

from oikonome.db import tenancy
from oikonome.sync.base import MAX_DESCRIPTOR

from . import body_fuzz as fuzz
from .util import add_txn

DOORS = ("/api/bills/merchant-category/preview",
         "/api/bills/merchant-category")


class APayeeNoNameCanHoldIsRefused(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.hh = fuzz.Household(appmod.app)

    def _post(self, path, payee):
        from oikonome.web import security
        security._limiter._hits.clear()
        return self.hh.client.post(path, content=json.dumps(
            {"payee": payee, "category": "FOOD_AND_DRINK"}),
            headers={"content-type": "application/json"})

    def test_one_long_word_is_a_400_not_a_500(self):
        for path in DOORS:
            r = self._post(path, "abcd" * 50_000)
            self.assertEqual(r.status_code, 400, (path, r.text[:80]))
            self.assertIn("too long", r.json()["detail"])

    def test_the_longest_name_the_ledger_holds_still_previews(self):
        longest = ("Grocer" + "x" * MAX_DESCRIPTOR)[:MAX_DESCRIPTOR]
        conn = tenancy.tenant_connect(self.hh.tid)
        try:
            add_txn(conn, fuzz.DAY, 9.5, longest, txn_id="long-descriptor")
        finally:
            conn.close()
        r = self._post(DOORS[0], longest)
        self.assertEqual(r.status_code, 200, r.text[:80])
        self.assertEqual(self._post(DOORS[0], longest + "y").status_code, 400)


if __name__ == "__main__":
    unittest.main()
