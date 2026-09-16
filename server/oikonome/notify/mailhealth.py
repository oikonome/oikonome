"""Is outbound mail actually working? — the instance-wide answer.

Transactional mail (invites, verification, resets, operator alerts)
fails silently unless something watches it: `report.send` runs inside a
daemon thread, so a raise there becomes one log line nobody reads while
the console goes on saying "invite emailed" — a relay port blocked
upstream for an hour would go unnoticed until a user reports a missing
email.

Two signals live here, both written to `ops_state` so every reader (the
console banner, the mint door, Doctor) sees the same thing:

* **outcomes** — `report.send` records every success and failure
  (`mail_last_ok` / `mail_last_fail`), so the most recent real delivery
  attempt is always on record.
* **the probe** — the worker connects to the configured relay (EHLO →
  STARTTLS → AUTH → QUIT, no message) every half hour and records
  `smtp_probe`. A probe catches a dead path BEFORE anyone needed it.

`failing()` is the one question callers ask: is mail believed to be down
right now? The newest signal wins, by timestamp: a failed send or probe
that nothing newer has contradicted means down; a later passing probe or
delivered mail means up again. Operator email cannot carry
the bad news (it is the thing that is broken), so failure surfaces in the
console and Doctor and as a stdout `event=smtp_probe_failed` line; the
RECOVERY is mailed, once, when the probe goes red → green.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import smtplib
import ssl

log = logging.getLogger("oikonome.mail")

K_OK = "mail_last_ok"
K_FAIL = "mail_last_fail"
K_PROBE = "smtp_probe"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()   # µs: ordering matters


def _write(key: str, value: dict) -> None:
    """Best-effort; a bookkeeping failure must never fail a send."""
    try:
        from ..db import tenancy
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO ops_state (key, value, updated_at) "
                "VALUES (%s, %s, now()) ON CONFLICT (key) DO UPDATE SET "
                "value = EXCLUDED.value, updated_at = now()",
                (key, json.dumps(value)))
        finally:
            admin.close()
    except Exception:                                     # noqa: BLE001
        log.debug("mailhealth: could not persist %s", key, exc_info=True)


def _read(admin, key: str) -> dict | None:
    try:
        row = admin.execute("SELECT value FROM ops_state WHERE key = %s",
                            (key,)).fetchone()
    except Exception:                                     # noqa: BLE001
        return None
    raw = (row or {}).get("value")
    if not raw:
        return None
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else None
    except ValueError:
        return None


# ops_state is instance-wide and every reader of it (Doctor, the feedback
# ZIP, the console) is outside the sending tenant, so nothing that names a
# person or a relay may be persisted: no subject lines (an invite subject
# carries the inviter's address), no addresses, no exception payloads.
_EMAIL_RE = re.compile(r"[\w.+%-]+@[\w-]+(?:\.[\w-]+)+")
# dict / tuple / bracket payloads (smtplib replies, errno tags) in that order
_PAYLOAD_RES = (re.compile(r"\{.*\}", re.S), re.compile(r"\(.*\)", re.S),
                re.compile(r"\[[^\]]*\]"))
_KINDS = (("invite", "invite"), ("invited you", "invite"),
          ("verif", "verification"), ("confirm your email", "verification"),
          ("reset", "reset"), ("sign-in", "unlock"), ("unlock", "unlock"),
          ("welcome", "welcome"),
          ("mail:", "operator"), ("summary", "verdict"),
          ("alert", "alert"))


def classify(subject: str | None) -> str:
    """A coarse label for a subject line — the label is what persists,
    the subject itself never does."""
    low = (subject or "").lower()
    from .. import ext
    for needle, label in (*_KINDS, *ext.gate.mail_kinds()):
        if needle in low:
            return label
    return "mail"


def sanitize_error(exc: BaseException | str | None) -> str:
    """Class name plus a scrubbed first line. smtplib's recipient errors
    embed the recipient dict in their text, relay replies can echo the
    login user or hostname, and either would otherwise land verbatim in
    the instance-wide record."""
    if exc is None:
        return ""
    if isinstance(exc, BaseException):
        name, text = type(exc).__name__, str(exc)
    else:
        name, _, text = str(exc).partition(": ")
        if not text:
            name, text = "Error", name
    text = text.splitlines()[0] if text else ""
    for rx in _PAYLOAD_RES:
        text = rx.sub("", text)
    text = _EMAIL_RE.sub("[addr]", text)
    text = " ".join(text.split())[:120].strip(" :,-")
    return f"{name}: {text}" if text else name


def record(ok: bool, *, kind: str, to: list[str] | None,
           error: BaseException | str | None = None) -> None:
    """Called by report.send on every attempt. `kind` is a subject or a
    label — only its coarse class is kept; recipients are kept as a count,
    not addresses; the error is scrubbed. ops_state is operator-readable
    and tenant-blind: this is bookkeeping, not a log."""
    _write(K_OK if ok else K_FAIL,
           {"at": _now(), "kind": classify(kind), "n": len(to or []),
            **({"error": sanitize_error(error)} if not ok else {})})


def probe(smtp: dict | None = None) -> dict:
    """Connect to the relay and come straight back. Returns the result it
    also persists: {ok, at, host, port, error?}."""
    from ..web import report
    s = smtp or report._env_smtp()
    res: dict = {"at": _now(), "host": s.get("host"), "port": s.get("port")}
    if not s.get("configured"):
        res.update(ok=True, skipped="not configured")
        return res
    try:
        from ..web.netguard import pinned_smtp_host
        host = pinned_smtp_host(s["host"])
        with smtplib.SMTP(host, s["port"], timeout=15) as c:
            c._host = s["host"]
            c.ehlo()
            if s.get("starttls"):
                ctx = ssl.create_default_context()
                from ..envnum import env_flag
                if env_flag("OIKONOME_SMTP_NO_VERIFY"):
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                c.starttls(context=ctx)
                c.ehlo()
            if s.get("user") and s.get("password"):
                c.login(s["user"], s["password"])
        res["ok"] = True
    except Exception as e:                                # noqa: BLE001
        res["ok"] = False
        res["error"] = sanitize_error(e)
    return res


def run_probe(mail_operator=None) -> str:
    """The worker's half-hourly job. Persists the result and, on a
    red → green transition, mails the operator that the path is back
    (mail works again by definition at that moment). Returns a short
    disposition for the cron log."""
    from ..db import tenancy
    admin = tenancy.admin_connect()
    try:
        prev = _read(admin, K_PROBE)
    finally:
        admin.close()
    cur = probe()
    if cur.get("skipped"):
        return "skipped (no SMTP)"
    # keep the moment it first went bad so the banner can say "since"
    if not cur["ok"]:
        was_failing = bool(prev) and not prev.get("ok", True)
        cur["since"] = (prev.get("since") or prev.get("at")) if was_failing \
            else cur["at"]
    _write(K_PROBE, cur)
    if not cur["ok"]:
        # stdout, structured, so a host-side watcher can grep it — the
        # operator mailbox is on the far side of the broken path
        log.error("event=smtp_probe_failed host=%s port=%s error=%s",
                  cur["host"], cur["port"], cur.get("error"))
        return f"FAIL {cur.get('error')}"
    if prev and not prev.get("ok", True):
        since = prev.get("since") or prev.get("at") or "?"
        subject = "Oikonome mail: outbound path restored"
        plain = (f"The SMTP relay {cur['host']}:{cur['port']} answers again "
                 f"(failing since {since}, last error: "
                 f"{prev.get('error', '?')}).\n\nAnything the app tried to "
                 f"send in that window was written to the container log "
                 f"instead; invites can be re-sent from the console.")
        if mail_operator:
            try:
                mail_operator(subject, plain, f"<p>{plain}</p>")
            except Exception:                             # noqa: BLE001
                log.warning("restore notice not sent", exc_info=True)
        return "restored (mailed)"
    return "ok"


def status(admin=None) -> dict:
    """Everything a reader needs: {failing, probe, last_ok, last_fail,
    reason}. `failing` is the headline."""
    own = admin is None
    if own:
        from ..db import tenancy
        admin = tenancy.admin_connect()
    try:
        pr, ok, fail = (_read(admin, K_PROBE), _read(admin, K_OK),
                        _read(admin, K_FAIL))
    finally:
        if own:
            admin.close()
    # The newest signal decides. A probe that passed after the last failed
    # send means the relay is back (the failure may have been one bounced
    # address, not a dead path); a probe that failed after the last good
    # send means it is down even though nothing has been sent since.
    probe_bad = bool(pr) and not pr.get("ok", True)
    good = [x for x in (ok, None if probe_bad else pr) if x]
    bad = [x for x in (fail, pr if probe_bad else None) if x]
    good_at = max((x.get("at", "") for x in good), default="")
    bad_at = max((x.get("at", "") for x in bad), default="")
    failing, reason = False, ""
    if bad and bad_at > good_at:
        failing = True
        if probe_bad and pr.get("at", "") == bad_at:
            reason = (f"relay probe failing since "
                      f"{pr.get('since') or pr.get('at')} — "
                      f"{pr.get('error', '')}")
        else:
            reason = (f"last send failed at {fail.get('at')} "
                      f"({fail.get('kind', '?')}): {fail.get('error', '')}")
    return {"failing": failing, "reason": reason, "probe": pr,
            "last_ok": ok, "last_fail": fail, "since": bad_at if failing else ""}


def configured() -> bool:
    from ..web import report
    return bool(report._env_smtp().get("configured"))


__all__ = ["record", "probe", "run_probe", "status", "configured",
           "classify", "sanitize_error", "K_OK", "K_FAIL", "K_PROBE"]
