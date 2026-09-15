"""/favicon.ico must serve the app icon: a 404 there leaves bare bookmarks
and bookmark managers showing a blank globe."""

import unittest

from fastapi.testclient import TestClient


class FaviconTests(unittest.TestCase):
    def test_favicon_ico_serves_the_icon(self):
        from oikonome.web import app as appmod
        if appmod._SPA_DIST is None:
            self.skipTest("SPA dist not built in this checkout")
        r = TestClient(appmod.app).get("/favicon.ico")
        self.assertEqual(r.status_code, 200)
        self.assertIn("image/svg+xml", r.headers["content-type"])


if __name__ == "__main__":
    unittest.main()
