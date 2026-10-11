"""A restore or a connections bundle never lifts, plants or sidesteps a
credential rotation's outbound pause.

After an owner's credential rotates, the mail relay, extra recipients and
AI endpoint stand down until the owner presses Resume, which asks for a
step-up. The pause record is this instance's own decision:

* an archive never carries it out, and one that carries it in is ignored —
  otherwise a record naming nothing lifts the pause without Resume, and
  an export taken during a pause plants a stale one elsewhere;
* while a pause is armed, an outbound setting an import brings in is
  paused with the rest — otherwise a relay, recipient or AI endpoint the
  record never named sends at once while the confirmed ones stay held;
* an import that brings no relay or AI endpoint in pauses none — the
  destination's own, saved by the owner after the pause, stays working
  rather than going silent until Resume;
* the owner's own address is never one of the recipients a pause holds.
"""

import csv
import io
import json
import os
import unittest
import uuid
import zipfile

from cryptography.fernet import Fernet

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.notify import outbound_pause
from oikonome.sync import connections_bundle as cb
from oikonome.sync import export, restore

from .util import make_db, write_config

PAUSE = {"at": "2031-05-01T10:00:00+00:00", "reason": "rotation",
         "smtp": True, "recipients": ["old@example.com"], "llm": False}


def _settings_zip(cfg: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=["config"])
        w.writeheader()
        w.writerow({"config": json.dumps(cfg)})
        z.writestr("tenant_settings.csv", s.getvalue())
    return buf.getvalue()


class RestoreKeepsThePauseTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def _paused(self):
        return outbound_pause.paused(budget.load_config(self.conn))

    def test_an_export_never_carries_the_pause(self):
        write_config(self.conn, smtp_host="mail.example.com",
                     outbound_paused=PAUSE)
        z = zipfile.ZipFile(io.BytesIO(export.build_zip(self.conn)))
        self.assertNotIn(b"outbound_paused", z.read("tenant_settings.csv"))

    def test_an_archive_cannot_lift_an_armed_pause(self):
        write_config(self.conn, smtp_host="mail.example.com",
                     outbound_paused=PAUSE)
        restore.restore_zip(self.conn, _settings_zip(
            {"food_monthly": 900,
             "outbound_paused": {"smtp": False, "recipients": [],
                                 "llm": False}}))
        p = self._paused()
        self.assertIsNotNone(p)
        self.assertTrue(p["smtp"])
        self.assertEqual(p["recipients"], ["old@example.com"])
        self.assertEqual(budget.load_config(self.conn)["food_monthly"], 900)

    def test_an_archive_cannot_plant_a_pause(self):
        write_config(self.conn)
        restore.restore_zip(self.conn, _settings_zip(
            {"food_monthly": 900, "outbound_paused": PAUSE}))
        self.assertIsNone(self._paused())

    def test_what_a_restore_brings_in_during_a_pause_is_paused_too(self):
        write_config(self.conn, smtp_host="mail.example.com",
                     email_recipients=["old@example.com"],
                     outbound_paused=PAUSE)
        restore.restore_zip(self.conn, _settings_zip(
            {"email_recipients": ["old@example.com", "new@example.com"],
             "llm_url": "https://203.0.113.9/v1/chat/completions"}))
        p = self._paused()
        self.assertEqual(p["recipients"],
                         ["new@example.com", "old@example.com"])
        self.assertTrue(p["llm"])
        # still the rotation the owner has to answer
        self.assertEqual(p["at"], PAUSE["at"])
        self.assertEqual(p["reason"], "rotation")

    def test_a_restore_without_a_relay_leaves_the_owners_own_running(self):
        # the pause came while no relay or endpoint was set; the owner has
        # since saved their real ones, which the pause never held
        write_config(self.conn, smtp_host="mail.example.com",
                     smtp_from="me@example.com",
                     llm_url="https://203.0.113.9/v1/chat/completions",
                     email_recipients=["old@example.com"],
                     outbound_paused={**PAUSE, "smtp": False})
        restore.restore_zip(self.conn, _settings_zip({"food_monthly": 900}))
        p = self._paused()
        self.assertFalse(p["smtp"])
        self.assertFalse(p["llm"])
        self.assertEqual(p["recipients"], ["old@example.com"])

    def test_a_restore_that_changes_the_relay_pauses_it(self):
        write_config(self.conn, smtp_host="mail.example.com",
                     email_recipients=["old@example.com"],
                     outbound_paused={**PAUSE, "smtp": False})
        restore.restore_zip(self.conn, _settings_zip(
            {"smtp_host": "relay.elsewhere.example"}))
        self.assertTrue(self._paused()["smtp"])

    def test_a_restore_never_pauses_the_owners_own_address(self):
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        owner = f"owner-{uuid.uuid4().hex[:8]}@example.com"
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s, %s, 'x', 'owner')", (tid, owner))
        finally:
            admin.close()
        write_config(self.conn, email_recipients=["old@example.com"],
                     outbound_paused=PAUSE)
        restore.restore_zip(self.conn, _settings_zip(
            {"email_recipients": ["old@example.com", owner.upper(),
                                  "new@example.com"]}))
        self.assertEqual(self._paused()["recipients"],
                         ["new@example.com", "old@example.com"])

    def test_without_a_pause_a_restore_pauses_nothing(self):
        write_config(self.conn)
        restore.restore_zip(self.conn, _settings_zip(
            {"email_recipients": ["new@example.com"]}))
        self.assertIsNone(self._paused())


class BundleKeepsThePauseTests(unittest.TestCase):

    def tearDown(self):
        os.environ.pop("OIKONOME_MASTER_KEY", None)

    def _db(self):
        conn = make_db()
        self.addCleanup(conn.close)
        return conn

    def test_a_relay_and_endpoint_imported_during_a_pause_are_paused(self):
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        src = self._db()
        write_config(src, smtp_host="mail.example.com", smtp_port=587,
                     llm_url="https://203.0.113.9/v1/chat/completions")
        blob = cb.export_bytes(src, "correct horse battery")
        dest = self._db()
        write_config(dest, email_recipients=["old@example.com"],
                     outbound_paused={**PAUSE, "smtp": False})
        cb.import_bytes(dest, blob, "correct horse battery")
        p = outbound_pause.paused(budget.load_config(dest))
        self.assertTrue(p["smtp"])
        self.assertTrue(p["llm"])
        self.assertEqual(p["recipients"], ["old@example.com"])
        self.assertEqual(p["at"], PAUSE["at"])

    def test_a_bundle_without_a_relay_leaves_the_owners_own_running(self):
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        src = self._db()
        write_config(src, food_monthly=900)
        blob = cb.export_bytes(src, "correct horse battery")
        dest = self._db()
        write_config(dest, smtp_host="mail.example.com",
                     llm_url="https://203.0.113.9/v1/chat/completions",
                     email_recipients=["old@example.com"],
                     outbound_paused={**PAUSE, "smtp": False})
        cb.import_bytes(dest, blob, "correct horse battery")
        p = outbound_pause.paused(budget.load_config(dest))
        self.assertFalse(p["smtp"])
        self.assertFalse(p["llm"])


if __name__ == "__main__":
    unittest.main()
