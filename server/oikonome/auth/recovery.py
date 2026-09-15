"""one-time recovery codes — the lockout safety net for forced 2FA.

A hosted account must enroll a second factor (TOTP or passkey). A TOTP user
who loses their authenticator would otherwise be locked out, so enrollment
issues a batch of one-time recovery codes (shown once). Codes are sha256 at
rest (like every other token here) and single-use.

Format: 5 groups of 4 lowercase base32-ish chars, dash-separated
(e.g. `k7f2-9qtp-m3xz-...`) — easy to write down, hard to guess.
"""

from __future__ import annotations

import hashlib
import secrets

_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # no ambiguous 0/o/1/i/l
BATCH = 8


def _fmt() -> str:
    groups = ["".join(secrets.choice(_ALPHABET) for _ in range(4))
              for _ in range(5)]
    return "-".join(groups)


def _h(code: str) -> str:
    # normalize: strip spaces + lowercase so "AB CD" matches "abcd"
    return hashlib.sha256(
        code.strip().lower().replace(" ", "").encode()).hexdigest()


def issue(admin_conn, user_id, n: int = BATCH) -> list[str]:
    """Replace any prior codes and mint a fresh batch. Returns the PLAINTEXT
    codes (shown once); only their hashes persist. Admin connection — the
    app role can't INSERT here (mint is a server-side act)."""
    admin_conn.execute("DELETE FROM recovery_codes WHERE user_id=%s",
                        (user_id,))
    codes = [_fmt() for _ in range(n)]
    for c in codes:
        admin_conn.execute(
            "INSERT INTO recovery_codes (user_id, code_hash) VALUES (%s, %s)",
            (user_id, _h(c)))
    return codes


def redeem(conn, user_id, code: str) -> bool:
    """Consume a recovery code for this user. True iff it matched an unused
    code (which is now burned). Constant-ish work: always hashes."""
    if not code or len(code.strip()) < 8:
        return False
    row = conn.execute(
        "UPDATE recovery_codes SET used_at=now() "
        "WHERE user_id=%s AND code_hash=%s AND used_at IS NULL "
        "RETURNING id", (user_id, _h(code))).fetchone()
    return row is not None


def remaining(conn, user_id) -> int:
    return conn.execute(
        "SELECT count(*) n FROM recovery_codes "
        "WHERE user_id=%s AND used_at IS NULL", (user_id,)).fetchone()["n"]
