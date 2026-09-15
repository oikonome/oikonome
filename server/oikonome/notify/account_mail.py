"""The one mail that outlives an account: the receipt that its erasure
happened, sent to the address that asked for it. Plain, best-effort, and
never raised — the erasure has already committed.

Also the sentence every mail about deletion carries (the person can take
everything with them, any time), and the sender the other notice mails
share.
"""
from __future__ import annotations

import logging

log = logging.getLogger("oikonome.notify")


def export_line(url: str) -> tuple[str, str]:
    """Points at Settings → Data, where the export lives."""
    ex = url.replace("/settings/billing", "/settings/data") if url else ""
    plain = ("Everything you have in Oikonome — accounts, transactions, "
             "bills, budgets, notes — can be downloaded any time as a "
             "standard-format archive from Settings → Data → Export"
             + (f" ({ex})" if ex else "") + ", before or after this date.")
    html = ("Everything you have in Oikonome — accounts, transactions, "
            "bills, budgets, notes — can be downloaded any time as a "
            "standard-format archive from "
            + (f'<a href="{ex}">Settings → Data → Export</a>' if ex else
               "Settings → Data → Export") + ", before or after this date.")
    return plain, html


def send(email: str, subject: str, plain: str, html: str,
         *, kind: str = "notice") -> bool:
    """Best-effort send over the instance's mail; True when it went out."""
    from ..web import report as _report
    if not email:
        return False
    smtp = _report._env_smtp()
    if not smtp["configured"]:
        log.info("%s mail to %s not sent: mail is not configured", kind, email)
        return False
    try:
        _report.send(subject, plain, html, [email], smtp=smtp, bcc=False)
        return True
    except Exception:                    # noqa: BLE001 — never raise here
        log.exception("%s mail to %s failed", kind, email)
        return False


def account_deleted(email: str, *, extra_plain: str = "",
                    extra_html: str = "") -> bool:
    """The erasure receipt. `extra_*` is anything the erasure could not
    undo on the person's behalf, supplied by whoever knows."""
    body = ("Your Oikonome account and everything in it have been "
            "deleted.")
    return send(email, "Your Oikonome account has been deleted",
                body + extra_plain, f"<p>{body}</p>{extra_html}",
                kind="account-deleted")
