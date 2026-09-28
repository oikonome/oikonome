"""Every enum field of a relay push is type-checked before it is compared.

The relay's payload is a strict shape: `event` and `platform` are enums,
`badge` a small integer. A caller-typed JSON value that is a list or an
object must be refused with the batch's 400 — never reach a set
membership test (a list is unhashable, so `[1] in frozenset(...)` raises
and the shared relay answers an unhandled 500), and never slip through a
check that happens to accept it (`True` is an `int` in Python, so a
boolean badge would be forwarded to the platform as `1`).
"""

import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient


class RelayMistypedFieldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("OIKONOME_RELAY_KEYS", None)
        from oikonome.relay import app as relay
        cls.relay = relay
        # a crash must show up as the 500 a caller would see, not as an
        # exception inside the test
        cls.client = TestClient(relay.app, raise_server_exceptions=False)

    def _push(self, **fields):
        item = {"token": "tok-" + "a" * 20, "platform": "fcm",
                "event": "daily", **fields}
        sent = []
        with mock.patch.object(self.relay.fcm, "available",
                               return_value=True), \
             mock.patch.object(self.relay.fcm, "send",
                               side_effect=lambda *a, **k: (sent.append(a)
                                                            or "ok")):
            r = self.client.post("/v1/push",
                                 json={"notifications": [item]})
        return r, sent

    def test_non_string_event_is_a_400_not_a_crash(self):
        for bad in ([1], {"a": 1}, ["daily"], 7, None, True):
            with self.subTest(event=bad):
                r, sent = self._push(event=bad)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertEqual(sent, [])

    def test_non_string_platform_is_a_400_not_a_crash(self):
        for bad in ([1], {"fcm": 1}, ["fcm"], 1, None):
            with self.subTest(platform=bad):
                r, sent = self._push(platform=bad)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertEqual(sent, [])

    def test_boolean_badge_is_refused(self):
        r, sent = self._push(badge=True)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(sent, [])

    def test_well_formed_item_still_sends(self):
        r, sent = self._push(badge=3)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(sent), 1)


if __name__ == "__main__":
    unittest.main()
