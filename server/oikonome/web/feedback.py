"""Feedback and bug-report pipeline.

One form on /feedback with two kinds — feedback and a bug report. The
person writes what happened (the bug kind prompts for what happened, what
they expected and where), optionally attaches a screenshot, and submits.
The app assembles a package — feedback.md (message + version), the Doctor
diagnostic bundle, the app's recent log lines (in-process ring buffer;
podman/docker logs aren't reachable from inside the container), and the
screenshot — stamps it with a feedback number (FB-YYYYMMDD-xxxx), then:

 * SMTP configured and OIKONOME_FEEDBACK_TO set → emails it there, or
 * otherwise → hands the person the zip to send on themselves.

Only the number + message are stored (feedback_reports); the package is
never persisted server-side.
"""

from __future__ import annotations

import collections
import datetime as dt
import io
import json
import logging
import os
from ..envnum import env_flag
import secrets
import zipfile

# no inbox by default: an operator names theirs in OIKONOME_FEEDBACK_TO
FEEDBACK_TO = ""
LOG_LINES = 500

# The two kinds one form takes. The kind rides in the mail subject so the
# inbox sorts itself, and heads the report so a reader knows which they
# are holding before the first line.
KINDS = ("feedback", "bug")
_HEADING = {"feedback": "Feedback", "bug": "Bug report"}


def normalize_kind(kind: str | None) -> str:
    """The default is feedback: every older client sends no kind at all."""
    k = (kind or "").strip().lower()
    return k if k in KINDS else "feedback"


# ---- in-process log capture -------------------------------------------------

class _RingHandler(logging.Handler):
    def __init__(self, maxlen: int = LOG_LINES):
        super().__init__()
        self.buf: collections.deque[str] = collections.deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        # Never capture records marked no_capture (the
        # password-reset link carries a live one-hour token and would
        # otherwise ride out inside a feedback package's app-logs.txt)
        if getattr(record, "no_capture", False):
            return
        try:
            line = self.format(record)
        except Exception:                         # noqa: BLE001
            return
        self.buf.append(_redact(line))


# the feedback bundle ships these log lines to support — scrub the
# secret shapes that could ride along beyond the reset link (setup/invite
# tokens, bearer creds, api keys, passwords, userinfo in URLs, our own
# ciphertext). Conservative: only touches key=value / URL-token shapes.
import re as _re

# Every one-time link the app mints, by URL shape. Query params: reset /
# setup / verify-email / recipient-invite / unsubscribe / act (?token=),
# export
# and dump downloads (?t=), admin-enrol (?ticket=), verification (?code=),
# and the admin-console / approval signup link (?invite=). Path segments
# whose NEXT segment is the token: legacy /invite/<token> and the Plaid
# hosted-link page /accounts/plaid/link/<link_token>[/status]. The lists
# are explicit rather than folded into the regexes because a shape that
# is minted but not listed rides out verbatim into both this bundle and
# the console's log snapshot. oikonome.sh's cmd_logs sed
# pipeline carries a copy of both (a shell script cannot import this
# module); tests/test_log_link_scrubbing.py holds the two copies equal, so
# extend BOTH when a new link shape appears.
TOKEN_QUERY_PARAMS = ("token", "t", "ticket", "code", "invite")
TOKEN_PATH_SEGMENTS = ("invite", "link")

