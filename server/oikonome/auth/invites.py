"""One-time invites: the owner mints a link that creates ONE
family-member account (the role rides on the invite — a member who can
edit, or a view-only reader) within 7 days. The token is stored hashed —
the URL is the only copy; listing exposes the hash as an API handle, which
grants nothing without the preimage.

An invite that NAMES an address may only be claimed by that address (see
`bound_address`). That binding is what stops the claim door from being an
account-planting primitive: the claim form is pre-auth and proves nothing
about the person typing into it, so an unbound link creates a user row
bearing whatever address was typed. On hosted, where `users.email` is one
global namespace shared by every household, that lets anyone with an
account plant a row on a stranger's address — occupying it against the
stranger's own future signup, and standing inside the planter's household
as a member wearing the stranger's name. The mint door therefore requires
an address on hosted and hands the link ONLY to that mailbox
(`web/app.invite_create`), so holding a claimable link IS the mailbox
proof this door otherwise lacks. Self-host keeps the bearer share-link: one
household, no shared namespace, and frequently no SMTP at all to deliver
through — the same split, for the same reason, as `claim`'s
`mark_verified`."""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets

import psycopg

from ..envnum import env_flag
from ..web import permissions
from . import email_verify, passwords

INVITE_TTL = dt.timedelta(days=7)
# What an invite may carry. The owner picks when they mint the link, and
# can change it afterwards from the household roster — see
# `web/permissions.py` for what each role may actually do.
ROLES = permissions.ASSIGNABLE_ROLES

# A household is a family, not an org: cap the member count so a leaked
# (or maliciously re-minted) share link can't stuff a tenant with accounts.
# Mint is step-up-gated but claims are pre-auth, so the ceiling lives here.
MEMBER_CAP = 10


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# The label doubles as the delivery address whenever it looks like one —
# that is already how the mint door decides whom to email, so binding the
# claim to it adds no second source of truth. Anything else (a first name,
# "the sitter") is a display label and binds nothing.
def bound_address(label: str) -> str:
    """The address this invite may be claimed by, or "" when it names none."""
    label = (label or "").strip().lower()
    at = label.find("@")
    return label if 0 < at < len(label) - 1 and " " not in label else ""


# Named on hosted, where an unnamed invite would be a bearer link into a
# global address namespace. Says what to do, not what is forbidden.
ADDRESS_REQUIRED = ("enter the email address this invite is for — we send "
                    "the link there, and only that address can use it")


def create(conn, tenant_id, created_by,
           role: str = permissions.DEFAULT_ROLE,
           label: str = "") -> dict:
    if role not in ROLES:
        raise ValueError(f"unsupported role '{role}'")
    label = label.strip()[:80]
    # Store the address normalized so the claim's comparison is stable —
    # mail addresses are typed with whatever capitals the owner likes.
    bound = bound_address(label)
    if bound:
        label = bound
    elif env_flag("OIKONOME_HOSTED"):
        raise ValueError(ADDRESS_REQUIRED)
    token = secrets.token_urlsafe(24)
    expires = dt.datetime.now(dt.timezone.utc) + INVITE_TTL
    conn.execute(
        """INSERT INTO invites (token_hash, tenant_id, role, label,
                                created_by, expires_at)
           VALUES (%s,%s,%s,%s,%s,%s)""",
        (_h(token), tenant_id, role, label, created_by, expires))
    return {"token": token, "token_hash": _h(token), "role": role,
            "label": label, "expires_at": expires}


def pending(conn, tenant_id) -> list[dict]:
    return conn.execute(
        """SELECT token_hash, role, label, created_at, expires_at
           FROM invites
           WHERE tenant_id = %s AND used_at IS NULL AND expires_at > now()
           ORDER BY created_at DESC""", (tenant_id,)).fetchall()


def revoke(conn, tenant_id, token_hash: str) -> int:
    return conn.execute(
        "DELETE FROM invites WHERE tenant_id = %s AND token_hash = %s "
        "AND used_at IS NULL", (tenant_id, token_hash)).rowcount


def peek(conn, token: str) -> dict | None:
    """Is this invite claimable, for which role, and — when it names one —
    which address? For the claim page's greeting. Handing the bound address
    back to whoever holds the token discloses nothing: on hosted the token
    reached exactly that mailbox, and the page needs it to fill the field
    the claim is checked against."""
    row = conn.execute(
        """SELECT tenant_id, role, label FROM invites
           WHERE token_hash = %s AND used_at IS NULL AND expires_at > now()""",
        (_h(token),)).fetchone()
    if row is None:
        return None
    return {**row, "email": bound_address(row["label"])}


# ONE message for bad-invite AND taken-email. A distinct pair ("invalid,
# used, or expired" vs "an account with that email already exists"), plus a
# burn that rolls back on the email check, would make a valid invite a
# reusable account-enumeration oracle.
CLAIM_FAILED = ("couldn't create an account with this invite — the link may "
                "be used or expired, or the email may already have an "
                "account (try signing in). Ask for a new invite if needed.")

# Distinct from CLAIM_FAILED on purpose: it fires before the email check
# and regardless of the address typed, so it can't be used to probe
# whether an email has an account — and the owner needs to hear "full",
# not "bad link", to know a member must be removed first.
HOUSEHOLD_FULL = ("this household already has the maximum number of "
                  "members — ask the owner to remove one first, then "
                  "request a new invite")

