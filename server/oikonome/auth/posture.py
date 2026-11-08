"""Credential posture: what every account-security door takes away.

A door that changes how an account signs in — a new password or login
address, an authenticator turned on or off, a passkey added or removed,
a fresh recovery sheet, an emailed reset, the operator clearing the
factors, a link that undoes an address change, a reclaim moving a person
out of a household — is a door an owner reaches for when they think
someone else is in. Each must take away what that someone could have
planted, and nothing the door's own purpose does not justify. A list
spelled out door by door drifts: one door misses a foothold, another
over-rotates on a routine change. So the list lives here, as a LEVEL per door, and `rotate` is the only code that
takes footholds away.

The footholds, and which level closes which:

    S  other sessions             K  script tokens
    D  device tokens              W  webhooks
    T  device-mint tickets        O  the household's outbound settings
    P  browser push               M  the household's SMS number
    R  pending recoveries         I  pending invites + recent members
                                  G  support-access grants
    PK passkeys    TF the authenticator secret    C the recovery sheet

    SHEET       S D T P R                       a routine new sheet
    FACTOR      SHEET + K W O M I G             a factor on or off
    CREDENTIAL  FACTOR + PK (the proving key kept)   password, address
    RECOVERY    CREDENTIAL + TF, the caller too, M dropped, C dropped
    REVERT      S D T K W G R P, the caller too  an address change undone
    MOVE_OUT    S D T K W G P R PK C + unused invites + the sign-in links
                a person moved out of a household by a reclaim

The in-session levels (SHEET, FACTOR, CREDENTIAL) keep the caller's own
session, device and push subscription. REVERT touches no factor and keeps
the sheet: the old inbox may be why the owner moved away from it, so the
reset it mails still asks for a recovery code. MOVE_OUT leaves the
household's settings alone: the person is leaving it, not losing it.

The sheet. SHEET always mints a new one. FACTOR and CREDENTIAL mint one
when the door asks (a client that shows the new sheet says so, and an
older build keeps the sheet it has); CREDENTIAL also voids the sheet
outright when the rotation leaves no factor, because a code outlives the
factor and works again the moment a passkey is re-enrolled. RECOVERY
voids it after the door's transaction commits (`drop_sheet_after_commit`);
MOVE_OUT deletes it in the door's own transaction.

Lock order, which every door keeps by calling `rotate` and writing its own
`users` row only after it returns:

  1. the address lock `oikonome:signup:<email>` (login-address doors) →
     the factor lock `oikonome:passkeys:<uid>` (transaction scope, on the
     door's connection; passkey_register's session lock on the same key
     is re-entrant) → `tenant_settings` → the household-member lock →
     each removed member's factor lock → `users` rows. Nothing here waits
     on `tenant_settings` while holding a `users` row.
  2. The code lock `oikonome:recovery-codes:<uid>` only on the admin
     connection that carries the batch, only after the factor lock; its
     holder never asks for the factor lock. A staged batch commits after
     the door's transaction, a recovery's drop after that commit.
  3. Never the code lock while holding `users ... FOR UPDATE`: the
     reclaim's FOR UPDATE already waits on a staged batch's foreign-key
     share lock, and taking the code lock under it would deadlock. So
     MOVE_OUT deletes the codes under the users lock without the code
     lock. The move takes the factor lock before its FOR UPDATE (rule 1),
     so a reissue that starts after the move waits for it and then finds
     its caller signed out; and a batch about to be minted re-checks the
     household under the code lock (`recovery.hold`) before the door
     writes anything, so a reissue the move overtakes stops there instead
     of landing on the moved account — and holds no row the move queues
     behind while it waits.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from ..envnum import env_flag
from . import device_tokens
from . import reset as reset_mod

log = logging.getLogger("oikonome.auth")

# the footholds
SESSIONS = "sessions"
DEVICES = "device_tokens"
MINT_TICKETS = "mint_tickets"
PUSH = "push_subscriptions"
RECOVERIES = "pending_recoveries"
SCRIPT_TOKENS = "script_tokens"
WEBHOOKS = "webhooks"
OUTBOUND = "outbound_settings"
SMS = "sms_number"
INVITES = "invites_and_members"
SUPPORT = "support_grants"
PASSKEYS = "passkeys"
TOTP = "totp_secret"
SHEET_CODES = "recovery_sheet"

# the levels
SHEET = "sheet"
FACTOR = "factor"
CREDENTIAL = "credential"
RECOVERY = "recovery"
REVERT = "revert"
MOVE_OUT = "move_out"

_SIGN_IN = frozenset({SESSIONS, DEVICES, MINT_TICKETS, PUSH, RECOVERIES})
_HIJACK = _SIGN_IN | {SCRIPT_TOKENS, WEBHOOKS, OUTBOUND, SMS, INVITES,
                      SUPPORT}

LEVELS: dict[str, frozenset] = {
    SHEET: _SIGN_IN,
    FACTOR: _HIJACK,
    CREDENTIAL: _HIJACK | {PASSKEYS},
    RECOVERY: _HIJACK | {PASSKEYS, TOTP, SHEET_CODES},
    REVERT: frozenset({SESSIONS, DEVICES, MINT_TICKETS, PUSH, RECOVERIES,
                       SCRIPT_TOKENS, WEBHOOKS, SUPPORT}),
    MOVE_OUT: frozenset({SESSIONS, DEVICES, MINT_TICKETS, PUSH, RECOVERIES,
                         SCRIPT_TOKENS, WEBHOOKS, SUPPORT, INVITES, PASSKEYS,
                         SHEET_CODES}),
}

# the levels a signed-in caller runs, which spare that caller's own
# session, device and push subscription
KEEPS_CALLER = frozenset({SHEET, FACTOR, CREDENTIAL})


def lock_user_factors(conn, user_id) -> None:
    """The per-user factor lock, first in every door's transaction."""
    reset_mod.lock_user_factors(conn, user_id)


