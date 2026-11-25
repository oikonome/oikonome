"""The assistant chat (web/api_assistant.py)."""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field

from . import Shape


class AiDestination(Shape):
    where: Literal["local", "remote", "off"]
    host: str


class AiRoles(Shape):
    categorize: AiDestination
    assistant: AiDestination
    vision: AiDestination


class AssistantStatus(Shape):
    """GET /api/assistant. local=false means answers, and the ledger
    figures the tools return, travel to `host`."""
    available: bool
    local: bool
    host: str
    roles: AiRoles


class TextBlock(Shape):
    kind: Literal["text"]
    text: str


class SeriesSpec(Shape):
    name: str
    values: list[float]


class RingRow(Shape):
    label: str
    dollars: int
    """whole dollars; the rows add up to the ring's total"""
    share: int | None
    """whole percent; the rows add up to 100"""


class RingShown(Shape):
    """What a ring prints (assistant_blocks.ring_figures): the centre
    total and each drawn (positive) part, in order."""
    total: int
    rows: list[RingRow]


class ChartBlock(Shape):
    kind: Literal["bar", "line", "pie"]
    title: str
    labels: list[str]
    series: list[SeriesSpec]
    shown: RingShown | None = None
    """a ring's printed figures; sent on every pie, never on a bar or a
    line"""


class TableBlock(Shape):
    kind: Literal["table"]
    title: str
    columns: list[str]
    rows: list[list[float | str]]


AnswerBlock = Annotated[Union[TextBlock, ChartBlock, TableBlock],
                        Field(discriminator="kind")]


class AssistantAsk(Shape):
    question: str
    thread_id: str | None = None
    """the conversation a follow-up continues"""


class AssistantAnswer(Shape):
    """POST /api/assistant: `answer` is the prose alone, `blocks` the
    ordered text, chart and table pieces."""
    answer: str
    blocks: list[AnswerBlock]
    tools_used: list[str]
    id: str | None
    saved: bool
    removed: bool
    thread_id: str | None


class ReportTurn(Shape):
    question: str
    blocks: list[AnswerBlock]


class AssistantReport(Shape):
    """POST /api/assistant/report (answers with a printable page): one
    answer, or with `turns` a whole conversation."""
    question: str = ""
    blocks: list[AnswerBlock] = []
    turns: list[ReportTurn] = []
    partial: bool = False


class AssistantTurn(Shape):
    id: str
    thread_id: str
    asked_at: str
    asked_by: str
    question: str
    answer: str
    blocks: list[AnswerBlock]
    tools_used: list[str]


class HistoryCursor(Shape):
    before: str
    before_id: str


class AssistantHistory(Shape):
    """GET /api/assistant/history, newest first."""
    turns: list[AssistantTurn]
    more: bool
    next: HistoryCursor | None


class AssistantForgotten(Shape):
    """DELETE /api/assistant/history/{turn_id}: a conversation's opening
    turn stays while follow-ups remain, and `reason` says so."""
    ok: Literal[True]
    deleted: int
    reason: str = ""


class AssistantCleared(Shape):
    ok: Literal[True]
    deleted: int
