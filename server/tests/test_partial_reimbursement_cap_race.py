"""Partial-reimbursement caps hold under concurrent links.

Each partial link records ``min(charge's remaining need, deposit's remaining
balance)``, so the sum of link amounts can never exceed the charge (expense
cap) or ``abs(deposit.amount)`` (deposit cap). Both caps are computed from a
SUM read taken before the INSERT — two concurrent links sharing either side
must serialize across that window, or both read the pre-insert total, both
pass the check, and the pair of inserts overshoots the cap (a deposit
credited past its balance, or an expense netted below zero: spend
understated, a false UNDER BUDGET).

These tests pin the widest version of that window open with a barrier: both
writers are parked right after the cap read, then released together.
"""
import contextlib
import threading
import unittest

from oikonome.db import tenancy
from oikonome.web import data

from .util import TODAY, _ensure_db, add_txn, make_db, write_config

# generous: a serialized second writer waits for the first's whole link
_WAIT = 3.0


class _PauseAfterCapRead:
    """Connection proxy that parks the caller at a barrier right after the
    deposit-side cap read — after both caps are computed, before the INSERT.
    With proper serialization only one writer can be inside that window, so
    the barrier times out; without it both arrive and are released together,
    interleaved exactly like two concurrent requests."""

    def __init__(self, conn, barrier):
        self._conn = conn
        self._barrier = barrier

    def execute(self, sql, *a, **kw):
        cur = self._conn.execute(sql, *a, **kw)
        if "reimburse_id=%s AND partial=1" in sql:
            with contextlib.suppress(threading.BrokenBarrierError):
                self._barrier.wait(timeout=_WAIT)
        return cur

    def __getattr__(self, name):
        return getattr(self._conn, name)


class PartialReimbursementCapRaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        write_config(self.conn)

    def _race(self, pairs):
        """Run one link per (txn_id, other_id) pair concurrently, each on its
        own connection, all parked at the post-cap-read barrier."""
        barrier = threading.Barrier(len(pairs))
        results: list[dict] = [None] * len(pairs)

        def work(i, txn_id, other_id):
            conn = tenancy.tenant_connect(self.tid)
            try:
                results[i] = data.link_reimbursement(
                    _PauseAfterCapRead(conn, barrier), txn_id, other_id,
                    partial=True)
            finally:
                conn.close()

        threads = [threading.Thread(target=work, args=(i, t, o), daemon=True)
                   for i, (t, o) in enumerate(pairs)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            self.assertFalse(t.is_alive(), "a linking writer never returned")
        return results

    def _sum(self, col, txn_id):
        return float(self.conn.execute(
            f"SELECT COALESCE(SUM(amount),0) AS s FROM reimbursements "
            f"WHERE {col}=%s AND partial=1", (txn_id,)).fetchone()["s"])

    def test_concurrent_links_cannot_credit_a_deposit_past_its_balance(self):
        """One $500 deposit against two different $300 charges, concurrently:
        the deposit hands out at most $500 total — the second link is clamped
        to the $200 remaining, never a second full $300."""
        a = add_txn(self.conn, TODAY, 300.0, "MEDICAL A", account="chk",
                    primary="MEDICAL")
        b = add_txn(self.conn, TODAY, 300.0, "MEDICAL B", account="chk",
                    primary="MEDICAL")
        dep = add_txn(self.conn, TODAY, -500.0, "INSURANCE CO",
                      account="chk", primary="INCOME")
        results = self._race([(a, dep), (b, dep)])
        self.assertEqual(
            sorted(r.get("received") for r in results), [200.0, 300.0],
            f"both writers recorded a full link — the deposit cap read a "
            f"pre-insert total under concurrency: {results}")
        self.assertLessEqual(self._sum("reimburse_id", dep), 500.0,
                             "Σ link amounts per deposit exceeded the deposit")

    def test_concurrent_links_cannot_reimburse_a_charge_past_its_amount(self):
        """Two different $300 deposits against the same $300 charge,
        concurrently: only one link lands; the other is refused, or the net
        spend on the row goes negative — the false UNDER BUDGET."""
        exp = add_txn(self.conn, TODAY, 300.0, "SPECIALIST", account="chk",
                      primary="MEDICAL")
        d1 = add_txn(self.conn, TODAY, -300.0, "INSURANCE CO",
                     account="chk", primary="INCOME")
        d2 = add_txn(self.conn, TODAY, -300.0, "INSURANCE CO 2",
                     account="chk", primary="INCOME")
        results = self._race([(exp, d1), (exp, d2)])
        errors = [r for r in results if "error" in r]
        self.assertEqual(
            len(errors), 1,
            f"both deposits fully linked to one charge — the expense cap "
            f"read a pre-insert total under concurrency: {results}")
        self.assertIn("fully reimbursed", errors[0]["error"])
        self.assertLessEqual(self._sum("expense_id", exp), 300.0,
                             "Σ received per charge exceeded the charge")


if __name__ == "__main__":
    unittest.main()
