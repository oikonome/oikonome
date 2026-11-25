"""A retirement answer given as an integer too large for a float is a bad
answer: the settings save refuses it (400) and a restore drops that one
answer, keeping the rest of the household.

JSON hands over a 400-digit integer as readily as any number. The answer
cleaner, shared by the settings door and the restore, asked whether a
claim age was a whole number with `float(v)` before the guard that catches
the float overflow, so the door answered 500 and the restore's one
transaction stopped on that cell.
"""

import csv
import io
import json
import os
import unittest
import uuid
import zipfile

from fastapi.testclient import TestClient

from oikonome.engine import budget
from oikonome.sync import restore

from .util import _ensure_db, make_db, write_config

HUGE = 10 ** 400
PW = "correct-horse-battery"


def _settings_zip(config: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=["config"])
        w.writeheader()
        w.writerow({"config": json.dumps(config)})
        z.writestr("tenant_settings.csv", s.getvalue())
    return buf.getvalue()


class TheCleanerNeverRaises(unittest.TestCase):
    def test_every_answer_past_float_range_reads_as_no_answer(self):
        for key in restore.RET_WIZARD_ANSWERS:
            for v in (HUGE, -HUGE):
                self.assertIsNone(restore.ret_wizard_answer(key, v), key)


class TheRestoreDropsOnlyThatAnswer(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def test_a_huge_claim_age_in_an_archive_costs_only_that_answer(self):
        restore.restore_zip(self.conn, _settings_zip({
            "food_monthly": 700,
            "ret_wizard_answers": {"ssage": HUGE, "age": 41}}))
        cfg = budget.load_config(self.conn)
        self.assertEqual(cfg["ret_wizard_answers"], {"age": 41})
        self.assertEqual(cfg["food_monthly"], 700)


class TheSettingsDoorRefusesIt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        from oikonome.web import security
        cls.security = security
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app, raise_server_exceptions=False)
        security._limiter._hits.clear()
        r = cls.client.post("/api/signup", data={
            "email": f"age-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        assert r.status_code == 200, r.text
        cls.client.post("/api/auth/elevate", json={"password": PW})

    def test_a_huge_claim_age_answers_400(self):
        for key in ("ssage", "age", "spend"):
            self.security._limiter._hits.clear()
            r = self.client.post(
                "/api/settings",
                content=json.dumps({"ret_wizard_answers": {key: HUGE}}),
                headers={"content-type": "application/json"})
            self.assertEqual(r.status_code, 400, (key, r.text))


if __name__ == "__main__":
    unittest.main()
