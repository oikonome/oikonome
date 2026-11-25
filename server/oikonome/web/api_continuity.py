"""The continuity packet.
Routes register on the shared API router (api_common.router); api.py
imports this module at a fixed point so the route order is preserved
(specs/api-routers.md).
"""

from __future__ import annotations

import re
from fastapi import Depends, HTTPException

from . import demoguard, mailguard
from ..engine import activity as _activity
from .api_common import (_conn, _is_owner, _owner_only, _scalar_text,
                         _user, router)
from .fields import Fields, json_fields
from .schemas import documented
from .schemas.continuity import (Continuity, ContinuityEmail, ContinuityEmailed,
                                 ContinuitySave, ContinuitySaved)
from .security import limit


# ---- the continuity packet: the household's money, on paper, for whoever
# is left to run it. The PDF itself leaves through pages.py's ticket door;
# these are its two fields and the emailed copy.

_CONTINUITY_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.get("/continuity")
@documented(response=Continuity)
def continuity_get(user: dict = Depends(_user())):
    """The owner's two fields (who it is for, where the rest is) and what
    the email door will accept, so the clients can offer the right thing:
    on hosted, only a household member's address; on self-host, any.

    The note's text goes to the owner alone, like the PDF it heads: it
    says where the will and the passwords are. Everyone else is told
    whether one is written (`note_set`) and that they may not read it
    (`note_readable`), never what it says."""
    from ..engine import budget, continuity
    from .report import resolve_smtp
    conn = _conn(user)
    try:
        cfg = budget.load_config(conn)
        out = continuity.settings_of(cfg)
        out["mail_configured"] = bool(resolve_smtp(conn)["configured"])
        out["members_only"] = mailguard.members_only()
        # every member's address, which the shared demonstration login is
        # shown only its own of; and the owner's note (where the will and
        # the passwords are) and who it is for are withheld from it too
        out["members"] = demoguard.own_addresses_only(user, [
            r["email"] for r in conn.execute(
                "SELECT email FROM users WHERE tenant_id = "
                "current_setting('app.tenant_id')::uuid ORDER BY created_at"
            ).fetchall()])
        if demoguard.people_hidden(user):
            out["prepared_for"] = ""
        out["note_set"] = bool(out["note"])
        out["note_readable"] = (_is_owner(user)
                                and not demoguard.people_hidden(user))
        if not out["note_readable"]:
            out["note"] = ""
        out["note_max"] = continuity.NOTE_MAX
        return out
    finally:
        conn.close()


@router.put("/continuity")
@documented(response=ContinuitySaved, body=ContinuitySave)
def continuity_put(user: dict = Depends(_user()),
                   body: Fields = Depends(json_fields)):
    """Save the note and the name. Owner only: the note says where the
    will and the passwords are, which is the owner's to write."""
    _owner_only(user)
    demoguard.deny(user)
    if demoguard.people_hidden(user):
        # it is shown an empty note, so a save from it would erase the
        # owner's
        raise HTTPException(403, demoguard.SHARED_PEOPLE_DENIED)
    from ..engine import budget, continuity
    try:
        # absent = leave it; null or "" = clear it
        note = (continuity.clean_note(body.text("note", strip=False))
                if body.present("note") else None)
        who = (continuity.clean_for(body.text("prepared_for", strip=False))
               if body.present("prepared_for") else None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    conn = _conn(user)
    try:
        changed = []
        with budget.config_txn(conn) as cfg:
            for key, val in ((continuity.KEY_NOTE, note),
                             (continuity.KEY_FOR, who)):
                if val is None:
                    continue
                if (cfg.get(key) or "") != val:
                    changed.append(key)
                if val:
                    cfg[key] = val
                else:
                    cfg.pop(key, None)
            out = continuity.settings_of(cfg)
        if changed:
            _activity.record(conn, user, "settings", "changed",
                             detail={"keys": changed})
        return {"ok": True, **out}
    finally:
        conn.close()


@router.post("/continuity/email",
             dependencies=[Depends(limit("continuity_email", 6, 3600))])
@documented(response=ContinuityEmailed, body=ContinuityEmail)
def continuity_email(user: dict = Depends(_user()),
                     body: Fields = Depends(json_fields)):
    """Mail the packet to one address. The same fresh elevation the
    download demands — this is the whole shape of the household's money
    leaving the instance, and a stolen cookie must not be able to send it
    anywhere. The recipient is held to the household mail rule: hosted
    mails balances only to an address with an account here, self-host to
    any address the operator's relay will carry."""
    _owner_only(user)
    demoguard.deny(user)
    from .app import _require_elevation
    from .pages import _continuity_pdf
    from .report import resolve_smtp
    from .report import send as _send
    _require_elevation(user, fresh_seconds=60,
                       password=_scalar_text(body, "password"),
                       totp_code=_scalar_text(body, "totp_code"),
                       recovery_code=_scalar_text(body, "recovery_code"))
    to = mailguard.parse_recipients([body.text("to", strip=False)], cap=1)
    # length before shape: the shape regex backtracks quadratically on a
    # long run of dots after the first @, and parse_recipients keeps an
    # unparseable entry verbatim at whatever length it arrived
    if not to or len(to[0]) > 254 or not _CONTINUITY_ADDRESS.match(to[0]):
        raise HTTPException(400, "a valid email address is required")
    conn = _conn(user)
    try:
        mailguard.check_recipients(conn, to)
        smtp = resolve_smtp(conn)
        if not smtp["configured"]:
            raise HTTPException(400, "email is not configured on this "
                                     "instance — download the packet instead")
        pdf, name = _continuity_pdf(user)
        # the sender's address is account-supplied text; in the HTML part
        # it is escaped like any other, so it reads as text and never as
        # markup (a link dressed as a sign-in prompt, say)
        import html as _html
        who = to[0]
        plain = ("Attached is the continuity packet for the household in "
                 "Oikonome, printed today: every account and where it is "
                 "held, the balances that day, the recurring bills and what "
                 "pays them, the income, and the businesses. It holds no "
                 "passwords — its first section says where those are "
                 f"kept.\n\nSent by {user['email']} from Oikonome.")
        html = ("<p>Attached is the continuity packet for the household in "
                "Oikonome, printed today: every account and where it is "
                "held, the balances that day, the recurring bills and what "
                "pays them, the income, and the businesses. It holds no "
                "passwords — its first section says where those are "
                f"kept.</p><p>Sent by {_html.escape(user['email'])} from "
                "Oikonome.</p>")
        try:
            _send("Continuity packet from Oikonome", plain, html, [who],
                  attachments=[(name, pdf, "application/pdf")],
                  smtp=smtp, bcc=False)
        except Exception as e:                             # noqa: BLE001
            raise HTTPException(502, "the mail relay refused it: "
                                     f"{type(e).__name__}")
        _activity.record(conn, user, "settings", "continuity_emailed",
                         label=who)
        return {"ok": True, "to": who}
    finally:
        conn.close()
