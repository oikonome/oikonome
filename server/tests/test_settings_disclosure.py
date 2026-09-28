"""Hosted /api/settings withholds provider key material it must not echo.

* hosted /api/settings withholds mx_api_key_set, like every other
  provider key field.
* non-ASCII input answers 403/401, never a 500, at the pre-auth
  setup door and the TOTP verify.
* email_recipients of the wrong JSON type 400s instead of an
  AttributeError-500 out of parse_recipients.
* the export's formula-neutralizing apostrophe is reversed on
  restore, so a '-ACH descriptor round-trips byte-identical.
* receipt_items cascade with their receipt.
"""

import io
import os
import unittest
import uuid
import zipfile
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.sync import restore

from .export_ticket import export_get
from .util import _ensure_db, make_db, seed_accounts, write_config

PW = "correct-horse-battery"


class SettingsAndGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app
        cls.client = TestClient(cls.app)
        cls.client.post("/api/signup", data={
            "email": f"settings-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})

    def test_hosted_settings_withholds_mx_api_key_set(self):
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            data = self.client.get("/api/settings").json()
        self.assertNotIn("mx_api_key_set", data,
                         "mx_api_key_set must be withheld on hosted")
        # self-host still gets it
        data = self.client.get("/api/settings").json()
        self.assertIn("mx_api_key_set", data)

    def test_wrong_typed_email_recipients_is_a_400_not_a_500(self):
        for bad in ({"a": "b"}, 5, True):
            r = self.client.post("/api/settings",
                                 json={"email_recipients": bad})
            self.assertEqual(r.status_code, 400, f"payload {bad!r}")

    def test_non_ascii_setup_token_is_a_403_not_a_500(self):
        """compare_digest raises TypeError on non-ASCII str — the check
        must answer 403, never explode (unit-level: the /setup route only
        consults the token while the instance is unclaimed)."""
        from fastapi import HTTPException

        from oikonome.web.app import _setup_token_check

        class _Req:
            query_params = {"token": "café"}
        with mock.patch.dict(os.environ, {"OIKONOME_SETUP_TOKEN": "tok-x"}):
            with self.assertRaises(HTTPException) as ctx:
                _setup_token_check(_Req(), "")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_non_ascii_totp_code_is_wrong_not_an_error(self):
        from oikonome.auth import totp
        s = totp.new_secret()
        self.assertFalse(totp.verify(s, "cafééé"))
        self.assertIsNone(totp.verify_used(s, "cafééé", None))

    def test_non_ascii_webhook_credentials_are_a_401_not_a_500(self):
        """The unauthenticated Postmark webhook fed non-ASCII
        (or U+FFFD manufactured by its own lenient base64 decode) into
        compare_digest — TypeError, 500 per request at 120/min/IP."""
        import base64
        with mock.patch.dict(os.environ,
                             {"OIKONOME_EMAIL_WEBHOOK_TOKEN": "tok-x"}):
            r = self.client.post("/api/email/webhook?token=caf%C3%A9",
                                 json={})
            self.assertEqual(r.status_code, 401)
            bad = base64.b64encode("café:mot-de-passe".encode()).decode()
            r = self.client.post("/api/email/webhook", json={},
                                 headers={"Authorization": f"Basic {bad}"})
            self.assertEqual(r.status_code, 401)



class RestoreUnescapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_formula_escaped_descriptor_round_trips_identically(self):
        """The export writes '-ACH DEBIT as "'-ACH DEBIT" (the
        Excel guard); restore must strip it back or exact-string merchant
        rules stop matching after every migration."""
        os.environ["OIKONOME_DEV"] = "1"
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        client = TestClient(appmod.app)
        client.post("/api/signup", data={
            "email": f"escape-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        tid = client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            conn.execute(
                """INSERT INTO transactions (id, account_id, date, amount,
                       name, merchant_name, pending, removed)
                   VALUES ('esc-1','chk','2026-07-10',25.0,
                           '-ACH DEBIT INTERNET TRANSFER',
                           '@Home Depot',0,0)""")
        finally:
            conn.close()
        r = export_get(client, "/export")
        self.assertEqual(r.status_code, 200)
        # the ZIP cell itself is escaped (the Excel half still works)
        txns_csv = zipfile.ZipFile(io.BytesIO(r.content)) \
            .read("transactions.csv").decode()
        self.assertIn("'-ACH DEBIT", txns_csv)
        dest = make_db()
        try:
            restore.restore_zip(dest, r.content)
            row = dest.execute(
                "SELECT name, merchant_name FROM transactions "
                "WHERE id='esc-1'").fetchone()
            self.assertEqual(row["name"], "-ACH DEBIT INTERNET TRANSFER",
                             "the export's guard apostrophe stuck to the "
                             "restored ledger text")
            self.assertEqual(row["merchant_name"], "@Home Depot")
        finally:
            dest.close()


class ReceiptItemsCascadeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_deleting_the_transaction_takes_the_line_items_too(self):
        """receipt_items cascade off receipts, so deleting a transaction
        leaves no stranded line items."""
        conn = make_db()
        rid = str(uuid.uuid4())
        try:
            conn.execute(
                """INSERT INTO transactions (id, account_id, date, amount,
                       name, pending, removed)
                   VALUES ('rc-1','chk','2026-07-10',60.0,'STORE',0,0)""")
            conn.execute(
                """INSERT INTO receipts (id, txn_id, image, mime)
                   VALUES (%s,'rc-1',%s,'image/jpeg')""",
                (rid, b"\xff\xd8fake"))
            conn.execute(
                """INSERT INTO receipt_items (receipt_id, line, description)
                   VALUES (%s, 1, 'widget')""", (rid,))
            conn.execute("DELETE FROM transactions WHERE id='rc-1'")
            left = conn.execute(
                "SELECT count(*) AS n FROM receipt_items "
                "WHERE receipt_id=%s", (rid,)).fetchone()["n"]
            self.assertEqual(left, 0,
                             "line items survived their receipt's cascade")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
