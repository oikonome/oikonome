"""Apply schema + migrations, bootstrap the app role. Idempotent.

Usage: python -m oikonome.db.migrate  (env: OIKONOME_ADMIN_DSN, OIKONOME_DSN)

Order: (1) advisory lock so concurrent app starts don't race, (2) ensure the
non-superuser `oikonome_app` role exists with least privilege and its
password matches OIKONOME_APP_PASSWORD (rotations take effect), (3) schema.sql
(idempotent DDL), (4) numbered migrations from migrations/ not yet recorded
in schema_migrations, (5) re-grant on all tables (new tables need grants).
"""

import os
import re
import sys
from pathlib import Path

import psycopg

from .tenancy import admin_connect

HERE = Path(__file__).parent
LOCK_KEY = 74210091  # arbitrary constant, shared by all oikonome processes


def _manifest_subject(version: str) -> str | None:
    """The real 'what changed' for a version — its commit subject from the
    build manifest (server/oikonome/version_history.tsv, the same file the
    admin console's Version-history panel reads). This is why the deploy
    'what changed' text is accurate per version instead of echoing a
    hand-set OIKONOME_VERSION_NOTE that goes stale (every deploy re-stamps
    the last string typed, so a whole run of versions ends up describing
    one of them). Returns None when the manifest lacks the version
    (unreadable file, or an image built before the version was numbered),
    so the caller falls back to the env note."""
    path = HERE.parent / "version_history.tsv"
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 4 and parts[0] == version:
                    return parts[3] or None
    except OSError:
        return None
    return None


def _deploy_summary(version: str) -> str | None:
    """What to stamp as this deployment's 'what changed'. The manifest is
    authoritative; the env note is honored ONLY when it was captured for
    this exact version (OIKONOME_VERSION_NOTE_FOR, stamped by install.sh
    next to the note). A note without a matching version marker is stale —
    a deploy path that bumps OIKONOME_VERSION without
    re-running install.sh (a compose-build deploy, say) re-serves the last
    note ever typed, so two different versions claim the same change. An
    honest blank beats a wrong answer."""
    subject = _manifest_subject(version)
    if subject:
        return subject
    note = os.environ.get("OIKONOME_VERSION_NOTE", "").strip()
    note_for = os.environ.get("OIKONOME_VERSION_NOTE_FOR", "").strip()
    if note and note_for == version:
        return note
    return None


APP_ROLE_SQL = """
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'oikonome_app') THEN
        CREATE ROLE oikonome_app LOGIN PASSWORD '{pw}';
    ELSE
        -- re-assert on every run: create-if-absent alone meant a rotated
        -- OIKONOME_APP_PASSWORD in .env never took effect on an existing
        -- database. Inside the DO block the password also stays out
        -- of statement logs.
        ALTER ROLE oikonome_app WITH LOGIN PASSWORD '{pw}';
    END IF;
END $$;
"""

# the RUNTIME admin role — what app/worker get instead of the
# postgres superuser. BYPASSRLS + DML is everything the runtime admin
# paths need (cross-tenant reads, tenant/user lifecycle, control-plane
# writes the app role is column-revoked from, pg_dump); it deliberately
# CANNOT do DDL beyond its grants, create roles, or reach superuser —
# those stay in the one-shot migrate container with the real superuser.
# {attrs} is "BYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE" on a real
# superuser connection and EMPTY on the managed-PG fallback — merely
# *mentioning* NOSUPERUSER in ALTER counts as "changing the SUPERUSER
# attribute" and needs superuser (managed-provider CREATEROLE admins fail
# on the ALTER even though CREATE defaults to all the NO-attributes).
ADMIN_ROLE_SQL = """
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'oikonome_admin') THEN
        CREATE ROLE oikonome_admin LOGIN PASSWORD '{pw}' {attrs};
    ELSE
        ALTER ROLE oikonome_admin WITH LOGIN PASSWORD '{pw}' {attrs};
    END IF;
END $$;
"""

