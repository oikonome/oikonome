"""A form never saves a value the person never saw loaded.

A form whose read has not answered, or failed, holds its defaults: an
empty AI backend list, a blank mail user and sender, a setup mode of
"auto", a retirement wizard prefilled with generic numbers. Saved, each of
those replaces what is stored. So every such Save waits for the read it was
seeded from and says why it waits (the shared saveWaits sentence, pinned
by the case table in mobile/scripts/money-cases.mjs); and saveWholeLists
refuses a whole-list save with no settings view under it before anything
is sent (the same case table). These checks pin the screens to that rule
(specs/screen-states.md, "A form never saves until it has been seeded").
"""

import pathlib
import re
import unittest

import oikonome

_APP = pathlib.Path(oikonome.__file__).parent.parent.parent
_WEB = _APP / "webapp" / "src"
_MOB = _APP / "mobile" / "src"


def _src(test: unittest.TestCase, p: pathlib.Path) -> str:
    if not p.exists():
        test.skipTest(f"{p} not present")
    return p.read_text()


def _fn(src: str, head: str) -> str:
    """The text of the top-level function starting at `head`."""
    body = src[src.index(head):]
    return body[:body.index("\n}\n")]


class FormsNeverSaveUnseededValues(unittest.TestCase):
    def test_phone_ai_and_mail_cards_save_only_once_seeded(self):
        src = _src(self, _MOB / "app" / "settings.tsx")
        for card, label in (("function LlmCard(", "Save AI settings"),
                            ("function SmtpCard(", "Save SMTP settings")):
            body = _fn(src, card)
            self.assertIn("const waits = saveWaits(seeded, settings.isError);",
                          body, card)
            # the Save button itself is held while the form is unseeded
            press = body[:body.rindex(label)]
            press = press[press.rindex("<Pressable"):]
            self.assertRegex(press, r"disabled=\{[^}]*!!waits", card)
            # and the reason is on screen
            self.assertIn("{waits && <Text", body, card)

    def test_setup_mode_is_never_guessed(self):
        """Continue on the bills question writes the mode it shows: a
        guessed "auto" over a stored "manual" turns the finder back on."""
        web = _fn(_src(self, _WEB / "pages" / "Welcome.tsx"),
                  "export default function Welcome(")
        mob = _src(self, _MOB / "app" / "welcome.tsx")
        for p, src, var in (("Welcome.tsx", web, "mode"),
                            ("welcome.tsx", mob, "setupMode")):
            decl = src[src.index(f"const {var}: SetupMode"):]
            decl = decl[:decl.index(";")]
            self.assertIn("SetupMode | null", decl, p)
            self.assertNotIn('"auto";', decl + ";", p)
            self.assertNotRegex(decl, r'\?\?\s*"auto"', p)
            self.assertIn(f"disabled: pickMode.isPending || {var} === null"
                          if var == "mode" else f"disabled: {var} === null",
                          src, p)
            self.assertIn("saveWaits(", src, p)
        # the option cards accept "not read yet" and mark neither
        self.assertIn("mode: SetupMode | null",
                      _src(self, _WEB / "components" / "WizardBills.tsx"))
        self.assertIn("value: SetupMode | null",
                      _src(self, _MOB / "components" / "wizard-bills.tsx"))

    def test_retirement_wizard_never_opens_on_a_failed_read(self):
        """Opened without the saved answers, the wizard starts on generic
        defaults and its step saves write them over what is stored."""
        wiz = _fn(_src(self, _WEB / "components" / "RetireWizard.tsx"),
                  "export default function RetireWizard(")
        self.assertRegex(wiz, r"if \(!s\.data\)\s*return")
        self.assertIn("SAVE_WAITS_FAILED", wiz)
        setup = _src(self, _WEB / "pages" / "RetirementSetup.tsx")
        self.assertIn("if (!s.data || !r.data)", setup)
        mob = _src(self, _MOB / "app" / "retire.tsx")
        self.assertIn("if (wizard === true && !settings.data)", mob)
        self.assertNotIn("if (wizard === true && settings.isPending)", mob)
        self.assertIn("SAVE_WAITS_FAILED", mob)

    def test_the_guard_lives_in_the_one_whole_list_save(self):
        """Both clients refuse an unread whole-list save inside
        saveWholeLists, which every settings save goes through."""
        for p in (_WEB / "moneymath.ts", _MOB / "lib" / "pure.ts"):
            fn = _fn(_src(self, p), "export async function saveWholeLists<V>(")
            self.assertRegex(
                fn, re.escape("if (basis == null && WHOLE_LIST_KEYS.some(")
                + r"[^\n]*\n\s*throw new SettingsUnreadError\(\);", p)
            # the refusal comes before the first send
            self.assertLess(fn.index("SettingsUnreadError"),
                            fn.index("send("), p)


if __name__ == "__main__":
    unittest.main()
