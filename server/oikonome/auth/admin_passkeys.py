"""WebAuthn for the ADMIN CONSOLE operator.

A sibling of `auth/passkeys.py`, not a reuse of it. That module keys every
row on a tenant `users.id`; the console is deliberately outside the tenant
auth world (see `web/adminconsole.py`), so its credentials live in their
own `admin_credentials` table on the admin role — the app role holds no
grants on it (migration 072).

What this buys over the shared `OIKONOME_ADMIN_TOKEN`:
 * hardware-bound and unphishable — the key signs the real origin, so a
   lookalike page gets nothing;
 * ATTRIBUTABLE — the audit trail names the credential that acted, where
   the token could only ever name an IP;
 * revocable one key at a time, with no restart.

The token does NOT go away: WebAuthn needs a secure context, and a
self-host install on plain http:// over the LAN cannot enrol anything. It
stays a fully working break-glass path; passkey-only enforcement is opt-in
(OIKONOME_ADMIN_REQUIRE_PASSKEY).

Every function here takes an ADMIN connection (`tenancy.admin_connect`).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import secrets

from webauthn import (generate_authentication_options,
                      generate_registration_options, options_to_json,
                      verify_authentication_response,
                      verify_registration_response)
from webauthn.helpers import base64url_to_bytes
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria,
                                      PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement,
                                      UserVerificationRequirement)

CHALLENGE_TTL = dt.timedelta(minutes=10)
TICKET_TTL = dt.timedelta(minutes=5)
RP_NAME = "Oikonome admin"
TICKET_PREFIX = "adminstep-"


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _norm_cred_id(s: str) -> str:
    """Normalize a base64url credential id for lookup (strip padding,
    standard→urlsafe alphabet) — mirrors passkeys._norm_cred_id."""
    if not s:
        return ""
    return s.strip().replace("+", "-").replace("/", "_").rstrip("=")


def _origins(origin: str) -> list[str]:
    """Origins a console assertion may claim — the tenant flow's web rule
    (passkeys._expected_origins) without its native-app origins.

    The https twin is always accepted: a proxy that loses
    X-Forwarded-Proto makes the server derive http:// while the browser
    signed the real https:// page. Plain http is accepted only for
    localhost/127.0.0.1: browsers refuse WebAuthn on any other insecure
    origin, so accepting http:// for a public console host could never
    admit a real login and only weakens downgrade resistance."""
    alt = None
    if origin.startswith("http://"):
        alt = "https://" + origin[len("http://"):]
    elif origin.startswith("https://"):
        alt = "http://" + origin[len("https://"):]

    def _localhost(o: str) -> bool:
        host = o.split("://", 1)[-1].split("/", 1)[0].split(":")[0]
        return host in ("localhost", "127.0.0.1")

    out = [o for o in (origin, alt)
           if o and (o.startswith("https://") or _localhost(o))]
    return out or [origin]


def _store_challenge(admin, purpose: str, challenge: bytes,
                     session_hash: str | None = None) -> str:
    row = admin.execute(
        """INSERT INTO admin_challenges (purpose, challenge, session_hash,
                                         expires_at)
           VALUES (%s,%s,%s,%s) RETURNING id""",
        (purpose, _b64u(challenge), session_hash,
         dt.datetime.now(dt.timezone.utc) + CHALLENGE_TTL)).fetchone()
    # opportunistic sweep so the table never accumulates
    admin.execute("DELETE FROM admin_challenges WHERE expires_at < now()")
    return str(row["id"])


def _take_challenge(admin, challenge_id: str, purpose: str,
                    session_hash: str | None = None) -> bytes | None:
    """Single use. With session_hash, the challenge must have been minted
    FOR that admin session — a step-up started in one browser cannot be
    completed from another."""
    if not challenge_id:
        return None
    try:
        row = admin.execute(
            """DELETE FROM admin_challenges
               WHERE id = %s::uuid AND purpose = %s AND expires_at > now()
                 AND (%s::text IS NULL OR session_hash = %s::text)
               RETURNING challenge""",
            (challenge_id, purpose, session_hash, session_hash)).fetchone()
    except Exception:                                        # noqa: BLE001
        # a malformed uuid is a wrong answer, not a 500
        return None
    return base64url_to_bytes(row["challenge"]) if row else None


def count(admin) -> int:
    return admin.execute(
        "SELECT count(*) AS n FROM admin_credentials").fetchone()["n"]


def credentials(admin) -> list[dict]:
    return admin.execute(
        """SELECT id, label, transports, created_at, last_used
           FROM admin_credentials ORDER BY created_at""").fetchall()


# ---- enrolment ------------------------------------------------------------


def register_options(admin, rp_id: str, label: str = "") -> dict:
    """Options for enrolling a NEW operator key.

    Every enrolment gets its own random user handle. A constant handle would
    make a second key on the SAME authenticator overwrite the first (the
    (rp_id, user handle) pair is a passkey's identity), which is exactly the
    "hold more than one so you can't lock yourself out" case this exists for.
    Lookup never uses the handle — resident credentials are found by
    credential id — so nothing depends on them matching.
    """
    existing = [PublicKeyCredentialDescriptor(
        id=base64url_to_bytes(r["credential_id"]))
        for r in admin.execute(
            "SELECT credential_id FROM admin_credentials").fetchall()]
    opts = generate_registration_options(
        rp_id=rp_id, rp_name=RP_NAME,
        user_id=secrets.token_bytes(16),
        user_name=(label.strip()[:60] or "operator"),
        user_display_name="Oikonome operator",
        exclude_credentials=existing,
        authenticator_selection=AuthenticatorSelectionCriteria(
            # REQUIRED so console sign-in is usernameless — the login page
            # has no identifier field to allow-list against, and publishing
            # the credential ids to an unauthenticated caller would hand a
            # prober the operator's key inventory.
            resident_key=ResidentKeyRequirement.REQUIRED,
            # the assertion IS the whole credential here (no password behind
            # it), so the key itself must verify the human: PIN/biometric.
            user_verification=UserVerificationRequirement.REQUIRED))
    cid = _store_challenge(admin, "register", opts.challenge)
    return {"challenge_id": cid, "options": json.loads(options_to_json(opts))}


def register_verify(admin, challenge_id: str, credential: dict, rp_id: str,
                    origin: str, label: str = "") -> dict:
    challenge = _take_challenge(admin, challenge_id, "register")
    if challenge is None:
        raise ValueError("challenge expired — start the enrolment again")
    # options are advisory — a tampering client could strip the UV flag, so
    # REQUIRED is enforced here, on the authenticator's SIGNED response
    v = verify_registration_response(
        credential=credential, expected_challenge=challenge,
        expected_rp_id=rp_id, expected_origin=_origins(origin),
        require_user_verification=True)
    transports = ",".join(credential.get("response", {})
                          .get("transports", []) or [])
    row = admin.execute(
        """INSERT INTO admin_credentials (label, credential_id, public_key,
                                          sign_count, transports)
           VALUES (%s,%s,%s,%s,%s) RETURNING id, label""",
        (label.strip()[:60], _b64u(v.credential_id),
         _b64u(v.credential_public_key), v.sign_count,
         transports)).fetchone()
    return {"id": str(row["id"]), "label": row["label"]}


def delete(admin, cred_id: str) -> bool:
    """Remove one operator passkey AND every console session it authenticated.

    Order is load-bearing: `admin_sessions.credential_id` is
    `ON DELETE SET NULL`, so deleting the credential first NULLs the column
    and a later session WHERE matches nothing.
    """
    try:
        key_uuid = str(cred_id)
        # Sessions first — same order as adminconsole.passkey_delete.
        admin.execute(
            "DELETE FROM admin_sessions WHERE credential_id = %s::uuid",
            (key_uuid,))
        return admin.execute(
            "DELETE FROM admin_credentials WHERE id = %s::uuid",
            (key_uuid,)).rowcount > 0
    except Exception:                                        # noqa: BLE001
        return False


def reset_all(admin) -> int:
    """Break-glass: drop every operator passkey and every admin session.

    Host CLI recovery (`oikonome admin-keys --reset`). Sessions outlive a
    bare credential wipe (FK SET NULL), so a cookie from before the reset
    would still act — and with zero passkeys enrolled, step-up on
    destructive console ops is skipped.
    """
    admin.execute("DELETE FROM admin_sessions")
    return admin.execute("DELETE FROM admin_credentials").rowcount


# ---- sign-in --------------------------------------------------------------


def login_options(admin, rp_id: str) -> dict:
    """Discoverable-credential (usernameless) flow — no allow-list.

    Deliberate: an allow-list would enumerate the operator's credential ids
    to anyone who gets past the console's cloak, and the keys are resident
    by construction, so the authenticator can find them itself."""
    if count(admin) == 0:
        raise ValueError("no operator passkeys enrolled")
    opts = generate_authentication_options(
        rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED)
    cid = _store_challenge(admin, "login", opts.challenge)
    return {"challenge_id": cid, "options": json.loads(options_to_json(opts))}


def _verify_assertion(admin, challenge: bytes, credential: dict, rp_id: str,
                      origin: str) -> dict:
    """Shared tail of login and step-up: find the credential, verify the
    signature, bump the sign count. Returns the credential row."""
    raw = str(credential.get("rawId") or "")
    cred_id = _norm_cred_id(raw) or _norm_cred_id(str(credential.get("id") or ""))
    if cred_id:
        credential = {**credential, "id": cred_id,
                      "rawId": _norm_cred_id(raw) or cred_id}
    row = admin.execute(
        """SELECT id, label, public_key, sign_count FROM admin_credentials
           WHERE rtrim(replace(replace(credential_id, '+', '-'), '/', '_'),
                       '=') = %s""", (cred_id,)).fetchone()
    if row is None:
        raise ValueError("unknown passkey")
    v = verify_authentication_response(
        credential=credential, expected_challenge=challenge,
        expected_rp_id=rp_id, expected_origin=_origins(origin),
        credential_public_key=base64url_to_bytes(row["public_key"]),
        credential_current_sign_count=row["sign_count"],
        # same stance as enrolment: the assertion stands alone, so it must
        # carry user verification
        require_user_verification=True)
    admin.execute(
        "UPDATE admin_credentials SET sign_count = %s, last_used = now() "
        "WHERE id = %s", (v.new_sign_count, row["id"]))
    return row


def login_verify(admin, challenge_id: str, credential: dict, rp_id: str,
                 origin: str) -> dict:
    """Returns {id, label} of the credential that signed in."""
    challenge = _take_challenge(admin, challenge_id, "login")
    if challenge is None:
        raise ValueError("challenge expired — try again")
    row = _verify_assertion(admin, challenge, credential, rp_id, origin)
    return {"id": str(row["id"]), "label": row["label"]}


# ---- step-up (destructive host-agent commands) ----------------------------


def stepup_options(admin, session_hash: str, rp_id: str) -> dict:
    """A fresh assertion at the moment of a destructive click, independent
    of how old the console session is. Bound to that session."""
    if count(admin) == 0:
        raise ValueError("no operator passkeys enrolled")
    opts = generate_authentication_options(
        rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED)
    cid = _store_challenge(admin, "stepup", opts.challenge, session_hash)
    return {"challenge_id": cid, "options": json.loads(options_to_json(opts))}


def stepup_verify(admin, session_hash: str, challenge_id: str,
                  credential: dict, rp_id: str, origin: str) -> str:
    """Verify the assertion and mint a short-lived single-use ticket the
    destructive form carries. The ticket is stored HASHED — a read of the
    table during its 5-minute life must not be redeemable."""
    challenge = _take_challenge(admin, challenge_id, "stepup", session_hash)
    if challenge is None:
        raise ValueError("challenge expired — try again")
    _verify_assertion(admin, challenge, credential, rp_id, origin)
    ticket = TICKET_PREFIX + secrets.token_urlsafe(24)
    admin.execute(
        """INSERT INTO admin_challenges (purpose, challenge, session_hash,
                                         expires_at)
           VALUES ('ticket', %s, %s, %s)""",
        (_h(ticket), session_hash,
         dt.datetime.now(dt.timezone.utc) + TICKET_TTL))
    return ticket


# ---- enrolment tickets (host-issued, the passkey-only recovery) ----------
#
# On a passkey-only console there is no token to fall back to, so "I lost my
# key" cannot be answered by removing credentials — that recovers to nothing.
# It is answered by `oikonome admin-enrol` on the HOST, which mints one of
# these and prints a URL. Same shape as the install wizard's setup token and
# the password-reset link: single-use, short-lived, hashed at rest, and it
# can only be minted by someone who already controls the box.

ENROL_TICKET_TTL = dt.timedelta(minutes=15)
ENROL_PREFIX = "adminenrol-"


def mint_enrol_ticket(admin, minutes: int = 0) -> str:
    ttl = dt.timedelta(minutes=minutes) if minutes else ENROL_TICKET_TTL
    ticket = ENROL_PREFIX + secrets.token_urlsafe(24)
    admin.execute(
        """INSERT INTO admin_challenges (purpose, challenge, expires_at)
           VALUES ('enrol-ticket', %s, %s)""",
        (_h(ticket), dt.datetime.now(dt.timezone.utc) + ttl))
    return ticket


def enrol_ticket_valid(admin, ticket: str) -> bool:
    """Peek WITHOUT burning — the ticket has to survive the page render and
    the options call, and is spent only when a key actually lands."""
    if not ticket or not ticket.startswith(ENROL_PREFIX):
        return False
    return admin.execute(
        """SELECT 1 FROM admin_challenges
           WHERE purpose = 'enrol-ticket' AND challenge = %s
             AND expires_at > now()""", (_h(ticket),)).fetchone() is not None


def burn_enrol_ticket(admin, ticket: str) -> bool:
    if not ticket or not ticket.startswith(ENROL_PREFIX):
        return False
    return admin.execute(
        """DELETE FROM admin_challenges
           WHERE purpose = 'enrol-ticket' AND challenge = %s
             AND expires_at > now() RETURNING id""",
        (_h(ticket),)).fetchone() is not None


def redeem_ticket(admin, session_hash: str, ticket: str) -> bool:
    """Single-use, unexpired, minted by THIS session's own assertion."""
    if not ticket or not ticket.startswith(TICKET_PREFIX):
        return False
    return admin.execute(
        """DELETE FROM admin_challenges
           WHERE purpose = 'ticket' AND challenge = %s
             AND session_hash = %s AND expires_at > now()
           RETURNING id""", (_h(ticket), session_hash)).fetchone() is not None
