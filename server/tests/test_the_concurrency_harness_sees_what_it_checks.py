"""The concurrency harness (tests/concurrency.py) sees each failure it
exists to catch: a lost update, a deadlock, a lock left on a pooled
backend, a worker giving up busy, and a seam no actor reaches; and long
mode's random schedules are well formed, replayable from their seed, and
find the lost update too.

Each scenario here is a writer built wrong on purpose, on a household of
its own. A harness that passed these would pass the real scenarios
without having looked.
"""

from __future__ import annotations

import json
import unittest

from oikonome.db import tenancy

from .concurrency import Harness, Scenario, Seam, random_schedules
from .lock_order_util import deadlocks_since
from .util import add_txn

READ = Seam("the unlocked read", sql=r"SELECT config FROM tenant_settings$")
FIRST_ROW = Seam("the first row lock", sql=r"FOR UPDATE -- first")


def _read_then_write(key):
    """The pattern config_txn exists to stop: read the whole document,
    change one key, write the whole document back, with no lock."""
    def write(hh, ctx):
        conn = hh.connect()
        try:
            cfg = conn.execute(
                "SELECT config FROM tenant_settings").fetchone()["config"]
            cfg = dict(cfg or {}, **{key: True})
            conn.execute("UPDATE tenant_settings SET config = %s::jsonb",
                         (json.dumps(cfg),))
        finally:
            conn.close()
    return write


def _keys(hh, ctx):
    cfg = hh.conn.execute(
        "SELECT config FROM tenant_settings").fetchone()["config"]
    return tuple(sorted(k for k in cfg if k.startswith("probe_")))


def _rows_in_order(first, second):
    def write(hh, ctx):
        conn = hh.connect()
        try:
            with conn.transaction():
                conn.execute("SELECT 1 FROM transactions WHERE id = %s"
                             " FOR UPDATE -- first", (first,))
                conn.execute("SELECT 1 FROM transactions WHERE id = %s"
                             " FOR UPDATE -- second", (second,))
        finally:
            conn.close()
    return write


def _two_rows(hh):
    add_txn(hh.conn, "2026-03-02", 10.0, "ROW ONE", txn_id="d1")
    add_txn(hh.conn, "2026-03-02", 20.0, "ROW TWO", txn_id="d2")


class TheHarnessSeesWhatItChecks(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.begin()
        self.addCleanup(lambda: self.h.observer.close())

    def test_a_lost_update_matches_no_serial_order(self):
        found = self.h.run(Scenario(
            "lost update", setup=lambda hh: None,
            actors={"A": _read_then_write("probe_a"),
                    "B": _read_then_write("probe_b")},
            seams=(READ,), digest=_keys))
        self.assertTrue(any("L3" in f and "parks at the unlocked read" in f
                            for f in found), found)

    def test_random_schedules_find_the_lost_update_and_replay(self):
        """Seeded random schedules: the same seed draws the same schedules;
        each actor starts once and resumes only after it started, and only
        if it parks; the label names the seed; and among a handful, one
        lets a writer read, park, and lose the other's write."""
        names, seams = ("A", "B", "C"), (READ, FIRST_ROW)
        drawn = random_schedules(names, seams, seed=7, n=12, tag="t")
        again = random_schedules(names, seams, seed=7, n=12, tag="t")
        self.assertEqual([s.label for s in drawn], [s.label for s in again])
        self.assertNotEqual(
            [s.label for s in drawn],
            [s.label for s in random_schedules(names, seams, seed=8, n=12,
                                               tag="t")])
        for s in drawn:
            self.assertIn("OIKONOME_PROPERTY_SEED=7", s.label)
            seen = [(st.kind, st.names[0]) for st in s.steps]
            self.assertEqual(sorted(n for k, n in seen if k == "go"),
                             sorted(names), s.label)
            for k, n in seen:
                if k == "resume":
                    self.assertIn(n, s.pauses, s.label)
                    self.assertLess(seen.index(("go", n)),
                                    seen.index(("resume", n)), s.label)
            self.assertEqual(sorted(n for k, n in seen if k == "resume"),
                             sorted(s.pauses), s.label)
        found = self.h.run(Scenario(
            "lost update", setup=lambda hh: None,
            actors={"A": _read_then_write("probe_a"),
                    "B": _read_then_write("probe_b")},
            seams=(READ,), digest=_keys),
            schedules=random_schedules(("A", "B"), (READ,), seed=3, n=6,
                                       tag="lost update"))
        self.assertTrue(any("L3" in f and "OIKONOME_PROPERTY_SEED=3" in f
                            for f in found), found)

    def test_two_writers_locking_rows_in_opposite_orders_deadlock(self):
        found = self.h.run(Scenario(
            "opposite orders", setup=_two_rows,
            actors={"A": _rows_in_order("d1", "d2"),
                    "B": _rows_in_order("d2", "d1")},
            seams=(FIRST_ROW,), digest=lambda hh, ctx: ()))
        self.assertTrue(any("L2" in f and "deadlock" in f for f in found),
                        found)
        # the count sees it too, and is waited for here so that a later
        # batch never inherits this one
        self.assertGreaterEqual(
            deadlocks_since(self.h.deadlocks_before, settle=15.0), 1)

    def test_a_lock_left_on_a_backend_is_charged(self):
        held = []

        def leaves_a_lock(hh, ctx):
            conn = tenancy.admin_connect()
            held.append(conn)
            conn.execute("SELECT pg_advisory_lock(hashtext(%s))",
                         (f"probe-leak-{len(held)}",))

        self.addCleanup(lambda: [c.close() for c in held])
        found = self.h.run(Scenario(
            "a held lock", setup=lambda hh: None,
            actors={"A": leaves_a_lock, "B": lambda hh, ctx: None},
            digest=lambda hh, ctx: ()),
            schedules=None)
        self.assertTrue(any("L6" in f for f in found), found)

    def test_a_worker_that_gives_up_busy_is_a_failure(self):
        import psycopg

        def gives_up(hh, ctx):
            raise psycopg.errors.LockNotAvailable("probe")

        found = self.h.run(Scenario(
            "busy worker", setup=lambda hh: None,
            actors={"worker": gives_up, "B": lambda hh, ctx: None},
            digest=lambda hh, ctx: ()))
        self.assertTrue(any("L4" in f and "not a door" in f for f in found),
                        found)

    def test_a_seam_nobody_reaches_is_reported(self):
        found = self.h.run(Scenario(
            "dead seam", setup=lambda hh: None,
            actors={"A": lambda hh, ctx: None, "B": lambda hh, ctx: None},
            seams=(Seam("never", sql="no statement says this"),),
            digest=lambda hh, ctx: ()))
        self.assertTrue(any("ever reached" in f for f in found), found)


if __name__ == "__main__":
    unittest.main()
