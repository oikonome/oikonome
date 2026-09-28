"""A NEW institution under an already-connected SimpleFIN bridge gets one
deep first pull.

One bridge token returns every institution the user added at
bridge.simplefin.org — a bank added there later simply appears in the next
routine payload, with no claim/reconnect on our side. If that first
appearance were fetched with the routine 30-day window, everything older
would never be fetched by anything, ever (net worth history and spend
totals silently truncated). So sync() re-fetches ONCE with the first-pull
window when the payload carries an org it has never seen; routine syncs of
known orgs keep the shallow window and make a single request.
"""

import datetime as dt
import json
import unittest

import httpx

from oikonome.sync import simplefin

from .util import make_db, write_config

ORG_A = {"name": "Demo Bank"}
ORG_B = {"name": "Late Added Bank"}


def _acct(acct_id: str, org: dict, txn_id: str, posted: int) -> dict:
    return {"id": acct_id, "name": f"{org['name']} Checking ({acct_id[-4:]})",
            "currency": "USD", "balance": "100.00", "org": org,
            "transactions": [
                {"id": txn_id, "posted": posted, "amount": "-5.00",
                 "description": "COFFEE", "payee": "Coffee"}]}


def _ts(day: dt.date) -> int:
    return int(dt.datetime.combine(day, dt.time.min,
                                   dt.timezone.utc).timestamp())


class _Bridge:
    """Mock bridge that records every requested start-date and can grow a
    second institution between calls."""

    def __init__(self):
        self.orgs = [ORG_A]
        self.requests: list[dt.date | None] = []

    def transport(self):
        def handler(request):
            raw = request.url.params.get("start-date")
            self.requests.append(
                dt.datetime.fromtimestamp(int(raw), dt.timezone.utc).date()
                if raw else None)
            accounts = []
            for i, org in enumerate(self.orgs):
                posted = _ts(dt.date.today() - dt.timedelta(days=60))
                accounts.append(_acct(f"acct-{i}00{i}", org,
                                      f"t{i}-{len(self.requests)}", posted))
            return httpx.Response(200, text=json.dumps(
                {"errors": [], "accounts": accounts}))
        return httpx.MockTransport(handler)


class FirstPullDepthTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.bridge = _Bridge()

    def tearDown(self):
        self.conn.close()

    def _sync(self, days: int):
        return simplefin.sync(
            self.conn, "sfin-demo", "https://u:p@bridge.test/simplefin",
            since=dt.date.today() - dt.timedelta(days=days),
            transport=self.bridge.transport())

    def test_claim_depth_pull_makes_a_single_request(self):
        # the claim-time call sites already ask for the first-pull window —
        # no re-fetch even though every org is brand new
        self._sync(simplefin.FIRST_PULL_DAYS)
        self.assertEqual(self.bridge.requests, [
            dt.date.today() - dt.timedelta(days=simplefin.FIRST_PULL_DAYS)])

    def test_routine_sync_of_known_orgs_stays_shallow(self):
        self._sync(simplefin.FIRST_PULL_DAYS)      # initial claim-depth pull
        self.bridge.requests.clear()
        self._sync(30)                             # routine hourly window
        self.assertEqual(self.bridge.requests,
                         [dt.date.today() - dt.timedelta(days=30)])

    def test_new_org_in_a_routine_sync_triggers_one_deep_refetch(self):
        self._sync(simplefin.FIRST_PULL_DAYS)      # bridge starts with org A
        self.bridge.orgs.append(ORG_B)             # user adds a bank later
        self.bridge.requests.clear()
        self._sync(30)
        # shallow probe first, then exactly one deep re-fetch
        self.assertEqual(self.bridge.requests, [
            dt.date.today() - dt.timedelta(days=30),
            dt.date.today() - dt.timedelta(days=simplefin.FIRST_PULL_DAYS)])
        # the new org's child item and its transactions actually landed
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM items WHERE aggregator='simplefin-org' "
            "AND institution_name=%s", (ORG_B["name"],)).fetchone())
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM transactions "
            "WHERE account_id LIKE 'acct-1%'").fetchone()["n"]
        self.assertGreater(n, 0)
        # and the pull after THAT is shallow again — the depth is one-time
        self.bridge.requests.clear()
        self._sync(30)
        self.assertEqual(self.bridge.requests,
                         [dt.date.today() - dt.timedelta(days=30)])

    def test_archived_org_does_not_retrigger_the_deep_pull(self):
        self._sync(simplefin.FIRST_PULL_DAYS)
        self.bridge.orgs.append(ORG_B)
        self._sync(30)                             # org B claimed deep once
        # user disconnects org B; its data keeps arriving in the payload
        self.conn.execute(
            "UPDATE items SET status='archived' WHERE "
            "aggregator='simplefin-org' AND institution_name=%s",
            (ORG_B["name"],))
        self.bridge.requests.clear()
        self._sync(30)
        self.assertEqual(self.bridge.requests,
                         [dt.date.today() - dt.timedelta(days=30)])


if __name__ == "__main__":
    unittest.main()
