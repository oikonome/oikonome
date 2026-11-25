"""The help topics (web/help.py), the export ticket doors and the manual
connection sync (web/pages.py)."""

from __future__ import annotations

from typing import Literal

from . import Shape
from .access import StepUp


class HelpTopic(Shape):
    id: str
    title: str
    body: str
    """The topic's Markdown."""
    group: Literal["setup", "selfhost", "reference", "guides"]


class HelpTopics(Shape):
    """GET /api/help: every shipped document, in reading order."""
    topics: list[HelpTopic]


ExportKind = Literal["zip", "dump", "continuity"]


class ExportTicketAsk(StepUp):
    """POST /api/export/token; kind defaults to zip."""
    kind: ExportKind = "zip"


class ExportTicket(Shape):
    """A one-shot download ticket: navigate to `url` within
    `expires_in` seconds."""
    ok: Literal[True]
    token: str
    url: str
    expires_in: int


class ExportLast(Shape):
    """GET /api/export/last: when the last full-data export was built
    (null when none has been) and what a restore would refuse in it."""
    at: str | None
    warnings: list[str]


class AccountSyncAsk(Shape):
    item_id: str


class AccountSynced(Shape):
    """POST /api/accounts/sync: a refused or failed pull is an error
    status carrying the message, so this reply is a success."""
    ok: Literal[True]
    message: str
