"""An iOS app an operator names must be able to sign in with a passkey.

Apple's associated-domains check fetches
``/.well-known/apple-app-site-association`` (no extension, JSON) from the
domain the app lists at build time and, only if the ``webcredentials``
service names this app's Team ID + bundle id, offers that domain's
passkeys inside the app. The ids are configuration (OIKONOME_IOS_APP_IDS);
a server with no app of its own serves an empty list.
"""

import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.auth import passkeys
from oikonome.web.app import app

APP_ID = "ABCDE12345.org.example.oikonome"


class AppleAppSiteAssociationTests(unittest.TestCase):
    def test_served_public_as_json_with_the_configured_app_id(self):
        with mock.patch.dict(os.environ, {"OIKONOME_IOS_APP_IDS": APP_ID}):
            r = TestClient(app).get("/.well-known/apple-app-site-association")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("application/json"))
        self.assertEqual(r.json(), {"webcredentials": {"apps": [APP_ID]}})

    def test_statement_takes_only_well_formed_ids(self):
        # a Team ID is ten alphanumerics; anything else is not an app id
        # and must not be published
        with mock.patch.dict(os.environ, {"OIKONOME_IOS_APP_IDS":
                                          f" {APP_ID} ,short.org.example,junk"}):
            apps = passkeys.apple_app_site_association()["webcredentials"]["apps"]
        self.assertEqual(apps, [APP_ID])

    def test_no_configured_app_serves_an_empty_list(self):
        with mock.patch.dict(os.environ, {"OIKONOME_IOS_APP_IDS": ""}):
            r = TestClient(app).get("/.well-known/apple-app-site-association")
        self.assertEqual(r.json(), {"webcredentials": {"apps": []}})


if __name__ == "__main__":
    unittest.main()
