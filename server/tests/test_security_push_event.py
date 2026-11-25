"""A security change reaches the phone as its own event, and an instance
newer than its relay still gets the push through.

The phone has to say something a person who did NOT make the change will
act on, and a tap has to open the screen where they can undo it. That is a
"security" event: the relay chooses its fixed line, exactly as for every
other event, and the instance still sends nothing but token, event and
badge — the line the relay can read is not widened.

Instances and the relay upgrade at different times. A relay that predates
the event refuses the whole batch with 400 "unknown event" (and a 400
always means nothing was delivered), so the instance re-sends that batch
once as the generic "alert" — never both, never more than once, and never
on any other refusal, which an "alert" would only hit again.
"""

import json
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient


class RelaySecurityEventTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("OIKONOME_RELAY_KEYS", None)
        from oikonome.relay import app as relay
        cls.relay = relay
        cls.client = TestClient(relay.app)

    def _push(self, item):
        sent = []
        with mock.patch.object(self.relay.fcm, "available", return_value=True), \
             mock.patch.object(self.relay.fcm, "send",
                               side_effect=lambda *a, **k: (sent.append(a)
                                                            or "ok")):
            r = self.client.post("/v1/push", json={"notifications": [
                {"token": "tok-" + "s" * 20, "platform": "fcm", **item}]})
        return r, sent

    def test_relay_accepts_the_security_event(self):
        r, sent = self._push({"event": "security"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["sent"], 1)
        self.assertEqual(sent[0][1], "security")
        self.assertIsNone(sent[0][3])

    def test_the_security_line_is_the_relays_own_and_names_nothing(self):
        from oikonome.relay import fcm
        title, body = fcm._DISPLAY["security"]
        self.assertIn("Security", title)
        self.assertNotEqual((title, body), fcm._DISPLAY["alert"])
        self.assertNotEqual(body, "Open the app for details")

    def test_a_security_push_cannot_carry_text(self):
        r, sent = self._push({"event": "security", "text": "someone else"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(sent, [])

    def test_an_unknown_event_is_refused_by_name(self):
        # The instance's fallback keys on this exact answer; it is the
        # relay's contract for an event it does not know.
        r, sent = self._push({"event": "no-such-event"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json(), {"detail": "unknown event"})
        self.assertEqual(sent, [])

    def test_the_instance_never_sends_an_event_the_relay_lacks(self):
        from oikonome.notify import push_native
        self.assertLessEqual(push_native.EVENTS, self.relay.EVENTS)
        self.assertIn("security", push_native.EVENTS)


class _Relay:
    """httpx.post at the channel's seam, answering from a script."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, url, json=None, timeout=None, headers=None):
        self.calls.append(json)
        status, body = self.answers.pop(0) if self.answers else (200, {"sent": 1})
        r = mock.Mock()
        r.status_code = status
        r.json = lambda: body
        r.text = __import__("json").dumps(body)
        return r


class InstanceSecurityPushTests(unittest.TestCase):
    TARGETS = [{"push_token": "tok-" + "a" * 20, "push_platform": "fcm"},
               {"push_token": "tok-" + "b" * 20, "push_platform": "apns"}]

    def _send(self, relay):
        from oikonome.notify import push_native

        def connect():
            return mock.Mock()
        with mock.patch.dict(os.environ,
                             {"OIKONOME_PUSH_RELAY_URL": "https://relay.test"}), \
                mock.patch("httpx.post", relay), \
                mock.patch("oikonome.auth.device_tokens.push_targets",
                           lambda c, t, u=None: list(self.TARGETS)):
            return push_native.send_security(None, "tenant", user_id="u",
                                             connect=connect)

    def test_a_security_push_carries_the_event_and_no_text(self):
        relay = _Relay((200, {"sent": 2}))
        self.assertEqual(self._send(relay), 2)
        self.assertEqual(len(relay.calls), 1)
        for n in relay.calls[0]["notifications"]:
            self.assertEqual(n["event"], "security")
            self.assertEqual(set(n) - {"badge"}, {"token", "platform", "event"})

    def test_a_relay_without_the_event_gets_one_alert_instead(self):
        relay = _Relay((400, {"detail": "unknown event"}), (200, {"sent": 2}))
        self.assertEqual(self._send(relay), 2)
        self.assertEqual([n["event"] for c in relay.calls
                          for n in c["notifications"]],
                         ["security", "security", "alert", "alert"])
        self.assertNotIn("text", json.dumps(relay.calls))

    def test_the_fallback_is_tried_once_even_if_it_is_refused_too(self):
        relay = _Relay((400, {"detail": "unknown event"}),
                       (400, {"detail": "unknown event"}))
        self.assertEqual(self._send(relay), 0)
        self.assertEqual(len(relay.calls), 2)

    def test_other_refusals_do_not_fall_back(self):
        for status, body in ((400, {"detail": "bad token"}),
                             (401, {"detail": "instance key required"}),
                             (403, {"detail": "registered elsewhere"}),
                             (429, {"detail": "slow down"}),
                             (500, {"detail": "boom"})):
            with self.subTest(status=status):
                relay = _Relay((status, body))
                self.assertEqual(self._send(relay), 0)
                self.assertEqual(len(relay.calls), 1)

    def test_an_unreachable_relay_does_not_fall_back_or_raise(self):
        calls = []

        def down(url, json=None, **kw):
            calls.append(json)
            raise OSError("no route")
        self.assertEqual(self._send(down), 0)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
