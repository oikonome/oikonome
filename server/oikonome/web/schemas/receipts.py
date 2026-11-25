"""Receipts: upload, waiting list, matching, items (web/api_receipts.py)."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict

from . import Shape

ReceiptStatus = Literal["uploaded", "parsing", "parsed", "failed"]


class ReceiptParsed(Shape):
    """What the parse (or a person) read off the image. A model writes it,
    so keys beyond these may ride along; the clients read only these."""
    model_config = ConfigDict(extra="allow")
    merchant: str | None = None
    date: str | None = None
    total: float | None = None
    tax: float | None = None
    tip: float | None = None
    source: str | None = None
    """"manual" when a person typed the total and date in"""
    # check-kind fields (front-of-check parse)
    check_number: str | None = None
    payee: str | None = None
    amount: float | None = None
    memo: str | None = None
    bank: str | None = None


class ReceiptItem(Shape):
    line: int
    description: str
    qty: float | None
    amount: float
    tag: str | None


class ReceiptTwin(Shape):
    """Another receipt of this one's slip, or one read alike that is not
    (specs/receipts.md, "A receipt is one slip"): where it is, and whether
    it is the copy counted."""
    receipt_id: str
    txn_id: str | None
    """null while it waits for its charge"""
    date: str | None
    """the charge's date, or the day the waiting receipt printed"""
    payee: str | None
    """the charge's payee, or the store the waiting receipt printed"""
    amount: float | None
    """the charge's amount, or the total the waiting receipt printed"""
    waiting: bool
    off_ledger: bool
    """its charge has left the ledger"""
    counted: bool
    """its lines are the ones counted for its slip"""


class Receipt(Shape):
    """A receipt or check attached to a transaction."""
    id: str
    mime: str
    kind: Literal["receipt", "check"]
    status: ReceiptStatus
    parsed: ReceiptParsed | None
    error: str | None
    created_at: str
    matched_at: str | None
    match_method: Literal["auto", "manual"] | None
    parse_started_at: str | None
    parse_stale: bool
    has_optimized: bool
    items: list[ReceiptItem]
    """the lines it counts: payment lines left out, each amount the line's
    total (specs/receipts.md)"""
    duplicate_of: str | None
    """the receipt of the same slip whose lines are counted, when it is not
    this one: this copy's lines are that one's and are not counted again
    (items is empty, listed_items holds them)"""
    counts: bool = True
    """its lines are the ones counted for its slip (false: another copy is
    counted, or its charge is off the ledger)"""
    listed_items: list[ReceiptItem] = []
    """this copy's lines when another copy of the slip is counted: shown,
    never added up"""
    slip_twins: list[ReceiptTwin] = []
    """the other receipts of its slip"""
    suspected: list[ReceiptTwin] = []
    """receipts read exactly alike that are not its slip: both count until
    the person says Same receipt or Two purchases"""
    lines_gap: float | None
    """the charge less the counted lines, tax and tip, when they differ by
    more than a few cents; null when they add up (or nothing to add)"""
    amount_mismatch: bool = False
    """a parsed check whose amount disagrees with the transaction (display
    only; absent unless it is a parsed check)"""


class ReceiptAttached(Shape):
    """POST /api/transactions/{txn_id}/receipt."""
    id: str
    txn_id: str
    receipts: list[Receipt]


class TxnReceiptsTxn(Shape):
    amount: float
    date: str


class TxnReceipts(Shape):
    """GET /api/transactions/{txn_id}/receipts."""
    receipts: list[Receipt]
    txn: TxnReceiptsTxn | None


class WaitingCandidate(Shape):
    txn_id: str
    date: str
    amount: float
    payee: str | None
    account: str | None
    pending: bool
    exact: bool
    merchant_match: Literal["same", "different", "unknown"]


class WaitingReceipt(Shape):
    """A receipt snapped before its transaction reached the ledger."""
    id: str
    mime: str
    kind: Literal["receipt", "check"]
    status: ReceiptStatus
    parsed: ReceiptParsed | None
    error: str | None
    created_at: str
    parse_started_at: str | None
    parse_stale: bool
    has_optimized: bool
    total: float | None
    date: str | None
    merchant: str | None
    tie: bool
    """two charges fit equally well: the choice is left to a person"""
    released: bool
    needs_details: bool
    manual: bool
    stale: bool
    candidates: list[WaitingCandidate]
    counts: bool = True
    """its lines are the ones counted for its slip (false: another copy
    is counted)"""
    slip_twins: list[ReceiptTwin] = []
    """the other receipts of its slip"""
    suspected: list[ReceiptTwin] = []
    """receipts read exactly alike that are not its slip"""


