"""Password-reset tokens: one-time, 1-hour, opaque 256-bit token in the
link, sha256 of it in the DB (same at-rest discipline as sessions — a DB
leak alone can't reset anyone's password). Control-plane table, no RLS.

Validation is a hash lookup: the server never compares raw tokens, so
there's no timing side channel on the token bytes.
"""

import datetime as dt
import hashlib
import logging
import secrets

log = logging.getLogger("oikonome.auth")

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
    "forgot password" click leaves another valid key under the mat for an
    hour — request five, and an attacker who reads ONE old email (or one
    leaked link) still has a working reset even after the user has moved on. The
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


def lock_user_factors(conn, user_id) -> None:
    """The per-user lock every account-security write serializes on, held
    to the end of the caller's transaction. Taken FIRST, before any write
    to the user's row, so two holders can never wait on each other in
    opposite orders. Re-entrant within a transaction."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                 ("oikonome:passkeys:" + str(user_id),))


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


class SecondProofWithdrawn(Exception):
    """Raised by `consume` when its `gate` says the proof that let this
    reset skip the recovery code no longer stands. Everything the call did
    is rolled back — the token stays live, no factor is touched."""


def consume(conn, reset_id, user_id, new_password_hash: str,
            gate=None) -> bool:
    """Atomically: set the new password, burn the token, and revoke EVERY
    session — whoever held the old password (or a hijacked cookie) is out.
    Returns False when the token was already burned (concurrent submit):
    the burn is the conditional gate, so one-time-use holds under races.

    `gate(conn)`, when given, runs inside the same transaction before
    anything is stripped; a falsy answer raises `SecondProofWithdrawn` and
    rolls the whole reset back. It is how the caller re-proves, at the
    moment of commit, a condition it checked earlier."""
    with conn.transaction():
        # first, as every account-security door does: see revoke_everything
        lock_user_factors(conn, user_id)
        burned = conn.execute(
            "UPDATE password_resets SET used_at=now() "
            "WHERE id=%s AND used_at IS NULL", (reset_id,)).rowcount
        if not burned:
            return False
        if gate is not None and not gate(conn):
            raise SecondProofWithdrawn()
        # the eviction first: it writes the household's settings (the SMS
        # number) before the users row, and the password must not put a
        # users row lock ahead of that (auth/posture lock order)
        revoke_everything(conn, user_id, reason="reset")
        conn.execute("UPDATE users SET password_hash=%s WHERE id=%s",
                     (new_password_hash, user_id))
    drop_recovery_codes(user_id)
    return True


class MembersChanged(Exception):
    """The members an owner's recovery would remove are not the ones the
    caller was shown. Raised inside the caller's transaction, so nothing
    of the eviction is kept; `members` is the set as it stands now."""

    def __init__(self, members: list[dict]):
        super().__init__("the members this recovery removes changed")
        self.members = members


def revoke_everything(conn, user_id, expect_members=None,
                      reason: str = "rotation") -> list[dict]:
    """Every session, factor and foothold on the account, gone — inside the
    CALLER's transaction, so a reset and the operator's clear-second-factor
    evict identically and a failure part-way rolls the lot back: the
    RECOVERY level of auth/posture, which says what that takes. The
    password is the caller's business. The recovery sheet goes after the
    caller commits: see `drop_recovery_codes`.

    Returns the household members it removed, read from the deletes
    themselves, so whoever reports the removal names exactly those rows.
    `expect_members`, when given, is the set of addresses the operator's
    confirmation listed: if the members this would remove (read under the
    member lock, the same query the preview runs) are any other set,
    `MembersChanged` is raised and nobody is removed by a step whose
    prompt never named them. `reason`: what a cancelled cooling-off
    recovery records."""
    from . import posture
    return posture.rotate(conn, None, {"user_id": user_id}, posture.RECOVERY,
                          expect_members=expect_members,
                          reason=reason).members


def _cancel_invites_and_read_invitees(conn, user_id, expect_members=None
                                      ) -> tuple[int, list[dict]]:
    """Cancel the user's pending invites and read the members their recent
    claimed ones created (see _recent_invitees), inside the caller's
    transaction. Returns (invites cancelled, invitees).

    The invitees are read only once no claim of this user's invites is in
    flight: a claim burns its invite and then inserts the member under the
    household's member lock, and a read taken while either was uncommitted
    would miss the new member, who would then outlive the rotation. So it
    waits out both — the member lock (same key the claim takes) and every
    invite row of the user — and cancels the pending invites, before
    reading who joined. `expect_members`: see revoke_everything.

    Only an OWNER's waits on the member lock. A member's rotation removes
    nobody (_recent_invitees is empty for them), so it has no claim to
    wait out, and it must not: it already holds its own factor lock,
    while an owner's rotation takes that very lock UNDER the member lock
    to remove the member (_remove_members). A member's door that then
    asked for the member lock would close a cycle with the owner removing
    them, and Postgres would kill one side — most often the owner's, whose
    whole rotation rolls back. The invite rows below are all a member's
    cancellation needs. (No door makes anyone an owner, so the role read
    here cannot change under the caller.)"""
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext("
        "  'oikonome:tenant-members:' || tenant_id::text))"
        "  FROM users WHERE id = %s AND role = 'owner'", (user_id,))
    conn.execute("SELECT 1 FROM invites WHERE created_by=%s FOR UPDATE",
                 (user_id,))
    # Pending family invites are the same pre-planted persistence — an
    # invite minted by a hijacked session must not stay claimable after the
    # credential rotates.
    cancelled = conn.execute("DELETE FROM invites WHERE created_by=%s "
                             "AND used_at IS NULL", (user_id,)).rowcount
    invitees = _recent_invitees(conn, user_id)
    if expect_members is not None and (
            {m["email"].lower() for m in invitees}
            != {e.strip().lower() for e in expect_members if e.strip()}):
        raise MembersChanged(invitees)
    # An invite burned moments ago whose member is not in yet is a claim
    # between its burn and its insert. Deleting the burned row here, under
    # the member lock, is what that claim re-checks before it inserts, so
    # it fails rather than create a member this rotation never saw.
    conn.execute("DELETE FROM invites WHERE created_by=%s "
                 "AND used_at > now() - interval '10 minutes'", (user_id,))
    return cancelled, invitees


def evict_invite_footholds(conn, user_id) -> tuple[int, list[dict]]:
    """What an OWNER's in-session rotation (a password or login-email
    change, an authenticator turned on or off, a first passkey) takes away
    of the household roster, the same as recovery takes: the pending
    invites, and the members who joined in the last days through this
    owner's invites — removed, off every mail list, and named on the
    paused-settings record so the banner says who went and that the ones
    the owner recognises can be invited again.

    The owner is still signed in, so this could have been a routine
    change rather than a hijack response; the removal is the same anyway.
    A session thief who claimed an invite holds a whole second account
    that none of the owner's credentials reach, and nothing tells a
    rotation made in good faith from one made to close a hijack. Inviting
    a real member back is one step, and is the owner's confirmation.
    A member's own rotation cancels their pending invites and removes
    nobody, and takes no household-member lock to do it (see
    _cancel_invites_and_read_invitees for the lock order that forces).

    Inside the caller's transaction (one is opened for an autocommit
    caller, so the locks below hold to the end of the work). The caller
    must already have taken the household's settings row — the outbound
    pause does — or hold no users row yet: the record below writes the
    settings row, and removing a member writes a users row, in the order
    every account-security door keeps. Returns (invites cancelled,
    members removed)."""
    from ..notify import outbound_pause
    with conn.transaction():
        cancelled, invitees = _cancel_invites_and_read_invitees(conn, user_id)
        # an in-session rotation, so the record says so rather than "recovery"
        outbound_pause.note_removed_members(conn, user_id, invitees,
                                            reason=outbound_pause.ROTATION)
        return cancelled, _remove_members(conn, invitees)


# How far back an owner's recovery — and an owner's in-session rotation,
# see evict_invite_footholds — reaches for members who joined through
# that owner's own invites. The cancelled pending invites above are only
# half of what a stolen owner session can plant: a claimed one is a whole
# second account, with its own password and its own second factor, that no
# credential of the owner's reaches. There is no marker for when the
# account was taken, so the reach is a window, kept short because every
# member inside it is removed, and re-inviting the person is the owner's
# confirmation. 0 turns the removal off.
RECOVERY_MEMBER_WINDOW_DAYS = "7"


def _recovery_member_window_days() -> int:
    from ..envnum import env_num
    try:
        return max(0, int(env_num("OIKONOME_RECOVERY_MEMBER_WINDOW_DAYS",
                                  RECOVERY_MEMBER_WINDOW_DAYS)))
    except ValueError:
        return int(RECOVERY_MEMBER_WINDOW_DAYS)


def _recent_invitees(conn, owner_id) -> list[dict]:
    """The members an OWNER's recovery or rotation removes: everyone who
    joined, inside the window, by claiming an invite that owner minted.
    Empty for a member's own — only the owner's is the path for someone
    who lost the account to whoever minted the link.

    Which invite a member claimed is not stored, so the claim is recognised
    by its timing: the claim burns the invite and creates the account
    straight after, so a member created within minutes after one of the
    owner's invites was used came through it."""
    days = _recovery_member_window_days()
    if not days:
        return []
    return [{"id": r["id"], "email": r["email"], "role": r["role"],
             "tenant_id": r["tenant_id"],
             "joined": r["created_at"].isoformat()}
            for r in conn.execute(
        """SELECT u.id, u.email, u.role, u.tenant_id, u.created_at
             FROM users o
             JOIN users u ON u.tenant_id = o.tenant_id AND u.id <> o.id
            WHERE o.id = %s AND o.role = 'owner' AND u.role <> 'owner'
              AND u.created_at > now() - make_interval(days => %s)
              AND EXISTS (
                  SELECT 1 FROM invites i
                   WHERE i.created_by = o.id AND i.tenant_id = o.tenant_id
                     AND i.used_at IS NOT NULL
                     AND u.created_at >= i.used_at
                     AND u.created_at < i.used_at + interval '10 minutes')
            ORDER BY u.id""", (owner_id, days)).fetchall()]


