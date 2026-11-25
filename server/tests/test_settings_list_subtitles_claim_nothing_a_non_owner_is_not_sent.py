"""The web Settings list never states a fact about the household's setup
to someone who is not sent that setup.

Some settings are the owner's alone to read (web/settings_exposure.py), so
a member's or a viewer's settings read simply lacks them. A subtitle that
is built from such a key and falls back to a default when it is absent
("no endpoint — built-in only" for a missing llm_model) would tell a
non-owner something false. Pinned here, on the real source of the list's
subtitles in webapp/src/pages/Settings.tsx: every subtitle that reads an
owner-only key off the settings read is the owner's alone, and anyone else
gets no subtitle from it.
"""

import pathlib
import re
import unittest

import oikonome
from oikonome.web import settings_exposure as se

_SETTINGS = (pathlib.Path(oikonome.__file__).parent.parent.parent
             / "webapp" / "src" / "pages" / "Settings.tsx")

_OWNER_ONLY = frozenset(k for k, e in se.TABLE.items()
                        if e.read in ("owner", "never"))


def _summary_entries() -> dict[str, str]:
    """Each subtitle's source, keyed by its section id."""
    src = _SETTINGS.read_text(encoding="utf-8")
    start = src.index("const summary: Record<string")
    body = src[start:src.index("\n  };\n", start)]
    # drop the comments, so a key named in prose is not taken for a read
    body = "\n".join(line for line in body.splitlines()
                     if not line.lstrip().startswith("//"))
    parts = re.split(r"\n    (\w+): ", body)
    return dict(zip(parts[1::2], parts[2::2]))


class OwnerOnlySubtitles(unittest.TestCase):
    def setUp(self):
        if not _SETTINGS.exists():
            self.skipTest("webapp/ not present")
        self.entries = _summary_entries()

    def test_the_list_was_read(self):
        for sec in ("email", "ai", "prefs", "security"):
            self.assertIn(sec, self.entries)

    def test_a_subtitle_from_an_owner_only_key_is_the_owners_alone(self):
        wrong = []
        for sec, code in self.entries.items():
            keys = set(re.findall(r"q\.data\??\.(\w+)", code)) & _OWNER_ONLY
            if keys and not code.lstrip().startswith("owner ?"):
                wrong.append(f"{sec}: reads {sorted(keys)} for everyone")
        self.assertEqual(wrong, [])

    def test_the_ai_subtitle_says_nothing_to_a_non_owner(self):
        ai = self.entries["ai"]
        self.assertIn("llm_effective", ai)
        self.assertTrue(ai.lstrip().startswith("owner ?"))
        self.assertTrue(ai.rstrip().rstrip(",").endswith(": undefined"))


if __name__ == "__main__":
    unittest.main()
