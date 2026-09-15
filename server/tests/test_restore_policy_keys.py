"""A restore carries a household's data, never this instance's consent.

`scrub_config` already dropped demo_mode, demo_login and smtp_starttls on the
way in — decisions that belong to the instance doing the importing. The tax
document remote-LLM consent was not on that list, and it is the sharpest one
there is: it lets an image of a W-2, Social Security number included, be sent
to whatever remote model this instance is pointed at. A crafted (or merely
stale) export ZIP could turn it on with nobody saying yes.
"""

import unittest

from oikonome.sync import restore


class RestoreConsentKeyTests(unittest.TestCase):

    def test_tax_document_remote_llm_consent_never_rides_in(self):
        cfg = restore.scrub_config({
            "taxdocs_allow_remote_llm": "1",
            "budgets": {"FOOD": 500},
        })
        self.assertNotIn("taxdocs_allow_remote_llm", cfg)
        self.assertEqual(cfg["budgets"], {"FOOD": 500},
                         "ordinary household data still restores")

    def test_it_is_dropped_however_it_is_spelled(self):
        for v in ("1", "true", "on", "yes", 1, True):
            cfg = restore.scrub_config({"taxdocs_allow_remote_llm": v})
            self.assertEqual(cfg, {}, v)

    def test_the_other_instance_decisions_are_still_dropped(self):
        cfg = restore.scrub_config({
            "demo_mode": "1", "demo_login": {"password": "x"},
            "smtp_starttls": False, "theme": "dark",
        })
        self.assertEqual(cfg, {"theme": "dark"})
