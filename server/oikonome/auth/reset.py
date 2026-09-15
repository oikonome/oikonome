"""Password-reset tokens: one-time, 1-hour, opaque 256-bit token in the
link, sha256 of it in the DB (same at-rest discipline as sessions — a DB
leak alone can't reset anyone's password). Control-plane table, no RLS.

Validation is a hash lookup: the server never compares raw tokens, so
there's no timing side channel on the token bytes.
"""

import datetime as dt
import hashlib
import secrets

RESET_TTL = dt.timedelta(hours=1)
# Operator-provisioned accounts get a WELCOME set-password link — a
# brand-new user may not open their mail within the hour, and the link is
# their only way in (the account starts with an unusable random password)
WELCOME_TTL = dt.timedelta(days=7)


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# Advisory-lock namespace for the per-user reset mint (arbitrary constant; it
# is the first half of the (space, user) pair so these locks cannot collide
# with any other advisory lock taken against the same database).
_RESET_LOCK_SPACE = 74210093


def create_reset(conn, user_id, ttl: dt.timedelta = RESET_TTL) -> str:
    """Mint a token for the user; returns the RAW token (goes in the link).

    Minting burns every older live token for this user. Otherwise every
    "forgot password" click left another valid key under the mat for an hour
    — request five, and an attacker who reads ONE old email (or one leaked
    link) still has a working reset even after the user has moved on. The
    newest link is the only one that works, which is also what a user
    clicking "resend" already assumes.

    Every reset link is the same link. There is deliberately no flavour of
    this token that carries an exemption from the recovery code a second
    factor demands. Such an exemption — one minted, say, for an address
    whose account had never proved the mailbox — would hinge on
    `verified_at`, which an email change resets, so a fully verified
    household could be handed to whoever received mail at the new address.
    A reset that means different things depending on where it came from is
    a reset whose rules have to be re-derived at every call site, and that
    is where a takeover lives.
    """
    token = secrets.token_urlsafe(32)
    with conn.transaction():
        # Serialize per user, or the burn above is a promise this cannot
        # keep: two clicks racing each other each burn only the rows they
        # can see, neither sees the other's uncommitted insert, and both
        # links come out live. A row lock cannot stand in — the row that
        # would have to be locked is the one about to be written — so the
        # lock keys on the account instead.
        conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                     (_RESET_LOCK_SPACE, str(user_id)))
        conn.execute(
            "UPDATE password_resets SET used_at=now() "
            "WHERE user_id=%s AND used_at IS NULL", (user_id,))
        conn.execute(
            "INSERT INTO password_resets (user_id, token_hash, expires_at) "
            "VALUES (%s,%s,%s)",
            (user_id, _h(token), dt.datetime.now(dt.timezone.utc) + ttl))
    return token


def lookup_reset(conn, token: str) -> dict | None:
    """The reset row IF the token is real, unexpired, and unused."""
    if not token:
        return None
    return conn.execute(
        """SELECT r.id, r.user_id, u.email
           FROM password_resets r JOIN users u ON u.id = r.user_id
           WHERE r.token_hash = %s AND r.used_at IS NULL
             AND r.expires_at > now()""",
        (_h(token),)).fetchone()


def consume(conn, reset_id, user_id, new_password_hash: str) -> bool:
    """Atomically: set the new password, burn the token, and revoke EVERY
    session — whoever held the old password (or a hijacked cookie) is out.
    Returns False when the token was already burned (concurrent submit):
    the burn is the conditional gate, so one-time-use holds under races."""
    with conn.transaction():
        burned = conn.execute(
            "UPDATE password_resets SET used_at=now() "
            "WHERE id=%s AND used_at IS NULL", (reset_id,)).rowcount
        if not burned:
            return False
        conn.execute("UPDATE users SET password_hash=%s WHERE id=%s",
                     (new_password_hash, user_id))
        revoke_everything(conn, user_id)
    drop_recovery_codes(user_id)
    return True


def revoke_everything(conn, user_id) -> None:
    """Every session, factor and foothold on the account, gone — inside the
    CALLER's transaction, so a reset and the operator's clear-second-factor
    evict identically and a failure part-way rolls the lot back. The
    password is the caller's business; this touches nothing else of it.
    Recovery codes are NOT here: see `drop_recovery_codes`."""
    conn.execute("DELETE FROM sessions WHERE user_id=%s", (user_id,))
    # passkeys must die with the credential too —
    # otherwise a hijacker who enrolled one before the victim reset
    # keeps a permanent backdoor (POST /api/login/passkey)
    conn.execute("DELETE FROM passkeys WHERE user_id=%s", (user_id,))
    # Script tokens are the other
    # persistence a hijacker plants before the reset — a leaked oik_
    # token outlives the password otherwise. Revoke every token this
    # user minted (revoked_at, not delete — keeps the audit row).
    conn.execute("UPDATE api_tokens SET revoked_at=now() "
                 "WHERE created_by=%s AND revoked_at IS NULL",
                 (user_id,))
    # mobile device tokens are the same pre-plantable persistence —
    # every device re-authenticates after the reset
    conn.execute("UPDATE device_tokens SET revoked_at=now() "
                 "WHERE user_id=%s AND revoked_at IS NULL",
                 (user_id,))
    # Pending family invites are the same pre-planted
    # persistence — an invite minted by a hijacked session must not
    # stay claimable after the credential rotates.
    conn.execute("DELETE FROM invites WHERE created_by=%s "
                 "AND used_at IS NULL", (user_id,))
    # An unspent device-mint ticket is a bearer stand-in for a full
    # login (issued on EVERY sign-in, spent only by phones) — left
    # alive it would plant a fresh 90-day device token right after
    # this reset revoked the old ones.
    conn.execute("DELETE FROM webauthn_challenges "
                 "WHERE purpose='device-mint-ticket' AND user_id=%s",
                 (user_id,))
    # A browser's web-push subscription outlives its session: every
    # other posture change that evicts sessions drops these too
    # (web.app._drop_web_push), and this door did not — a hijacker's
    # browser kept receiving the verdict text and alert titles after
    # the owner recovered the account.
    conn.execute("DELETE FROM push_subscriptions WHERE user_id=%s",
                 (user_id,))
    # TOTP is the LAST persistence a hijacker
    # can plant before the victim recovers — enrolled TOTP otherwise
    # survives a password reset and locks the real owner out (they don't
    # have the attacker's authenticator or recovery codes). Email-proven
    # recovery legitimately clears 2FA; the owner re-enrolls.
    conn.execute("UPDATE users SET totp_secret=NULL, totp_last_counter=NULL "
                 "WHERE id=%s", (user_id,))


def drop_recovery_codes(user_id) -> None:
    """Recovery codes go with the factors. They are not inert once the
    secret is gone: passkey-only sign-in and every passkey step-up accept a
    recovery code with no TOTP enrolled, so a batch a hijacker saw — or a
    printed sheet the owner believed this reset voided — would keep working
    the moment a passkey is re-enrolled. The table is admin-write-only (the
    app role may only mark a code used), so this rides its own connection,
    AFTER the caller's transaction committed: a rollback there leaves the
    codes in place, and a failure here leaves the account reset with no
    codes to lose."""
    from ..db import tenancy
    admin = tenancy.admin_connect()
    try:
        admin.execute("DELETE FROM recovery_codes WHERE user_id=%s",
                      (user_id,))
        admin.commit()
    finally:
        admin.close()
