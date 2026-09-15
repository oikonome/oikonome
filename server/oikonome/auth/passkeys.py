"""Passkeys — WebAuthn sign-in as an optional per-user upgrade.
Email+password stays the floor; passkeys are nicer and
phishing-resistant for browsers that have them.

Caveat the UI surfaces too: WebAuthn only exists in SECURE contexts —
https or localhost. A plain-http LAN install (http://10.0.0.x) can
enroll nothing; the card explains instead of failing cryptically.

Challenges live server-side (webauthn_challenges, 10-minute TTL,
single-use) between the options call and the browser's response."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re

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
RP_NAME = "Oikonome"

# The native apps this server trusts, as the platforms name them. Both are
# an operator's own: the package/bundle id and signing certificates of the
# build THEY distribute (the public mobile tree carries placeholders). A
# self-hosted server with no app of its own publishes empty trust
# statements, which is the honest answer — an Android or iOS app can only
# sign in with a passkey against a server that names it.
#
# OIKONOME_ANDROID_PACKAGE — the app's application id.
# OIKONOME_ANDROID_CERT_SHA256 — comma-separated colon-hex SHA-256 signing
#   certificate fingerprints. Public identifiers, not secrets: anyone
#   holding the APK can read them off its signature. List every key the
#   app may be signed with (a store's app-signing key, the upload key for
#   sideloads) — either form must pass Digital Asset Links and the WebAuthn
#   origin check.
# OIKONOME_IOS_APP_IDS — comma-separated "<Team ID>.<bundle id>".
_PACKAGE_RE = re.compile(r"[a-zA-Z][\w]*(\.[a-zA-Z][\w]*)+")
_FINGERPRINT_RE = re.compile(r"^([0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2}$")


def _csv_env(name: str) -> list[str]:
    return [p.strip() for p in (os.environ.get(name) or "").split(",")
            if p.strip()]


def android_package() -> str | None:
    p = (os.environ.get("OIKONOME_ANDROID_PACKAGE") or "").strip()
    return p if p and _PACKAGE_RE.fullmatch(p) else None


def android_cert_fingerprints() -> list[str]:
    """The configured fingerprints, upper-cased; a malformed entry is
    dropped rather than published — this text ends up in a trust
    statement the platform enforces."""
    return [fp.upper() for fp in _csv_env("OIKONOME_ANDROID_CERT_SHA256")
            if _FINGERPRINT_RE.match(fp)]


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def android_app_origins() -> list[str]:
    """WebAuthn origins Android's Credential Manager signs for a native
    app: ``android:apk-key-hash:<base64url(sha256 of the signing cert)>``.
    The colon-hex fingerprint IS that sha256, so decoding it yields the
    digest bytes directly — no re-hashing."""
    return ["android:apk-key-hash:"
            + _b64u(bytes.fromhex(fp.replace(":", "")))
            for fp in android_cert_fingerprints()]


def android_packages() -> list[str]:
    """Every Android package this server trusts, the shipping app first.

    Credential Manager gates on the PACKAGE as well as the signing cert,
    so a build that renames the application id — a side-by-side dev
    variant, which carries a different package precisely so it can sit
    next to the store app on one phone — is refused even though it is
    signed with a key already listed here, and the person is told to
    "sign in another way" with no hint that the server is the one
    declining. Extra packages are env-driven and empty by default:
    naming a second app in the statement is a real trust grant, and a
    production host should have to say so deliberately rather than
    inherit one because a dev instance needed it. No configured app at
    all means no package — and no statement."""
    main = android_package()
    if main is None:
        return []
    # a package name, not arbitrary text: this ends up in a published
    # trust statement
    ok = [p for p in _csv_env("OIKONOME_ANDROID_EXTRA_PACKAGES")
          if p != main and _PACKAGE_RE.fullmatch(p)]
    return [main] + ok


def android_assetlinks() -> list[dict]:
    """Digital Asset Links statement for /.well-known/assetlinks.json.
    ``get_login_creds`` is what lets Credential Manager offer this
    server's passkeys to the app; ``handle_all_urls`` covers app links.
    One statement per trusted package, all carrying the same certificate
    list — every build of one operator's app is signed with one of the
    keys they configured."""
    certs = android_cert_fingerprints()
    return [{
        "relation": ["delegate_permission/common.handle_all_urls",
                     "delegate_permission/common.get_login_creds"],
        "target": {"namespace": "android_app",
                   "package_name": pkg,
                   "sha256_cert_fingerprints": certs},
    } for pkg in android_packages()]


# The iOS app, as Apple names it: Team ID + bundle id, from
# OIKONOME_IOS_APP_IDS. Apple's associated-domains check fetches the file
# below through Apple's CDN before the system will offer this domain's
# passkeys inside the app — and the app can only list domains at BUILD
# time (mobile/app.json ios.associatedDomains), so the server and the
# build have to name each other.
_IOS_APP_ID_RE = re.compile(r"^[A-Z0-9]{10}\.[a-zA-Z][\w]*(\.[a-zA-Z][\w]*)+$")


def ios_app_ids() -> list[str]:
    return [a for a in _csv_env("OIKONOME_IOS_APP_IDS") if _IOS_APP_ID_RE.match(a)]


def apple_app_site_association() -> dict:
    """Apple App Site Association for
    /.well-known/apple-app-site-association — the iOS counterpart of
    ``android_assetlinks``. ``webcredentials`` is the only service the
    app needs: it is what lets the passkey sheet offer this domain's
    credentials (sign-in and step-up alike). Like the Android statement
    it grants nothing by itself; the WebAuthn verify still runs."""
    return {"webcredentials": {"apps": ios_app_ids()}}


def _expected_origins(origin: str) -> list[str]:
    """Origins an assertion may claim, given the configured/derived one.

    The https twin is always accepted: a reverse proxy that loses
    X-Forwarded-Proto makes the server derive http:// while the browser
    signed the real https:// page origin, and that mismatch would brick
    logins. The http form is accepted only for localhost/127.0.0.1
    (dev): browsers refuse WebAuthn on any other insecure origin (and
    passkeys need a secure origin besides), so a server-side
    http:// accept for a public host can never admit a legitimate login
    — it can only weaken downgrade resistance.

    The Android app's origins are always in the set: a native app has no
    page origin, so Credential Manager signs the app's identity (package
    signing cert) instead, and it still proves possession of one of the
    configured certs — trusting it is exactly as strong as trusting the https page
    origin."""
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
    return (out or [origin]) + android_app_origins()


def _store_challenge(conn, purpose: str, challenge: bytes,
                     user_id=None) -> str:
    row = conn.execute(
        """INSERT INTO webauthn_challenges (purpose, user_id, challenge,
                                            expires_at)
           VALUES (%s,%s,%s,%s) RETURNING id""",
        (purpose, user_id, _b64u(challenge),
         dt.datetime.now(dt.timezone.utc) + CHALLENGE_TTL)).fetchone()
    # opportunistic sweep so the table never accumulates
    conn.execute("DELETE FROM webauthn_challenges WHERE expires_at < now()")
    return str(row["id"])


def _take_challenge(conn, challenge_id: str, purpose: str,
                    user_id=None) -> bytes | None:
    """Single use: read + delete atomically-enough for our purposes.
    With user_id, the challenge must have been minted FOR that user —
    step-up assertions are bound to the session's account."""
    row = conn.execute(
        """DELETE FROM webauthn_challenges
           WHERE id = %s::uuid AND purpose = %s AND expires_at > now()
             AND (%s::uuid IS NULL OR user_id = %s::uuid)
           RETURNING challenge, user_id""",
        (challenge_id, purpose, user_id, user_id)).fetchone()
    if row is None:
        return None
    return base64url_to_bytes(row["challenge"])


