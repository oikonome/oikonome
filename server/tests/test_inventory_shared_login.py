"""Inventory: every write route has decided what the SHARED LOGIN may do.

The shared demonstration login is a household owner whose password is
handed out and whose second factor is waived, so the role gate lets it
through every owner door and the step-up in front of a durable grant
proves nothing for it. What it must never reach is the set of doors that
act on the household's OTHER PEOPLE or outlive the visit: who is in the
household and what each member is sent, the principals and channels that
survive a password rotation, the mail and notices that land in every
member's inbox or phone, its own password, factors and sessions (one
visitor changing them locks out the next), and the live credentials
behind the bank connections.

Doors in that set were closed one at a time, each after somebody noticed
the gap, and the next gap was always a sibling nobody had listed. So this
file lists every write route that takes a session, in one of three
tables, and a route in none of them fails the run:

* **REFUSED** — the shared login gets a 403 whatever the body says; the
  test signs in as it and calls each one.
* **REFUSED_BY_BODY** — one door serves both a harmless and a refused
  request, told apart by the body; the named test pins the refusal.
* **ALLOWED** — the household's money and the visitor's own switches,
  one line of reason per group.

Whoever adds a write route decides which it is while they still know.
"""

import os
import re
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web import permissions

from .util import _ensure_db, all_routes, handler_source, route_key

PW = "correct-horse-battery"

SESSION_DEPS = ("Depends(current_user)", "Depends(_user())")

# Refused to the shared login outright.
REFUSED = {
    # --- the household's people: invites, roles, removal, and what each
    # member is sent (permissions.SHARED_LOGIN_PEOPLE_DOORS, at the gate)
    "POST /api/invites",
    "DELETE /api/invites/{token_hash}",
    "POST /api/users/{user_id}/role",
    "POST /api/users/{user_id}/notify",
    "DELETE /api/users/{user_id}",
    # --- principals and channels that outlive the login
    "POST /api/tokens",
    "POST /api/tokens/revoke",
    "POST /api/support-access",
    "POST /api/support-access/revoke",
    "POST /api/webhooks",
    "POST /api/webhooks/{hook_id}",
    "POST /api/webhooks/{hook_id}/delete",
    "POST /api/webhooks/{hook_id}/rotate",
    "POST /api/webhooks/{hook_id}/test",
    "POST /api/connections/import",
    "POST /api/notify/phone",
    "POST /api/notify/phone/verify",
    "POST /api/notify/push/subscribe",
    "POST /api/settings/outbound/resume",
    "POST /settings/email",
    # --- mail and notices sent to members, not to the caller
    "POST /api/jobs/email",
    "POST /api/notify/test",
    "POST /api/continuity/email",
    # --- its own password, address, factors and sessions: one visitor
    # changing them locks every other visitor out
    "POST /api/password/change",
    "POST /api/email/change",
    "POST /api/email/change/cancel",
    "POST /api/email/change/resend",
    "POST /api/totp/enroll",
    "POST /api/totp/confirm",
    "POST /api/totp/recovery-regenerate",
    "POST /api/passkeys",
    "POST /api/passkeys/options",
    "POST /api/sessions/revoke",
    "POST /api/recovery/cancel-pending",
    "POST /api/account/delete",
    "POST /api/account/leave",
    # --- live credentials behind the bank connections, and the plan
    # (permissions.SHARED_LOGIN_CREDENTIAL_DOORS, at the gate)
    "POST /api/accounts/plaid/keys",
    "DELETE /api/accounts/plaid/keys",
    "POST /api/accounts/plaid/validate",
    "POST /api/accounts/mx/keys",
    "DELETE /api/accounts/mx/keys",
    "POST /api/accounts/mx/connect",
    "POST /api/accounts/mx/sync",
    "POST /api/accounts/simplefin",
    "POST /api/accounts/coinbase/link",
    "POST /accounts/plaid/link",
    "DELETE /accounts/plaid/link/{link_token}",
    "POST /accounts/simplefin",
    "POST /api/import/enrich",
    "POST /api/connections/export",
    "POST /api/connections/{item_id}/disconnect",
    "POST /api/connections/{item_id}/reconnect",
}

