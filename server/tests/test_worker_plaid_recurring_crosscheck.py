"""The nightly Plaid recurring cross-check spends one Plaid call per item.

Only healthy items are called; one item's failure never drops the others'
proposals.

1. The hourly sync retries broken items because retrying is how a re-auth
   or an outage is noticed to have cleared; this cross-check has no such
   duty, so an item parked in login_required or error:* would buy the same
   400 every night — and a shell item restored from an export has no access
   token to call with at all.
2. Each call is guarded on its own, so one sick connection cannot silently
   drop every OTHER connection's proposals.
"""

import unittest
from unittest import mock

from oikonome.jobs import worker

from .util import make_db


class NightlyPlaidRecurringTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.conn = make_db()
        cls.tid = cls.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        for iid, status in (("p-ok", "ok"), ("p-null", None),
                            ("p-login", "login_required"),
                            ("p-error", "error:ITEM_LOGIN_REQUIRED"),
                            ("p-archived", "archived"),
                            ("p-restored", "restored")):
            cls.conn.execute(
                "INSERT INTO items (id, aggregator, institution_name, "
                "access_token, status) VALUES (%s,'plaid',%s,'tok',%s) "
                "ON CONFLICT (tenant_id, id) DO UPDATE SET status=EXCLUDED.status",
                (iid, iid, status))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def _run(self, recurring):
        """nightly_tenant with everything but the cross-check stubbed out."""
        with mock.patch.object(worker.bills, "run",
                               return_value={"proposed_add": 0}), \
             mock.patch.object(worker.bills, "propose_from_plaid_streams",
                               return_value={"proposed": 0}) as propose, \
             mock.patch("oikonome.sync.plaid.recurring_streams",
                        side_effect=recurring), \
             mock.patch("oikonome.engine.amazon_match.run_match"), \
             mock.patch("oikonome.engine.merchant_dedup.apply"), \
             mock.patch("oikonome.engine.llm_categorize.run"):
            worker.nightly_tenant(self.tid)
        return propose

    def test_only_healthy_items_cost_a_plaid_call(self):
        seen = []

        def streams(conn, item_id):
            seen.append(item_id)
            return []
        self._run(streams)
        # a NULL status is a freshly linked item (the column defaults to
        # 'ok'); everything else has already failed or been retired
        self.assertEqual(["p-null", "p-ok"], sorted(seen))

    def test_one_items_failure_does_not_drop_the_tenants_other_items(self):
        def streams(conn, item_id):
            if item_id == "p-null":
                raise RuntimeError("plaid timed out")
            return [{"stream_id": "s1"}]
        propose = self._run(streams)
        propose.assert_called_once()
        self.assertEqual([{"stream_id": "s1"}], propose.call_args[0][1],
                         "the healthy item's streams must still be proposed")

    def test_the_billed_cross_check_asks_the_gate_first(self):
        """Recurring streams is a separately billed aggregator product,
        like liabilities and investments, so an operator's gate may
        withhold it; the household's own setting cannot turn it back on.
        With nothing installed tier_allows says yes, so a self-host keeps
        it."""
        seen = []

        def streams(conn, item_id):
            seen.append(item_id)
            return []
        asked = []

        def allows(conn, feature):
            asked.append(feature)
            return feature != "plaid_recurring"
        with mock.patch.object(worker.ext.gate, "tier_allows",
                               side_effect=allows):
            self._run(streams)
        self.assertIn("plaid_recurring", asked)
        self.assertEqual(seen, [])

    def test_a_failing_categorizer_does_not_end_the_tenants_night(self):
        """The categorizer run is one step of a tenant's night like the
        others: a model that is down must not skip the receipt pass, the
        receipt matching or the retraining behind it."""
        after = []
        with mock.patch.object(worker.bills, "run",
                               return_value={"proposed_add": 0}), \
             mock.patch("oikonome.sync.plaid.recurring_streams",
                        return_value=[]), \
             mock.patch("oikonome.engine.amazon_match.run_match"), \
             mock.patch("oikonome.engine.merchant_dedup.apply"), \
             mock.patch("oikonome.engine.llm_categorize.run",
                        side_effect=RuntimeError("model unreachable")), \
             mock.patch("oikonome.engine.receipts.parse_pending",
                        side_effect=lambda c: after.append("parse")), \
             mock.patch("oikonome.engine.receipt_match.match_waiting",
                        side_effect=lambda c: after.append("match")):
            worker.nightly_tenant(self.tid)
        self.assertEqual(after, ["parse", "match"])

    def test_a_gate_that_failed_to_load_refuses_it(self):
        from oikonome import ext
        self.assertFalse(ext._FailClosedGate().tier_allows(
            None, "plaid_recurring"))


if __name__ == "__main__":
    unittest.main()
