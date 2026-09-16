"""Inventory: every tenant-scoped table is protected by RLS.

Tenancy in this app is RLS, not application filtering — engine SQL never
mentions `tenant_id` because `tenancy.tenant_connect` sets
`app.tenant_id` and the policies do the rest. That contract has exactly one
failure mode: a table carrying `tenant_id` that nobody enabled RLS on. It
looks tenant-scoped to every reader, and it isn't.

The trap is not hypothetical: `users`, `invites` and `api_tokens` all carry
`tenant_id` with NO RLS, so anything that walks "the tenant's tables"
generically — the portable export is written that way — sweeps in rows the
tenant must never see. Nothing about such a table announces itself, so the
check has to be structural.

So the exemption list below is PINNED: the assertion is equality, not
subset. A new table with a `tenant_id` column and no RLS fails this test
until someone either enables RLS or adds it here with a reason. Deleting a
policy from an existing table fails it too. Neither can happen quietly.

Exempt tables are control-plane by design (DEVELOPMENT.md: "control-plane tables
have no RLS — use a plain app-role connection, and be careful"). They are
read across tenants on purpose — the nightly push sweep in
`notify/push.py`, operator support grants, fleet-wide rollups — so a
policy keyed on the ambient tenant would break them. They filter with an explicit
`WHERE tenant_id = %s` instead, which is weaker and is the point of writing
them down here rather than letting them blend in.
"""

import unittest

import psycopg

from oikonome import ext

from .util import TEST_DB, _admin_dsn, _ensure_db

# tenant_id, deliberately NO RLS — control plane. Keep the reason current.
RLS_EXEMPT = {
    "api_tokens":         "script tokens; authenticated pre-tenant-context",
    "device_tokens":      "looked up by bearer token, before a tenant is known",
    "invites":            "claimed by a recipient who has no tenant yet",
    "push_subscriptions": "notify/push.py sweeps every tenant nightly",
    # Answered by an invited person who, on self-host, has no
    # account and therefore no tenant context — the token is looked up
    # before any tenant is known, exactly like `invites`. RLS would also
    # protect nothing here: the app role holds NO grant on this table at
    # all (migration 074), so every read and write is admin_connect with an
    # explicit tenant_id in the WHERE clause.
    "recipient_invites":  "answered by a recipient with no tenant context",
    "sessions":           "looked up by cookie, before a tenant is known",
    "support_consents":   "operator support access; read outside the tenant",
    "users":              "login is by email, before a tenant is known",
} | ext.gate.inventory().get('rls_exempt', {})


class RlsInventoryTests(unittest.TestCase):
    """Structural, not behavioural — asks the catalog, not the app."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def _tenant_tables(self):
        """(table, rls_enabled) for every public table with a tenant_id."""
        with psycopg.connect(_admin_dsn(TEST_DB)) as c:
            return c.execute(
                """SELECT t.tablename, c.relrowsecurity
                     FROM pg_tables t
                     JOIN pg_class c ON c.relname = t.tablename
                    WHERE t.schemaname = 'public'
                      AND EXISTS (SELECT 1 FROM information_schema.columns col
                                   WHERE col.table_schema = 'public'
                                     AND col.table_name = t.tablename
                                     AND col.column_name = 'tenant_id')
                    ORDER BY 1""").fetchall()

    def test_every_tenant_table_has_rls_or_is_a_named_exemption(self):
        rows = self._tenant_tables()
        self.assertTrue(rows, "no tenant tables found — migrations didn't run")
        unprotected = {t for t, rls in rows if not rls}
        missing = unprotected - set(RLS_EXEMPT)
        self.assertEqual(
            set(), missing,
            "table(s) carry tenant_id with NO row-level security: "
            f"{sorted(missing)}. Engine SQL does not filter by tenant_id — "
            "RLS is the only thing separating tenants, so an unprotected "
            "table is a cross-tenant leak. Enable RLS in the migration, or "
            "if it is genuinely control-plane add it to RLS_EXEMPT with a "
            "reason.")

    def test_exemption_list_has_not_gone_stale(self):
        """An exemption that no longer describes reality is worse than none —
        it reads as 'reviewed' while protecting nothing."""
        rows = self._tenant_tables()
        unprotected = {t for t, rls in rows if not rls}
        names = {t for t, _ in rows}

        gained = set(RLS_EXEMPT) & names - unprotected
        self.assertEqual(
            set(), gained,
            f"{sorted(gained)} now HAS rls — drop it from RLS_EXEMPT so the "
            "list keeps meaning 'deliberately unprotected'.")

        gone = set(RLS_EXEMPT) - names
        self.assertEqual(
            set(), gone,
            f"RLS_EXEMPT names table(s) that no longer exist: {sorted(gone)}")

    def test_rls_covers_the_bulk_of_the_schema(self):
        """Backstop for the pathological case the two tests above can't see:
        a migration that drops RLS from everything at once would leave the
        exemption list 'accurate' and the app wide open."""
        rows = self._tenant_tables()
        protected = [t for t, rls in rows if rls]
        self.assertGreaterEqual(
            len(protected), 40,
            f"only {len(protected)} tenant tables have RLS — a migration "
            "may have stripped policies wholesale")
