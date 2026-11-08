"""Short one-time codes sent to a destination the user claims (a texted
six-digit code today) — issued, stored and checked in ONE place.

A six-digit code has a million values, so everything that keeps it from
being guessed lives here rather than at each door:

* The stored value is an HMAC keyed by the instance master key, bound to
  the purpose and the subject (tenant). A bare sha256 of a six-digit code
  is reversed by trying all million values, so an export, a backup or a
  read of the settings row would hand the code over.
* The pending record carries its own miss counter. After `MAX_MISSES`
  wrong answers the record is burned: the caller drops it, a new code (and
  a new, separately capped send) is required, and the right code for the
  burned record no longer works.
* Every attempt is also charged to a per-ACCOUNT budget. A per-address
  limiter alone is spent once per address, and an attacker with a routed
  IPv6 block has tens of thousands of addresses.
* The comparison is constant-time (`hmac.compare_digest`).

The record is a plain dict the caller persists inside its own settings
transaction. `check` mutates it (the miss counter) and returns a verdict;
the caller must WRITE the record back (or drop it when burned) BEFORE it
raises the refusal, because raising inside the transaction rolls the
counter back — which is the whole control.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import secrets

DIGITS = 6
TTL = dt.timedelta(minutes=10)
MAX_MISSES = 5
# per-account attempts per hour across every code of one purpose: two full
# burned codes, which the daily send cap already bounds at three codes
ACCOUNT_ATTEMPTS = 10
ACCOUNT_WINDOW_S = 3600

OK = "ok"
WRONG = "wrong"
BURNED = "burned"        # this miss used the last attempt — drop the record
INVALID = "invalid"      # no record, expired, or an old-format record


def _key() -> bytes:
    # No master key is the dev / plaintext mode the rest of the crypto layer
    # also passes through; a fixed domain key keeps codes verifiable across
    # workers there. With a master key the stored mac is not reversible
    # without it.
    mk = os.environ.get("OIKONOME_MASTER_KEY") or ""
    return hashlib.sha256(mk.encode() + b"|one-time-code").digest()


def _mac(purpose: str, subject: str, code: str) -> str:
    msg = f"{purpose}|{subject}|{code}".encode()
    return hmac.new(_key(), msg, hashlib.sha256).hexdigest()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def issue(purpose: str, subject: str) -> tuple[str, dict]:
    """A fresh code and the record to store for it. The code is returned
    only to be sent; it is never stored."""
    code = f"{secrets.randbelow(10 ** DIGITS):0{DIGITS}d}"
    return code, {"code_mac": _mac(purpose, str(subject), code),
                  "misses": 0,
                  "expires": (_now() + TTL).isoformat()}


def normalize(raw) -> str:
    """A pasted "123 456" is the same code: drop every space."""
    return "".join(str(raw or "").split())


def check(record: dict | None, raw_code, purpose: str, subject: str) -> str:
    """OK, WRONG, BURNED or INVALID. Counts a miss on `record` in place."""
    if not isinstance(record, dict) or not record.get("code_mac"):
        return INVALID
    try:
        expired = dt.datetime.fromisoformat(str(record.get("expires"))) < _now()
    except ValueError:
        expired = True
    misses = int(record.get("misses") or 0)
    if expired or misses >= MAX_MISSES:
        return INVALID
    if hmac.compare_digest(_mac(purpose, str(subject), normalize(raw_code)),
                           str(record["code_mac"])):
        return OK
    record["misses"] = misses + 1
    return BURNED if record["misses"] >= MAX_MISSES else WRONG


def charge_attempt(purpose: str, account_id) -> None:
    """Spend one attempt from the account's budget, or refuse with a 429.
    Charged before the code is compared, right or wrong, so the budget does
    not depend on which address the guesses come from."""
    from ..web.security import account_limit_or_429
    account_limit_or_429(("otp-verify", purpose, str(account_id)),
                         ACCOUNT_ATTEMPTS, ACCOUNT_WINDOW_S,
                         what="too many code attempts")
