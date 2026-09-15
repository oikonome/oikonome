"""/api/help serves the shipped docs/*.md as topics (single
source with the repo docs) plus a short FAQ, auth-gated like everything
else."""

import unittest
import uuid

from fastapi.testclient import TestClient

from .util import _ensure_db


class HelpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"help-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})

    def test_requires_auth(self):
        anon = TestClient(self.client.app)
        self.assertEqual(anon.get("/api/help").status_code, 401)

    def test_serves_faq_and_every_doc(self):
        r = self.client.get("/api/help")
        self.assertEqual(r.status_code, 200)
        topics = {t["id"]: t for t in r.json()["topics"]}
        # FAQ leads, quickstart next (curated order)
        ids = [t["id"] for t in r.json()["topics"]]
        self.assertEqual(ids[0], "faq")
        self.assertEqual(ids[1], "quickstart")
        # every shipped doc is a topic with its real title + body
        for doc in ("quickstart", "importing", "troubleshooting",
                    "reverse-proxy", "community-scripts"):
            self.assertIn(doc, topics)
            self.assertTrue(topics[doc]["title"])
            self.assertGreater(len(topics[doc]["body"]), 200)
        self.assertIn("Quick start", topics["quickstart"]["title"])
        self.assertIn("compose", topics["quickstart"]["body"])

    def test_docs_dir_env_override(self):
        """OIKONOME_DOCS_DIR wins over path probing — the escape hatch for
        layouts where neither the repo walk nor /app/docs applies (a
        site-packages install resolves the relative walk outside /app, and
        Help would serve zero topics)."""
        import importlib
        import os
        from unittest import mock

        from oikonome.web import help as help_mod
        with mock.patch.dict(os.environ, {"OIKONOME_DOCS_DIR": "/nonexistent"}):
            importlib.reload(help_mod)
            self.assertEqual(str(help_mod.DOCS_DIR), "/nonexistent")
        importlib.reload(help_mod)
        self.assertTrue((help_mod.DOCS_DIR / "faq.md").exists())

    def test_every_registered_guide_ships(self):
        """Every id in the curated guide order must exist as a real
        docs/guides file with substance — a registry/file drift here means
        a page's ? deep-link lands on the wrong topic silently."""
        from oikonome.web import help as help_mod
        r = self.client.get("/api/help")
        topics = {t["id"]: t for t in r.json()["topics"]}
        for gid in help_mod._GUIDE_ORDER:
            self.assertIn(gid, topics, f"guide '{gid}' registered but missing")
            self.assertEqual(topics[gid]["group"], "guides", gid)
            self.assertGreater(len(topics[gid]["body"]), 400,
                               f"guide '{gid}' is a stub")


if __name__ == "__main__":
    unittest.main()


class HelpSupportAddressTests(unittest.TestCase):
    """In-app help must name the address this instance actually answers.

    The shipped markdown carries the project's own inbox. Served verbatim by
    someone else's install, it tells their household to write to a stranger
    about their finances — and the operator has no way to correct it, because
    the file is inside the image. The failure is silent: /help renders, the
    address is simply the wrong one."""

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True

    def _topics(self):
        from fastapi.testclient import TestClient
        from oikonome.web.app import app
        c = TestClient(app)
        c.post("/api/signup", data={
            "email": f"sup-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        r = c.get("/api/help")
        self.assertEqual(r.status_code, 200)
        return {t["id"]: t["body"] for t in r.json()["topics"]}

    def test_operator_address_replaces_the_phrase(self):
        import os
        from unittest import mock
        from oikonome.web.help import SUPPORT_PHRASE
        mine = "help@example.org"
        with mock.patch.dict(os.environ,
                             {"OIKONOME_SUPPORT_EMAIL": mine}):
            bodies = self._topics()
        joined = "\n".join(bodies.values())
        self.assertIn(mine, joined)
        self.assertNotIn(SUPPORT_PHRASE, joined)

    def test_unset_leaves_the_phrase_and_names_no_address(self):
        import os
        from unittest import mock
        from oikonome.web.help import SUPPORT_PHRASE
        with mock.patch.dict(os.environ,
                             {"OIKONOME_SUPPORT_EMAIL": ""}):
            bodies = self._topics()
        joined = "\n".join(bodies.values())
        self.assertIn(SUPPORT_PHRASE, joined)
        self.assertNotIn("support@", joined)
