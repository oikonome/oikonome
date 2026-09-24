"""Which merchant a payee gets must not depend on the order rows arrive in.

An unenriched row keyed on the bank's own descriptor takes the merchant the
enriched rows of that descriptor already have. That reads the ledger, so it
is only an answer if it sees the same ledger however the rows are presented:
within one pass the scan has no ORDER BY, and across two overlapping passes
neither transaction can see the other's uncommitted writes. Both cases end
in a second merchant for one payee, and neither is recoverable — a resolved
row is never revisited.
"""

import os
import threading
import unittest

from oikonome.db import tenancy
from oikonome.engine import merchant_identity

from . import util
from .util import add_txn, as_date, jsonb, make_db, write_config

DESCRIPTOR = "EXAMPLE TELECOM NEW YORK USA"


class ResolutionOrderTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")

    def tearDown(self):
        self.conn.close()

    def _unenriched(self, date, amount, name, txn_id):
        """A row the aggregator gave no merchant_name — what a refund
        usually looks like."""
        self.conn.execute(
            """INSERT INTO transactions (id,account_id,date,amount,name,
                   merchant_name,category_primary,pending,removed,raw)
               VALUES (%s,'card',%s,%s,%s,NULL,'GENERAL_MERCHANDISE',0,0,%s)""",
            (txn_id, as_date(date), amount, name, jsonb({})))
        return txn_id

    def _merchant_of(self, txn_id, conn=None):
        return (conn or self.conn).execute(
            "SELECT m.id, m.name FROM transactions t "
            "JOIN merchants m ON m.id = t.merchant_id WHERE t.id=%s",
            (txn_id,)).fetchone()

    def _scan_order(self):
        """The order the resolver's own SELECT will hand the rows back in."""
        return [r["id"] for r in self.conn.execute(
            "SELECT t.id FROM transactions t "
            " WHERE t.removed = 0 AND t.merchant_id IS NULL").fetchall()]

    def test_the_unenriched_half_may_be_scanned_first(self):
        """Both halves of one payee in ONE pass, unenriched half first.

        This is the ordinary arrival shape, not a contrived one: the
        aggregator enriches the charge and leaves the refund bare, and the
        refund is the newer row. Deciding it first finds nothing under the
        descriptor and mints a second merchant."""
        self._unenriched("2026-06-04", -21.70, DESCRIPTOR, "ref5")
        add_txn(self.conn, "2026-06-03", 21.70, DESCRIPTOR,
                merchant="example telecom", txn_id="chg5")
        order = self._scan_order()
        self.assertLess(order.index("ref5"), order.index("chg5"),
                        "the fixture must present the unenriched row FIRST "
                        "— that order is the whole point of this test")

        merchant_identity.resolve(self.conn)

        refund, charge = self._merchant_of("ref5"), self._merchant_of("chg5")
        self.assertIsNotNone(refund)
        self.assertIsNotNone(charge)
        self.assertEqual(refund["id"], charge["id"],
                         f"refund got {refund['name']!r}, "
                         f"charge got {charge['name']!r}")

    def test_one_merchant_however_the_rows_are_ordered(self):
        """The same fixture in either order yields one merchant, and the
        SAME one — the outcome is a function of the ledger, not the scan."""
        self._unenriched("2026-06-04", -21.70, DESCRIPTOR, "ref6")
        add_txn(self.conn, "2026-06-03", 21.70, DESCRIPTOR,
                merchant="example telecom", txn_id="chg6")
        merchant_identity.resolve(self.conn)
        unenriched_first = self._merchant_of("ref6")["name"]

        other = make_db()
        self.addCleanup(other.close)
        write_config(other, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")
        add_txn(other, "2026-06-03", 21.70, DESCRIPTOR,
                merchant="example telecom", txn_id="chg7")
        other.execute(
            """INSERT INTO transactions (id,account_id,date,amount,name,
                   merchant_name,category_primary,pending,removed,raw)
               VALUES ('ref7','card',%s,-21.70,%s,NULL,
                       'GENERAL_MERCHANDISE',0,0,%s)""",
            (as_date("2026-06-04"), DESCRIPTOR, jsonb({})))
        merchant_identity.resolve(other)

        self.assertEqual(self._merchant_of("ref7", other)["id"],
                         self._merchant_of("chg7", other)["id"])
        self.assertEqual(self._merchant_of("ref7", other)["name"],
                         unenriched_first)

    def test_a_second_pass_waits_for_the_one_in_flight(self):
        """Two overlapping passes over one household.

        The ingest hook for a fresh row and the nightly sweep are separate
        transactions, and under READ COMMITTED the second cannot see the
        merchant the first has written and not committed. Both then find
        nothing under the descriptor and both mint. Two REAL connections
        here, the first deliberately held open across the second's whole
        run."""
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        add_txn(self.conn, "2026-06-03", 21.70, DESCRIPTOR,
                merchant="example telecom", txn_id="chg9")
        self._unenriched("2026-06-04", -21.70, DESCRIPTOR, "ref9")

        app_dsn = os.environ.get(
            "OIKONOME_TEST_DSN",
            f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{util.TEST_DB}")
        second = tenancy.tenant_connect(tid, app_dsn)
        self.addCleanup(second.close)

        finished = threading.Event()
        raised: list = []

        def second_pass():
            try:
                merchant_identity.resolve(second, txn_ids=["ref9"])
            except Exception as exc:            # reported by the assert below
                raised.append(exc)
            finally:
                finished.set()

        worker = threading.Thread(target=second_pass)
        with self.conn.transaction():
            # the enriched half decided, and deliberately not yet committed
            merchant_identity.resolve(self.conn, txn_ids=["chg9"])
            worker.start()
            # every chance to run to completion: unless it is made to wait,
            # it gets here, sees nothing, and mints its own merchant
            finished.wait(timeout=3.0)
        worker.join(timeout=30)
        self.assertFalse(worker.is_alive(), "the second pass never finished")
        self.assertFalse(raised, f"the second pass raised {raised}")

        refund = self._merchant_of("ref9")
        charge = self._merchant_of("chg9")
        self.assertIsNotNone(refund)
        self.assertEqual(refund["id"], charge["id"],
                         f"refund got {refund['name']!r}, "
                         f"charge got {charge['name']!r}")
        n = self.conn.execute(
            "SELECT count(*) AS n FROM merchants "
            " WHERE lower(name) LIKE 'example telecom%'").fetchone()["n"]
        self.assertEqual(n, 1, "two passes minted twin merchants")


if __name__ == "__main__":
    unittest.main()
