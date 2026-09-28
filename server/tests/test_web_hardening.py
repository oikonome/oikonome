"""Web-layer hardening regressions: reset-token burn, redirect safety,
error-detail hygiene, and upload caps."""

import io
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config


class WebHardeningRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.email = f"sec2-{uuid.uuid4().hex[:8]}@example.dev"
        cls.client.post("/api/signup", data={
            "email": cls.email, "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def test_reset_token_burn_is_atomic_one_time(self):
        # own user: consume() revokes every session, which would sign out
        # the class-level client used by the other tests
        from fastapi.testclient import TestClient as TC

        from oikonome.auth import passwords, reset
        from oikonome.web.app import _control_conn
        email = f"burn-{uuid.uuid4().hex[:8]}@example.dev"
        TC(self.client.app).post("/api/signup", data={
            "email": email, "password": "correct-horse-battery"})
        with _control_conn() as conn:
            uid = conn.execute("SELECT id FROM users WHERE email=%s",
                               (email,)).fetchone()["id"]
            token = reset.create_reset(conn, uid)
            row = reset.lookup_reset(conn, token)
            h = passwords.hash_password("correct-horse-battery")
            self.assertTrue(reset.consume(conn, row["id"], row["user_id"], h))
            # second consume of the SAME row must refuse (concurrent submit)
            self.assertFalse(reset.consume(conn, row["id"], row["user_id"], h))

    def test_backslash_back_param_never_redirects_external(self):
        for route, data in (
                ("/transactions/recategorize",
                 {"txn_id": "x", "category": "", "back": "/\\evil.com"}),):
            r = self.client.post(route, data=data, follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertNotIn("evil.com", r.headers["location"])

    def test_simplefin_errors_are_generic(self):
        r = self.client.post("/api/accounts/simplefin",
                             json={"token": "!!!broken-base64!!!"})
        self.assertEqual(r.status_code, 400)
        detail = r.json()["detail"].lower()
        self.assertIn("connection failed", detail)
        # no exception class names / URLs / padding internals leak
        for needle in ("traceback", "error(", "http://", "https://",
                       "padding", "base64"):
            self.assertNotIn(needle, detail)

    def test_pdf_page_cap(self):
        from pypdf import PdfWriter

        from oikonome.sync import pdfimport
        w = PdfWriter()
        for _ in range(pdfimport.MAX_PAGES + 1):
            w.add_blank_page(width=200, height=200)
        buf = io.BytesIO()
        w.write(buf)
        with self.assertRaises(ValueError) as cm:
            pdfimport.extract_text(buf.getvalue())
        self.assertIn("pages", str(cm.exception))

    def test_oversize_upload_rejected_cleanly(self):
        # 50MB+1 of zeros streams through the cap and returns the error page
        big = b"0" * (50 * 1024 * 1024 + 1)
        r = self.client.post(
            "/import",
            files={"file": ("huge.csv", big, "text/csv")},
            data={"account_id": "chk"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("larger than 50", r.text)


if __name__ == "__main__":
    unittest.main()


class HtmlMailEscapingTests(unittest.TestCase):
    """Hand-built HTML mail bodies must not interpolate the recipient
    address raw: an address carrying markup would inject it into a message
    the recipient has every reason to trust. The shared helper means the
    next hand-built body starts escaped.
    """

    def test_escape_helper_neutralizes_markup(self):
        from oikonome.web.app import _html_escape
        self.assertEqual(
            _html_escape('x<img src=y onerror=alert(1)>@e.com'),
            'x&lt;img src=y onerror=alert(1)&gt;@e.com')
        self.assertEqual(_html_escape(None), "")

    def test_reset_and_invite_html_bodies_escape_the_address(self):
        """Source-level: the HTML bodies must route the address through the
        helper. Rendering them needs live SMTP, so assert the wiring.

        Targets the HTML fragments specifically — the PLAIN-text twins next
        to them must stay unescaped, or the recipient reads '&lt;' in a
        text/plain mail.
        """
        import inspect

        from oikonome.web import app as appmod
        src = inspect.getsource(appmod)
        # the reset and unlock mails no longer interpolate the address at
        # all — the mail is a link and one caution, nothing else — like the
        # welcome mail below, no user-supplied value reaches their HTML
        for name in ("_deliver_reset", "_deliver_unlock"):
            body = src.split(f"def {name}")[1].split("\ndef ")[0]
            self.assertNotIn("{email}", body)
            self.assertNotIn("{_html_escape(email)", body)
        # the account-ready mail does not interpolate the address at all —
        # it is three fixed sentences and the token link, so no
        # user-supplied value reaches its HTML, which is stronger than
        # escaping one. Guard against the address quietly returning raw.
        welcome = src.split("def _deliver_welcome")[1].split("\ndef ")[0]
        self.assertNotIn("{email}", welcome)
        self.assertNotIn("{_html_escape(email)", welcome)
        # and the plain-text bodies that DO carry an address stay unescaped
        self.assertIn('14 days, one use):\\n\\n{url}', src)