# One door, two requests: refused when the body asks for the protected
# thing. Value: the test that pins the refusal.
REFUSED_BY_BODY = {
    # a new recipient, another member's mute, or a scheduled text or push
    # switched on (switching one off is open)
    "POST /api/settings": "test_shared_demo_login_cannot_grant",
    # the disconnect and purge modes; 'hide' keeps the bank linked
    "POST /api/accounts/{account_id}/remove":
        "test_shared_demo_login_cannot_grant",
    # a ZIP is a restore, which lands an archive's endpoints; a CSV is not
    "POST /api/import": "test_shared_demo_login_cannot_grant",
    "POST /import": "test_shared_demo_login_cannot_grant",
    "POST /api/import/bulk/run": "test_shared_demo_login_cannot_grant",
    # registering a phone's push token; clearing one plants nothing
    "POST /api/devices/push": "test_shared_demo_login_cannot_grant",
    # signing out its own phone is free; kicking another holder's is not
    "POST /api/devices/revoke": "test_inventory_shared_login",
}

# Form doors validate their fields before the handler refuses; these
# bodies get each one as far as the refusal.
FORM_BODIES = {
    "POST /api/email/change": {"new_email": "elsewhere@example.dev",
                               "password": PW},
    "POST /api/totp/confirm": {"secret": "JBSWY3DPEHPK3PXP",
                               "code": "000000", "password": PW},
}

# Open to the shared login on purpose.
ALLOWED = {
    # --- its OWN switches and sign-in steps: what it is sent, its own
    # browser's push, proving its own password for a step-up. Removing a
    # factor locks nobody out (demoguard.deny_shared_login says why).
    "POST /api/me/email", "POST /api/me/push",
    "POST /api/notify/push/unsubscribe",
    "POST /api/verify-email/resend",
    "POST /api/auth/elevate",
    "POST /api/stepup/passkey", "POST /api/stepup/passkey/options",
    "POST /api/totp/disable", "DELETE /api/passkeys/{pk_id}",
    "POST /api/devices/widget",
    # mail that reaches only the caller: both narrow the shared login to
    # its own address (demoguard.own_address_only)
    "POST /api/settings/smtp-test", "POST /api/settings/recipients/resend",
    # its view of the household's people is only itself, and a save keeps
    # the rows it was not shown
    "PUT /api/continuity",
    # a one-shot download ticket; the export behind it is refused to it
    "POST /api/export/token",
    # feedback to the operator, signed by the login that sent it
    "POST /api/testing/feedback",

    # --- the household's money: the data a visitor came to explore.
    # Inside the household, visible, and undoable; nobody else's access,
    # mail or credentials move.
    "DELETE /api/accounts/links/{group_id}",
    "DELETE /api/transactions/{txn_id:path}/split",
    "PUT /api/transactions/{txn_id:path}/split",
    "DELETE /api/receipts/{rid}",
    "DELETE /api/doctor/unlinked-movements/{movement_id}",
    "POST /accounts/add", "POST /accounts/balance", "POST /accounts/classify",
    "POST /accounts/sync", "POST /alerts/dismiss", "POST /alerts/restore",
    "POST /api/accounts/add", "POST /api/accounts/balance",
    "POST /api/accounts/classify", "POST /api/accounts/exclude",
    "POST /api/accounts/links", "POST /api/accounts/links/dismiss",
    "POST /api/accounts/links/{group_id}/order",
    "POST /api/accounts/rename", "POST /api/accounts/sync",
    "POST /api/accounts/{account_id}/entity",
    "POST /api/accounts/{account_id}/hidden",
    "POST /api/accounts/{account_id}/owner",
    "POST /api/alerts/dismiss", "POST /api/alerts/restore",
    "POST /api/bills/delete", "POST /api/bills/detect", "POST /api/bills/hint",
    "POST /api/bills/merchant-category",
    "POST /api/bills/merchant-category/preview",
    "POST /api/bills/merchant-category/undo",
    "POST /api/bills/proposal", "POST /api/bills/proposals/approve-all",
    "POST /api/bills/restore", "POST /api/bills/save", "POST /api/bills/toggle",
    "POST /bills/detect", "POST /bills/proposal",
    "POST /api/budget/snapshots/backfill", "POST /api/debt-plan",
    "POST /api/categories/rename",
    "POST /api/merchants/merge", "POST /api/merchants/rename",
    "POST /api/merchants/undo",
    "POST /api/rules/delete", "POST /api/rules/disable", "POST /api/rules/set",
    "POST /api/receipts", "POST /api/receipts/{rid}/details",
    "POST /api/receipts/{rid}/items/{line}/tag",
    "POST /api/receipts/{rid}/match", "POST /api/receipts/{rid}/parse",
    "POST /api/receipts/{rid}/unmatch",
    "POST /api/reimburse/link", "POST /api/reimburse/unlink",
    "POST /api/reimburse/{txn_id:path}/flag",
    "POST /api/reimburse/{txn_id:path}/unflag",
    "POST /api/transactions/bulk", "POST /api/transactions/retire-pending",
    "POST /api/transactions/{txn_id:path}/category",
    "POST /api/transactions/{txn_id:path}/entity",
    "POST /api/transactions/{txn_id:path}/note",
    "POST /api/transactions/{txn_id:path}/owner",
    "POST /api/transactions/{txn_id:path}/receipt",
    "POST /transactions/recategorize",
    "POST /settings",
    # a collector's watch hours and stale-alert switch: the household's
    # own list, sent to the recipients it already has
    "POST /api/doctor/scripts/{source}",
    "POST /api/onboarding/backfill/nudge",
    # the business books, members included: ownership shares are
    # bookkeeping, not accounts
    "DELETE /api/business/entities/{entity_id}",
    "DELETE /api/business/entities/{entity_id}/compliance/{obligation_id}",
    "DELETE /api/business/entities/{entity_id}/equity/{movement_id}",
    "DELETE /api/business/entities/{entity_id}/members/{member_id}",
    "DELETE /api/business/entities/{entity_id}/mileage/{trip_id}",
    "PATCH /api/business/entities/{entity_id}",
    "PATCH /api/business/entities/{entity_id}/members/{member_id}",
    "POST /api/business/combine", "POST /api/business/entities",
    "POST /api/business/entities/{entity_id}/archive",
    "POST /api/business/entities/{entity_id}/compliance",
    "POST /api/business/entities/{entity_id}/delete-forever",
    "POST /api/business/entities/{entity_id}/equity",
    "POST /api/business/entities/{entity_id}/import-flags",
    "POST /api/business/entities/{entity_id}/members",
    "POST /api/business/entities/{entity_id}/mileage",
    "POST /api/business/entities/{entity_id}/reimburse",
    "POST /api/business/entities/{entity_id}/restore",
    "POST /api/business/entities/{entity_id}/status",
    "POST /api/business/entities/{entity_id}/transactions/{txn_id:path}/class",
    "POST /api/business/entities/{entity_id}/vendors",
    "POST /api/business/{txn_id:path}/flag",
    "POST /api/business/{txn_id:path}/unflag",
    # importing files and refreshing data the household already has
    "POST /api/import/amazon", "POST /api/import/bulk/analyze",
    "POST /api/import/bulk/jobs/{job_id}/cancel",
    "POST /api/import/coinbase", "POST /api/import/costco",
    "POST /api/import/mapped", "POST /api/import/plan-activity",
    "POST /api/import/rollback", "POST /api/import/taxdoc/analyze",
    "POST /api/import/taxdoc/commit", "POST /api/import/ynab-accounts",
    "POST /import/mapped", "POST /import/rollback",
    "POST /api/jobs/sync", "POST /api/jobs/sync/start",
    # the assistant; its shared question log refuses deletes in the handler
    "POST /api/assistant", "POST /api/assistant/report",
    "DELETE /api/assistant/history", "DELETE /api/assistant/history/{turn_id}",
    "DELETE /api/assistant/threads/{thread_id}",
}

