"""Import, sign-in and setup chrome.

Import has ONE upload door, not three (bulk, single file, tax documents,
each with its own card, form and file input). A person holding a file does
not know which door it belongs to; the app does.

Sign-in does not explain itself in five sentences on the one screen where a
person wants to get in, does not bury the recovery path inside a red
failure box, lets the second-factor step correct a mistyped email, and gives
a passkey-only account its own state rather than an error-shaped experience
for working as configured.
"""

import pathlib
import unittest

import oikonome


def _src(rel: str) -> str:
    return (pathlib.Path(oikonome.__file__).parent.parent.parent
            / "webapp" / "src" / rel).read_text()


def _tmpl(name: str) -> str:
    return (pathlib.Path(oikonome.__file__).parent / "web" / "templates"
            / name).read_text()


class ImportOneDoorTests(unittest.TestCase):
    def setUp(self):
        try:
            self.src = _src("pages/Import.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_there_is_one_drop_zone(self):
        self.assertIn("function DropZone(", self.src)
        self.assertIn('className={"dropzone"', self.src)
        self.assertIn(".dropzone{", _src("index.css"))

    def test_the_app_routes_the_file_not_the_person(self):
        self.assertIn("const route = (files: File[])", self.src)
        self.assertIn("TAXDOC_RE", self.src)

    def test_the_accepted_formats_live_inside_the_zone(self):
        zone = self.src[self.src.index("function DropZone("):
                        self.src.index("// heading/prelude")]
        self.assertIn("Quicken QIF", zone)
        self.assertIn("export ZIP", zone)
        self.assertIn("ssa.gov earnings records", zone)

    def test_the_other_two_doors_are_gone(self):
        """Their file inputs, not their pipelines — both still run."""
        self.assertNotIn("Bulk historical import", self.src)
        self.assertNotIn("Choose a document…", self.src)
        self.assertIn("function PlanReview(", self.src)
        self.assertIn("function TaxDocsCard(", self.src)

    def test_what_was_found_is_stated_before_committing(self):
        self.assertIn("What we found", self.src)
        self.assertIn("Nothing is imported until you press the button",
                      self.src)

    def test_amount_convention_is_a_correction_not_a_prerequisite(self):
        self.assertIn("If amounts look backwards", self.src)
        self.assertNotIn("<label>Amount convention", self.src)

    def test_a_file_needing_column_mapping_is_carried_there(self):
        """Bulk run answers 'import it alone to map them' — so the file is
        carried to the mapping step rather than left to the person."""
        self.assertIn('String(x.error).includes("map them")', self.src)
        self.assertIn("onNeedsMapping", self.src)

    def test_the_mapping_step_previews_real_rows(self):
        self.assertIn("need.sample", self.src)
        self.assertIn("read the way you have mapped", self.src)

    def test_the_server_ships_those_rows(self):
        pages = (pathlib.Path(oikonome.__file__).parent / "web"
                 / "pages.py").read_text()
        self.assertIn('"sample": sample', pages)


class LoginChromeTests(unittest.TestCase):
    def setUp(self):
        try:
            self.src = _src("pages/Login.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_the_page_does_not_teach_what_a_passkey_is(self):
        self.assertNotIn("fingerprint, face, or security key", self.src)

    def test_recovery_is_a_standing_link_not_a_failure_message(self):
        self.assertIn("Lost your authenticator?", self.src)
        self.assertIn("Use a recovery code", self.src)
        self.assertNotIn("or paste a recovery code, or use Forgot password",
                         self.src)

    def test_the_second_factor_step_offers_the_exit(self):
        """Without it, a mistyped email costs a page reload."""
        self.assertIn("not you?", self.src)
        self.assertIn("setNeedTotp(false)", self.src)

    def test_a_passkey_only_account_has_its_own_state(self):
        self.assertIn("const [passkeyOnly, setPasskeyOnly]", self.src)
        self.assertIn("This account signs in with a passkey.", self.src)
        self.assertIn("Use your passkey", self.src)
        # both ways back in from a device without the passkey
        self.assertIn("email yourself a link", self.src)

    def test_the_demo_says_what_it_is_on_the_wordmark(self):
        self.assertIn('className="pill a"', self.src)
        self.assertIn("resets hourly", self.src)

    def test_the_2fa_caveat_moved_to_the_page_it_belongs_to(self):
        self.assertNotIn("email link clears 2FA", self.src)
        self.assertIn("clears it, so you can enrol your authenticator again",
                      _tmpl("forgot.html"))


class WizardChromeTests(unittest.TestCase):
    def setUp(self):
        try:
            self.src = _src("pages/Welcome.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_the_wizard_has_one_progress_row(self):
        self.assertIn('className="wiz"', self.src)
        self.assertIn(".wiz{", _src("index.css"))
        self.assertIn("Step {idx + 1} of {steps.length}", self.src)

    def test_the_way_out_is_named_and_in_that_row(self):
        start = self.src.index('className="wiz"')
        row = self.src[start:self.src.index('<h2 style={{ margin: ".1rem',
                                            start)]
        self.assertIn("save &amp; finish later", row)

    def test_the_three_rows_of_chrome_are_one(self):
        self.assertNotIn("Welcome — let's set up Oikonome", self.src)
        self.assertNotIn("exit — resume later from", self.src)
