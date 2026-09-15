"""An emailed answer to the human check.

Turnstile asks "is a human who controls this account signing in?". A
CAPTCHA is a weak proxy for that question, and it is unanswerable by anyone
the Cloudflare script cannot reach — JavaScript off, ad blocker, corporate
MITM proxy, privacy browser, Tor. Those users hit a challenge they can
never satisfy, on both `/login` and `/forgot`.

Proving control of the account's mailbox answers the SAME question, more
strongly, with no JavaScript. So this is not a way to skip the check — it
is a second way to pass it.

What it deliberately is NOT:

  * Not a sign-in. Following the link unlocks the challenge and nothing
    else; the password is still required, and so is TOTP/passkey. Inbox
    compromise alone still gets an attacker nowhere, and it never becomes
    the silent session a magic-link login would be.
  * Not IP-scoped. The exemption covers ONE ACCOUNT. Clearing the per-IP
    bucket would open a laundering hole — unlock an account you own, wipe
    your IP counter, resume guessing at someone else's. See
    ``security.clear_challenge_risk`` for the same reasoning.
  * Not Turnstile-gated itself. That would be a third locked door for the
    exact user it exists to rescue. The abuse it must not enable is
    mail-bombing, and the right defense for that is a cap keyed on the
    ADDRESS being mailed (``SEND_MAX_PER_HOUR``), which a botnet with a
    million IPs cannot spread out of.

Token discipline matches ``password_resets``: opaque 256-bit link token, sha256
at rest, hash lookup (no timing channel on the bytes), one use, and minting
burns older live tokens for the user.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets

# The link is short-lived: it exists to get someone through a challenge they
# are sitting in front of right now, not to be a spare key in an inbox.
LINK_TTL = dt.timedelta(minutes=30)
# How long the account stays challenge-exempt after the link is followed —
# enough to type a password and a TOTP code without racing a clock.
UNLOCK_TTL = dt.timedelta(minutes=10)
# Per-ADDRESS send cap. This is the load-bearing anti-abuse control, since
# the door carries no human check of its own.
SEND_MAX_PER_HOUR = 3


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def recent_sends(conn, user_id) -> int:
    """Links minted for this user in the last hour — the mail-bomb cap."""
    return conn.execute(
        """SELECT COUNT(*) AS n FROM login_unlocks
           WHERE user_id=%s AND created_at > now() - interval '1 hour'""",
        (user_id,)).fetchone()["n"]


def create(conn, user_id) -> str | None:
    """Mint an unlock token; returns the RAW token for the link, or None if
    this address has already been mailed its hourly allowance.

    Minting burns older live links for the user (same rule as resets):
    otherwise every click leaves another working key under the mat.
    """
    token = secrets.token_urlsafe(32)
    with conn.transaction():
        # The per-address cap is the load-bearing anti-abuse control, so
        # the count and the insert must be ONE atomic step. An unlocked
        # check-then-act let N concurrent requests (a botnet across many
        # IPs — the per-IP limiter buys "nothing" against that by design)
        # each read < 3 before any committed, and mail-bomb the victim.
        # A per-user advisory lock serialises them; the loser sees the cap.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext("
                     "'oikonome:login-unlock:' || %s))", (str(user_id),))
        if recent_sends(conn, user_id) >= SEND_MAX_PER_HOUR:
            return None
        conn.execute(
            "UPDATE login_unlocks SET used_at=now() "
            "WHERE user_id=%s AND used_at IS NULL", (user_id,))
        conn.execute(
            """INSERT INTO login_unlocks (user_id, token_hash, expires_at)
               VALUES (%s,%s,%s)""",
            (user_id, _h(token),
             dt.datetime.now(dt.timezone.utc) + LINK_TTL))
    return token


def peek(conn, token: str) -> dict | None:
    """Is this link still usable? Reads only — NOTHING is consumed.

    Peeking has to be separate from following: if `follow` ran straight
    from the GET handler, a mail scanner (Defender Safe Links, Proofpoint,
    Mimecast) or a link prefetcher fetching the URL would burn the token and
    start the 10-minute window before the human ever clicked. The population
    this hatch exists for — corporate proxies, locked-down browsers — is
    exactly the population whose mail gets prescanned, so the escape hatch
    would fail for precisely the people it was built for.
    """
    if not token:
        return None
    return conn.execute(
        """SELECT u.id, u.user_id, usr.email
           FROM login_unlocks u JOIN users usr ON usr.id = u.user_id
           WHERE u.token_hash = %s AND u.used_at IS NULL
             AND u.expires_at > now()""",
        (_h(token),)).fetchone()


def follow(conn, token: str) -> dict | None:
    """Consume the LINK and open the exemption window. Returns the row (with
    the user's email) or None if the token is unknown, expired or spent."""
    if not token:
        return None
    row = conn.execute(
        """UPDATE login_unlocks u SET used_at=now(),
                  unlocked_until = now() + %s
           FROM users usr
           WHERE usr.id = u.user_id AND u.token_hash=%s
             AND u.used_at IS NULL AND u.expires_at > now()
           RETURNING u.id, u.user_id, usr.email""",
        (UNLOCK_TTL, _h(token))).fetchone()
    return row


def is_unlocked(conn, email: str) -> bool:
    """Does this account currently hold a live, unspent exemption?"""
    if not email:
        return False
    row = conn.execute(
        """SELECT 1 FROM login_unlocks u JOIN users usr ON usr.id = u.user_id
           WHERE lower(usr.email)=%s AND u.consumed_at IS NULL
             AND u.unlocked_until IS NOT NULL AND u.unlocked_until > now()
           LIMIT 1""", (email.strip().lower(),)).fetchone()
    return row is not None


def refund(conn, email: str) -> None:
    """Un-spend an exemption consumed MOMENTS ago by a sign-in step that
    could not possibly finish — password verified, but the account needs a
    second factor the same request does not carry (totp_required /
    passkey_required). The two-step door posts twice, so without this the
    password step burned the account's one exemption and the code step
    could never pass: a 2FA account could not use its own unlock at
    all. Scoped tight: only a row consumed in the last 90 seconds
    whose window is still open — a spend that led to 'bad credentials' or
    a wrong code stays spent.

    ONE refund per unlock, ever (refunded_at is the stamp). The two-step
    form needs exactly one — password post spends and refunds, code post
    spends for good. Refunding every empty-code post instead turned the
    one-attempt spend into a consume→refund cycle that held the exemption
    open for unlimited password-verified posts across the whole window."""
    if not email:
        return
    conn.execute(
        """UPDATE login_unlocks u SET consumed_at = NULL, refunded_at = now()
           FROM users usr
           WHERE usr.id = u.user_id AND lower(usr.email) = %s
             AND u.consumed_at IS NOT NULL
             AND u.consumed_at > now() - interval '90 seconds'
             AND u.refunded_at IS NULL
             AND u.unlocked_until IS NOT NULL
             AND u.unlocked_until > now()""",
        (email.strip().lower(),))


def consume(conn, email: str) -> bool:
    """Spend the exemption — ONE sign-in attempt, not a ten-minute pass for
    unlimited guessing. Returns True if one was actually spent."""
    if not email:
        return False
    row = conn.execute(
        """UPDATE login_unlocks u SET consumed_at=now()
           FROM users usr
           WHERE usr.id = u.user_id AND lower(usr.email)=%s
             AND u.consumed_at IS NULL
             AND u.unlocked_until IS NOT NULL AND u.unlocked_until > now()
           RETURNING u.id""", (email.strip().lower(),)).fetchone()
    return row is not None
