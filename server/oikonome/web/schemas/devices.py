"""The phones signed in to an account: minting a device token, the
widget's lesser token, native push routing, the device roster and
signing one out (web/app.py)."""

from __future__ import annotations

from typing import Literal

from . import Shape
from .access import StepUp


class DeviceMint(StepUp):
    """POST /api/devices: a native app trades its login's `mint_ticket`
    for a device token; without one the session cookie stands in, with the
    step-up proof an older client sends. `replace` names a device to sign
    out in the same act, at the device ceiling."""
    mint_ticket: str | None = None
    device_name: str | None = None
    platform: str | None = None
    replace: str | None = None


class DeviceMinted(Shape):
    """The device token, shown this once."""
    token: str
    id: str
    device_name: str


class DeviceMintAbandon(Shape):
    """POST /api/devices/abandon: the ticket of a sign-in the phone will
    not finish."""
    mint_ticket: str | None = None


class MobileDevice(Shape):
    id: str
    device_name: str
    platform: str
    created_at: str
    last_seen: str | None
    current: bool
    """The device asking."""
    push: Literal["on", "dead"] | None
    """Native push: on, a token the platform stopped accepting, or never
    registered."""


class MobileDevices(Shape):
    """GET /api/devices: this login's live devices; `hidden` (and none
    listed) on a demonstration instance and for the shared demonstration
    login."""
    devices: list[MobileDevice]
    hidden: bool = False


class WidgetToken(Shape):
    """POST /api/devices/widget: the home-screen widget's token, shown
    this once."""
    token: str


class DevicePushSet(Shape):
    """POST /api/devices/push: the platform's routing token (fcm or
    apns), or null to clear it."""
    token: str | None = None
    platform: str | None = None


class DevicePushSaved(Shape):
    ok: Literal[True]
    registered: bool


class DeviceRevoke(StepUp):
    """POST /api/devices/revoke: a device signing itself out needs no
    proof; any other takes the step-up."""
    id: str | None = None


class DeviceRevoked(Shape):
    ok: Literal[True]
    revoked: int
