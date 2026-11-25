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
    flooding an inbox, and the right defense for that is a cap keyed on the
    ADDRESS being mailed (``SEND_MAX_PER_HOUR``), which a botnet with a
    million IPs cannot spread out of.

Token discipline matches ``password_resets``: opaque 256-bit link token, sha256
at rest, hash lookup (no timing channel on the bytes), one use. Minting does
NOT burn the address's other live links: anyone can ask for a link to any
address, so burning would let a stranger's request kill the one the owner
is about to open. The per-address hourly cap bounds how many are live.

The exemption is NOT a bearer on the address. Opened, it would be spendable
by whoever reached a sign-in door first with the account's email — and a
stranger polling the door with junk passwords would spend every unlock the
owner confirmed, keeping them out while turning each one into an
unchallenged guess. So each unlock has a PROOF, and only a sign-in carrying
it can spend that unlock. The proof exists only for whoever proved the
mailbox: the confirm step sets it in that browser and shows its short form
(the sign-in code) for the app. Asking for a link yields nothing — asking
is open to anyone. The proof is never stored — it is an HMAC, under the
instance master key, of the row id and the link token's hash, so nothing in
the row alone yields it. A sign-in without it leaves every row as it was.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
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


def _proof_key() -> bytes:
    # No master key is the dev / plaintext mode the rest of the crypto layer
    # also passes through; the link token's hash in the message still keeps
    # the proof out of reach of anyone who has not read the table.
    mk = os.environ.get("OIKONOME_MASTER_KEY") or ""
    return hashlib.sha256(mk.encode() + b"|login-unlock-proof").digest()


def proof_for(row_id, token_hash: str) -> str:
    """The proof that spends the unlock `row_id`. Derived, never stored."""
    msg = f"login-unlock|{row_id}|{token_hash}".encode()
    return hmac.new(_proof_key(), msg, hashlib.sha256).hexdigest()


# The sign-in code: the proof's short form, typed into the app by someone
# who confirmed the link in a browser. 10 base32 characters = 50 bits. It
# answers only for its own account, only inside that unlock's 10-minute
# window, and every guess goes through the sign-in doors' own limits.
CODE_LEN = 10
_B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
# what a person reading the code off a screen is likely to type instead
_LOOKALIKE = str.maketrans({"0": "O", "1": "I", "8": "B"})


def code_for(row_id, token_hash: str) -> str:
    """The sign-in code for the unlock `row_id`, as XXXXX-XXXXX."""
    msg = f"login-unlock-code|{row_id}|{token_hash}".encode()
    d = int.from_bytes(hmac.new(_proof_key(), msg, hashlib.sha256).digest(),
                       "big")
    raw = "".join(_B32[(d >> (5 * i)) & 31] for i in range(CODE_LEN))
    return raw[:5] + "-" + raw[5:]


def _norm_code(value: str) -> str:
    v = "".join(ch for ch in value.upper().translate(_LOOKALIKE)
                if ch.isalnum())
    return v[:5] + "-" + v[5:] if len(v) == CODE_LEN else ""


def recent_sends(conn, user_id) -> int:
    """Links minted for this user in the last hour — the per-address cap."""
    return conn.execute(
        """SELECT COUNT(*) AS n FROM login_unlocks
           WHERE user_id=%s AND created_at > now() - interval '1 hour'""",
        (user_id,)).fetchone()["n"]


