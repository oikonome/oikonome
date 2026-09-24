"""Linking a reimbursement is one indivisible act, serialized per household.

A link writes three things — the pair, and a pass-through category on each
side — and the money math only adds up once all three have landed. Between
them the charge is still ordinary spend while the pair already exists, so a
reader in that window counts the same money twice, and a failure in that
window leaves it that way permanently.

Concurrency is the second half of the same problem. The partial caps are
check-then-act (read a SUM, compare, INSERT), so two links sharing a side
must not interleave; and links that take row-scoped locks take them in an
ORDER, which is a deadlock waiting for a third call site. One per-tenant
lock, taken for the whole link, answers both.
"""

import contextlib
import threading
import unittest

from oikonome.db import tenancy
from oikonome.web import data

from .util import TODAY, _ensure_db, add_txn, make_db, write_config

# generous: a serialized second writer waits out the first writer's whole link
_WAIT = 3.0


class _PauseInsideTheLink:
    """Connection proxy that parks the caller mid-link — after the two rows
    are read, before anything is written. With the link serialized only one
    writer can be in there, so the barrier times out; without it both arrive
    and are released together, interleaved like two concurrent requests."""

    def __init__(self, conn, barrier):
        self._conn = conn
        self._barrier = barrier

    def execute(self, sql, *a, **kw):
        cur = self._conn.execute(sql, *a, **kw)
        if "COALESCE(t.entity_id, a.entity_id)::text AS entity" in sql:   # the link's first read of both rows
            with contextlib.suppress(threading.BrokenBarrierError):
                self._barrier.wait(timeout=_WAIT)
        return cur

    def __getattr__(self, name):
        return getattr(self._conn, name)


class ReimbursementLinkAtomicityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        write_config(self.conn)
        self.exp = add_txn(self.conn, TODAY, 120.0, "CITY CLINIC",
                           account="chk", primary="MEDICAL")
        self.dep = add_txn(self.conn, TODAY, -120.0, "INSURANCE CO",
                           account="chk", primary="INCOME")

    def _pairs(self) -> int:
        return len(self.conn.execute(
            "SELECT 1 FROM reimbursements WHERE expense_id=%s "
            "AND reimburse_id=%s", (self.exp, self.dep)).fetchall())

    def _category(self, txn_id: str):
        return self.conn.execute(
            "SELECT category_override FROM transactions WHERE id=%s",
            (txn_id,)).fetchone()["category_override"]

    def test_a_failed_link_leaves_no_pair_behind(self):
        """The pair and both pass-through categories land together or not
        at all. A pair whose expense is still ordinary spend is counted
        twice by every spend total that reads it."""
        real = data.set_category
        calls = []

        def explode(conn, txn_id, category, **kw):
            calls.append(txn_id)
            if txn_id == self.dep:            # the second of the two writes
                raise RuntimeError("backend went away mid-link")
            return real(conn, txn_id, category, **kw)

        data.set_category = explode
        try:
            with self.assertRaises(RuntimeError):
                data.link_reimbursement(self.conn, self.exp, self.dep)
        finally:
            data.set_category = real
        self.assertEqual(calls, [self.exp, self.dep])   # it got that far
        self.assertEqual(self._pairs(), 0,
                         "a half-written link left a pair on the books")
        self.assertIsNone(self._category(self.exp),
                          "a half-written link left one side recategorized")

    def test_two_links_on_the_same_pair_in_opposite_orders_both_settle(self):
        """Two requests naming the same charge and deposit in opposite
        argument orders, released together. Neither may deadlock, and the
        household ends with exactly one pair, both sides pass-through."""
        barrier = threading.Barrier(2)
        results: list = [None, None]
        errors: list[Exception] = []

        def work(i, first, second):
            conn = tenancy.tenant_connect(self.tid)
            try:
                results[i] = data.link_reimbursement(
                    _PauseInsideTheLink(conn, barrier), first, second)
            except Exception as e:                      # noqa: BLE001
                errors.append(e)
            finally:
                conn.close()

        threads = [
            threading.Thread(target=work, args=(0, self.exp, self.dep),
                             daemon=True),
            threading.Thread(target=work, args=(1, self.dep, self.exp),
                             daemon=True)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            self.assertFalse(t.is_alive(), "a linking writer never returned")
        self.assertEqual(errors, [],
                         f"a concurrent link failed outright: {errors}")
        self.assertEqual(self._pairs(), 1, "the pair was written twice")
        self.assertEqual(self._category(self.exp), "TRANSFER_OUT")
        self.assertEqual(self._category(self.dep), "TRANSFER_IN")

    def test_a_partial_link_still_records_what_came_back(self):
        """The serialization must not cost the behaviour it protects."""
        out = data.link_reimbursement(self.conn, self.exp, self.dep,
                                      partial=True)
        self.assertEqual(out["received"], 120.0)
        self.assertEqual(self._category(self.dep), "TRANSFER_IN")
        self.assertIsNone(self._category(self.exp),
                          "a partial pair must leave the charge's category")


if __name__ == "__main__":
    unittest.main()
