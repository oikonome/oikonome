"""Inventory: the demo instance's data doors and security doors stay shut.

The hosted demo (`demo_mode: true`) is deliberately explorable — budgets,
buckets, goals and bills all save, because a demo you cannot touch teaches
nobody anything. What must never work is a DOOR: anything that moves real
credentials or real data in or out (aggregator links, file imports, the
sealed connections bundle, script tokens, live pulls, outbound mail) or
mutates account security (password, email, 2FA, passkeys, invites, delete).
Its login credentials are printed publicly, so one visitor walking through
an open door reconfigures the instance for everyone after them.

`demoguard.deny(user)` is the backstop that makes the disabled-looking SPA
true no matter what is POSTed. It is applied per route, which is the whole
problem: one missing call in a codebase with 167 mutating routes leaves a
door open, and the Jinja `POST /settings` write and the session
mass-revoke are exactly the shape that gets missed.

Two assertions:

  * DEMO_GUARDED is a PIN — every route in it denies today and must keep
    denying. This catches the regression that actually happens: a handler
    gets refactored and the guard does not come along.
  * DOOR matches by PATH — a new import/aggregator/credential route must
    either deny or be written into DEMO_OPEN with a reason.

Ordinary editing routes (bills, budgets, categories, rules) are absent from
both lists on purpose. They are supposed to work in the demo, and demanding
a decision about every new one would be friction with no security value.

Detection is source-based and follows ONE level of delegation, because
`pages.plaid_link_start` calls `demoguard.deny` inside `_start_plaid_session`
rather than in the handler — a handler-only grep reports that door as wide
open. Behavioural probing cannot replace this: FastAPI validates the request
body before the handler runs, so calling these routes without a payload
returns 422 and never reaches the guard. Per-route behaviour is covered by
test_demo_data_doors.py and test_demo.py; this file covers the SET.
"""

import inspect
import re
import sys
import unittest

from oikonome import ext
from oikonome.web.app import app

from .util import handler_source, mutating_routes, route_key

# PINNED: these refuse a demo tenant today and must continue to.
DEMO_GUARDED = {
    "DELETE /api/accounts/mx/keys",
    "DELETE /api/accounts/plaid/keys",
    "DELETE /api/business/entities/{entity_id}",
    "DELETE /api/business/entities/{entity_id}/compliance/{obligation_id}",
    "DELETE /api/business/entities/{entity_id}/equity/{movement_id}",
    "DELETE /api/business/entities/{entity_id}/mileage/{trip_id}",
    "DELETE /api/invites/{token_hash}",
    "DELETE /api/passkeys/{pk_id}",
    "DELETE /api/users/{user_id}",
    "PATCH /api/business/entities/{entity_id}",
    "POST /accounts/plaid/link",
    "POST /accounts/simplefin",
    "POST /accounts/sync",
    "POST /api/account/delete",
    "POST /api/accounts/coinbase/link",
    "POST /api/accounts/mx/connect",
    "POST /api/accounts/mx/keys",
    "POST /api/accounts/mx/sync",
    "POST /api/accounts/plaid/keys",
    "POST /api/accounts/plaid/validate",
    "POST /api/accounts/simplefin",
    "POST /api/accounts/{account_id}/entity",
    "POST /api/accounts/{account_id}/remove",
    "POST /api/business/combine",
    "POST /api/business/entities",
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
    "POST /api/business/entities/{entity_id}/transactions/{txn_id}/class",
    "POST /api/business/entities/{entity_id}/vendors",
    "POST /api/connections/export",
    "POST /api/connections/import",
    "POST /api/connections/{item_id}/disconnect",
    "POST /api/email/change",
    "POST /api/import",
    "POST /api/import/amazon",
    "POST /api/import/costco",
    "POST /api/import/bulk/analyze",
    "POST /api/import/bulk/run",
    "POST /api/import/coinbase",
    "POST /api/import/enrich",
    "POST /api/import/plan-activity",
    "POST /api/import/mapped",
    "POST /api/import/rollback",
    "POST /api/import/taxdoc/analyze",
    "POST /api/import/taxdoc/commit",
    "POST /api/invites",
    "POST /api/jobs/email",
    "POST /api/jobs/sync",
    "POST /api/jobs/sync/start",
    "POST /api/notify/phone",
    "POST /api/onboarding/backfill/nudge",
    "POST /api/notify/phone/verify",
    "POST /api/notify/push/subscribe",
    "POST /api/notify/test",
    "POST /api/passkeys",
    "POST /api/passkeys/options",
    "POST /api/password/change",
    "POST /api/receipts/{rid}/parse",
    "POST /api/sessions/revoke",
    "POST /api/settings",
    "POST /api/settings/recipients/resend",
    "POST /api/settings/smtp-test",
    "POST /api/support-access",
    "POST /api/support-access/revoke",
    "POST /api/testing/feedback",
    "POST /api/tokens",
    "POST /api/tokens/revoke",
    "POST /api/totp/confirm",
    "POST /api/totp/disable",
    "POST /api/totp/enroll",
    "POST /api/totp/recovery-regenerate",
    "POST /api/transactions/{txn_id}/entity",
    "POST /api/transactions/{txn_id}/note",
    "POST /api/transactions/{txn_id}/receipt",
    "POST /api/users/{user_id}/role",
    "POST /import",
    "POST /import/mapped",
    "POST /import/rollback",
    "POST /settings",
    "POST /settings/email",
} | set(ext.gate.inventory().get('demo_guarded', ()))

