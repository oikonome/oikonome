"""With no language model, the Assistant's one way out opens the place the
model endpoint is set — Settings → AI — on both clients. The web linked to
Categorization and the phone to the top of Settings, where nothing about a
model is in view."""

import pathlib
import unittest

import oikonome

_ROOT = pathlib.Path(oikonome.__file__).parent.parent.parent


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


class AssistantSetupLinkTests(unittest.TestCase):
    def test_the_web_links_the_ai_section(self):
        src = _read("webapp/src/pages/Assistant.tsx")
        self.assertIn('EMPTY_LINES.assistantOff} to="/settings/ai"', src)
        self.assertNotIn("/settings/categorize", src)

    def test_the_phone_opens_settings_on_the_ai_section(self):
        src = _read("mobile/src/app/assistant.tsx")
        self.assertIn('const SETTINGS_AI = "/settings?find=ai%20assistant";',
                      src)
        self.assertIn("to={SETTINGS_AI}", src)
        settings = _read("mobile/src/app/settings.tsx")
        # Settings seeds its search from the link, and the words name the
        # AI section's own keywords
        self.assertIn("useLocalSearchParams<{ find?: string }>()", settings)
        self.assertIn('llm: hit("ai assistant ', settings)


if __name__ == "__main__":
    unittest.main()
