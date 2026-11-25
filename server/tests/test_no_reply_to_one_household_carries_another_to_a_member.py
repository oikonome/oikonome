"""Nothing crosses from one household to another, for a member of the
first (specs/invariants-and-fuzzing.md, F4; tests/fuzz_checks.py).

A second household holds rows named with canary words under ids of its
own: accounts, an institution, transactions and a note, a bill, a
business, a receipt, a webhook, an account link, its owner and a viewer.
Every route of the product is sent those ids, in every path parameter and
every body field that names a row, and every page with no parameter is
read (`fuzz_checks.CrossingCase`). Property: each reply is a 4xx or
touches nothing of the second household (no row of it written, by the
per-request write count and by a digest of all its rows at the end), and
no canary word or address of it appears in any reply. The doors that
bypass the row-level security (`admin_connect`) are among the routes
like every other. Each reply is also held to F1, F2 and F5.
"""

import unittest

from .fuzz_checks import CrossingCase


class AsMember(CrossingCase, unittest.TestCase):
    ROLE = "member"


if __name__ == "__main__":
    unittest.main()
