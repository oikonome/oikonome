"""A device token belongs to the instance that registered it.

The relay fronts the publisher's APNs and FCM credentials, so it demands
an instance key. But a key only proves the caller is *an* opted-in
instance — it says nothing about whose phones are in the batch. Any
accepted key could therefore push to any token it learned: content-free
notification bombing of another household, on the publisher's quota,
attributable only as far as "some instance".

So the first push carrying a token REGISTERS it to the key that sent it
(the relay hears of a token at no other moment — devices register with
their own instance), and a later push for that token from a different key
is refused with a 403 and logged. The registering key keeps pushing as
before, and an open relay — one with no keys at all, a self-hoster
relaying only for themselves — binds nothing, because there is no sender
identity there to bind to.
"""

import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

TOKEN = "tok-" + "a" * 20
OTHER = "tok-" + "b" * 20


class RelayTokenBindingTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from oikonome.relay import app as relay
        cls.relay = relay
        cls.client = TestClient(relay.app)

    def setUp(self):
        env = mock.patch.dict(os.environ,
                              {"OIKONOME_RELAY_KEYS": "key-one,key-two"})
        env.start()
        self.addCleanup(env.stop)
        self.relay._owners.clear()
        self.relay._hits.clear()

    def _push(self, key, *tokens):
        """One batch from one instance key. Returns (response, sends)."""
        sends = []
        headers = {"X-Relay-Key": key} if key else {}
        with mock.patch.object(self.relay.fcm, "available", return_value=True), \
             mock.patch.object(self.relay.fcm, "send",
                               side_effect=lambda *a, **k: (sends.append(a)
                                                            or "ok")):
            r = self.client.post(
                "/v1/push", headers=headers,
                json={"notifications": [{"token": t, "platform": "fcm",
                                         "event": "alert"} for t in tokens]})
        return r, sends

    def test_the_first_push_registers_the_token_to_the_key_that_sent_it(self):
        r, sends = self._push("key-one", TOKEN)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["sent"], 1)
        self.assertEqual(len(sends), 1)

    def test_another_key_cannot_push_a_registered_token(self):
        self._push("key-one", TOKEN)
        r, sends = self._push("key-two", TOKEN)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(sends, [],
                         "the phone was woken by an instance it does not "
                         "belong to")

    def test_the_registering_key_still_pushes(self):
        self._push("key-one", TOKEN)
        r, sends = self._push("key-one", TOKEN)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(sends), 1)

    def test_a_refused_batch_sends_nothing_and_registers_nothing(self):
        """All-or-nothing, like the field validation: a 403 means zero
        notifications delivered, and the tokens the refused batch also
        carried are not quietly re-registered to the sender that was just
        turned away."""
        self._push("key-one", TOKEN)
        r, sends = self._push("key-two", OTHER, TOKEN)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(sends, [])
        # OTHER was never claimed by the refused batch, so its real owner
        # can still register it
        r, sends = self._push("key-one", OTHER)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(sends), 1)

    def test_an_open_relay_binds_nothing(self):
        """No keys configured: every caller is the same anonymous sender,
        so a binding would record a fact about nobody."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OIKONOME_RELAY_KEYS")
            r, _ = self._push(None, TOKEN)
            self.assertEqual(r.status_code, 200, r.text)
            r, sends = self._push(None, TOKEN)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(len(sends), 1)

    def test_a_binding_lapses_after_the_token_goes_quiet(self):
        """A household that MOVES instances — hosted to self-hosted — keeps
        the same phone and the same token. Without expiry the new instance
        could never push it again."""
        self._push("key-one", TOKEN)
        with mock.patch.object(self.relay, "TOKEN_BINDING_TTL", -1):
            r, sends = self._push("key-two", TOKEN)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(sends), 1)
        # and the token is the NEW instance's now
        r, _ = self._push("key-one", TOKEN)
        self.assertEqual(r.status_code, 403)

    def test_a_retired_key_takes_its_claims_with_it(self):
        """How a key rotation finishes. The operator adds the new key,
        switches the instance over, then drops the old one — and the
        tokens the old key held are free the moment it leaves the list, so
        the instance re-registers them on its next push instead of being
        refused until the binding lapses."""
        self._push("key-one", TOKEN)
        with mock.patch.dict(os.environ, {"OIKONOME_RELAY_KEYS": "key-two"}):
            r, sends = self._push("key-two", TOKEN)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(sends), 1)

    def test_the_registry_holds_a_digest_not_the_token(self):
        """The relay's promise is that it does not keep device tokens. A
        registry of raw ones would be a copy of every phone it has pushed,
        in the process that also holds the publisher's push credentials."""
        self._push("key-one", TOKEN)
        self.assertEqual(len(self.relay._owners), 1)
        self.assertNotIn(TOKEN, self.relay._owners)
        self.assertNotIn(TOKEN, repr(list(self.relay._owners.items())))

    def test_an_unkeyed_caller_is_still_refused_before_any_of_this(self):
        r, sends = self._push(None, TOKEN)
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(sends, [])
        self.assertEqual(len(self.relay._owners), 0)

    def test_the_rate_limits_still_bound_a_keyed_sender(self):
        """Binding is an extra check, not a replacement: the per-key window
        still refuses at the edge, before the body is read."""
        with mock.patch.object(self.relay, "RATE_LIMIT_PER_MIN", 1):
            self.assertEqual(self._push("key-one", TOKEN)[0].status_code, 200)
            r, sends = self._push("key-one", TOKEN)
        self.assertEqual(r.status_code, 429, r.text)
        self.assertEqual(sends, [])


if __name__ == "__main__":
    unittest.main()