def caller_still_signed_in(conn, user) -> None:
    """Under the factor lock: 401 when the session or device token this
    caller authenticated with is gone. current_user checked it once, at
    the start of the request; a reset or the operator's clear that landed
    since has removed it, and a write committed after that would plant a
    factor, a bearer or a recovery batch for whoever sent the request.
    The eviction takes the same lock first, so the two are ordered."""
    if user.get("device_id"):
        row = conn.execute(
            "SELECT 1 FROM device_tokens WHERE id=%s AND revoked_at IS NULL",
            (user["device_id"],)).fetchone()
    elif user.get("session_id"):
        row = conn.execute("SELECT 1 FROM sessions WHERE id=%s",
                           (user["session_id"],)).fetchone()
    else:
        return
    if row is None:
        raise HTTPException(401, "not signed in")


def hold_sheet(conn, batch, user) -> None:
    """Before a door writes anything that a batch it will mint could
    queue behind: the code lock and the household re-check
    (`recovery.hold`), then the caller re-checked, since the wait may have
    outlasted a move that signed them out. Idempotent; `rotate` calls it
    when it mints."""
    if not batch.hold(user["user_id"], user.get("tenant_id")):
        raise HTTPException(401, "not signed in")
    caller_still_signed_in(conn, user)


def blank_report() -> dict:
    """The answer every door gives about its rotation: every key, always,
    so a client reads one shape whatever the door."""
    return {"other_sessions_signed_out": 0, "devices_revoked": 0,
            "tokens_revoked": 0, "webhooks_paused": 0,
            "invites_removed": 0, "members_removed": [],
            "support_access_revoked": 0, "sms_number_needs_verify": False,
            "passkeys_removed": 0, "passkeys_kept": 0,
            "recovery_codes": None}


def members_public(members) -> list[dict]:
    """Removed members as a door answers them: who, in which role, joined
    when — what the paused-settings banner shows."""
    return [{"email": m["email"], "role": m["role"], "joined": m["joined"]}
            for m in members or []]


def notice(verb: str, level: str | None, report: dict | None = None) -> str:
    """The security-notice key for a door: its verb, and — when the
    rotation reissued the sheet — the variant that says the old codes are
    dead. A routine new sheet IS the reissue, so its notice says it once."""
    if level is None or level == SHEET or not report:
        return verb
    return verb + "_reissued" if report.get("recovery_codes") else verb


class Report(dict):
    """`blank_report()`'s keys, plus `members`: the removed members' full
    rows, for the operator's audit line."""
    members: list


