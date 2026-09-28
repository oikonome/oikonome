"""Password hashing — argon2id, library defaults (tuned, memory-hard)."""

import hashlib
import os

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_ph = PasswordHasher()

# a floor for strength, a ceiling because argon2 hashes whatever it
# is handed — an unbounded password is a free CPU/memory burn per attempt.
MIN_LEN, MAX_LEN = 10, 256


def policy_error(password: str) -> str | None:
    """The length rule every door applies. None when acceptable."""
    if len(password) < MIN_LEN:
        return f"password must be at least {MIN_LEN} characters"
    if len(password) > MAX_LEN:
        return f"password must be at most {MAX_LEN} characters"
    return None


def _pwned_check_on() -> bool:
    # default OFF: self-host and the test suite never make a network call
    # unless an operator opts in with OIKONOME_PWNED_CHECK=1.
    return os.environ.get("OIKONOME_PWNED_CHECK", "").strip().lower() in (
        "1", "true", "yes", "on")


def pwned_count(password: str) -> int | None:
    """Times this password appears in Have I Been Pwned's breach corpus, via
    the k-anonymity range API — only the first 5 chars of the SHA-1 leave this
    host, NEVER the password itself. Returns the count, 0 if unseen, or None on
    any error/timeout (the check FAILS OPEN: a HIBP outage must never block a
    signup or reset)."""
    h = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
    prefix, suffix = h[:5], h[5:]
    try:
        import httpx
        r = httpx.get(
            "https://api.pwnedpasswords.com/range/" + prefix,
            # Add-Padding hides the real bucket size from a network observer
            headers={"Add-Padding": "true",
                     "User-Agent": "oikonome-pwned-check"},
            timeout=3.0)
        r.raise_for_status()
    except Exception:
        return None
    for line in r.text.splitlines():
        parts = line.split(":")
        if len(parts) == 2 and parts[0].strip().upper() == suffix:
            try:
                return int(parts[1].strip())
            except ValueError:
                return None
    return 0


def password_error(password: str) -> str | None:
    """What every password-setting door calls: the length policy, then — when
    OIKONOME_PWNED_CHECK is enabled — a breached-password lookup. Fail-open, so
    a HIBP outage degrades to length-only rather than blocking the door."""
    err = policy_error(password)
    if err:
        return err
    if _pwned_check_on() and (pwned_count(password) or 0) > 0:
        return ("This password has appeared in a known data breach — "
                "please choose a different one.")
    return None


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(hashed: str, password: str) -> bool:
    try:
        return _ph.verify(hashed, password)
    except VerifyMismatchError:
        return False


def needs_rehash(hashed: str) -> bool:
    return _ph.check_needs_rehash(hashed)