_REDACTIONS = [
    (_re.compile(r"([?&](?:%s)=)[\w.\-]+" % "|".join(TOKEN_QUERY_PARAMS)),
     r"\1<redacted>"),
    (_re.compile(r"(/(?:%s)/)[\w.\-]{8,}" % "|".join(TOKEN_PATH_SEGMENTS)),
     r"\1<redacted>"),
    (_re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"), r"\1<redacted>"),
    (_re.compile(r"(?i)\b(token|api[_-]?key|secret|password|passwd|pwd|"
                 r"access[_-]?token|client[_-]?secret)"
                 r"(['\"]?\s*[:=]\s*['\"]?)[^\s'\"&,}]+"),
     r"\1\2<redacted>"),
    (_re.compile(r"://[^/\s:@]+:[^/\s@]+@"), r"://<redacted>@"),  # user:pass@
    # both envelope formats: tenant (v1) and control-plane (cp1 — TOTP
    # seeds under the master key)
    (_re.compile(r"(enc:(?:v1|cp1):)[\w=\-]+"), r"\1<redacted>"),
    # Email addresses. The ring logger captures WARNING/ERROR
    # lines, and several interpolate a raw address — the invite/decline
    # failure paths in web/app.py, report.send_each's per-recipient failure,
    # and worker.py's "daily email failed for N recipients" join. A feedback
    # bundle is sent off-instance, so on a household install one member's
    # click could carry another member's address off the box. The secret-shaped
    # rules above do not match a plain address. Local part is kept to one
    # character so a support thread can still tell two failures apart
    # without the address itself leaving the box.
    (_re.compile(r"\b([A-Za-z0-9])[A-Za-z0-9._%+\-]*@[A-Za-z0-9.\-]+"
                 r"\.[A-Za-z]{2,}"), r"\1<redacted>@<redacted>"),
]


def _redact(line: str) -> str:
    for pat, repl in _REDACTIONS:
        line = pat.sub(repl, line)
    return line


_ring: _RingHandler | None = None



def _single_tenant_instance() -> bool:
    """True only when this install demonstrably holds at most one tenant AND
    is not the hosted product. Fails CLOSED: any doubt returns False."""
    if env_flag("OIKONOME_HOSTED"):
        return False
    try:
        from ..db import tenancy
        admin = tenancy.admin_connect()
        try:
            # every row, pending_delete included: a household in its grace
            # period is still a household until purge, and the log ring
            # still holds its lines (the dump door counts the same way)
            n = admin.execute(
                "SELECT count(*) AS n FROM tenants").fetchone()["n"]
        finally:
            admin.close()
        return int(n) <= 1
    except Exception:                                     # noqa: BLE001
        logging.getLogger("oikonome.feedback").warning(
            "tenant-count check failed; withholding app logs from the "
            "feedback bundle", exc_info=True)
        return False

def install_ring_logger() -> None:
    """Attach the ring buffer to the root logger once (app startup)."""
    global _ring
    from .. import logsafe
    logsafe.install()
    if _ring is not None:
        return
    _ring = _RingHandler()
    _ring.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _ring.setLevel(logging.INFO)
    logging.getLogger().addHandler(_ring)


def recent_logs() -> str:
    if _ring is None or not _ring.buf:
        return "(no app log lines captured yet)"
    return "\n".join(_ring.buf)


# ---- package assembly -------------------------------------------------------

def new_number() -> str:
    return (f"FB-{dt.datetime.now(dt.timezone.utc):%Y%m%d}"
            f"-{secrets.token_hex(2)}")


def build_package(conn, tenant_id: str, number: str, message: str,
                  screenshot: tuple[str, bytes] | None = None,
                  kind: str = "feedback") -> bytes:
    """The zip a maintainer needs to act on a report: what the person
    said, what the instance looks like, what the app logged. NO
    transaction contents, NO tokens (doctor.bundle's own rule)."""
    from . import doctor
    kind = normalize_kind(kind)
    md = [f"# Oikonome {_HEADING[kind].lower()} {number}",
          f"kind: {kind}",
          f"submitted: {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC",
          "", f"## {_HEADING[kind]}", "",
          message.strip() or "(no message)", ""]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{number}/feedback.md", "\n".join(md))
        z.writestr(f"{number}/doctor-bundle.json",
                   json.dumps(doctor.bundle(tenant_id), indent=2, default=str))
        # The log ring is process-wide, so on a MULTI-TENANT box it carries
        # other households' log lines (emails, ids, error payloads) — PII
        # not tenant-scoped even though secret SHAPES are redacted.
        # (doctor.bundle above is tenant-scoped.)
        #
        # OIKONOME_HOSTED alone is not the gate: "self-host ⇒ single tenant"
        # is an assumption, not a fact — a self-hosted box where the operator
        # invited a second account is multi-tenant with the flag unset. Gate
        # on what actually matters: the real tenant count. Unknown (query
        # fails) is treated as multi — withholding logs degrades a support
        # bundle, leaking them exposes another household.
        if _single_tenant_instance():
            z.writestr(f"{number}/app-logs.txt", recent_logs())
        if screenshot:
            name, data = screenshot
            z.writestr(f"{number}/{name}", data)
    return buf.getvalue()


# ---- submission -------------------------------------------------------------

def feedback_to() -> str:
    # `or`, not a get() default: compose passes the var as "" when unset
    return os.environ.get("OIKONOME_FEEDBACK_TO") or FEEDBACK_TO


def smtp_configured(conn=None) -> bool:
    """Can this instance email? Settings-card config (needs a tenant
    conn) or env; env-only check when no conn given."""
    if conn is not None:
        from . import report
        return report.resolve_smtp(conn)["configured"]
    return bool(os.environ.get("OIKONOME_SMTP_HOST"))


def submit(conn, tenant_id: str, message: str,
           screenshot: tuple[str, bytes] | None = None,
           kind: str = "feedback") -> dict:
    """Returns {number, delivery, package?}: delivery 'emailed' when SMTP
    took it, else 'downloaded' with the zip for the person to send to
    feedback_to() themselves. Email failure falls back to the download —
    a broken SMTP config must never eat a bug report."""
    kind = normalize_kind(kind)
    label = _HEADING[kind].lower()
    number = new_number()
    package = build_package(conn, tenant_id, number, message, screenshot,
                            kind=kind)
    delivery = "downloaded"
    from . import report
    smtp = report.resolve_smtp(conn)
    if smtp["configured"] and feedback_to():
        import html as _html
        try:
            msg = message.strip() or "(no message)"
            report.send(
                f"[Oikonome] [{kind}] {number}",
                f"{msg}\n\n— {label} {number}; the diagnostic bundle "
                f"(instance state, recent app log, screenshot if any) is "
                f"attached as {number}.zip ({len(package):,} bytes). No "
                f"transaction contents, no tokens.",
                f"<pre style=\"white-space:pre-wrap;font:inherit\">"
                f"{_html.escape(msg)}</pre>"
                f"<p style=\"color:#666\">— {label} <b>{number}</b>; the "
                f"diagnostic bundle (instance state, recent app log, "
                f"screenshot if any) is attached as {number}.zip "
                f"({len(package):,} bytes). No transaction contents, no "
                f"tokens.</p>",
                [feedback_to()], bcc=False,
                attachments=[(f"{number}.zip", package, "application/zip")],
                smtp=smtp)
            delivery = "emailed"
        except Exception:                         # noqa: BLE001
            logging.getLogger("oikonome.feedback").exception(
                "feedback email failed — falling back to download")
    conn.execute(
        "INSERT INTO feedback_reports (number, message, delivery, kind) "
        "VALUES (%s,%s,%s,%s)", (number, message.strip(), delivery, kind))
    return {"number": number, "delivery": delivery,
            "package": None if delivery == "emailed" else package}
