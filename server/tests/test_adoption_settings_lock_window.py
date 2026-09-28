"""Adoption must not hold the settings row lock across its row work.

Renaming a restored account onto the id a fresh link minted also rewrites
the settings document, which names accounts by plain string — and that
rewrite goes through `budget.config_txn`, which takes the tenant_settings
ROW lock. A row lock taken inside a transaction is held until that
transaction commits, so where in the sequence it is taken decides how long
every OTHER settings writer waits.

Taken early it spans the accounts DELETE, the unresolvable-pending sweep
and the bookkeeping INSERT as well — none of which touch the document. The
household's settings page, the setup wizard and the budget seed all queue
behind that, once per adopted account, on the SYNCHRONOUS Plaid
link-exchange path: a person is sitting in front of the app while it runs.

So the rewrite goes last, and these pin it: the lock is free while the row
work happens, it is still held around the read-modify-write it exists to
protect, and the document still ends up carrying the new ids.
"""

import datetime as dt
import unittest
from unittest import mock

import psycopg

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.sync import adopt

from .util import make_db, write_config


def _item(conn, iid, status, token=None):
    conn.execute(
        "INSERT INTO items (id, aggregator, institution_id, "
        "institution_name, status, access_token) "
        "VALUES (%s,'plaid','ins_1','Acme Bank',%s,%s)", (iid, status, token))


def _account(conn, aid, iid, mask, name="Checking"):
    conn.execute(
        "INSERT INTO accounts (id, item_id, name, type, mask) "
        "VALUES (%s,%s,%s,'depository',%s)", (aid, iid, name, mask))


class AdoptionSettingsLockWindowTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        _item(self.conn, "old-item", "restored")
        _account(self.conn, "old-chk", "old-item", "1234")
        _account(self.conn, "old-side", "old-item", "5678", "Side business")
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name, "
            "pending, removed) VALUES ('t-1','old-chk',%s,10,'COFFEE',0,0)",
            (dt.date(2026, 6, 1),))
        write_config(self.conn, checking_account_id="old-chk",
                     excluded_accounts=["old-side"])
        _item(self.conn, "new-item", "ok", token="live-token")
        # the observer is a SECOND connection, so what it sees is what a
        # concurrent settings writer in the same household would see
        self.observer = tenancy.tenant_connect(self.tid)
        self.addCleanup(self.observer.close)

    def _settings_lock_free(self) -> bool:
        """Can a settings writer get the row right now?

        `SET LOCAL`, inside a transaction, is the whole point: a plain SET
        would ride the POOLED connection back to the next borrower and
        arm a timeout on queries that never asked for one. LOCAL is
        reverted by the commit, whichever way the statement ends.
        """
        try:
            with self.observer.transaction():
                self.observer.execute("SET LOCAL lock_timeout = '400ms'")
                self.observer.execute(
                    "SELECT config FROM tenant_settings FOR UPDATE")
            return True
        except psycopg.errors.LockNotAvailable:
            return False

    def _adopt(self):
        return adopt.adopt_accounts(
            self.conn, item_id="new-item", institution_id="ins_1",
            institution_name="Acme Bank",
            incoming=[{"account_id": "new-chk", "mask": "1234",
                       "type": "depository"},
                      {"account_id": "new-side", "mask": "5678",
                       "type": "depository"}])

    def test_the_row_work_runs_with_the_settings_lock_free(self):
        seen = []
        real = adopt._retire_orphaned_pending

        def probe(conn, account_id):
            # mid-adoption: the account has been renamed, the bookkeeping
            # row is not written yet, and this is exactly the stretch the
            # early lock used to cover
            seen.append(self._settings_lock_free())
            return real(conn, account_id)

        with mock.patch.object(adopt, "_retire_orphaned_pending", probe):
            self.assertEqual(len(self._adopt()), 2)
        # once per adopted account, and free every time — not one long hold
        # spanning the row work of the whole batch
        self.assertEqual(seen, [True, True],
                         "a settings writer was blocked by row work that "
                         "does not touch the settings document")

    def test_the_document_still_carries_the_new_ids(self):
        """Narrowing the window must not lose the rewrite it narrowed."""
        self.assertEqual(len(self._adopt()), 2)
        cfg = budget.load_config(self.conn)
        self.assertEqual(cfg["checking_account_id"], "new-chk")
        self.assertEqual(cfg["excluded_accounts"], ["new-side"])

    def test_the_lock_still_covers_the_document_rewrite(self):
        """And the probe can tell: the same check that reports FREE during
        the row work reports BLOCKED while the read-modify-write it
        protects is in flight, so the test above is not measuring a lock
        that stopped being taken at all."""
        seen = []
        real = budget.save_config

        def probe(conn, cfg):
            seen.append(self._settings_lock_free())
            return real(conn, cfg)

        with mock.patch.object(budget, "save_config", probe):
            self._adopt()
        self.assertEqual(seen, [False, False],
                         "the settings document was rewritten without the "
                         "row lock — two writers can lose an update")

    def test_the_probe_leaves_no_lock_timeout_on_the_pooled_connection(self):
        self._settings_lock_free()
        self.assertEqual(
            self.observer.execute("SHOW lock_timeout").fetchone()[
                "lock_timeout"], "0")


if __name__ == "__main__":
    unittest.main()
