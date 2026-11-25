"""The continuity note says where the will and the passwords are, so only
the household owner reads its text — the same people the PDF it heads is
printed for. Everyone else in the household is told a note exists and
nothing of what it says: a member, a viewer, a script token, and the
shared demonstration login (an owner-role login every visitor holds).
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"
MEMBER_PW = "another-fine-passphrase"
NOTE = "Passwords: the manager.\nWill: the blue folder."


def _signup(app):
    c = TestClient(app)
    email = f"note-{uuid.uuid4().hex[:8]}@example.dev"
    data = {"email": email, "password": PW}
    if os.environ.get("OIKONOME_HOSTED"):
        from oikonome.auth import signup_invites
        admin = tenancy.admin_connect()
        try:
            data["invite"] = signup_invites.mint(admin, email)
        finally:
            admin.close()
    r = c.post("/api/signup", data=data)
    assert r.status_code == 200, r.text
    return c, email


class ContinuityNoteReaders(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app
        cls.owner, cls.email = _signup(cls.app)
        r = cls.owner.put("/api/continuity", json={"note": NOTE,
                                                   "prepared_for": "Sam"})
        assert r.status_code == 200, r.text
        cls.member = cls._claim("member")
        cls.viewer = cls._claim("viewer")

    @classmethod
    def _claim(cls, role):
        r = cls.owner.post("/api/invites", json={"label": role, "role": role,
                                                 "password": PW})
        assert r.status_code == 200, r.text
        token = r.json()["url"].rsplit("token=", 1)[1]
        c = TestClient(cls.app)
        got = c.post("/api/invite/claim", json={
            "token": token,
            "email": f"{role}-{uuid.uuid4().hex[:8]}@example.dev",
            "password": MEMBER_PW})
        assert got.status_code == 200, got.text
        return c

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    def _withheld(self, got):
        self.assertEqual(got.status_code, 200, got.text)
        d = got.json()
        self.assertEqual(d["note"], "")
        self.assertNotIn("manager", got.text)
        self.assertIs(d["note_set"], True)
        self.assertIs(d["note_readable"], False)

    def test_the_owner_reads_the_note(self):
        d = self.owner.get("/api/continuity").json()
        self.assertEqual(d["note"], NOTE)
        self.assertIs(d["note_set"], True)
        self.assertIs(d["note_readable"], True)

    def test_a_member_is_told_a_note_exists_but_not_what_it_says(self):
        self._withheld(self.member.get("/api/continuity"))

    def test_a_viewer_is_told_a_note_exists_but_not_what_it_says(self):
        self._withheld(self.viewer.get("/api/continuity"))

    def test_a_script_token_does_not_read_the_note(self):
        r = self.owner.post("/api/tokens", json={"name": "collector",
                                                 "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        bearer = TestClient(self.app)
        bearer.cookies.clear()
        bearer.headers["Authorization"] = "Bearer " + r.json()["token"]
        got = bearer.get("/api/continuity")
        if got.status_code == 200:
            self._withheld(got)
        else:
            self.assertNotIn("manager", got.text)

    def test_no_note_reads_as_none_for_every_role(self):
        c, _ = _signup(self.app)
        d = c.get("/api/continuity").json()
        self.assertIs(d["note_set"], False)
        self.assertIs(d["note_readable"], True)

    def test_the_shared_login_is_told_a_note_exists_but_not_what_it_says(self):
        c, email = _signup(self.app)
        self.assertEqual(c.put("/api/continuity",
                               json={"note": NOTE}).status_code, 200)
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE users SET second_factor_waived=TRUE "
                          "WHERE email=%s", (email,))
        finally:
            admin.close()
        self._withheld(c.get("/api/continuity"))

    def test_a_non_owner_still_cannot_write_or_print_it(self):
        for c in (self.member, self.viewer):
            self.assertEqual(c.put("/api/continuity",
                                   json={"note": "x"}).status_code, 403)
            self.assertEqual(c.post("/api/export/token", json={
                "kind": "continuity", "password": MEMBER_PW}).status_code,
                403)
        self.assertEqual(self.owner.get("/api/continuity").json()["note"],
                         NOTE)

    def test_the_settings_read_never_carries_the_note(self):
        for c in (self.owner, self.member, self.viewer):
            got = c.get("/api/settings")
            self.assertEqual(got.status_code, 200, got.text)
            self.assertNotIn("manager", got.text)


if __name__ == "__main__":
    unittest.main()
