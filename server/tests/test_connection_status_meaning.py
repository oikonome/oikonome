"""A failing connection is not one kind of thing.

Read as one thing, every non-'ok' status reaches the clients the same way
— red, "sync failing", and a Link update-mode button. Plaid documents an
institution outage as transient and self-clearing; only a second group
of states needs the person to sign in again. Offering re-auth for a bank
that is merely down produces a button that appears to work, which is how
a warning stops meaning anything.
"""

import datetime as dt
import json
import unittest
import unittest.mock

import httpx

from oikonome.sync import base as sync_base
from oikonome.sync import plaid
from oikonome.web.pages import _connections

from .util import make_db, write_config

ACCOUNTS = {"accounts": [], "item": {"item_id": "it-p"}}
EMPTY_SYNC = {"added": [], "modified": [], "removed": [],
              "next_cursor": "c1", "has_more": False}


class StatusMeaningTests(unittest.TestCase):
    def test_an_institution_outage_is_retrying_not_broken(self):
        for code in ("INSTITUTION_DOWN", "INSTITUTION_NOT_RESPONDING",
                     "INSTITUTION_NOT_AVAILABLE", "PLANNED_MAINTENANCE"):
            self.assertEqual(plaid.status_kind(f"error:{code}"), "retrying",
                             code)

    def test_only_a_person_can_fix_these(self):
        for code in ("ITEM_LOGIN_REQUIRED", "PENDING_EXPIRATION",
                     "PENDING_DISCONNECT", "USER_PERMISSION_REVOKED"):
            self.assertEqual(plaid.status_kind(f"error:{code}"), "reauth",
                             code)

    def test_the_webhook_receivers_own_words_classify_too(self):
        """The receiver stores 'pending_expiration'/'revoked'/
        'new_accounts', not error codes — one function must read both
        vocabularies or the clients end up re-deriving it."""
        self.assertEqual(plaid.status_kind("pending_expiration"), "reauth")
        self.assertEqual(plaid.status_kind("pending_disconnect"), "reauth")
        self.assertEqual(plaid.status_kind("revoked"), "reauth")
        self.assertEqual(plaid.status_kind("new_accounts"), "reauth")

    def test_a_released_item_is_gone_not_fixable(self):
        self.assertEqual(plaid.status_kind("reaped"), "gone")
        self.assertEqual(plaid.status_kind("error:ITEM_NOT_FOUND"), "gone")

    def test_healthy_is_healthy_and_an_unknown_code_still_surfaces(self):
        self.assertEqual(plaid.status_kind(None), "ok")
        self.assertEqual(plaid.status_kind("ok"), "ok")
        # deliberately NOT 'ok': a code Plaid adds later must show up as
        # something rather than silently read as working
        self.assertEqual(plaid.status_kind("error:SOME_NEW_CODE"),
                         "attention")


class ConnectionsPayloadTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")
        sync_base.upsert_item(self.conn, "it-p", "plaid", "Demo",
                              "access-prod-abc")

    def tearDown(self):
        self.conn.close()

    def _kind(self):
        return [r for r in _connections(self.conn)
                if r["id"] == "it-p"][0]["status_kind"]

    def test_the_payload_carries_the_meaning_not_just_the_code(self):
        self.conn.execute("UPDATE items SET status='error:INSTITUTION_DOWN' "
                          "WHERE id='it-p'")
        self.assertEqual(self._kind(), "retrying")
        self.conn.execute("UPDATE items SET status='error:ITEM_LOGIN_REQUIRED' "
                          "WHERE id='it-p'")
        self.assertEqual(self._kind(), "reauth")


