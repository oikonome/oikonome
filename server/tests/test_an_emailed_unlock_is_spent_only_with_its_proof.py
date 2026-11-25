"""An emailed unlock is spent only by whoever proved the mailbox.

The unlock answers the human check for one account. Spendable by anyone
who named that account's address, it was a bearer on the address: a
stranger polling the sign-in door with junk passwords spent every unlock
the owner confirmed. Handing a proof to whoever ASKED for the link did not
help — anyone may ask for any address, and asking used to burn the owner's
own link. So the proof exists only at the confirm step: a cookie in the
confirming browser, and a short sign-in code on the page for the app.

There is no way through without the proof: a request carrying none spends
nothing, even from the address that confirmed the link — anyone behind the
same public address (a household, an office, a carrier NAT) would share it.
"""

import os
import re
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.auth import login_unlock, passwords

from .util import _ensure_db

PW = "correct-horse-battery"
OWNER_IP = "192.0.2.44"
STRANGER_IP = "192.0.2.45"
# the owner reading mail on one connection and signing in on another
MAIL_IP = "192.0.2.46"
APP_IP = "192.0.2.47"
OTHER_MAIL_IP = "192.0.2.48"
CODE_RE = re.compile(r"<b>([A-Z2-7]{5}-[A-Z2-7]{5})</b>")


class UnlockProofTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        from oikonome.web import security, turnstile
        security._limiter._hits.clear()
        for p in (mock.patch.object(security, "_redis_limiter", False),
                  mock.patch.dict(os.environ, {
                      "OIKONOME_TURNSTILE_SITE_KEY": "site",
                      "OIKONOME_TURNSTILE_SECRET": "secret"}),
                  # the check is armed, as after a burst of wrong passwords
                  mock.patch.object(turnstile, "required_for_login",
                                    return_value=True),
                  # nobody here can produce a Turnstile token
                  mock.patch.object(turnstile, "verify", return_value=False),
                  mock.patch.object(self.appmod, "_deliver_unlock"),
                  mock.patch.object(self.appmod, "_deliver_reset")):
            p.start()
            self.addCleanup(p.stop)
        turnstile.forget_spent_unlocks()
        self.uid, self.email = self._user()

    def _user(self):
        from oikonome.db import tenancy
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, f"prf-{uuid.uuid4().hex[:8]}")
            email = f"prf-{uuid.uuid4().hex[:8]}@example.dev"
            uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, "
                "verified_at) VALUES (%s,%s,%s,now()) RETURNING id",
                (tid, email, passwords.hash_password(PW))).fetchone()["id"]
            admin.commit()
        finally:
            admin.close()
        return uid, email

    def _client(self, ip=OWNER_IP):
        return TestClient(self.appmod.app, raise_server_exceptions=False,
                          client=(ip, 50000))

    def _unlocked(self, email=None):
        from oikonome.db import tenancy
        with tenancy.control_connect() as conn:
            return login_unlock.is_unlocked(conn, email or self.email)

    def _ask(self, client, email=None):
        r = client.post("/unlock", data={"email": email or self.email})
        self.assertEqual(r.status_code, 200, r.text)
        return r

    def _last_link(self):
        return self.appmod._deliver_unlock.call_args.args[1]

    def _confirm(self, token, ip=OWNER_IP):
        """The owner opens the mailed link and confirms it. Returns the
        confirming browser and the sign-in code its page showed."""
        browser = self._client(ip)
        r = browser.post("/unlock/confirm", data={"token": token})
        self.assertEqual(r.status_code, 200, r.text)
        m = CODE_RE.search(r.text)
        self.assertIsNotNone(m, "the confirm page shows no sign-in code")
        return browser, m.group(1)

    def _sign_in(self, client, **extra):
        return client.post("/api/login", data={
            "email": self.email, "password": PW, **extra})

    # ---- the request hands out nothing ---------------------------------

    def test_asking_for_a_link_returns_nothing_usable(self):
        """Known, unknown and capped addresses get the same answer, and
        none of them carries a cookie or a proof."""
        known = self._ask(self._client())
        unknown = self._ask(self._client(),
                            f"nobody-{uuid.uuid4().hex}@x.dev")
        for _ in range(login_unlock.SEND_MAX_PER_HOUR):
            capped = self._ask(self._client(STRANGER_IP))
        for r in (known, unknown, capped):
            self.assertNotIn("set-cookie", r.headers)
            self.assertNotIn("x-unlock-proof", r.headers)
        self.assertEqual(known.text, unknown.text)
        self.assertEqual(known.text, capped.text)

    def test_a_stranger_who_asked_for_the_link_cannot_spend_it(self):
        stranger = self._client(STRANGER_IP)
        asked = self._ask(stranger)
        browser, _ = self._confirm(self._last_link())
        for _ in range(5):
            r = stranger.post("/api/login", data={
                "email": self.email, "password": "a-guess",
                "unlock_proof": asked.headers.get("x-unlock-proof", "")})
            self.assertEqual(r.status_code, 403, r.text)
        self.assertTrue(self._unlocked(), "the stranger who asked for the "
                        "link spent the owner's confirmed unlock")
        self.assertEqual(self._sign_in(browser).status_code, 200)

    def test_a_strangers_request_does_not_burn_the_owners_link(self):
        self._ask(self._client())
        owners = self._last_link()
        self._ask(self._client(STRANGER_IP))
        self.assertNotEqual(self._last_link(), owners)
        browser, _ = self._confirm(owners)
        self.assertEqual(self._sign_in(browser).status_code, 200)

    def test_asking_again_past_the_cap_leaves_the_confirmed_link_usable(self):
        """The slow-mail case: the person asks four times from the app,
        the fourth is over the hourly cap, and the third link is the one
        they open. Nothing the capped request answered may get in the way
        of the code that link's page shows."""
        app = self._client(APP_IP)
        for _ in range(login_unlock.SEND_MAX_PER_HOUR):
            self._ask(app)
        third = self._last_link()
        capped = self._ask(app)
        self.assertNotIn("set-cookie", capped.headers)
        _, code = self._confirm(third, ip=MAIL_IP)
        r = self._sign_in(app, unlock_proof=code)
        self.assertEqual(r.status_code, 200, r.text)

    # ---- the confirming browser ----------------------------------------

    def test_the_browser_that_confirmed_signs_in_once(self):
        self._ask(self._client(STRANGER_IP))
        browser, _ = self._confirm(self._last_link(), ip=MAIL_IP)
        r = browser.post("/login", data={"email": self.email,
                                         "password": PW},
                         follow_redirects=False)
        self.assertIn(r.status_code, (302, 303), r.text)
        self.assertFalse(self._unlocked())
        # replaying the spent proof answers nothing
        self.assertEqual(self._sign_in(browser).status_code, 403)

    def test_a_stranger_polling_with_nothing_cannot_spend_it(self):
        self._ask(self._client())
        browser, _ = self._confirm(self._last_link())
        stranger = self._client(STRANGER_IP)
        for door in ("/api/login", "/login"):
            r = stranger.post(door, data={"email": self.email,
                                          "password": "a-guess"},
                              follow_redirects=False)
            self.assertEqual(r.status_code, 403, r.text)
        self.assertTrue(self._unlocked())
        self.assertEqual(self._sign_in(browser).status_code, 200)

    # ---- the sign-in code ----------------------------------------------

    def test_the_sign_in_code_spends_the_unlock_once(self):
        self._ask(self._client())
        _, code = self._confirm(self._last_link(), ip=MAIL_IP)
        app = self._client(APP_IP)
        # typed the way a person reads it off a screen
        typed = code.lower().replace("-", " ")
        self.assertEqual(self._sign_in(app, unlock_proof=typed).status_code,
                         200)
        self.assertEqual(self._sign_in(app, unlock_proof=code).status_code,
                         403, "a used sign-in code answered a second time")

    def test_a_wrong_code_leaves_the_unlock_standing(self):
        self._ask(self._client())
        _, code = self._confirm(self._last_link(), ip=MAIL_IP)
        app = self._client(APP_IP)
        wrong = ("A" if code[0] != "A" else "B") + code[1:]
        self.assertEqual(self._sign_in(app, unlock_proof=wrong).status_code,
                         403)
        self.assertTrue(self._unlocked())
        self.assertEqual(self._sign_in(app, unlock_proof=code).status_code,
                         200)

    def test_a_code_for_one_account_does_nothing_for_another(self):
        _, other_email = self._user()
        self._ask(self._client(), other_email)
        _, other_code = self._confirm(self._last_link(), ip=MAIL_IP)
        self._ask(self._client())
        self._confirm(self._last_link(), ip=OTHER_MAIL_IP)
        r = self._sign_in(self._client(APP_IP), unlock_proof=other_code)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertTrue(self._unlocked())
        self.assertTrue(self._unlocked(other_email))

    # ---- no proof, no spend — not even from the confirming address -----

    def test_no_proof_from_the_confirming_address_leaves_it(self):
        """The person confirms in the phone's browser, whose cookie the app
        never sees: an app that sends no code is refused even over the same
        connection, and the unlock stays for the code."""
        self._ask(self._client())
        _, code = self._confirm(self._last_link(), ip=OWNER_IP)
        r = self._sign_in(self._client(OWNER_IP))
        self.assertEqual(r.status_code, 403, r.text)
        self.assertTrue(self._unlocked())
        r = self._sign_in(self._client(OWNER_IP), unlock_proof=code)
        self.assertEqual(r.status_code, 200, r.text)

    def test_no_proof_from_another_address_leaves_it(self):
        self._ask(self._client())
        self._confirm(self._last_link(), ip=OWNER_IP)
        self.assertEqual(self._sign_in(self._client(STRANGER_IP))
                         .status_code, 403)
        self.assertTrue(self._unlocked())

    def test_a_wrong_proof_leaves_it(self):
        self._ask(self._client())
        self._confirm(self._last_link(), ip=OWNER_IP)
        r = self._sign_in(self._client(OWNER_IP), unlock_proof="AAAAA-AAAAA")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertTrue(self._unlocked())

    # ---- the confirm step ----------------------------------------------

    def test_a_cross_site_confirm_is_refused(self):
        """A page elsewhere posting someone's own link token to the confirm
        door (to plant its unlock cookie in a visitor's browser) is a
        cross-origin write, and the link stays unspent."""
        from oikonome.db import tenancy
        self._ask(self._client())
        token = self._last_link()
        r = self._client().post("/unlock/confirm", data={"token": token},
                                headers={"Origin": "https://elsewhere.example"})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertNotIn("oikonome_unlock", r.headers.get("set-cookie", ""))
        with tenancy.control_connect() as conn:
            self.assertIsNotNone(login_unlock.peek(conn, token))

    def test_moving_the_login_address_ends_its_unlocks(self):
        """A link mailed to the address being left must not open (showing
        the new address) or answer the check after the move — opened or
        not."""
        from oikonome.db import tenancy
        self._ask(self._client())
        self._confirm(self._last_link())
        self._ask(self._client())
        unopened = self._last_link()
        new_email = f"prf-new-{uuid.uuid4().hex[:8]}@example.dev"
        admin = tenancy.admin_connect()
        try:
            with admin.transaction():
                self.appmod._move_login_email(admin, self.uid, self.email,
                                              new_email)
        finally:
            admin.close()
        self.assertFalse(self._unlocked(new_email))
        r = self._client().get(f"/unlock?token={unopened}")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertNotIn(new_email, r.text)
        r = self._client().post("/unlock/confirm", data={"token": unopened})
        self.assertEqual(r.status_code, 400, r.text)

    # ---- /forgot follows the same rule ---------------------------------

    def test_forgot_spends_the_unlock_only_for_the_mailbox_holder(self):
        stranger = self._client(STRANGER_IP)
        self._ask(stranger)
        browser, _ = self._confirm(self._last_link(), ip=MAIL_IP)
        r = stranger.post("/forgot", data={"email": self.email})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertTrue(self._unlocked(),
                        "a stranger's /forgot spent the owner's unlock")
        r = browser.post("/forgot", data={"email": self.email})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(self._unlocked())

    def test_forgot_with_no_proof_from_the_confirming_address_is_refused(self):
        self._ask(self._client())
        self._confirm(self._last_link(), ip=OWNER_IP)
        r = self._client(OWNER_IP).post("/forgot",
                                        data={"email": self.email})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertTrue(self._unlocked())

    # ---- refunds -------------------------------------------------------

    def test_the_two_step_refund_re_arms_only_the_spenders_unlock(self):
        from oikonome.web import turnstile
        self._ask(self._client())
        browser, _ = self._confirm(self._last_link())
        proof = browser.cookies.get("oikonome_unlock")
        self.assertTrue(turnstile.verify_login("", APP_IP, self.email,
                                               proof))
        self.assertFalse(self._unlocked())
        turnstile.refund_unlock(self.email)
        self.assertTrue(self._unlocked())
        self.assertFalse(turnstile.verify_login("", STRANGER_IP, self.email))
        self.assertTrue(self._unlocked())
        self.assertTrue(turnstile.verify_login("", APP_IP, self.email,
                                               proof))

    def test_a_passkey_accounts_password_step_re_arms_only_for_the_owner(self):
        """The app's passkey-only branch: the password post spends the
        unlock and answers passkey_required; the unlock comes back for the
        recovery-code post — to the code holder, not to a stranger — and a
        wrong code then spends it for good."""
        from oikonome.db import tenancy
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO passkeys (user_id, credential_id, public_key, "
                "sign_count) VALUES (%s, %s, %s, 0)",
                (self.uid, f"cred-{uuid.uuid4().hex}", "x"))
        finally:
            admin.close()
        self._ask(self._client())
        _, code = self._confirm(self._last_link(), ip=MAIL_IP)
        app = self._client(APP_IP)
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            r = self._sign_in(app, unlock_proof=code)
            self.assertEqual(r.status_code, 401, r.text)
            self.assertIn("passkey_required", r.text)
            self.assertTrue(self._unlocked(), "the password step's spend "
                            "was not given back for the code step")
            r = self._sign_in(self._client(STRANGER_IP),
                              totp_code="ABCD-EFGH-IJKL-MNOP-QRST")
            self.assertEqual(r.status_code, 403, r.text)
            self.assertTrue(self._unlocked())
            r = self._sign_in(app, unlock_proof=code,
                              totp_code="ABCD-EFGH-IJKL-MNOP-QRST")
            self.assertEqual(r.status_code, 401, r.text)
            self.assertFalse(self._unlocked(),
                             "a wrong recovery code got its spend back")


if __name__ == "__main__":
    unittest.main()
