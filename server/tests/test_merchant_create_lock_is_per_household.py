"""Minting a merchant by name serialises within one household only.

Two importers in the same household can both find no merchant of a name
and both insert one, so the name-only create takes a lock and looks again
under it. That lock is held until the creating pass commits, and a large
import's pass can run for minutes; keyed on the name alone, it made every
other household minting a merchant of the same common name wait behind
it, or deadlock against it when two households minted two names in
opposite orders.
"""
import contextlib
import unittest

import psycopg

from oikonome.engine import merchant_identity

from .util import make_db


class MerchantCreateLockTests(unittest.TestCase):
    def setUp(self):
        self.a = make_db()
        self.b = make_db()
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)

    def test_another_household_is_not_held_up_by_an_open_create(self):
        with contextlib.ExitStack() as held:
            held.enter_context(self.a.transaction())
            merchant_identity._create(self.a, "Lantern Coffee", "layer1")
            self.b.execute("SET lock_timeout = '500ms'")
            try:
                mid = merchant_identity._create(self.b, "Lantern Coffee",
                                                "layer1")
            except psycopg.errors.LockNotAvailable:
                self.fail("one household's open merchant create blocked "
                          "another household's")
            finally:
                self.b.execute("SET lock_timeout = 0")
        self.assertIsNotNone(mid)

    def test_the_same_household_still_finds_the_merchant_made_first(self):
        first = merchant_identity._create(self.a, "Lantern Coffee", "layer1")
        again = merchant_identity._create(self.a, "lantern coffee", "layer1")
        self.assertEqual(first, again)


if __name__ == "__main__":
    unittest.main()