def rotate(conn, batch, user, level: str, *, mint: bool = False,
           keep_passkey=None, keep_push_endpoint: str = "",
           expect_members=None, reason: str = "rotation") -> Report:
    """Close every foothold `LEVELS[level]` names for `user`, inside the
    caller's transaction on `conn` (opened here for an autocommit caller,
    so the locks hold to the end of the work).

    `user` is the signed-in caller's dict for the in-session levels, whose
    own session, device (`device_id`) and push subscription
    (`session_id`, `keep_push_endpoint`) stay; for the others it need only
    carry `user_id`. `batch` is the door's `recovery.issue_staged` (or
    `recovery.issue_here` on a login-address door) — needed when the
    level may mint or void the sheet; `mint` asks for a new one where the
    level allows it. `keep_passkey`: the one key a CREDENTIAL rotation
    spares (see `_evict_passkeys`). `expect_members`: RECOVERY only, see
    `reset.revoke_everything`. `reason`: what a cancelled cooling-off
    recovery records as having ended it.

    Writes no `users` row of this user except RECOVERY's authenticator
    clear, which is last: the door writes its own change after this
    returns."""
    if level not in LEVELS:
        raise ValueError(f"no posture level {level!r}")
    closes = LEVELS[level]
    uid = user["user_id"]
    keeps = level in KEEPS_CALLER
    keep_session = user.get("session_id") if keeps else None
    keep_device = user.get("device_id") if keeps else None
    if not keeps:
        keep_push_endpoint = ""
    if mint and level not in (SHEET, FACTOR, CREDENTIAL):
        raise ValueError(f"level {level!r} never mints a sheet")
    if batch is None and (mint or level in (SHEET, CREDENTIAL)):
        # without one a CREDENTIAL rotation could not void the sheet it
        # leaves with no factor, and would succeed with the old codes live
        raise ValueError(f"level {level!r} needs the door's batch")
    rep = Report(blank_report())
    rep.members = []
    invitees: list[dict] = []
    from ..notify import outbound_pause, webhooks
    with conn.transaction():
        lock_user_factors(conn, uid)
        if keeps:
            caller_still_signed_in(conn, user)
        if level == SHEET:
            mint = True
        # Whether this rotation touches the sheet is known here: a mint,
        # or a CREDENTIAL rotation of an account that has one (it may be
        # voided below). Held now, before anything is written (rule 3).
        held = bool(mint or (level == CREDENTIAL
                             and _has_a_sheet(conn, uid)))
        if held:
            hold_sheet(conn, batch, user)
        # the settings row first: the SMS number, the outbound pause and
        # the record naming removed members are all the one row
        if SMS in closes:
            if level == RECOVERY:
                _drop_sms_number(conn, uid)
            else:
                rep["sms_number_needs_verify"] = _unverify_sms_number(conn,
                                                                      uid)
        if OUTBOUND in closes:
            outbound_pause.pause_for_rotation(
                conn, uid, reason=(outbound_pause.RECOVERY
                                   if level == RECOVERY
                                   else outbound_pause.ROTATION))
        if INVITES in closes:
            if level == RECOVERY:
                # read now, under the member lock; removed last, after the
                # settings row and before nothing else of this user's
                _, invitees = reset_mod._cancel_invites_and_read_invitees(
                    conn, uid, expect_members)
                outbound_pause.note_removed_members(conn, uid, invitees)
            elif level == MOVE_OUT:
                # a person leaving the household removes nobody from it
                rep["invites_removed"] = conn.execute(
                    "DELETE FROM invites WHERE created_by=%s "
                    "AND used_at IS NULL", (uid,)).rowcount
            else:
                n, gone = reset_mod.evict_invite_footholds(conn, uid)
                rep["invites_removed"] = n
                rep.members = gone
        if SESSIONS in closes:
            rep["other_sessions_signed_out"] = conn.execute(
                "DELETE FROM sessions WHERE user_id = %s "
                "AND id IS DISTINCT FROM %s", (uid, keep_session)).rowcount
        if MINT_TICKETS in closes:
            # before the device tokens: a ticket being redeemed holds its
            # row until its new token commits, so the revoke below then
            # sees that token. A move clears every challenge it holds.
            if level == MOVE_OUT:
                conn.execute("DELETE FROM webauthn_challenges "
                             "WHERE user_id=%s", (uid,))
            else:
                device_tokens.revoke_mint_tickets(conn, uid)
        if DEVICES in closes:
            rep["devices_revoked"] = device_tokens.revoke_all_for_user(
                conn, uid, keep_device_id=keep_device)
        if PASSKEYS in closes:
            if level == CREDENTIAL:
                rep["passkeys_removed"], rep["passkeys_kept"] = \
                    _evict_passkeys(conn, uid, keep_passkey)
            else:
                rep["passkeys_removed"] = conn.execute(
                    "DELETE FROM passkeys WHERE user_id=%s", (uid,)).rowcount
        if SCRIPT_TOKENS in closes:
            # revoked, not deleted: the row is the audit
            rep["tokens_revoked"] = conn.execute(
                "UPDATE api_tokens SET revoked_at=now() "
                "WHERE created_by=%s AND revoked_at IS NULL",
                (uid,)).rowcount
        if SUPPORT in closes:
            rep["support_access_revoked"] = _revoke_support_grants(conn, uid)
        if WEBHOOKS in closes and level == MOVE_OUT:
            # the hooks this person made in the household they are
            # leaving, paused as a removal pauses them. The pause pins the
            # old household for the rest of the transaction, and the
            # move provisions the new one after this, so the pin is put
            # back as it was.
            prev = conn.execute("SELECT current_setting('app.tenant_id', "
                                "true) AS t").fetchone()["t"]
            rep["webhooks_paused"] = webhooks.pause_for_rotation(
                conn, uid, reason=webhooks.REMOVED_REASON)
            conn.execute("SELECT set_config('app.tenant_id', %s, true)",
                         (prev or "",))
        elif WEBHOOKS in closes:
            rep["webhooks_paused"] = webhooks.pause_for_rotation(conn, uid)
        if PUSH in closes:
            _drop_web_push(conn, uid, keep_push_endpoint,
                           keep_session_id=keep_session)
        if RECOVERIES in closes:
            end_pending_recoveries(conn, uid, reason=reason)
        if level == MOVE_OUT:
            _move_out_extras(conn, uid)
        if level == RECOVERY:
            # the authenticator, last of this user's own rows: the
            # settings row above came first (lock order 1)
            conn.execute("UPDATE users SET totp_secret=NULL, "
                         "totp_last_counter=NULL WHERE id=%s", (uid,))
            # and the members, whose rows are users rows too
            rep.members = reset_mod._remove_members(conn, invitees)
        if level == CREDENTIAL and held and not _has_a_factor(conn, uid):
            # no factor left: a code would work again the moment a passkey
            # is re-enrolled, and there is no factor to show a new sheet for
            batch.drop(uid)
        elif mint:
            # last, under every lock: the batch the reply carries is the
            # batch that stands, committed after the door's transaction
            rep["recovery_codes"] = batch.issue(uid)
    rep["members_removed"] = members_public(rep.members)
    return rep