# Managed-PG fallback: granting the BYPASSRLS attribute needs a
# real superuser, which managed providers don't give you.
# Equivalent access without the attribute: make oikonome_admin OWN the
# tables — an owner bypasses RLS unless the table is FORCEd (ours aren't),
# and CREATEROLE admins can grant themselves the role to hand ownership
# over. Tradeoff (documented): as owner, oikonome_admin could ALTER/DROP
# its own tables on managed PG — still no superuser, no role powers, no
# new objects. Re-run every boot so new migration tables transfer too.
ADMIN_OWNERSHIP_SQL = """
GRANT oikonome_admin TO CURRENT_USER;
-- receiving ownership requires CREATE on the schema — part
-- of the documented fallback tradeoff on managed providers
GRANT CREATE ON SCHEMA public TO oikonome_admin;
DO $$ DECLARE t text; BEGIN
    FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public'
    LOOP
        EXECUTE format('ALTER TABLE %I OWNER TO oikonome_admin', t);
    END LOOP;
    FOR t IN SELECT sequencename FROM pg_sequences
             WHERE schemaname = 'public'
    LOOP
        EXECUTE format('ALTER SEQUENCE %I OWNER TO oikonome_admin', t);
    END LOOP;
END $$;
"""

def _local_host(host: str | None) -> bool:
    """Is the connection local? Unix sockets report the socket directory
    (a path); TCP reports the host name/address given in the DSN."""
    return (host is None or host.startswith("/")
            or host in ("127.0.0.1", "::1", "localhost"))


# A migration whose FIRST line is this marker runs outside a transaction,
# one statement at a time. CREATE INDEX CONCURRENTLY is why the marker
# exists: it is illegal inside a transaction block, and an ordinary index
# build holds ACCESS EXCLUSIVE on the table for its whole duration — on a
# large ledger that is an outage, where a concurrent build only takes
# SHARE UPDATE EXCLUSIVE and lets the household keep working.
# The price: a failure part-way leaves the migration applied in part and
# NOT recorded, so every statement in such a file must be idempotent
# (IF NOT EXISTS / drop-then-create) and safe to re-run.
NO_TRANSACTION = "-- oikonome: no-transaction"

# Inside a no-transaction migration a statement may be preceded by
#     -- oikonome: skip-unless <query>
# and then runs only if <query> returns a row. It buys back the
# conditionality a DO block would have given (CONCURRENTLY cannot live in
# one) without a blanket try/except, which would swallow real errors.
SKIP_UNLESS = re.compile(r"^[ \t]*--[ \t]*oikonome:[ \t]*skip-unless[ \t]+(.+)$",
                         re.MULTILINE)

_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


def _sql_statements(sql: str) -> list[str]:
    """Split a migration into top-level statements, each keeping the
    comments that precede it (that is where its directives live).

    A ';' inside a string literal, a comment or a dollar-quoted body
    ($$ … $$ — every DO block we write) does not split. Chunks holding
    nothing but comments are dropped. Only the no-transaction path needs
    this; ordinary migrations are still handed to the server whole.
    """
    out: list[str] = []
    raw: list[str] = []      # the text to send, comments included
    code: list[str] = []     # the same minus comments, to spot empty chunks
    i, n, tag = 0, len(sql), None
    while i < n:
        ch = sql[i]
        if tag is not None:                      # inside $tag$ … $tag$
            if sql.startswith(tag, i):
                raw.append(tag)
                code.append(tag)
                i += len(tag)
                tag = None
            else:
                raw.append(ch)
                code.append(ch)
                i += 1
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j + 1
            raw.append(sql[i:j])
            i = j
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i)
            j = n if j < 0 else j + 2
            raw.append(sql[i:j])
            i = j
            continue
        if ch == "'":
            j = i + 1
            while j < n and (sql[j] != "'" or sql.startswith("''", j)):
                j += 2 if sql.startswith("''", j) else 1
            j = min(j + 1, n)
            raw.append(sql[i:j])
            code.append(sql[i:j])
            i = j
            continue
        m = _DOLLAR_TAG.match(sql, i)
        if m:
            tag = m.group(0)
            raw.append(tag)
            code.append(tag)
            i += len(tag)
            continue
        if ch == ";":
            if "".join(code).strip():
                out.append("".join(raw))
            raw, code = [], []
            i += 1
            continue
        raw.append(ch)
        code.append(ch)
        i += 1
    if "".join(code).strip():
        out.append("".join(raw))
    return out


