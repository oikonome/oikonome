"""Outbound-mail recipient policy.

The daily verdict carries balances and spending, so on the HOSTED
platform financial mail only goes to addresses that hold an account on
the requesting tenant — free-form recipients would let one compromised
session exfiltrate the household's finances to an attacker inbox.
Self-host keeps free-form recipients: the operator owns the SMTP.

Every settings door that writes `email_recipients` (POST /api/settings
and the transitional Jinja POST /settings/email) shares this check so
the policy can't drift between paths: it is easy to ship one door
without it.

`invite_added` carries the second half of the same policy — an added
address is INVITED, not enrolled — and lives here for the same reason: it
is the one place both doors call, so the rule cannot ship on one path and
quietly skip the other.
"""

import logging
import re
from ..envnum import env_flag

from fastapi import HTTPException

log = logging.getLogger("oikonome.mailguard")


# A household's mail list. Twenty is far past any real one (the biggest
# plausible case is a family plus an accountant) and the cap exists for the
# other direction: every address on this list becomes a stored config entry,
# an invite row, and — the expensive part — an outbound mail. Every other
# list field in settings carries a bound (income_scenarios[:20],
# savings_goals[:30], manual_assets[:30]); this one must too.
MAX_RECIPIENTS = 20

# Characters that make one string more than one mailbox, or a display name
# rather than a mailbox, once it lands in a To header: smtplib builds the
# envelope by parsing that header, so a stored "me@a.example,other@b.example"
# is delivered to both. Whitespace and quotes are refused for the same
# reason — nothing this app mails needs a quoted local part.
_NOT_IN_ADDRESS = frozenset(',;<>()[]"\\:')


def valid_address(addr) -> bool:
    """Is this ONE bare mailbox (local@domain) and nothing else?

    Every door that stores an address people are mailed at — the login
    email (signup, setup, invite claim, email change, operator console)
    and the send path itself — asks this. Checking only for an "@" let a
    comma-joined pair through as one address, and the mail then reached a
    mailbox nobody verified."""
    if not isinstance(addr, str) or not 3 <= len(addr) <= 254:
        return False
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127
           or ch in _NOT_IN_ADDRESS for ch in addr):
        return False
    local, at, domain = addr.rpartition("@")
    if not at or not local or "@" in local or len(local) > 64:
        return False
    # no dot required: a LAN relay's single-label domain is a real mailbox
    return all(domain.split("."))


# `Display Name <local@domain>` — the one mailbox inside it is recoverable
# without guessing. Anything else carrying brackets or quotes is not.
_DISPLAY_FORM = re.compile(r'^[^<>]*<([^<>]+)>$')


def split_mailboxes(raw) -> tuple[list[str], list[str]]:
    """A stored address list, in any shape it was ever written in, as
    `(mailboxes, unrecoverable)`.

    Settings once stored whatever was typed, restore accepts an old ZIP's
    shape, and an operator writes a comma list into an env var, so a list
    entry can be `a@x.example;b@y.example` or `Name <a@x.example>`. Each
    of those names mailboxes somebody meant, so they are split at `,`/`;`
    and unwrapped from the display form. What is still not one mailbox
    after that comes back verbatim in `unrecoverable`, so a caller can drop
    that one entry — naming it — and carry on with the rest instead of
    refusing the whole list. Nothing is guessed: `a b@x.example` is not
    rewritten into some other address.

    Accepts a string (split at `,` and `;`) or a list of strings (each
    entry split the same way). Order is kept; duplicates, compared
    case-insensitively, are dropped."""
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    good, bad, seen = [], [], set()
    for item in items:
        if item is None:
            continue
        # a quoted display name may carry the separator ("Doe, Jane"
        # <a@x>); nothing this app mails has a quoted local part, so the
        # quoted text is a name and can go before splitting
        text = re.sub(r'"[^"]*"', "", str(item))
        for piece in re.split(r"[,;]", text):
            piece = piece.strip()
            if not piece:
                continue
            m = _DISPLAY_FORM.match(piece)
            addr = m.group(1).strip() if m else piece
            if not valid_address(addr):
                bad.append(piece)
                continue
            if addr.lower() not in seen:
                seen.add(addr.lower())
                good.append(addr)
    return good, bad


