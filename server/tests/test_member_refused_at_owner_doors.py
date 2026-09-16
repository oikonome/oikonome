"""Every door that guards itself with an inline `_owner_only` refuses a
household MEMBER.

These routes sit outside the `permissions.OWNER_ONLY` prefix list that
the client-chrome lockstep test pins, so nothing else exercised the
member → 403 branch for them. The list here is every `_owner_only(user)`
call site in the API; a new one is added here when it is added there.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PASSWORD = "correct-horse-battery"

_EID = str(uuid.uuid4())
# (method, route template as declared, concrete path, body)
DOORS = [
    ("get", "/tokens", "/api/tokens", None),
    ("post", "/tokens", "/api/tokens", {"label": "x"}),
    ("post", "/tokens/revoke", "/api/tokens/revoke", {"id": str(uuid.uuid4())}),
    ("post", "/business/entities/{entity_id}/delete-forever",
     f"/api/business/entities/{_EID}/delete-forever", {"confirm": "x"}),
    ("get", "/import/enrich", "/api/import/enrich", None),
    ("post", "/import/enrich", "/api/import/enrich", {"limit": 10}),
    ("post", "/notify/phone", "/api/notify/phone", {"number": "+15555550100"}),
    ("post", "/notify/phone/verify", "/api/notify/phone/verify",
     {"code": "000000"}),
    ("post", "/notify/test", "/api/notify/test", {"channel": "email"}),
    ("post", "/support-access", "/api/support-access", {"hours": 1}),
    ("post", "/support-access/revoke", "/api/support-access/revoke", {}),
]


class MemberOwnerDoorsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod
        from oikonome.auth import passwords
        tag = uuid.uuid4().hex[:8]
        admin = tenancy.admin_connect()
        try:
            cls.tid = tenancy.create_tenant(admin, f"door-own-{tag}@x.dev")
            for role in ("owner", "member"):
                admin.execute(
                    "INSERT INTO users (tenant_id, email, password_hash, "
                    "role, verified_at) VALUES (%s, %s, %s, %s, now())",
                    (cls.tid, f"door-{role}-{tag}@x.dev",
                     passwords.hash_password(PASSWORD), role))
        finally:
            admin.close()
        cls.member = TestClient(appmod.app)
        # the login limiter is per process and other modules sign in too
        from oikonome.web import security
        security._limiter._hits.clear()
        r = cls.member.post("/login", data={
            "email": f"door-member-{tag}@x.dev", "password": PASSWORD},
            follow_redirects=False)
        assert r.status_code == 303, r.text
        assert cls.member.get("/api/me").json()["role"] == "member"

    def test_every_inline_owner_door_refuses_a_member(self):
        from oikonome.web import security
        for method, _tpl, path, body in DOORS:
            security._limiter._hits.clear()
            r = (self.member.get(path) if method == "get"
                 else getattr(self.member, method)(path, json=body))
            self.assertEqual(r.status_code, 403, f"{path}: {r.text[:200]}")

    def test_the_list_is_complete(self):
        # the doors above are every inline _owner_only call site in the
        # API module (routes under a permissions.OWNER_ONLY prefix are
        # pinned elsewhere)
        import re
        from pathlib import Path
        src = Path(__file__).resolve().parents[1].joinpath(
            "oikonome", "web", "api.py").read_text()
        routes = set()
        # a handler's body up to the next decorator — the window must not
        # run into the following route's own guard
        for m in re.finditer(
                r'@router\.(?:post|get|delete|put|patch)\("([^"]+)"[^\n]*\n'
                r'(?:(?!@router\.).*\n){0,30}?[^\n]*_owner_only\(user\)', src):
            routes.add(m.group(1))
        listed = {t for _, t, _, _ in DOORS}
        self.assertTrue(routes, "no owner doors found — regex drifted?")
        self.assertEqual(routes - listed, set(),
                         "owner-only doors missing from this test")


if __name__ == "__main__":
    unittest.main()