# Path shapes that move credentials or data across the instance boundary.
DOOR = re.compile(
    r"/import|/connections|plaid|/mx/|simplefin|coinbase|plan-activity|/settings"
    r"|/tokens|/notify|/jobs|/sync|/keys|/scripts|/support-access|/passkeys"
    r"|/totp|/password|/invites|/sessions|/account/delete|/email/change"
    r"|/users/",
    re.IGNORECASE)

# Matches DOOR but correctly does NOT call demoguard.
DEMO_OPEN = {
    "POST /admin/console/sync-now":
        "operator console, not a tenant session — demoguard needs a user",
    "POST /api/plaid/webhook":
        "Plaid calls this, not a browser; no user to guard, verified by sig",
    "POST /api/notify/push/unsubscribe":
        "de-escalation: drops a push endpoint, cannot add or read one",
    "POST /act":
        "a button from the daily email, no session; demo tenants send no "
        "mail so no button exists for one, and notify.mailact.apply refuses "
        "a demo tenant outright",
    "POST /api/doctor/scripts/{source}":
        "sets stale-warning hours/alerts only; opens no door and moves no "
        "data (the token doors themselves are POST /api/tokens, pinned)",
}


def guards_demo(route) -> bool:
    """Does this route refuse a demo tenant?

    Checks the handler, then any module-level callable it invokes — one
    level, which is all the delegation this codebase actually uses.
    """
    src = handler_source(route)
    if "demoguard.deny" in src:
        return True
    module = sys.modules.get(route.endpoint.__module__)
    if module is None:
        return False
    for name in set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", src)):
        fn = getattr(module, name, None)
        if fn is None or not callable(fn):
            continue
        try:
            if "demoguard.deny" in inspect.getsource(fn):
                return True
        except (OSError, TypeError):
            continue
    return False


class DemoInventoryTests(unittest.TestCase):

    def setUp(self):
        self.routes = {route_key(r): r for r in mutating_routes(app)}

    def test_pinned_doors_still_refuse_the_demo(self):
        unguarded = sorted(k for k in DEMO_GUARDED
                           if k in self.routes and not guards_demo(self.routes[k]))
        self.assertEqual(
            [], unguarded,
            f"route(s) lost their demo guard: {unguarded}. The demo's "
            "credentials are printed publicly — an open door lets one "
            "visitor reconfigure the instance for every visitor after them. "
            "Restore the demoguard.deny(user) call.")

    def test_pin_has_not_gone_stale(self):
        gone = sorted(k for k in DEMO_GUARDED if k not in self.routes)
        self.assertEqual(
            [], gone,
            f"DEMO_GUARDED names route(s) that no longer exist: {gone}")

    def test_every_door_route_is_guarded_or_triaged(self):
        untriaged = sorted(
            key for key, route in self.routes.items()
            if DOOR.search(route.path)
            and key not in DEMO_GUARDED
            and key not in DEMO_OPEN)
        self.assertEqual(
            [], untriaged,
            f"new door-shaped route(s) with no demo decision: {untriaged}. "
            "Either call demoguard.deny(user) and add the route to "
            "DEMO_GUARDED, or add it to DEMO_OPEN with the reason it is "
            "safe to leave open on a publicly-shared instance.")

    def test_open_doors_are_all_real_routes_with_reasons(self):
        gone = sorted(k for k in DEMO_OPEN if k not in self.routes)
        self.assertEqual(
            [], gone, f"DEMO_OPEN names route(s) that no longer exist: {gone}")
        blank = sorted(k for k, why in DEMO_OPEN.items() if not why.strip())
        self.assertEqual([], blank, f"exemption(s) with no reason: {blank}")

    def test_the_guard_itself_is_still_wired(self):
        """Backstop: if demoguard.deny stopped raising, every assertion above
        would still pass while the demo sat wide open."""
        from fastapi import HTTPException

        from oikonome.web import demoguard
        tid = "00000000-0000-0000-0000-0000000000de"
        demoguard._cache[tid] = True
        try:
            with self.assertRaises(HTTPException) as caught:
                demoguard.deny({"tenant_id": tid})
            self.assertEqual(403, caught.exception.status_code)
        finally:
            demoguard._cache.pop(tid, None)
