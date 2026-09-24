"""Widget tokens: the credential a phone's home-screen widget polls with.

A widget is redrawn by the OS every half hour with nobody holding the
phone, so it cannot use the device token — that one lives behind the
biometric gate on purpose. It gets a lesser credential: 32 random bytes
behind an ``oikw_`` prefix, sha256 at rest, a CHILD of one device row,
and honoured at exactly one door (``GET /api/today/glance`` — today's
verdict and allowance, never a balance or a transaction).

It has no lifetime of its own. The lookup joins through its parent
device row and requires that row to be live, so revoking the device from
the sessions panel, a password change or reset, and the device idling
out all end the widget in the same moment. Polling never slides the
parent's idle deadline: a widget on a drawer-bound phone must not keep a
90-day credential alive by itself. One live widget token per device —
minting again replaces the previous one."""

import hashlib
import secrets

PREFIX = "oikw_"


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mint(conn, user_id, tenant_id, device_id) -> str:
    """Create the device's widget token, replacing any earlier one.
    Returns the plaintext, shown exactly once — only the hash is stored."""
    token = PREFIX + secrets.token_hex(32)
    with conn.transaction():
        conn.execute(
            """UPDATE widget_tokens SET revoked_at = now()
               WHERE device_id = %s AND revoked_at IS NULL""", (device_id,))
        conn.execute(
            """INSERT INTO widget_tokens (token_hash, device_id, user_id,
                                          tenant_id)
               VALUES (%s, %s, %s, %s)""",
            (_h(token), device_id, user_id, tenant_id))
    return token


def lookup(conn, token: str) -> dict | None:
    """token → a session-shaped row (user_id, tenant_id, email, role,
    tenant_status, has_totp, device_id, widget=True) or None. The parent
    device must be unrevoked and unexpired: the widget has no standing of
    its own. Stamps the widget's last_seen (throttled) and nothing on the
    parent."""
    if not token or not token.startswith(PREFIX):
        return None
    row = conn.execute(
        """SELECT w.device_id, w.user_id, w.tenant_id, u.email, u.role,
                  t.status AS tenant_status,
                  (u.totp_secret IS NOT NULL) AS has_totp,
                  u.second_factor_waived
           FROM widget_tokens w
                JOIN device_tokens d ON d.id = w.device_id
                JOIN users u ON u.id = w.user_id
                JOIN tenants t ON t.id = w.tenant_id
           WHERE w.token_hash = %s AND w.revoked_at IS NULL
                 AND d.revoked_at IS NULL AND d.expires_at > now()""",
        (_h(token),)).fetchone()
    if row is None:
        return None
    conn.execute(
        """UPDATE widget_tokens SET last_seen = now()
           WHERE token_hash = %s AND (last_seen IS NULL
                 OR last_seen < now() - interval '60 seconds')""",
        (_h(token),))
    out = dict(row)
    out["widget"] = True
    return out

