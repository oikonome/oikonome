"""Every "Settings → <section>" direction the admin console gives must name
a section the web app's Settings page actually has.

An operator follows these words by hand: there is no link to fail loudly,
so a renamed or invented section leaves them searching a page that has no
such heading. Reads the clients as text: no build needed."""

import re
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[2]
CONSOLE = [APP / "server/oikonome/web/adminconsole.py",
           APP / "server/oikonome/web/templates/admin.html"]


def _web_section_names() -> set[str]:
    src = (APP / "webapp/src/pages/Settings.tsx").read_text()
    secs = src[src.index("const SECS = ["):]
    secs = secs[:secs.index("\n];")]
    return set(re.findall(r'\b(?:label|title):\s*"([^"]+)"', secs))


def _directions(text: str) -> list[str]:
    """What follows each "Settings →" in one file, with Python's adjacent
    string literals joined and markup and line breaks flattened."""
    text = re.sub(r'"\s*\n\s*(?:f)?"', "", text)
    text = text.replace("&rarr;", "→")
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text)
    return re.findall(r"Settings → ([^.\"\n]{1,60})", text)


class ConsoleSettingsPathsExist(unittest.TestCase):

    def test_parser_sees_the_sections(self):
        names = _web_section_names()
        self.assertIn("Household", names)
        self.assertIn("Security", names)

    def test_every_console_direction_names_a_real_section(self):
        names = _web_section_names()
        seen = 0
        for path in CONSOLE:
            for after in _directions(path.read_text()):
                seen += 1
                ok = any(after == n or after.startswith(n + " ")
                         or after.startswith(n + "→") for n in names)
                self.assertTrue(
                    ok, f"{path.name}: 'Settings → {after}' names no "
                        f"Settings section (have {sorted(names)})")
        self.assertGreater(seen, 0, "parser found no directions at all")


if __name__ == "__main__":
    unittest.main()
