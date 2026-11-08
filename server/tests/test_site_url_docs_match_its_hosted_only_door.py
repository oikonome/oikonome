"""OIKONOME_SITE_URL's documentation says who it works for.

The "No account?" link is a hosted door: a self-hosted instance deliberately
has no way in from its sign-in page, so setting the variable there does
nothing. An operator reads the comment, sets it, and sees no link, while the
privacy URL beside it does render. So the comments have to say hosted only,
and the door has to stay what they say it is.
"""

import os
import pathlib
import re
import unittest
from unittest.mock import patch

import oikonome
from fastapi.testclient import TestClient

from .util import _ensure_db

_DOCKER = pathlib.Path(oikonome.__file__).parent.parent.parent / "docker"


def _comment_above(text: str, marker: str) -> str:
    """The comment lines directly above the first line holding `marker`,
    commented-out settings excluded."""
    lines = text.splitlines()
    at = next(i for i, ln in enumerate(lines) if marker in ln)
    out = []
    for ln in reversed(lines[:at]):
        s = ln.strip()
        if not s.startswith("#") or re.match(r"#\s*[A-Z_]+=", s):
            break
        out.append(s)
    return " ".join(reversed(out))


class SiteUrlDocs(unittest.TestCase):

    def test_env_example_says_the_site_link_is_hosted_only(self):
        c = _comment_above((_DOCKER / ".env.example").read_text(),
                           "OIKONOME_SITE_URL=")
        self.assertRegex(c, r"(?i)hosted")

    def test_compose_comment_says_the_site_link_is_hosted_only(self):
        c = _comment_above((_DOCKER / "compose.yaml").read_text(),
                           "OIKONOME_SITE_URL:")
        self.assertRegex(c, r"(?i)hosted")

    def test_self_host_has_no_door_even_with_the_variable_set(self):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        client = TestClient(appmod.app)
        with patch.dict(os.environ, {"OIKONOME_HOSTED": "",
                                     "OIKONOME_OPEN_SIGNUP": "",
                                     "OIKONOME_SITE_URL": "https://example.org",
                                     "OIKONOME_PRIVACY_URL": "https://example.org/p"}):
            access = client.get("/api/access").json()
            page = client.get("/login").text
        self.assertIsNone(access["site_url"])
        self.assertNotIn("No account?", page)
        # the privacy link beside it is not hosted-only
        self.assertIn("https://example.org/p", page)


if __name__ == "__main__":
    unittest.main()