def stored_mailboxes(stored) -> tuple[set[str], set[str]]:
    """A stored recipient list as `(every mailbox, mailboxes whose entry
    named only them)`, lowercased.

    The first set is who is ALREADY on the list: a client re-posts the
    stored list, the save door splits it, and comparing those mailboxes
    against the raw stored strings would read every `Name <a>` or `a;b`
    entry as a brand-new address. The second is who has already been
    asked: a consent recorded under `Name <a>` is `a`'s, but one recorded
    under `a;b` is not both people's, so a joined entry's mailboxes are
    still invited on their own."""
    if stored is None:
        return set(), set()
    items = stored if isinstance(stored, (list, tuple)) else [stored]
    every, single = set(), set()
    for entry in items:
        good, _bad = split_mailboxes([entry])
        every.update(g.lower() for g in good)
        if len(good) == 1:
            single.add(good[0].lower())
    return every, single


def operator_addresses() -> list[str]:
    """OIKONOME_OPERATOR_EMAIL as mailboxes. The variable has always been
    read as free text, and a comma list in it reached every address; read
    as one address, every operator notice would be refused at the send
    path and fall to a log line. An entry that is not an address is
    dropped and logged, never the whole list."""
    import os
    good, bad = split_mailboxes(
        os.environ.get("OIKONOME_OPERATOR_EMAIL", ""))
    if bad:
        log.warning("OIKONOME_OPERATOR_EMAIL: %d entr%s not an email "
                    "address, skipped", len(bad),
                    "y is" if len(bad) == 1 else "ies are")
    return good


def parse_recipients(raw: str | list | None, cap: int | None = -1) -> list[str]:
    """Form/JSON value → clean recipient list, de-duplicated and capped.

    Accepts either shape on purpose. The comma-separated string is what the
    server-rendered forms and every stored config have always sent; a LIST is
    what the chip editor sends, because a list of addresses is what the
    thing actually is and round-tripping it through a comma-joined string
    just to split it again is where "a, b" vs "a,b" bugs come from.

    De-dup is case-insensitive and keeps the FIRST spelling: an address is a
    mailbox, not a string, so listing it twice is one recipient however it is
    capitalised — and without this, a list of 100k copies of one legitimate
    address passed every membership check there is.

    `cap=None` returns the whole de-duplicated list. The settings door uses it
    to COUNT what was submitted, so it can refuse an over-long list out loud
    rather than truncate one and report success."""
    # every entry split into its mailboxes (a stored `a;b` or `Name <a>`
    # is two, or one, real recipients); what is not an address stays in,
    # verbatim, for check_recipients to name
    items: list[str] = []
    for r in (raw if isinstance(raw, list) else [raw or ""]):
        good, bad = split_mailboxes([r])
        items.extend(good)
        items.extend(bad)
    limit = MAX_RECIPIENTS if cap == -1 else cap
    out, seen = [], set()
    for r in items:
        if r.lower() in seen:
            continue
        seen.add(r.lower())
        out.append(r)
        if limit is not None and len(out) >= limit:
            break
    return out


def members_only() -> bool:
    """Is the hosted member-only recipient rule in force?

    Hosted mails a household's balances only to addresses that hold an
    account on the tenant. The invite a recipient answers is consent, not
    proof of anything — a session that can write the recipient list can also
    answer the invite it just mailed to itself — so membership is what
    actually keeps one compromised session from streaming the finances to an
    outside inbox.

    `OIKONOME_HOSTED_FREEFORM_RECIPIENTS` lifts it for a hosted-SHAPE test
    instance, where the recipient is deliberately somebody with no account
    and no intention of signing in. It is off unless an operator sets it, and
    it does not belong on an instance serving ordinary households. Self-host
    is always free-form: the operator owns the SMTP.
    """
    return (env_flag("OIKONOME_HOSTED")
            and not env_flag("OIKONOME_HOSTED_FREEFORM_RECIPIENTS"))


def filter_recipients(conn, recipients: list[str]) -> list[str]:
    """Non-raising sibling of check_recipients for BULK writes (restore):
    drop any recipient that isn't a tenant member instead of 400-ing the
    whole operation. A restore ZIP can otherwise plant free-form
    email_recipients — scrub_config only strips secret/demo keys, not the
    mail boundary. Self-host is unchanged — except
    for the cap and de-dup, which apply everywhere: an archive is untrusted
    input on BOTH deployments, and a config carrying 100k recipients is not
    a mail list, it is a payload."""
    # an entry that is not an address is dropped here, not stored: bulk
    # writes don't 400, and a kept one would fail every later Settings save
    recipients = [r for r in parse_recipients(list(recipients or []))
                  if valid_address(r)]
    if not recipients or not members_only():
        return recipients
    members = {r["email"] for r in conn.execute(
        "SELECT email FROM users WHERE tenant_id = "
        "current_setting('app.tenant_id')::uuid").fetchall()}
    return [r for r in recipients if r.lower() in members]


