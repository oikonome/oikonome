"""A "file under" tap from the email takes the charge's row before the
charge's category pin — the order every in-app category writer takes them —
so a tap landing during an in-app edit of the same charge waits for it
instead of deadlocking with it.

The tap used to claim the `manual_categories` pin first and lock the
`transactions` row only inside set_category. An in-app edit holding the row
and next writing the pin then formed a cycle with it; Postgres killed one
side, and when that was the tap the person was told filing had failed.
"""

import threading
import time
import unittest
from unittest import mock

from oikonome.db import tenancy
from oikonome.notify import mailact

from .util import TODAY, add_txn, make_db, write_config

PIN = """INSERT INTO manual_categories (transaction_id, category, set_at,
                                        bill_id)
         VALUES (%s, %s, now(), NULL)
         ON CONFLICT (tenant_id, transaction_id)
         DO UPDATE SET category = EXCLUDED.category"""


class MailedTapLockOrder(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        self.txn = add_txn(self.conn, TODAY.replace(day=5), 42.0,
                           "CORNER SHOP", primary=None)
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.tok = mailact.parse(mailact.token(
            self.tid, "me@example.dev", "cat", self.txn, "FOOD_AND_DRINK"))
        self.assertIsNotNone(self.tok)

    def test_a_tap_during_an_in_app_edit_waits_for_it(self):
        out, errors = {}, {}
        peer = tenancy.tenant_connect(self.tid)
        self.addCleanup(peer.close)

        def tap():
            with mock.patch.object(mailact, "_standing", return_value=None):
                out["tap"] = mailact.apply(self.tok)

        th = threading.Thread(target=tap)
        try:
            # the in-app edit: row first, then its pin, then the row's
            # override, held past Postgres's deadlock_timeout (1 s)
            with peer.transaction():
                peer.execute("SELECT 1 FROM transactions WHERE id=%s "
                             "FOR UPDATE", (self.txn,))
                th.start()
                time.sleep(0.5)
                peer.execute(PIN, (self.txn, "GROCERIES"))
                peer.execute("UPDATE transactions SET category_override=%s,"
                             " override_source='user' WHERE id=%s",
                             ("GROCERIES", self.txn))
                time.sleep(1.5)
        except Exception as e:                           # noqa: BLE001
            errors["peer"] = type(e).__name__
        th.join(30)
        self.assertEqual(errors, {})
        # the in-app edit committed first, so the mailed guess is stale
        self.assertEqual(out["tap"]["state"], "already", out["tap"])
        row = self.conn.execute(
            "SELECT category_override FROM transactions WHERE id=%s",
            (self.txn,)).fetchone()
        self.assertEqual(row["category_override"], "GROCERIES")

    def test_an_untouched_charge_is_filed(self):
        with mock.patch.object(mailact, "_standing", return_value=None):
            out = mailact.apply(self.tok)
        self.assertEqual(out["state"], "done", out)


if __name__ == "__main__":
    unittest.main()
