"""The continuity packet's shapes (web/api_continuity.py)."""

from __future__ import annotations

from typing import Literal

from . import Shape


class Continuity(Shape):
    """GET /api/continuity: the owner's two fields and what the email door
    will accept."""
    note: str
    """the owner's note; "" for anyone else, who reads note_set instead"""
    note_set: bool
    """a note is written, whether or not the caller may read it"""
    note_readable: bool
    """the caller may read and edit the note: the owner, signed in as
    themself"""
    prepared_for: str
    mail_configured: bool
    members_only: bool
    """hosted: only a household member's address may receive it"""
    members: list[str]
    note_max: int


class ContinuitySaved(Shape):
    """PUT /api/continuity: the two fields as saved."""
    ok: Literal[True]
    note: str
    prepared_for: str


class ContinuitySave(Shape):
    """PUT /api/continuity; a field left out is left as it was."""
    note: str | None = None
    prepared_for: str | None = None


class ContinuityEmail(Shape):
    """POST /api/continuity/email. The step-up proof may ride in the body
    instead of a fresh elevation beforehand."""
    to: str
    password: str | None = None
    totp_code: str | None = None
    recovery_code: str | None = None


class ContinuityEmailed(Shape):
    ok: Literal[True]
    to: str
