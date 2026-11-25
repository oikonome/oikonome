"""A restore writes into its own household and nowhere else
(specs/invariants-and-fuzzing.md, R2).

Household A's export is restored into household B through the restore
job (`restore_job._run`: the restore, then everything it runs after the
commit: the merge onto linked accounts, the recipient invites over the
admin connection, the merchant resolve, the pair repair and the
categorize pass). A and B are built from the same id pools
(tests/roundtrip_builders.py), so the archive's ids collide with B's.
A third household C is a signed-up one with a plan
(tests/money_households.py), and A's archive names C's owner: as a
recipient of A's daily email who accepted, and in A's settings.

Afterwards A and C are byte-identical (every row of every table carrying
their tenant id, the control-plane ones included), and no statement wrote
a row of theirs at all, not even one that left it as it was (the write
counter of tests/write_detector.py). The same holds when B already held
A's archive (a second restore of it), and C's owner never became a
recipient of B's mail.
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.sync import export, restore_job

from . import body_fuzz, props, write_detector
from . import roundtrip_builders as rb
from .money_households import Household
from .restore_invariants import (digest_diff, fresh_household,
                                 tenant_digest, tenant_id)
from .util import seed_accounts


class _Now:
    """The categorize pass after a restore runs in a thread; here it runs
    inside the job, so its writes are counted with the job's."""

    def __init__(self, target=None, **_kw):
        self.target = target

    def start(self):
        self.target()


class ARestoreTouchesOnlyItsOwnHousehold(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.started = time.monotonic()
        write_detector.install()
        try:
            cls.detector = write_detector.Detector()
        except BaseException:
            write_detector.uninstall()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            cls.detector.close()
        finally:
            write_detector.uninstall()
        print(f"\n[timing] {cls.__name__}: "
              f"{time.monotonic() - cls.started:.1f}s", flush=True)

    def households(self, seed):
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        client, c_email, c_tid = body_fuzz.signup(appmod.app, "r2c")
        client.close()
        conn = tenancy.tenant_connect(c_tid)
        try:
            seed_accounts(conn)
            Household(seed, plan=True, conn=conn)
        finally:
            conn.close()
        a = fresh_household()
        try:
            rb.build(a, seed)
            a_tid = tenant_id(a)
            # A's daily email went to C's owner too, who accepted
            budget.save_config(a, {**budget.load_config(a),
                                   "email_recipients": [c_email]})
            admin = tenancy.admin_connect()
            try:
                admin.execute(
                    """INSERT INTO recipient_invites (tenant_id, email,
                                                      created_at, accepted_at)
                       VALUES (%s, %s, now(), now())""", (a_tid, c_email))
            finally:
                admin.close()
            data = export.build_zip(a)
        finally:
            a.close()
        b = fresh_household()
        try:
            rb.build(b, seed + 1000)
            b_tid = tenant_id(b)
        finally:
            b.close()
        return data, a_tid, b_tid, c_tid, c_email

    def test_a_and_c_are_untouched(self):
        for seed in props.seeds():
            with self.subTest(seed=seed):
                data, a_tid, b_tid, c_tid, c_email = self.households(seed)
                before = {t: tenant_digest(t) for t in (a_tid, c_tid)}
                self.detector.delta()
                for attempt in ("first", "again"):
                    conn = tenancy.tenant_connect(b_tid)
                    try:
                        with conn.transaction():
                            self.assertIsNotNone(restore_job._claim(conn))
                    finally:
                        conn.close()
                    with mock.patch("threading.Thread", _Now):
                        restore_job._run(b_tid, data)
                    conn = tenancy.tenant_connect(b_tid)
                    try:
                        job = restore_job.status(conn)
                    finally:
                        conn.close()
                    self.assertEqual(job["state"], "done", (attempt, job))
                    wrote = self.detector.delta()
                    self.assertEqual(
                        {k: n for k, n in wrote.items()
                         if k[0] in (a_tid, c_tid)}, {},
                        f"seed {seed} {attempt}: rows of A or C written")
                    if attempt == "first":
                        self.assertTrue(
                            any(k[0] == b_tid for k in wrote),
                            f"seed {seed}: the restore wrote nothing into B")
                    for tid, label in ((a_tid, "A"), (c_tid, "C")):
                        self.assertEqual(
                            digest_diff(before[tid], tenant_digest(tid)), {},
                            f"seed {seed} {attempt}: {label} changed")
                admin = tenancy.admin_connect()
                try:
                    accepted = admin.execute(
                        """SELECT 1 FROM recipient_invites
                            WHERE tenant_id = %s AND lower(email) = %s
                              AND accepted_at IS NOT NULL""",
                        (b_tid, c_email.lower())).fetchone()
                finally:
                    admin.close()
                self.assertIsNone(accepted, f"seed {seed}: another "
                                  "household's member was enrolled in B's "
                                  "mail")


if __name__ == "__main__":
    unittest.main()
