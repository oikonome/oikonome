"""Revoking an operator passkey kills its console sessions, and the
security log cannot be forged.
"""
import logging
import unittest

from .test_admin_passkey import _Base, _admin


class PasskeyRevocationKillsItsSessionsTests(_Base):
    """Revoking the credential must revoke what the credential has done.

    `admin_sessions.credential_id` is filled in by `_issue_session`, so a
    `passkey_delete` that removes only the credential row leaves every
    session that key authenticated valid for the rest of SESSION_TTL (4h).
    An operator console can be passkey-only and reachable over a private
    network only — the passkey IS the credential there — and it drives a
    root host-agent, so a stolen authenticator that had already signed in
    would keep full cross-tenant access for hours after being "removed".
    """

    def _revoke(self):
        """Sign in with a passkey, then remove that passkey. Returns the
        token_hash of the session it authenticated, CAPTURED BEFORE the
        delete — migration 072's FK is ON DELETE SET NULL, so afterwards
        `credential_id` is NULL on any session that survived and querying by
        it cannot tell 'revoked' from 'still live' — a check made after the
        delete would pass against unfixed code."""
        self._token_login()
        self._enrol()
        self._passkey_login()
        cred_id = str(_admin("SELECT id FROM admin_credentials")[0]["id"])
        rows = _admin("SELECT token_hash FROM admin_sessions "
                      "WHERE credential_id = %s::uuid", (cred_id,))
        self.assertTrue(rows, "precondition: the passkey sign-in must record "
                              "the credential on the session row")
        held = rows[0]["token_hash"]
        ticket = self._ticket()
        r = self.client.post("/admin/console/passkey/delete",
                             data={"cred_id": cred_id, "stepup": ticket},
                             follow_redirects=False)
        self.assertIn(r.status_code, (200, 303), r.text)
        return held

    def test_removing_a_passkey_revokes_the_sessions_it_authenticated(self):
        held = self._revoke()
        self.assertEqual(
            [], _admin("SELECT 1 FROM admin_sessions WHERE token_hash = %s",
                       (held,)),
            "the session the revoked passkey authenticated is still in the "
            "table — removing a key has to mean the key can no longer ACT, "
            "not merely that it cannot sign in again")

    def test_the_session_holding_the_revoked_key_is_locked_out_immediately(
            self):
        """End to end, from the browser's side: the cookie must stop working
        on the very next request, not at expiry."""
        self._revoke()
        self.assertEqual(
            401,
            self.client.post("/admin/console/passkey/options",
                             json={}).status_code,
            "the cookie minted by the revoked passkey still authenticates — "
            "this console reaches the root host-agent")

    def test_a_malformed_cred_id_is_still_just_a_bad_id(self):
        """The session delete runs ahead of admin_passkeys.delete, so an
        unparseable id must still be answered as a bad id there."""
        self._token_login()
        self._enrol()
        self._passkey_login()
        ticket = self._ticket()
        r = self.client.post("/admin/console/passkey/delete",
                             data={"cred_id": "not-a-uuid", "stepup": ticket},
                             follow_redirects=False)
        self.assertLess(r.status_code, 500, "a malformed id 500s")
        self.assertTrue(_admin("SELECT 1 FROM admin_credentials"),
                        "a malformed id must not remove anything")


class CliPasskeyRevocationKillsSessionsTests(_Base):
    """The CLI's --remove/--reset must kill sessions too, not only the
    console's delete: the documented recovery path would otherwise leave a
    4h root console cookie live, and after a full reset step-up on
    destructive operations is skipped."""

    def test_cli_remove_kills_sessions_that_key_authenticated(self):
        from oikonome import cli
        self._token_login()
        self._enrol()
        self._passkey_login()
        cred_id = str(_admin("SELECT id FROM admin_credentials")[0]["id"])
        held = _admin("SELECT token_hash FROM admin_sessions "
                      "WHERE credential_id = %s::uuid", (cred_id,))[0][
                          "token_hash"]
        self.assertEqual(cli.admin_keys(remove=cred_id), 0)
        self.assertEqual(
            [], _admin("SELECT 1 FROM admin_sessions WHERE token_hash = %s",
                       (held,)),
            "CLI --remove left the session alive")
        self.assertEqual(
            401,
            self.client.post("/admin/console/passkey/options",
                             json={}).status_code)

    def test_cli_reset_kills_every_admin_session(self):
        from oikonome import cli
        self._token_login()
        self._enrol()
        self._passkey_login()
        n_before = _admin("SELECT count(*) AS n FROM admin_sessions")[0]["n"]
        self.assertGreater(n_before, 0)
        self.assertEqual(cli.admin_keys(reset=True), 0)
        self.assertEqual(
            0, _admin("SELECT count(*) AS n FROM admin_sessions")[0]["n"],
            "CLI --reset left admin_sessions live")
        self.assertEqual(
            401,
            self.client.post("/admin/console/passkey/options",
                             json={}).status_code)


