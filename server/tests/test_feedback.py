"""Feedback pipeline: number format, package contents,
email vs download delivery, SMTP-failure fallback, log ring buffer."""

import io
import logging
import os
import unittest
import zipfile
from unittest import mock

from oikonome.web import feedback

from .util import make_db


def _tid(conn):
    return str(conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.tid = _tid(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_number_format(self):
        self.assertRegex(feedback.new_number(), r"^FB-\d{8}-[0-9a-f]{4}$")

    def test_package_contents(self):
        # a captured log line should ship with the message.
        # _single_tenant_instance is forced True because the SUITE's database
        # holds a tenant per test — this asserts the single-tenant self-host
        # case, which is the only one that ever ships the process-wide ring
        # (FeedbackLogGateTests owns the multi-tenant + hosted cases).
        feedback.install_ring_logger()
        logging.getLogger("oikonome.test").warning("ring-buffer-marker")
        with mock.patch.object(feedback, "_single_tenant_instance",
                               return_value=True):
            pkg = feedback.build_package(
                self.conn, self.tid, "FB-20260713-abcd",
                "the chart is upside down",
                screenshot=("screenshot.png", b"\x89PNG fake"))
        with zipfile.ZipFile(io.BytesIO(pkg)) as z:
            names = z.namelist()
            self.assertIn("FB-20260713-abcd/feedback.md", names)
            self.assertIn("FB-20260713-abcd/doctor-bundle.json", names)
            self.assertIn("FB-20260713-abcd/app-logs.txt", names)
            self.assertIn("FB-20260713-abcd/screenshot.png", names)
            md = z.read("FB-20260713-abcd/feedback.md").decode()
            self.assertIn("the chart is upside down", md)
            logs = z.read("FB-20260713-abcd/app-logs.txt").decode()
            self.assertIn("ring-buffer-marker", logs)

    def test_no_smtp_downloads(self):
        with mock.patch.dict("os.environ", {"OIKONOME_SMTP_HOST": ""}):
            r = feedback.submit(self.conn, self.tid, "broken thing")
        self.assertEqual(r["delivery"], "downloaded")
        self.assertIsNotNone(r["package"])
        row = self.conn.execute(
            "SELECT delivery, message FROM feedback_reports WHERE number=%s",
            (r["number"],)).fetchone()
        self.assertEqual(row["delivery"], "downloaded")
        self.assertEqual(row["message"], "broken thing")

    def test_smtp_emails_with_attachment(self):
        with mock.patch.dict("os.environ",
                             {"OIKONOME_SMTP_HOST": "smtp.test",
                              "OIKONOME_FEEDBACK_TO": "feedback@example.org"}), \
             mock.patch("oikonome.web.report.send") as send:
            r = feedback.submit(self.conn, self.tid, "broken thing")
        self.assertEqual(r["delivery"], "emailed")
        self.assertIsNone(r["package"])
        send.assert_called_once()
        self.assertIn(r["number"], send.call_args.args[0])       # subject
        self.assertEqual(send.call_args.args[3],
                         ["feedback@example.org"])
        # addressed to the inbox — the To:sender+Bcc shape delivered the
        # bundle to the sending mailbox as well
        self.assertIs(send.call_args.kwargs.get("bcc"), False)
        (name, data, mime), = send.call_args.kwargs["attachments"]
        self.assertEqual(name, f"{r['number']}.zip")
        self.assertEqual(mime, "application/zip")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertTrue(any(n.endswith("feedback.md")
                                for n in z.namelist()))

    def test_smtp_failure_falls_back_to_download(self):
        with mock.patch.dict("os.environ",
                             {"OIKONOME_SMTP_HOST": "smtp.test"}), \
             mock.patch("oikonome.web.report.send",
                        side_effect=OSError("boom")):
            r = feedback.submit(self.conn, self.tid, "still broken")
        self.assertEqual(r["delivery"], "downloaded")
        self.assertIsNotNone(r["package"])

    def test_feedback_to_env_override_and_empty(self):
        with mock.patch.dict("os.environ", {"OIKONOME_FEEDBACK_TO": ""}):
            self.assertEqual(feedback.feedback_to(), "")
        with mock.patch.dict("os.environ",
                             {"OIKONOME_FEEDBACK_TO": "me@x.dev"}):
            self.assertEqual(feedback.feedback_to(), "me@x.dev")




class FeedbackLogGateTests(unittest.TestCase):
    """The app-log ring was gated on OIKONOME_HOSTED
    alone, which encodes 'self-host ⇒ single tenant'.

    That is an assumption, not a fact. A household self-hosting with separate
    logins — or any operator who accepted a second signup — is multi-tenant
    with the flag unset, and the process-wide ring shipped every tenant's log
    lines to whoever clicked Feedback. The gate now asks how many tenants
    actually exist, and fails CLOSED: a support bundle missing logs is an
    inconvenience, one carrying a stranger's email is an incident.
    """

    def setUp(self):
        self._saved = os.environ.get("OIKONOME_HOSTED")
        os.environ.pop("OIKONOME_HOSTED", None)

    def tearDown(self):
        os.environ.pop("OIKONOME_HOSTED", None)
        if self._saved is not None:
            os.environ["OIKONOME_HOSTED"] = self._saved

    def test_hosted_never_includes_logs(self):
        from oikonome.web import feedback
        os.environ["OIKONOME_HOSTED"] = "1"
        self.assertFalse(feedback._single_tenant_instance())

    def test_gate_counts_real_tenants(self):
        """The case the old gate got wrong: self-host, flag unset, two
        tenants — logs must be withheld."""
        import inspect

        from oikonome.web import feedback
        src = inspect.getsource(feedback._single_tenant_instance)
        self.assertIn("FROM tenants", src)
        self.assertIn("<= 1", src)

        class _Row(dict):
            pass

        class _Admin:
            def execute(self, *a, **k):
                class R:
                    def fetchone(self_inner):
                        return {"n": 2}
                return R()

            def close(self):
                pass

        with mock.patch("oikonome.db.tenancy.admin_connect",
                        return_value=_Admin()):
            self.assertFalse(feedback._single_tenant_instance())
        class _Admin1(_Admin):
            def execute(self, *a, **k):
                class R:
                    def fetchone(self_inner):
                        return {"n": 1}
                return R()

        with mock.patch("oikonome.db.tenancy.admin_connect",
                        return_value=_Admin1()):
            self.assertTrue(feedback._single_tenant_instance())

    def test_unknown_tenant_count_fails_closed(self):
        from unittest import mock

        from oikonome.web import feedback
        with mock.patch("oikonome.db.tenancy.admin_connect",
                        side_effect=RuntimeError("db down")):
            self.assertFalse(feedback._single_tenant_instance())


class LogRedactionTests(unittest.TestCase):
    """Captured log lines are scrubbed of secret shapes — tokens,
    bearer headers, api keys, DSN passwords, envelope ciphertext —
    before a feedback bundle can carry them."""

    def test_secret_shapes_are_redacted(self):
        cases = {
            "GET /setup?token=abc123def": "<redacted>",
            "user link /invite/QQQlongtoken12345": "<redacted>",
            "Authorization: Bearer sk-live-9999": "<redacted>",
            "saved api_key=supersecretvalue done": "<redacted>",
            "dsn postgres://u:pw@host/db": "<redacted>@",
            "stored enc:v1:Z0FBQUFBQ": "enc:v1:<redacted>",
        }
        for line, marker in cases.items():
            r = feedback._redact(line)
            self.assertIn(marker, r, line)
            # the raw secret must be gone
        self.assertNotIn("supersecretvalue",
                         feedback._redact("api_key=supersecretvalue"))
        self.assertNotIn("sk-live-9999",
                         feedback._redact("Authorization: Bearer sk-live-9999"))
        # ordinary lines survive untouched
        self.assertEqual(feedback._redact("worker synced 3 items"),
                         "worker synced 3 items")


if __name__ == "__main__":
    unittest.main()