def _has_a_factor(conn, user_id) -> bool:
    row = conn.execute(
        "SELECT totp_secret IS NOT NULL AS t, (SELECT count(*) FROM passkeys "
        "WHERE user_id = %s) AS n FROM users WHERE id = %s",
        (user_id, user_id)).fetchone()
    return bool(row and (row["t"] or row["n"]))


def _has_a_sheet(conn, user_id) -> bool:
    return conn.execute("SELECT 1 FROM recovery_codes WHERE user_id=%s "
                        "LIMIT 1", (user_id,)).fetchone() is not None


def _move_out_extras(conn, user_id) -> None:
    """What a reclaim's move-out ends beyond the level's footholds: the
    sheet (under the door's users-row lock, so without the code lock —
    lock rule 3), and every link still on its way to the address the row
    carried, which now belongs to the clicker's own household."""
    for sql in ("DELETE FROM recovery_codes WHERE user_id=%s",
                "UPDATE login_unlocks SET consumed_at=now() "
                "WHERE user_id=%s AND consumed_at IS NULL",
                "UPDATE email_verifications SET used_at=now() "
                "WHERE user_id=%s AND used_at IS NULL"):
        conn.execute(sql, (user_id,))
    from . import email_revert
    email_revert.burn_all(conn, user_id)


def end_pending_recoveries(conn, user_id, reason: str = "rotation") -> None:
    """Void every recovery still waiting to land on this account: unspent
    mailed reset links AND a cooling-off factor-clearing reset. Both strip
    the factors when they land; one started before a rotation would land
    afterwards and take away what the owner just put on. Voided together
    so a door cannot remember one and forget the other. Also run alone by
    a door that adds a factor beside another (a second passkey) and by a
    login-address move."""
    conn.execute("UPDATE password_resets SET used_at=now() "
                 "WHERE user_id=%s AND used_at IS NULL", (user_id,))
    from . import factor_reset
    if factor_reset.cancel_for_user(conn, user_id, reason=reason):
        log.info("pending factor reset cancelled by a credential rotation")


