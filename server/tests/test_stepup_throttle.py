"""Every password-verifying door is throttled AND recorded.

Without both, a thief with a live cookie but not the password can grind
guesses against /api/sessions/revoke (and /api/tokens/revoke and DELETE
/api/users/{id}) at network speed — and a step-up helper that feeds no
failure into the auto-ban, account-lock or Turnstile-risk signals makes
the guessing invisible as well.
"""
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.web import security

from .util import _ensure_db


class StepUpThrottleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        security._limiter._hits.clear()
        self.client = TestClient(self.appmod.app)
        self.client.post("/api/signup", data={
            "email": f"su-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})

    def test_sessions_revoke_password_guessing_is_rate_limited(self):
        codes = [self.client.post("/api/sessions/revoke",
                                  json={"all_others": True,
                                        "password": f"guess-{i}"}).status_code
                 for i in range(14)]
        self.assertIn(429, codes, f"never throttled: {codes}")

    def test_every_password_door_declares_a_limit(self):
        """Structural: each of these three doors carries a limiter, so a
        future route cannot quietly go without one."""
        from oikonome.web import api as apimod
        routes = []
        for r in list(self.appmod.app.routes) + list(apimod.router.routes):
            routes.append((getattr(r, "path", ""),
                           tuple(getattr(r, "methods", ()) or ()), r))
        # api.router is mounted under a prefix, so match on the suffix
        wanted = [("/api/sessions/revoke", "POST"),
                  ("/tokens/revoke", "POST"),
                  ("/api/users/{user_id}", "DELETE")]
        for path, method in wanted:
            with self.subTest(path=path):
                match = [r for p, m, r in routes
                         if p.endswith(path) and method in m]
                self.assertTrue(match, f"route missing: {path}")
                dep = getattr(match[0], "dependant", None)
                names = [getattr(d.call, "__qualname__", "")
                         for d in (dep.dependencies if dep else [])]
                self.assertTrue(any("limit" in n for n in names),
                                f"{path} has no rate limit: {names}")

    def test_failed_step_up_records_an_auth_failure(self):
        """The guess must feed the same ban/lock signals as a failed login."""
        seen = []
        real = security.record_auth_failure
        security.record_auth_failure = (
            lambda ip, email="", route="login": seen.append((ip, route)))
        try:
            self.client.post("/api/sessions/revoke",
                             json={"all_others": True, "password": "wrong"})
        finally:
            security.record_auth_failure = real
        self.assertTrue(seen, "a wrong step-up password recorded nothing")
        self.assertEqual(seen[0][1], "step_up")

    def test_password_email_and_delete_doors_record_a_wrong_password(self):
        """These three verify the password themselves rather than through
        the step-up helper. A bare 401 from them is silent — a stolen
        session could grind the password with no strike, no lock and no
        human-check risk — so every password door records the guess."""
        doors = [
            ("password_change", lambda: self.client.post(
                "/api/password/change",
                json={"current_password": "wrong",
                      "new_password": "another-long-passphrase"})),
            ("email_change", lambda: self.client.post(
                "/api/email/change",
                data={"new_email": "someone@example.dev",
                      "password": "wrong"})),
            ("account_delete", lambda: self.client.post(
                "/api/account/delete", data={"password": "wrong"})),
        ]
        for route, call in doors:
            with self.subTest(route=route):
                seen = []
                real = security.record_auth_failure
                security.record_auth_failure = (
                    lambda ip, email="", route="login":
                        seen.append((ip, email, route)))
                try:
                    r = call()
                finally:
                    security.record_auth_failure = real
                self.assertEqual(r.status_code, 401, r.text)
                self.assertTrue(seen, f"{route}: wrong password recorded "
                                      "nothing")
                self.assertEqual(seen[0][2], route)
                self.assertTrue(seen[0][1], f"{route}: no email recorded")


if __name__ == "__main__":
    unittest.main()
