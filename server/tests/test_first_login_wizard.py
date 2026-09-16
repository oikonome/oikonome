"""A new account lands in the guided setup, not on an empty dashboard.

Signup redirects to /app/welcome — but on hosted the very next steps are
email verification and the 2FA gate, so the first thing anyone actually does
is LOG IN, and login always lands on /app/. Without this redirect the wizard
is reachable only through the green "Finish setup" pill in the nav: easy to
miss, and on a phone it sits behind the menu.

The redirect must not become a trap: the wizard's own exits navigate to the
root, which is the path that redirects.
"""

import pathlib
import unittest

import oikonome


def _src(rel: str) -> str:
    return (pathlib.Path(oikonome.__file__).parent.parent.parent
            / "webapp" / "src" / rel).read_text()


class FirstLoginWizardTests(unittest.TestCase):
    def setUp(self):
        try:
            self.app = _src("App.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_the_root_sends_an_unfinished_setup_into_the_wizard(self):
        self.assertRegex(self.app,
                         r"const wantsWizard = [^;]*&& !hasLeftWizard\("
                         r"me(?:\.data)?\.tenant_id\);")
        self.assertIn('wantsWizard ? <Navigate to="/welcome" replace />',
                      self.app)

    def test_only_the_root_redirects(self):
        """Every other route — a deep link, a bookmark, the nav — must still
        go where it says."""
        self.assertEqual(self.app.count('<Navigate to="/welcome"'), 1)

    def test_it_waits_for_the_onboarding_state(self):
        """setupPending is false until `ob.data` exists, so nothing
        redirects on a guess and there is no flash of the wrong page."""
        i = self.app.index("const setupPending =")
        self.assertIn("!!ob.data", self.app[i:i + 200])

    def test_leaving_on_purpose_sticks(self):
        w = _src("pages/Welcome.tsx")
        self.assertIn('leftWizard(me.data?.tenant_id); nav("/")', w)
        self.assertIn('leftWizard(me.data?.tenant_id);\n      nav("/");', w)

    def test_the_exit_flag_is_session_scoped(self):
        """It lasts as long as the tab: a later sign-in offers the wizard
        again while setup is still genuinely unfinished."""
        x = _src("wizardexit.ts")
        self.assertIn("sessionStorage", x)
        self.assertNotIn("localStorage", x)

    def test_the_flag_is_scoped_to_the_ACCOUNT_not_just_the_tab(self):
        """Signing out is a full navigation, which does NOT clear
        sessionStorage — so a tab-scoped flag alone would have user A leaving
        the wizard and handing the laptop to user B skip setup entirely for
        B's brand-new account."""
        x = _src("wizardexit.ts")
        self.assertIn("function key(scope?: string)", x)
        self.assertIn("`${KEY}:${scope}`", x)
        self.assertRegex(_src("App.tsx"),
                         r"hasLeftWizard\(me(?:\.data)?\.tenant_id\)")
        self.assertIn("leftWizard(me.data?.tenant_id)",
                      _src("pages/Welcome.tsx"))

    def test_private_mode_cannot_break_the_app(self):
        """sessionStorage throws in some privacy modes; a storage failure
        must not take the whole shell down."""
        x = _src("wizardexit.ts")
        self.assertEqual(x.count("catch"), 2)

    def test_the_finish_setup_pill_is_still_there(self):
        """The redirect is the default, not a replacement — someone who
        left mid-way still needs the door back in."""
        self.assertIn("Finish setup", self.app)
