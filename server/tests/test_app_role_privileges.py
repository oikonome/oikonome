"""The app role's control-plane blast radius stays small.

Control-plane tables (users, sessions, invites, …) have no RLS, so any
SQL-injection-class bug anywhere in the app is global. The app role's
legitimate UPDATE surface on `users` is exactly two columns —
password_hash (change/reset) and totp_secret (enroll/disable) — while a
blanket re-grant would hand it UPDATE on every column. Column-scoped grants
make the classic takeover pivots hard permission errors even under
injection: rewriting a victim's email (then password-resetting into an
attacker inbox), promoting viewer → owner, re-parenting a user across
tenants, forging verified_at. schema_migrations is migrate-only (admin
role), so the app role keeps SELECT and loses writes.

Honest residual (see migrate.py): password_hash itself must stay
app-writable, so an injected UPDATE of the hash is still possible; a
GUC-gated trigger would not help because injected SQL can call
set_config inside the same query. Full closure means SECURITY DEFINER
auth functions — deliberately deferred.
"""

import unittest
import uuid

import psycopg
from psycopg import errors
from psycopg.rows import dict_row

from oikonome import ext
from oikonome.db import tenancy

from .util import _ensure_db


def _app_conn():
    return psycopg.connect(tenancy.APP_DSN, row_factory=dict_row,
                           autocommit=True)


class AppRolePrivilegeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()
        admin = tenancy.admin_connect()
        try:
            cls.tid = tenancy.create_tenant(
                admin, f"priv-{uuid.uuid4().hex[:8]}")
            cls.uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash) "
                "VALUES (%s, %s, 'x') RETURNING id",
                (cls.tid, f"priv-{uuid.uuid4().hex[:8]}@example.dev"),
            ).fetchone()["id"]
        finally:
            admin.close()

    def _denied(self, sql, *params):
        with _app_conn() as conn:
            with self.assertRaises(errors.InsufficientPrivilege):
                conn.execute(sql, params)

    def _allowed(self, sql, *params):
        with _app_conn() as conn:
            conn.execute(sql, params)

    # -- the takeover pivots an injected UPDATE loses ------------------

    def test_app_role_cannot_rewrite_email(self):
        self._denied("UPDATE users SET email='a@evil.test' WHERE id=%s",
                     self.uid)

    def test_app_role_cannot_promote_role(self):
        self._denied("UPDATE users SET role='owner' WHERE id=%s", self.uid)

    def test_app_role_cannot_reparent_across_tenants(self):
        self._denied("UPDATE users SET tenant_id=%s WHERE id=%s",
                     self.tid, self.uid)

    def test_app_role_cannot_forge_verification(self):
        self._denied("UPDATE users SET verified_at=now() WHERE id=%s",
                     self.uid)

    # -- what the auth flows legitimately need stays -------------------

    def test_password_and_totp_columns_stay_writable(self):
        self._allowed("UPDATE users SET password_hash='y' WHERE id=%s",
                      self.uid)
        self._allowed("UPDATE users SET totp_secret=NULL WHERE id=%s",
                      self.uid)

    def test_member_lifecycle_stays(self):
        with _app_conn() as conn:
            uid = conn.execute(
                "INSERT INTO users (tenant_id, email, password_hash) "
                "VALUES (%s, %s, 'x') RETURNING id",
                (self.tid, f"cycle-{uuid.uuid4().hex[:8]}@example.dev"),
            ).fetchone()["id"]
            conn.execute("DELETE FROM users WHERE id=%s", (uid,))

    # -- the mobile / lockout twins of sessions --------------------------

    def _device_row(self):
        with _app_conn() as conn:
            return conn.execute(
                "INSERT INTO device_tokens (token_hash, user_id, tenant_id, "
                "expires_at) VALUES (%s, %s, %s, now() + interval '1 day') "
                "RETURNING id", (uuid.uuid4().hex, self.uid, self.tid),
            ).fetchone()["id"]

    def test_device_token_cannot_be_repointed_to_another_user(self):
        did = self._device_row()
        self._denied("UPDATE device_tokens SET user_id=%s WHERE id=%s",
                     self.uid, did)
        self._denied("UPDATE device_tokens SET tenant_id=%s WHERE id=%s",
                     self.tid, did)
        self._denied("UPDATE device_tokens SET token_hash='k' WHERE id=%s",
                     did)
        self._denied("DELETE FROM device_tokens WHERE id=%s", did)

    def test_device_token_lifecycle_columns_stay_writable(self):
        did = self._device_row()
        self._allowed("UPDATE device_tokens SET last_seen=now(), "
                      "expires_at=now() + interval '2 day' WHERE id=%s", did)
        self._allowed("UPDATE device_tokens SET push_token='t', "
                      "push_platform='fcm', push_updated=now(), "
                      "push_dead_at=NULL WHERE id=%s", did)
        self._allowed("UPDATE device_tokens SET revoked_at=now() WHERE id=%s",
                      did)

    def test_login_unlock_cannot_be_forged_or_moved(self):
        with _app_conn() as conn:
            lid = conn.execute(
                "INSERT INTO login_unlocks (user_id, token_hash, expires_at) "
                "VALUES (%s, %s, now() + interval '1 hour') RETURNING id",
                (self.uid, uuid.uuid4().hex)).fetchone()["id"]
        self._denied("UPDATE login_unlocks SET user_id=%s WHERE id=%s",
                     self.uid, lid)
        self._denied("UPDATE login_unlocks SET token_hash='k' WHERE id=%s",
                     lid)
        self._allowed("UPDATE login_unlocks SET used_at=now(), "
                      "unlocked_until=now(), consumed_at=now(), "
                      "refunded_at=NULL WHERE id=%s", lid)

    # -- operator-only tables the app role may only read ---------------

    def test_broadcast_and_ops_state_are_read_only_for_the_app(self):
        with _app_conn() as conn:
            conn.execute("SELECT message FROM broadcast")
            conn.execute("SELECT value FROM ops_state")
        self._denied("INSERT INTO broadcast (id, message) VALUES (1, 'pwned')")
        self._denied("UPDATE broadcast SET message='pwned'")
        self._denied("DELETE FROM broadcast")
        self._denied("INSERT INTO ops_state (key, value) VALUES ('x', 'y')")
        self._denied("UPDATE ops_state SET value='y'")
        self._denied("DELETE FROM ops_state")

    def test_push_vapid_key_cannot_be_swapped(self):
        self._denied("UPDATE push_vapid SET private_key='mine' WHERE id=1")
        self._denied("DELETE FROM push_vapid")

    def test_support_consent_window_cannot_be_extended(self):
        with _app_conn() as conn:
            cid = conn.execute(
                "INSERT INTO support_consents (tenant_id, granted_by, "
                "expires_at) VALUES (%s, %s, now() + interval '1 hour') "
                "RETURNING id", (self.tid, self.uid)).fetchone()["id"]
        self._denied("UPDATE support_consents SET expires_at=now() + "
                     "interval '1 year' WHERE id=%s", cid)
        self._denied("UPDATE support_consents SET tenant_id=%s WHERE id=%s",
                     self.tid, cid)
        self._denied("DELETE FROM support_consents WHERE id=%s", cid)
        self._allowed("UPDATE support_consents SET revoked_at=now() "
                      "WHERE id=%s", cid)


    # -- schema_migrations is migrate-only -----------------------------

    def test_schema_migrations_readable_not_writable(self):
        with _app_conn() as conn:
            conn.execute("SELECT COUNT(*) AS n FROM schema_migrations")
        self._denied("INSERT INTO schema_migrations (name) VALUES ('evil')")
        self._denied("DELETE FROM schema_migrations WHERE name='evil'")


