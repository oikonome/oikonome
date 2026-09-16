"""Inventory: every mutating route has decided what a MEMBER may do with it.

A household has three roles. Owner and viewer were easy to keep straight
because the gate was one line — anybody who was not the owner could not
write at all. The member role removes that simplicity: a member passes the
write gate, so from now on every mutating route is either something a
member may do or something only the owner may do, and NOT deciding is
itself a decision — the permissive one.

This file is the triage. It is deliberately shaped like
`test_inventory_stepup.py`, which exists for the same reason: reading the
route table by eye is how the gaps get in.

Three ways a route can be classified, checked in this order:

* **owner by policy** — `permissions.owner_only(path)` says so. This is
   the list a reader should look at first, because it is the product
   promise: connections, exports, the household roster, billing.
* **owner in the handler** — the route calls `_owner_only(...)` or reads
   the role itself. Doors whose path carries an id cannot be named by a
   prefix, and a few (the ZIP restore) depend on the request body rather
   than the path.
* **MEMBER_OK** — written down here, on purpose, one line each.

A new mutating route that is none of the three fails this test, and the
person who added it decides which it is while they still remember why.
"""

import re
import unittest

from oikonome import ext
from oikonome.web import permissions
from oikonome.web.app import app

from .util import handler_source, mutating_routes, route_key

# Reads its own authorization instead of leaning on the path policy.
INLINE_OWNER = re.compile(
    r"_owner_only\(|may_restore\(|"
    r'role"\]\s*!=\s*"owner"|role"\)\s*!=\s*"owner"')

# Doors that answer before any session exists (login, signup, invite
# claim, the pre-auth mail links) and the operator console, which has its
# own credential and is not a household role at all. Detected structurally
# — no `Depends(current_user)` / `Depends(_user())` — rather than listed,
# so the set cannot go stale.
SESSION_DEPS = ("Depends(current_user)", "Depends(_user())")

# Everything a member may do. Grouped the way the product talks about it;
# each line is somebody's decision, not a leftover.
MEMBER_OK = {
    # --- the household's money: the whole point of the role -------------
    "POST /api/transactions/{txn_id}/category",
    "POST /api/transactions/{txn_id}/note",
    "POST /api/transactions/{txn_id}/owner",
    "POST /api/transactions/{txn_id}/entity",
    "POST /api/transactions/{txn_id}/receipt",
    "POST /api/transactions/bulk",
    # retiring a pending hold the bank abandoned: inside the household,
    # visible, and reversible (removed=1 keeps the row and its id)
    "POST /api/transactions/retire-pending",
    "POST /transactions/recategorize",
    "POST /api/categories/rename",
    "POST /api/merchants/merge",
    "POST /api/merchants/rename",
    "POST /api/merchants/undo",
    "POST /api/rules/set",
    "POST /api/rules/delete",
    "POST /api/rules/disable",
    "POST /api/bills/save",
    "POST /api/bills/delete",
    "POST /api/bills/restore",
    "POST /api/bills/toggle",
    "POST /api/bills/detect",
    "POST /api/bills/hint",
    "POST /api/bills/proposal",
    "POST /api/bills/proposals/approve-all",
    "POST /api/bills/merchant-category",
    "POST /api/bills/merchant-category/preview",
    "POST /api/bills/merchant-category/undo",
    "POST /bills/detect",
    "POST /bills/proposal",
    "POST /api/reimburse/link",
    "POST /api/reimburse/unlink",
    "POST /api/reimburse/{txn_id}/flag",
    "POST /api/reimburse/{txn_id}/unflag",
    "POST /api/receipts/{rid}/parse",
    "POST /api/receipts/{rid}/items/{line}/tag",
    "DELETE /api/receipts/{rid}",
    "POST /api/budget/snapshots/backfill",
    "POST /api/settings",
    "POST /settings",
    "POST /api/assistant",

    # --- the business books, for a household that runs one --------------
    "POST /api/business/entities",
    "PATCH /api/business/entities/{entity_id}",
    "DELETE /api/business/entities/{entity_id}",
    "POST /api/business/entities/{entity_id}/archive",
    "POST /api/business/entities/{entity_id}/restore",
    "POST /api/business/entities/{entity_id}/status",
    "POST /api/business/entities/{entity_id}/members",
    "POST /api/business/entities/{entity_id}/vendors",
    "POST /api/business/entities/{entity_id}/equity",
    "DELETE /api/business/entities/{entity_id}/equity/{movement_id}",
    "POST /api/business/entities/{entity_id}/mileage",
    "DELETE /api/business/entities/{entity_id}/mileage/{trip_id}",
    "POST /api/business/entities/{entity_id}/compliance",
    "DELETE /api/business/entities/{entity_id}/compliance/{obligation_id}",
    "POST /api/business/entities/{entity_id}/reimburse",
    "POST /api/business/entities/{entity_id}/import-flags",
    "POST /api/business/entities/{entity_id}/transactions/{txn_id}/class",
    "POST /api/business/combine",
    "POST /api/business/{txn_id}/flag",
    "POST /api/business/{txn_id}/unflag",
    "POST /api/accounts/{account_id}/entity",
    "POST /api/accounts/{account_id}/owner",

    # --- accounts as DATA (naming, tidying, manual balances). Linking,
    # re-keying and disconnecting are the owner's and live in the policy
    # list; nothing here touches a credential or an aggregator item.
    "POST /api/accounts/add",
    "POST /api/accounts/rename",
    "POST /api/accounts/classify",
    "POST /api/accounts/exclude",
    "POST /api/accounts/balance",
    "POST /api/accounts/links",
    "POST /api/accounts/links/dismiss",
    "POST /api/accounts/links/{group_id}/order",
    "DELETE /api/accounts/links/{group_id}",
    "POST /accounts/add",
    "POST /accounts/balance",
    "POST /accounts/classify",

    # --- importing transactions. A ZIP through these doors is a RESTORE
    # and is refused for members inside the handler (permissions.
    # may_restore), which is why /api/import — and since the no-JS door
    # gained the same zip-to-background-restore branch, POST /import —
    # read as owner-guarded above while members still import files.
    "POST /api/import/mapped",
    "POST /api/import/rollback",
    "POST /api/import/bulk/analyze",
    "POST /api/import/bulk/run",
    "POST /api/import/taxdoc/analyze",
    "POST /api/import/taxdoc/commit",
    "POST /api/import/plan-activity",
    "POST /api/import/coinbase",
    "POST /api/import/amazon",
    "POST /api/import/costco",
    "POST /import/mapped",
    "POST /import/rollback",

    # --- running the household's jobs. Refreshing data is not changing
    # the connection that carries it.
    "POST /api/jobs/sync",
    "POST /api/jobs/sync/start",
    "POST /accounts/sync",
    "POST /api/alerts/dismiss",
    "POST /api/alerts/restore",
    "POST /alerts/dismiss",
    "POST /alerts/restore",

    # --- their OWN account: password, factors, sessions, devices, the
    # address the mail goes to, and leaving the household. Viewers hold
    # these too (permissions.SELF_WRITE_OK).
    "POST /api/password/change",
    "POST /api/email/change",
    "POST /api/me/email",
    "POST /api/account/leave",
    "POST /api/sessions/revoke",
    "POST /api/totp/enroll",
    "POST /api/totp/confirm",
    "POST /api/totp/disable",
    "POST /api/totp/recovery-regenerate",
    "POST /api/passkeys",
    "POST /api/passkeys/options",
    "DELETE /api/passkeys/{pk_id}",
    "POST /api/stepup/passkey",
    "POST /api/stepup/passkey/options",
    "POST /api/auth/elevate",
    "POST /api/devices/push",
    "POST /api/devices/revoke",
    "POST /api/notify/push/subscribe",
    "POST /api/notify/push/unsubscribe",
    "POST /api/verify-email/resend",
    "POST /api/testing/feedback",
} | set(ext.gate.inventory().get('member_routes', ()))


