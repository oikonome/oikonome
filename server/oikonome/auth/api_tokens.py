"""Script tokens: long-lived bearer credentials for host-side
collector scripts (community scripts). A token is 32 random bytes behind
an ``oik_`` prefix, stored sha256-hashed (high-entropy — no KDF needed),
revocable, and restricted at the auth layer to the import doors. Minted
and revoked by the owner in Settings → Connections."""

import hashlib
import secrets

PREFIX = "oik_"


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mint(conn, tenant_id, user_id, name: str) -> tuple[str, dict]:
    """Create a token; returns (plaintext, row). The plaintext is shown
    exactly once — only the hash is stored."""
    token = PREFIX + secrets.token_hex(32)
    row = conn.execute(
        """INSERT INTO api_tokens (token_hash, tenant_id, created_by, name)
           VALUES (%s, %s, %s, %s) RETURNING id, name, created_at""",
        (_h(token), tenant_id, user_id, (name or "script").strip()[:80]),
    ).fetchone()
    return token, row


def lookup(conn, token: str) -> dict | None:
    """token → {id, tenant_id, name, created_by} or None. Stamps
    last_used_at (throttled) so Settings can show liveness."""
    if not token or not token.startswith(PREFIX):
        return None
    row = conn.execute(
        """SELECT id, tenant_id, name, created_by FROM api_tokens
           WHERE token_hash = %s AND revoked_at IS NULL""",
        (_h(token),)).fetchone()
    if row is not None:
        conn.execute(
            """UPDATE api_tokens SET last_used_at = now()
               WHERE token_hash = %s AND (last_used_at IS NULL
                     OR last_used_at < now() - interval '60 seconds')""",
            (_h(token),))
    return row


def list_for_tenant(conn, tenant_id) -> list[dict]:
    return conn.execute(
        """SELECT id, name, created_at, last_used_at, revoked_at
           FROM api_tokens WHERE tenant_id = %s
           ORDER BY created_at DESC""", (tenant_id,)).fetchall()


def revoke(conn, tenant_id, token_id) -> bool:
    row = conn.execute(
        """UPDATE api_tokens SET revoked_at = now()
           WHERE tenant_id = %s AND id = %s AND revoked_at IS NULL
           RETURNING id""", (tenant_id, token_id)).fetchone()
    return row is not None