# Routes an installed add-on serves are classified in the add-on's own
# tests; this inventory holds the product's routes only.
ADDON_ROUTES: set[str] = set()

SHARED_TEXT = "shared demonstration login"


def session_write_routes() -> dict:
    out = {}
    for r in all_routes(app_()):
        if not set(r.methods) & {"POST", "PUT", "PATCH", "DELETE"}:
            continue
        if getattr(r.endpoint, "__module__", "").startswith("oikonome_"):
            continue                    # an add-on's door, not the product's
        if any(d in handler_source(r) for d in SESSION_DEPS):
            out[route_key(r)] = r
    return out


def app_():
    import oikonome.web.app as appmod
    return appmod.app


class SharedLoginRouteInventory(unittest.TestCase):

    def setUp(self):
        self.routes = session_write_routes()

    def test_every_session_write_route_is_classified(self):
        named = REFUSED | set(REFUSED_BY_BODY) | ALLOWED
        loose = sorted(k for k in self.routes if k not in named)
        self.assertEqual(
            [], loose,
            f"write route(s) with no shared-login decision: {loose}. If it "
            "acts on another member, sends to them, mints something that "
            "outlives the login or changes its sign-in, refuse it (a path "
            "family in permissions.SHARED_LOGIN_PEOPLE_DOORS) and add it "
            "to REFUSED; otherwise add it to ALLOWED with the reason.")

    def test_no_route_is_named_twice(self):
        tables = [REFUSED, set(REFUSED_BY_BODY), ALLOWED]
        for i, a in enumerate(tables):
            for b in tables[i + 1:]:
                self.assertEqual(set(), a & b)

    def test_the_tables_have_not_gone_stale(self):
        named = REFUSED | set(REFUSED_BY_BODY) | ALLOWED
        gone = sorted(k for k in named - ADDON_ROUTES
                      if k not in self.routes)
        self.assertEqual([], gone, f"tables name gone route(s): {gone}")

    def test_every_gate_family_route_is_listed_refused(self):
        # a door under a refused family is refused by the gate whether or
        # not anyone listed it; the table must say so, not ALLOWED
        for key, r in self.routes.items():
            if permissions.people_door(r.path) \
                    or permissions.credential_door(r.path):
                self.assertIn(key, REFUSED, key)


