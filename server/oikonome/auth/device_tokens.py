"""Mobile device tokens: long-lived bearer credentials for the native
apps. A token is 32 random bytes behind an ``oikd_`` prefix, stored
sha256-hashed (high-entropy — no KDF needed), and grants the SAME access
as a browser session for its user — unlike script tokens, which are
restricted to the import doors.

Lifecycle: minted by an already-authenticated session (the app signs in
normally first, so every login defense — rate limits, lockout, TOTP —
has already run), kept in the device's secure enclave behind a biometric
gate, and revocable individually from the web sessions panel. The idle
window slides like a session's but is longer (a phone that hasn't opened
the app in 90 days re-authenticates); there is no absolute cap because
the credential is device-bound and the revocation panel, password
change, and password reset all kill it.
"""

import datetime as dt
import hashlib
import secrets

PREFIX = "oikd_"
IDLE_TTL = dt.timedelta(days=90)

# per-user ceiling on LIVE tokens — a runaway client re-minting on every
# launch must not grow the table unbounded; a household owns far fewer
# devices than this
MAX_ACTIVE = 10


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mint(conn, user_id, tenant_id, device_name: str = "",
         platform: str = "") -> tuple[str, dict] | None:
    """Create a token; returns (plaintext, row), or None when the user is
    at the active-device ceiling. The plaintext is shown exactly once —
    only the hash is stored.

    A shared demonstration login (its second factor waived, so many people
    sign in with the same credentials) never refuses at the ceiling: it
    signs out its stalest device instead. Each visitor's device mints a
    token and walks away from it, so the ceiling fills quickly, and a
    refusal would show the next visitor a device picker full of other
    people's phones instead of the app."""
    # per-user serialization: the ceiling below is a count-then-insert,
    # and two concurrent mints (a retrying client, two browsers) could
    # otherwise both pass the check and overshoot MAX_ACTIVE. The
    # explicit transaction matters — control connections run autocommit,
    # where a bare xact-scoped advisory lock would release at statement
    # end and serialize nothing.
    with conn.transaction():
        conn.execute(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended('device_mint:' || %s::text, 0))", (user_id,))
        n = conn.execute(
            """SELECT count(*) AS n FROM device_tokens
               WHERE user_id = %s AND revoked_at IS NULL
                     AND expires_at > now()""", (user_id,)).fetchone()["n"]
        if n >= MAX_ACTIVE:
            shared = conn.execute(
                "SELECT second_factor_waived FROM users WHERE id = %s",
                (user_id,)).fetchone()
            if not (shared and shared["second_factor_waived"]):
                return None
            conn.execute(
                """UPDATE device_tokens SET revoked_at = now()
                   WHERE token_hash IN (
                       SELECT token_hash FROM device_tokens
                        WHERE user_id = %s AND revoked_at IS NULL
                              AND expires_at > now()
                        ORDER BY coalesce(last_seen, created_at), created_at
                        LIMIT %s)""", (user_id, n - MAX_ACTIVE + 1))
        token = PREFIX + secrets.token_hex(32)
        row = conn.execute(
            """INSERT INTO device_tokens (token_hash, user_id, tenant_id,
                                          device_name, platform, expires_at)
               VALUES (%s, %s, %s, %s, %s, now() + %s)
               RETURNING id, device_name, created_at""",
            (_h(token), user_id, tenant_id,
             (device_name or "mobile device").strip()[:80],
             (platform or "").strip()[:20], IDLE_TTL)).fetchone()
    return token, row


def lookup(conn, token: str) -> dict | None:
    """token → a session-shaped row (user_id, tenant_id, email, role,
    tenant_status, has_totp, device_id) or None. Same shape as
    sessions.lookup_session so the auth dependency's status checks apply
    unchanged. Stamps last_seen and slides the idle deadline (throttled
    to one write per minute)."""
    if not token or not token.startswith(PREFIX):
        return None
    row = conn.execute(
        """SELECT d.id AS device_id, d.user_id, d.tenant_id, u.email,
                  u.role, t.status AS tenant_status,
                  (u.totp_secret IS NOT NULL) AS has_totp,
                  u.second_factor_waived
           FROM device_tokens d JOIN users u ON u.id = d.user_id
                JOIN tenants t ON t.id = d.tenant_id
           WHERE d.token_hash = %s AND d.revoked_at IS NULL
                 AND d.expires_at > now()""", (_h(token),)).fetchone()
    if row is not None:
        conn.execute(
            """UPDATE device_tokens SET last_seen = now(),
                   expires_at = now() + %s
               WHERE token_hash = %s AND (last_seen IS NULL
                     OR last_seen < now() - interval '60 seconds')""",
            (IDLE_TTL, _h(token)))
    return row


def list_for_user(conn, user_id, current_token: str = "") -> list[dict]:
    """The user's live devices for the management API — surrogate `id`
    only, never the token hash."""
    return conn.execute(
        """SELECT id, device_name, platform, created_at, last_seen,
                  (token_hash = %s) AS current,
                  CASE WHEN push_token IS NULL THEN NULL
                       WHEN push_dead_at IS NOT NULL THEN 'dead'
                       ELSE 'on' END AS push_state
           FROM device_tokens
           WHERE user_id = %s AND revoked_at IS NULL AND expires_at > now()
           ORDER BY (token_hash = %s) DESC, created_at DESC""",
        (_h(current_token), user_id, _h(current_token))).fetchall()


