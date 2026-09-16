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
    items = ([str(r).strip() for r in raw if str(r).strip()]
             if isinstance(raw, list)
             else [r.strip() for r in (raw or "").split(",") if r.strip()])
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
    recipients = parse_recipients(list(recipients or []))
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
    against its own household. Self-host returns without checking."""
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
