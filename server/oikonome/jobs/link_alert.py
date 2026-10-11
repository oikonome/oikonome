"""Proactive email when a linked institution's connection breaks — or
recovers. Called from the worker's sync sweep with the item-status
transitions that run observed.

Dedup: only healthy<->link-issue TRANSITIONS alert, so a steady-state
outage (old == new == error:...) fires exactly once — not every hour — and
a later recovery, or a fresh break after a recovery, re-triggers. The
"old" side of the edge is what was last ANNOUNCED for the item (a job_runs
row per item, `announced_edges` / `stamp_announced`), not the status the
sweep read when its pass began: every door that pulls a bank writes that
status itself — a bank-webhook pull, a one-bank refresh — so whichever of
them meets a break first would otherwise leave the sweep reading the
error as the old status, finding no edge, and never telling anyone.

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


# job_runs key prefix of the per-item announced state: note = "issue" when
# a break was last announced, "ok" when a healthy state (or a recovery) was
_ANNOUNCED = "link-alert:"
# the same per-item state for the connection.changed webhook, kept apart
# from the mail's: the webhook does not go through the mail relay, so an
# edge held back for a mail retry must not be posted to the receiver again
# on every retry pass, and the receiver must still hear the edge the moment
# it is seen rather than when the relay comes back
HOOK_ANNOUNCED = "link-hook:"


def _health(status: str | None) -> str | None:
    """"issue" / "ok" for the two states an announcement is about; None for
    a transient blip, which announces nothing and changes nothing."""
    if _is_link_issue(status):
        return "issue"
    return "ok" if status == "ok" else None


def announced_edges(conn, transitions: list[dict],
                    prefix: str = _ANNOUNCED) -> list[dict]:
    """`transitions` with each item's `old` replaced by what was last
    announced for it, where that is known. An item never announced (a
    fresh link, or the first sweep after this memory existed) keeps the
    status the pass read, which is the best evidence there is. `prefix`
    picks whose announcements: the mail's, or the webhook's."""
    ids = [t["id"] for t in transitions if t.get("id")]
    if not ids:
        return transitions
    state = {r["job"][len(prefix):]: r["note"] for r in conn.execute(
        "SELECT job, note FROM job_runs WHERE job = ANY(%s)",
        ([prefix + i for i in ids],)).fetchall()}
    out = []
    for t in transitions:
        s = state.get(t.get("id"))
        if s == "issue":
            t = dict(t, old=t["old"] if _is_link_issue(t["old"])
                     else "login_required")
        elif s == "ok":
            t = dict(t, old="ok")
        out.append(t)
    return out


def stamp_announced(conn, transitions: list[dict],
                    prefix: str = _ANNOUNCED) -> None:
    """Record, per item, the state this sweep has now announced (or found
    nothing to announce about). Written only when it changes."""
    for t in transitions:
        h = _health(t.get("new"))
        if not t.get("id") or h is None:
            continue
        conn.execute(
            """INSERT INTO job_runs (job, ran_at, note)
               VALUES (%s, now(), %s)
               ON CONFLICT (tenant_id, job) DO UPDATE
               SET ran_at = now(), note = EXCLUDED.note
               WHERE job_runs.note IS DISTINCT FROM EXCLUDED.note""",
            (prefix + t["id"], h))


# job_runs row that bounds the retry of a link alert whose mail every
# recipient refused: ran_at = the first refusal, note = how many sweeps
# have failed it. A relay that stays broken (a wrong password on a
# self-host) must not hold the edge, and the push waiting on it, forever.
_MAIL_RETRY = "link-alert-mail-retry"
MAIL_RETRY_SWEEPS = 6
MAIL_RETRY_HOURS = 24


def mail_retry_exhausted(conn) -> bool:
    """Record one more refused send of the pending link alert and say
    whether to stop retrying: True once the mail has failed on
    MAIL_RETRY_SWEEPS sweeps or for MAIL_RETRY_HOURS since the first
    refusal. The caller then announces the edge without the mail."""
    row = conn.execute(
        """INSERT INTO job_runs (job, ran_at, note) VALUES (%s, now(), '1')
           ON CONFLICT (tenant_id, job) DO UPDATE
           SET note = ((COALESCE(NULLIF(job_runs.note, ''), '0'))::int + 1)::text
           RETURNING note, ran_at <= now() - make_interval(hours => %s) AS old""",
        (_MAIL_RETRY, MAIL_RETRY_HOURS)).fetchone()
    try:
        n = int(row["note"])
    except (TypeError, ValueError):
        n = MAIL_RETRY_SWEEPS
    if n >= MAIL_RETRY_SWEEPS or row["old"]:
        clear_mail_retry(conn)
        return True
    return False


def clear_mail_retry(conn) -> None:
    """The pending alert went out (or there is none): start counting
    afresh at the next refusal."""
    conn.execute("DELETE FROM job_runs WHERE job=%s", (_MAIL_RETRY,))


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
    results = report.send_each(subject, plain, html_body, recipients,
                               smtp=smtp, unsubscribe=unsubscribe)
    broke, fixed = _transitions_split(transitions)
    # `delivered` False = every recipient's send failed on a relay that is
    # configured: the caller must not record these edges as announced, or
    # the outage that refused the mail loses the alert for good. No relay
    # at all is a setting, not a failure — retrying it would never end.
    no_relay = isinstance(smtp, dict) and smtp.get("configured") is False
    return {"broke": [t["name"] for t in broke],
            "fixed": [t["name"] for t in fixed],
            "delivered": no_relay or any(r.get("ok") for r in results or [])}
