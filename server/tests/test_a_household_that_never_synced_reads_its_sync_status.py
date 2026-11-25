"""A household that never synced reads its sync status in the documented
shape: state "none", no progress, and both times present as null — a
client that reads `started_at` finds it there, not missing
(specs/invariants-and-fuzzing.md, F3; tests/api_conformance.py checks
every reply the suite gets)."""

import unittest

from . import body_fuzz


class ANeverSyncedHouseholdReadsItsStatus(unittest.TestCase):
    def test_the_status_carries_every_documented_field(self):
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        client, _e, _tid = body_fuzz.signup(appmod.app, "nosync")
        try:
            r = client.get("/api/jobs/sync/status")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), {"state": "none", "progress": {},
                                        "started_at": None,
                                        "updated_at": None})
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
