"""Who in a household may change what.

Three roles, and the difference between them is the size of the blast
radius, not the amount of the data anyone can see. Everybody in a
household sees everything: a partner who can read the balances but not the
plan is a worse product, not a safer one.

  * **owner** — the account. Can do everything, including the things that
    end it or hand it to somebody else.
  * **member** — can edit the household's money: transactions, bills,
    budgets, rules, categories, receipts, imports, syncs. Cannot do the
    handful of things that are the ACCOUNT rather than the money (below).
  * **viewer** — reads everything, writes nothing but their own account
    (password, factors, sessions, their own delivery preference).

The member line is drawn at irreversibility and reach, not at
importance. Recategorising a year of transactions is a bigger edit than
disconnecting a bank, and a member may do it: it is inside the household,
it is visible, and it can be undone. What a member may NOT do is the set
where a mistake — or a person who should not have been added — leaves the
household's control:

  * **ending the account** — delete it, or change the billing behind it.
  * **taking the data out** — the export ZIP, the database dump, the
    connections bundle. A copy that has left the instance cannot be
    recalled, and "can't export" is most of what people mean when they say
    someone should not be able to walk off with the finances.
  * **deciding who else is in the household** — invites, removals, roles,
    delegated script tokens, support access. Otherwise the restriction is
    self-lifting: a member who can add a member can add an owner.
  * **the bank connections** — linking, re-keying, disconnecting, removing
    an account. These hold credentials, and disconnecting is felt by
    everyone in the house.
  * **the mail plumbing and where data is sent** — SMTP, the daily-email
    recipient list, AI backend URLs. Redirecting the household's mail to
    an outside mailbox is an export with extra steps, so it sits with the
    exports rather than with the settings a member may edit.

The policy is in ONE module because it is enforced in two places that
must not drift: the `current_user` choke point (every mutating request)
and the settings save (which is a single door carrying fifty fields, so
its owner-only fields are filtered rather than refused). A route added
tomorrow gets classified by `test_inventory_role_permissions.py`, which
fails when a new mutating route belongs to neither list — the same
technique the step-up inventory uses, for the same reason: reading the
route table by eye is how a gap gets in.
"""

from __future__ import annotations

import re

ROLES = ("owner", "member", "viewer")

# What an owner may hand out. 'owner' is deliberately absent: there is
# exactly one owner, and promoting somebody is a transfer of the account,
# not a role change — a different act with different consequences (billing,
# deletion rights, the ability to remove the previous owner) that should be
# designed on its own rather than fall out of a dropdown.
ASSIGNABLE_ROLES = ("member", "viewer")

DEFAULT_ROLE = "viewer"

# Doors that are the CALLER'S OWN ACCOUNT rather than the household's
# data. Every role holds these, viewers included — a view-only member
# still has a password to change, factors to enrol, sessions to revoke and
# their own delivery preference to set.
SELF_WRITE_OK = ("/api/logout", "/api/password/change", "/api/totp",
                 "/api/sessions/revoke", "/api/testing", "/api/passkeys",
                 "/api/devices",
                 "/api/me/email", "/api/account/leave",
                 # the caller's OWN login address: changing it (re-authed)
                 # and asking for a fresh verification link. A viewer whose
                 # verify link lapsed needs a route to a new one, and the
                 # owner cannot request it on their behalf.
                 "/api/email/change", "/api/verify-email/resend",
                 # this browser's/device's OWN push subscription — a
                 # view-only member still gets their own notifications
                 "/api/notify/push/subscribe", "/api/notify/push/unsubscribe",
                 # inline WebAuthn step-up: verifies an assertion against
                 # the caller's OWN passkey and mints a single-use
                 # self-scoped ticket — no privilege beyond the door it
                 # unlocks, and a passkey-only viewer needs it to pass
                 # step-up on the account doors above without burning a
                 # finite recovery code
                 "/api/stepup",
                 # the "sudo" window: proves the caller's OWN password /
                 # passkey and stamps the caller's OWN session — a viewer
                 # has to pass it to reach any of the doors above
                 "/api/auth/elevate")

