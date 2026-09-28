"""A pooled tenant checkout remembers the lookups a request repeats,
and forgets each one the moment a statement writes a table it was read
from — never earlier (an unrelated write must not cost the next lookup
its answer) and never later (a stale identity after a bill edit would
match the wrong rows for the rest of the request)."""

import unittest

from oikonome.db import tenancy


class _FakeConn:
    def __init__(self):
        self.statements = []

    def execute(self, q, *a, **kw):
        self.statements.append(q)
        return None


class _FakePool:
    def putconn(self, conn):
        pass


def _checkout():
    return tenancy._PooledConn(_FakePool(), _FakeConn())


class CheckoutMemoTests(unittest.TestCase):
    def test_a_lookup_is_answered_from_the_memo_until_its_table_is_written(self):
        c = _checkout()
        self.assertIsNone(c.memo_get("shadow_ids"))
        c.memo_put("shadow_ids", ["a"], "account_links", "accounts", "items")
        c.memo_put("config", {"x": 1}, "tenant_settings")
        c.execute("INSERT INTO alerts (kind) VALUES ('x')")     # unrelated
        self.assertEqual(c.memo_get("shadow_ids"), ["a"])
        self.assertEqual(c.memo_get("config"), {"x": 1})
        c.execute("UPDATE accounts SET user_removed_at = now() WHERE id = 'a'")
        self.assertIsNone(c.memo_get("shadow_ids"))
        self.assertEqual(c.memo_get("config"), {"x": 1})       # untouched

    def test_a_cte_write_and_a_qualified_target_are_seen(self):
        c = _checkout()
        c.memo_put("k", 1, "bills")
        c.execute("WITH moved AS (SELECT 1) UPDATE ONLY bills SET amount = 2")
        self.assertIsNone(c.memo_get("k"))
        c.memo_put("k", 1, "merchants")
        c.execute("DELETE FROM\n  merchants WHERE id = 'm'")
        self.assertIsNone(c.memo_get("k"))

    def test_ddl_and_non_string_statements_forget_everything(self):
        c = _checkout()
        c.memo_put("k", 1, "bills")
        c.execute("ALTER TABLE x ADD COLUMN y int")
        self.assertIsNone(c.memo_get("k"))
        c.memo_put("k", 1, "bills")
        c.execute(object())
        self.assertIsNone(c.memo_get("k"))

    def test_reads_leave_the_memo_alone(self):
        c = _checkout()
        c.memo_put("k", 1, "bills")
        c.execute("SELECT * FROM bills WHERE payee = 'update'")
        c.execute("SELECT count(*) FROM merchants")
        self.assertEqual(c.memo_get("k"), 1)

    def test_the_memo_does_not_return_to_the_pool(self):
        c = _checkout()
        c.memo_put("k", 1, "bills")
        c.close()
        self.assertIsNone(c.memo_get("k"))


if __name__ == "__main__":
    unittest.main()
