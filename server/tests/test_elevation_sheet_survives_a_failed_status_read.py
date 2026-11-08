"""A failed elevation status read never leaves a sheet that cannot end.

Every guarded action waits on the elevation sheet. When its one status
read fails (a deploy's 502, a busy server, a dropped connection), the
sheet must say so and offer Try again and Cancel. The web sheet once
showed "checking…" forever with no way out but a reload, the guarded
action stuck pending and every later one queued behind it; the phone's
sheet guessed a bare password form, which no TOTP or passkey-only
account can confirm with.

Source-shaped, like test_elevation_recovery_reachable: what is protected
is which controls the failure branch renders.
"""

import os
import pathlib
import unittest

import oikonome

ROOT = pathlib.Path(os.environ.get(
    "OIKONOME_CLIENT_ROOT", pathlib.Path(oikonome.__file__).parent.parent.parent))


def _read(rel):
    try:
        return (ROOT / rel).read_text()
    except FileNotFoundError:
        raise unittest.SkipTest(f"{rel} not present")


class WebSheet(unittest.TestCase):

    def setUp(self):
        self.src = _read("webapp/src/elevation.tsx")

    def _loading_branch(self):
        start = self.src.index("{!info ? (")
        return self.src[start:self.src.index(") : passkey", start)]

    def test_the_loading_branch_can_be_cancelled_and_retried(self):
        branch = self._loading_branch()
        self.assertIn("onClick={onCancel}", branch)
        self.assertIn("Try again", branch)

    def test_try_again_re_runs_the_status_read(self):
        self.assertRegex(self.src, r"\}, \[info, tries\]\);")
        self.assertIn("setTries((n) => n + 1)", self._loading_branch())

    def test_escape_cancels(self):
        self.assertRegex(self.src, r'e\.key === "Escape"[^\n]*onCancel\(\)')


class PhoneSheet(unittest.TestCase):

    def setUp(self):
        self.src = _read("mobile/src/components/elevation-modal.tsx")

    def _catch(self):
        start = self.src.index("client.elevationStatus().then(")
        return self.src[start:self.src.index("return () => { gone = true; }",
                                             start)]

    def test_a_failed_read_guesses_no_form(self):
        catch = self._catch().split(".catch(", 1)[1]
        self.assertNotIn("setForm(", catch)
        self.assertIn("setLoadFailed(true)", catch)

    def test_retry_re_runs_the_status_read(self):
        self.assertRegex(self.src, r"\}, \[prompt, tries\]\);")
        start = self.src.index("{!form && loadFailed && (")
        retry = self.src[start:self.src.index("</Pressable>", start)]
        self.assertIn("setTries((n) => n + 1)", retry)
        self.assertIn("Retry", retry)

    def test_a_wanted_recovery_code_is_not_called_a_wrong_one(self):
        body = self.src[self.src.index("function explain("):]
        self.assertLess(body.index("recovery_required"),
                        body.index("/recovery/i"))


if __name__ == "__main__":
    unittest.main()