# Doors that carry a BODY but change nothing: a read that has to be a POST
# because the question is too large or too structured for a query string.
# The gates that consult this module are method-based — anything that is
# not GET/HEAD/OPTIONS is a write — which is cheap and right for almost
# every route, and wrong for these. Left unnamed, a body-carrying read is
# refused as a write: a viewer is told to "ask the instance owner to make
# changes" for asking a question, which contradicts the policy at the top
# of this file (everybody in a household reads everything).
#
# The bar for this tuple is that the handler cannot write, not that it
# usually does not: a preview or an analyse step that stages rows for a
# later commit belongs on the write side, because it is half of an edit
# and the person who cannot finish it should not start it.
#
# Naming a route here waives the ROLE gate and NOTHING else — see
# `read_only_route`. A read-only standing and a hosted account with no
# enrolled factor still do not reach a route listed here.
READ_POST_OK = (
    # the conversational assistant over the household's own ledger: it
    # routes tool calls that only SELECT and returns prose. Nothing it can
    # reach writes a row, so it is a read in every sense but the verb.
    "/api/assistant",
)

# Owner-only path families. Prefix match at a segment boundary, so
# "/api/invites" covers "/api/invites/{hash}" and nothing that merely
# shares the spelling.
OWNER_ONLY = (
    # ending the account
    "/api/account/delete",
    "/api/billing",
    # who else is in the household, and what else holds its credentials
    "/api/invites", "/api/users", "/api/tokens", "/api/support-access",
    # where the ledger gets posted to
    "/api/webhooks",
    # data leaving the instance
    "/api/export", "/export", "/api/connections",
    # bank connections: linking, re-keying, disconnecting
    "/api/accounts/plaid", "/api/accounts/mx", "/api/accounts/simplefin",
    "/api/accounts/coinbase",
    # the collector script doors mint and rotate push credentials
    "/api/doctor/scripts",
    # the transitional Jinja twins of the same doors
    "/accounts/plaid", "/accounts/simplefin", "/settings/email",
    # the household's mail: re-inviting a recipient, testing the relay,
    # and sending the daily verdict to everyone on the list. The list
    # itself is filtered out of a member's settings save
    # (OWNER_ONLY_SETTINGS); these are the doors that ACT on it.
    "/api/settings/recipients", "/api/settings/smtp-test", "/api/jobs/email",
)

# Owner-only doors whose path carries an id, so a prefix cannot name them.
OWNER_ONLY_PATTERNS = (
    # removing an account: 'hide' keeps history, but the same door
    # disconnects the institution and can purge its transactions
    re.compile(r"^/api/accounts/[^/]+/remove$"),
)

# Settings fields a member's save may not change. The settings door is one
# endpoint carrying the whole document, and the SPA posts every field on
# every save — so refusing the request would mean a member could never
# save anything. These keys are dropped from a non-owner's body instead,
# leaving whatever is stored untouched.
OWNER_ONLY_SETTINGS = frozenset({
    # mail plumbing: whoever sets the relay reads everything sent through it
    "smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_from",
    "smtp_starttls", "smtp_no_verify",
    # who receives the household's daily mail (an outside mailbox here is
    # an export that arrives every morning)
    "email_recipients",
    # where transaction text is sent to be read — including which backend
    # serves each role, the vision model, and the consent that lets a
    # W-2's SSN or a check's account number leave the box for a remote
    # endpoint: both clients render all of these owner-only, and a member
    # must not be able to flip them with a raw settings POST
    "llm_url", "llm_api_key", "llm_backends", "llm_roles",
    "llm_vision_model", "llm_model", "llm_extra_body",
    "taxdocs_allow_remote_llm",
    # the household's clock and the hours its mail goes out — both clients
    # show these to the owner only, and the server has to agree
    "timezone", "email_schedule", "email_send_hour_utc",
    # the household-wide mute list (a member's OWN mute goes through
    # /api/me/email, not here) — otherwise a member could silence every
    # recipient, the owner included
    "email_muted",
    # bank-connection knobs that cost money or change what is billed /
    # detected — both clients render these owner-only, same class as the
    # AI endpoint. plaid_enrich_cap is a per-row Plaid spend ceiling and
    # plaid_recurring toggles a billed cross-check; the BYO aggregator
    # credentials point the whole household at an account's own Plaid/MX.
    "plaid_recurring", "plaid_enrich_cap",
    "plaid_client_id", "plaid_env", "mx_client_id", "mx_env",
})
# NOTE: merchant_logos, feedback_enabled and merchant_strip_cities are NOT
# here on purpose — both clients expose them to members (a display / logo /
# feedback preference), so gating them server-side would make a member's
# toggle a silent 200 no-op that springs back with no error. Owner-only
# means the CLIENT renders it owner-only; keep this set and the clients'
# `owner &&` gating in lockstep, which the member-role test pins.


