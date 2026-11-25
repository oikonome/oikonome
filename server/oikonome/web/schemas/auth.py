"""Sign-in, session, second-factor and passkey doors (web/app.py).

These describe what the doors already send; nothing here changes how a
sign-in, a session or a factor is handled.
"""

from __future__ import annotations

from typing import Literal

from . import Shape
from .settings import RemovedMember


class RotationReport(Shape):
    """What a credential, factor or sheet rotation closed: every key,
    always, whichever door rotated (auth/posture.blank_report)."""
    other_sessions_signed_out: int
    devices_revoked: int
    tokens_revoked: int
    webhooks_paused: int
    invites_removed: int
    members_removed: list[RemovedMember]
    support_access_revoked: int
    sms_number_needs_verify: bool
    passkeys_removed: int
    passkeys_kept: int
    recovery_codes: list[str] | None
    """The sheet that now stands, when the rotation reissued it."""


class SignedIn(Shape):
    """POST /api/signup, /api/login, /api/login/passkey, /api/invite/claim:
    the session rides the cookie; a native app, which cannot read it,
    trades the one-shot ticket for its device token."""
    ok: Literal[True]
    mint_ticket: str


class LogoutBody(Shape):
    """POST /api/logout: this browser's own web-push endpoint, so its
    subscription goes with the session."""
    push_endpoint: str | None = None


class AccessInfo(Shape):
    """GET /api/access, read before any sign-in: how someone without an
    account gets one here, and where a locked-out person can write."""
    hosted: bool
    signup_open: bool
    signup_path: str | None
    request_access_open: Literal[False]
    """Kept, always false, for older builds that read it."""
    request_access_path: None
    """Kept, always null, for older builds that read it."""
    site_url: str | None
    support_email: str | None
    android_test_url: str | None


class DemoLogin(Shape):
    """GET /api/demo-login: credentials only on a single-account
    demonstration instance."""
    demo: bool
    email: str = ""
    password: str = ""


class RecoveryCancelled(Shape):
    """POST /api/recovery/cancel-pending: how many pending recoveries the
    click stopped."""
    ok: Literal[True]
    cancelled: int


class VerifyResent(Shape):
    """POST /api/verify-email/resend: `verified` true means nothing was
    left to do and nothing was sent."""
    ok: Literal[True]
    verified: bool


class PasswordChangeBody(Shape):
    current_password: str | None = None
    new_password: str | None = None
    totp_code: str | None = None
    """For a server that still asks for a code on this form."""
    recovery_code: str | None = None
    reissue_codes: bool | str | None = None
    """Set by a client that shows a reissued recovery sheet; an older one
    leaves it out and keeps the sheet it has."""
    keep_push_endpoint: str | None = None


class PasswordChanged(RotationReport):
    ok: Literal[True]


ElevationMethod = Literal["passkey", "password", "recovery+password"]


class Elevation(Shape):
    """GET /api/auth/elevation and POST /api/auth/elevate: is this session
    inside its window, and which proofs open it."""
    elevated: bool
    until: str | None
    methods: list[ElevationMethod]
    totp: bool


class ElevateBody(Shape):
    """One of: password (+ totp_code on a TOTP account); password +
    recovery_code (a lost factor); stepup_ticket (a passkey assertion)."""
    password: str | None = None
    totp_code: str | None = None
    recovery_code: str | None = None
    stepup_ticket: str | None = None


class TotpEnrollment(Shape):
    """POST /api/totp/enroll: the new secret and its QR code (an SVG data
    URI)."""
    secret: str
    qr: str


class TotpConfirmed(RotationReport):
    ok: Literal[True]


class RecoveryRegenerated(RotationReport):
    ok: Literal[True]
    recovery_codes: list[str]
    """The fresh sheet: this rotation always mints one."""


class TotpDisabled(Shape):
    """POST /api/totp/disable: the rotation's report, or — when no
    authenticator was enrolled — only `sms_number_needs_verify`."""
    ok: Literal[True]
    sms_number_needs_verify: bool
    other_sessions_signed_out: int = 0
    devices_revoked: int = 0
    tokens_revoked: int = 0
    webhooks_paused: int = 0
    invites_removed: int = 0
    members_removed: list[RemovedMember] = []
    support_access_revoked: int = 0
    passkeys_removed: int = 0
    passkeys_kept: int = 0
    recovery_codes: list[str] | None = None


class StepUpBody(Shape):
    """The step-up proof an older client sends in a body (current ones
    open the elevation window first)."""
    password: str | None = None
    totp_code: str | None = None
    recovery_code: str | None = None


# --- WebAuthn ceremonies (the verifier library's JSON, as it sends it) --

class WebAuthnRelyingParty(Shape):
    name: str
    id: str = ""


