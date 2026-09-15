"""Surfaces whose whole behaviour hangs on a single condition.

The 1040-ES note, Settings' unknown-
section branch, the env-number helper, the login mode switch and the
tax-reserve picker are each one branch deep. Invert the condition or typo
the value it reads and the page still renders, so only an explicit test
here fails.
"""

import datetime as dt
import pathlib
import unittest

import oikonome
from oikonome.engine import compliance


def _spa(rel: str) -> str:
    return (pathlib.Path(oikonome.__file__).parent.parent.parent
            / "webapp" / "src" / rel).read_text()


class EstimatedTaxNoteTests(unittest.TestCase):
    """The 1040-ES row names the nominated tax account when there is one —
    knowing where the money IS when the deadline arrives is the point of
    nominating it. A typo or an inverted condition would ship silently."""

    def _entity(self, **over):
        e = {"income_tax_rate": 22.0}
        e.update(over)
        return e

    def test_a_nomination_is_named_on_the_deadline(self):
        rows = compliance._estimated_tax(
            self._entity(tax_reserve_account_id="acct-1"), dt.date(2026, 2, 1))
        self.assertEqual(len(rows), 1)
        self.assertIn("nominated tax account", rows[0]["note"])

    def test_no_nomination_says_nothing_extra(self):
        rows = compliance._estimated_tax(self._entity(), dt.date(2026, 2, 1))
        self.assertEqual(len(rows), 1)
        self.assertNotIn("nominated", rows[0]["note"])

    def test_an_empty_nomination_is_not_a_nomination(self):
        for empty in (None, ""):
            rows = compliance._estimated_tax(
                self._entity(tax_reserve_account_id=empty),
                dt.date(2026, 2, 1))
            self.assertNotIn("nominated", rows[0]["note"], repr(empty))

    def test_the_row_still_needs_an_opted_in_rate(self):
        """The whole obligation only appears once the entity has set an
        assumed rate — the nomination must not smuggle it in."""
        rows = compliance._estimated_tax(
            {"tax_reserve_account_id": "acct-1"}, dt.date(2026, 2, 1))
        self.assertEqual(rows, [])


class UnknownSettingsSectionTests(unittest.TestCase):
    """A URL naming a section this instance lacks used to render a DIFFERENT
    section while the address bar went on claiming the first."""

    def setUp(self):
        try:
            self.src = _spa("pages/Settings.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_the_branch_exists_and_is_rendered(self):
        self.assertIn("const missingSec =", self.src)
        self.assertIn("{missingSec && (", self.src)

    def test_an_old_deep_link_still_redirects_rather_than_erroring(self):
        """OLD_SEC exists so links minted in old emails still land
        somewhere real — those must NOT be reported as missing."""
        i = self.src.index("const missingSec =")
        self.assertIn("!OLD_SEC[secParam]", self.src[i:i + 220])

    def test_it_names_which_setup_the_section_belongs_to(self):
        i = self.src.index("{missingSec && (")
        self.assertIn("self-hosted", self.src[i:i + 500])
        self.assertIn("hosted", self.src[i:i + 500])


class EnvNumHasOneHomeTests(unittest.TestCase):
    """The empty-is-not-unset rule lives in one helper: every restatement
    of it is a chance for the next caller to get it wrong, and getting it
    wrong crash-loops the container on an empty env value."""

    def test_the_helper_is_defined_once(self):
        import subprocess
        root = pathlib.Path(oikonome.__file__).parent
        out = subprocess.run(["grep", "-rn", "def env_num\\|def _env_num",
                              str(root)], capture_output=True, text=True).stdout
        self.assertEqual(len(out.strip().splitlines()), 1, out)

    def test_both_callers_use_it(self):
        from oikonome.engine import receipts
        from oikonome.web import plaid_webhook
        from oikonome.envnum import env_num
        self.assertIs(receipts.env_num, env_num)
        self.assertIs(plaid_webhook.env_num, env_num)

    def test_empty_and_whitespace_are_unset(self):
        import os

        from oikonome.envnum import env_flag, env_num
        os.environ["OIKONOME_TMP_PROBE"] = ""
        try:
            self.assertEqual(env_num("OIKONOME_TMP_PROBE", "7"), "7")
            self.assertFalse(env_flag("OIKONOME_TMP_PROBE"))
            os.environ["OIKONOME_TMP_PROBE"] = "  "
            self.assertEqual(env_num("OIKONOME_TMP_PROBE", "7"), "7")
        finally:
            os.environ.pop("OIKONOME_TMP_PROBE", None)

    def test_a_falsey_string_is_not_a_true_flag(self):
        """The other half of the trap: os.environ.get alone reads "0" and
        "false" as ON."""
        import os

        from oikonome.envnum import env_flag
        for v in ("0", "false", "no", "off"):
            os.environ["OIKONOME_TMP_PROBE"] = v
            try:
                self.assertFalse(env_flag("OIKONOME_TMP_PROBE"), v)
            finally:
                os.environ.pop("OIKONOME_TMP_PROBE", None)


class LoginModeSwitchTests(unittest.TestCase):
    """Switching between authenticator and recovery mode left the typed
    digits in the box under a placeholder that now said something else."""

    def test_the_field_clears_with_the_mode(self):
        try:
            s = _spa("pages/Login.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")
        self.assertIn('setRecovery(true); setTotp("")', s)
        self.assertIn('setRecovery(false); setTotp("")', s)


class TaxReservePickerAgreesWithTheCardTests(unittest.TestCase):
    """The card said "the nominated account is missing"; the picker said
    "none nominated". Two surfaces disagreeing about one fact is worse than
    either message alone."""

    def test_the_picker_admits_a_dangling_nomination(self):
        try:
            s = _spa("pages/Business.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")
        self.assertIn("const dangling =", s)
        self.assertIn("the nominated account is missing", s)

    def test_the_stored_value_is_kept_not_reset(self):
        """Re-assigning the account should restore the nomination rather
        than having silently cleared it."""
        try:
            s = _spa("pages/Business.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")
        self.assertIn('value={dangling ? "" : current}', s)