def path_within(path: str, prefixes) -> bool:
    """Whether a request path is one of these endpoints or nested under one.

    The gate allowlists name path FAMILIES ("/api/totp" admits
    "/api/totp/enroll"). A bare str.startswith would also admit any future
    sibling that merely shares the spelling — "/api/totp-foo" would sail
    through the viewer and 2FA-enrollment gates without anyone deciding it
    should — so matching is anchored at a segment boundary: the exact path,
    or the prefix followed by "/".
    """
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def owner_only(path: str) -> bool:
    """Is this door the account rather than the money?"""
    return (path_within(path, OWNER_ONLY)
            or any(p.match(path) for p in OWNER_ONLY_PATTERNS))


def read_only_route(path: str) -> bool:
    """Is this door a read that merely arrives as a POST?

    Asked by the ROLE gate only, and that scope is the point. This
    tuple exists because "everybody in a household reads everything" is a
    statement about ROLES, so a body-carrying read must not be refused to
    a viewer. It says nothing about whether the ACCOUNT is entitled to the
    call. The billing read-only state and the hosted forced-2FA gate ask
    the second question and go on method alone; wiring this predicate into
    them would quietly exempt a route listed here from both — including a
    billed or step-up door. Adding a route here must never buy anything
    but the role gate.
    """
    return path_within(path, READ_POST_OK)


def can_write(role: str, path: str) -> bool:
    """May a caller holding `role` make this mutating request?

    Unknown roles are treated as viewers. A typo in a role column, or a
    value from a future version restored onto an older one, must fail to
    the least privilege rather than the most.
    """
    if role == "owner":
        return True
    if path_within(path, SELF_WRITE_OK) or read_only_route(path):
        return True
    if role == "member":
        return not owner_only(path)
    return False


def denial(role: str, path: str) -> str:
    """What to tell someone the gate just refused.

    A member and a viewer are refused for different reasons and can do
    different things about it, so they are not told the same sentence.
    """
    if role == "member":
        return ("this is an owner-only change — ask the account owner "
                "(connections, exports, members and billing stay with them)")
    return "view-only access — ask the instance owner to make changes"


# Restoring an export is not an import: it merges a whole other ledger —
# accounts, connections, settings — into this household, and the archive
# comes from outside the instance. It is the mirror of the export door, so
# it answers to the same role.
RESTORE_DENIED = ("restoring an export is owner-only — a member can import "
                  "transactions, but replacing the household's data is the "
                  "account owner's call")


def may_restore(role: str) -> bool:
    return role == "owner"


def settings_for(role: str, body: dict) -> dict:
    """A settings body with the fields this role may not set removed.

    Owners get their body back untouched. Everyone else gets a copy
    missing the owner-only keys, which the save then leaves at whatever is
    already stored — the alternative, refusing the whole save, would stop a
    member editing budgets because the form also carries an SMTP host they
    never typed.
    """
    if role == "owner" or not isinstance(body, dict):
        return body
    return {k: v for k, v in body.items() if k not in OWNER_ONLY_SETTINGS}
