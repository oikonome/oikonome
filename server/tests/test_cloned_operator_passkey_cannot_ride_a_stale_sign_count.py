"""An operator passkey's signature counter is checked and advanced as one step.

The counter is WebAuthn's cloned-authenticator alarm: an assertion that does
not beat the stored count came from a copy of the key. The console reads the
count, verifies against it and writes the new one on an autocommit
connection, so two assertions for one credential arriving together would both
be judged against the same stale baseline and both be let in — the clone
beside the operator. The write is therefore conditional on the baseline it
was judged against; whichever assertion lands second is refused.

An authenticator that always reports 0 keeps working however its sign-ins
overlap: it writes 0 over 0.
"""

from unittest import mock

from oikonome.db import tenancy

from .test_admin_passkey import _Base


class _Counter:
    def __init__(self, n):
        self.new_sign_count = n


def _set_count(n):
    a = tenancy.admin_connect()
    try:
        a.execute("UPDATE admin_credentials SET sign_count = %s", (n,))
    finally:
        a.close()


class OperatorSignCountIsCompareAndSet(_Base):
    def _login(self, verifier):
        o = self.client.post("/admin/console/passkey/login/options").json()
        with mock.patch("oikonome.auth.admin_passkeys."
                        "verify_authentication_response", side_effect=verifier):
            return self.client.post("/admin/console/passkey/login", json={
                "challenge_id": o["challenge_id"],
                "credential": self._assertion()})

    def test_an_assertion_judged_against_a_stale_count_is_refused(self):
        self._token_login()
        self._enrol()
        _set_count(5)

        def other_assertion_lands_first(**kw):
            self.assertEqual(kw["credential_current_sign_count"], 5)
            _set_count(9)               # the owner's sign-in, mid-verify
            return _Counter(6)          # signed off the stale reading of 5

        r = self._login(other_assertion_lands_first)
        self.assertEqual(r.status_code, 401, r.text)
        a = tenancy.admin_connect()
        try:
            n = a.execute("SELECT sign_count FROM admin_credentials").fetchone()
        finally:
            a.close()
        self.assertEqual(n["sign_count"], 9)

    def test_a_counterless_authenticator_still_signs_in(self):
        self._token_login()
        self._enrol()
        r = self._login(lambda **kw: _Counter(0))
        self.assertEqual(r.status_code, 200, r.text)
