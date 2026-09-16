"""Cloudflare Turnstile on the public auth doors.

The feature is OFF unless both keys are configured, so these tests set and
unset the env around each case. What they pin:

**Off by default.** No keys → no widget in the HTML, and every door behaves
exactly as before. This is the self-host contract: no Cloudflare account,
no third-party script, no behaviour change.
**Sign-in is RISK-GATED, signup is not.** A clean IP signing into a clean
account is never asked for a token; the challenge arms once failures pile
up on that IP or that account. This is what stops a blocked Cloudflare
script from locking an innocent user out, so the tests below pin both
halves — the free first attempt AND the challenge that follows failures.
**Fail open on silence, closed on a verdict.** A siteverify that times out
ALLOWS the request — `/login` is the door to someone's money and a
Cloudflare outage must not lock everyone out. A token Cloudflare actively
rejects is refused.
**Both login steps are checked.** Two-step sign-in posts to /api/login
twice and both carry the password, so verifying only the code-less step
would let a brute force skip the challenge by always sending a junk
totp_code. The regression here is subtle and worth a test.
**The check runs before the expensive work** on signup (password rules,
invite lookup, spam-defense network call).
"""

import os
import unittest

from fastapi.testclient import TestClient

SITE = "1x00000000000000000000AA"          # Cloudflare's always-passes test key
SECRET = "1x0000000000000000000000000000000AA"


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        from .util import _ensure_db
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.appmod.app)

    def tearDown(self):
        os.environ.pop("OIKONOME_TURNSTILE_SITE_KEY", None)
        os.environ.pop("OIKONOME_TURNSTILE_SECRET", None)

    def _on(self):
        os.environ["OIKONOME_TURNSTILE_SITE_KEY"] = SITE
        os.environ["OIKONOME_TURNSTILE_SECRET"] = SECRET

    def _at_risk(self, email="", n=5):
        """Put this client past the risk threshold, the way real failed
        sign-ins would. TestClient's peer is the literal 'testclient'."""
        from oikonome.web import security
        for _ in range(n):
            security.record_challenge_risk("testclient", email)


class TestVerify(_Base):
    """The module's own contract, with no network."""

    def test_unconfigured_allows_everything(self):
        from oikonome.web import turnstile
        self.assertFalse(turnstile.enabled())
        self.assertTrue(turnstile.verify(""))
        self.assertTrue(turnstile.verify("anything"))

    def test_half_configured_is_off(self):
        # a site key with no secret cannot verify anything; treating that as
        # "on" would hard-fail every login on a partial rollout
        from oikonome.web import turnstile
        os.environ["OIKONOME_TURNSTILE_SITE_KEY"] = SITE
        self.assertFalse(turnstile.enabled())
        self.assertTrue(turnstile.verify(""))

    def test_empty_env_reads_as_unset(self):
        # compose materializes every declared key, so an unconfigured widget
        # arrives as "" — it must not read as "on"
        from oikonome.web import turnstile
        os.environ["OIKONOME_TURNSTILE_SITE_KEY"] = ""
        os.environ["OIKONOME_TURNSTILE_SECRET"] = ""
        self.assertFalse(turnstile.enabled())

    def test_missing_token_is_refused_when_on(self):
        from oikonome.web import turnstile
        self._on()
        self.assertFalse(turnstile.verify(""))

    def test_rejected_token_is_refused(self):
        from oikonome.web import turnstile
        self._on()
        calls = []

        class _R:
            @staticmethod
            def json():
                return {"success": False, "error-codes": ["invalid-input-response"]}

        def _post(url, data=None, timeout=None):
            calls.append((url, data))
            return _R()

        orig = turnstile.httpx.post
        turnstile.httpx.post = _post
        try:
            self.assertFalse(turnstile.verify("forged", ip="203.0.113.9"))
        finally:
            turnstile.httpx.post = orig
        self.assertEqual(calls[0][0], turnstile.VERIFY_URL)
        self.assertEqual(calls[0][1]["response"], "forged")
        self.assertEqual(calls[0][1]["remoteip"], "203.0.113.9")

    def test_accepted_token_passes(self):
        from oikonome.web import turnstile
        self._on()

        class _R:
            @staticmethod
            def json():
                return {"success": True}

        orig = turnstile.httpx.post
        turnstile.httpx.post = lambda *a, **k: _R()
        try:
            self.assertTrue(turnstile.verify("good"))
        finally:
            turnstile.httpx.post = orig

    def test_outage_fails_open(self):
        """A Cloudflare outage must not lock every user out of their money."""
        from oikonome.web import turnstile
        self._on()

        def _boom(*a, **k):
            raise RuntimeError("connection reset")

        orig = turnstile.httpx.post
        turnstile.httpx.post = _boom
        try:
            self.assertTrue(turnstile.verify("whatever"))
        finally:
            turnstile.httpx.post = orig