def register_options(conn, user_id, email: str, rp_id: str) -> dict:
    existing = [PublicKeyCredentialDescriptor(
        id=base64url_to_bytes(r["credential_id"]))
        for r in conn.execute(
            "SELECT credential_id FROM passkeys WHERE user_id = %s",
            (user_id,)).fetchall()]
    opts = generate_registration_options(
        rp_id=rp_id, rp_name=RP_NAME,
        user_id=str(user_id).encode(), user_name=email,
        exclude_credentials=existing,
        authenticator_selection=AuthenticatorSelectionCriteria(
            # REQUIRED so usernameless login works (no email on the login
            # form). PREFERRED leaves some authenticators without a resident
            # credential, which forces "enter email first" client-side.
            resident_key=ResidentKeyRequirement.REQUIRED,
            # REQUIRED, not PREFERRED — a passkey sign-in can skip
            # TOTP entirely, so for a finance app the key itself must prove
            # the user (PIN/biometric), not just possession of the device
            user_verification=UserVerificationRequirement.REQUIRED))
    cid = _store_challenge(conn, "register", opts.challenge, user_id)
    return {"challenge_id": cid, "options": json.loads(options_to_json(opts))}


def register_verify(conn, user_id, challenge_id: str, credential: dict,
                    rp_id: str, origin: str, label: str = "") -> dict:
    challenge = _take_challenge(conn, challenge_id, "register")
    if challenge is None:
        raise ValueError("challenge expired — try adding the passkey again")
    # options are advisory — a tampering client could strip the
    # UV flag, so REQUIRED is enforced here, on the authenticator's signed
    # response, or it isn't enforced at all
    # accept the same origin set login and step-up accept (the http/https
    # twin for a proxy that drops X-Forwarded-Proto, plus the Android
    # apk-key-hash origins). With a single literal origin here, a
    # misconfigured proxy would reject every enrollment while a later login
    # against the SAME setup succeeded via the twin — passkeys that can sign
    # in but can never be added.
    v = verify_registration_response(
        credential=credential, expected_challenge=challenge,
        expected_rp_id=rp_id, expected_origin=_expected_origins(origin),
        require_user_verification=True)
    transports = ",".join(credential.get("response", {})
                          .get("transports", []) or [])
    row = conn.execute(
        """INSERT INTO passkeys (user_id, credential_id, public_key,
                                 sign_count, transports, label)
           VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
        (user_id, _b64u(v.credential_id), _b64u(v.credential_public_key),
         v.sign_count, transports, label.strip()[:60])).fetchone()
    return {"id": str(row["id"])}


# How many credential descriptors an email-scoped login challenge always
# carries. The allow-list is padded to this width with decoys, so its LENGTH
# says nothing: a stranger, an account with no passkey, and an account with
# three all answer with the same number of entries. Four covers the realistic
# enrollment ceiling (phone + laptop + two security keys); an account past it
# is returned in full — never truncated, because a credential missing from
# the list is a key that can no longer sign in.
_ALLOW_WIDTH = 4


# Byte lengths a decoy credential id may take, weighted toward what real
# authenticators mint: compact platform-passkey ids (Apple ~20, many CTAP2
# keys 16-32) are common, wide resident-key handles (Windows Hello, some
# security keys) run 64+. Eight entries so a single seed byte indexes it
# without modulo bias.
_DECOY_SIZES = (16, 20, 20, 32, 32, 32, 64, 96)


def _decoy_credential(email: str, index: int = 0) -> bytes:
    """A deterministic fake credential id, so login_options has the SAME
    shape for every address — an empty, absent, or short allowCredentials tells
    an attacker which emails have accounts and how many keys they hold. Keyed
    on the install master key so the decoy isn't recomputable off-box;
    deterministic so repeat queries can't be diffed. No authenticator will ever
    match one — the browser just reports no usable passkey, same as a wrong
    device would. The byte length is drawn from _DECOY_SIZES by the same
    per-(email, index) seed, never from the account's real credentials: sizing
    decoys off a real credential would make any different-length real key the
    visible outlier in a mixed-authenticator account, and give credential-less
    addresses a uniform four-of-a-kind no multi-vendor account would show."""
    import os
    seed = (os.environ.get("OIKONOME_MASTER_KEY") or "oikonome").encode()
    base = seed + b"|passkey-decoy|" + email.strip().lower().encode() \
        + b"|" + str(index).encode()
    size = _DECOY_SIZES[hashlib.sha256(base + b"|size").digest()[0]
                        % len(_DECOY_SIZES)]
    out = b""
    while len(out) < size:                    # chain for ids wider than sha256
        out += hashlib.sha256(base + b"|" + str(len(out)).encode()).digest()
    return out[:size]


def _pad_allow(email: str, real: list[bytes]) -> list[bytes]:
    """Real credentials plus enough decoys to reach the fixed width, in an
    order derived from the email — so neither the count, the position, nor
    the byte-length pattern of a real credential is readable off the
    response. Decoy lengths depend on the email alone (see
    _decoy_credential), so every address — stranger or enrolled — shows an
    identically-derived, plausibly mixed set of lengths for real
    credentials to hide among."""
    out = list(real)
    for i in range(len(real), _ALLOW_WIDTH):
        out.append(_decoy_credential(email, i))
    key = email.strip().lower().encode()
    return sorted(out, key=lambda c: hashlib.sha256(key + b"|" + c).digest())


def login_options(conn, rp_id: str, email: str = "") -> dict:
    """With an email: that user's credentials as allow-list, padded with
    decoys to a fixed width so the response is the same shape for every
    address. Without an email: discoverable-credential flow (the
    authenticator picks)."""
    allow = None
    if email.strip():
        real = [base64url_to_bytes(r["credential_id"])
                for r in conn.execute(
                    """SELECT p.credential_id FROM passkeys p
                       JOIN users u ON u.id = p.user_id
                       WHERE u.email = %s
                       ORDER BY p.created_at, p.id""",
                    (email.strip().lower(),)).fetchall()]
        allow = [PublicKeyCredentialDescriptor(id=cid)
                 for cid in _pad_allow(email, real)]
    opts = generate_authentication_options(
        rp_id=rp_id, allow_credentials=allow,
        # same as registration — passkey login bypasses password
        # AND TOTP, so it must carry its own second factor (UV)
        user_verification=UserVerificationRequirement.REQUIRED)
    cid = _store_challenge(conn, "login", opts.challenge)
    return {"challenge_id": cid, "options": json.loads(options_to_json(opts))}


def stepup_options(conn, user_id, rp_id: str) -> dict:
    """Inline re-assertion for a destructive posture change — the
    signed-in user's OWN credentials as the allow-list. Mirrors
    login_options but binds the challenge to the user so a parallel
    session can't complete someone else's step-up."""
    allow = [PublicKeyCredentialDescriptor(
        id=base64url_to_bytes(r["credential_id"]))
        for r in conn.execute(
            "SELECT credential_id FROM passkeys WHERE user_id = %s",
            (user_id,)).fetchall()]
    if not allow:
        raise ValueError("no passkeys enrolled")
    opts = generate_authentication_options(
        rp_id=rp_id, allow_credentials=allow,
        # same stance as login: the assertion IS the second factor, so it
        # must carry user verification
        user_verification=UserVerificationRequirement.REQUIRED)
    cid = _store_challenge(conn, "stepup", opts.challenge, user_id)
    return {"challenge_id": cid, "options": json.loads(options_to_json(opts))}


STEPUP_TICKET_TTL = dt.timedelta(minutes=5)


def _h(ticket: str) -> str:
    """sha256 of a bearer ticket for at-rest storage: the step-up ticket is a bearer credential like a session or
    a reset token, so it must be hashed at rest (mirrors reset._h). A DB read
    primitive during the 5-min TTL can no longer redeem it against the exact
    destructive endpoints step-up exists to gate."""
    return hashlib.sha256(ticket.encode()).hexdigest()


def stepup_verify(conn, user_id, challenge_id: str, credential: dict,
                  rp_id: str, origin: str) -> str:
    """Verify the assertion (bound to this user) and mint a short-lived
    single-use step-up ticket. The ticket rides the SAME field the
    recovery code does, so the dozen destructive endpoints need no new
    parameters — _require_passkey_stepup redeems either."""
    challenge = _take_challenge(conn, challenge_id, "stepup", user_id)
    if challenge is None:
        raise ValueError("challenge expired — try again")
    raw = str(credential.get("rawId") or "")
    cred_id = _norm_cred_id(raw) or _norm_cred_id(
        str(credential.get("id") or ""))
    if cred_id:
        credential = {**credential, "id": cred_id,
                      "rawId": _norm_cred_id(raw) or cred_id}
    row = conn.execute(
        """SELECT id, public_key, sign_count FROM passkeys
           WHERE rtrim(replace(replace(credential_id, '+', '-'), '/', '_'),
                       '=') = %s AND user_id = %s""",
        (cred_id, user_id)).fetchone()
    if row is None:
        raise ValueError("unknown passkey")
    v = verify_authentication_response(
        credential=credential, expected_challenge=challenge,
        expected_rp_id=rp_id, expected_origin=_expected_origins(origin),
        credential_public_key=base64url_to_bytes(row["public_key"]),
        credential_current_sign_count=row["sign_count"],
        require_user_verification=True)
    conn.execute(
        "UPDATE passkeys SET sign_count = %s, last_used = now() "
        "WHERE id = %s", (v.new_sign_count, row["id"]))
    import secrets as _secrets
    ticket = "pkstep-" + _secrets.token_urlsafe(24)
    # store the HASH, return the RAW ticket (it goes to the client).
    # The ticket also remembers WHICH credential signed this assertion: a
    # credential rotation keeps the key that proved the caller is present
    # and evicts every other, and "the key that proved it" has to be an
    # identity, not a guess from recency — an attacker who plants a key can
    # keep it as freshly used as the owner's.
    conn.execute(
        """INSERT INTO webauthn_challenges (purpose, user_id, challenge,
                                            expires_at, passkey_id)
           VALUES ('stepup-ticket', %s, %s, %s, %s)""",
        (user_id, _h(ticket),
         dt.datetime.now(dt.timezone.utc) + STEPUP_TICKET_TTL, row["id"]))
    return ticket


def redeem_stepup_ticket(conn, user_id, ticket: str) -> tuple[bool, str | None]:
    """Single-use, unexpired, minted for THIS user by stepup_verify. The
    row stores sha256(ticket), so match on the hash.

    Returns (redeemed, passkey_id) — the credential that signed the
    assertion behind this ticket. The id is separate from the boolean on
    purpose: a ticket minted before the id was recorded still redeems, and
    a missing id must read as "keep no key", never as a failed step-up.
    """
    if not ticket.startswith("pkstep-"):
        return False, None
    row = conn.execute(
        """DELETE FROM webauthn_challenges
           WHERE purpose = 'stepup-ticket' AND user_id = %s
             AND challenge = %s AND expires_at > now()
           RETURNING id, passkey_id""",
        (user_id, _h(ticket))).fetchone()
    if row is None:
        return False, None
    return True, (str(row["passkey_id"]) if row["passkey_id"] else None)


def _norm_cred_id(s: str) -> str:
    """Normalize a base64url credential id for lookup (strip padding,
    standard→urlsafe alphabet). Browser `id` and what we stored at enroll
    should already match; this absorbs padding / alphabet drift."""
    if not s:
        return ""
    t = s.strip().replace("+", "-").replace("/", "_").rstrip("=")
    return t


def login_verify(conn, challenge_id: str, credential: dict,
                 rp_id: str, origin: str) -> dict:
    """Returns {user_id, tenant_id} on success; raises ValueError on any
    user-facing failure."""
    challenge = _take_challenge(conn, challenge_id, "login")
    if challenge is None:
        raise ValueError("challenge expired — try again")
    # Prefer rawId (canonical bytes→b64u) when present; fall back to id.
    # py_webauthn also requires bytes_to_base64url(raw_id) == id.
    raw = str(credential.get("rawId") or "")
    cred_id = _norm_cred_id(raw) or _norm_cred_id(
        str(credential.get("id") or ""))
    if raw and credential.get("id"):
        # force id to match rawId encoding so parse doesn't reject the pair
        credential = {**credential, "id": _norm_cred_id(raw) or cred_id,
                      "rawId": _norm_cred_id(raw) or raw}
    elif cred_id:
        credential = {**credential, "id": cred_id,
                      "rawId": _norm_cred_id(raw) or cred_id}
    row = conn.execute(
        """SELECT p.id, p.user_id, p.public_key, p.sign_count,
                  u.tenant_id
           FROM passkeys p JOIN users u ON u.id = p.user_id
           WHERE rtrim(replace(replace(p.credential_id, '+', '-'), '/', '_'),
                       '=') = %s""", (cred_id,)).fetchone()
    if row is None:
        raise ValueError("unknown passkey")
    # enforce UV on the signed assertion, not just in the options
    # (see register_verify) — without this a UV=0 assertion still logs in
    # browsers always sign the real page origin; the accepted set is
    # https-only outside localhost (see _expected_origins)
    v = verify_authentication_response(
        credential=credential, expected_challenge=challenge,
        expected_rp_id=rp_id, expected_origin=_expected_origins(origin),
        credential_public_key=base64url_to_bytes(row["public_key"]),
        credential_current_sign_count=row["sign_count"],
        require_user_verification=True)
    conn.execute(
        "UPDATE passkeys SET sign_count = %s, last_used = now() "
        "WHERE id = %s", (v.new_sign_count, row["id"]))
    return {"user_id": row["user_id"], "tenant_id": row["tenant_id"]}
