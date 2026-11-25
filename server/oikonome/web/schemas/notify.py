"""Notification channels and the send-now email (web/api_notify.py)."""

from __future__ import annotations

from typing import Literal

from . import Shape
from .access import StepUp


class EmailSent(Shape):
    """POST /api/jobs/email: today's email, sent now."""
    ok: Literal[True]
    subject: str


class NotifyStatus(Shape):
    """GET /api/notify."""
    sms_available: bool
    sms_entitled: bool
    phone: str | None
    """masked"""
    phone_verified: bool
    phone_paused_at: str | None
    phone_pending: str | None
    push_available: bool
    vapid_public_key: str | None
    push_subscribed: bool
    native_push_available: bool
    native_push_devices: int
    invites_need_base_url: bool


class NotifyPhone(StepUp):
    """POST /api/notify/phone; a blank number clears the phone. `consent`
    is true (every program) or the program names."""
    number: str
    consent: bool | list[str] | None = None


class NotifyPhoneSet(Shape):
    ok: Literal[True]
    cleared: bool = False
    sent_to: str = ""
    """the masked number the code went to"""


class NotifyPhoneVerify(Shape):
    code: str


class PushKeys(Shape):
    p256dh: str
    auth: str


class PushSubscribe(StepUp):
    endpoint: str
    keys: PushKeys
    user_agent: str | None = None


class PushUnsubscribe(Shape):
    endpoint: str


class NotifyTest(Shape):
    channel: str


class NotifyTested(Shape):
    ok: Literal[True]
    delivered: int = 0