class InstitutionHealthTests(unittest.TestCase):
    """The dot is honest about the ITEM, but a bank whose own OAuth is
    degraded fleet-wide leaves a reconnect dying on the bank's error page
    with a green dot and no context. The hourly sync caches Plaid's
    institution login health on the item; the connections payload carries
    it only while fresh — a stale DEGRADED from a stopped sync would
    outlive the outage it described."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")
        sync_base.upsert_item(self.conn, "it-h", "plaid", "Demo", "tok-h")
        self.conn.execute(
            "UPDATE items SET institution_id='ins_1' WHERE id='it-h'")

    def tearDown(self):
        self.conn.close()

    def _record(self, status="DEGRADED"):
        def handler(request):
            if request.url.path == "/institutions/get_by_id":
                return httpx.Response(200, text=json.dumps(
                    {"institution": {"status": {
                        "item_logins": {"status": status}}}}))
            return httpx.Response(404, text="{}")
        client = plaid.Client("cid", "sec", plaid.ENV_URLS["sandbox"],
                              transport=httpx.MockTransport(handler))
        plaid.record_institution_health(self.conn, client, "it-h")

    def _payload(self):
        return [r for r in _connections(self.conn)
                if r["id"] == "it-h"][0]

    def test_degraded_health_reaches_the_payload(self):
        self._record("DEGRADED")
        self.assertEqual(self._payload()["institution_health"], "DEGRADED")
        # and the item's own meaning is untouched — the bank being
        # degraded for the fleet does not make THIS item broken
        self.assertEqual(self._payload()["status_kind"], "ok")

    def test_a_stale_reading_is_dropped(self):
        self._record("DEGRADED")
        self.conn.execute(
            """UPDATE items SET raw = raw || jsonb_build_object(
                   'institution_health', jsonb_build_object(
                       'logins', 'DEGRADED',
                       'as_of', (now() - interval '25 hours')::text))
               WHERE id='it-h'""")
        self.assertIsNone(self._payload()["institution_health"])

    def test_a_failed_read_never_fails_the_caller(self):
        def handler(request):
            return httpx.Response(500, text="{}")
        client = plaid.Client("cid", "sec", plaid.ENV_URLS["sandbox"],
                              transport=httpx.MockTransport(handler))
        plaid.record_institution_health(self.conn, client, "it-h")
        self.assertIsNone(self._payload()["institution_health"])

    def _counting_client(self, calls, status="HEALTHY"):
        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200, text=json.dumps(
                {"institution": {"status": {
                    "item_logins": {"status": status}}}}))
        return plaid.Client("cid", "sec", plaid.ENV_URLS["sandbox"],
                            transport=httpx.MockTransport(handler))

    def _age_the_reading(self, hours):
        stamp = (dt.datetime.now(dt.timezone.utc)
                 - dt.timedelta(hours=hours)).isoformat()
        self.conn.execute(
            """UPDATE items SET raw = raw || jsonb_build_object(
                   'institution_health',
                   (raw->'institution_health') || jsonb_build_object(
                       'as_of', %s::text))
               WHERE id='it-h'""", (stamp,))

    def test_a_fresh_reading_is_not_refetched_on_the_next_sync(self):
        """This hangs off the HOURLY sync but is only trusted for a day, so
        re-reading it every run buys nothing and spends one
        /institutions/get_by_id per item per hour against the same
        client_id rate limit the sweep's bucketing exists to protect. One
        local read of what is already stored decides."""
        calls = []
        client = self._counting_client(calls)
        for _ in range(4):
            plaid.record_institution_health(self.conn, client, "it-h")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self._payload()["institution_health"], "HEALTHY")

    def test_an_ageing_reading_is_refreshed_before_it_goes_stale(self):
        """The throttle must expire well inside the 24 h the display side
        stops trusting the value at, or the dot would go dark on the
        throttle rather than on the bank."""
        calls = []
        plaid.record_institution_health(
            self.conn, self._counting_client(calls), "it-h")
        self._age_the_reading(plaid.HEALTH_MAX_AGE_H + 1)
        plaid.record_institution_health(
            self.conn, self._counting_client(calls, "DEGRADED"), "it-h")
        self.assertEqual(len(calls), 2)
        self.assertEqual(self._payload()["institution_health"], "DEGRADED")
        self.assertLess(plaid.HEALTH_MAX_AGE_H, 24)

    def test_an_unreadable_stamp_refreshes_rather_than_sticking(self):
        """A bad stamp must fall to the API call, never wedge the throttle
        shut and freeze the reading for good."""
        calls = []
        plaid.record_institution_health(
            self.conn, self._counting_client(calls), "it-h")
        self.conn.execute(
            """UPDATE items SET raw = raw || jsonb_build_object(
                   'institution_health', jsonb_build_object(
                       'logins', 'HEALTHY', 'as_of', 'not a date'))
               WHERE id='it-h'""")
        plaid.record_institution_health(
            self.conn, self._counting_client(calls), "it-h")
        self.assertEqual(len(calls), 2)


def _transport(webhook=None, seen=None):
    def handler(request):
        path = request.url.path
        if path == "/accounts/get":
            return httpx.Response(200, text=json.dumps(ACCOUNTS))
        if path == "/transactions/sync":
            return httpx.Response(200, text=json.dumps(EMPTY_SYNC))
        if path == "/item/get":
            return httpx.Response(200, text=json.dumps(
                {"item": {"item_id": "it-p", "webhook": webhook},
                 "status": {"transactions": {}}}))
        if path == "/item/webhook/update":
            if seen is not None:
                seen.append(json.loads(request.content.decode())["webhook"])
            return httpx.Response(200, text=json.dumps({"item": {}}))
        return httpx.Response(404, text="{}")
    return httpx.MockTransport(handler)


class WebhookRegistrationTests(unittest.TestCase):
    """An Item's webhook is registered on the LINK TOKEN, so one linked
    before the deployment had a webhook URL never receives
    SYNC_UPDATES_AVAILABLE and silently falls back to the hourly poll
    forever. Nothing else in the system would ever notice."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")
        sync_base.upsert_item(self.conn, "it-p", "plaid", "Demo",
                              "access-prod-abc")

    def tearDown(self):
        self.conn.close()

    def test_an_item_with_no_webhook_gets_one_on_the_next_sync(self):
        seen = []
        with unittest.mock.patch.dict(
                "os.environ",
                {"OIKONOME_PLAID_WEBHOOK_URL": "https://x.test/api/plaid/webhook"}):
            plaid.sync(self.conn, "it-p",
                       transport=_transport(webhook=None, seen=seen))
        self.assertEqual(seen, ["https://x.test/api/plaid/webhook"])

    def test_an_item_already_pointed_at_us_is_left_alone(self):
        seen = []
        url = "https://x.test/api/plaid/webhook"
        with unittest.mock.patch.dict(
                "os.environ", {"OIKONOME_PLAID_WEBHOOK_URL": url}):
            plaid.sync(self.conn, "it-p",
                       transport=_transport(webhook=url, seen=seen))
        self.assertEqual(seen, [])

    def test_a_deployment_with_no_webhook_url_never_calls_the_endpoint(self):
        """Self-hosted instances have no public URL; they must not try to
        clear a registration they never made."""
        seen = []
        with unittest.mock.patch.dict("os.environ",
                                      {"OIKONOME_PLAID_WEBHOOK_URL": ""}):
            plaid.sync(self.conn, "it-p",
                       transport=_transport(webhook="https://old.test/hook",
                                            seen=seen))
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
