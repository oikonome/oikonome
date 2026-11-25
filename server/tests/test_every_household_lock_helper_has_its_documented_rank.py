"""The lock-order hook (tests/lock_order_hook.py) knows every household lock
the documented order names, by the SQL its helper really runs, and catches
an inversion on a real connection.

If a helper's statement changed shape, the hook would stop recognising it
and every test would silently stop checking that lock; so each helper is run
against a recording connection and its statements must classify to the
rank the order gives it.
"""

import inspect
import unittest
from contextlib import contextmanager

from oikonome import erasure
from oikonome.engine import bills, budget, merchant_identity, storage_quota
from oikonome.sync import reanchor

from . import lock_order_hook as hook
from .util import make_db


class _Recorder:
    """Enough of a connection for a lock helper: it records statements."""

    def __init__(self):
        self.seen: list[tuple] = []

    def execute(self, query, params=None):
        self.seen.append((query, params))
        return self

    def fetchone(self):
        return {"t": "00000000-0000-0000-0000-00000000000a",
                "tid": "00000000-0000-0000-0000-00000000000a", "config": {}}

    def fetchall(self):
        return []

    @contextmanager
    def transaction(self):
        yield


def _ranks(fn) -> set:
    conn = _Recorder()
    try:
        fn(conn)
    except Exception:                                    # noqa: BLE001
        pass            # the statements before a fake reply ran out count
    return {hook.rank_of(q, p) for q, p in conn.seen} - {None}


class EveryHelperIsRecognised(unittest.TestCase):

    def test_each_helper_classifies_to_its_rank(self):
        def config(conn):
            with budget.config_txn(conn):
                pass
        cases = {
            0: bills.hold_bill_claims,
            1: storage_quota.hold_lock,
            2: reanchor.hold_pairing_lock,
            3: merchant_identity._lock_identity,
            4: bills.lock_active_bills,
            5: config,
        }
        for rank, fn in cases.items():
            self.assertEqual(_ranks(fn), {rank}, hook.RANKS[rank])

    def test_the_tax_document_locks_are_ranked_years_then_ids(self):
        """A stored tax document's doors take its years' locks, then the
        household's document-ids lock (taxdocs.lock_document_ids), after
        the bill-ceiling lock a restore holds when its income lands and
        before the settings row it merges later."""
        from oikonome.engine import taxdocs
        self.assertEqual(_ranks(lambda c: taxdocs._lock_years(
            c, [2020, 2021], bounded=False)), {4.5})
        self.assertEqual(_ranks(taxdocs.lock_document_ids), {4.6})
        self.assertLess(4, 4.5)
        self.assertLess(4.6, 5)

    def test_an_edits_shared_pairing_lock_is_ranked_with_the_pairing_lock(self):
        """Every edit door resolves its row under the pairing lock in
        shared mode (data.live_edit_ids). A shared take waits on an
        exclusive holder as the plain one does, so it has the same place
        in the order."""
        from oikonome.web import data
        self.assertEqual(_ranks(lambda c: data.live_edit_ids(c, ["x"])), {2})

    def test_the_member_lock_is_recognised_where_it_is_taken(self):
        from oikonome.auth import invites, reset
        for mod in (invites, reset):
            self.assertIn("oikonome:tenant-members:", inspect.getsource(mod))
        self.assertEqual(hook.rank_of(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            ("oikonome:tenant-members:x",)), 6)
        self.assertEqual(hook.rank_of(
            "SELECT pg_advisory_xact_lock(hashtext("
            "  'oikonome:tenant-members:' || tenant_id::text))"
            "  FROM users WHERE id = %s AND role = 'owner'", ("u",)), 6)

    def test_every_rank_is_one_the_documented_order_names(self):
        comment = inspect.getsource(erasure)
        for name in ("hold_bill_claims", "lock_active_bills", "storage_quota",
                     "hold_pairing_lock", "_lock_identity", "config_txn",
                     "oikonome:tenant-members", "_lock_years",
                     "lock_document_ids"):
            self.assertIn(name, comment)

    def test_a_lock_that_never_waits_is_not_ranked(self):
        self.assertIsNone(hook.rank_of(
            "SELECT pg_try_advisory_xact_lock(hashtext('bills-active'))", ()))
        self.assertIsNone(hook.rank_of(
            "SELECT config FROM tenant_settings FOR UPDATE NOWAIT", ()))


class AnInversionOnARealConnectionIsCaught(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        hook.take_violations()

    def test_pairing_then_storage_is_an_inversion(self):
        with self.conn.transaction():
            reanchor.hold_pairing_lock(self.conn)
            storage_quota.hold_lock(self.conn)
        found = hook.take_violations()
        self.assertEqual(len(found), 1, found)
        self.assertIn("file-storage lock", found[0])
        self.assertIn("pairing lock", found[0])

    def test_the_documented_order_and_a_retake_are_not(self):
        with self.conn.transaction():
            storage_quota.hold_lock(self.conn)
            reanchor.hold_pairing_lock(self.conn)
            storage_quota.hold_lock(self.conn)        # re-entrant
            merchant_identity._lock_identity(self.conn)
        self.assertEqual(hook.take_violations(), [])

    def test_a_new_transaction_starts_with_nothing_held(self):
        with self.conn.transaction():
            merchant_identity._lock_identity(self.conn)
        with self.conn.transaction():
            storage_quota.hold_lock(self.conn)
        self.assertEqual(hook.take_violations(), [])


if __name__ == "__main__":
    unittest.main()
