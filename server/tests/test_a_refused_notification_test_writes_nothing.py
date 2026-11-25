"""A notification test the door refuses writes nothing.

`POST /api/notify/test` builds the day's verdict to send it, and building
it refreshes the household's alert log. A channel it does not know, a text
test to a number that cannot take one, or a push test with no browser or
device to go to, is refused before any of that, so the refusal leaves every row as it was (the write detector's
rule, specs/invariants-and-fuzzing.md, F2)."""

import unittest

from oikonome.db import tenancy

from . import body_fuzz
from .money_surfaces import at_today


def _alert_rows(tid) -> list:
    conn = tenancy.tenant_connect(tid)
    try:
        # xmin moves on every write to a row, even one that sets the
        # values it already had
        return [tuple(r.values()) for r in conn.execute(
            "SELECT kind, xmin::text AS x FROM alerts_log ORDER BY kind")]
    finally:
        conn.close()


class ARefusedNotificationTestWritesNothing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.hh = body_fuzz.Household(appmod.app)
        cls.clock = at_today()
        cls.clock.start()

    def setUp(self):
        # an alert the household no longer has: building the verdict
        # clears it, so a verdict built for a refusal would write it
        conn = tenancy.tenant_connect(self.hh.tid)
        try:
            conn.execute(
                "INSERT INTO alerts_log (kind, severity, message, first_seen,"
                " last_seen) VALUES ('example', 'warn', %s, '2026-07-01',"
                " '2026-07-01')", (self.id(),))
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.clock.stop()

    def test_an_unknown_channel_is_refused_before_the_verdict(self):
        before = _alert_rows(self.hh.tid)
        self.assertTrue(before)
        for channel in ("email", "", "SMS "):
            from oikonome.web import security
            security._limiter._hits.clear()
            r = self.hh.client.post("/api/notify/test",
                                    json={"channel": channel})
            self.assertEqual(r.status_code, 400, r.text)
            self.assertEqual(_alert_rows(self.hh.tid), before, channel)

    def test_a_text_to_no_verified_number_is_refused_before_it(self):
        before = _alert_rows(self.hh.tid)
        from oikonome.web import security
        security._limiter._hits.clear()
        r = self.hh.client.post("/api/notify/test", json={"channel": "sms"})
        self.assertIn(r.status_code, (400, 402), r.text)
        self.assertEqual(_alert_rows(self.hh.tid), before)

    def test_a_push_with_nowhere_to_go_is_refused_before_it(self):
        before = _alert_rows(self.hh.tid)
        from oikonome.web import security
        security._limiter._hits.clear()
        r = self.hh.client.post("/api/notify/test", json={"channel": "push"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("no live push subscription", r.text)
        self.assertEqual(_alert_rows(self.hh.tid), before)


if __name__ == "__main__":
    unittest.main()
