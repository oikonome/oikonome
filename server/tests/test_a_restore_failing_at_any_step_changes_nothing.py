"""A restore that fails at any step leaves the household exactly as it was
(specs/invariants-and-fuzzing.md, R4; specs/restore-landers.md).

A populated household (tests/roundtrip_builders.py) receives another
household's export through the restore job (`restore_job._run`, the
worker thread's own body), and the restore is made to fail at one step:
after each lander of `landers.ORDER` and each `Step` between them has
done its work, after the restore locks and `Ctx.prepare`, after each part
of `finalize` (the settings merge and every re-key and cut after it), and
after `FINALIZE` as a whole. For every step:

* the job reports the failure (`job_progress` state `error`);
* every row of every table carrying the household's tenant id is
  byte-identical to before, apart from the job's own status row;
* nothing that runs after the commit ran: `AFTER_COMMIT` (the merge onto
  linked accounts, the recipient invites, the merchant resolve) and the
  pair repair, which `restore_now` runs before the categorize pass;
* the job's connection let go of the household's sync lock.

Outside this rule by design: `AFTER_COMMIT` and what `restore_job.
restore_now` runs after it (the pair repair, `web.data.repair_pairs`, and
the categorize pass). They run once the restore has committed, each a
transaction of its own whose failure costs only itself and is mended by
the nightly sweep; a failure there leaves the restored rows in place, as
designed (sync/restore.py, `after_commit`).
"""

from __future__ import annotations

import importlib
import time
import unittest
from unittest import mock

from oikonome.db import tenancy
from oikonome.sync import export, landers, restore, restore_job

from . import roundtrip_builders as rb
from .restore_invariants import (digest_diff, fresh_household,
                                 tenant_digest, tenant_id)

SEED = 3
JOB_TABLE = "job_progress"


class Injected(RuntimeError):
    """The failure made to happen at one step."""


def _entry_name(entry) -> str:
    return entry.name if isinstance(entry, landers.Step) else entry


# what runs inside the transaction outside the landers, each patched
# where the restore looks it up: (label, patch target)
PARTS = (
    ("the restore locks", "oikonome.web.bulk_import.hold_restore_write_locks"),
    ("finalize: the settings merge", "oikonome.sync.restore._settle_settings"),
    ("finalize: re-key numbered accounts",
     "oikonome.sync.base.rekey_numbered_accounts"),
    ("finalize: key file-import ids", "oikonome.sync.base.key_file_import_ids"),
    ("finalize: stored names of hand-made accounts",
     "oikonome.sync.account_sources.backfill"),
    ("finalize: cut detected bill keys",
     "oikonome.engine.bills.cut_detected_keys"),
    ("finalize: cut the names the log quotes",
     "oikonome.engine.activity.cut_stored_names"),
)


def _then_fail(real):
    def run(*a, **kw):
        real(*a, **kw)
        raise Injected("injected")
    return run


def _real(target: str):
    mod, attr = target.rsplit(".", 1)
    return getattr(importlib.import_module(mod), attr)


class ARestoreFailingAtAnyStepChangesNothing(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.started = time.monotonic()
        src = fresh_household()
        try:
            rb.build(src, SEED)
            cls.data = export.build_zip(src)
        finally:
            src.close()
        dest = fresh_household()
        try:
            rb.build(dest, SEED + 1000)
            cls.tid = tenant_id(dest)
        finally:
            dest.close()
        cls.before = cls.digest()

    @classmethod
    def tearDownClass(cls):
        print(f"\n[timing] {cls.__name__}: "
              f"{time.monotonic() - cls.started:.1f}s", flush=True)

    @classmethod
    def digest(cls) -> dict:
        d = tenant_digest(cls.tid)
        d.pop(JOB_TABLE, None)
        return d

    def run_failing(self, where: str, *patches):
        """Claim the job, run it with `patches` in force, and hold the
        household to R4."""
        conn = tenancy.tenant_connect(self.tid)
        try:
            with conn.transaction():
                self.assertIsNotNone(restore_job._claim(conn), where)
        finally:
            conn.close()
        after = mock.Mock(side_effect=AssertionError("after the commit"))
        raised = []
        real_restore = restore.restore_zip

        def restore_zip(*a, **kw):
            try:
                return real_restore(*a, **kw)
            except BaseException as e:
                raised.append(e)
                raise

        with mock.patch.object(restore, "restore_zip", restore_zip), \
                mock.patch.object(landers, "AFTER_COMMIT",
                               landers.Step("after_commit", after)), \
                mock.patch("oikonome.web.data.repair_pairs", after):
            for p in patches:
                p.start()
            try:
                restore_job._run(self.tid, self.data)
            finally:
                for p in reversed(patches):
                    p.stop()
        after.assert_not_called()
        # the job failed at the step, not before it for some other reason
        self.assertEqual([type(e) for e in raised], [Injected], where)
        conn = tenancy.tenant_connect(self.tid)
        try:
            job = restore_job.status(conn)
            # the sync lock went with the job's connection: it is free
            free = conn.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                (f"oikonome:sync:{self.tid}",)).fetchone()["ok"]
            if free:
                conn.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                             (f"oikonome:sync:{self.tid}",))
        finally:
            conn.close()
        self.assertEqual(job["state"], "error", (where, job))
        self.assertTrue(free, f"{where}: the household's sync lock is held")
        self.assertEqual(digest_diff(self.before, self.digest()), {},
                         f"{where}: the household changed")

    def test_the_same_restore_unfailed_writes_rows(self):
        """The control: without a failure the same archive writes rows
        into the same kind of household, so an unchanged digest below
        means the failure undid them."""
        conn = fresh_household()
        try:
            rb.build(conn, SEED + 1000)
            _result, out = restore.restore_outcomes(conn, self.data)
        finally:
            conn.close()
        self.assertGreater(sum(out.written.values()), 20, out.written)

    def test_a_failure_after_any_lander_or_step(self):
        real_land, real_step = landers.land_one, landers.run_step
        for entry in landers.ORDER:
            name = _entry_name(entry)

            def land(conn, z, ctx, out, L, _n=name):
                real_land(conn, z, ctx, out, L)
                if L.table == _n:
                    raise Injected(_n)

            def step(s, conn, ctx, out, _n=name):
                got = real_step(s, conn, ctx, out)
                if s.name == _n:
                    raise Injected(_n)
                return got

            with self.subTest(step=name):
                self.run_failing(
                    name, mock.patch.object(landers, "land_one", land),
                    mock.patch.object(landers, "run_step", step))

    def test_a_failure_before_any_row_and_inside_finalize(self):
        cases = [
            ("Ctx.prepare", mock.patch.object(
                landers.Ctx, "prepare", _then_fail(landers.Ctx.prepare))),
            ("finalize as a whole", mock.patch.object(
                landers, "FINALIZE", landers.Step(
                    "finalize", _then_fail(landers.FINALIZE.run)))),
        ] + [(label, mock.patch(target, _then_fail(_real(target))))
             for label, target in PARTS]
        for label, patch in cases:
            with self.subTest(step=label):
                self.run_failing(label, patch)


if __name__ == "__main__":
    unittest.main()
