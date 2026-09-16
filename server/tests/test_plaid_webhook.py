"""Plaid webhook receiver: JWT verification (real ES256 signatures against
a mocked /webhook_verification_key/get), SYNC_UPDATES_AVAILABLE enqueue,
ITEM status recording, and the link-token webhook-URL wiring."""

import base64
import hashlib
import json
import os
import time
import unittest
import uuid
from unittest import mock

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import \
    decode_dss_signature

from oikonome.sync import base as sync_base
from oikonome.sync import plaid
from oikonome.web import plaid_webhook

from .util import make_db, write_config

KID = "test-kid-1"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class _Signer:
    """A stand-in for Plaid's signing side: P-256 key, JWK, JOSE JWTs."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())

    def jwk(self, expired_at=None) -> dict:
        nums = self.key.public_key().public_numbers()
        return {"alg": "ES256", "crv": "P-256", "kty": "EC", "use": "sig",
                "kid": KID, "created_at": int(time.time()) - 1000,
                "expired_at": expired_at,
                "x": _b64url(nums.x.to_bytes(32, "big")),
                "y": _b64url(nums.y.to_bytes(32, "big"))}

    def jwt(self, body: bytes, *, alg="ES256", kid=KID, iat=None,
            sha=None) -> str:
        header = _b64url(json.dumps({"alg": alg, "kid": kid,
                                     "typ": "JWT"}).encode())
        claims = _b64url(json.dumps({
            "iat": int(time.time()) if iat is None else iat,
            "request_body_sha256": sha if sha is not None
            else hashlib.sha256(body).hexdigest()}).encode())
        signing_input = f"{header}.{claims}".encode()
        der = self.key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return f"{header}.{claims}.{_b64url(sig)}"


def _key_transport(jwk: dict, hits: list | None = None):
    def handler(request):
        if request.url.path == "/webhook_verification_key/get":
            if hits is not None:
                hits.append(json.loads(request.content.decode())["key_id"])
            return httpx.Response(200, text=json.dumps({"key": jwk}))
        return httpx.Response(404, text="{}")
    return httpx.MockTransport(handler)


class WebhookBase(unittest.TestCase):
    ENV = {"OIKONOME_PLAID_CLIENT_ID": "cid",
           "OIKONOME_PLAID_SECRET": "sec",
           "OIKONOME_PLAID_ENV": "sandbox"}

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        from oikonome.web.app import app
        cls.http = TestClient(app)

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in self.ENV}
        os.environ.update(self.ENV)
        self.signer = _Signer()
        plaid_webhook._KEY_CACHE.clear()
        plaid_webhook._NEG_CACHE.clear()
        plaid_webhook._TRANSPORT = _key_transport(self.signer.jwk())
        from oikonome.web import security
        security._limiter._hits.clear()

    def tearDown(self):
        plaid_webhook._TRANSPORT = None
        plaid_webhook._KEY_CACHE.clear()
        plaid_webhook._NEG_CACHE.clear()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def post(self, body: bytes, jwt: str | None = None):
        headers = {}
        if jwt is not None:
            headers["Plaid-Verification"] = jwt
        return self.http.post("/api/plaid/webhook", content=body,
                              headers=headers)


class VerificationTests(WebhookBase):
    BODY = json.dumps({"webhook_type": "TRANSACTIONS",
                       "webhook_code": "SYNC_UPDATES_AVAILABLE",
                       "item_id": "no-such-item"}).encode()

    def test_valid_signature_accepted(self):
        r = self.post(self.BODY, self.signer.jwt(self.BODY))
        self.assertEqual(r.status_code, 200)

    def test_missing_header_401(self):
        self.assertEqual(self.post(self.BODY).status_code, 401)

    def test_forged_key_401(self):
        """JWT signed by a DIFFERENT key than the one Plaid serves."""
        forger = _Signer()
        r = self.post(self.BODY, forger.jwt(self.BODY))
        self.assertEqual(r.status_code, 401)

    def test_alg_confusion_401(self):
        r = self.post(self.BODY, self.signer.jwt(self.BODY, alg="HS256"))
        self.assertEqual(r.status_code, 401)
        r = self.post(self.BODY, self.signer.jwt(self.BODY, alg="none"))
        self.assertEqual(r.status_code, 401)

    def test_stale_iat_401(self):
        r = self.post(self.BODY, self.signer.jwt(
            self.BODY, iat=int(time.time()) - 600))
        self.assertEqual(r.status_code, 401)

    def test_tampered_body_401(self):
        """Signature valid for the ORIGINAL body; a different body must
        fail the request_body_sha256 claim check."""
        jwt = self.signer.jwt(self.BODY)
        tampered = self.BODY.replace(b"no-such-item", b"another-item")
        self.assertEqual(self.post(tampered, jwt).status_code, 401)

    def test_expired_key_401(self):
        plaid_webhook._TRANSPORT = _key_transport(
            self.signer.jwk(expired_at=int(time.time()) - 10))
        r = self.post(self.BODY, self.signer.jwt(self.BODY))
        self.assertEqual(r.status_code, 401)

    def test_key_cached_across_requests(self):
        hits: list = []
        plaid_webhook._TRANSPORT = _key_transport(self.signer.jwk(), hits)
        self.post(self.BODY, self.signer.jwt(self.BODY))
        self.post(self.BODY, self.signer.jwt(self.BODY))
        self.assertEqual(hits, [KID])            # one fetch, then cache

    def test_unknown_kid_negative_cached_no_refetch(self):
        """An attacker spraying novel `kid`s must not turn each request
        into a fresh outbound fetch to Plaid. The first unknown kid fetches
        once (and fails 401); the second is absorbed by the negative cache."""
        hits: list = []

        def handler(request):
            if request.url.path == "/webhook_verification_key/get":
                kid = json.loads(request.content.decode())["key_id"]
                hits.append(kid)
                jwk = self.signer.jwk() if kid == KID else None
                return httpx.Response(200, text=json.dumps({"key": jwk}))
            return httpx.Response(404, text="{}")
        plaid_webhook._TRANSPORT = httpx.MockTransport(handler)
        bogus = self.signer.jwt(self.BODY, kid="bogus-kid-xyz")
        self.assertEqual(self.post(self.BODY, bogus).status_code, 401)
        self.assertEqual(self.post(self.BODY, bogus).status_code, 401)
        self.assertEqual(hits, ["bogus-kid-xyz"])   # one fetch, then negative

    def test_negative_cache_expires(self):
        """The negative cache is short — a kid that becomes resolvable is
        picked up once the TTL passes (no permanent poisoning)."""
        hits: list = []
        plaid_webhook._TRANSPORT = _key_transport(self.signer.jwk(), hits)
        # seed a stale negative entry for KID
        plaid_webhook._NEG_CACHE[KID] = time.monotonic() - 1
        r = self.post(self.BODY, self.signer.jwt(self.BODY))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(hits, [KID])                # expired neg → refetched

    def test_verify_is_async(self):
        import inspect
        self.assertTrue(inspect.iscoroutinefunction(plaid_webhook.verify))

    def test_no_platform_creds_401(self):
        os.environ.pop("OIKONOME_PLAID_CLIENT_ID")
        r = self.post(self.BODY, self.signer.jwt(self.BODY))
        self.assertEqual(r.status_code, 401)

    def test_oversized_body_413(self):
        r = self.post(b"x" * (plaid_webhook._MAX_BODY + 1))
        self.assertEqual(r.status_code, 413)


class EventTests(WebhookBase):
    def setUp(self):
        super().setUp()
        self.conn = make_db()
        self.tenant_id = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        # item ids are globally unique at Plaid; the shared test DB keeps
        # rows from past runs, so a fixed id would resolve to an old tenant
        self.item_id = f"pl-wh-{uuid.uuid4().hex[:10]}"
        sync_base.upsert_item(self.conn, self.item_id, "plaid", "Demo Bank",
                              "access-tok")

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def _post_event(self, event: dict):
        body = json.dumps(event).encode()
        return self.post(body, self.signer.jwt(body))

    def test_sync_updates_available_enqueues_scoped_sync(self):
        calls = []

        async def fake_enqueue(tenant_id, item_id):
            calls.append((tenant_id, item_id))
            return True
        with mock.patch.object(plaid_webhook, "_enqueue_sync", fake_enqueue):
            r = self._post_event({"webhook_type": "TRANSACTIONS",
                                  "webhook_code": "SYNC_UPDATES_AVAILABLE",
                                  "item_id": self.item_id})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["disposition"], "sync-enqueued")
        self.assertEqual(calls, [(self.tenant_id, self.item_id)])

    def test_unknown_item_is_acknowledged_not_synced(self):
        calls = []

        async def fake_enqueue(tenant_id, item_id):
            calls.append((tenant_id, item_id))
            return True
        with mock.patch.object(plaid_webhook, "_enqueue_sync", fake_enqueue):
            r = self._post_event({"webhook_type": "TRANSACTIONS",
                                  "webhook_code": "SYNC_UPDATES_AVAILABLE",
                                  "item_id": "never-linked"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["disposition"], "unknown-item")
        self.assertEqual(calls, [])

    def _webhook_status(self):
        return self.conn.execute(
            "SELECT webhook_status, webhook_status_at FROM items "
            "WHERE id=%s", (self.item_id,)).fetchone()

    def test_item_error_recorded(self):
        r = self._post_event({
            "webhook_type": "ITEM", "webhook_code": "ERROR",
            "item_id": self.item_id,
            "error": {"error_type": "ITEM_ERROR",
                      "error_code": "ITEM_LOGIN_REQUIRED"}})
        self.assertEqual(r.json()["disposition"], "status-recorded")
        row = self._webhook_status()
        self.assertEqual(row["webhook_status"], "error:ITEM_LOGIN_REQUIRED")
        self.assertIsNotNone(row["webhook_status_at"])

    def test_pending_expiration_and_revoked_recorded(self):
        self._post_event({"webhook_type": "ITEM",
                          "webhook_code": "PENDING_EXPIRATION",
                          "item_id": self.item_id})
        self.assertEqual(self._webhook_status()["webhook_status"],
                         "pending_expiration")
        self._post_event({"webhook_type": "ITEM",
                          "webhook_code": "USER_PERMISSION_REVOKED",
                          "item_id": self.item_id})
        self.assertEqual(self._webhook_status()["webhook_status"], "revoked")

    def test_new_accounts_available_is_recorded(self):
        """The bank saying this household opened an account it has not
        shared. Ignoring it is why a newly-opened account would simply
        never appear, with nothing on screen suggesting we knew."""
        r = self._post_event({"webhook_type": "ITEM",
                              "webhook_code": "NEW_ACCOUNTS_AVAILABLE",
                              "item_id": self.item_id})
        self.assertEqual(r.json()["disposition"], "status-recorded")
        self.assertEqual(self._webhook_status()["webhook_status"],
                         "new_accounts")

    def _restored_shell(self):
        """Another household holding a restored copy of this item: same
        Plaid item id, no token, status 'restored'."""
        other = make_db()
        sync_base.upsert_item(other, self.item_id, "plaid", "Demo Bank", None)
        other.execute("UPDATE items SET status='restored' WHERE id=%s",
                      (self.item_id,))
        return other

    def test_an_item_event_touches_only_the_live_households_row(self):
        """A restore copies a Plaid item id into another household. The
        webhook runs without RLS, so an event about the live connection
        must not stamp — or, on LOGIN_REPAIRED, reset to 'ok' — the copy."""
        other = self._restored_shell()
        try:
            self._post_event({
                "webhook_type": "ITEM", "webhook_code": "ERROR",
                "item_id": self.item_id,
                "error": {"error_code": "ITEM_LOGIN_REQUIRED"}})
            self._post_event({"webhook_type": "ITEM",
                              "webhook_code": "LOGIN_REPAIRED",
                              "item_id": self.item_id})
            shell = other.execute(
                "SELECT status, webhook_status FROM items WHERE id=%s",
                (self.item_id,)).fetchone()
            self.assertEqual(shell["status"], "restored")
            self.assertIsNone(shell["webhook_status"])
        finally:
            other.close()

    def test_a_sync_event_is_queued_for_the_live_household(self):
        other = self._restored_shell()
        calls = []

        async def fake_enqueue(tenant_id, item_id):
            calls.append((tenant_id, item_id))
            return True
        try:
            with mock.patch.object(plaid_webhook, "_enqueue_sync",
                                   fake_enqueue):
                self._post_event({"webhook_type": "TRANSACTIONS",
                                  "webhook_code": "SYNC_UPDATES_AVAILABLE",
                                  "item_id": self.item_id})
            self.assertEqual(calls, [(self.tenant_id, self.item_id)])
        finally:
            other.close()

    def test_pending_disconnect_is_recorded(self):
        """PENDING_EXPIRATION's sibling: update mode PREVENTS the
        disconnect, so a warning that only arrives afterwards is no
        warning at all."""
        self._post_event({"webhook_type": "ITEM",
                          "webhook_code": "PENDING_DISCONNECT",
                          "item_id": self.item_id})
        self.assertEqual(self._webhook_status()["webhook_status"],
                         "pending_disconnect")

    def test_login_repaired_stops_the_reauth_prompt(self):
        """An Item can heal without the person doing anything. Plaid says
        so explicitly, and the app must stop demanding a sign-in that is no
        longer needed."""
        self.conn.execute(
            "UPDATE items SET status='error:ITEM_LOGIN_REQUIRED', "
            "webhook_status='error:ITEM_LOGIN_REQUIRED' WHERE id=%s",
            (self.item_id,))
        r = self._post_event({"webhook_type": "ITEM",
                              "webhook_code": "LOGIN_REPAIRED",
                              "item_id": self.item_id})
        self.assertEqual(r.json()["disposition"], "status-cleared")
        row = self.conn.execute(
            "SELECT status, webhook_status FROM items WHERE id=%s",
            (self.item_id,)).fetchone()
        self.assertEqual(row["status"], "ok")
        self.assertIsNone(row["webhook_status"])

    def test_login_repaired_never_revives_a_disconnected_item(self):
        """A late heal must not undo a disconnect that already released
        the connection."""
        self.conn.execute("UPDATE items SET status='archived' WHERE id=%s",
                          (self.item_id,))
        self._post_event({"webhook_type": "ITEM",
                          "webhook_code": "LOGIN_REPAIRED",
                          "item_id": self.item_id})
        self.assertEqual(self.conn.execute(
            "SELECT status FROM items WHERE id=%s",
            (self.item_id,)).fetchone()["status"], "archived")

    def test_archived_item_webhook_not_synced(self):
        # post-disconnect race: a late
        # SYNC_UPDATES_AVAILABLE for a released Item must not enqueue a
        # sync — ITEM_NOT_FOUND would overwrite status='archived'
        self.conn.execute("UPDATE items SET status='archived' WHERE id=%s",
                          (self.item_id,))
        calls = []

        async def fake_enqueue(tenant_id, item_id):
            calls.append((tenant_id, item_id))
            return True
        with mock.patch.object(plaid_webhook, "_enqueue_sync", fake_enqueue):
            r = self._post_event({"webhook_type": "TRANSACTIONS",
                                  "webhook_code": "SYNC_UPDATES_AVAILABLE",
                                  "item_id": self.item_id})
        self.assertEqual(r.json()["disposition"], "unknown-item")
        self.assertEqual(calls, [])

    def test_sync_error_never_clobbers_archived(self):
        # the other half of the race: a sync already past the receiver's
        # filter (or the archive landing mid-sync) must leave 'archived'
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")
        self.conn.execute("UPDATE items SET status='archived' WHERE id=%s",
                          (self.item_id,))

        def handler(request):
            return httpx.Response(400, text=json.dumps(
                {"error_code": "ITEM_NOT_FOUND", "error_type": "ITEM_ERROR",
                 "error_message": "removed"}))
        with self.assertRaises(plaid.PlaidError):
            plaid.sync(self.conn, self.item_id,
                       transport=httpx.MockTransport(handler))
        self.assertEqual(self.conn.execute(
            "SELECT status FROM items WHERE id=%s",
            (self.item_id,)).fetchone()["status"], "archived")

    def test_enqueued_job_skips_archived_item(self):
        # a sync_item job that was queued BEFORE the disconnect archives
        # the item runs after — the worker body must skip it
        from oikonome.jobs import worker as worker_mod
        self.conn.execute("UPDATE items SET status='archived' WHERE id=%s",
                          (self.item_id,))
        r = worker_mod._sync_item_body(self.tenant_id, self.item_id)
        self.assertEqual(r, {"skipped": "archived"})

    def test_clean_sync_clears_webhook_status(self):
        self._post_event({"webhook_type": "ITEM",
                          "webhook_code": "PENDING_EXPIRATION",
                          "item_id": self.item_id})
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")
        page = {"added": [], "modified": [], "removed": [],
                "has_more": False, "next_cursor": "c-1"}

        def handler(request):
            if request.url.path == "/accounts/get":
                return httpx.Response(200, text=json.dumps({"accounts": []}))
            return httpx.Response(200, text=json.dumps(page))
        plaid.sync(self.conn, self.item_id,
                   transport=httpx.MockTransport(handler))
        row = self._webhook_status()
        self.assertIsNone(row["webhook_status"])
        self.assertIsNone(row["webhook_status_at"])

    def test_unhandled_codes_acknowledged(self):
        """A code we deliberately do not act on is still a 200 — Plaid
        retries anything else, and a retry storm for an event that needs
        no action is worse than the event. WEBHOOK_UPDATE_ACKNOWLEDGED is
        Plaid's own confirmation that a webhook URL change landed; its
        documented handling is "no action needed". Ignoring a code that
        DOES need action is a different thing entirely — see
        NEW_ACCOUNTS_AVAILABLE, handled above."""
        r = self._post_event({"webhook_type": "ITEM",
                              "webhook_code": "WEBHOOK_UPDATE_ACKNOWLEDGED",
                              "item_id": self.item_id})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["disposition"], "ignored")
        self.assertIsNone(self._webhook_status()["webhook_status"])


class LinkTokenWebhookWiringTests(unittest.TestCase):
    def _create_body(self) -> dict:
        captured: dict = {}

        def handler(request):
            captured.update(json.loads(request.content.decode()))
            return httpx.Response(200, text=json.dumps(
                {"link_token": "lt", "hosted_link_url": "https://h"}))
        client = plaid.Client("cid", "sec", plaid.ENV_URLS["sandbox"],
                              transport=httpx.MockTransport(handler))
        client.create_hosted_link("t-1", client_name="Oikonome")
        return captured

    def test_webhook_url_env_wires_into_link_token(self):
        with mock.patch.dict(os.environ, {
                "OIKONOME_PLAID_WEBHOOK_URL":
                "https://app.example.com/api/plaid/webhook"}):
            body = self._create_body()
        self.assertEqual(body["webhook"],
                         "https://app.example.com/api/plaid/webhook")

    def test_no_env_no_webhook_param(self):
        env = {k: v for k, v in os.environ.items()
               if k != "OIKONOME_PLAID_WEBHOOK_URL"}
        with mock.patch.dict(os.environ, env, clear=True):
            body = self._create_body()
        self.assertNotIn("webhook", body)


if __name__ == "__main__":
    unittest.main()