def revoke_passkey(conn, user_id, passkey_id) -> int:
    """Remove one passkey and what its sign-ins left live — key-scoped,
    not a level: the sessions it signed in (the caller's own included),
    the device tokens minted from them and their unspent mint tickets,
    and every elevation stamp that names it. Returns how many keys went.

    In this order: a ticket is found through the session it was issued
    with, so the tickets go before the sessions. The stamp's column has no
    foreign key, and a later rotation reads a name it carries as "keep
    this key", which for a key that no longer exists means deleting every
    other one; the whole stamp goes, window and all, since a sudo window
    is only as good as the credential that opened it."""
    conn.execute(
        "DELETE FROM webauthn_challenges "
        "WHERE purpose='device-mint-ticket' AND user_id=%s "
        "AND session_hash IN ("
        "    SELECT token_hash FROM sessions "
        "    WHERE user_id=%s AND signin_passkey_id=%s)",
        (user_id, user_id, passkey_id))
    conn.execute(
        "UPDATE device_tokens SET revoked_at=now() "
        "WHERE user_id=%s AND signin_passkey_id=%s AND revoked_at IS NULL",
        (user_id, passkey_id))
    conn.execute(
        "DELETE FROM sessions WHERE user_id=%s AND signin_passkey_id=%s",
        (user_id, passkey_id))
    n = conn.execute("DELETE FROM passkeys WHERE id = %s AND user_id = %s",
                     (passkey_id, user_id)).rowcount
    for tbl in ("sessions", "device_tokens"):
        conn.execute(
            f"UPDATE {tbl} SET elevated_at = NULL, "             # noqa: S608 — table name is a literal from the tuple above
            "elevated_passkey_id = NULL "
            "WHERE user_id = %s AND elevated_passkey_id = %s",
            (user_id, passkey_id))
    return n


def drop_sheet_after_commit(user_id) -> None:
    """RECOVERY's sheet: the codes go with the factors. They are not inert
    once the secret is gone — passkey-only sign-in and every passkey
    step-up accept a code with no TOTP enrolled — so a batch a hijacker saw
    would work again the moment a passkey is re-enrolled. The table is
    admin-write-only, so this rides its own connection, AFTER the door's
    transaction committed: a rollback there leaves the codes in place, and
    a failure here leaves the account recovered with no codes to lose.
    Under the code lock, so a reissue still committing is waited for and
    dropped too."""
    from ..db import tenancy
    from . import recovery
    admin = tenancy.admin_connect()
    try:
        recovery.drop(admin, user_id)
    finally:
        admin.close()


def _revoke_support_grants(conn, user_id) -> int:
    """End every live support-access grant this user made. Revoked, never
    deleted: the row is the audit of who granted what."""
    return conn.execute(
        "UPDATE support_consents SET revoked_at=now() "
        "WHERE granted_by=%s AND revoked_at IS NULL", (user_id,)).rowcount


def _drop_web_push(conn, user_id, keep_endpoint: str = "",
                   keep_session_id=None) -> int:
    """A browser's web-push subscription outlives its session, so a
    rotation drops them; the browser making the request may keep its own,
    named by its endpoint or by the session it was made under. Native
    tickles are content-free and follow the device token's revocation."""
    return conn.execute(
        "DELETE FROM push_subscriptions WHERE user_id=%s AND endpoint <> %s"
        " AND (%s::uuid IS NULL OR session_id IS DISTINCT FROM %s::uuid)",
        (user_id, keep_endpoint or "", keep_session_id,
         keep_session_id)).rowcount


