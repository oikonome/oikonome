"""A pooled connection whose session state cannot be trusted is dropped,
not returned: a session-level advisory lock whose unlock failed would ride
into the next borrower and hold the tenant's sync lock until the backend
died."""

import time
import unittest

from oikonome.db import tenancy

from .util import make_db


class DiscardTests(unittest.TestCase):
    def test_discard_drops_the_session_and_the_pool_recovers(self):
        conn = make_db()
        try:
            tid = conn.execute(
                "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        finally:
            conn.close()
        c1 = tenancy.tenant_connect(tid)
        c1.execute("SELECT pg_advisory_lock(hashtext('oikonome:sync:x'))")
        c1.discard()
        c2 = tenancy.tenant_connect(tid)
        try:
            # Closing the socket asks the backend to exit; the server frees
            # the session's locks when that backend is gone, a moment later
            # under load. A leak holds it for good, so a few seconds of
            # polling tells the two apart.
            deadline = time.monotonic() + 5
            while True:
                got = c2.execute(
                    "SELECT pg_try_advisory_lock(hashtext('oikonome:sync:x'))"
                    " AS ok").fetchone()["ok"]
                if got or time.monotonic() > deadline:
                    break
                time.sleep(0.05)
            self.assertTrue(got, "the discarded session's lock leaked")
            c2.execute("SELECT pg_advisory_unlock(hashtext('oikonome:sync:x'))")
        finally:
            c2.close()
