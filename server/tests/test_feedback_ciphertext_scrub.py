"""The feedback-bundle scrub knows BOTH ciphertext prefixes: the tenant
envelope's (enc:v1:) and the control-plane one (enc:cp1: — TOTP seeds
under the master key). Knowing only the first, a log line carrying a cp1
token would ship to support verbatim. Synthetic values only."""

import unittest

from oikonome.web.feedback import _redact


class FeedbackScrubCiphertextTests(unittest.TestCase):
    def test_control_plane_ciphertext_is_scrubbed(self):
        line = ("totp update failed for user x: "
                "enc:cp1:Z0FBQUFBQm9TWU5USJHNfaKE9lZz09= (retrying)")
        out = _redact(line)
        self.assertNotIn("Z0FBQUFBQm9TWU5U", out)
        self.assertIn("enc:cp1:<redacted>", out)
        self.assertIn("(retrying)", out)

    def test_tenant_ciphertext_still_scrubbed(self):
        out = _redact("stored enc:v1:c3ludGhldGljLXRva2Vu= ok")
        self.assertIn("enc:v1:<redacted>", out)
        self.assertNotIn("c3ludGhldGljLXRva2Vu", out)

    def test_plain_lines_untouched(self):
        line = "sync ok: 42 rows, encoder=utf-8, encrypted-at-rest"
        self.assertEqual(_redact(line), line)


if __name__ == "__main__":
    unittest.main()