class TestRendering(_Base):
    def test_login_has_no_widget_when_off(self):
        html = self.client.get("/login").text
        self.assertNotIn("cf-turnstile", html)
        self.assertNotIn("challenges.cloudflare.com", html)

    def test_login_renders_widget_when_on(self):
        self._on()
        html = self.client.get("/login").text
        self.assertIn("cf-turnstile", html)
        self.assertIn(SITE, html)
        self.assertIn("challenges.cloudflare.com/turnstile/v0/api.js", html)

    def test_widget_is_inside_the_login_form(self):
        """The no-JS door depends on it: the widget's hidden field only
        posts if it sits inside <form>."""
        self._on()
        html = self.client.get("/login").text
        form = html.split('id="login-form"', 1)[1].split("</form>", 1)[0]
        self.assertIn("cf-turnstile", form)

    def test_widget_callback_matches_the_handler_pages_js_defines(self):
        """An escalated challenge is resumed by data-callback, not by a
        timer. If the widget names a function pages.js does not define,
        Turnstile calls nothing and a parked sign-in waits forever — so the
        two halves are pinned to each other here."""
        import pathlib

        import oikonome
        self._on()
        html = self.client.get("/login").text
        self.assertIn('data-callback="oikoTurnstileToken"', html)
        js = (pathlib.Path(oikonome.__file__).parent
              / "web" / "static" / "pages.js").read_text()
        self.assertIn("window.oikoTurnstileToken =", js)

    def test_signup_renders_widget_when_on(self):
        self._on()
        html = self.client.get("/signup").text
        self.assertIn("cf-turnstile", html)
        self.assertIn(SITE, html)

    def test_signup_has_no_widget_when_off(self):
        self.assertNotIn("cf-turnstile", self.client.get("/signup").text)


