"""Script tokens and consented support access (web/api_access.py)."""

from __future__ import annotations

from typing import Literal

from . import Shape

TokenScope = Literal["push", "read"]


class ApiToken(Shape):
    id: str
    name: str
    created_at: str
    last_used_at: str | None
    revoked_at: str | None
    scope: TokenScope


class ApiTokens(Shape):
    """GET /api/tokens: every token the household minted, revoked ones
    included."""
    tokens: list[ApiToken]


class ApiTokenMinted(Shape):
    """POST /api/tokens: the plaintext is shown this once."""
    token: str
    id: str
    name: str
    created_at: str
    scope: TokenScope


class StepUp(Shape):
    """The step-up proof a door's body may carry instead of a fresh
    elevation beforehand (older clients send it; current ones step up
    first)."""
    password: str | None = None
    totp_code: str | None = None
    recovery_code: str | None = None


class ApiTokenMint(StepUp):
    """POST /api/tokens; scope defaults to push."""
    name: str | None = None
    scope: TokenScope | None = None


class ApiTokenRevoke(StepUp):
    id: str


class SupportAccess(Shape):
    """GET /api/support-access: the live grant, if any."""
    granted: bool
    expires_at: str | None
    reason: str | None


class SupportAccessGrant(StepUp):
    """POST /api/support-access: hours 1-168 (digits in a string read the
    same), 24 when left out."""
    hours: int | str = 24
    reason: str = ""


class SupportAccessGranted(Shape):
    ok: Literal[True]
    hours: int