def set_push(conn, user_id, device_id, token: str | None,
             platform: str = "") -> int:
    """Register (or with token=None clear) the device's native push token.
    Scoped to the owning user like revoke_by_id. A re-register clears
    push_dead_at: the platform handed the device a fresh token, so the
    old death verdict no longer applies."""
    if token is None:
        return conn.execute(
            """UPDATE device_tokens
               SET push_token = NULL, push_platform = NULL,
                   push_updated = now(), push_dead_at = NULL
               WHERE user_id = %s AND id = %s AND revoked_at IS NULL""",
            (user_id, device_id)).rowcount
    return conn.execute(
        """UPDATE device_tokens
           SET push_token = %s, push_platform = %s,
               push_updated = now(), push_dead_at = NULL
           WHERE user_id = %s AND id = %s AND revoked_at IS NULL""",
        (token, platform, user_id, device_id)).rowcount


def push_targets(conn, tenant_id) -> list[dict]:
    """Live push destinations for a tenant: every unrevoked, unexpired
    device with a registered token the push service hasn't declared
    dead. Control-plane read with an explicit tenant filter (this table
    has no RLS)."""
    return conn.execute(
        """SELECT push_token, push_platform FROM device_tokens
           WHERE tenant_id = %s AND revoked_at IS NULL
                 AND expires_at > now() AND push_token IS NOT NULL
                 AND push_dead_at IS NULL""", (tenant_id,)).fetchall()


def mark_push_dead(conn, tokens: list[str]) -> int:
    """The push service reported these tokens unroutable (uninstalled
    app, rotated token). Kept on the row, not deleted — a re-register
    from the device clears it."""
    if not tokens:
        return 0
    return conn.execute(
        """UPDATE device_tokens SET push_dead_at = now()
           WHERE push_token = ANY(%s) AND push_dead_at IS NULL""",
        (tokens,)).rowcount


def revoke_by_id(conn, user_id, device_id) -> int:
    """Revoke ONE device by surrogate id — scoped to the owning user, so
    an id from another account is a no-op."""
    return conn.execute(
        """UPDATE device_tokens SET revoked_at = now()
           WHERE user_id = %s AND id = %s AND revoked_at IS NULL""",
        (user_id, device_id)).rowcount


def revoke_all_for_user(conn, user_id, keep_device_id=None) -> int:
    """Credential rotation (password change/reset, email change): every
    device re-authenticates. revoked_at, not delete — keeps the audit row.

    `keep_device_id` spares ONE device — the one making the request, when a
    posture change (enrolling the first second factor) is driven from the
    phone itself: the person holding it just proved the new factor, so it
    is the one device that must stay signed in, exactly as the calling
    browser session survives the same change."""
    if keep_device_id is not None:
        return conn.execute(
            """UPDATE device_tokens SET revoked_at = now()
               WHERE user_id = %s AND revoked_at IS NULL AND id != %s""",
            (user_id, keep_device_id)).rowcount
    return conn.execute(
        """UPDATE device_tokens SET revoked_at = now()
           WHERE user_id = %s AND revoked_at IS NULL""",
        (user_id,)).rowcount


# ---- mint tickets: the cookie-free path from a login to a device token --
# The app signs in, then immediately exchanges that authentication for a
# device token. Riding the login's session COOKIE would assume the client
# can both see and re-send a Set-Cookie, which iOS does not allow: the
# header is not exposed to the app's fetch and the platform jar does not
# carry it either.
#
# So the login hands back an explicit one-shot ticket instead. It is a
# bearer credential like the passkey step-up ticket next door and is
# stored the same way: sha256 at rest, bound to the user it was minted
# for, single-use, and short-lived — it can do exactly one thing, and
# only for the minutes right after a full credential login.

MINT_TICKET_TTL = dt.timedelta(minutes=5)
MINT_TICKET_PREFIX = "oikm-"


def issue_mint_ticket(conn, user_id) -> str:
    """One-shot proof that THIS user just completed a full login."""
    ticket = MINT_TICKET_PREFIX + secrets.token_urlsafe(24)
    conn.execute(
        """INSERT INTO webauthn_challenges (purpose, user_id, challenge,
                                            expires_at)
           VALUES ('device-mint-ticket', %s, %s, %s)""",
        (user_id, _h(ticket),
         dt.datetime.now(dt.timezone.utc) + MINT_TICKET_TTL))
    # Sweep on the way in, the way the passkey challenges next door do.
    # Every login issues a ticket and only a phone ever spends one, so on
    # an instance where nobody signs in from the app these rows are pure
    # sediment — and the existing sweep only fires when someone touches a
    # passkey, which such an instance never does either.
    conn.execute("DELETE FROM webauthn_challenges WHERE expires_at < now()")
    return ticket


def revoke_mint_tickets(conn, user_id) -> int:
    """Burn every unspent mint ticket this user holds. A ticket is issued
    on EVERY login (browsers simply never spend theirs), so one is usually
    lying in a login response — DevTools, a proxy log, a shared machine —
    for its whole five-minute window. It is a bearer credential that
    stands in for a full login, so it has to die with the things a logout
    or a credential rotation kills: otherwise the very reset that revoked
    a hijacker's devices leaves them a ticket that plants a fresh one."""
    return conn.execute(
        """DELETE FROM webauthn_challenges
           WHERE purpose = 'device-mint-ticket' AND user_id = %s""",
        (user_id,)).rowcount


def redeem_mint_ticket(conn, ticket: str):
    """Return the user_id this ticket was minted for, or None. Deleting on
    read is what makes it single-use: a ticket replayed after the app has
    its token buys nothing."""
    if not ticket or not ticket.startswith(MINT_TICKET_PREFIX):
        return None
    row = conn.execute(
        """DELETE FROM webauthn_challenges
           WHERE purpose = 'device-mint-ticket' AND challenge = %s
             AND expires_at > now()
           RETURNING user_id""", (_h(ticket),)).fetchone()
    return row["user_id"] if row else None