class SharedLoginIsRefused(unittest.TestCase):
    """Sign in as the shared login and call every REFUSED route."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    def _login(self, waived: bool):
        c = TestClient(self.app)
        email = f"inv-{uuid.uuid4().hex[:10]}@example.dev"
        data = {"email": email, "password": PW}
        if os.environ.get("OIKONOME_HOSTED"):
            from oikonome.auth import signup_invites
            admin = tenancy.admin_connect()
            try:
                data["invite"] = signup_invites.mint(admin, email)
            finally:
                admin.close()
        self.assertEqual(c.post("/api/signup", data=data).status_code, 200)
        admin = tenancy.admin_connect()
        try:
            if waived:
                admin.execute("UPDATE users SET second_factor_waived=TRUE "
                              "WHERE email=%s", (email,))
            row = admin.execute("SELECT id, tenant_id FROM users "
                                "WHERE email=%s", (email,)).fetchone()
        finally:
            admin.close()
        return c, email, row

    def _member(self, tenant_id):
        """A second member of the same household, as the owner added it."""
        uid = uuid.uuid4()
        email = f"member-{uid.hex[:10]}@example.dev"
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO users (id, tenant_id, email, password_hash, "
                "role) VALUES (%s, %s, %s, 'x', 'member')",
                (uid, tenant_id, email))
        finally:
            admin.close()
        return str(uid), email

    @staticmethod
    def _url(path: str, member_id: str) -> str:
        path = path.replace("{user_id}", member_id)
        return re.sub(r"\{[^}]+\}", uuid.uuid4().hex, path)

    def test_every_refused_route_refuses_the_shared_login(self):
        c, _, row = self._login(waived=True)
        member_id, _ = self._member(row["tenant_id"])
        routes = session_write_routes()
        let_through = []
        for key in sorted(REFUSED):
            if key not in routes:
                self.assertIn(key, ADDON_ROUTES, key)
                continue
            method, path = key.split(" ", 1)
            url = self._url(path, member_id)
            if key in FORM_BODIES:
                r = c.request(method, url, data=FORM_BODIES[key])
            else:
                r = c.request(method, url, json={"password": PW})
            if r.status_code != 403 or SHARED_TEXT not in r.text:
                let_through.append(f"{key} -> {r.status_code} "
                                   f"{r.text[:120]}")
        self.assertEqual([], let_through, "\n".join(let_through))

    def test_the_shared_login_cannot_mute_another_member(self):
        c, _, row = self._login(waived=True)
        member_id, email = self._member(row["tenant_id"])
        r = c.post(f"/api/users/{member_id}/notify",
                   json={"email_muted": True, "push_muted": True})
        self.assertEqual(r.status_code, 403, r.text)
        admin = tenancy.admin_connect()
        try:
            u = admin.execute("SELECT push_muted FROM users WHERE id=%s",
                              (member_id,)).fetchone()
        finally:
            admin.close()
        self.assertFalse(u["push_muted"])
        tc = tenancy.tenant_connect(row["tenant_id"])
        try:
            from oikonome.engine import budget
            muted = budget.load_config(tc).get("email_muted") or []
        finally:
            tc.close()
        self.assertNotIn(email, muted)

    def test_the_shared_login_cannot_sign_out_another_phone(self):
        c, _, _ = self._login(waived=True)
        r = c.post("/api/devices/revoke",
                   json={"id": str(uuid.uuid4()), "password": PW})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn(SHARED_TEXT, r.text)

    def test_an_ordinary_owner_still_mutes_a_member(self):
        c, _, row = self._login(waived=False)
        member_id, _ = self._member(row["tenant_id"])
        r = c.post(f"/api/users/{member_id}/notify",
                   json={"push_muted": True})
        self.assertEqual(r.status_code, 200, r.text)

    def test_an_ordinary_owner_still_revokes_an_invite(self):
        c, _, _ = self._login(waived=False)
        r = c.delete(f"/api/invites/{uuid.uuid4().hex}")
        # no such invite, but the door answered the owner
        self.assertEqual(r.status_code, 404, r.text)


if __name__ == "__main__":
    unittest.main()
