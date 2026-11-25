"""A written spec or guide never repeats a prose line more than a handful
of times.

Specs and guides ship in the public tree and the guides are the in-app
Help: a document that carries one paragraph pasted over and over is
unreadable once published.
"""

import collections
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DIRS = ("specs", "docs")
MAX_REPEATS = 25


class WrittenDocsHoldNoRepeatedPaste(unittest.TestCase):
    def test_no_prose_line_repeats_past_a_handful(self):
        bad = []
        for d in DIRS:
            for p in (ROOT / d).rglob("*.md"):
                lines = [ln.strip() for ln in
                         p.read_text(encoding="utf-8").splitlines()]
                prose = [ln for ln in lines if len(ln) > 30
                         and not ln.startswith(("|", "```", "---", "-", "*"))]
                for ln, n in collections.Counter(prose).items():
                    if n > MAX_REPEATS:
                        bad.append(f"{p.relative_to(ROOT)}: {n}x {ln[:60]!r}")
                        break
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