def _remove_members(conn, members: list[dict]) -> list[dict]:
    """Removal is the household roster's removal: the member's script
    tokens are revoked, their unclaimed invites deleted and their webhooks
    paused before the row goes (the SET NULL keys would otherwise erase who
    made them), and their sessions, passkeys and device tokens cascade with
    it. Their questions stay in the assistant's log without their name.
    Their mail-list entries were taken off by
    `outbound_pause.note_removed_members` earlier in the same transaction.
    Returns the members whose row this deleted."""
    if not members:
        return []
    from ..engine import assistant as _assistant
    from ..notify import webhooks
    gone = []
    for m in members:
        # the member's own factor lock before any of their rows, the order
        # every account-security door keeps — and after the household-member
        # lock this caller holds: a removed member's factor lock is the one
        # taken under it, which is why a member's own door never asks for
        # the member lock while holding this one
        lock_user_factors(conn, m["id"])
        conn.execute(
            "UPDATE api_tokens SET revoked_at = now() WHERE created_by = %s "
            "AND tenant_id = %s AND revoked_at IS NULL",
            (m["id"], m["tenant_id"]))
        conn.execute(
            "DELETE FROM invites WHERE created_by = %s AND tenant_id = %s "
            "AND used_at IS NULL", (m["id"], m["tenant_id"]))
        webhooks.pause_for_rotation(conn, m["id"],
                                    reason=webhooks.REMOVED_REASON)
        if conn.execute(
                "DELETE FROM users WHERE id = %s AND tenant_id = %s "
                "AND role <> 'owner' RETURNING id",
                (m["id"], m["tenant_id"])).fetchone() is not None:
            # the assistant's log keeps their questions but not their name,
            # as every other removal leaves it
            _assistant.forget_asker(conn, m["tenant_id"], m["id"])
            gone.append(m)
    log.warning("an owner's recovery or credential rotation removed %d "
                "member(s) who joined through the owner's recent invites",
                len(gone))
    return gone


def _drop_sms_number(conn, user_id) -> bool:
    """See auth/posture: RECOVERY drops the household's SMS number."""
    from . import posture
    return posture._drop_sms_number(conn, user_id)


def _unverify_sms_number(conn, user_id) -> bool:
    """See auth/posture: FACTOR and CREDENTIAL mark it unverified."""
    from . import posture
    return posture._unverify_sms_number(conn, user_id)


def drop_recovery_codes(user_id) -> None:
    """RECOVERY's sheet, voided after the caller's transaction committed:
    see auth/posture.drop_sheet_after_commit."""
    from . import posture
    posture.drop_sheet_after_commit(user_id)
