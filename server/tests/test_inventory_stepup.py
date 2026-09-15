"""Inventory: credential-changing routes require step-up auth.

A live session is not proof of the human. Step-up auth shipped
because a stolen cookie could enroll its OWN second factor — and enrolling
revokes every other session, so session theft escalated into locking the
real owner out of their own money using the 2FA machinery as the weapon.
The password (or a passkey step-up) is the thing a session thief does not
have, so every route that mutates credentials, factors, identity, or
delegated access re-asks for it.

The problem this file solves is not "is step-up implemented" — it is — but
that step-up is applied route by route, and the next unguarded one is a
single `def` away at all times, and reading every mutating route by eye
is probabilistic; this is not.

Two assertions, doing different jobs:

* STEPUP_REQUIRED is a PIN. Every route in it calls a step-up helper
today. If a refactor drops the call, the test fails — a guard cannot be
removed silently, which is the regression that actually happens.
* SENSITIVE matches by PATH. Any new mutating route that looks like it
touches credentials must either step up or be written into
STEPUP_EXEMPT with a reason. New routes get triaged when they land, by
the person who has the context, instead of at review time months later.

Deliberately source-based rather than behavioural: FastAPI validates the
body before the handler runs, so a payload-free probe of these routes gets
422 and proves nothing about the guard behind it. Per-route behaviour is
covered by test_stepup_enroll_and_reset.py and test_passkey_stepup.py;
this file covers the SET.
"""

import re
import unittest

from oikonome.web.app import app

from .util import handler_source, mutating_routes, route_key

# a call to any of these counts as stepping up. _require_elevation is the
# per-session window: the same proofs, collected once and stamped on the
# session row, read by the door instead of asked again.
STEPUP_CALLS = ("_step_up(", "_require_passkey_stepup(",
                "_require_elevation(")

# PINNED: these step up today and must continue to.
STEPUP_REQUIRED = {
    "DELETE /api/passkeys/{pk_id}",
    "DELETE /api/users/{user_id}",
    "POST /api/users/{user_id}/role",
    "POST /api/account/delete",
    "POST /api/email/change",
    "POST /api/invites",
    "POST /api/passkeys",
    "POST /api/password/change",
    "POST /api/sessions/revoke",
    "POST /api/support-access",
    "POST /api/tokens",
    "POST /api/tokens/revoke",
    "POST /api/totp/confirm",
    "POST /api/totp/disable",
    "POST /api/totp/enroll",
    "POST /api/totp/recovery-regenerate",
}

# Path shapes that smell like credential/identity/delegation mutations.
# Broad on purpose — a false positive costs one line in STEPUP_EXEMPT, a
# false negative ships an unguarded route.
SENSITIVE = re.compile(
    r"passkey|totp|password|recovery|/email/change|/account/delete"
    r"|/users/|invite|/sessions|/tokens|support-access|stepup|elevat"
    # irreversible destruction of household data — a delete-forever-shaped
    # route must be triaged for re-auth / owner-gating, not shipped on a
    # bare edit check.
    r"|delete-forever|delete_forever",
    re.IGNORECASE)

# Matches SENSITIVE but correctly does NOT step up. Each line is a claim
# someone made on purpose; if it stops being true the pin above catches it.
STEPUP_EXEMPT = {
    # De-escalations. Step-up protects against an attacker GAINING access;
    # requiring it to give access up would strand a user who suspects
    # compromise behind the very factor they may have lost.
    "DELETE /api/invites/{token_hash}":
        "revokes an unclaimed invite — removes access, cannot grant it",
    "POST /api/support-access/revoke":
        "ends an operator's access early — strictly a de-escalation",

    # Pre-auth: there is no session to step up FROM. These carry their own
    # protection (rate limits, single-use tokens, WebAuthn challenges).
    "POST /api/invite/claim":
        "pre-auth: the claimer has no account yet; the token IS the proof",
    "POST /api/login/passkey":
        "pre-auth: this IS an authentication, not a change to one",
    # answered by the INVITED person, who in the self-host case has
    # no account here at all — there is no session to step up from, and
    # demanding one would make the invitation unanswerable. The single-use
    # token is the proof, the route is rate-limited, and the only state it
    # writes is that person's own yes/no about their own mailbox.
    "POST /recipient-invite":
        "pre-auth: the recipient may have no account; the token IS the proof",
    "POST /api/login/passkey/options":
        "pre-auth: hands out a WebAuthn challenge, mutates no state",

    # Challenge issuers. They mint a challenge; the verifying route that
    # follows is where the guard belongs (and it is pinned above).
    "POST /api/passkeys/options":
        "issues a registration challenge; POST /api/passkeys does the work",
    "POST /api/stepup/passkey":
        "IS the step-up — guarding it with itself is circular",
    "POST /api/stepup/passkey/options":
        "issues the step-up challenge itself",
    "POST /api/auth/elevate":
        "IS the elevation — it takes the same proofs the doors took and "
        "stamps the caller's own session; guarding it with itself is "
        "circular",

    # Owner-gated + typed-name confirm, not a credential/identity mutation.
    # Permanently destroying a business entity's records is reserved for the
    # owner (_owner_only) and additionally demands the entity's exact typed
    # name; step-up (tenant password/TOTP) is not the control this needs.
    "POST /api/business/entities/{entity_id}/delete-forever":
        "owner-only + typed-name confirm; destroys data, mutates no credential",

    # Separate auth domain: operator console, not a tenant user session.
    "POST /admin/console/invite":
        "admin console has its own token auth, not user sessions",

    # the console's operator passkeys. Same separate auth domain —
    # these run on admin_connect() against admin_credentials and have their
    # own step-up primitive (_stepup_ok / admin_passkeys.redeem_ticket),
    # which _step_up (tenant password/TOTP/recovery) cannot express.
    "POST /admin/console/passkey/login/options":
        "pre-auth: hands out a WebAuthn challenge, mutates no session",
    "POST /admin/console/passkey/login":
        "pre-auth: this IS the operator authentication, not a change to one",
    "POST /admin/console/passkey/options":
        "issues an enrolment challenge; POST /admin/console/passkey persists",
    "POST /admin/console/passkey":
        "bootstrap: the FIRST operator key must be enrollable from a token "
        "session or the feature can never start; audited, and once a key "
        "exists OIKONOME_ADMIN_REQUIRE_PASSKEY makes that session a passkey "
        "session anyway",
    "POST /admin/console/passkey/delete":
        "steps up with the console's own primitive (_stepup_ok) and refuses "
        "to remove the last key under the require-flag",
    "POST /admin/console/passkey/stepup":
        "IS the console step-up — guarding it with itself is circular",
    "POST /admin/console/passkey/stepup/options":
        "issues the console step-up challenge itself",
}