# Also distinct from CLAIM_FAILED, and also no oracle: it is decided
# entirely by the invite the caller already holds (peek hands the same
# address to the same caller), and it never reads the users table. Saying
# it plainly is what keeps a mistyped address from being read as a dead
# link — and keeps the invite unspent so the real address can still use it.
WRONG_ADDRESS = ("this invite was sent to a different email address — "
                 "create the account with the address the invite was "
                 "mailed to")


def claim(conn, token: str, email: str, password: str,
          mark_verified: bool | None = None) -> dict:
    """Burn the invite and create the member. Raises ValueError on any
    user-facing problem. Input validation (which reveals nothing about
    other accounts) happens BEFORE the burn; after the burn, every failure
    is the one generic CLAIM_FAILED (except the member cap, which leaks
    nothing about other accounts — see HOUSEHOLD_FULL).

    An invite that names an address may be claimed by that address only —
    the module docstring says why. That check runs inside the burn's own
    transaction and rolls it back, so a mistyped address neither creates an
    account nor spends the invitee's one link.

    mark_verified: an unbound invite is a bearer link — the claimer types
    ANY address, and nothing proves mailbox control. Stamping verified_at
    here would make that address a recipient of the daily financial email,
    so a typo (or a stranger's address, typed on purpose) would mail the
    household's balances to a non-consenting third party. On hosted the
    member therefore starts unverified — same contract as /api/signup — and
    the returned verify_token drives the emailed confirmation link. Self-host keeps the
    trusted-household default (verified, like its operator-typed signups).
    None (the default) decides from OIKONOME_HOSTED; pass a bool to force.
    """
    email = email.strip().lower()
    if "@" not in email:
        raise ValueError("enter a valid email address")
    err = passwords.password_error(password)
    if err:
        raise ValueError(err)
    if mark_verified is None:
        mark_verified = not env_flag("OIKONOME_HOSTED")
    # Control connections are autocommit, so a SELECT … FOR UPDATE lock
    # would NOT span the claim — two concurrent claims on one "one-time"
    # invite could each pass the SELECT and both create a user. Burn the
    # invite atomically FIRST with a conditional UPDATE … RETURNING: exactly
    # one racer's UPDATE matches the `used_at IS NULL` row, the loser gets no
    # row. The burn COMMITS on its own — a taken-email failure below
    # no longer rolls it back, so a failed claim spends the invite instead
    # of leaving a reusable probe (the owner mints a fresh link, which is
    # cheap; enumeration retries are not).
    with conn.transaction():
        inv = conn.execute(
            """UPDATE invites SET used_at = now()
               WHERE token_hash = %s AND used_at IS NULL
                     AND expires_at > now()
               RETURNING tenant_id, role, label""", (_h(token),)).fetchone()
        # The address check reads the row the burn just locked and raises
        # INSIDE the block, so the burn rolls back with it: a mistyped
        # address neither creates an account nor spends the invitee's one
        # link, and no window exists between reading the binding and
        # honouring it. Every OTHER failure below still spends the invite,
        # deliberately — those read the users table and would otherwise be
        # a retryable oracle; this one reads only the invite the caller is
        # already holding.
        if inv is not None:
            bound = bound_address(inv["label"])
            if bound and bound != email:
                raise ValueError(WRONG_ADDRESS)
    if inv is None:
        raise ValueError(CLAIM_FAILED)
    # The taken-email check and the INSERT below would otherwise straddle a
    # TOCTOU window: two concurrent claims for one address (or a claim racing
    # /api/signup) both pass the exists-check and the loser hits the
    # users.email UNIQUE index with a 500. Serialize the claim per email
    # under the SAME advisory key signup uses, so the two doors also
    # serialize against each other; the unique-violation catch is the
    # backstop for any writer that doesn't hold the lock — either way the
    # caller sees the one generic CLAIM_FAILED, never a 500.
    try:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                         ("oikonome:signup:" + email,))
            # Member ceiling, under a PER-TENANT advisory lock: the email
            # lock above serializes per ADDRESS, so two claims with
            # different addresses would both read count=cap-1 and both
            # insert. Lock order is email-then-tenant everywhere (signup
            # takes only the email lock), so no cycle is possible. Checked
            # before the taken-email read so a full household answers
            # HOUSEHOLD_FULL for every address — no email oracle.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                         ("oikonome:tenant-members:"
                          + str(inv["tenant_id"]),))
            n = conn.execute(
                "SELECT count(*) AS n FROM users WHERE tenant_id = %s",
                (inv["tenant_id"],)).fetchone()["n"]
            if n >= MEMBER_CAP:
                raise ValueError(HOUSEHOLD_FULL)
            if conn.execute("SELECT 1 FROM users WHERE email = %s",
                            (email,)).fetchone():
                raise ValueError(CLAIM_FAILED)
            u = conn.execute(
                """INSERT INTO users (tenant_id, email, password_hash, role,
                                      verified_at)
                   VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                (inv["tenant_id"], email, passwords.hash_password(password),
                 inv["role"],
                 dt.datetime.now(dt.timezone.utc) if mark_verified
                 else None)).fetchone()
            # Mint the confirmation token inside the same transaction so a
            # failure rolls the member back with it — never an unverified
            # account with no way to a link but the day-3 resend banner.
            vtoken = (None if mark_verified
                      else email_verify.create(conn, u["id"]))
    except psycopg.errors.UniqueViolation:
        raise ValueError(CLAIM_FAILED) from None
    return {"user_id": u["id"], "tenant_id": inv["tenant_id"],
            "role": inv["role"], "email": email, "verify_token": vtoken}
