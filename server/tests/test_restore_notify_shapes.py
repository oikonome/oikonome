"""A restore ZIP's notification settings arrive in the shapes the settings
door would have written, or not at all.

The worker reads these every hour. Unchecked, an hour of "x" would raise
out of the due-check and silence every cadence; a recipient list written as
one string would be iterated letter by letter; a phone that is not an object
would crash the send.
"""

import csv
import datetime as dt
import io
import json
import unittest
import zipfile
from unittest import mock

from oikonome.engine import budget
from oikonome.jobs import worker
from oikonome.sync import restore

from .util import make_db, write_config


def _zip(config: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=["config"])
        w.writeheader()
        w.writerow({"config": json.dumps(config)})
        z.writestr("tenant_settings.csv", s.getvalue())
    return buf.getvalue()


class NormalizeTests(unittest.TestCase):
    def test_malformed_cadence_is_dropped_and_noted(self):
        cfg = {"email_schedule": {"daily": {"on": True, "hour": "x"},
                                  "weekly": {"on": True, "hour": 8,
                                             "weekday": 3, "sms": 1}}}
        notes = restore.normalize_notify_shapes(cfg)
        self.assertEqual(cfg["email_schedule"], {
            "weekly": {"on": True, "hour": 8, "weekday": 3, "sms": True}})
        self.assertTrue(any("daily" in n for n in notes))

    def test_a_stray_weekday_on_a_daily_entry_is_ignored(self):
        cfg = {"email_schedule": {"daily": {"on": True, "hour": 8,
                                            "weekday": 99}}}
        notes = restore.normalize_notify_shapes(cfg)
        self.assertEqual(cfg["email_schedule"], {"daily": {"on": True,
                                                            "hour": 8}})
        self.assertEqual(notes, [])

    def test_muted_list_is_strings_only_and_capped(self):
        from oikonome.web.mailguard import MAX_RECIPIENTS
        cfg = {"email_muted": [f"m{i}@example.dev" for i in range(500)]
               + [7, " "]}
        restore.normalize_notify_shapes(cfg)
        self.assertEqual(len(cfg["email_muted"]), MAX_RECIPIENTS)
        self.assertTrue(all(isinstance(m, str) for m in cfg["email_muted"]))

    def test_every_cadence_malformed_removes_the_schedule(self):
        cfg = {"email_schedule": {"daily": {"on": True, "hour": 99}}}
        restore.normalize_notify_shapes(cfg)
        self.assertNotIn("email_schedule", cfg)

    def test_recipient_string_becomes_a_list(self):
        cfg = {"email_recipients": "a@example.dev, b@example.dev"}
        restore.normalize_notify_shapes(cfg)
        self.assertEqual(cfg["email_recipients"],
                         ["a@example.dev", "b@example.dev"])

    def test_non_object_phone_and_bad_legacy_hour_are_dropped(self):
        cfg = {"notify_phone": "555", "notify_phone_pending": [1],
               "email_send_hour_utc": "noon", "email_muted": "x"}
        restore.normalize_notify_shapes(cfg)
        self.assertEqual(cfg, {})

    def test_legacy_hour_is_clamped(self):
        cfg = {"email_send_hour_utc": 40}
        restore.normalize_notify_shapes(cfg)
        self.assertEqual(cfg["email_send_hour_utc"], 23)


class RestoreThenSweepTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_restored_bad_schedule_never_silences_the_sweep(self):
        report = restore.restore_zip(self.conn, _zip({
            "email_schedule": {"daily": {"on": True, "hour": "x"},
                               "weekly": {"on": True, "hour": 7,
                                          "weekday": 2}},
            "email_recipients": "p@example.dev",
            "notify_phone": "not-an-object"}))
        self.assertEqual(report.get("settings"), 1, report)
        cfg = budget.load_config(self.conn)
        self.assertNotIn("daily", cfg["email_schedule"])
        self.assertNotIn("notify_phone", cfg)
        with mock.patch.dict("os.environ", {"OIKONOME_TZ": "UTC"}):
            due = worker.emails_due(
                self.conn, dt.datetime(2026, 7, 15, 9, tzinfo=dt.timezone.utc))
        self.assertEqual(due, ["weekly"])


if __name__ == "__main__":
    unittest.main()
