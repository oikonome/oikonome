"""Server-side sessions: opaque 256-bit token in a cookie, sha256 of it in
the DB (a DB leak alone can't forge sessions). Sessions & users are control
tables (no RLS) — reads/writes go through the app role directly.
"""

import datetime as dt
import hashlib
import secrets

# OWASP-style lifetimes: activity slides the
# idle deadline forward, so daily use never logs you out; a session
# untouched for IDLE_TTL dies, and nothing lives past ABSOLUTE_TTL from
# sign-in no matter how active.
IDLE_TTL = dt.timedelta(days=30)
ABSOLUTE_TTL = dt.timedelta(days=90)
SESSION_TTL = ABSOLUTE_TTL           # cookie max-age; server enforces the rest
COOKIE_NAME = "oikonome_session"


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn, user_id, tenant_id, user_agent: str = "",
                   ip: str = "") -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        """INSERT INTO sessions (token_hash, user_id, tenant_id, expires_at,
                                 user_agent, ip)
           VALUES (%s,%s,%s,%s,%s,%s)""",
        (_h(token), user_id, tenant_id,
         dt.datetime.now(dt.timezone.utc) + IDLE_TTL, user_agent[:300],
         ip[:64]))
    return token


def lookup_session(conn, token: str) -> dict | None:
    """token → the session-shaped row current_user hands every route.
    `session_id` is the row's surrogate id (never the token hash): the
    elevation window is stamped per SESSION, so the doors that read it
    need to name THIS row and not merely the user."""
    if not token:
        return None
    row = conn.execute(
        """SELECT s.user_id, s.tenant_id, u.email, u.role,
                  s.id AS session_id,
                  t.status AS tenant_status,
                  (u.totp_secret IS NOT NULL) AS has_totp,
                  u.second_factor_waived
           FROM sessions s JOIN users u ON u.id = s.user_id
                JOIN tenants t ON t.id = s.tenant_id
           WHERE s.token_hash = %s AND s.expires_at > now()""",
        (_h(token),)).fetchone()
    if row is not None:
        # coarse activity stamp + SLIDING idle renewal (never past the
        # absolute cap); throttled to one write per minute per session so
        # hot paths stay read-mostly
        conn.execute(
            """UPDATE sessions SET last_seen = now(),
                   expires_at = LEAST(now() + %s, created_at + %s)
               WHERE token_hash = %s AND (last_seen IS NULL
                     OR last_seen < now() - interval '60 seconds')""",
            (IDLE_TTL, ABSOLUTE_TTL, _h(token)))
    return row


def revoke_session(conn, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash = %s", (_h(token),))


def revoke_all(conn, user_id) -> int:
    return conn.execute("DELETE FROM sessions WHERE user_id = %s",
                        (user_id,)).rowcount


def list_sessions(conn, user_id, current_token: str = "") -> list[dict]:
    """The user's live sessions for the management API — surrogate `id`
    only, never the token hash."""
    return conn.execute(
        """SELECT id, created_at, last_seen, expires_at, user_agent, ip,
                  (token_hash = %s) AS current
           FROM sessions
           WHERE user_id = %s AND expires_at > now()
           ORDER BY (token_hash = %s) DESC, created_at DESC""",
        (_h(current_token), user_id, _h(current_token))).fetchall()


def revoke_all_others(conn, user_id, current_token: str) -> int:
    return conn.execute(
        "DELETE FROM sessions WHERE user_id = %s AND token_hash != %s",
        (user_id, _h(current_token))).rowcount


def revoke_by_id(conn, user_id, session_id) -> int:
    """Revoke ONE session by surrogate id — scoped to the owning user, so
    a session id from another account is a no-op."""
    return conn.execute(
        "DELETE FROM sessions WHERE user_id = %s AND id = %s",
        (user_id, session_id)).rowcount
