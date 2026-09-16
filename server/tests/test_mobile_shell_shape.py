"""Mobile shell: the unlock gate gates, and routine actions stay narrow.

Three invariants with quiet failure modes. The app-unlock gate must key on
the device's enrolled security level — a biometric-only check silently opens
the app on passcode-only phones, because the skipped authenticateAsync call
is exactly where the passcode fallback lives. Settings' connections-bundle
import must invalidate only the queries it touches — an unfiltered
invalidateQueries refetches every query any screen has populated. And the
More list mirrors the web's rule of hiding Business until an entity exists,
so a household that never ran a business doesn't see a permanently empty
destination.
"""

import pathlib
import unittest

import oikonome


def _src(rel: str) -> str:
    return (pathlib.Path(oikonome.__file__).parent.parent.parent
            / "mobile" / "src" / rel).read_text()


class MobileShellShapeTests(unittest.TestCase):
    def _read(self, rel: str) -> str:
        try:
            return _src(rel)
        except FileNotFoundError:
            self.skipTest("mobile/ not present")

    def test_unlock_gates_on_enrolled_security_level(self):
        src = self._read("lib/session.tsx")
        self.assertIn("getEnrolledLevelAsync()", src)
        self.assertIn("SecurityLevel.NONE", src)
        # biometric-only enrollment must not decide whether to prompt:
        # a passcode-only device reports false there and would walk in
        self.assertNotIn("LocalAuthentication.isEnrolledAsync", src)

    def test_bundle_import_invalidates_only_what_it_touches(self):
        src = self._read("app/settings.tsx")
        self.assertNotIn("invalidateQueries()", src,
                         "unfiltered invalidation refetches the whole app")
        for key in ('["settings"]', '["connections"]', '["accounts"]'):
            self.assertIn(f"invalidateQueries({{ queryKey: {key} }})", src)

    def test_more_hides_business_until_an_entity_exists(self):
        src = self._read("app/(tabs)/more.tsx")
        self.assertIn('if (!me.data?.has_business) hidden.add("/business")',
                      src)
