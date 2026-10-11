"""The user docs describe what the server's doors actually do.

Each check pins a sentence against the behaviour it describes, so the two
cannot drift apart again: a doc that tells an owner a rotation covers
everything while it silently pauses their texts, or that a document never
leaves the instance while it goes to a remote model, is a promise the code
does not keep.
"""

import pathlib
import re
import unittest

DOCS = pathlib.Path(__file__).resolve().parents[2] / "docs"


def _flat(path: str) -> str:
    return re.sub(r"\s+", " ", (DOCS / path).read_text())


class SecurityGuideTests(unittest.TestCase):
    def test_a_rotation_says_it_pauses_the_text_number(self):
        # password_change / email_change / the 2FA doors call
        # auth.reset._unverify_sms_number for an owner
        s = _flat("guides/security.md")
        self.assertIn("text-message number is paused", s)
        self.assertIn("Removing a paused number needs no code", s)

    def test_the_step_up_list_names_the_new_destinations(self):
        # notify_phone_api and push_subscribe_api both require elevation
        s = _flat("guides/security.md")
        rule = s[s.index("## The step-up rule"):s.index("You prove it")]
        self.assertIn("text-message number", rule)
        self.assertIn("notifications in a browser", rule)


class ScriptTokenDocTests(unittest.TestCase):
    def test_tokens_are_not_said_to_survive_a_password_change(self):
        s = _flat("community-scripts.md")
        self.assertNotIn("survive password changes", s)
        self.assertIn("do **not** survive a credential change", s)


class TaxDocumentDocTests(unittest.TestCase):
    def test_remote_reading_is_disclosed(self):
        s = _flat("tax-documents.md")
        self.assertNotIn("nothing is sent anywhere", s)
        self.assertIn("Social Security number included", s)
        self.assertNotIn("read by the **chat model**", s)


class DeleteDocTests(unittest.TestCase):
    def test_kept_feed_rows_and_the_question_are_documented(self):
        self.assertIn("Delete anyway", _flat("guides/accounts.md"))
        self.assertIn("Undo anyway", _flat("guides/import-history.md"))


if __name__ == "__main__":
    unittest.main()
