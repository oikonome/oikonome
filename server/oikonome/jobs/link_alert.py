"""Proactive email when a linked institution's connection breaks — or
recovers. Called from the worker's sync sweep with the item-status
transitions that run observed.

Dedup is free: only healthy<->link-issue TRANSITIONS alert, so a
steady-state outage (old == new == error:...) fires exactly once — not
every hour — and a later recovery, or a fresh break after a recovery,
re-triggers. No separate state file: the items table IS the memory.

Recipients are per-tenant (the same resolution as the daily email),
delivery goes through web/report.send, and the re-link CTA points at
OIKONOME_BASE_URL/accounts (relative when the self-hosted operator hasn't
set a base URL).
"""

from __future__ import annotations

import html


def _accounts_url() -> str:
    """Empty when the base URL is not safe to hyperlink in recurring mail —
    a LAN/IP base as an email's only link gets the message silently spam-
    filtered by the big providers, and this alert rides the same relay to
    the same inboxes. The callers drop the CTA and say where to click
    instead."""
    from ..web.report import mailable_base
    base = mailable_base()
    return f"{base}/accounts" if base else ""


def _is_link_issue(status: str | None) -> bool:
    """True for statuses that need the user to re-authenticate / re-link, as
    opposed to transient blips that self-heal on the next sync (network
    errors, RATE_LIMIT_EXCEEDED, API_ERROR, INSTITUTION_NOT_RESPONDING /
    INSTITUTION_DOWN — alerting on those would send a break+recover email
    pair per blip)."""
    if not status or status == "ok":
        return False
    return (status == "login_required"
            or status.startswith("error:ITEM")
            or status.startswith("error:PENDING"))


def _describe(status: str) -> str:
    if status in ("login_required", "error:ITEM_LOGIN_REQUIRED"):
        return "login expired — needs re-authentication"
    if status == "error:ITEM_NOT_SUPPORTED":
        return ("account not currently supported (often an institution "
                "migration or outage) — may need re-linking via the new provider")
    if status.startswith("error:PENDING"):
        return "consent expiring soon — re-authenticate to keep it live"
    if status.startswith("error:INSTITUTION"):
        return "institution-side connection issue (usually clears on its own)"
    if status.startswith("error:"):
        return "connection error (" + status.split("error:", 1)[1] + ")"
    return status


def _transitions_split(transitions: list[dict]):
    broke = [t for t in transitions
             if _is_link_issue(t.get("new")) and not _is_link_issue(t.get("old"))]
    fixed = [t for t in transitions
             if t.get("new") == "ok" and _is_link_issue(t.get("old"))]
    return broke, fixed


def build_alert(transitions: list[dict]) -> tuple[str, str, str] | None:
    """(subject, plain, html) for the newly-broken / newly-recovered
    connections in `transitions`, or None when nothing is worth an email.
    Each transition is {name, old, new, error?}."""
    broke, fixed = _transitions_split(transitions)
    if not broke and not fixed:
        return None
    accounts_url = _accounts_url()

    if broke:
        names = ", ".join(t["name"] for t in broke)
        subject = (f"⚠️ Oikonome: {names} "
                   + ("connections need attention" if len(broke) > 1
                      else "connection needs attention"))
    else:
        names = ", ".join(t["name"] for t in fixed)
        subject = f"✅ Oikonome: {names} reconnected"

    # ---- plain text ----
    p: list[str] = []
    if broke:
        p.append("A linked account's connection needs attention:\n")
        for t in broke:
            p.append(f"  • {t['name']}: {_describe(t['new'])}")
        if accounts_url:
            p.append(f"\nRe-link it: {accounts_url}  (Bank connections → fix)")
        else:
            p.append("\nRe-link it on the Accounts page (Bank connections "
                     "→ fix)")
    if fixed:
        p.append("\nBack online:")
        for t in fixed:
            p.append(f"  • {t['name']}")
    plain = "\n".join(p)

    # ---- html (all styles inline; no <style>/<script>/<img>) ----
    e = html.escape
    h = ['<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;'
         'font-size:15px;color:#1a1a1a;max-width:560px">']
    if broke:
        h.append('<div style="border:1px solid #e5b4b4;background:#fdf3f3;'
                 'border-radius:8px;padding:14px 16px;margin-bottom:12px">'
                 '<div style="font-weight:600;color:#b91c1c;margin-bottom:6px">'
                 '⚠️ Connection needs attention</div>')
        for t in broke:
            h.append(f'<div style="margin:4px 0"><b>{e(t["name"])}</b>'
                     f' &mdash; {e(_describe(t["new"]))}</div>')
        if accounts_url:
            h.append(f'<div style="margin-top:10px"><a href="{e(accounts_url)}" '
                     'style="color:#2563eb;text-decoration:none">Re-link on the '
                     'Accounts page ↗</a> <span style="color:#666">(Bank '
                     'connections → fix ↗)</span></div></div>')
        else:
            h.append('<div style="margin-top:10px;color:#666">Re-link on '
                     'the Accounts page (Bank connections → fix)</div></div>')
    if fixed:
        h.append('<div style="border:1px solid #b4d7b8;background:#f2faf3;'
                 'border-radius:8px;padding:14px 16px">'
                 '<div style="font-weight:600;color:#15803d;margin-bottom:6px">'
                 '✅ Back online</div>')
        for t in fixed:
            h.append(f'<div style="margin:4px 0"><b>{e(t["name"])}</b></div>')
        h.append('</div>')
    h.append('<div style="color:#888;font-size:12px;margin-top:12px">'
             'Sent by Oikonome sync when a connection&rsquo;s status changes.'
             '</div>')
    h.append('</div>')
    return subject, plain, "".join(h)


def notify(transitions: list[dict], recipients: list[str],
           smtp: dict | None = None,
           unsubscribe: str | None = None) -> dict | None:
    """Build and send the alert. Returns a small summary (or None if nothing
    was sent). Callers wrap in try/except — a mail failure must never break
    the sync sweep."""
    built = build_alert(transitions)
    if not built or not recipients:
        return None
    subject, plain, html_body = built
    from ..web import report
    report.send_each(subject, plain, html_body, recipients, smtp=smtp,
                     unsubscribe=unsubscribe)
    broke, fixed = _transitions_split(transitions)
    return {"broke": [t["name"] for t in broke],
            "fixed": [t["name"] for t in fixed]}