# Every table WITHOUT row-level security, with the app role's write
# surface pinned: table-wide INSERT / UPDATE / DELETE flags, plus the
# column list when UPDATE is column-scoped. A no-RLS table is global to
# every tenant, so its app-role grant is a security decision that has to
# be made on purpose — a new one that is not listed here fails this test
# rather than silently inheriting the blanket "ALL TABLES" grant.
NO_RLS_APP_WRITES = {
    "admin_audit":            ("", ()),
    "admin_challenges":       ("", ()),
    "admin_credentials":      ("", ()),
    "admin_sessions":         ("", ()),
    "api_tokens":             ("I", ("last_used_at", "revoked_at")),
    "blocklist_meta":         ("", ()),
    "broadcast":              ("", ()),
    "deployments":            ("", ()),
    # elevated_at: the session-elevation stamp (migration 116) — the one
    # column /api/auth/elevate writes, so the app role may set it; and
    # elevated_passkey_id beside it (migration 120), which names the key
    # that proved the elevation. The id only ever SPARES a passkey from a
    # rotation's eviction, so a forged one costs an unevicted key, never
    # access — the same residual elevated_at already carries.
    "device_tokens":          ("I", ("elevated_at", "elevated_passkey_id",
                                     "expires_at", "last_seen", "push_dead_at",
                                     "push_platform", "push_token",
                                     "push_updated", "revoked_at")),
    "email_delivery_state":   ("", ()),
    # a widget token is minted (INSERT) and only ever stamped or revoked;
    # re-parenting one onto another device or user is what would let it
    # outlive the revocation of the phone that minted it
    "widget_tokens":          ("I", ("last_seen", "revoked_at")),
    "email_verifications":    ("I", ("used_at",)),
    "infra_metrics":          ("", ()),
    "invites":                ("ID", ("used_at",)),
    # the one no-RLS table the app role may write: cached logos from two
    # allowlisted hosts, behind a session, capped and holding no tenant data
    "logo_cache":             ("IUD", ()),
    "login_unlocks":          ("ID", ("consumed_at", "expires_at",
                                      "refunded_at", "unlocked_until",
                                      "used_at")),
    "ops_state":              ("", ()),
    "passkeys":               ("ID", ("last_used", "sign_count")),
    "password_resets":        ("I", ("used_at",)),
    # the cooling-off request: minted and stamped (cancelled / spent) by
    # the app role; never re-pointed at another user, never deleted
    "factor_resets":          ("I", ("cancel_reason", "cancelled_at",
                                     "consumed_at")),
    "plaid_item_ledger":      ("I", ()),
    "push_subscriptions":     ("IUD", ()),
    "push_vapid":             ("I", ()),
    "recipient_invites":      ("", ()),
    "recovery_codes":         ("", ("used_at",)),
    "schema_migrations":      ("", ()),
    "sessions":               ("ID", ("elevated_at", "elevated_passkey_id",
                                     "expires_at", "last_seen")),
    "signup_invites":         ("", ("used_at", "used_by_tenant")),
    # admin-only: a parked password hash and the token that hands an
    # address to whoever clicks it — the app role reads nothing here
    "signup_reclaims":        ("", ()),
    "spam_addresses_blocked": ("", ()),
    "spam_domains_blocked":   ("", ()),
    "spam_domains_public":    ("", ()),
    "spam_drops":             ("", ()),
    "spam_reputation_cache":  ("", ()),
    "support_consents":       ("I", ("revoked_at",)),
    "tenants":                ("", ()),
    "users":                  ("ID", ("password_hash", "totp_last_counter",
                                      "totp_secret")),
    "webauthn_challenges":    ("IUD", ()),
} | ext.gate.inventory().get('no_rls_app_writes', {})


class NoRlsTablesArePinnedTests(unittest.TestCase):
    """A no-RLS table the app role can write is a global write. Enumerate
    them from the catalog and compare to the pinned map — a new control
    plane table, or a widened grant, must show up here on purpose."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_every_no_rls_table_has_a_deliberate_app_write_surface(self):
        admin = tenancy.admin_connect()
        try:
            rows = admin.execute(
                """SELECT c.relname AS name,
                          has_table_privilege('oikonome_app', c.oid, 'INSERT') AS i,
                          has_table_privilege('oikonome_app', c.oid, 'UPDATE') AS u,
                          has_table_privilege('oikonome_app', c.oid, 'DELETE') AS d,
                          (SELECT array_agg(cp.column_name::text ORDER BY cp.column_name)
                             FROM information_schema.column_privileges cp
                            WHERE cp.table_schema = 'public'
                              AND cp.table_name = c.relname
                              AND cp.grantee = 'oikonome_app'
                              AND cp.privilege_type = 'UPDATE') AS ucols
                     FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND c.relkind = 'r'
                      AND NOT c.relrowsecurity
                    ORDER BY 1""").fetchall()
        finally:
            admin.close()
        actual = {}
        for r in rows:
            flags = ("I" if r["i"] else "") + ("U" if r["u"] else "") + \
                    ("D" if r["d"] else "")
            # column_privileges lists every column when the grant is
            # table-wide; only a column-scoped UPDATE is a pinned list
            cols = tuple(r["ucols"] or ()) if not r["u"] else ()
            actual[r["name"]] = (flags, cols)
        unknown = sorted(set(actual) - set(NO_RLS_APP_WRITES))
        self.assertEqual([], unknown,
                         f"no-RLS tables without a pinned app-role write "
                         f"surface (decide the grant in migrate.py, then pin "
                         f"it here): {unknown}")
        for name, want in NO_RLS_APP_WRITES.items():
            self.assertIn(name, actual, f"{name} vanished or grew RLS")
            self.assertEqual(want, actual[name],
                             f"{name}: app-role write surface drifted")


if __name__ == "__main__":
    unittest.main()
