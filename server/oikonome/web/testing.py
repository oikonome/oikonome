"""In-app feedback: a message + optional screenshot, emailed to
the maintainer when SMTP is configured or handed back as a downloadable
zip the user sends manually. Lives under /api/testing/* for route
compatibility.
"""

from __future__ import annotations

import os

from fastapi import (APIRouter, Depends, File, Form, HTTPException,
                     UploadFile)
from fastapi.responses import RedirectResponse, Response

from ..db import tenancy
from .security import limit

MAX_SCREENSHOT = 10 * 1024 * 1024

router = APIRouter()


def _user():
    from .app import current_user
    return current_user


@router.get("/testing")
def testing_page():
    """Old links land on Feedback."""
    return RedirectResponse("/app/feedback", status_code=303)


async def _read_screenshot(screenshot: UploadFile | None
                           ) -> tuple[str, bytes] | None:
    """Returns the (name, bytes) attachment, None when absent, or raises
    ValueError when over the cap."""
    if screenshot is None or not screenshot.filename:
        return None
    # Bounded stream — a plain read() buffers the whole body first, so a
    # multi-GB upload OOMs the host before the length check ever runs.
    got = 0
    chunks: list[bytes] = []
    while True:
        chunk = await screenshot.read(1024 * 1024)
        if not chunk:
            break
        got += len(chunk)
        if got > MAX_SCREENSHOT:
            raise ValueError("screenshot too large (10 MB max)")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        return None
    ext = os.path.splitext(screenshot.filename)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        ext = ".png"
    return ("screenshot" + ext, data)


def _submit_feedback(user: dict, message: str,
                     shot: tuple[str, bytes] | None,
                     kind: str = "feedback") -> dict:
    from . import feedback as feedback_mod
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        return feedback_mod.submit(conn, str(user["tenant_id"]), message, shot,
                                   kind=kind)
    finally:
        conn.close()


def _zip_response(r: dict) -> Response:
    return Response(
        r["package"], media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="oikonome-feedback-{r["number"]}.zip"',
                 "X-Feedback-Number": r["number"]})


# ---- JSON endpoints for the SPA Feedback page -------------------------------

def _feedback_enabled(conn) -> bool:
    from ..engine import budget
    return budget.load_config(conn).get("feedback_enabled") is not False


@router.get("/api/testing")
def testing_api(user: dict = Depends(_user())):
    from . import demoguard
    from . import feedback as feedback_mod
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        # demo: off and not switchable — the submit door already denies
        # demos, this stops the UI offering it at all
        enabled = (_feedback_enabled(conn)
                   and not demoguard.is_demo(user["tenant_id"]))
        can_email = (feedback_mod.smtp_configured(conn)
                     and bool(feedback_mod.feedback_to()))
    finally:
        conn.close()
    # The inbox address is only handed to the client when the client
    # NEEDS it: with no mail path the person sends the downloaded zip
    # themselves, so they must know where. When the server delivers the
    # mail, the operator's inbox stays on the server.
    to = "" if can_email else feedback_mod.feedback_to()
    return {"enabled": enabled, "can_email": can_email, "feedback_to": to}


@router.post("/api/testing/feedback",
             dependencies=[Depends(limit("feedback", 10, 3600))])
async def testing_feedback_api(user: dict = Depends(_user()),
                               message: str = Form(""),
                               kind: str = Form("feedback"),
                               screenshot: UploadFile | None = File(None)):
    """SPA flavor: JSON {number, delivery} when emailed; the zip itself
    (with X-Feedback-Number) when the tester must send it manually.
    `kind` is feedback (the default, and what older clients send) or bug."""
    from . import feedback as feedback_mod
    if kind.strip().lower() not in feedback_mod.KINDS:
        raise HTTPException(400, "kind must be feedback or bug")
    # A demo visitor could relay bounded spam to the operator's feedback
    # address (demoguard covers data doors but not this). Deny outright on
    # a demo instance.
    from . import demoguard
    demoguard.deny(user)
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        enabled = _feedback_enabled(conn)
    finally:
        conn.close()
    if not enabled:
        raise HTTPException(403, "feedback is turned off on this instance")
    try:
        shot = await _read_screenshot(screenshot)
    except ValueError as e:
        raise HTTPException(413, str(e))
    r = _submit_feedback(user, message, shot, kind=kind.strip().lower())
    if r["delivery"] == "emailed":
        return {"number": r["number"], "delivery": "emailed"}
    return _zip_response(r)