class RolePermissionInventoryTests(unittest.TestCase):

    def setUp(self):
        self.routes = {}
        for r in mutating_routes(app):
            src = handler_source(r)
            if any(d in src for d in SESSION_DEPS):
                self.routes[route_key(r)] = (r.path, src)

    def _owner_guarded(self, key: str) -> bool:
        path, src = self.routes[key]
        return permissions.owner_only(path) or bool(INLINE_OWNER.search(src))

    def test_every_mutating_route_is_triaged(self):
        untriaged = sorted(k for k in self.routes
                           if not self._owner_guarded(k)
                           and k not in MEMBER_OK)
        self.assertEqual(
            [], untriaged,
            f"new mutating route(s) with no member decision: {untriaged}. "
            "Either make it owner-only (add the path family to "
            "permissions.OWNER_ONLY, or check the role in the handler), or "
            "add it to MEMBER_OK — a route nobody classifies is one a "
            "member gets by default.")

    def test_member_list_has_not_gone_stale(self):
        gone = sorted(k for k in MEMBER_OK if k not in self.routes)
        self.assertEqual(
            [], gone, f"MEMBER_OK names route(s) that no longer exist: {gone}")

    def test_nothing_is_both(self):
        """A route cannot be a member's and the owner's at once. When the
        two disagree the handler wins silently, so the list would be
        documentation of something that is not true."""
        both = sorted(k for k in MEMBER_OK
                      if k in self.routes and self._owner_guarded(k))
        self.assertEqual(
            [], both,
            f"listed as member-allowed but owner-guarded in code: {both}")

    def test_a_viewer_can_reach_its_own_account_doors(self):
        """SELF_WRITE_OK is 'every role holds these, viewers included' —
        so a VIEWER (not just a member) may verify and change its own login
        address and manage its own push subscription. Otherwise a viewer
        whose verify link lapses is a permanent dead end: Resend and the
        change form both 403, and the owner has no door to help either."""
        for path in ("/api/email/change", "/api/verify-email/resend",
                     "/api/notify/push/subscribe",
                     "/api/notify/push/unsubscribe",
                     "/api/password/change", "/api/auth/elevate"):
            self.assertTrue(permissions.can_write("viewer", path),
                            f"a viewer is wrongly refused {path}")

    def test_the_promised_owner_only_doors_really_are(self):
        """The four things the product says a member cannot do, pinned by
        the route that does each one."""
        for key in ("POST /api/account/delete",     # end the account
                    "POST /api/export/token",       # take the data out
                    "POST /api/invites",            # add somebody
                    "DELETE /api/users/{user_id}",  # remove somebody
                    "POST /api/users/{user_id}/role",
                    "POST /api/accounts/plaid/keys",       # connections
                    "POST /api/connections/{item_id}/disconnect"):
            self.assertIn(key, self.routes, f"{key} vanished")
            self.assertTrue(self._owner_guarded(key),
                            f"{key} is no longer owner-only")


if __name__ == "__main__":
    unittest.main()
