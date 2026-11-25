"""Reimbursement pairing (web/api_reimburse.py)."""

from __future__ import annotations

from typing import Literal

from . import Shape


class PendingReimb(Shape):
    """A charge flagged as awaiting reimbursement."""
    id: str
    date: str
    amount: float
    pending: int
    payee: str
    category: str
    account: str | None
    flagged_at: str
    partial: int
    """1: only part of it is expected back"""
    expected: float | None
    received: float
    received_pct: int
    """Received over what is expected back (the charge when no narrower
    figure was given), whole percent, at most 100
    (alerts.reimb_received_pct)."""
    waited_label: str | None = None
    """how long it has been out of pocket, in the household's days:
    "today", "waiting 1 day", "waiting 12 days" """
    left_amount: float | None = None
    """what of it is still to be matched (data.reimb_left: the charge less
    what came back, never below 0) — the same figure the candidates door's
    `anchor.left_amount` states; the matcher's "of $X" before that door
    answers"""


class ReimbPending(Shape):
    """GET /api/reimburse/pending, oldest first."""
    pending: list[PendingReimb]
    owed: float
    """Still owed back across them (alerts.reimb_owed)."""
    oldest_label: str | None = None
    """how long the oldest has waited ("waiting 12 days"); null when none
    has waited a day"""


class ReimbPair(Shape):
    expense_id: str
    reimburse_id: str
    partial: int
    received: float | None
    created_at: str
    expense_date: str
    expense_amount: float
    expense_payee: str
    deposit_date: str
    deposit_amount: float
    deposit_payee: str
    amount_label: str | None = None
    """what the pair nets: "$80.00", or "$30.00 of $80.00" when partial"""
    deposit_note: str | None = None
    """for a deposit dated more than a few days before the charge it
    repays, how far ahead it came ("9 days before the charge"); null for
    an ordinary repayment"""


class ReimbPairsPage(Shape):
    """GET /api/reimburse/pairs: linked pairs, newest charge first."""
    pairs: list[ReimbPair]
    more: bool
    page: int


class ReimbAnchor(Shape):
    """The transaction the candidates are for: what it still needs
    (`left_amount`) and what already came back to it (`received`)."""
    id: str
    date: str
    amount: float
    account_id: str | None
    entity: str | None
    left_amount: float
    received: float


class ReimbLinkedPair(Shape):
    """A pair the anchor is already in, with its counterpart's details."""
    expense_id: str
    reimburse_id: str
    partial: int
    received: float | None
    other_id: str
    date: str
    amount: float
    payee: str


class ReimbCandidate(Shape):
    id: str
    date: str
    amount: float
    pending: int
    payee: str
    category: str
    account: str | None
    left_amount: float
    """what of it no link has claimed yet: the figure the list ranks by"""


class ReimbCandidates(Shape):
    """GET /api/reimburse/{txn_id}/candidates."""
    anchor: ReimbAnchor
    pairs: list[ReimbLinkedPair]
    candidates: list[ReimbCandidate]


class ReimbLink(Shape):
    txn_id: str
    other_ids: list[str]
    partial: bool = False


class ReimbLinked(Shape):
    """POST /api/reimburse/link."""
    linked: int
    errors: list[str]
    txn_id: str
    partial: int = 0
    """how many of the links were partial (absent when none)"""
    notes: list[str] = []


class ReimbSelection(Shape):
    """POST /api/reimburse/selection: what Pair would do with the ticked
    rows, worked out by the link rule and undone. Nothing is linked.
    More rows ticked than a person picks by hand (over 50) are not
    weighed: every field is null and the clients print no line."""
    selected: float | None
    """what the ticked rows have left to give; null when not weighed"""
    need: float | None
    """what the anchor still needs; null when not weighed"""
    line: str | None
    """the sentence beside Pair, printed as sent; null: print none"""


class ReimbUnlink(Shape):
    expense_id: str
    reimburse_id: str


class ReimbUnlinked(Shape):
    ok: Literal[True]
    removed: bool
    expense_id: str
    reimburse_id: str


class ReimbFlag(Shape):
    """POST /api/reimburse/{txn_id}/flag; the whole body is optional."""
    expected: float | None = None
    partial: bool = False


class ReimbFlagged(Shape):
    ok: Literal[True]
    id: str


class ReimbUnflagged(Shape):
    ok: Literal[True]
    removed: bool
    id: str
