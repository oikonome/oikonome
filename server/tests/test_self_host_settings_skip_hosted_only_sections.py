"""On a self-hosted server the Settings page never calls GET /api/passkeys (a
hosted-only door that answers 404 there): the passkey reads are made only
where the feature exists. A link to a section the instance does not have,
such as /settings/billing, names it as missing on every branch of the page,
the owner's included, rather than rendering a blank body.
(Pinned on webapp/src/pages/Settings.tsx; the door itself stays hosted-only.)
"""
import pathlib
import re
import unittest

import oikonome

_SETTINGS = (pathlib.Path(oikonome.__file__).parent.parent.parent
             / "webapp" / "src" / "pages" / "Settings.tsx")


class SelfHostSettings(unittest.TestCase):
    def setUp(self):
        if not _SETTINGS.exists():
            self.skipTest("webapp/ not present")
        self.src = _SETTINGS.read_text(encoding="utf-8")

    def test_every_passkeys_read_waits_for_a_hosted_instance(self):
        reads = re.findall(
            r"useQuery\(\{\s*queryKey: \[\"passkeys\"\][^)]*\}\)", self.src)
        self.assertGreaterEqual(len(reads), 2)
        for r in reads:
            self.assertRegex(r, r"enabled: !!(meQ|me)\.data\?\.hosted", r)

    def test_a_missing_section_is_named_on_the_owners_page_too(self):
        self.assertEqual(self.src.count("{missingNote}"), 2)
        self.assertIn("const missingNote = missingSec && (", self.src)

    def test_the_security_line_on_self_host_speaks_of_2fa_alone(self):
        self.assertIn('meQ.data && !meQ.data.hosted\n      ? (twoFA ? '
                      '{ text: "2FA on" } : { text: "2FA off", warn: true })',
                      self.src)


if __name__ == "__main__":
    unittest.main()