def _apply_no_transaction(conn: psycopg.Connection, sql: str) -> None:
    """Run a NO_TRANSACTION migration statement by statement on the
    autocommit connection. A single-statement simple query is not a
    transaction block, which is exactly what makes CONCURRENTLY legal
    here — sending the file whole would put it back inside one."""
    for stmt in _sql_statements(sql):
        guard = SKIP_UNLESS.search(stmt)
        if guard is not None:
            if conn.execute(guard.group(1).strip().rstrip(";")).fetchone() is None:
                continue
        conn.execute(stmt)


def _apply_dir(conn, mig_dir, done: set, applied: list) -> None:
    for f in sorted(mig_dir.glob("*.sql")) if mig_dir.exists() else []:
        if f.name in done:
            continue
        sql = f.read_text()
        if sql.lstrip().startswith(NO_TRANSACTION):
            # recorded only once every statement landed; a failed run
            # leaves nothing recorded and is fixed by re-running
            # migrate, which is why these files are idempotent
            _apply_no_transaction(conn, sql)
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)",
                         (f.name,))
        else:
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (name) VALUES (%s)",
                    (f.name,))
        applied.append(f.name)


def run(admin_dsn: str | None = None) -> list[str]:
    applied = []
    conn = admin_connect(admin_dsn)
    conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
    try:
        pw = os.environ.get("OIKONOME_APP_PASSWORD")
        if pw is None:
            # the 'apppass' fallback exists for LOCAL dev only (it matches
            # tenancy.APP_DSN's localhost default; compose marks the var
            # required, so containers never land here). ALTERing the app
            # role on a non-local database to a publicly known password
            # is never right — refuse instead of doing it silently.
            if not _local_host(conn.info.host):
                print("OIKONOME_APP_PASSWORD is not set and the database "
                      f"host ({conn.info.host!r}) is not local — refusing "
                      "to default the app role password to 'apppass'. Set "
                      "OIKONOME_APP_PASSWORD and re-run.", file=sys.stderr)
                raise SystemExit(1)
            pw = "apppass"
        conn.execute(APP_ROLE_SQL.format(pw=pw.replace("'", "''")))
        apw = os.environ.get("OIKONOME_ADMIN_PASSWORD")
        if apw is None:
            # same doctrine as the app role above: 'adminpass' is a LOCAL
            # dev fallback only — never silently weaken a remote database
            if not _local_host(conn.info.host):
                print("OIKONOME_ADMIN_PASSWORD is not set and the database "
                      f"host ({conn.info.host!r}) is not local — refusing "
                      "to default the runtime-admin role password. Set "
                      "OIKONOME_ADMIN_PASSWORD and re-run.", file=sys.stderr)
                raise SystemExit(1)
            apw = "adminpass"
        admin_owns = False
        try:
            conn.execute(ADMIN_ROLE_SQL.format(
                pw=apw.replace("'", "''"),
                attrs="BYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE"))
        except psycopg.errors.InsufficientPrivilege:
            # managed PG: no superuser to grant BYPASSRLS — fall back to
            # table ownership (see ADMIN_OWNERSHIP_SQL)
            conn.execute(ADMIN_ROLE_SQL.format(
                pw=apw.replace("'", "''"), attrs=""))
            admin_owns = True
        conn.execute((HERE / "schema.sql").read_text())
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        done = {r["name"] for r in conn.execute(
            "SELECT name FROM schema_migrations").fetchall()}
        # the product's own migrations first, then each installed add-on's
        # directory; one ledger records them all by file name, so an add-on
        # that ships a file the database already saw applies nothing
        from .. import ext
        for mig_dir in [HERE / "migrations", *ext.migration_dirs()]:
            _apply_dir(conn, mig_dir, done, applied)
        # deployment history (admin console): stamp each version the FIRST
        # time migrate sees it — re-running the same version (compose
        # restarts re-run migrate) never duplicates the row
        version = os.environ.get("OIKONOME_VERSION", "dev")
        summary = _deploy_summary(version)
        last = conn.execute("SELECT version FROM deployments "
                            "ORDER BY id DESC LIMIT 1").fetchone()
        if last is None or last["version"] != version:
            conn.execute(
                "INSERT INTO deployments (version, migrations, summary) "
                "VALUES (%s, %s, %s)", (version, len(applied), summary))
        # Once a later image's manifest knows a version's real subject,
        # correct any row whose summary disagrees. Idempotent; versions the manifest doesn't
        # know (e.g. 'dev') are left alone.
        for row in conn.execute(
                "SELECT id, version, summary FROM deployments").fetchall():
            real = _manifest_subject(row["version"])
            if real and row["summary"] != real:
                conn.execute("UPDATE deployments SET summary = %s "
                             "WHERE id = %s", (real, row["id"]))
        conn.execute(
            "GRANT USAGE ON SCHEMA public TO oikonome_app;"
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public"
            " TO oikonome_app;"
            "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO oikonome_app;"
            "REVOKE INSERT, UPDATE, DELETE ON tenants FROM oikonome_app;"
            # control-plane tables have no RLS, so an injected
            # UPDATE is global. The app's whole legitimate UPDATE surface
            # on users is password_hash (change/reset) + totp_secret
            # (enroll/disable) — column-scope the grant so email rewrite,
            # role promotion, cross-tenant re-parenting and verified_at
            # forgery are permission errors even under SQL injection.
            # Residual, accepted: password_hash itself must stay writable
            # (a GUC-gated trigger wouldn't help — injected SQL can call
            # set_config() in the same query); closing that means
            # SECURITY DEFINER auth functions, deferred as invasive.
            "REVOKE UPDATE ON users FROM oikonome_app;"
            # totp_last_counter is the TOTP replay defense —
            # the app stamps it on each accepted code; a monotonically
            # increasing counter is not an escalation surface
            "GRANT UPDATE (password_hash, totp_secret, totp_last_counter)"
            " ON users TO oikonome_app;"
            # same column-scoping for the other control-plane
            # tables (no RLS ⇒ an injected write is global). The app's
            # whole legitimate UPDATE surface is timestamps/counters —
            # scope to exactly those columns so an injected UPDATE can't
            # re-point sessions.user_id (session takeover), rewrite
            # api_tokens.token_hash, or promote invites.role. DELETE is
            # revoked where no code path deletes (api_tokens are revoked
            # via revoked_at; password_resets via used_at).
            # Residual, accepted (the doctrine): INSERT into
            # sessions/api_tokens must stay grantable — login and token
            # mint run as the app role — so injected SQL could still
            # forge rows; closing that means SECURITY DEFINER auth
            # functions, deferred as invasive.
            # The Plaid Item ledger is APPEND-ONLY: it counts every Item
            # ever created and must be monotonic, so the app role may only
            # insert and read.
            "REVOKE UPDATE, DELETE ON plaid_item_ledger FROM oikonome_app;"
            "REVOKE UPDATE, DELETE ON api_tokens FROM oikonome_app;"
            "GRANT UPDATE (last_used_at, revoked_at) ON api_tokens"
            " TO oikonome_app;"
            "REVOKE UPDATE, DELETE ON password_resets FROM oikonome_app;"
            "GRANT UPDATE (used_at) ON password_resets TO oikonome_app;"
            # the cooling-off factor reset is stamped, never re-pointed: an
            # injected UPDATE must not move a landed request onto another
            # user_id or pull lands_at forward
            "REVOKE UPDATE, DELETE ON factor_resets FROM oikonome_app;"
            "GRANT UPDATE (cancelled_at, cancel_reason, consumed_at) "
            "ON factor_resets TO oikonome_app;"
            "REVOKE UPDATE ON invites FROM oikonome_app;"
            "GRANT UPDATE (used_at) ON invites TO oikonome_app;"
            # elevated_at is the "sudo" window stamp (migration 116):
            # a timestamp the app writes on its own row after a proof, and
            # elevated_passkey_id (migration 120) says which passkey the
            # proof was. Residual, accepted under the doctrine above:
            # injected SQL that can already forge a session row can stamp
            # one too — and the id only ever SPARES a key from eviction,
            # so a forged one costs at most an unevicted passkey.
            "REVOKE UPDATE ON sessions FROM oikonome_app;"
            "GRANT UPDATE (last_seen, expires_at, elevated_at,"
            " elevated_passkey_id) ON sessions"
            " TO oikonome_app;"
            "REVOKE UPDATE ON passkeys FROM oikonome_app;"
            "GRANT UPDATE (sign_count, last_used) ON passkeys"
            " TO oikonome_app;"
            # invites are minted by the OPERATOR (admin role via
            # the CLI) — the app role only reads and burns them, so
            # injected SQL can't mint itself a signup invite.
            # email_verifications mirrors password_resets: the app mints
            # tokens (INSERT) but can only burn, never rewrite or delete.
            # signup_reclaims is ADMIN-ONLY: it holds a parked password
            # hash and the token that hands an address to whoever clicks.
            # Signup and the reclaim door both already run on the admin
            # connection (they create tenants), so the app role needs
            # nothing here — not even SELECT.
            "REVOKE ALL ON signup_reclaims FROM oikonome_app;"
            "REVOKE INSERT, UPDATE, DELETE ON signup_invites"
            " FROM oikonome_app;"
            "GRANT UPDATE (used_at, used_by_tenant) ON signup_invites"
            " TO oikonome_app;"
            "REVOKE UPDATE, DELETE ON email_verifications"
            " FROM oikonome_app;"
            "GRANT UPDATE (used_at) ON email_verifications TO oikonome_app;"
            # Spam-defense tables (migration 060): the check + refresh run on
            # the ADMIN connection only, so the app role gets nothing — an
            # injected app-role write can't seed a known-good domain to slip a
            # spam address past the intake gate, nor scrub the drop log.
            "REVOKE ALL ON spam_domains_public, spam_domains_blocked,"
            " spam_addresses_blocked, spam_reputation_cache, blocklist_meta,"
            " spam_drops FROM oikonome_app;"
            # email_delivery_state (migration 073) is READ by the
            # app role and WRITTEN only by admin. The asymmetry is the
            # point: a row here PAUSES an address's scheduled mail, so an
            # injected app-role INSERT would be a way to silence another
            # household's verdict email — while reading one exposes nothing
            # the app role cannot already see on `users`. Reads are on the
            # hot path (/api/me, every page load) and must come off the
            # pooled app connection; admin_connect opens a fresh unpooled
            # connection per call and has no business there.
            "REVOKE ALL ON email_delivery_state FROM oikonome_app;"
            "GRANT SELECT ON email_delivery_state TO oikonome_app;"
            # recipient_invites (migration 074) is admin-only in
            # BOTH directions — the app role gets nothing at all. An
            # `accepted_at` row is what makes a household's balances and
            # spending flow to an address, so an injected app-role INSERT
            # would be an exfiltration channel that needs no session and no
            # password; and the read side is one query on the Settings page,
            # nowhere near a hot path, so there is no cost to keeping it on
            # admin_connect too. `email_recipients` in tenant_settings stays
            # app-writable: after this, writing config PROPOSES a recipient
            # and only an accepted invite enrols one.
            "REVOKE ALL ON recipient_invites FROM oikonome_app;"
            # logo_cache (migration 099) is a global, tenant-free store of
            # fetched merchant logos, and the app role reads AND writes it.
            # The write grant is deliberate: /api/logo is a per-ledger-row
            # hot path that must run on the pooled app connection, it needs
            # DELETE to hold the cache to its caps, and the exposure is
            # bounded — the URL must be one of Plaid's two logo hosts, the
            # route requires a session, the body must be a PNG under
            # 512 KB, and the table holds no tenant data. The residual (an
            # injected app-role write could plant an image other households
            # would then be served) is worth less than a fresh superuser
            # connection per row.
            "GRANT SELECT, INSERT, UPDATE, DELETE ON logo_cache"
            " TO oikonome_app;"
            # schema_migrations is written by migrate alone (admin role);
            # the app only reads it (doctor, /api/setup-status)
            "REVOKE INSERT, UPDATE, DELETE ON schema_migrations"
            " FROM oikonome_app;"
            # the admin console runs entirely on admin_connect —
            # the app role gets NOTHING, so injected SQL can't forge an
            # operator session or scrub the audit trail
            "REVOKE ALL ON admin_sessions, admin_audit FROM oikonome_app;"
            # same for the operator passkeys and their challenges /
            # step-up tickets — an injected app-role write must not be able
            # to enrol a console credential or mint a step-up ticket.
            "REVOKE ALL ON admin_credentials, admin_challenges"
            " FROM oikonome_app;"
            # recovery codes are MINTED server-side on the admin
            # connection (enrollment); the app role reads + burns (used_at)
            # at login but can't INSERT/DELETE its own codes.
            "REVOKE INSERT, DELETE ON recovery_codes FROM oikonome_app;"

            # deployment history is written by migrate alone (admin role);
            # the app role only reads (console renders on admin anyway)
            "REVOKE INSERT, UPDATE, DELETE ON deployments FROM oikonome_app;"
            # infra metrics are written by the worker sampler and
            # read by the console — both on the admin connection. The app
            # role must not be able to fake or scrub the stress history.
            "REVOKE INSERT, UPDATE, DELETE ON infra_metrics"
            " FROM oikonome_app;"
            "REVOKE UPDATE ON recovery_codes FROM oikonome_app;"
            "GRANT UPDATE (used_at) ON recovery_codes TO oikonome_app;"
            # device_tokens are the mobile twin of sessions — same access
            # as a browser session, so the same shape of grant: the app
            # touches idle deadlines, the native push routing and the
            # revocation stamp; an injected UPDATE must not be able to
            # re-point user_id/tenant_id (session takeover) or rewrite
            # token_hash. Nothing deletes a device row (revoked_at keeps
            # the audit trail).
            "REVOKE UPDATE, DELETE ON device_tokens FROM oikonome_app;"
            "GRANT UPDATE (last_seen, expires_at, push_token, push_platform,"
            " push_updated, push_dead_at, revoked_at, elevated_at,"
            " elevated_passkey_id)"
            " ON device_tokens TO oikonome_app;"
            # login_unlocks is the lockout-exemption table: an injected
            # UPDATE that re-parents a row to another user_id, or rewrites
            # token_hash to a known value, hands the attacker a working
            # bypass link. The app mints (INSERT), burns older links and
            # follows/consumes/refunds — timestamps only. (expires_at stays
            # writable: whoever can UPDATE it can already INSERT a fresh
            # row, so scoping it out would protect nothing.)
            "REVOKE UPDATE ON login_unlocks FROM oikonome_app;"
            "GRANT UPDATE (used_at, unlocked_until, consumed_at, refunded_at,"
            " expires_at) ON login_unlocks TO oikonome_app;"
            # broadcast is the site-wide banner every signed-in user's
            # /api/me renders; it is set/cleared by the console and the
            # maintenance-reboot choreography, both on admin. The app
            # role only reads it — an injected write must not be able to
            # put arbitrary text (or a countdown) in front of every user.
            "REVOKE INSERT, UPDATE, DELETE ON broadcast FROM oikonome_app;"
            # ops_state (capacity alert transitions) is written by the
            # watcher on admin; the app has no business writing it.
            "REVOKE INSERT, UPDATE, DELETE ON ops_state FROM oikonome_app;"
            # push_vapid holds the instance's web-push signing key. The
            # app role bootstraps it (INSERT … ON CONFLICT DO NOTHING on
            # first use — a singleton, so once present an INSERT is a
            # no-op) and reads it; only rotate-master-key rewrites it, on
            # admin. Revoking UPDATE means injected SQL cannot swap in a
            # key it knows and sign pushes as this instance.
            "REVOKE UPDATE, DELETE ON push_vapid FROM oikonome_app;"
            # support_consents: the tenant grants (INSERT) and revokes
            # (revoked_at) support access; expires_at / tenant_id /
            # granted_by are set once and never rewritten, so an injected
            # UPDATE cannot extend a window or move a consent onto another
            # tenant. Nothing deletes a consent (audit).
            "REVOKE UPDATE, DELETE ON support_consents FROM oikonome_app;"
            "GRANT UPDATE (revoked_at) ON support_consents TO oikonome_app;"
            # push_subscriptions and webauthn_challenges keep full DML: the
            # subscribe upsert legitimately re-parents a row (same browser,
            # proof of possession) and challenges are minted/burned by the
            # app; neither carries anything an injected write could turn
            # into access.
            # the runtime-admin role gets full DML everywhere
            # (BYPASSRLS attribute rides on the role itself) — but none of
            # the app-role REVOKEs above apply to it, and it gets no DDL.
            "GRANT USAGE ON SCHEMA public TO oikonome_admin;"
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA"
            " public TO oikonome_admin;"
            "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public"
            " TO oikonome_admin;")
        # an installed add-on narrows the app role's reach on its own tables
        from .. import ext
        for _sql in ext.grant_sql():
            conn.execute(_sql)
        if admin_owns:
            conn.execute(ADMIN_OWNERSHIP_SQL)
        _encrypt_totp_secrets(conn)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
        conn.close()
    return applied