def _evict_passkeys(conn, user_id, keep_passkey_id=None) -> tuple[int, int]:
    """CREDENTIAL's passkeys. Returns (removed, kept).

    A hijacked session could have enrolled one, and WebAuthn login would
    outlive everything else the rotation revokes. But on a hosted account
    whose ONLY strong factor is a passkey, evicting them all drops it to
    zero factors and email+password alone opens the household — the
    downgrade passkey_delete and totp_disable each refuse. So exactly ONE
    key may survive: the one that produced this caller's elevation proof,
    `keep_passkey_id`. Recency is not that test (any session can re-assert
    a planted key every few minutes). With no passkey proof — a password,
    a TOTP code, a recovery code — they all go: the lost-authenticator
    road, landing where a reset lands on the same proof. An account with
    TOTP keeps a factor either way, and a self-hosted or waived account
    was never under forced 2FA, so both evict unconditionally."""
    with conn.transaction():
        lock_user_factors(conn, user_id)
        row = conn.execute(
            "SELECT totp_secret, second_factor_waived FROM users WHERE id=%s",
            (user_id,)).fetchone()
        if (env_flag("OIKONOME_HOSTED") and row and not row["totp_secret"]
                and not row["second_factor_waived"] and keep_passkey_id):
            # Truthy is not existence: the door refused a dangling name up
            # front, so this fires only when the key went between that
            # check and this lock — and here a name that no longer exists
            # would delete the last factor and call it success.
            if not conn.execute(
                    "SELECT 1 FROM passkeys WHERE id=%s AND user_id=%s",
                    (keep_passkey_id, user_id)).fetchone():
                raise HTTPException(403,
                                    detail={"error": "elevation_required",
                                            "fresh": False})
            removed = conn.execute(
                "DELETE FROM passkeys WHERE user_id=%s AND id <> %s",
                (user_id, keep_passkey_id)).rowcount
            kept = conn.execute(
                "SELECT count(*) AS n FROM passkeys WHERE user_id=%s",
                (user_id,)).fetchone()["n"]
            if not kept:
                # unreachable given the check above, and kept checked: the
                # one outcome this branch must never have is success with
                # no factor left. Raising rolls the delete back.
                raise HTTPException(500, "passkey rotation would have left "
                                         "this account with no second "
                                         "factor — nothing was removed")
            return removed, kept
        return conn.execute("DELETE FROM passkeys WHERE user_id=%s",
                            (user_id,)).rowcount, 0


def _drop_sms_number(conn, user_id) -> bool:
    """RECOVERY's SMS number: an owner's emailed reset or cleared factors
    drop the household's number, verified or pending. A verified number
    keeps receiving the daily summary with no session at all, and its
    code went to the number itself, so whoever held the account could have
    verified their own phone. Only an owner can set it, so only an
    owner's recovery clears it. Returns whether a number was cleared.
    Control-plane or admin connection: the tenant is pinned for this
    transaction only, and the row is also named by tenant id."""
    u = conn.execute("SELECT tenant_id FROM users "
                     "WHERE id=%s AND role='owner'", (user_id,)).fetchone()
    if not u:
        return False
    with conn.transaction():
        conn.execute("SELECT set_config('app.tenant_id', %s, true)",
                     (str(u["tenant_id"]),))
        return conn.execute(
            "UPDATE tenant_settings "
            "SET config = config - 'notify_phone' - 'notify_phone_pending' "
            "WHERE tenant_id=%s AND (config ? 'notify_phone' "
            "OR config ? 'notify_phone_pending')",
            (u["tenant_id"],)).rowcount > 0


def _unverify_sms_number(conn, user_id) -> bool:
    """FACTOR and CREDENTIAL's SMS number: an owner's routine rotation
    marks it unverified rather than dropping it. Texts stop until a fresh
    code sent to that number is typed back, so a phone a hijacker verified
    stops at once while the owner's own is one confirmation from working
    again; any half-finished verification goes too. Returns whether a
    verified number was marked, so the door can say so. A member's
    rotation does nothing here: only an owner can set the number."""
    u = conn.execute("SELECT tenant_id FROM users "
                     "WHERE id=%s AND role='owner'", (user_id,)).fetchone()
    if not u:
        return False
    with conn.transaction():
        conn.execute("SELECT set_config('app.tenant_id', %s, true)",
                     (str(u["tenant_id"]),))
        conn.execute(
            "UPDATE tenant_settings "
            "SET config = config - 'notify_phone_pending' "
            "WHERE tenant_id=%s AND config ? 'notify_phone_pending'",
            (u["tenant_id"],))
        return conn.execute(
            "UPDATE tenant_settings SET config = jsonb_set("
            "config, '{notify_phone}', (config->'notify_phone') "
            "|| jsonb_build_object('verified', false, "
            "'unverified_at', now()::text)) "
            "WHERE tenant_id=%s "
            "AND jsonb_typeof(config->'notify_phone') = 'object' "
            "AND config->'notify_phone'->'verified' = 'true'::jsonb",
            (u["tenant_id"],)).rowcount > 0