class TestCSP(_Base):
    """The app's own CSP is `script-src 'self'` with `default-src 'self'`,
    which blocks BOTH the Turnstile script and the iframe it renders in unless
    the policy names them. The widget then never produces a token and every
    login 403s — while curl sees a perfectly correct page and a perfectly
    correct 403, because only a browser enforces CSP.

    The last test here is the one that matters: it couples the rendered widget
    to the policy, so the two cannot drift apart.
    """

    def test_strict_policy_when_off(self):
        csp = self.client.get("/login").headers["content-security-policy"]
        self.assertNotIn("cloudflare", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("frame-src", csp)

    def test_policy_opens_exactly_three_directives_when_on(self):
        self._on()
        csp = self.client.get("/login").headers["content-security-policy"]
        host = "https://challenges.cloudflare.com"
        self.assertIn(f"script-src 'self' {host}", csp)   # loads api.js
        self.assertIn(f"frame-src {host}", csp)           # renders the challenge
        self.assertIn(f"connect-src 'self' {host}", csp)  # posts the result
        # and nothing else loosened
        self.assertIn("object-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("base-uri 'self'", csp)
        self.assertIn("form-action 'self'", csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertNotIn("http://", csp)

    def test_rendered_widget_host_is_always_allowed_by_csp(self):
        """THE regression guard. If a page renders a script from some host,
        the CSP it is served with must permit that host — otherwise the
        feature is silently dead in every real browser."""
        self._on()
        for path in ("/login", "/signup"):
            r = self.client.get(path)
            csp = r.headers["content-security-policy"]
            if "challenges.cloudflare.com/turnstile" not in r.text:
                continue                      # widget not on this page
            for directive in ("script-src", "frame-src", "connect-src"):
                seg = [d.strip() for d in csp.split(";")
                       if d.strip().startswith(directive)]
                self.assertTrue(
                    seg and "https://challenges.cloudflare.com" in seg[0],
                    f"{path}: {directive} does not permit the widget host "
                    f"it renders — the challenge will be blocked. CSP={csp}")


class TestDoors(_Base):
    """Every public auth door refuses a tokenless post while the feature is
    on — the sign-in doors once the attempt is risky, signup always — and all
    of them are untouched while it is off."""

    def _deny(self):
        from oikonome.web import turnstile
        self._on()
        orig = turnstile.httpx.post
        turnstile.httpx.post = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("no token should never reach siteverify"))
        return orig

    def test_api_login_refuses_without_token_once_at_risk(self):
        from oikonome.web import turnstile
        orig = self._deny()
        self._at_risk("a@b.co")
        try:
            r = self.client.post("/api/login",
                                 data={"email": "a@b.co", "password": "x"})
        finally:
            turnstile.httpx.post = orig
        self.assertEqual(r.status_code, 403)
        self.assertIn("human_check_failed", r.text)

    def test_form_login_refuses_without_token_once_at_risk(self):
        from oikonome.web import turnstile
        orig = self._deny()
        self._at_risk("a@b.co")
        try:
            r = self.client.post("/login",
                                 data={"email": "a@b.co", "password": "x"},
                                 follow_redirects=False)
        finally:
            turnstile.httpx.post = orig
        self.assertEqual(r.status_code, 403)
        # re-renders the form (with a fresh widget) rather than a bare error
        self.assertIn("cf-turnstile", r.text)

    def test_api_signup_refuses_without_token(self):
        from oikonome.web import turnstile
        orig = self._deny()
        try:
            r = self.client.post("/api/signup",
                                 data={"email": "a@b.co",
                                       "password": "longenough123"})
        finally:
            turnstile.httpx.post = orig
        self.assertEqual(r.status_code, 403)

    def test_signup_check_precedes_password_rules(self):
        """A too-short password with no token must fail the HUMAN check, not
        leak that the password rules were even consulted."""
        from oikonome.web import turnstile
        orig = self._deny()
        try:
            r = self.client.post("/api/signup",
                                 data={"email": "a@b.co", "password": "x"})
        finally:
            turnstile.httpx.post = orig
        self.assertEqual(r.status_code, 403)

    def test_second_login_step_is_also_checked(self):
        """Both steps carry the password, so a brute force must not be able
        to skip the challenge by always sending a totp_code."""
        from oikonome.web import turnstile
        orig = self._deny()
        self._at_risk("a@b.co")
        try:
            r = self.client.post("/api/login",
                                 data={"email": "a@b.co", "password": "x",
                                       "totp_code": "123456"})
        finally:
            turnstile.httpx.post = orig
        self.assertEqual(r.status_code, 403)

    def test_doors_untouched_when_feature_off(self):
        """Self-host contract: no keys, no change. Bad credentials still get
        the normal 401, not a human-check 403."""
        r = self.client.post("/api/login",
                             data={"email": "nobody@example.com",
                                   "password": "wrong-password"})
        self.assertEqual(r.status_code, 401)


class TestRiskGate(_Base):
    """The gate itself: who gets asked, who doesn't, and when that flips.

    The reason it exists at all is the user whose browser blocks the
    Cloudflare script: they see no widget, so an unconditional check is a
    lockout with nothing to click. Gating on failures means that dead end
    can only be reached by someone who is already getting their password
    wrong.
    """

    def _no_network(self):
        """siteverify must not even be consulted for an unchallenged attempt."""
        from oikonome.web import turnstile
        orig = turnstile.httpx.post
        turnstile.httpx.post = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("an unchallenged sign-in must not call siteverify"))
        return orig

    def test_clean_attempt_is_not_challenged(self):
        """THE point of the feature. A tokenless first attempt gets the normal
        wrong-password 401 — not a 403 demanding a check it may be unable to
        produce."""
        from oikonome.web import turnstile
        self._on()
        orig = self._no_network()
        try:
            r = self.client.post("/api/login",
                                 data={"email": "nobody@example.com",
                                       "password": "wrong-password"})
        finally:
            turnstile.httpx.post = orig
        self.assertEqual(r.status_code, 401)

    def test_failures_from_one_ip_arm_the_challenge(self):
        """Each failed attempt is recorded by the door itself — no test
        scaffolding — so this also pins that the doors DO record."""
        from oikonome.web import security, turnstile
        self._on()
        orig = turnstile.httpx.post
        turnstile.httpx.post = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("no token should never reach siteverify"))
        try:
            seen = []
            for i in range(security._RISK_IP_MAX + 1):
                seen.append(self.client.post(
                    "/api/login",
                    data={"email": f"who-{i}@example.com",
                          "password": "wrong-password"}).status_code)
        finally:
            turnstile.httpx.post = orig
        # different email each time, so only the IP bucket can be doing this
        self.assertEqual(seen[:security._RISK_IP_MAX],
                         [401] * security._RISK_IP_MAX)
        self.assertEqual(seen[-1], 403)

    def test_account_bucket_is_ip_independent(self):
        """A botnet gives every attempt a fresh IP; the per-account count is
        what still notices one victim being ground on."""
        from oikonome.web import security
        for i in range(security._RISK_ACCT_MAX):
            security.record_challenge_risk(f"203.0.113.{i}", "victim@example.com")
        self.assertTrue(security.challenge_warranted("198.51.100.7",
                                                     "victim@example.com"))
        # and it is scoped to that account, not global
        self.assertFalse(security.challenge_warranted("198.51.100.7",
                                                      "bystander@example.com"))

    def test_a_burst_across_the_instance_challenges_everyone(self):
        """The gap the first two buckets leave: a botnet hitting one account
        each fills neither, and that IS the credential-stuffing case Turnstile
        exists for."""
        from oikonome.web import security
        for i in range(security._RISK_SITE_MAX):
            security.record_challenge_risk(f"203.0.113.{i}", f"v{i}@example.com")
        self.assertTrue(security.challenge_warranted("198.51.100.7",
                                                     "innocent@example.com"))

    def test_the_site_bucket_stays_well_clear_of_the_targeted_ones(self):
        """The site-wide bucket is the only one that challenges people who did
        nothing wrong, so it must not fire on ordinary bad days. Every other
        test here reads the constant dynamically and would pass at ANY value —
        this is the one that would notice it being dropped.

        The bar: at least ten separate users must be able to exhaust their own
        account allowance before the whole instance starts paying. Below that
        the site gate front-runs the targeted gates it exists to back up.
        """
        from oikonome.web import security
        self.assertGreater(security._RISK_SITE_MAX, security._RISK_ACCT_MAX)
        self.assertGreater(security._RISK_SITE_MAX, security._RISK_IP_MAX)
        self.assertGreaterEqual(security._RISK_SITE_MAX,
                                security._RISK_ACCT_MAX * 10)

    def test_one_good_login_does_not_disarm_the_instance(self):
        from oikonome.web import security
        for i in range(security._RISK_SITE_MAX):
            security.record_challenge_risk(f"203.0.113.{i}", f"v{i}@example.com")
        security.clear_challenge_risk("198.51.100.7", "innocent@example.com")
        self.assertTrue(security.challenge_warranted("198.51.100.8",
                                                     "someone@example.com"))

    def test_success_does_not_launder_the_per_ip_bucket(self):
        """Clearing the IP bucket on success lets anyone
        with ONE valid credential erase the per-IP gate forever — two guesses
        at a victim, one self-login, repeat, and the counter never climbs."""
        from oikonome.web import security
        for _ in range(security._RISK_IP_MAX):
            security.record_challenge_risk("203.0.113.9", "victim@example.com")
        self.assertTrue(security.challenge_warranted("203.0.113.9"))
        # attacker now signs into their OWN account from the same address
        security.clear_challenge_risk("203.0.113.9", "attacker@example.com")
        self.assertTrue(
            security.challenge_warranted("203.0.113.9"),
            "a successful sign-in wiped the per-IP risk bucket — the gate can "
            "be laundered indefinitely by anyone holding one valid login")

    def test_a_successful_sign_in_clears_the_account_but_not_the_ip(self):
        """Success settles the ACCOUNT. The IP keeps its own record — see
        test_success_does_not_launder_the_per_ip_bucket for why."""
        from oikonome.web import security
        self._at_risk("them@example.com")
        self.assertTrue(security.challenge_warranted("testclient",
                                                     "them@example.com"))
        security.clear_challenge_risk("testclient", "them@example.com")
        # the account is clear...
        self.assertFalse(security.challenge_warranted("", "them@example.com"))
        # ...the address that produced the failures is not
        self.assertTrue(security.challenge_warranted("testclient"))

    def test_gate_is_dead_while_the_feature_is_off(self):
        """A self-host accrues risk marks like anyone else; with no keys
        configured that must still mean no challenge, ever."""
        from oikonome.web import turnstile
        self._at_risk("a@b.co", n=50)
        self.assertFalse(turnstile.required_for_login("testclient", "a@b.co"))
        self.assertTrue(turnstile.verify_login("", "testclient", "a@b.co"))

    def test_forgot_is_risk_gated_and_can_always_answer(self):
        """A per-IP hourly limit alone lets a botnet drive unlimited reset
        mail, so /forgot is risk-gated too — but it is also the recovery door,
        so the page it re-renders MUST carry a widget."""
        self._on()
        # clean client: never challenged (this is the door you reach for when
        # you are already locked out; a hard gate here would be the worst
        # possible place for the blocked-script dead end)
        r = self.client.post("/forgot", data={"email": "a@b.co"},
                             follow_redirects=False)
        self.assertNotEqual(r.status_code, 403)
        self._at_risk()                       # IP bucket only
        r = self.client.post("/forgot", data={"email": "a@b.co"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        self.assertIn("cf-turnstile", r.text,
                      "challenged /forgot re-render has no widget — the user "
                      "could never produce the token it just demanded")
        self.assertIn('data-required="1"', r.text)

    def test_forgot_account_bucket_cannot_gate_someone_elses_reset(self):
        """Failures against an ACCOUNT must never arm /forgot: that would let
        anyone who knows your address put a checkbox in front of your password
        reset, which is exactly the lockout this feature exists to remove."""
        from oikonome.web import security, turnstile
        self._on()
        # enough to arm the ACCOUNT bucket, deliberately under the site-wide
        # threshold — otherwise the instance burst gate fires and proves nothing
        n = security._RISK_ACCT_MAX + 1
        self.assertLess(n, security._RISK_SITE_MAX)
        self._at_risk("victim@example.com", n=n)
        self.assertTrue(security.challenge_warranted("", "victim@example.com"),
                        "precondition: the account bucket should be armed")
        fresh_ip = "198.51.100.44"             # an address with no history
        self.assertFalse(turnstile.required_for_login(fresh_ip),
                         "account-scoped risk leaked into the IP-only gate "
                         "/forgot uses — someone who knows your address could "
                         "put a checkbox in front of your password reset")

    def test_signup_is_challenged_from_the_first_request(self):
        """Signup has no history to gate on — every request is a stranger and
        a bot getting through costs a whole tenant."""
        from oikonome.web import turnstile
        self._on()
        r = self.client.post("/api/signup",
                             data={"email": "new@example.com",
                                   "password": "longenough123"})
        self.assertEqual(r.status_code, 403)
        self.assertTrue(turnstile.enabled())

    def test_login_page_tells_the_client_whether_to_wait(self):
        """pages.js only pauses for a token when the server will enforce one;
        otherwise a blocked script would add 6s to every honest sign-in."""
        self._on()
        self.assertIn('data-required="0"', self.client.get("/login").text)
        self._at_risk()
        self.assertIn('data-required="1"', self.client.get("/login").text)

    def test_every_login_render_can_produce_a_token(self):
        """The no-JS door re-renders the whole page on each failure, and the
        user's next submit carries only what that render put in the form. A
        render that dropped the widget would arm the challenge and remove the
        means of answering it in one move — so every exit from this handler
        has to carry it."""
        from oikonome.web import security
        self._on()
        cases = {}
        self._at_risk("a@b.co")
        cases["human check"] = self.client.post(
            "/login", data={"email": "a@b.co", "password": "x"},
            follow_redirects=False)
        # a genuine reset of BOTH buckets — clear_challenge_risk deliberately
        # does not touch the per-IP one (see the laundering test), so a test
        # that wants an un-gated client has to drop it directly
        security._limiter.clear(("risk-ip", "testclient"))
        security.clear_challenge_risk("testclient", "a@b.co")
        cases["wrong password"] = self.client.post(
            "/login", data={"email": "a@b.co", "password": "x"},
            follow_redirects=False)
        security._limiter.clear(("risk-ip", "testclient"))
        for _ in range(security._ACCT_FAIL_MAX):
            security.record_login_failure("a@b.co")
        cases["account locked"] = self.client.post(
            "/login", data={"email": "a@b.co", "password": "x"},
            follow_redirects=False)
        for name, r in cases.items():
            self.assertIn("cf-turnstile", r.text,
                          f"the {name} render has no widget — a challenged "
                          f"retry from this page could never carry a token")
        self.assertEqual(cases["account locked"].status_code, 429)

    def test_rejected_login_re_renders_with_a_usable_widget(self):
        """A wrong password on the no-JS door re-renders the form. If that
        render omitted the widget, the user's next submit could never carry a
        token — the challenge would be armed and impossible in the same
        breath."""
        self._on()
        self._at_risk("a@b.co")
        r = self.client.post("/login",
                             data={"email": "a@b.co", "password": "x"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        self.assertIn("cf-turnstile", r.text)
        self.assertIn('data-required="1"', r.text)


if __name__ == "__main__":
    unittest.main()


class HalfConfiguredTests(unittest.TestCase):
    """A widget must never render where the CSP will block it.

    Were the pages gated on `site_key()` while the CSP opened on
    `enabled()`, setting only the site key — an ordinary halfway point while
    wiring Turnstile up — would have every sign-in page render a challenge
    whose script and iframe the browser then refuses.
    """

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("OIKONOME_TURNSTILE_SITE_KEY",
                        "OIKONOME_TURNSTILE_SECRET")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_site_key_alone_renders_nothing(self):
        from oikonome.web import turnstile
        os.environ["OIKONOME_TURNSTILE_SITE_KEY"] = "0x-site"
        os.environ.pop("OIKONOME_TURNSTILE_SECRET", None)
        self.assertEqual(turnstile.site_key(), "0x-site")
        self.assertFalse(turnstile.enabled())
        self.assertEqual(turnstile.render_key(), "",
                         "a widget with no secret verifies nothing")

    def test_both_halves_render_the_key(self):
        from oikonome.web import turnstile
        os.environ["OIKONOME_TURNSTILE_SITE_KEY"] = "0x-site"
        os.environ["OIKONOME_TURNSTILE_SECRET"] = "0x-secret"
        self.assertTrue(turnstile.enabled())
        self.assertEqual(turnstile.render_key(), "0x-site")

    def test_the_pages_use_the_gated_key(self):
        import pathlib as _p

        import oikonome
        src = (_p.Path(oikonome.__file__).parent / "web"
               / "app.py").read_text()
        self.assertNotIn("turnstile.site_key()", src)
        self.assertIn("turnstile.render_key()", src)
