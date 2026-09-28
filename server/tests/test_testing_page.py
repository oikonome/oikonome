"""Feedback pipeline via the SPA's JSON API: state endpoint, zip/email
delivery, and the redirect of the retired checklist page's URL."""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config


class FeedbackPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"test-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def test_legacy_page_redirects_into_the_spa(self):
        # the retired checklist's URL and old bookmarks land on Feedback
        r = self.client.get("/testing", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/app/feedback")

    def test_state_endpoint(self):
        d = self.client.get("/api/testing").json()
        self.assertTrue(d["enabled"])
        self.assertIn("can_email", d)
        self.assertIn("feedback_to", d)
        # the checklist is gone — its keys must not resurface
        self.assertNotIn("steps", d)
        self.assertNotIn("saved", d)

    def test_private_inbox_stays_on_the_server_when_it_emails(self):
        # any signed-in user can read this endpoint; the operator's own
        # inbox override is shown only when the person must send the zip
        # by hand (no SMTP), never when the server does the delivery
        from unittest import mock
        with mock.patch.dict("os.environ",
                             {"OIKONOME_FEEDBACK_TO": "me@private.example"}):
            with mock.patch("oikonome.web.feedback.smtp_configured",
                            return_value=True):
                d = self.client.get("/api/testing").json()
                self.assertTrue(d["can_email"])
                self.assertEqual(d["feedback_to"], "")
            with mock.patch("oikonome.web.feedback.smtp_configured",
                            return_value=False):
                d = self.client.get("/api/testing").json()
                self.assertEqual(d["feedback_to"], "me@private.example")
        # no inbox named: SMTP alone cannot deliver, and there is no address
        # to hand the person either
        with mock.patch.dict("os.environ", {"OIKONOME_FEEDBACK_TO": ""}):
            with mock.patch("oikonome.web.feedback.smtp_configured",
                            return_value=True):
                d = self.client.get("/api/testing").json()
                self.assertFalse(d["can_email"])
                self.assertEqual(d["feedback_to"], "")

    def test_checklist_endpoints_are_gone(self):
        r = self.client.post("/api/testing/step",
                             json={"step": "setup", "status": "pass",
                                   "note": "x"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.client.get("/testing/report",
                                         follow_redirects=False).status_code,
                         404)

    def test_feedback_api_downloads_zip_without_smtp(self):
        from unittest import mock
        with mock.patch.dict("os.environ", {"OIKONOME_SMTP_HOST": ""}):
            r = self.client.post("/api/testing/feedback",
                                 data={"message": "the chart is upside down"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "application/zip")
        self.assertRegex(r.headers["x-feedback-number"],
                         r"^FB-\d{8}-[0-9a-f]{4}$")

    def test_feedback_api_emails_with_smtp(self):
        from unittest import mock
        with mock.patch.dict("os.environ",
                             {"OIKONOME_SMTP_HOST": "smtp.test",
                              "OIKONOME_FEEDBACK_TO": "feedback@example.org"}), \
             mock.patch("oikonome.web.report.send"):
            r = self.client.post("/api/testing/feedback",
                                 data={"message": "still upside down"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["delivery"], "emailed")
        self.assertRegex(body["number"], r"^FB-\d{8}-[0-9a-f]{4}$")

    def test_bug_kind_rides_in_the_subject_and_heads_the_report(self):
        """One form, two kinds: a bug report must be sortable from the
        inbox subject alone and readable as one from the package's first
        heading, so the operator never has to guess which they hold."""
        import io
        import zipfile
        from unittest import mock
        with mock.patch.dict("os.environ",
                             {"OIKONOME_SMTP_HOST": "smtp.test",
                              "OIKONOME_FEEDBACK_TO": "feedback@example.org"}), \
             mock.patch("oikonome.web.report.send") as send:
            r = self.client.post("/api/testing/feedback",
                                 data={"message": "What happened:\nit broke",
                                       "kind": "bug"})
        self.assertEqual(r.status_code, 200)
        number = r.json()["number"]
        subject = send.call_args.args[0]
        self.assertEqual(subject, f"[Oikonome] [bug] {number}")
        self.assertIn("bug report", send.call_args.args[1])
        name, package, ctype = send.call_args.kwargs["attachments"][0]
        md = zipfile.ZipFile(io.BytesIO(package)).read(
            f"{number}/feedback.md").decode()
        self.assertIn("## Bug report", md)
        self.assertIn("kind: bug", md)
        self.assertIn("it broke", md)

    def test_default_kind_is_feedback_for_clients_that_send_none(self):
        import io
        import zipfile
        from unittest import mock
        with mock.patch.dict("os.environ",
                             {"OIKONOME_SMTP_HOST": "smtp.test",
                              "OIKONOME_FEEDBACK_TO": "feedback@example.org"}), \
             mock.patch("oikonome.web.report.send") as send:
            r = self.client.post("/api/testing/feedback",
                                 data={"message": "nice chart"})
        number = r.json()["number"]
        self.assertEqual(send.call_args.args[0],
                         f"[Oikonome] [feedback] {number}")
        name, package, _ = send.call_args.kwargs["attachments"][0]
        md = zipfile.ZipFile(io.BytesIO(package)).read(
            f"{number}/feedback.md").decode()
        self.assertIn("## Feedback", md)

    def test_unknown_kind_is_refused(self):
        r = self.client.post("/api/testing/feedback",
                             data={"message": "x", "kind": "rant"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