def _encrypt_totp_secrets(conn) -> int:
    """Encrypt any control-plane TOTP seed still stored in plaintext.

    Rows enrolled on a build without seed encryption keep their secret in
    the clear; this sweeps them under the master key. Runs on every boot
    (migrate precedes serve), is a no-op once swept, and cannot be a .sql
    migration because Fernet isn't SQL. Without a master key (dev) it
    leaves rows alone; decrypt_cp passes them through."""
    from . import crypto
    if not crypto.master_key():
        return 0
    rows = conn.execute(
        "SELECT id, totp_secret FROM users WHERE totp_secret IS NOT NULL "
        "AND totp_secret NOT LIKE %s", (crypto.CP_PREFIX + "%",)).fetchall()
    for r in rows:
        conn.execute("UPDATE users SET totp_secret=%s WHERE id=%s",
                     (crypto.encrypt_cp(r["totp_secret"]), r["id"]))
    return len(rows)


# config keys holding tenant secrets (mirror connections_bundle._SECRET_KEYS)
_CFG_SECRET_KEYS = ("plaid_secret", "mx_api_key", "llm_api_key",
                    "smtp_password")


def reencrypt_tenant_secrets() -> int:
    """A box run WITHOUT a master key, then given one, keeps its
    already-stored TENANT secrets in plaintext — the
    TOTP sweep above only covers control-plane seeds. Re-encrypt the
    untagged per-tenant secrets (items.access_token + the config secret
    keys) under each tenant's own key. Runs on a tenant-scoped (app-role)
    connection so the ambient tenant key + RLS apply — hence it lives in
    the worker's startup, NOT the migrate one-shot (which has only the
    superuser DSN and would grab the wrong tenant_keys row under
    BYPASSRLS). Idempotent: skips already-tagged rows, a no-op once swept
    and on every dev box without a master key."""
    from . import crypto, tenancy
    if not crypto.master_key():
        return 0
    admin = tenancy.admin_connect()
    try:
        tids = [str(r["id"]) for r in
                admin.execute("SELECT id FROM tenants").fetchall()]
    finally:
        admin.close()
    from ..engine import budget
    swept = 0
    for tid in tids:
        conn = tenancy.tenant_connect(tid)
        try:
            rows = conn.execute(
                "SELECT id, access_token FROM items WHERE access_token "
                "IS NOT NULL AND access_token NOT LIKE %s",
                (crypto.PREFIX + "%",)).fetchall()
            for r in rows:
                conn.execute(
                    "UPDATE items SET access_token=%s WHERE id=%s",
                    (crypto.encrypt(conn, r["access_token"]), r["id"]))
                swept += 1
            # per-tenant `tenant_connect` above, so config_txn's unqualified
            # FOR UPDATE is RLS-scoped to this tenant's row (it must never be
            # run on an admin_connect — see config_txn's docstring)
            # ONE load for the probe. Calling load_config per key would
            # re-read a whole settings document each time — and load_config
            # is not even read-only: it seed-persists, so the "is there
            # anything to do here" check would itself write, repeatedly.
            probe = budget.load_config(conn)
            if any(v and not str(v).startswith(crypto.PREFIX)
                   for v in (probe.get(k) for k in _CFG_SECRET_KEYS)):
                with budget.config_txn(conn) as cfg:
                    for k in _CFG_SECRET_KEYS:
                        v = cfg.get(k)
                        if v and not str(v).startswith(crypto.PREFIX):
                            cfg[k] = crypto.encrypt(conn, v)
                            swept += 1
        except Exception:                                   # noqa: BLE001
            # one tenant's failure must not block the rest — log and move on
            import logging
            logging.getLogger("oikonome.db").warning(
                "secret re-encryption failed for tenant %s", tid,
                exc_info=True)
        finally:
            conn.close()
    return swept


if __name__ == "__main__":
    names = run()
    print(f"migrations applied: {names or 'none (schema current)'}")
