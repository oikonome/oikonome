"""The outbound-pause banner, on web and on mobile, never lists the
household's own Plaid / MX keys among what is paused, and says plainly
that new bank links on them are held.

Those keys keep serving every sync of the banks already linked through a
credential rotation; nothing stands them down. Listed under "Paused since
…", the banner would tell an owner recovering from a hijack that the bank
feeds are off. What the rotation does hold is new links (a new bank, a
re-auth, the MX widget), so the banner names that apart, with a Resume
the owner can press even when nothing else is paused."""
import pathlib
import re
import unittest

import oikonome

ROOT = pathlib.Path(oikonome.__file__).parent.parent.parent
BANNERS = [ROOT / "webapp" / "src" / "pages" / "Settings.tsx",
           ROOT / "mobile" / "src" / "app" / "settings.tsx"]


def _banner(path):
    src = path.read_text()
    start = src.index("function OutboundPausedBanner")
    end = src.index("\n}\n", start)
    return src[start:end]


class OutboundBannerWording(unittest.TestCase):

    def test_byo_keys_are_not_in_the_paused_list(self):
        for path in BANNERS:
            body = _banner(path)
            pushes = re.findall(r"what\.push\([^;]*\);", body, re.S)
            self.assertTrue(pushes, path)
            for line in pushes:
                self.assertNotIn("Plaid", line, path)
                self.assertNotIn("aggregator", line, path)

    def test_byo_keys_are_named_as_holding_new_links(self):
        for path in BANNERS:
            body = _banner(path)
            self.assertIn("New bank links are held", body, path)
            self.assertNotIn("Not paused, still in use", body, path)

    def test_a_hold_alone_still_offers_resume(self):
        for path in BANNERS:
            body = _banner(path)
            self.assertRegex(body, r"what\.length \|\| p\.aggregator\s*\?"
                                   r"\s*\"These are mine — resume\"", path)


if __name__ == "__main__":
    unittest.main()
