"""Settings → Email: the recipient editor has to say what happened.

Four ways it can fail to. An over-long list truncated by the save reports
success while the address the person just typed is gone and still on their
screen. A server refusal rendered as the raw error string — status code,
JSON braces and all — lands in the SAME amber note that says "Saved." A
Resend button with no error path makes the 10/hour limit (exactly what a
second click hits) a silent no-op, and one pending resend disables every
row's button. And an add-address box with no accessible name is invisible
to a screen reader.
"""

import pathlib
import unittest

import oikonome
from oikonome.web import mailguard


def _src(rel: str) -> str:
    return (pathlib.Path(oikonome.__file__).parent.parent.parent
            / "webapp" / "src" / rel).read_text()


class RecipientCapTests(unittest.TestCase):

    def test_parse_still_caps_by_default(self):
        """The cap guards the invite fan-out; it stays."""
        many = [f"r{i}@example.dev" for i in range(mailguard.MAX_RECIPIENTS + 5)]
        self.assertEqual(len(mailguard.parse_recipients(many)),
                         mailguard.MAX_RECIPIENTS)

    def test_the_uncapped_read_is_available_for_counting(self):
        many = [f"r{i}@example.dev" for i in range(mailguard.MAX_RECIPIENTS + 5)]
        self.assertEqual(len(mailguard.parse_recipients(many, cap=None)),
                         mailguard.MAX_RECIPIENTS + 5)

    def test_it_still_dedupes_when_uncapped(self):
        got = mailguard.parse_recipients(
            ["a@example.dev", "A@Example.dev", "b@example.dev"], cap=None)
        self.assertEqual(got, ["a@example.dev", "b@example.dev"])

    def test_the_settings_door_refuses_instead_of_truncating(self):
        api = (pathlib.Path(oikonome.__file__).parent / "web"
               / "api.py").read_text()
        self.assertIn("cap=None", api)
        self.assertIn("the \"\n                             f\"limit is", api)
        self.assertIn("Remove ", api)


class RecipientEditorFeedbackTests(unittest.TestCase):
    def setUp(self):
        try:
            self.src = _src("pages/Settings.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_a_server_error_is_shown_as_a_sentence(self):
        self.assertIn("errText(e)", self.src)
        self.assertIn("export function errText", _src("api/client.ts"))

    def test_a_failure_and_a_success_do_not_look_the_same(self):
        self.assertIn('"note" + (notice.bad ? " bad" : " good")', self.src)

    def test_resend_reports_both_outcomes(self):
        self.assertIn("onError: (e) => setResendMsg(", self.src)
        self.assertIn("Invitation re-sent to", self.src)

    def test_one_resend_does_not_disable_every_row(self):
        self.assertIn("disabled={resending === r.email}", self.src)
        self.assertNotIn("disabled={resend.isPending}", self.src)

    def test_the_add_box_has_a_name(self):
        self.assertIn('aria-label="Add an email recipient"', self.src)
