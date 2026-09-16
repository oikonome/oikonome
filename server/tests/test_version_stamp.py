"""The version an instance reports must survive a change of version series.

`git describe` names the nearest version tag and how far past it HEAD is —
"vA.B.C-M-gSHA" — and every entry point collapses that into the vA.B.(C+M)
an operator actually reads. Two invariants live here:

* The collapse ADDS: base patch + commits-since. A base-blind count would
  restart the numbering at 1 after any tag rewrite and an instance would
  claim a version far below the one it shipped.
* Neither the tag glob nor the arithmetic may be pinned to one series. The
  numbering moves from one vA.B series to the next by tagging vA.B.0 and
  nothing else; a hard-coded glob would keep counting in the old series
  forever, and a hard-coded anchor would make the stamp jump backwards.

The manifest the admin console renders is numbered by the same rule, so a
commit that has already shipped as v0.1.N must keep that label when a later
series starts — a renumbered history would relabel releases that are out in
the world.
"""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HELPERS = ROOT / "scripts" / "version-stamp.sh"
GEN = ROOT / "scripts" / "gen_version_history.py"


def collapse(raw: str) -> str:
    """Run the real shell function, not a Python restatement of it."""
    r = subprocess.run(
        ["bash", "-c", '. "$1"; collapse_version "$2"', "_", str(HELPERS), raw],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


class VersionCollapseTests(unittest.TestCase):
    def test_describe_output_collapses_to_a_plain_version(self):
        cases = {
            # base + commits-since, in the series the tag names
            "v0.3.407-8-gabc1234": "v0.3.415",
            "v1.2.0-3-gabc1234": "v1.2.3",
            "v1.2.7-12-gabc1234": "v1.2.19",
            # an exact tag is what describe prints with no suffix at all
            "v1.2.0": "v1.2.0",
            "v0.3.407": "v0.3.407",
            # not a version at all — describe --always fell back to the sha
            "abc1234": "abc1234",
            "unknown": "unknown",
            "": "",
        }
        for raw, want in cases.items():
            self.assertEqual(collapse(raw), want, f"collapsing {raw!r}")

    def test_a_modified_checkout_still_says_so(self):
        # A dirty tree must never be able to present itself as a clean
        # release: the marker survives every branch of the collapse.
        self.assertEqual(collapse("v0.3.407-8-gabc1234-dirty"),
                         "v0.3.415-dirty")
        self.assertEqual(collapse("v1.2.0-3-gabc1234-dirty"), "v1.2.3-dirty")
        self.assertEqual(collapse("v1.2.0-dirty"), "v1.2.0-dirty")
        self.assertEqual(collapse("abc1234-dirty"), "abc1234-dirty")

    def test_the_entry_points_share_one_implementation(self):
        # Two copies of this arithmetic is how the installer and the control
        # script come to disagree about what is running.
        for script in ("install.sh", "oikonome.sh"):
            src = (ROOT / script).read_text()
            self.assertIn("scripts/version-stamp.sh", src,
                          f"{script} no longer sources the shared helpers")
            self.assertNotRegex(
                src, r"--match ['\"]v0\.1",
                f"{script} pins describe to one version series")

    def test_the_tag_glob_matches_any_series(self):
        src = HELPERS.read_text()
        glob = re.search(r"VERSION_TAG_GLOB='([^']+)'", src).group(1)
        for tag in ("v0.1.0", "v0.3.407", "v1.2.0", "v2.0.0"):
            r = subprocess.run(["bash", "-c",
                                f'case "$1" in {glob}) exit 0;; esac; exit 1',
                                "_", tag])
            self.assertEqual(r.returncode, 0, f"{glob} misses {tag}")


@unittest.skipUnless(shutil.which("git"), "git not available")
class ManifestNumberingTests(unittest.TestCase):
    """The manifest must number commits the same way `describe` does."""

    def _repo(self, d: str) -> None:
        def git(*a: str) -> None:
            subprocess.run(["git", "-C", d, *a], check=True,
                           capture_output=True)
        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "T")
        (Path(d) / "f.txt").write_text("one\n")
        git("add", "f.txt")
        git("commit", "-qm", "anchor")
        git("tag", "v0.1.0")
        for subject in ("first", "second", "third", "fourth"):
            (Path(d) / "f.txt").write_text(subject + "\n")
            git("commit", "-qam", subject)

    def _versions(self, d: str) -> list[str]:
        out = subprocess.run(["python3", str(GEN), "v0.1.0"], cwd=d,
                             capture_output=True, text=True, check=True).stdout
        return [ln.split("\t")[0] for ln in out.splitlines()
                if not ln.startswith("#")]

    def test_one_series_numbers_from_the_anchor(self):
        with tempfile.TemporaryDirectory() as d:
            self._repo(d)
            self.assertEqual(self._versions(d),
                             ["v0.1.1", "v0.1.2", "v0.1.3", "v0.1.4"])

    def test_a_new_series_restarts_at_zero_and_leaves_history_alone(self):
        with tempfile.TemporaryDirectory() as d:
            self._repo(d)
            # tag the third commit as the start of the next series
            subprocess.run(["git", "-C", d, "tag", "v1.2.0", "HEAD~1"],
                           check=True, capture_output=True)
            self.assertEqual(self._versions(d),
                             ["v0.1.1", "v0.1.2", "v1.2.0", "v1.2.1"])

    def test_the_manifest_agrees_with_git_describe(self):
        # Same commit, two independent paths to a version string. If these
        # ever disagree, the admin console's history and the running
        # instance's footer are naming different things.
        with tempfile.TemporaryDirectory() as d:
            self._repo(d)
            subprocess.run(["git", "-C", d, "tag", "v1.2.0", "HEAD~1"],
                           check=True, capture_output=True)
            raw = subprocess.run(
                ["bash", "-c",
                 '. "$1"; describe_version "$2"', "_", str(HELPERS), d],
                capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(raw, self._versions(d)[-1])


if __name__ == "__main__":
    unittest.main()