def create(conn, user_id) -> str | None:
    """Mint an unlock token; returns the RAW token for the link, or None if
    this address has already been mailed its hourly allowance.

    Older live links stay live (see the module docstring): the cap, not a
    burn, bounds them.
    """
    token = secrets.token_urlsafe(32)
    with conn.transaction():
        # The per-address cap is the load-bearing anti-abuse control, so
        # the count and the insert are one atomic step under a per-user
        # lock: concurrent requests from many IPs serialise, and the losers
        # see the cap.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext("
                     "'oikonome:login-unlock:' || %s))", (str(user_id),))
        if recent_sends(conn, user_id) >= SEND_MAX_PER_HOUR:
            return None
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
    exactly the population whose mail gets prescanned.
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
    the user's email, the `proof` that spends this unlock and its short
    sign-in `code`) or None if the token is unknown, expired or spent."""
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
    if row is None:
        return None
    return {**row, "proof": proof_for(row["id"], _h(token)),
            "code": code_for(row["id"], _h(token))}


def end_for_user(conn, user_id) -> None:
    """Void every link and open exemption this account holds. A link was
    mailed to the address the account had then; once the login address
    moves, opening it would name the new address and answer the check for
    an account that mailbox no longer controls."""
    conn.execute(
        """UPDATE login_unlocks SET used_at = coalesce(used_at, now()),
                  consumed_at = coalesce(consumed_at, now()),
                  unlocked_until = least(unlocked_until, now())
           WHERE user_id = %s
             AND (used_at IS NULL OR consumed_at IS NULL)""", (user_id,))


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


def refund(conn, email: str, ids) -> None:
    """Un-spend an exemption consumed MOMENTS ago by a sign-in step that
    could not possibly finish — password verified, but the account needs a
    second factor the same request does not carry (totp_required /
    passkey_required). The two-step door posts twice, so without this the
    password step would burn the account's one exemption and the code step
    could never pass. Scoped tight: only a row consumed in the last 90
    seconds whose window is still open — a spend that led to 'bad
    credentials' or a wrong code stays spent.

    One refund per unlock (refunded_at is the stamp). The two-step form
    needs exactly one — the password post spends and refunds, the code post
    spends for good — so the exemption still buys a single attempt.

    `ids`: the unlocks the asking request itself spent (`consume`'s
    return), and only those. There is no refund by address: a concurrent
    attempt's spend inside the same 90 seconds is that attempt's, not this
    one's, and re-arming it would hand the stranger's attempt back."""
    if not email or not ids:
        return
    conn.execute(
        """UPDATE login_unlocks u SET consumed_at = NULL, refunded_at = now()
           FROM users usr
           WHERE usr.id = u.user_id AND lower(usr.email) = %s
             AND u.id = ANY(%s)
             AND u.consumed_at IS NOT NULL
             AND u.consumed_at > now() - interval '90 seconds'
             AND u.refunded_at IS NULL
             AND u.unlocked_until IS NOT NULL
             AND u.unlocked_until > now()""",
        (email.strip().lower(), list(ids)))


def live_ids(conn, email: str) -> list:
    """The account's opened, unspent unlocks (ids)."""
    return [r["id"] for r in _live(conn, email)]


def _live(conn, email: str) -> list:
    if not email:
        return []
    return conn.execute(
        """SELECT u.id, u.token_hash FROM login_unlocks u
           JOIN users usr ON usr.id = u.user_id
           WHERE lower(usr.email)=%s AND u.consumed_at IS NULL
             AND u.unlocked_until IS NOT NULL AND u.unlocked_until > now()""",
        (email.strip().lower(),)).fetchall()


def consume_ids(conn, ids) -> list:
    """Spend exactly these unlocks, each only if still open and unspent —
    so two requests naming the same row cannot both pass. Returns the ids
    actually spent."""
    if not ids:
        return []
    rows = conn.execute(
        """UPDATE login_unlocks u SET consumed_at=now()
           WHERE u.id = ANY(%s) AND u.consumed_at IS NULL
             AND u.unlocked_until IS NOT NULL AND u.unlocked_until > now()
           RETURNING u.id""", (list(ids),)).fetchall()
    return [r["id"] for r in rows]


def consume(conn, email: str, proof: str) -> list:
    """Spend the exemption the PROOF (or its sign-in code) names — ONE
    sign-in attempt, not a ten-minute pass for unlimited guessing. Returns
    the ids of the unlocks spent (empty, so falsy, when there was none): a
    request that later has to give its spend back names exactly these
    rows, never another attempt's.

    Without the proof nothing is touched: knowing the address is not
    enough to spend — or burn — the owner's unlock."""
    if not email or not proof or not proof.isascii():
        return []
    code = _norm_code(proof)
    match = [r["id"] for r in _live(conn, email)
             if hmac.compare_digest(proof_for(r["id"], r["token_hash"]), proof)
             or (code and hmac.compare_digest(
                 code_for(r["id"], r["token_hash"]), code))]
    return consume_ids(conn, match)
