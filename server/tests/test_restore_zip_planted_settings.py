"""A restore ZIP cannot plant AI endpoints, a verified SMS number, or an
off-list receipt.

The ZIP is attacker-shaped input (a hand-edited export, a script-token
push). Three doors it must not walk through:

  * `llm_backends` / `llm_roles` merged in raw: the next sync or receipt
    parse would post merchants, amounts and receipt images to whatever URL
    the list names. The .oikx bundle validates the list; the ZIP gets the
    identical rules.
  * `notify_phone {number, verified: true}` treated as a plain key: a ZIP
    could plant any number as verified and, with `email_schedule.*.sms`
    on, the next cadence would send the household's verdict there.
    Verification happens on this instance or not at all.
  * receipt rows storing any base64 under any mime — the mime is what the
    image route serves the bytes back as, and the size cap live uploads
    respect has to apply here too.
"""

import base64
import csv
import io
import json
import os
import unittest
import uuid
import zipfile
from unittest import mock

from oikonome.engine import budget, receipts
from oikonome.sync import restore

from .util import TODAY, add_txn, make_db, write_config

METADATA = "http://169.254.169.254/latest/meta-data/"


def _zip(config: dict | None = None, receipts_rows: list[dict] | None = None
         ) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if config is not None:
            s = io.StringIO()
            w = csv.DictWriter(s, fieldnames=["config"])
            w.writeheader()
            w.writerow({"config": json.dumps(config)})
            z.writestr("tenant_settings.csv", s.getvalue())
        if receipts_rows:
            s = io.StringIO()
            w = csv.DictWriter(s, fieldnames=["id", "txn_id", "image",
                                              "mime", "kind", "status"])
            w.writeheader()
            for r in receipts_rows:
                w.writerow(r)
            z.writestr("receipts.csv", s.getvalue())
    return buf.getvalue()


class PlantedBackendsTests(unittest.TestCase):

    def setUp(self):
        os.environ.pop("OIKONOME_HOSTED", None)
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def test_backend_at_blocked_url_never_lands(self):
        """The planted URL must not reach stored config — but one bad
        endpoint is not grounds to refuse a person's whole ledger, and the
        two cases are indistinguishable from here anyway: a self-hoster's
        own AI box on a private address looks exactly like an SSRF target.
        So it is DROPPED, reported, and the restore proceeds."""
        data = _zip(config={"llm_backends": [
            {"id": "evil", "url": METADATA, "model": "m"}]})
        counts = restore.restore_zip(self.conn, data)
        self.assertEqual(budget.load_config(self.conn)["llm_backends"], [])
        notes = " ".join(counts.get("_notes") or [])
        self.assertIn("evil", notes)
        self.assertIn("dropped", notes)

    def test_backend_list_is_normalized_and_capped(self):
        # too many entries
        many = [{"id": f"b{i}", "url": "https://x.example/v1", "model": "m"}
                for i in range(40)]
        with self.assertRaises(ValueError):
            restore.restore_zip(self.conn, _zip(config={"llm_backends": many}))
        # reserved id
        with self.assertRaises(ValueError):
            restore.restore_zip(self.conn, _zip(config={"llm_backends": [
                {"id": "bundled", "url": "https://x.example/v1",
                 "model": "m"}]}))
        # unknown fields do not ride through; unknown roles are dropped
        restore.restore_zip(self.conn, _zip(config={
            "llm_backends": [{"id": "ok", "url": "https://x.example/v1",
                              "model": "m", "headers": {"x": "y"}}],
            "llm_roles": {"categorize": "ok", "vision": "missing",
                          "not_a_role": "ok"}}))
        cfg = budget.load_config(self.conn)
        self.assertEqual([b["id"] for b in cfg["llm_backends"]], ["ok"])
        self.assertNotIn("headers", cfg["llm_backends"][0])
        self.assertEqual(cfg["llm_roles"], {"categorize": "ok"})


class PlantedPhoneTests(unittest.TestCase):

    def setUp(self):
        os.environ.pop("OIKONOME_HOSTED", None)
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def test_verified_number_in_zip_does_not_land(self):
        restore.restore_zip(self.conn, _zip(config={
            "notify_phone": {"number": "+15555550100", "verified": True},
            "notify_phone_pending": {"number": "+15555550100",
                                     "code_hash": "x"},
            "email_schedule": {"daily": {"on": True, "sms": True}}}))
        cfg = budget.load_config(self.conn)
        self.assertNotIn("notify_phone", cfg)
        self.assertNotIn("notify_phone_pending", cfg)
        # the toggle without a verified number here is disarmed
        self.assertFalse(cfg["email_schedule"]["daily"].get("sms"))
        self.assertTrue(cfg["email_schedule"]["daily"].get("on"))

    def test_zip_never_overwrites_a_number_verified_here(self):
        budget.save_config(self.conn, {
            "notify_phone": {"number": "+15555550123", "verified": True},
            "food_monthly": 1, "other_monthly": 1})
        restore.restore_zip(self.conn, _zip(config={
            "notify_phone": {"number": "+15555550100", "verified": True},
            "email_schedule": {"weekly": {"on": True, "sms": True}}}))
        cfg = budget.load_config(self.conn)
        self.assertEqual(cfg["notify_phone"]["number"], "+15555550123")
        # a live verified number keeps the toggle meaningful
        self.assertTrue(cfg["email_schedule"]["weekly"]["sms"])


class PlantedReceiptTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_txn(self.conn, TODAY, 12.0, "Store", txn_id="rz-1")
        self.addCleanup(self.conn.close)

    def _mimes(self):
        return {r["mime"] for r in self.conn.execute(
            "SELECT mime FROM receipts WHERE txn_id='rz-1'").fetchall()}

    def test_off_list_mime_and_oversize_rows_are_skipped(self):
        png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode()
        html = base64.b64encode(b"<html><body>sign in again</body></html>"
                                ).decode()
        big = base64.b64encode(b"0" * (receipts.MAX_IMAGE + 1)).decode()
        with mock.patch.object(restore, "MAX_MEMBER_BYTES", 64 * 1024 * 1024):
            restore.restore_zip(self.conn, _zip(receipts_rows=[
                {"id": str(uuid.uuid4()), "txn_id": "rz-1", "image": png,
                 "mime": "image/png", "kind": "receipt", "status": "uploaded"},
                {"id": str(uuid.uuid4()), "txn_id": "rz-1", "image": html,
                 "mime": "text/html", "kind": "receipt", "status": "uploaded"},
                {"id": str(uuid.uuid4()), "txn_id": "rz-1", "image": png,
                 "mime": "image/svg+xml", "kind": "receipt",
                 "status": "uploaded"},
                {"id": str(uuid.uuid4()), "txn_id": "rz-1", "image": big,
                 "mime": "image/jpeg", "kind": "receipt",
                 "status": "uploaded"},
            ]))
        self.assertEqual(self._mimes(), {"image/png"})

    def test_photo_sized_receipt_still_restores(self):
        """The size rule is the live upload's, not the csv module's default
        128 KB field limit — under that limit a real phone photo (hundreds
        of KB) fails the whole receipts member."""
        photo = base64.b64encode(b"\xff\xd8\xff" + b"7" * (600 * 1024)).decode()
        restore.restore_zip(self.conn, _zip(receipts_rows=[
            {"id": str(uuid.uuid4()), "txn_id": "rz-1", "image": photo,
             "mime": "image/jpeg", "kind": "receipt", "status": "uploaded"}]))
        self.assertEqual(self._mimes(), {"image/jpeg"})


if __name__ == "__main__":
    unittest.main()
