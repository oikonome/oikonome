"""The unsubscribe link at the foot of every household email.

Every scheduled mail — the daily verdict, the weekly report, the monthly
report card, the year in review — and the alert mails that share their
recipient list (link trouble, savings goals, a quiet collector) carries one
link that stops ALL of them for the address it was sent to. It lands that
address in the household's `email_muted` list, which is the same per-person
opt-out the Settings page and the mobile app already expose — so the state
is one state, visible to the owner, and reversible from Settings. The link
is not a second mechanism; it is a second door to the existing one.

Why the link is per-address and not per-household: a partner who is tired
of the mail must be able to leave without silencing the owner, and the
owner must be able to leave without silencing the partner. Muting the
address that received the mail is the only reading under which "stop
sending me this" does exactly what the reader meant.

Why there is no table: the token is a signed statement — "this address may
mute itself in this tenant" — and needs no state to verify. It is signed
with a key derived from the install's master key, so a token minted on one
install says nothing on another. Anyone holding the token can mute that one
address, which is also true of every unsubscribe link ever sent and is the
right trade: the downside of a forwarded link is a missed email, which the
owner sees in Settings and can undo; the downside of a link that needs a
login is that the person who most wants out (a guest with no account) cannot
use it.

The GET only shows the page and a button; the mute itself is a POST. A mail
scanner that prefetches every link in a message must not silence someone who
did nothing (the same peek-then-act split as /recipient-invite and
/verify-email). RFC 8058 one-click — the POST a mail client sends from its
own Unsubscribe button — is honoured on the same route, because that POST is
a human pressing a button, not a prefetch.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html as _html
import logging
import os
import secrets
import uuid

log = logging.getLogger("oikonome.unsubscribe")

PATH = "/unsubscribe"

# A process-lifetime key for installs that run without a master key (the
# test suite, a bare dev checkout). Links then survive exactly as long as the
# process — fine for those, and never the shape of a real install, where the
# master key exists from first boot.
_EPHEMERAL = secrets.token_bytes(32)


def _key() -> bytes:
    mk = os.environ.get("OIKONOME_MASTER_KEY") or ""
    seed = mk.encode() if mk else _EPHEMERAL
    return hashlib.sha256(seed + b"|email-unsubscribe").digest()


def _norm(email: str) -> str:
    return (email or "").strip().lower()


def token(tenant_id, email: str) -> str:
    """`<payload>.<signature>`, both urlsafe-base64 without padding."""
    payload = f"{uuid.UUID(str(tenant_id))}|{_norm(email)}".encode()
    sig = hmac.new(_key(), payload, hashlib.sha256).digest()[:20]
    return (base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
            + "." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode())


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def parse(tok: str) -> tuple[str, str] | None:
    """`(tenant_id, email)` for a genuine token, else None. Constant-time on
    the signature; everything malformed is simply None."""
    try:
        p64, s64 = (tok or "").split(".", 1)
        payload = _unb64(p64)
        sig = _unb64(s64)
    except (ValueError, TypeError):
        return None
    want = hmac.new(_key(), payload, hashlib.sha256).digest()[:20]
    if not hmac.compare_digest(sig, want):
        return None
    try:
        tid, email = payload.decode().split("|", 1)
        return str(uuid.UUID(tid)), email
    except (ValueError, UnicodeDecodeError):
        return None


def url(tenant_id, email: str) -> str:
    """The absolute link, or "" when there is no base URL safe to put in
    recurring mail (see `report.mailable_base`) — the footer then says how
    to stop the mail without linking anywhere."""
    from ..web.report import mailable_base
    base = mailable_base()
    return f"{base}{PATH}?token={token(tenant_id, email)}" if base else ""


def footer(tenant_id, email: str) -> tuple[str, str, str]:
    """`(plain, html, url)` — the sentence that closes each mail."""
    u = url(tenant_id, email)
    who = _norm(email)
    if u:
        plain = (f"\n\n--\nSent to {who}. To stop every Oikonome email to this "
                 f"address — daily, weekly, monthly and yearly — open:\n{u}\n"
                 "The household owner can turn it back on under "
                 "Settings → Email.")
        html = (
            '<p style="margin:18px 0 0;font-size:12px;line-height:1.5;'
            'color:#8b98a3">'
            f'Sent to {_html.escape(who)}. '
            f'<a href="{_html.escape(u, quote=True)}" style="color:#8b98a3;'
            'text-decoration:underline">Unsubscribe</a> — stops every '
            'Oikonome email to this address (daily, weekly, monthly and '
            'yearly). The household owner can turn it back on under '
            'Settings → Email.</p>')
    else:
        plain = (f"\n\n--\nSent to {who}. To stop these emails, ask the "
                 "household owner to mute this address under Settings → "
                 "Email.")
        html = (
            '<p style="margin:18px 0 0;font-size:12px;line-height:1.5;'
            'color:#8b98a3">'
            f'Sent to {_html.escape(who)}. To stop these emails, ask the '
            'household owner to mute this address under Settings → '
            'Email.</p>')
    return plain, html, u


def decorate(plain: str, html: str, tenant_id, email: str
             ) -> tuple[str, str, str]:
    """`(plain, html, url)` with the footer attached. Both email bodies end
    in a two-level wrapper (`…</div></div>`), so the footer goes INSIDE the
    inner container where it inherits the mail's background; anything of
    another shape just gets it appended."""
    fp, fh, u = footer(tenant_id, email)
    tail = "</div></div>"
    if html.rstrip().endswith(tail):
        cut = html.rstrip()
        html = cut[:-len(tail)] + fh + tail
    else:
        html = html + fh
    return plain.rstrip("\n") + fp + "\n", html, u


def apply(tenant_id: str, email: str) -> bool:
    """Mute this address in the tenant's `email_muted`. Idempotent. False
    only when the tenant could not be written (unknown id, DB down) — the
    page then says so instead of claiming success."""
    from ..db import tenancy
    from ..engine import budget
    who = _norm(email)
    if not who:
        return False
    try:
        tc = tenancy.tenant_connect(tenant_id)
    except Exception:                                      # noqa: BLE001
        log.exception("unsubscribe: could not open tenant %s", tenant_id)
        return False
    try:
        with budget.config_txn(tc) as cfg:
            muted = cfg.get("email_muted") or []
            if isinstance(muted, str):
                muted = [x.strip() for x in muted.split(",") if x.strip()]
            if not isinstance(muted, list):
                muted = []
            muted = [m for m in muted if str(m).strip().lower() != who]
            muted.append(who)
            cfg["email_muted"] = muted
        log.info("unsubscribe: muted %s for tenant %s", who, tenant_id)
        return True
    except Exception:                                      # noqa: BLE001
        log.exception("unsubscribe: mute failed for tenant %s", tenant_id)
        return False
    finally:
        tc.close()
