"""The push relay's one content field.

A notification may carry a single display line (`text`) — the daily
verdict — and the relay bounds it: a string, flattened to one printable
line, at most MAX_TEXT_LEN characters, handed to the platform leg as the
notification body. Anything else is a 400 for the whole batch, so a bad
line never half-sends. Without `text` the fixed per-event line is shown,
exactly as before.

The line rides the DAILY push and no other. The instance already refuses
to attach it elsewhere, but the relay is the boundary that decides what
reaches a phone, so it refuses the batch rather than quietly stripping
the field — a sender that got the contract wrong learns on its first try.
"""

import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient


class RelayTextTests(unittest.TestCase):
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
                               side_effect=lambda *a, **k: (sent.append((a, k))
                                                            or "ok")):
            r = self.client.post("/v1/push", json={"notifications": [
                {"token": "tok-" + "a" * 20, "platform": "fcm", **item}]})
        return r, sent

    def test_text_rides_to_the_leg_as_one_line(self):
        r, sent = self._push({"event": "daily",
                              "text": "OVER budget by $120.\nFood +$40."})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["sent"], 1)
        (_tok, _event, _badge, text), _ = sent[0]
        self.assertEqual(text, "OVER budget by $120. Food +$40.")

    def test_no_text_means_the_fixed_line(self):
        r, sent = self._push({"event": "daily"})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(sent[0][0][3])

    def test_text_is_bounded_and_typed(self):
        r, sent = self._push({"event": "daily",
                              "text": "x" * (self.relay.MAX_TEXT_LEN + 1)})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(sent, [])
        r, sent = self._push({"event": "daily", "text": ["not", "a", "line"]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(sent, [])

    def test_text_on_any_other_event_refuses_the_batch(self):
        for event in sorted(self.relay.EVENTS - self.relay.TEXT_EVENTS):
            with self.subTest(event=event):
                r, sent = self._push({"event": event, "text": "a line"})
                self.assertEqual(r.status_code, 400, r.text)
                self.assertIn("only carried on the daily push",
                              r.json()["detail"])
                self.assertEqual(sent, [])

    def test_a_bad_event_poisons_the_whole_batch(self):
        # the batch is validated before anything is sent, so one weekly
        # item carrying a line takes the daily one down with it rather
        # than half-delivering
        sent = []
        with mock.patch.object(self.relay.fcm, "available",
                               return_value=True), \
             mock.patch.object(self.relay.fcm, "send",
                               side_effect=lambda *a, **k: (sent.append((a, k))
                                                            or "ok")):
            r = self.client.post("/v1/push", json={"notifications": [
                {"token": "tok-" + "a" * 20, "platform": "fcm",
                 "event": "daily", "text": "fine"},
                {"token": "tok-" + "b" * 20, "platform": "fcm",
                 "event": "weekly", "text": "not fine"}]})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(sent, [])

    def test_other_events_still_push_without_a_line(self):
        for event in sorted(self.relay.EVENTS - self.relay.TEXT_EVENTS):
            with self.subTest(event=event):
                r, sent = self._push({"event": event})
                self.assertEqual(r.status_code, 200, r.text)
                self.assertIsNone(sent[0][0][3])

    def test_blank_text_is_dropped_not_shown(self):
        r, sent = self._push({"event": "daily", "text": " \t\n "})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(sent[0][0][3])

    def test_legs_put_the_line_in_the_body(self):
        from oikonome.relay import fcm
        title, body = fcm._DISPLAY["daily"]
        self.assertEqual(body, "Today's verdict is ready")
        with mock.patch.object(fcm, "_load_creds",
                               return_value={"project_id": "p"}), \
             mock.patch.object(fcm, "_access_token", return_value="t"), \
             mock.patch("httpx.post") as post:
            post.return_value = mock.Mock(status_code=200)
            fcm.send("tok", "daily", text="UNDER budget by $30.")
            msg = post.call_args.kwargs["json"]["message"]
            self.assertEqual(msg["notification"]["body"],
                             "UNDER budget by $30.")
            self.assertEqual(msg["notification"]["title"], title)
            fcm.send("tok", "weekly")
            self.assertEqual(
                post.call_args.kwargs["json"]["message"]["notification"]["body"],
                fcm._DISPLAY["weekly"][1])
