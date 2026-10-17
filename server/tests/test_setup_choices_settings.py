"""The setup wizard's three choices are settings like any other: closed
vocabularies, validated on save, published with their effective defaults.

`history_months` is 1, 3, 6 or 12 (null = everything): it narrows what a
bank link and a file import keep, so a value the save guessed at would
quietly drop history. `setup_mode` is auto or manual, and choosing manual
turns automatic bill detection off unless the same save says otherwise.
`bill_detection_auto` is a plain boolean. The onboarding read mirrors all
three so the wizard's summary can show them.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from .util import _ensure_db


class SetupChoicesSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.c = TestClient(self.app)
        self.c.post("/api/signup", data={
            "email": f"setup-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})

    def _get(self):
        r = self.c.get("/api/settings")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_a_fresh_household_reads_the_defaults(self):
        v = self._get()
        self.assertIsNone(v["history_months"])
        self.assertEqual(v["setup_mode"], "auto")
        self.assertIs(v["bill_detection_auto"], True)
        o = self.c.get("/api/onboarding").json()
        self.assertIsNone(o["history_months"])
        self.assertEqual(o["setup_mode"], "auto")
        self.assertIs(o["bill_detection_auto"], True)

    def test_history_months_round_trips_and_null_clears(self):
        for n in (1, 3, 6, 12):
            r = self.c.post("/api/settings", json={"history_months": n})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(self._get()["history_months"], n)
        self.assertEqual(
            self.c.get("/api/onboarding").json()["history_months"], 12)
        r = self.c.post("/api/settings", json={"history_months": None})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(self._get()["history_months"])

    def test_history_months_outside_the_choices_is_refused(self):
        self.c.post("/api/settings", json={"history_months": 6})
        for bad in (2, 24, 0, -3, "6", 6.5, True, [6], {"m": 6}):
            r = self.c.post("/api/settings", json={"history_months": bad})
            self.assertEqual(r.status_code, 400, (bad, r.text))
        # a refused save changes nothing
        self.assertEqual(self._get()["history_months"], 6)

    def test_manual_setup_turns_detection_off(self):
        r = self.c.post("/api/settings", json={"setup_mode": "manual"})
        self.assertEqual(r.status_code, 200, r.text)
        v = self._get()
        self.assertEqual(v["setup_mode"], "manual")
        self.assertIs(v["bill_detection_auto"], False)
        o = self.c.get("/api/onboarding").json()
        self.assertEqual(o["setup_mode"], "manual")
        self.assertIs(o["bill_detection_auto"], False)

    def test_manual_setup_keeps_detection_the_same_save_asks_for(self):
        r = self.c.post("/api/settings", json={"setup_mode": "manual",
                                               "bill_detection_auto": True})
        self.assertEqual(r.status_code, 200, r.text)
        v = self._get()
        self.assertEqual(v["setup_mode"], "manual")
        self.assertIs(v["bill_detection_auto"], True)

    def test_auto_setup_leaves_detection_as_it_stands(self):
        self.c.post("/api/settings", json={"bill_detection_auto": False})
        self.c.post("/api/settings", json={"setup_mode": "auto"})
        v = self._get()
        self.assertEqual(v["setup_mode"], "auto")
        self.assertIs(v["bill_detection_auto"], False)

    def test_bad_setup_values_are_refused(self):
        for body in ({"setup_mode": "magic"}, {"setup_mode": 1},
                     {"bill_detection_auto": "no"},
                     {"bill_detection_auto": 0},
                     {"bill_detection_auto": None}):
            r = self.c.post("/api/settings", json=body)
            self.assertEqual(r.status_code, 400, (body, r.text))
        v = self._get()
        self.assertEqual(v["setup_mode"], "auto")
        self.assertIs(v["bill_detection_auto"], True)


if __name__ == "__main__":
    unittest.main()