class StepUpInventoryTests(unittest.TestCase):

    def setUp(self):
        self.routes = {route_key(r): r for r in mutating_routes(app)}

    def _steps_up(self, key: str) -> bool:
        src = handler_source(self.routes[key])
        return any(call in src for call in STEPUP_CALLS)

    def test_pinned_routes_still_step_up(self):
        """A guard must not be refactored away in silence."""
        missing = sorted(k for k in STEPUP_REQUIRED
                         if k in self.routes and not self._steps_up(k))
        self.assertEqual(
            [], missing,
            f"route(s) lost their step-up guard: {missing}. A live session "
            "is not proof of the human. Restore the "
            "_step_up() call, or if the route genuinely no longer mutates "
            "credentials, move it to STEPUP_EXEMPT with a reason.")

    def test_pin_has_not_gone_stale(self):
        """A pin naming a deleted route silently protects nothing."""
        gone = sorted(k for k in STEPUP_REQUIRED if k not in self.routes)
        self.assertEqual(
            [], gone,
            f"STEPUP_REQUIRED names route(s) that no longer exist: {gone}")

    def test_every_sensitive_route_is_guarded_or_triaged(self):
        """The one that catches the next route to land."""
        untriaged = sorted(
            key for key, route in self.routes.items()
            if SENSITIVE.search(route.path)
            and key not in STEPUP_REQUIRED
            and key not in STEPUP_EXEMPT)
        self.assertEqual(
            [], untriaged,
            f"new credential-shaped route(s) with no step-up decision: "
            f"{untriaged}. Either call _step_up() and add the route to "
            "STEPUP_REQUIRED, or add it to STEPUP_EXEMPT with the reason it "
            "is safe without one.")

    def test_exemptions_are_all_real_routes(self):
        gone = sorted(k for k in STEPUP_EXEMPT if k not in self.routes)
        self.assertEqual(
            [], gone,
            f"STEPUP_EXEMPT names route(s) that no longer exist: {gone}")

    def test_exemptions_carry_a_reason(self):
        blank = sorted(k for k, why in STEPUP_EXEMPT.items() if not why.strip())
        self.assertEqual([], blank, f"exemption(s) with no reason: {blank}")


class ReauthClassificationTests(unittest.TestCase):
    """The SPA decides "lost session" vs "re-auth prompt" by matching the
    server's 401 detail. The two live on opposite sides of the codebase, so
    a wording change on the server silently reclassifies a prompt as a
    logout — which is what happened to "current one-time code required":
    the app blanked to the login screen and ate the message."""

    def test_every_401_reauth_detail_matches_the_spa_pattern(self):
        import pathlib
        import re
        import oikonome
        root = pathlib.Path(oikonome.__file__).parent
        spa = (root.parent.parent / "webapp" / "src" / "main.tsx")
        if not spa.exists():
            self.skipTest("webapp/ not present")
        m = re.search(r"const reauth = /([^/]+)/i", spa.read_text())
        self.assertIsNotNone(m, "the re-auth pattern moved — update this test")
        pattern = re.compile(m.group(1), re.I)

        # every 401 the server raises for a STEP-UP (not a lost session)
        details = []
        for path in root.rglob("*.py"):
            src = path.read_text()
            for hit in re.finditer(
                    r'HTTPException\(\s*401\s*,\s*"([^"]+)"', src):
                details.append((path.name, hit.group(1)))
        stepup = [(f, d) for f, d in details
                  if re.search(r"one-time|password|passkey|recovery|totp",
                               d, re.I)]
        self.assertTrue(stepup, "no step-up 401s found — test is not looking")
        missed = [f"{f}: {d}" for f, d in stepup if not pattern.search(d)]
        self.assertEqual(
            [], missed,
            "these 401s are re-auth prompts the SPA would treat as a lost "
            "session, blanking the app: " + "; ".join(missed))
