"""The write-gate allowlists (viewer read-only, hosted 2FA enrollment,
billing read-only) name endpoint FAMILIES. Matching must anchor at a
path-segment boundary: a route that merely shares a prefix's spelling
("/api/totp-foo" vs "/api/totp") must never inherit its exemption."""

import unittest

from oikonome.web.app import (BILLING_WRITE_OK, NEEDS_2FA_OK,
                              VIEWER_WRITE_OK, path_within)


class GatePrefixBoundaryTests(unittest.TestCase):
    def test_exact_and_nested_paths_are_members(self):
        for path, prefixes in [
                ("/api/logout", VIEWER_WRITE_OK),
                ("/api/password/change", VIEWER_WRITE_OK),
                ("/api/totp/enroll", VIEWER_WRITE_OK),
                ("/api/totp/confirm", NEEDS_2FA_OK),
                ("/api/totp/recovery-regenerate", NEEDS_2FA_OK),
                ("/api/testing", VIEWER_WRITE_OK),
                ("/api/testing/feedback", VIEWER_WRITE_OK),
                ("/api/passkeys", VIEWER_WRITE_OK),
                ("/api/passkeys/options", NEEDS_2FA_OK),
                ("/api/devices/revoke", VIEWER_WRITE_OK),
                ("/api/sessions/revoke", VIEWER_WRITE_OK),
                ("/api/verify-email/resend", NEEDS_2FA_OK),
                ("/api/email/change", NEEDS_2FA_OK),
                ("/api/billing", BILLING_WRITE_OK),
                ("/api/billing/checkout", BILLING_WRITE_OK)]:
            self.assertTrue(path_within(path, prefixes), path)

    def test_shared_spelling_is_not_membership(self):
        for path, prefixes in [
                ("/api/totp-foo", VIEWER_WRITE_OK),
                ("/api/totp-foo", NEEDS_2FA_OK),
                ("/api/totpx/enroll", NEEDS_2FA_OK),
                ("/api/testingx", VIEWER_WRITE_OK),
                ("/api/passkeys2", NEEDS_2FA_OK),
                ("/api/devicesx/revoke", VIEWER_WRITE_OK),
                ("/api/sessions/revoke-all", VIEWER_WRITE_OK),
                ("/api/verify-emailx", NEEDS_2FA_OK),
                ("/api/billing-export", BILLING_WRITE_OK),
                ("/api/logoutx", BILLING_WRITE_OK)]:
            self.assertFalse(path_within(path, prefixes), path)

    def test_settings_is_not_viewer_writable(self):
        # the SPA hides Settings controls from viewers; this is the server
        # truth that backs it — /api/settings carries no viewer exemption
        self.assertFalse(path_within("/api/settings", VIEWER_WRITE_OK))


if __name__ == "__main__":
    unittest.main()