class RecentMatch(Shape):
    id: str
    mime: str
    parsed: ReceiptParsed | None
    has_optimized: bool
    matched_at: str | None
    match_method: Literal["auto", "manual"] | None
    txn_id: str
    txn_date: str | None
    txn_amount: float
    payee: str | None


class WaitingReceipts(Shape):
    """GET /api/receipts/waiting: `count` is every receipt waiting,
    `waiting` one page of them from `offset`."""
    count: int
    limit: int
    offset: int
    ai_available: bool
    stale_days: int
    waiting: list[WaitingReceipt]
    recent: list[RecentMatch]


class ReceiptSnapped(WaitingReceipts):
    """POST /api/receipts: the new receipt's id and the waiting list."""
    id: str


class ReceiptMatched(WaitingReceipts):
    """POST /api/receipts/{rid}/match and /unmatch: the transaction it
    joined or left, and the waiting list."""
    txn_id: str | None


class ReceiptDetailsSaved(WaitingReceipts):
    """POST /api/receipts/{rid}/details."""
    id: str
    txn_id: str | None


class ReceiptMatch(Shape):
    txn_id: str


class ReceiptDetails(Shape):
    """The total and date a person typed in; a null merchant clears it."""
    total: float | str
    date: str
    merchant: str | None = None


class ReceiptTag(Shape):
    """An empty string or null clears the tag."""
    tag: str | None = None


class ReceiptParsing(Shape):
    status: Literal["parsing"]


class ReceiptItemRow(Shape):
    receipt_id: str
    line: int
    description: str
    qty: float | None
    amount: float
    """what the line cost in all (a unit price read off the receipt is
    multiplied out where the receipt's total says so)"""
    tag: str | None
    txn_id: str | None
    date: str | None
    payee: str | None
    counts: bool = True
    """added up in the groups: false for a copy of a slip counted on
    another receipt, and for a line whose charge left the ledger"""
    why: Literal["same_slip", "off_ledger"] | None = None
    """why a line does not count"""
    suspected: bool = False
    """counted, and another counted receipt reads the same store, day and
    total: the totals may hold this purchase twice until the person
    answers Same receipt or Two purchases"""
    note: str | None = None
    """the words printed beside the line (why it is not counted, or that
    it may be counted twice); decided by the server"""


class ReceiptItemGroup(Shape):
    key: str | None
    """null: receipts whose store is unknown"""
    n: int
    """lines; kept for older clients"""
    bought: int = 0
    """how many were bought: a line's whole quantity, one for a line with
    none or a weighed one"""
    receipts: int = 0
    """the distinct slips the group's lines are on"""
    total: float
    last_date: str | None
    note: str | None = None
    """the words printed under the total when it adds up a receipt that
    may be counted twice (a `suspected` line); decided by the server"""


class ReceiptItems(Shape):
    """GET /api/receipts/items."""
    rows: list[ReceiptItemRow]
    groups: list[ReceiptItemGroup]
    truncated: bool


class ReceiptReport(Shape):
    """GET /api/receipts/report (format=json; csv is a download)."""
    tag: str
    year: int
    month: int
    total: float
    count: int
    rows: list[ReceiptItemRow]
    note: str | None = None
    """the words printed under the total when it adds up a receipt that
    may be counted twice; decided by the server"""


class ReceiptOther(Shape):
    """POST /api/receipts/{rid}/same-slip and /two-purchases: the other
    receipt the person is answering about."""
    other: str


class ReceiptSlipAnswered(Shape):
    """A person's answer about two receipts, stored and never asked again."""
    id: str
    other: str
    same: bool
    """true: one slip, counted once; false: two purchases, each counted"""
    released: list[str] = []
    """receipts "Two purchases" sent back to waiting off a row that left
    the ledger, to be placed and counted"""