def check_recipients(conn, recipients: list[str]) -> None:
    """Hosted only: every recipient must hold an account on this tenant.

    `conn` must be a tenant-scoped connection — membership is read from
    the ambient app.tenant_id, so a caller can only ever validate
    against its own household. Self-host skips the membership half — but
    every entry, on every instance, must be one mailbox: a list entry
    carrying a comma is two recipients the invite never asked."""
    malformed = [r for r in recipients or [] if not valid_address(r)]
    if malformed:
        # name it: a stored entry from before this rule is re-posted by
        # every Settings save, and "1 not" leaves nobody able to find it
        shown = ", ".join(f"\u201c{str(r)[:80]}\u201d" for r in malformed[:3])
        raise HTTPException(
            400, f"not an email address: {shown} — remove or correct "
                 f"{'it' if len(malformed) == 1 else 'them'} and save again")
    if not recipients or not members_only():
        return
    members = {r["email"] for r in conn.execute(
        "SELECT email FROM users WHERE tenant_id = "
        "current_setting('app.tenant_id')::uuid").fetchall()}
    bad = [r for r in recipients if r.lower() not in members]
    if bad:
        raise HTTPException(
            400, f"recipients must be account emails on this "
                 f"instance ({', '.join(sorted(bad))}) — invite "
                 f"them under Settings first")


def added_recipients(previous: list | set | None,
                     current: list | None) -> list[str]:
    """The addresses in `current` that weren't in `previous`, original
    casing kept for the mail. Both doors save the WHOLE list on every
    write, so without this diff an unrelated settings save would re-invite
    everyone already on it.

    De-duplicated and capped for the same reason `parse_recipients` is: this
    result decides how many invites get minted and mailed, and a caller that
    hands it a list straight off the wire must not be able to turn one
    request into thousands of sends."""
    prev = {str(r).strip().lower() for r in (previous or [])}
    out, seen = [], set()
    for r in (current or []):
        v = str(r).strip()
        if not v or v.lower() in prev or v.lower() in seen:
            continue
        seen.add(v.lower())
        out.append(v)
        if len(out) >= MAX_RECIPIENTS:
            break
    return out


def invite_added(user: dict, emails: list[str]) -> None:
    """Mint + send an invite for each newly added address.

    Never raises. The settings save has already committed by the time this
    runs, and its response must not turn into a 500 because a relay was
    slow. A failed invite enrols nobody — the safe direction, since the
    cost is a row that sits at "not invited" with a Resend button next to
    it, while the cost of the other direction is mail somebody never
    agreed to receive.

    Sending is off-thread for the same reason `_deliver_verification` is:
    a save should not block on SMTP, least of all on a list of five.

    One thread per batch, not one per address: a thread per address would
    each check out a POOLED tenant connection and then sit in an SMTP
    conversation, so a single settings save could take twenty of them at
    once and starve every other request of the pool. They all belong to one
    tenant, so the SMTP settings
    are resolved once and reused; the sends are sequential, which is the
    right shape for something nobody is waiting on.

    The slice is a backstop, not the policy: both settings doors already cap
    at MAX_RECIPIENTS."""
    from ..notify import recipient_invites
    from .app import _deliver_recipient_invite, _tenant_smtp
    import threading
    minted: list[tuple[str, str]] = []
    for email in list(emails)[:MAX_RECIPIENTS]:
        try:
            token = recipient_invites.invite(
                user["tenant_id"], email, invited_by=user.get("user_id"))
        except Exception:                                # noqa: BLE001
            log.exception("recipient invite mint failed for %s", email)
            continue
        # None = already answered. Accepted stays accepted (consent is not
        # revoked by being taken off a list and put back), and declined
        # stays declined — re-adding somebody must not re-ask them.
        if not token:
            continue
        minted.append((email, token))
    if not minted:
        return

    tenant_id, inviter = user["tenant_id"], user.get("email")

    def _send_all() -> None:
        # resolve once for the tenant, then walk the batch
        smtp = _tenant_smtp(tenant_id)
        for email, token in minted:
            try:
                _deliver_recipient_invite(email, token, tenant_id, inviter,
                                          smtp=smtp, resolved=True)
            except Exception:                            # noqa: BLE001
                log.exception("recipient invite delivery failed for %s",
                              email)

    threading.Thread(target=_send_all, daemon=True).start()
