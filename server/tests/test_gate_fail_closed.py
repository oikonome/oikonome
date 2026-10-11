"""A gate add-on that is installed but cannot load must not turn into no gate.

A household's own instance runs with nothing installed and the permissive
defaults; that stays true, and a hosted instance run with no add-on at all
is a documented configuration that gets the same defaults. But when a gate
IS registered and its import fails (a dependency bump, a typo, a half-built
image), the operator meant this instance to be gated. On a hosted instance
falling back to the permissive gate would lift every cap and lockout for
every tenant at once with only a log line to show for it, so it falls back
to a gate that refuses instead: nothing billed, nothing written, no new
accounts, until the add-on loads again.
"""

import os
import unittest
from unittest import mock

from oikonome import ext


class _BrokenEntryPoint:
    name = "billing"

    def load(self):
        raise ImportError("synthetic: the add-on's dependency is missing")


class GateFailClosedTests(unittest.TestCase):

    def setUp(self):
        ext._group.cache_clear()
        ext.set_gate(None)
        self.addCleanup(ext._group.cache_clear)
        self.addCleanup(ext.set_gate, None)

    def _resolve(self, *, hosted, eps):
        env = {"OIKONOME_HOSTED": "1" if hosted else ""}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(ext, "entry_points", return_value=eps), \
                self.assertLogs("oikonome.ext", "WARNING") as logs:
            ext.log.warning("resolving")       # assertLogs needs one line
            g = ext.gate._resolve()
        return g, logs.output

    def test_a_hosted_instance_with_a_broken_gate_refuses(self):
        g, logs = self._resolve(hosted=True, eps=[_BrokenEntryPoint()])
        self.assertEqual(g.name, "unavailable")
        self.assertFalse(g.tier_allows(None, "sms"))
        self.assertFalse(g.tier_allows(None, "enrich"))
        self.assertEqual(g.institution_cap(None), 0)
        self.assertEqual(g.enrich_rows_cap(), 0)
        self.assertEqual(g.plaid_products(None), [])
        self.assertTrue(g.write_blocked("any-tenant"))
        self.assertTrue(g.at_cap(0))
        self.assertTrue(any("failed to load" in line for line in logs))

    def test_a_self_hosted_instance_stays_ungated(self):
        g, _ = self._resolve(hosted=False, eps=[_BrokenEntryPoint()])
        self.assertEqual(g.name, "open")
        self.assertTrue(g.tier_allows(None, "sms"))
        self.assertFalse(g.write_blocked("any-tenant"))

    def test_a_hosted_instance_with_no_add_on_keeps_the_defaults(self):
        g, _ = self._resolve(hosted=True, eps=[])
        self.assertEqual(g.name, "open")
        self.assertIsNone(g.institution_cap(None))


class _Admin:
    """Records writes; a fail-closed gate must make none."""

    def __init__(self):
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append(sql)


class EveryAnswerFailsClosedTests(unittest.TestCase):
    """Every policy question the product asks a gate, and the answer it
    gets while the add-on is registered but broken. A method added to Gate
    without a decision here fails the inventory test."""

    # answers that cannot spend money, lift a freeze or lose a record, so
    # the household's-own-instance default is also the safe answer
    INHERITED = {
        "billing_enabled", "plan", "tiers", "tier_name", "effective_tier",
        "lockout_statuses", "sync_priority_sql", "invite_options",
        "invite_summary", "erasure_receipt", "deliver_referral",
        "on_demo_household", "export_exclusions", "machinery_tables",
        "demo_keep_tables", "inventory", "nightly", "audit_noise",
        "mail_kinds", "console_context", "tenant_context", "health",
        "on_tenant_restored", "accounts_used", "accounts_used_guarded",
        "capacity_message",
    }

    def setUp(self):
        self.g = ext._FailClosedGate()

    def test_every_gate_method_has_a_decided_answer(self):
        methods = {n for n, v in vars(ext.Gate).items()
                   if callable(v) and not n.startswith("_")}
        overridden = {n for n in vars(ext._FailClosedGate)
                      if callable(getattr(ext._FailClosedGate, n))
                      and not n.startswith("_")}
        self.assertEqual(methods - overridden - self.INHERITED, set())

    def test_a_frozen_household_stays_frozen(self):
        code, _msg, open_paths = self.g.unknown_status_lockout()
        self.assertGreaterEqual(code, 400)
        self.assertEqual(open_paths, ())
        self.assertIsNone(ext.Gate().unknown_status_lockout())

    def test_reactivation_is_refused_and_writes_nothing(self):
        admin = _Admin()
        with self.assertRaises(ext.GateUnavailable):
            self.g.reactivate(admin, "any-tenant")
        self.assertEqual(admin.sql, [])

    def test_no_household_is_created(self):
        """Every door that creates a household provisions it inside its
        transaction; refusing there rolls the creation back rather than
        leave a household with no billing record."""
        admin = _Admin()
        with self.assertRaises(ext.GateUnavailable):
            self.g.provision(admin, "any-tenant", invite={"options": {}},
                             ref=None, verified=False, reclaim=False)
        self.assertEqual(admin.sql, [])

    def test_every_household_may_have_money_attached(self):
        self.assertTrue(self.g.money_attached(None, "any-tenant"))

    def test_nothing_is_reported_released_or_archived(self):
        self.assertNotEqual(self.g.on_tenant_delete_scheduled("t"), "n/a")
        with self.assertRaises(ext.GateUnavailable):
            self.g.release_tenant("any-tenant")
        with self.assertRaises(ext.GateUnavailable):
            self.g.on_tenant_purge(None, "any-tenant")

    def test_the_lockout_lookup_falls_to_the_gate_for_an_unnamed_status(self):
        known = {"suspended": (403, "suspended", ())}
        self.addCleanup(ext.set_gate, None)
        ext.set_gate(self.g)
        self.assertEqual(ext.lockout_for("suspended", known),
                         known["suspended"])
        self.assertIsNone(ext.lockout_for("active", known))
        self.assertIsNone(ext.lockout_for(None, known))
        code, _msg, _open = ext.lockout_for("an_add_on_freeze", known)
        self.assertGreaterEqual(code, 400)
        # with a permissive gate the same status passes, as it always did
        ext.set_gate(ext.Gate())
        self.assertIsNone(ext.lockout_for("an_add_on_freeze", known))

    def test_a_missing_add_on_is_its_own_failure(self):
        """Callers step past an add-on's own failures; a missing add-on
        must be distinguishable from them, never a plain error type."""
        self.assertIsNot(ext.GateUnavailable, RuntimeError)
        self.assertFalse(issubclass(ext.GateUnavailable,
                                    (ValueError, LookupError, OSError)))


if __name__ == "__main__":
    unittest.main()
