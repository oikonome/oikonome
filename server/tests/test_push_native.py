"""Native push: the device registers its platform token, the channel
forwards tickles through the relay and marks the tokens the relay
reports dead, and the relay itself accepts only the strict payload
shape. The payload's content is bounded and asserted, not assumed: the
daily push carries exactly one display line (the verdict), every other
push carries none, and an operator can switch the line off — anything
wider would turn the relay into a financial-data processor."""

import collections
import json
import os
import time
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PASSWORD = "correct-horse-battery"


class _RelayCapture:
    """Stands in for httpx.post at the channel's seam."""

    def __init__(self, response: dict, status: int = 200):
        self.response = response
        self.status = status
        self.calls = []

    def __call__(self, url, json=None, timeout=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers or {}})
        r = mock.Mock()
        r.status_code = self.status
        r.json = lambda: self.response
        r.text = str(self.response)
        return r


class PushRegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.email = f"push-{uuid.uuid4().hex[:8]}@example.dev"
        cls.client.post("/api/signup", data={
            "email": cls.email, "password": PASSWORD})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]

    def _device(self, name="push phone"):
        minted = self.client.post("/api/devices", json={
            "device_name": name, "platform": "android"}).json()
        return minted, TestClient(self.client.app), {
            "Authorization": f"Bearer {minted['token']}"}

    def test_device_registers_replaces_and_clears_its_push_token(self):
        minted, bare, hdr = self._device()
        r = bare.post("/api/devices/push", headers=hdr,
                      json={"token": "fcm-token-" + "x" * 20,
                            "platform": "fcm"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["registered"])
        conn = tenancy.control_connect()
        try:
            from oikonome.auth import device_tokens as dt_mod
            targets = dt_mod.push_targets(conn, self.tid)
            mine = [t for t in targets
                    if t["push_token"].startswith("fcm-token-")]
            self.assertEqual(len(mine), 1)
            self.assertEqual(mine[0]["push_platform"], "fcm")
        finally:
            conn.close()
        # re-register replaces rather than accumulating
        bare.post("/api/devices/push", headers=hdr,
                  json={"token": "fcm-token-" + "y" * 20, "platform": "fcm"})
        conn = tenancy.control_connect()
        try:
            from oikonome.auth import device_tokens as dt_mod
            mine = [t for t in dt_mod.push_targets(conn, self.tid)
                    if t["push_token"].startswith("fcm-token-")]
            self.assertEqual(len(mine), 1)
            self.assertIn("y" * 20, mine[0]["push_token"])
        finally:
            conn.close()
        # {"token": null} unregisters
        r = bare.post("/api/devices/push", headers=hdr, json={"token": None})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["registered"])

    def test_only_a_device_may_register_and_garbage_is_rejected(self):
        r = self.client.post("/api/devices/push", json={
            "token": "t" * 32, "platform": "fcm"})
        self.assertEqual(r.status_code, 403)     # web session — no device row
        minted, bare, hdr = self._device()
        for bad in ({"token": "short", "platform": "fcm"},
                    {"token": "t" * 32, "platform": "gcm"},
                    {"token": "t" * 5000, "platform": "fcm"},
                    {"token": 42, "platform": "fcm"}):
            r = bare.post("/api/devices/push", headers=hdr, json=bad)
            self.assertEqual(r.status_code, 400, bad)

    def test_push_state_is_visible_to_the_web_settings_surfaces(self):
        """The web app can only offer a native test-push (and explain a
        broken one) if the devices list and the notify status expose the
        registration — a phone-only household must not be blind."""
        minted, bare, hdr = self._device("visible")
        token = "visible-token-" + "v" * 20
        bare.post("/api/devices/push", headers=hdr,
                  json={"token": token, "platform": "fcm"})
        listed = self.client.get("/api/devices").json()["devices"]
        mine = [d for d in listed if d["id"] == minted["id"]][0]
        self.assertEqual(mine["push"], "on")
        self.assertGreaterEqual(
            self.client.get("/api/notify").json()["native_push_devices"], 1)
        conn = tenancy.control_connect()
        try:
            from oikonome.auth import device_tokens as dt_mod
            dt_mod.mark_push_dead(conn, [token])
            conn.commit()
        finally:
            conn.close()
        listed = self.client.get("/api/devices").json()["devices"]
        mine = [d for d in listed if d["id"] == minted["id"]][0]
        self.assertEqual(mine["push"], "dead")

    def test_apns_only_registration_gets_a_truthful_test_error(self):
        """An iOS phone with a live registration must be told the APNs
        leg isn't live — not that it needs to sign in (it already did)."""
        minted, bare, hdr = self._device("iphone-ish")
        # a plausibly real token: the endpoint enforces the APNs hex shape
        r = bare.post("/api/devices/push", headers=hdr,
                      json={"token": "ab12" * 8, "platform": "apns"})
        self.assertEqual(r.status_code, 200, r.text)
        relay = _RelayCapture({"sent": 0, "dead": [],
                               "unavailable": ["apns"]})
        with mock.patch.dict(os.environ, {
                "OIKONOME_PUSH_RELAY_URL": "https://relay.example"}), \
             mock.patch("httpx.post", relay):
            r = self.client.post("/api/notify/test",
                                 json={"channel": "push"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("apns", r.json()["detail"])
        self.assertNotIn("sign in", r.json()["detail"])
        # cleanup: clear the push registration for other tests
        bare.post("/api/devices/push", headers=hdr, json={"token": None})

    def test_revoking_the_device_silences_its_push(self):
        minted, bare, hdr = self._device("doomed")
        bare.post("/api/devices/push", headers=hdr,
                  json={"token": "doomed-token-" + "z" * 20,
                        "platform": "fcm"})
        self.client.post("/api/devices/revoke",
                         json={"id": minted["id"], "password": PASSWORD})
        conn = tenancy.control_connect()
        try:
            from oikonome.auth import device_tokens as dt_mod
            tokens = [t["push_token"]
                      for t in dt_mod.push_targets(conn, self.tid)]
            self.assertFalse([t for t in tokens if t.startswith("doomed-")])
        finally:
            conn.close()


class PushNativeChannelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()
        from oikonome.db import tenancy as _t
        conn = _t.admin_connect()
        try:
            cls.tid = str(uuid.uuid4())
            conn.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)",
                         (cls.tid, "push-channel-test"))
            cls.uid = conn.execute(
                """INSERT INTO users (tenant_id, email, password_hash, role)
                   VALUES (%s, %s, 'x', 'owner') RETURNING id""",
                (cls.tid, f"pc-{uuid.uuid4().hex[:8]}@example.dev"),
                ).fetchone()["id"]
        finally:
            conn.close()

    def _add_device_with_push(self, conn, token):
        from oikonome.auth import device_tokens as dt_mod
        _, row = dt_mod.mint(conn, self.uid, self.tid, "chan test")
        dt_mod.set_push(conn, self.uid, row["id"], token, "fcm")
        return row

    def test_unconfigured_is_a_quiet_noop(self):
        from oikonome.notify import push_native
        with mock.patch.dict(os.environ, {"OIKONOME_PUSH_RELAY_URL": ""}):
            self.assertFalse(push_native.available())
            self.assertIn("relay", push_native.unavailable_reason())
            conn = tenancy.control_connect()
            try:
                self.assertEqual(
                    push_native.send_tenant(conn, self.tid, "daily"), 0)
            finally:
                conn.close()

    def test_unconfigured_with_a_registered_device_says_why(self):
        """A phone with a registered token on a relay-less install delivers
        0, and an empty detail there has the test-push door tell the user to
        sign in from the mobile app they are already holding. The detail
        must name the true cause: no relay configured."""
        from oikonome.notify import push_native
        conn = tenancy.control_connect()
        try:
            self._add_device_with_push(conn, f"unconf-{uuid.uuid4().hex}")
            detail: dict = {}
            with mock.patch.dict(os.environ,
                                 {"OIKONOME_PUSH_RELAY_URL": ""}):
                n = push_native.send_tenant(conn, self.tid, "test",
                                            detail=detail)
            self.assertEqual(n, 0)
            self.assertTrue(detail.get("unconfigured"))
            self.assertGreaterEqual(detail.get("targets", 0), 1)
        finally:
            conn.close()

    def test_payload_is_routing_plus_one_line_and_dead_tokens_are_marked(self):
        from oikonome.notify import push_native
        conn = tenancy.control_connect()
        try:
            live = f"live-{uuid.uuid4().hex}"
            dying = f"dying-{uuid.uuid4().hex}"
            self._add_device_with_push(conn, live)
            self._add_device_with_push(conn, dying)
            capture = _RelayCapture({"sent": 1, "dead": [dying],
                                     "unavailable": []})
            with mock.patch.dict(os.environ, {
                    "OIKONOME_PUSH_RELAY_URL": "https://relay.example",
                    "OIKONOME_PUSH_CONTENT_FREE": ""}), \
                 mock.patch("httpx.post", capture):
                n = push_native.send_tenant(
                    conn, self.tid, "daily", badge=3,
                    text="OVER budget by $120.\nFood +$40 vs pace.")
            self.assertEqual(n, 1)
            self.assertEqual(len(capture.calls), 1)
            call = capture.calls[0]
            self.assertEqual(call["url"], "https://relay.example/v1/push")
            for item in call["json"]["notifications"]:
                # routing + kind + badge + the ONE display line, nothing else
                self.assertEqual(sorted(item), ["badge", "event",
                                                "platform", "text", "token"])
                self.assertEqual(item["event"], "daily")
                # flattened to one line, verdict first
                self.assertEqual(item["text"],
                                 "OVER budget by $120. Food +$40 vs pace.")
            # the relay's dead verdict landed on the row
            from oikonome.auth import device_tokens as dt_mod
            tokens = [t["push_token"]
                      for t in dt_mod.push_targets(conn, self.tid)]
            self.assertIn(live, tokens)
            self.assertNotIn(dying, tokens)
        finally:
            conn.close()

    def test_only_the_daily_push_carries_text_and_the_knob_removes_it(self):
        """Every other event stays routing-only even if a caller hands it
        text, a line is cut to the relay's ceiling, and
        OIKONOME_PUSH_CONTENT_FREE=1 restores the routing-only daily."""
        from oikonome.notify import push_native
        conn = tenancy.control_connect()
        try:
            self._add_device_with_push(conn, f"t-{uuid.uuid4().hex}")

            def sent(event, text, **env):
                capture = _RelayCapture({"sent": 1, "dead": [],
                                         "unavailable": []})
                with mock.patch.dict(os.environ, {
                        "OIKONOME_PUSH_RELAY_URL": "https://relay.example",
                        "OIKONOME_PUSH_CONTENT_FREE": "", **env}), \
                     mock.patch("httpx.post", capture):
                    push_native.send_tenant(conn, self.tid, event, text=text)
                return capture.calls[0]["json"]["notifications"][0]

            self.assertNotIn("text", sent("weekly", "UNDER budget"))
            self.assertNotIn("text", sent("alert", "Rent is late"))
            self.assertNotIn("text", sent("daily", "OVER budget by $9",
                                          OIKONOME_PUSH_CONTENT_FREE="1"))
            self.assertNotIn("text", sent("daily", "   "))
            long = "x" * 500
            self.assertEqual(len(sent("daily", long)["text"]),
                             push_native.MAX_TEXT_LEN)
        finally:
            conn.close()

    def test_instance_key_travels_in_the_relay_header_only_when_set(self):
        """A keyed relay refuses unkeyed pushes, so the instance's key
        rides along as X-Relay-Key; with no key configured the header is
        absent rather than empty (a self-run open relay needs none)."""
        from oikonome.notify import push_native
        conn = tenancy.control_connect()
        try:
            self._add_device_with_push(conn, f"live-{uuid.uuid4().hex}")
            capture = _RelayCapture({"sent": 1, "dead": [],
                                     "unavailable": []})
            with mock.patch.dict(os.environ, {
                    "OIKONOME_PUSH_RELAY_URL": "https://relay.example",
                    "OIKONOME_PUSH_RELAY_KEY": "instance-key-1"}), \
                 mock.patch("httpx.post", capture):
                push_native.send_tenant(conn, self.tid, "daily")
            self.assertEqual(capture.calls[-1]["headers"],
                             {"X-Relay-Key": "instance-key-1"})
            with mock.patch.dict(os.environ, {
                    "OIKONOME_PUSH_RELAY_URL": "https://relay.example",
                    "OIKONOME_PUSH_RELAY_KEY": ""}), \
                 mock.patch("httpx.post", capture):
                push_native.send_tenant(conn, self.tid, "daily")
            self.assertEqual(capture.calls[-1]["headers"], {})
        finally:
            conn.close()

    def test_wrong_shaped_relay_response_never_raises(self):
        """send_tenant's contract is log-and-never-raise; the relay is an
        external service, so a garbage 200 body (a JSON array, a bare
        string, wrong field types) must degrade exactly like an
        unreachable relay — not crash the notification sweep or 500 the
        test-push endpoint with an AttributeError."""
        from oikonome.notify import push_native
        conn = tenancy.control_connect()
        try:
            self._add_device_with_push(conn, f"shape-{uuid.uuid4().hex}")
            for garbage in ([1, 2, 3], "ok", 42,
                            {"sent": "many", "dead": "not-a-list",
                             "unavailable": 7}):
                detail: dict = {}
                capture = _RelayCapture(garbage)
                with mock.patch.dict(os.environ, {
                        "OIKONOME_PUSH_RELAY_URL": "https://relay.example"}), \
                     mock.patch("httpx.post", capture):
                    n = push_native.send_tenant(conn, self.tid, "daily",
                                                detail=detail)
                self.assertEqual(n, 0, repr(garbage))
                # detail's unavailable list must stay a list of strings
                self.assertEqual(detail.get("unavailable", []), [],
                                 repr(garbage))
            # a string "dead" value must never be iterated into
            # single-character tokens and mark random rows dead
            from oikonome.auth import device_tokens as dt_mod
            self.assertTrue(dt_mod.push_targets(conn, self.tid))
        finally:
            conn.close()

    def test_relay_failure_never_raises(self):
        from oikonome.notify import push_native
        conn = tenancy.control_connect()
        try:
            self._add_device_with_push(conn, f"t-{uuid.uuid4().hex}")

            def boom(*a, **k):
                raise OSError("connection refused")
            with mock.patch.dict(os.environ, {
                    "OIKONOME_PUSH_RELAY_URL": "https://relay.example"}), \
                 mock.patch("httpx.post", boom):
                self.assertEqual(
                    push_native.send_tenant(conn, self.tid, "daily"), 0)
        finally:
            conn.close()

    def test_unknown_event_is_refused_before_any_network(self):
        from oikonome.notify import push_native
        conn = tenancy.control_connect()
        try:
            capture = _RelayCapture({"sent": 0, "dead": []})
            with mock.patch.dict(os.environ, {
                    "OIKONOME_PUSH_RELAY_URL": "https://relay.example"}), \
                 mock.patch("httpx.post", capture):
                n = push_native.send_tenant(
                    conn, self.tid, "you are over budget at Corner Bistro")
            self.assertEqual(n, 0)
            self.assertEqual(capture.calls, [])
        finally:
            conn.close()


class RelayAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from oikonome.relay.app import app
        cls.client = TestClient(app)

    def setUp(self):
        from oikonome.relay import app as relay_app
        relay_app._hits.clear()
        # module-global like _hits: a token registered to one test's key
        # would refuse the next test's key (see test_relay_token_binding)
        relay_app._owners.clear()

    def _one(self, **over):
        item = {"token": "t" * 32, "platform": "fcm", "event": "daily"}
        item.update(over)
        return {"notifications": [item]}

    def test_healthz_is_liveness_only(self):
        """Which legs hold credentials is the operator's business; the
        health check must not tell whoever can reach it."""
        with mock.patch("oikonome.relay.apns.available", return_value=True):
            r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True})

    def test_keyed_relay_refuses_pushes_without_its_key(self):
        """The relay fronts the publisher's APNs/FCM credentials. Once it
        has instance keys, a push without one (or with a wrong one) is
        refused before any leg is touched — the structural limits alone
        leave it a firehose for anyone holding device tokens."""
        with mock.patch.dict(os.environ, {"OIKONOME_RELAY_KEYS":
                                          "k-one, k-two"}), \
             mock.patch("oikonome.relay.fcm.available", return_value=True), \
             mock.patch("oikonome.relay.fcm.send", return_value="ok") as snd:
            for hdrs in ({}, {"X-Relay-Key": ""}, {"X-Relay-Key": "k-on"},
                         {"X-Relay-Key": "k-onee"}):
                r = self.client.post("/v1/push", json=self._one(),
                                     headers=hdrs)
                self.assertEqual(r.status_code, 401, hdrs)
                snd.assert_not_called()
            # either configured key opens the door — each with its own
            # instance's tokens, since a token is bound to the key that
            # registered it (test_relay_token_binding)
            for key in ("k-one", "k-two"):
                r = self.client.post(
                    "/v1/push", json=self._one(token=key[-3:] * 11),
                    headers={"X-Relay-Key": key})
                self.assertEqual(r.status_code, 200, key)
            self.assertEqual(snd.call_count, 2)

    def test_relay_without_keys_stays_open(self):
        # a self-run relay with no key configured keeps working unkeyed
        with mock.patch.dict(os.environ, {"OIKONOME_RELAY_KEYS": ""}), \
             mock.patch("oikonome.relay.fcm.available", return_value=True), \
             mock.patch("oikonome.relay.fcm.send", return_value="ok"):
            r = self.client.post("/v1/push", json=self._one())
        self.assertEqual(r.status_code, 200)

    def test_open_relay_says_so_once_at_startup(self):
        from oikonome.relay.app import app
        with mock.patch.dict(os.environ, {"OIKONOME_RELAY_KEYS": ""}), \
             self.assertLogs("oikonome.relay", level="WARNING") as cm, \
             TestClient(app):
            pass
        self.assertTrue(any("OPEN" in line for line in cm.output), cm.output)
        with mock.patch.dict(os.environ, {"OIKONOME_RELAY_KEYS": "k"}), \
             self.assertLogs("oikonome.relay", level="INFO") as cm, \
             TestClient(app):
            pass
        self.assertFalse(any("OPEN" in line for line in cm.output), cm.output)

    def test_forwarded_for_is_only_believed_from_a_trusted_proxy(self):
        """The per-address limiter keys on the source address. A direct
        hit that forges X-Forwarded-For used to pick its own bucket, so
        the limit could be sidestepped one header value at a time."""
        from oikonome.relay import app as relay_app
        with mock.patch.dict(os.environ, {"OIKONOME_TRUSTED_PROXIES": ""}), \
             mock.patch("oikonome.relay.fcm.available", return_value=True), \
             mock.patch("oikonome.relay.fcm.send", return_value="ok"):
            for i in range(relay_app.RATE_LIMIT_PER_MIN):
                r = self.client.post("/v1/push", json=self._one(), headers={
                    "X-Forwarded-For": f"203.0.113.{i % 250}"})
                self.assertEqual(r.status_code, 200)
            r = self.client.post("/v1/push", json=self._one(),
                                 headers={"X-Forwarded-For": "198.51.100.9"})
            self.assertEqual(r.status_code, 429)
        relay_app._hits.clear()
        # from a TRUSTED peer the header is honoured (rightmost untrusted
        # hop, exactly as the instance does it)
        peer = "127.0.0.1"
        with mock.patch.dict(os.environ, {"OIKONOME_TRUSTED_PROXIES":
                                          f"{peer}/32"}), \
             mock.patch("oikonome.relay.fcm.available", return_value=True), \
             mock.patch("oikonome.relay.fcm.send", return_value="ok"):
            client = TestClient(relay_app.app, client=(peer, 50000))
            for i in range(relay_app.RATE_LIMIT_PER_MIN):
                r = client.post("/v1/push", json=self._one(), headers={
                    "X-Forwarded-For": f"203.0.113.{i % 250}"})
                self.assertEqual(r.status_code, 200)
            # distinct forwarded clients through the trusted proxy are
            # distinct buckets, so a fresh one is not limited
            r = client.post("/v1/push", json=self._one(),
                            headers={"X-Forwarded-For": "198.51.100.9"})
            self.assertEqual(r.status_code, 200)

    def test_address_cap_evicts_the_quietest_not_everyone(self):
        """Bounding the limiter map by clearing it outright would let a
        spray of fresh addresses hand every busy sender a clean minute —
        the cap becoming the limiter's own reset button. The least recently
        seen entries go instead, and the busy sender stays counted."""
        from oikonome.relay import app as relay_app
        with mock.patch.object(relay_app, "MAX_TRACKED_IPS", 50):
            for _ in range(relay_app.RATE_LIMIT_PER_MIN):
                self.assertFalse(relay_app._rate_limited("busy"))
            self.assertTrue(relay_app._rate_limited("busy"))
            for i in range(200):
                # the busy sender keeps talking while the spray runs
                relay_app._rate_limited("busy")
                relay_app._rate_limited(f"spray-{i}")
            self.assertLessEqual(len(relay_app._hits), 50)
            self.assertIn("busy", relay_app._hits)
            self.assertTrue(relay_app._rate_limited("busy"),
                            "the spray must not reset the busy sender")
            self.assertNotIn("spray-0", relay_app._hits)

    def test_an_open_relay_is_bounded_as_a_whole_not_only_per_address(self):
        """With no instance keys configured there is no sender identity, so
        a spray from fresh addresses gets a fresh per-address window every
        time and the publisher's push credentials are bounded by nothing.
        The relay as a whole is capped instead — the one bound a new source
        address cannot walk around."""
        from oikonome.relay import app as relay_app
        peer = "127.0.0.1"
        with mock.patch.dict(os.environ, {
                "OIKONOME_RELAY_KEYS": "",
                "OIKONOME_TRUSTED_PROXIES": f"{peer}/32"}), \
             mock.patch("oikonome.relay.fcm.available", return_value=True), \
             mock.patch("oikonome.relay.fcm.send", return_value="ok"):
            client = TestClient(relay_app.app, client=(peer, 50000))
            # every push from an address of its own: no per-address bucket
            # ever fills, so only the global one can refuse anything
            for i in range(relay_app.OPEN_RELAY_PER_MIN):
                r = client.post("/v1/push", json=self._one(), headers={
                    "X-Forwarded-For": f"198.18.{i // 250}.{i % 250}"})
                self.assertEqual(r.status_code, 200, i)
            r = client.post("/v1/push", json=self._one(),
                            headers={"X-Forwarded-For": "198.51.100.9"})
            self.assertEqual(r.status_code, 429,
                             "a fresh source address walked past the open "
                             "relay's global ceiling")

    def test_a_keyed_relay_is_bounded_per_key_not_globally(self):
        """The global ceiling exists because an open relay has no sender to
        attribute a push to. A keyed relay does: each instance carries its
        own bucket, so one busy instance must never spend a ceiling shared
        with every other instance on the same relay."""
        from oikonome.relay import app as relay_app
        with mock.patch.dict(os.environ, {"OIKONOME_RELAY_KEYS": "k-one"}), \
             mock.patch("oikonome.relay.fcm.available", return_value=True), \
             mock.patch("oikonome.relay.fcm.send", return_value="ok"):
            r = self.client.post("/v1/push", json=self._one(),
                                 headers={"X-Relay-Key": "k-one"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(relay_app._OPEN_GLOBAL, relay_app._hits)

    def test_strict_shape_rejections(self):
        for bad in (self._one(token="short"),
                    self._one(event="free text here"),
                    self._one(platform="webpush"),
                    self._one(badge="three"),
                    self._one(badge=10_000),
                    {"notifications": []},
                    {"notifications": "nope"}):
            r = self.client.post("/v1/push", json=bad)
            self.assertEqual(r.status_code, 400, bad)

    def test_batch_cap(self):
        from oikonome.relay.app import MAX_BATCH
        items = [{"token": "t" * 32, "platform": "fcm", "event": "daily"}
                 ] * (MAX_BATCH + 1)
        r = self.client.post("/v1/push", json={"notifications": items})
        self.assertEqual(r.status_code, 400)

    def test_apns_and_unconfigured_fcm_report_unavailable(self):
        r = self.client.post("/v1/push",
                             json=self._one(platform="apns", token="a" * 32))
        self.assertEqual(r.status_code, 200)
        self.assertIn("apns", r.json()["unavailable"])
        with mock.patch("oikonome.relay.fcm.available", return_value=False):
            r = self.client.post("/v1/push", json=self._one())
            self.assertEqual(r.status_code, 200)
            self.assertIn("fcm", r.json()["unavailable"])
            self.assertEqual(r.json()["sent"], 0)

    def test_fcm_outcomes_are_accounted(self):
        # outcome keyed on the token, not call order — the sends fan out
        # across threads, so arrival order is not guaranteed
        verdicts = {"tok-a" * 8: "ok", "tok-b" * 8: "dead",
                    "tok-c" * 8: "error"}
        with mock.patch("oikonome.relay.fcm.available", return_value=True), \
             mock.patch("oikonome.relay.fcm.send",
                        side_effect=lambda t, *a, **k: verdicts[t]):
            body = {"notifications": [
                {"token": f"tok-{c}" * 8, "platform": "fcm", "event": "test"}
                for c in "abc"]}
            r = self.client.post("/v1/push", json=body)
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertEqual(out["sent"], 1)
        self.assertEqual(out["dead"], ["tok-b" * 8])

    def test_apns_outcomes_are_accounted(self):
        # outcome keyed on the token, not call order — the sends may fan
        # out across threads, so arrival order is not guaranteed
        verdicts = {"a" * 32: "ok", "b" * 32: "dead", "c" * 32: "error"}
        with mock.patch("oikonome.relay.apns.available", return_value=True), \
             mock.patch("oikonome.relay.apns.send",
                        side_effect=lambda t, *a, **k: verdicts[t]):
            body = {"notifications": [
                {"token": c * 32, "platform": "apns", "event": "test"}
                for c in "abc"]}
            r = self.client.post("/v1/push", json=body)
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertEqual(out["sent"], 1)
        self.assertEqual(out["dead"], ["b" * 32])
        self.assertEqual(out["unavailable"], [])

    def test_impossible_apns_token_is_dead_without_touching_the_wire(self):
        """A string that cannot be an APNs device token (they are hex)
        would otherwise be spliced into the HTTP path of the relay's
        credentialed APNs connection. It is reported dead — the verdict
        that makes the instance stop resending it — and never reaches
        the send path at all."""
        with mock.patch("oikonome.relay.apns.available", return_value=True), \
             mock.patch("oikonome.relay.apns.send",
                        return_value="ok") as snd:
            for evil in ("z" * 32,                    # right length, not hex
                         "a" * 20 + "/3/device/xxxx",  # path metacharacters
                         "0123456789abcdef" + "%2e" * 8):
                r = self.client.post("/v1/push", json=self._one(
                    platform="apns", token=evil))
                self.assertEqual(r.status_code, 200, evil)
                self.assertEqual(r.json()["dead"], [evil])
                snd.assert_not_called()
            # a plausibly real (hex) token still goes out
            r = self.client.post("/v1/push", json=self._one(
                platform="apns", token="0123456789abcdef" * 4))
            self.assertEqual(r.json()["sent"], 1)
            snd.assert_called_once()

    def test_invalid_item_rejects_the_batch_before_any_send(self):
        """Validation covers the whole batch before the first send, so a
        400 always means zero notifications were delivered — never a
        half-sent batch the instance will blindly retry."""
        with mock.patch("oikonome.relay.fcm.available", return_value=True), \
             mock.patch("oikonome.relay.fcm.send",
                        return_value="ok") as snd:
            body = {"notifications": [
                {"token": "t" * 32, "platform": "fcm", "event": "daily"},
                {"token": "t" * 32, "platform": "fcm",
                 "event": "free text here"}]}
            r = self.client.post("/v1/push", json=body)
        self.assertEqual(r.status_code, 400)
        snd.assert_not_called()

    def test_apns_batch_fans_out_across_the_pool_with_exact_tallies(self):
        """A batch of blocking APNs sends must not run one after another
        on the request thread, and the fan-out must not garble the
        accounting: outcomes are aggregated back on the request thread,
        so sent/dead stay exact however the workers interleave."""
        import threading
        import time as _time
        seen_threads = set()
        guard = threading.Lock()

        def fake_send(token, event, badge=None, text=None):
            with guard:
                seen_threads.add(threading.get_ident())
            _time.sleep(0.02)      # long enough to engage several workers
            return ("ok", "dead", "error")[int(token[:2]) % 3]

        tokens = [f"{i:02d}" + "a" * 30 for i in range(24)]
        body = {"notifications": [
            {"token": t, "platform": "apns", "event": "test"}
            for t in tokens]}
        with mock.patch("oikonome.relay.apns.available", return_value=True), \
             mock.patch("oikonome.relay.apns.send", side_effect=fake_send):
            r = self.client.post("/v1/push", json=body)
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertEqual(out["sent"], 8)
        self.assertEqual(sorted(out["dead"]),
                         sorted(t for t in tokens if int(t[:2]) % 3 == 1))
        # more than one thread did the sending — not the request thread
        # grinding through the batch alone
        self.assertGreater(len(seen_threads), 1)

    def test_fcm_batch_fans_out_across_the_pool_too(self):
        """The FCM leg is a blocking HTTPS round-trip per token, exactly
        like APNs. Sending a batch one token at a time on the request
        thread held an ASGI worker for as long as the slowest 500 sends —
        one instance's batch starving every other instance sharing the
        relay."""
        import threading
        import time as _time
        seen_threads = set()
        guard = threading.Lock()

        def fake_send(token, event, badge=None, text=None):
            with guard:
                seen_threads.add(threading.get_ident())
            _time.sleep(0.02)      # long enough to engage several workers
            return ("ok", "dead", "error")[int(token[:2]) % 3]

        tokens = [f"{i:02d}" + "z" * 30 for i in range(24)]
        body = {"notifications": [
            {"token": t, "platform": "fcm", "event": "test"} for t in tokens]}
        with mock.patch("oikonome.relay.fcm.available", return_value=True), \
             mock.patch("oikonome.relay.fcm.send", side_effect=fake_send):
            started = _time.monotonic()
            r = self.client.post("/v1/push", json=body)
            elapsed = _time.monotonic() - started
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertEqual(out["sent"], 8)
        self.assertEqual(sorted(out["dead"]),
                         sorted(t for t in tokens if int(t[:2]) % 3 == 1))
        self.assertGreater(len(seen_threads), 1)
        # 24 sends of 20ms is 480ms in sequence; pooled it is a fraction
        self.assertLess(elapsed, 0.3)

    def test_a_mixed_batch_reaches_both_legs(self):
        """One pool serves both legs, so the split must still send each
        token down the right one."""
        with mock.patch("oikonome.relay.fcm.available", return_value=True), \
             mock.patch("oikonome.relay.fcm.send", return_value="ok") as f, \
             mock.patch("oikonome.relay.apns.available", return_value=True), \
             mock.patch("oikonome.relay.apns.send", return_value="ok") as a:
            body = {"notifications": [
                {"token": "f" * 40, "platform": "fcm", "event": "test"},
                {"token": "0123456789abcdef" * 4, "platform": "apns",
                 "event": "test"}]}
            r = self.client.post("/v1/push", json=body)
        self.assertEqual(r.json()["sent"], 2)
        f.assert_called_once()
        a.assert_called_once()

    def test_rate_limit_trips(self):
        from oikonome.relay import app as relay_app
        with mock.patch.object(relay_app, "RATE_LIMIT_PER_MIN", 3):
            for _ in range(3):
                r = self.client.post("/v1/push",
                                     json=self._one(platform="apns"))
                self.assertEqual(r.status_code, 200)
            r = self.client.post("/v1/push", json=self._one(platform="apns"))
            self.assertEqual(r.status_code, 429)

    def test_an_oversized_body_is_refused_before_it_is_parsed(self):
        """FastAPI reads and JSON-parses the whole body to build the
        handler's argument, so nothing inside the handler can protect the
        relay from a body it has already buffered. The ceiling is enforced
        at the ASGI edge; a body over it never becomes a Python object."""
        from oikonome.relay.app import MAX_BODY_BYTES
        with mock.patch("oikonome.relay.fcm.available", return_value=True), \
             mock.patch("oikonome.relay.fcm.send", return_value="ok") as snd:
            r = self.client.post(
                "/v1/push", content=b"a" * (MAX_BODY_BYTES + 1),
                headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 413)
        self.assertIn("too large", r.text)
        snd.assert_not_called()

    def test_the_instance_key_is_checked_before_the_body_is_read(self):
        """A body that is not even valid JSON gets a 401, not a parse
        error: an unkeyed caller is refused on its headers alone, before
        the relay spends a byte of memory on what it sent."""
        with mock.patch.dict(os.environ, {"OIKONOME_RELAY_KEYS": "k-one"}):
            r = self.client.post("/v1/push",
                                 content=b'{"notifications": [',
                                 headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 401)

    def test_a_chunked_body_is_counted_as_it_streams(self):
        """No content-length means the header check alone would leave the
        hole open."""
        from oikonome.relay.app import MAX_BODY_BYTES

        def chunks():
            yield b"a" * (MAX_BODY_BYTES + 4096)

        r = self.client.post("/v1/push", content=chunks(),
                             headers={"content-type": "application/json"})
        self.assertNotEqual(r.status_code, 200)

    def test_the_body_ceiling_admits_the_largest_legal_batch(self):
        """The cap is derived from the limits the handler publishes, so it
        can never sit below a batch the relay would otherwise accept."""
        from oikonome.relay.app import (MAX_BATCH, MAX_BODY_BYTES,
                                        MAX_TOKEN_LEN)
        biggest = json.dumps({"notifications": [
            {"token": "a" * MAX_TOKEN_LEN, "platform": "fcm",
             "event": "monthly", "badge": 999}] * MAX_BATCH})
        self.assertLess(len(biggest.encode()), MAX_BODY_BYTES)

    def test_every_push_names_the_key_that_sent_it(self):
        """Every push is attributable to the instance that sent it — by
        FINGERPRINT, never the key itself, and never a token. It is what
        the token binding refuses by (test_relay_token_binding) and what
        an operator reads a pattern of abuse out of."""
        from oikonome.relay.app import _fingerprint
        token = "t" * 32
        with mock.patch.dict(os.environ, {"OIKONOME_RELAY_KEYS":
                                          "k-one,k-two"}), \
             mock.patch("oikonome.relay.fcm.available", return_value=True), \
             mock.patch("oikonome.relay.fcm.send", return_value="ok"), \
             self.assertLogs("oikonome.relay", level="INFO") as cm:
            r = self.client.post("/v1/push", json=self._one(token=token),
                                 headers={"X-Relay-Key": "k-two"})
        self.assertEqual(r.status_code, 200)
        logged = "\n".join(cm.output)
        self.assertIn(f"key={_fingerprint('k-two')}", logged)
        self.assertNotIn("k-two", logged)
        self.assertNotIn(token, logged)
        self.assertNotEqual(_fingerprint("k-one"), _fingerprint("k-two"))

    def test_a_key_is_rate_limited_as_well_as_an_address(self):
        """The address bucket bounds one sender; the key bucket bounds one
        instance however many addresses it pushes from — otherwise a key
        holder rotating source addresses has no ceiling at all."""
        from oikonome.relay import app as relay_app
        peer = "127.0.0.1"
        with mock.patch.dict(os.environ, {
                "OIKONOME_RELAY_KEYS": "k-one",
                "OIKONOME_TRUSTED_PROXIES": f"{peer}/32"}), \
             mock.patch.object(relay_app, "RATE_LIMIT_PER_MIN", 3), \
             mock.patch("oikonome.relay.fcm.available", return_value=True), \
             mock.patch("oikonome.relay.fcm.send", return_value="ok"):
            client = TestClient(relay_app.app, client=(peer, 50000))
            hdr = {"X-Relay-Key": "k-one"}
            for i in range(3):
                r = client.post("/v1/push", json=self._one(), headers={
                    **hdr, "X-Forwarded-For": f"203.0.113.{i}"})
                self.assertEqual(r.status_code, 200, i)
            r = client.post("/v1/push", json=self._one(), headers={
                **hdr, "X-Forwarded-For": "203.0.113.200"})
            self.assertEqual(r.status_code, 429)

    def test_the_limiter_survives_concurrent_callers(self):
        """The endpoint is a sync def, so Starlette runs concurrent pushes
        on separate threads and the limiter's check-then-act is a real
        race: two threads arriving for the same fresh bucket each built
        their own deque, the second assignment discarded the first, and
        one thread's timestamps landed where nobody counts them. The
        widened window below is what makes that deterministic."""
        import threading
        import types
        from oikonome.relay import app as relay_app

        real_deque = collections.deque

        def slow_deque(*a):
            # holds the window between "no bucket yet" and the assignment
            # open long enough for every thread to be inside it
            time.sleep(0.05)
            return real_deque(*a)

        shim = types.SimpleNamespace(deque=slow_deque,
                                     OrderedDict=collections.OrderedDict)
        threads = 4
        barrier = threading.Barrier(threads)

        def call():
            barrier.wait()
            relay_app._rate_limited("concurrent")

        with mock.patch.object(relay_app, "collections", shim):
            ts = [threading.Thread(target=call) for _ in range(threads)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        # every admitted call is counted in the ONE bucket for that sender
        self.assertEqual(len(relay_app._hits["concurrent"]), threads)


class _ApnsResponse:
    def __init__(self, status: int, reason: str | None = None):
        self.status_code = status
        self._reason = reason

    def json(self):
        return {"reason": self._reason} if self._reason else {}


class ApnsLegTests(unittest.TestCase):
    """The APNs leg's delivery semantics against a mocked wire: a device
    token carries no marker of which environment minted it, so delivery
    must fall back from production to sandbox on BadDeviceToken and only
    call a token dead when both environments refuse it."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption())
        cls._keyfile = tempfile.NamedTemporaryFile(suffix=".p8")
        cls._keyfile.write(pem)
        cls._keyfile.flush()
        cls.env = {"OIKONOME_RELAY_APNS_KEY": cls._keyfile.name,
                   "OIKONOME_RELAY_APNS_KEY_ID": "TESTKEYID0",
                   "OIKONOME_RELAY_APNS_TEAM_ID": "TESTTEAMID",
                   "OIKONOME_RELAY_APNS_TOPIC": "org.example.oikonome"}

    @classmethod
    def tearDownClass(cls):
        cls._keyfile.close()

    def setUp(self):
        from oikonome.relay import apns
        apns._key = None
        apns._jwt = None
        self.apns = apns
        self.patch = mock.patch.dict(os.environ, self.env)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.apns._key = None
        self.apns._jwt = None

    def test_unconfigured_without_all_four_settings(self):
        self.apns._key = None
        with mock.patch.dict(os.environ,
                             {"OIKONOME_RELAY_APNS_TEAM_ID": ""}):
            self.assertFalse(self.apns.available())
        self.apns._key = None
        self.assertTrue(self.apns.available())

    def test_provider_token_is_a_signed_es256_jwt(self):
        import base64
        tok = self.apns._provider_token()
        header, claims, sig = tok.split(".")

        def dec(part):
            return json.loads(base64.urlsafe_b64decode(part + "=="))
        self.assertEqual(dec(header),
                         {"alg": "ES256", "kid": "TESTKEYID0"})
        self.assertEqual(dec(claims)["iss"], "TESTTEAMID")
        # raw r||s, not DER
        self.assertEqual(len(base64.urlsafe_b64decode(sig + "==")), 64)
        # cached until stale
        self.assertIs(tok, self.apns._provider_token())

    def test_prod_delivery_and_dead_token(self):
        with mock.patch.object(self.apns, "_request",
                               return_value=_ApnsResponse(200)) as req:
            self.assertEqual(self.apns.send("tok" * 8, "daily", 3), "ok")
        base, token, headers, payload = req.call_args[0]
        self.assertEqual(base, self.apns._PROD)
        self.assertEqual(headers["apns-topic"], "org.example.oikonome")
        # content-free: fixed text + event + badge, nothing else
        self.assertEqual(sorted(payload), ["aps", "event"])
        self.assertEqual(payload["event"], "daily")
        self.assertEqual(payload["aps"]["badge"], 3)
        with mock.patch.object(
                self.apns, "_request",
                return_value=_ApnsResponse(410, "Unregistered")):
            self.assertEqual(self.apns.send("tok" * 8, "daily"), "dead")

    def test_bad_device_token_falls_back_to_sandbox(self):
        responses = {self.apns._PROD: _ApnsResponse(400, "BadDeviceToken"),
                     self.apns._SANDBOX: _ApnsResponse(200)}
        with mock.patch.object(
                self.apns, "_request",
                side_effect=lambda base, *a: responses[base]) as req:
            self.assertEqual(self.apns.send("tok" * 8, "test"), "ok")
        self.assertEqual([c.args[0] for c in req.call_args_list],
                         [self.apns._PROD, self.apns._SANDBOX])
        # both environments refusing it is the only "dead" verdict
        with mock.patch.object(
                self.apns, "_request",
                return_value=_ApnsResponse(400, "BadDeviceToken")):
            self.assertEqual(self.apns.send("tok" * 8, "test"), "dead")

    def test_expired_provider_token_is_reminted_once(self):
        responses = iter([_ApnsResponse(403, "ExpiredProviderToken"),
                          _ApnsResponse(200)])
        with mock.patch.object(self.apns, "_request",
                               side_effect=lambda *a: next(responses)):
            self.assertEqual(self.apns.send("tok" * 8, "test"), "ok")

    def test_failed_resign_after_expiry_is_an_error_not_a_none_header(self):
        """When the re-sign after ExpiredProviderToken fails, retrying
        would put the literal string "bearer None" on the wire and
        misreport a signing failure as a delivery one. The send must
        stop at "error" after a single wire call."""
        def tok(fresh=False):
            return None if fresh else "stale-token"
        with mock.patch.object(self.apns, "_provider_token",
                               side_effect=tok), \
             mock.patch.object(
                 self.apns, "_request",
                 return_value=_ApnsResponse(403, "ExpiredProviderToken"),
                 ) as req:
            self.assertEqual(self.apns.send("ab" * 16, "test"), "error")
        self.assertEqual(req.call_count, 1)
        for call in req.call_args_list:
            self.assertNotIn("None", call.args[2]["authorization"])

    def test_device_path_never_carries_raw_token_text(self):
        """Defense in depth below the relay's shape check: whatever the
        caller passes, the token is percent-encoded before it becomes
        part of the request path on the credentialed connection."""
        cli = mock.Mock()
        with mock.patch.dict(self.apns._clients, {self.apns._PROD: cli}):
            self.apns._request(self.apns._PROD, "ab/../3/device/evil?x=1",
                               {}, {})
        path = cli.post.call_args[0][0]
        self.assertEqual(
            path, "/3/device/ab%2F..%2F3%2Fdevice%2Fevil%3Fx%3D1")

    def test_wire_failure_is_an_error_not_a_raise(self):
        with mock.patch.object(self.apns, "_request",
                               side_effect=OSError("boom")):
            self.assertEqual(self.apns.send("tok" * 8, "test"), "error")


class CredentialCacheConcurrencyTests(unittest.TestCase):
    """The relay's auth-token caches are shared by every request thread and
    by the send-workers each request fans its batch out across. A refresh
    is a check-then-act on that shared state, so at every expiry boundary
    the threads that meet the stale cache together must produce ONE refresh
    between them — not one apiece against the publisher's credentials.
    """

    N = 8

    def _race(self, call):
        """Run `call` on N threads released together, return their results."""
        import threading
        start = threading.Barrier(self.N)
        out: list = []
        guard = threading.Lock()

        def worker():
            start.wait()
            r = call()
            with guard:
                out.append(r)

        threads = [threading.Thread(target=worker) for _ in range(self.N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        return out

    def test_one_oauth_exchange_serves_every_concurrent_fcm_caller(self):
        """Google's token endpoint gets one exchange per expiry, however
        many threads noticed at once — a herd here is the publisher's own
        OAuth credentials being hammered from every instance the relay
        serves, on a schedule an attacker does not even have to guess."""
        import threading
        from oikonome.relay import fcm
        calls = []
        guard = threading.Lock()

        def exchange(url, data=None, timeout=None):
            with guard:
                calls.append(url)
            # hold the refresh open long enough that every other thread is
            # certainly inside the window the cache is stale
            time.sleep(0.2)
            r = mock.Mock()
            r.status_code = 200
            r.raise_for_status = lambda: None
            r.json = lambda: {"access_token": "at-1", "expires_in": 3600}
            return r

        with mock.patch.object(fcm, "_creds", {
                    "project_id": "p", "client_email": "e@x",
                    "private_key": "pem"}), \
             mock.patch.object(fcm, "_access", None), \
             mock.patch.object(fcm, "_access_tried", 0.0), \
             mock.patch.object(fcm, "_signed_jwt", return_value="jwt"), \
             mock.patch("httpx.post", exchange):
            got = self._race(fcm._access_token)
        self.assertEqual(len(calls), 1,
                         f"{len(calls)} concurrent callers each ran their "
                         "own token exchange")
        self.assertEqual(got, ["at-1"] * self.N)

    def test_one_provider_token_serves_every_concurrent_apns_caller(self):
        """Apple throttles a provider that re-signs too often, and both
        moments that need a fresh token — the lifetime boundary and the
        ExpiredProviderToken rejection — arrive for every in-flight send at
        the same instant."""
        import threading
        from oikonome.relay import apns
        signings = []
        guard = threading.Lock()

        def sign(key):
            with guard:
                signings.append(key)
            time.sleep(0.2)
            return "jwt-signed"

        with mock.patch.object(apns, "_key", {
                    "pem": b"pem", "key_id": "k", "team_id": "t",
                    "topic": "com.example"}), \
             mock.patch.object(apns, "_jwt", None), \
             mock.patch.object(apns, "_signed_jwt", sign):
            # cold cache
            got = self._race(apns._provider_token)
            self.assertEqual(len(signings), 1,
                             f"{len(signings)} concurrent callers each "
                             "signed their own provider token")
            self.assertEqual(got, ["jwt-signed"] * self.N)
            # and the remint every send hits together when APNs answers
            # ExpiredProviderToken
            got = self._race(lambda: apns._provider_token(fresh=True))
            self.assertEqual(len(signings), 2,
                             "the ExpiredProviderToken remint stampeded")
            self.assertEqual(got, ["jwt-signed"] * self.N)


if __name__ == "__main__":
    unittest.main()