class WebAuthnUser(Shape):
    id: str
    name: str
    displayName: str


class WebAuthnCredParam(Shape):
    type: str
    alg: int


class WebAuthnCredentialRef(Shape):
    id: str
    type: str
    transports: list[str] = []


class WebAuthnAuthenticatorSelection(Shape):
    authenticatorAttachment: str = ""
    residentKey: str = ""
    requireResidentKey: bool = False
    userVerification: str = ""


class WebAuthnCreationOptions(Shape):
    """PublicKeyCredentialCreationOptions in its JSON form."""
    rp: WebAuthnRelyingParty
    user: WebAuthnUser
    challenge: str
    pubKeyCredParams: list[WebAuthnCredParam]
    timeout: int = 0
    excludeCredentials: list[WebAuthnCredentialRef] = []
    authenticatorSelection: WebAuthnAuthenticatorSelection | None = None
    attestation: str = ""
    hints: list[str] = []


class WebAuthnRequestOptions(Shape):
    """PublicKeyCredentialRequestOptions in its JSON form."""
    challenge: str
    timeout: int = 0
    rpId: str = ""
    allowCredentials: list[WebAuthnCredentialRef] = []
    userVerification: str = ""


class PasskeyRegisterOptions(Shape):
    """POST /api/passkeys/options: the registration challenge."""
    challenge_id: str
    options: WebAuthnCreationOptions


class PasskeyAssertOptions(Shape):
    """POST /api/login/passkey/options and /api/stepup/passkey/options:
    the assertion challenge."""
    challenge_id: str
    options: WebAuthnRequestOptions


class PasskeyOptionsBody(Shape):
    """POST /api/passkeys/options: an older client's password (and code)
    up front; a current one opens the elevation window first."""
    password: str | None = None
    totp_code: str | None = None


class PasskeyRegisterBody(Shape):
    challenge_id: str
    credential: dict
    """The authenticator's attestation, as the browser API returns it."""
    label: str | None = None
    password: str | None = None
    totp_code: str | None = None
    recovery_code: str | None = None


class PasskeyAdded(Shape):
    """POST /api/passkeys: the new key's id, and — when it was the
    account's first strong factor — the rotation that came with it."""
    id: str
    other_sessions_signed_out: int = 0
    devices_revoked: int = 0
    tokens_revoked: int = 0
    webhooks_paused: int = 0
    invites_removed: int = 0
    members_removed: list[RemovedMember] = []
    support_access_revoked: int = 0
    sms_number_needs_verify: bool = False
    passkeys_removed: int = 0
    passkeys_kept: int = 0
    recovery_codes: list[str] | None = None


class PasskeyRow(Shape):
    id: str
    label: str
    transports: str
    """Comma-joined transport hints."""
    created_at: str
    last_used: str | None


class Passkeys(Shape):
    """GET /api/passkeys: this account's keys."""
    passkeys: list[PasskeyRow]


class PasskeyDeleteBody(StepUpBody):
    reissue_codes: bool | str | None = None
    """Set by a client that shows a reissued recovery sheet."""


class PasskeyRemoved(RotationReport):
    """DELETE /api/passkeys/{id}; `signed_out` when the caller's own
    session was opened by that key and ended with it."""
    ok: Literal[True]
    signed_out: bool


class WebAuthnAssertionBody(Shape):
    """POST /api/stepup/passkey and /api/login/passkey."""
    challenge_id: str
    credential: dict
    """The authenticator's assertion, as the browser API returns it."""


class StepUpTicket(Shape):
    """POST /api/stepup/passkey: a five-minute single-use ticket that
    /api/auth/elevate redeems."""
    stepup_ticket: str


class PasskeyLoginOptionsBody(Shape):
    """Without an email the authenticator picks the account."""
    email: str | None = None


class AuthSession(Shape):
    id: str
    created_at: str
    last_seen: str | None
    expires_at: str | None
    ip: str | None
    user_agent: str
    current: bool


class AuthSessions(Shape):
    """GET /api/sessions; `hidden` when the roster is withheld (a shared
    demonstration login), with no rows."""
    sessions: list[AuthSession]
    hidden: Literal[True] | None = None


class SessionRevokeBody(StepUpBody):
    """`all_others: true` signs out everywhere else; `id` one session."""
    all_others: bool | None = None
    id: str | None = None
    keep_push_endpoint: str | None = None


class SessionsRevoked(Shape):
    ok: Literal[True]
    revoked: int


class InvitePeek(Shape):
    """GET /api/invite/peek: is the link good, for which role, and the
    address it is bound to ("" when it names none)."""
    role: Literal["member", "viewer"]
    label: str
    email: str


class InviteClaimBody(Shape):
    token: str
    email: str
    password: str
