"""A link the server emits into Settings must open a section that exists.

Alerts, emails and pushes deep-link to `/settings/<id>`. The web page
renders an empty pane for an id it does not know, and the Today strip
dismisses the alert on the click, so a dead id strands the owner with no
pointer to the fix. The mobile app maps any `/settings/<id>` to its single
Settings screen. These read the clients as text: no build needed."""

import re
import unittest
from pathlib import Path

from oikonome import settings_sections

APP = Path(__file__).resolve().parents[2]
SERVER = APP / "server" / "oikonome"


def _web_sections() -> set[str]:
    src = (APP / "webapp/src/pages/Settings.tsx").read_text()
    secs = src[src.index("const SECS = ["):]
    secs = secs[:secs.index("\n];")]
    ids = set(re.findall(r'\bid:\s*"([a-z0-9_-]+)"', secs))
    old = src[src.index("const OLD_SEC"):]
    old = old[:old.index("};")]
    ids |= set(re.findall(r'\b([a-z0-9_]+):\s*"', old))
    return ids


def _emitted_literals() -> set[str]:
    """Section ids hard-coded into links in server source: the `link`
    field of an alert, or a URL built into a message."""
    found = set()
    pat = re.compile(r'''(?:["']link["']\s*:\s*|link\s*=\s*)f?["']'''
                     r'''/settings/([a-z0-9_-]+)''')
    for py in SERVER.rglob("*.py"):
        found |= set(pat.findall(py.read_text()))
    return found


class SettingsDeepLinksResolve(unittest.TestCase):

    def test_every_listed_section_exists_on_the_web(self):
        web = _web_sections()
        self.assertIn("connections", web, "parser must see the SECS list")
        missing = set(settings_sections.SECTIONS) - web
        self.assertFalse(missing, f"no Settings section for {missing}")

    def test_no_hard_coded_settings_link_points_nowhere(self):
        web = _web_sections()
        dead = _emitted_literals() - web
        self.assertFalse(dead, f"server links to missing sections: {dead}")

    def test_mobile_opens_settings_for_any_section_link(self):
        src = (APP / "mobile/src/lib/alert-routes.ts").read_text()
        self.assertRegex(src, r'path\.startsWith\("/settings/"\)\s*\?\s*'
                              r'"/settings"')
        self.assertIn('"/settings": "/settings"', src)

    def test_unknown_section_is_refused(self):
        with self.assertRaises(KeyError):
            settings_sections.settings_link("scripts")
