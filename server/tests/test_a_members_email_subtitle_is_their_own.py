"""A member's and a viewer's Settings list says what their OWN daily email
does — off, off for them, not reaching them, or the hour it goes out —
never a default.

The household's schedule and recipient list are the owner's to read, so
the web's email subtitle for anyone else is worked out from /api/me's
daily_email and their own /api/settings by myEmailSubtitle
(webapp/src/recipients.ts), on the same verdict the Daily email card shows.
Here the real replies a member gets, after the owner turns the daily email
off, moves its hour or saves a mail list, and after the member mutes
themselves, are fed to the real client function under node, and the answer
must match what is true: a "daily 7am" nobody set, or one shown to someone
the email does not reach, would be a wrong figure. (Mobile shows the household schedule
row to the owner only, so it has no such subtitle.)
"""

import json
import os
import pathlib
import shutil
import subprocess
import unittest
import uuid

from fastapi.testclient import TestClient

import oikonome
from oikonome.db import tenancy
from oikonome.engine import budget

from .util import _ensure_db

PW = "correct-horse-battery"
_APP = pathlib.Path(oikonome.__file__).parent.parent.parent
_WEB = _APP / "webapp" / "src" / "recipients.ts"
_SETTINGS = _APP / "webapp" / "src" / "pages" / "Settings.tsx"
_RUNNER = """
const [url, me, settings] = JSON.parse(process.argv[1]);
const m = await import(url);
console.log(JSON.stringify(m.myEmailSubtitle(me, settings)));
"""


class AMembersEmailSubtitleIsTheirOwn(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node or not _WEB.exists():
            raise unittest.SkipTest("node or webapp/ not present")
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        security._limiter._hits.clear()
        cls.owner = TestClient(appmod.app)
        r = cls.owner.post("/api/signup", data={
            "email": f"subown-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        assert r.status_code == 200, r.text
        cls.tid = cls.owner.get("/api/me").json()["tenant_id"]
        inv = cls.owner.post("/api/invites", json={
            "label": "m", "role": "member", "password": PW}).json()
        token = inv["url"].rsplit("token=", 1)[1]
        cls.member = TestClient(appmod.app)
        cls.member_email = f"submem-{uuid.uuid4().hex[:8]}@example.dev"
        r = cls.member.post("/api/invite/claim", json={
            "token": token, "email": cls.member_email,
            "password": "family-member-pass"})
        assert r.status_code == 200, r.text

    def setUp(self):
        self._recipients(None)

    def _config(self, key, value) -> None:
        conn = tenancy.tenant_connect(self.tid)
        try:
            with budget.config_txn(conn) as cfg:
                if value is None:
                    cfg.pop(key, None)
                else:
                    cfg[key] = value
        finally:
            conn.close()

    def _schedule(self, daily: dict) -> None:
        self._config("email_schedule", {"daily": daily})

    def _recipients(self, emails) -> None:
        self._config("email_recipients", emails)

    def _subtitle(self):
        me = self.member.get("/api/me").json()
        settings = self.member.get("/api/settings").json()
        r = subprocess.run(
            [self.node, "--experimental-strip-types", "--input-type=module",
             "-e", _RUNNER, json.dumps([_WEB.as_uri(), me, settings])],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_the_household_daily_email_off_reads_off(self):
        self._schedule({"on": False, "hour": 7})
        self.assertEqual(self._subtitle(), "daily email off")

    def test_another_hour_reads_that_hour(self):
        self._schedule({"on": True, "hour": 18})
        self.assertEqual(self._subtitle(), "daily 6pm")
        self._schedule({"on": True, "hour": 9})
        self.assertEqual(self._subtitle(), "daily 9am")

    def test_a_member_who_muted_themselves_reads_off_for_them(self):
        self._schedule({"on": True, "hour": 7})
        r = self.member.post("/api/me/email", json={"muted": True})
        self.assertEqual(r.status_code, 200, r.text)
        try:
            self.assertEqual(self._subtitle(), "daily email off for you")
        finally:
            self.member.post("/api/me/email", json={"muted": False})

    def test_a_list_that_leaves_the_member_out_reads_not_receiving(self):
        self._schedule({"on": True, "hour": 7})
        self._recipients(["someone-else@example.dev"])
        settings = self.member.get("/api/settings").json()
        self.assertTrue(settings["email_list_configured"])
        self.assertFalse(settings["email_on_list"])
        self.assertEqual(self._subtitle(), "not receiving it")

    def test_a_member_who_has_not_accepted_reads_not_receiving(self):
        self._schedule({"on": True, "hour": 7})
        self._recipients([self.member_email])
        settings = self.member.get("/api/settings").json()
        self.assertTrue(settings["email_on_list"])
        mine = [r for r in settings["email_recipient_status"]
                if r["email"] == self.member_email]
        self.assertEqual(len(mine), 1)
        self.assertFalse(mine[0].get("mailed"))
        self.assertEqual(self._subtitle(), "not receiving it")

    def test_the_settings_list_uses_it_for_everyone_but_the_owner(self):
        src = _SETTINGS.read_text(encoding="utf-8")
        i = src.index("const summary: Record<string")
        email = src[i:src.index("connections:", i)]
        self.assertIn("owner ?", email)
        self.assertIn("myEmailSubtitle(meQ.data, q.data)", email)


if __name__ == "__main__":
    unittest.main()
