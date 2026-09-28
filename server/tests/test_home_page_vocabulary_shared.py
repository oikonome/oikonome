"""The home-page vocabulary is one list, held equal across every copy.

A household picks where each client opens; the clients redirect there
blind at launch. The server's list is the one every write door checks
against (the Settings save and the export-ZIP restore), and the web and
phone apps each carry their own copy for the picker and the redirect.
Four hand-written lists drift unless something holds them together.
"""

import pathlib
import re
import unittest

from oikonome import homepages

APP = pathlib.Path(__file__).resolve().parents[2]


def _ts_pairs(src: str, const: str) -> tuple[str, ...]:
    m = re.search(rf"const {const}: \[string, string\]\[\] = \[(.*?)\];",
                  src, re.S)
    assert m, const
    return tuple(re.findall(r'\["([^"]+)",\s*"[^"]+"\]', m.group(1)))


class VocabularyIsSharedTests(unittest.TestCase):
    def test_web_client_lists_match_the_server(self):
        src = (APP / "webapp/src/api/client.ts").read_text()
        self.assertEqual(_ts_pairs(src, "WEB_HOMES"), homepages.HOME_WEB)
        self.assertEqual(_ts_pairs(src, "MOBILE_HOMES"),
                         homepages.HOME_MOBILE)

    def test_mobile_settings_picker_matches_the_server(self):
        src = (APP / "mobile/src/app/settings.tsx").read_text()
        self.assertEqual(_ts_pairs(src, "MOBILE_HOMES"),
                         homepages.HOME_MOBILE)

    def test_mobile_launch_redirect_accepts_exactly_the_non_default_pages(self):
        src = (APP / "mobile/src/app/(tabs)/index.tsx").read_text()
        m = re.search(r'\[((?:"[a-z]+",?\s*)+)\]\.includes\(h\)', src)
        self.assertIsNotNone(m, "launch redirect list not found")
        accepted = tuple(re.findall(r'"([a-z]+)"', m.group(1)))
        self.assertEqual(
            accepted,
            tuple(p for p in homepages.HOME_MOBILE
                  if p != homepages.DEFAULTS["home_mobile"]))


class ScrubTests(unittest.TestCase):
    def test_an_unknown_page_is_dropped_with_a_note(self):
        cfg = {"home_web": "/admin", "home_mobile": "bills", "x": 1}
        notes = homepages.scrub(cfg)
        self.assertEqual(cfg, {"home_mobile": "bills", "x": 1})
        self.assertEqual(len(notes), 1)
        self.assertIn("home_web", notes[0])

    def test_the_default_is_stored_as_absent(self):
        cfg = {"home_web": "/", "home_mobile": "index"}
        self.assertEqual(homepages.scrub(cfg), [])
        self.assertEqual(cfg, {})

    def test_a_non_string_is_dropped(self):
        cfg = {"home_web": ["/bills"]}
        self.assertEqual(len(homepages.scrub(cfg)), 1)
        self.assertNotIn("home_web", cfg)

    def test_restore_config_check_applies_it(self):
        from oikonome.sync.restore import _check_config
        cfg = {"home_web": "/nowhere"}
        notes = _check_config(cfg)
        self.assertNotIn("home_web", cfg)
        self.assertTrue(any("home_web" in n for n in notes))
