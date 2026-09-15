"""The nightly Plaid recurring cross-check spends one Plaid call per item.

Two things follow from that, and neither was true:

1. Only HEALTHY items are worth the call. The hourly sync retries broken
   ones because retrying is how a re-auth or an outage is noticed to have
   cleared; this cross-check has no such duty, so an item parked in
   login_required or error:* was buying the same 400 every night, per
   item, forever — and a shell item restored from an export has no access
   token to call with at all.
2. One item's failure must not cost the tenant the whole pass. The calls
   ran unguarded in a single try, so one sick connection silently dropped
   every OTHER connection's proposals.
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


if __name__ == "__main__":
    unittest.main()
