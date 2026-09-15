"""TOTP two-factor (RFC 6238) — stdlib only, no dependency. Standard
parameters (SHA-1, 6 digits, 30s step) so Google Authenticator / Aegis /
1Password all work. Verification accepts ±1 step of clock drift.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP = 30
DIGITS = 6


def new_secret() -> str:
    """Base32 secret (what authenticator apps consume)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def provisioning_uri(secret: str, account: str,
                     issuer: str = "Oikonome") -> str:
    return (f"otpauth://totp/{quote(issuer)}:{quote(account)}"
            f"?secret={secret}&issuer={quote(issuer)}"
            f"&algorithm=SHA1&digits={DIGITS}&period={STEP}")


def qr_svg_data_uri(uri: str) -> str:
    """The provisioning URI as a scannable QR, an SVG `data:` URI the client
    drops straight into `<img src>`. Forced light background + dark modules so
    it scans on the app's dark theme; the raw otpauth string is never shown to
    the user (it embeds the seed as plain text). segno is pure-Python."""
    import segno
    return segno.make(uri, error="m").svg_data_uri(
        scale=5, border=2, dark="#000000", light="#ffffff")


def _code(secret: str, counter: int) -> str:
    pad = "=" * (-len(secret) % 8)
    key = base64.b32decode(secret.upper() + pad)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    num = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(num % (10 ** DIGITS)).zfill(DIGITS)


def code_now(secret: str, at: float | None = None) -> str:
    return _code(secret, int((at or time.time()) // STEP))


def verify(secret: str, code: str, at: float | None = None) -> bool:
    """Constant-time compare across ±1 time step (clock drift)."""
    if not secret or not code:
        return False
    code = code.strip().replace(" ", "")
    # compare_digest raises TypeError on non-ASCII str — a
    # pasted 'café' in the code field 500'd the login door instead of
    # answering "wrong code". Digits-only short-circuit keeps the compare
    # total (a TOTP code is always 6 ASCII digits; anything else is wrong
    # by definition, not an error).
    if not code.isascii():
        return False
    counter = int((at or time.time()) // STEP)
    # A malformed base32 secret raises out of b32decode
    # (binascii.Error, a ValueError). The enrollment confirm door takes
    # the secret from the client, so garbage there must answer "wrong
    # code", never a 500 — same reasoning as the non-ASCII short-circuit:
    # it can't possibly match, which is a no, not an error.
    try:
        return any(hmac.compare_digest(_code(secret, counter + d), code)
                   for d in (-1, 0, 1))
    except ValueError:
        return False


def verify_used(secret: str, code: str, last_counter: int | None,
                at: float | None = None) -> int | None:
    """Replay-aware verify: returns the matched
    step-counter, or None if no match OR the matched counter has already
    been used (counter <= last_counter). Callers persist the returned
    counter so a code can't be replayed within its ~90s window."""
    if not secret or not code:
        return None
    code = code.strip().replace(" ", "")
    if not code.isascii():           # keep compare_digest total — see verify()
        return None
    counter = int((at or time.time()) // STEP)
    for d in (-1, 0, 1):
        c = counter + d
        try:
            matched = hmac.compare_digest(_code(secret, c), code)
        except ValueError:
            # un-decodable base32 secret (client-supplied at enrollment
            # confirm) can't match anything — a no, not a 500. See verify().
            return None
        if matched:
            if last_counter is not None and c <= last_counter:
                return None            # replay of an already-consumed code
            return c
    return None