class SecurityLogCannotBeForgedTests(unittest.TestCase):
    """`_sec_event` lines are PARSED BY fail2ban, which jails on
    `oikonome-security event=autoban ip=<HOST>` matched anywhere in the line.
    Several call sites pass request-controlled values — `path=` is the ASGI
    scope path, already percent-DECODED — so a request to
    `/x event=autoban ip=8.8.8.8` emitted a second, forged record and let an
    unauthenticated client choose which address the host firewall banned.
    """

    def _emit(self, **fields):
        from oikonome.web import security
        with self.assertLogs(security._seclog, level=logging.WARNING) as cap:
            security._sec_event("body_too_large", "203.0.113.7", **fields)
        return cap.output[0]

    def test_a_crafted_path_cannot_forge_a_second_record(self):
        line = self._emit(path="/x event=autoban ip=8.8.8.8")
        self.assertEqual(
            1, line.count("event="),
            f"a second event= record was forged into the log line: {line}")
        self.assertNotIn(
            "ip=8.8.8.8", line,
            "an unauthenticated request chose the IP fail2ban would ban")

    def test_a_newline_cannot_forge_a_whole_line(self):
        line = self._emit(path="/x\noikonome-security event=autoban "
                               "ip=8.8.8.8")
        self.assertNotIn("\n", line, "a raw newline forges an entire record")
        self.assertEqual(1, line.count("event="))

    def test_ordinary_values_stay_readable(self):
        """The guard must not make the log useless — real paths, routes and
        addresses have to survive intact or operators stop trusting it."""
        line = self._emit(path="/api/v1/accounts/42", route="login", limit=1024)
        self.assertIn("path=/api/v1/accounts/42", line)
        self.assertIn("route=login", line)
        self.assertIn("limit=1024", line)
        self.assertIn("ip=203.0.113.7", line)

    def test_an_ipv6_address_survives(self):
        line = self._emit(banned="[2001:db8::1]")
        self.assertIn("banned=[2001:db8::1]", line)


class TokenRotationEndsTokenSessionsTests(_Base):
    """Rotating a leaked operator token is the documented response to the
    leak; every console session the old value opened must end with it,
    not live on for the rest of its four hours."""

    def test_rotating_the_token_ends_the_sessions_it_opened(self):
        import os
        self._token_login()
        self.assertEqual(200, self.client.get("/admin/console").status_code)
        self.assertNotIn("Operator token",
                         self.client.get("/admin/console").text)
        os.environ["OIKONOME_ADMIN_TOKEN"] = "a-rotated-operator-token-value"
        page = self.client.get("/admin/console").text
        self.assertIn("Operator token", page,
                      "a session opened by the old token still signs in")
        self.assertEqual([], _admin("SELECT 1 FROM admin_sessions "
                                    "WHERE credential_id IS NULL"))

    def test_the_same_token_keeps_its_session(self):
        self._token_login()
        for _ in range(2):
            self.assertNotIn("Operator token",
                             self.client.get("/admin/console").text)

    def test_the_cookie_carries_nothing_computed_from_the_token(self):
        """A copied cookie must not be a way to test guesses at the operator
        token offline: with the same random draw, two different tokens
        must produce the same cookie, so the cookie is independent of the
        token, and the binding is checked on the server's side."""
        import os
        from unittest import mock
        from oikonome.web import adminconsole
        cookies = []
        for tok in ("first-operator-token-value", "other-operator-token"):
            os.environ["OIKONOME_ADMIN_TOKEN"] = tok
            self.client.cookies.clear()
            with mock.patch.object(adminconsole.secrets, "token_urlsafe",
                                   return_value="fixed-random-part"):
                r = self.client.post("/admin/console/login",
                                     data={"token": tok},
                                     follow_redirects=False)
            self.assertEqual(303, r.status_code, r.text)
            cookies.append(r.cookies.get(adminconsole.COOKIE))
            _admin("DELETE FROM admin_sessions RETURNING 1")
        self.assertEqual(cookies[0], cookies[1],
                         "the console cookie changes with the operator "
                         "token, so it carries a verifier for it")

    def test_a_token_session_with_no_server_binding_is_ended(self):
        self._token_login()
        _admin("UPDATE admin_sessions SET token_fp = NULL RETURNING 1")
        self.assertIn("Operator token",
                      self.client.get("/admin/console").text)

    def test_a_passkey_session_survives_a_token_rotation(self):
        import os
        self._token_login()
        self._enrol()
        self._passkey_login()
        os.environ["OIKONOME_ADMIN_TOKEN"] = "a-rotated-operator-token-value"
        self.assertTrue(_admin("SELECT 1 FROM admin_sessions "
                               "WHERE credential_id IS NOT NULL"))
