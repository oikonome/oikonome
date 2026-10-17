"""A household that turned automatic bill detection off gets no new-bill
or new-income offers from the nightly, and its bills are still kept up.

`bill_detection_auto: false` (set by choosing manual setup) stops the
nightly finder proposing bills and income it found in the ledger, and the
Plaid recurring cross-check with it, and says so in one log line. Amount
drift on the bills the household already has keeps applying on its own,
and the on-demand "Find bills & income" door still proposes.
"""

import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import bills
from oikonome.jobs import worker

from .util import add_bill, add_txn, make_db, write_config

TODAY = dt.date.today()


class NightlyDetectionOffTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        # a monthly charge the finder would offer as a new bill
        for i in range(5):
            add_txn(self.conn, TODAY - dt.timedelta(days=12 + 30 * i), 9.99,
                    "TUNEBOX MUSIC")
        # a tracked bill whose price has moved: drift's to correct
        add_bill(self.conn, "Northwind Power", 440.0,
                 next_due=TODAY + dt.timedelta(days=5),
                 last_seen=TODAY - dt.timedelta(days=25))
        for i, amt in enumerate((370.0, 375.0, 365.0)):
            add_txn(self.conn, TODAY - dt.timedelta(days=10 + 30 * i), amt,
                    "NORTHWIND POWER")

    def tearDown(self):
        self.conn.close()

    def _nightly(self):
        with mock.patch("oikonome.engine.amazon_match.run_match"), \
             mock.patch("oikonome.engine.merchant_dedup.apply"), \
             mock.patch("oikonome.engine.llm_categorize.run"), \
             mock.patch("oikonome.sync.plaid.recurring_streams",
                        return_value=[]):
            return worker.nightly_tenant(self.tid)

    def _new_offers(self) -> list[str]:
        return [r["payee"] for r in self.conn.execute(
            "SELECT payee FROM bill_proposals WHERE status='pending' "
            "AND kind='add'").fetchall()]

    def test_detection_off_offers_nothing_new_but_drift_still_applies(self):
        write_config(self.conn, bill_detection_auto=False)
        with self.assertLogs("oikonome.jobs", "INFO") as logs:
            self._nightly()
        self.assertEqual(self._new_offers(), [])
        self.assertEqual(
            sum("bill detection off" in m for m in logs.output), 1)
        self.assertEqual(
            bills.get_bill(self.conn, "Northwind Power")["amount"], -370.0)

    def test_detection_off_skips_the_plaid_cross_check(self):
        write_config(self.conn, bill_detection_auto=False)
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, "
            "access_token, status) VALUES ('p-ok','plaid','Example Credit "
            "Union','tok','ok') ON CONFLICT (tenant_id, id) DO NOTHING")
        with mock.patch.object(worker.bills, "propose_from_plaid_streams"
                               ) as propose, \
             mock.patch("oikonome.sync.plaid.recurring_streams",
                        return_value=[{"stream_id": "s1"}]) as streams, \
             mock.patch("oikonome.engine.amazon_match.run_match"), \
             mock.patch("oikonome.engine.merchant_dedup.apply"), \
             mock.patch("oikonome.engine.llm_categorize.run"):
            worker.nightly_tenant(self.tid)
        streams.assert_not_called()
        propose.assert_not_called()

    def test_detection_on_still_offers_the_new_bill(self):
        write_config(self.conn)                 # the default: on
        self._nightly()
        self.assertTrue(any("TUNEBOX" in p.upper()
                            for p in self._new_offers()),
                        self._new_offers())

    def test_the_on_demand_finder_ignores_the_setting(self):
        write_config(self.conn, bill_detection_auto=False)
        stats = bills.run(self.conn)            # the "Find bills" door's call
        self.assertGreaterEqual(stats["proposed_add"], 1)
        self.assertTrue(self._new_offers())


if __name__ == "__main__":
    unittest.main()
