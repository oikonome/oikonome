"""Accounts and connections: the accounts page, provider keys (Plaid, MX),
connection export and import, linked accounts, holdings, account edits,
manual accounts, syncs, disconnect and reconnect (web/api_accounts.py)."""

from __future__ import annotations

from typing import Literal

from . import Shape
from .access import StepUp
from .imports import HistoryMonths


class AccountLinkInfo(Shape):
    """Where an account sits in a group of linked accounts (one real
    account seen through two connections)."""
    group_id: str
    home_rank: int
    primary: bool
    healthy: bool


class ScriptSource(Shape):
    """The collector script that feeds this account."""
    label: str | None
    via: str | None
    kind: str | None
    source: str
    last_push: str
    ok: bool
    token_revoked_at: str | None


class Account(Shape):
    id: str
    name: str
    mask: str | None
    bank_name: str | None
    kind: str
    """type/subtype"""
    balance_current: float | None
    balance_available: float | None
    item_id: str
    owner: str | None
    entity_id: str | None
    user_removed_at: str | None
    institution_name: str | None
    status: str | None
    aggregator: str | None
    inst_logo_url: str | None
    inst_color: str | None
    inst_url: str | None
    txns: int
    card_due: str | None
    card_statement: float | None
    card_due_text: str | None = None
    """"due 11/25", "paid 11/14" or "past due since 11/15" — the words
    beside a card's balance (data.card_due_words); null with nothing
    owed or no due date."""
    link: AccountLinkInfo | None
    script: ScriptSource | None
    goal_destination: bool
    """a savings goal can be pointed at it: open, the serving copy of a
    linked account, and a ledger that can show money arriving"""
    can_exclude: bool = True
    """"Exclude from budget" applies: a row on this account's type can be
    spending (budget.account_counts_as_spending — every type but a loan)."""
    can_pin: bool = True
    """"Primary checking" is offered: a bank account, the type the cash
    forecast picks among (forecast.pin_offered)."""


class SectionShown(Shape):
    """A section's figures in the whole dollars the page prints, each list
    adding up to the figure above it: the sections' counted parts to the
    headline, the institutions to the section total, an institution's
    accounts to its subtotal."""
    total: int
    counted_total: int
    counted_text: str | None = None
    """What of the section the headline counts when it is not `total`
    ("−$250 in the total", "not in the total"); null when they agree."""
    by_item: dict[str, int]
    by_account: dict[str, int]
    """By account id; an account the section does not count (a linked
    backup, one with no balance) is not here."""


class SectionTotal(Shape):
    total: float
    counted_total: float
    by_item: dict[str, float]
    shown: SectionShown


class AccountTotals(Shape):
    label: str
    financial_total: float
    account_count: int
    institution_count: int
    loans_owed: float
    loan_count: int
    business_counted: bool
    sections: dict[Literal["personal", "business"], dict[str, SectionTotal]]
    """by section, then by group label"""
    backups: dict[str, str] = {}
    """By login: "backup of Plaid — not counted" for a login whose
    accounts are all linked backups — it has no subtotal
    (reporting._backup_logins)."""
    backup_shown: dict[str, int] = {}
    """By account id: a linked backup whose balance agrees to the cent
    with its serving account's prints that account's shown dollars
    (reporting._backup_dollars); a backup not here prints its own
    balance."""


class AccountsData(Shape):
    """GET /api/accounts."""
    accounts: list[Account]
    primary_checking_id: str | None
    primary_pin_unused: str | None
    totals: AccountTotals


class KeysSaved(Shape):
    ok: Literal[True]
    env: str


class MxKeys(StepUp):
    client_id: str
    api_key: str
    env: str | None = None


class PlaidKeys(StepUp):
    client_id: str
    secret: str


class PlaidValidate(Shape):
    client_id: str
    secret: str
    env: str | None = None


class PlaidKeysCleared(Shape):
    ok: Literal[True]
    released: int
    stranded: int


class MxKeysCleared(Shape):
    ok: Literal[True]
    mx_user: str


class MxConnect(Shape):
    url: str


class MxSynced(Shape):
    ok: Literal[True]
    members: int
    accounts: int
    transactions: int
    removed: int
    skipped_import_duplicates: int


class ConnectionsExport(StepUp):
    passphrase: str


class ConnectionsImported(Shape):
    ok: Literal[True]
    config: list[str]
    items: int
    credentials_dropped: list[str]


class LinkGroupMember(Shape):
    account_id: str
    home_rank: int
    healthy: bool
    hidden: bool
    failover_alerted: str | None
    """when the household was told this group failed over"""
    primary: bool


class LinkGroup(Shape):
    group_id: str
    members: list[LinkGroupMember]


class LinkSide(Shape):
    id: str
    name: str
    mask: str
    institution_name: str | None


class LinkSuggestion(Shape):
    key: str
    a: LinkSide
    b: LinkSide


class AccountLinks(Shape):
    """GET /api/accounts/links."""
    groups: list[LinkGroup]
    suggestions: list[LinkSuggestion]
    card_dismissed: bool


class AccountLinkCreate(Shape):
    account_ids: list[str]
    auto_order: bool = False


class AccountLinkOrder(Shape):
    account_ids: list[str]


class AccountLinkCreated(Shape):
    group_id: str


class AccountLinkDismiss(Shape):
    """A suggestion's key, or the whole card."""
    key: str | None = None
    card: bool | None = None


class ProductIssue(Shape):
    cause: Literal["consent", "not_supported"]
    code: str = ""


class Connection(Shape):
    id: str
    aggregator: str
    institution_name: str | None
    archived_reason: Literal["auto-reap", "plaid-gone", "sub-ended"] | None
    status: str | None
    last_ok: str | None
    bank_updated_at: str | None
    bank_failed_at: str | None
    product_issues: dict[str, ProductIssue] | None
    institution_health: str | None
    accounts: int
    status_kind: Literal["ok", "retrying", "reauth", "gone", "attention"]
    history_months: HistoryMonths
    needs_attention: bool
    """The connection needs a person's hand: re-auth, removed, or an
    unclassified error — and, when removed, not yet replaced by a live link
    to the same institution (pages._mark_attention)."""
    replaced: bool
    """Removed by the bank feed, and a live link to the same institution
    exists: history, not work. Forget it through the disconnect door."""
    removed_text: str | None
    """"removed by the bank feed on 06/03/26 — replaced", on a replaced
    connection only."""


class SyncProgressCount(Shape):
    done: int
    total: int
    phase: str | None


class Connections(Shape):
    """GET /api/connections."""
    connections: list[Connection]
    last_sync: str | None
    syncing: bool
    sync_progress: SyncProgressCount | None
    attention: int
    """How many connections need a person's hand (`needs_attention`)."""


class Holding(Shape):
    acct: str
    symbol: str
    fund: str | None
    qty: float | None
    price: float | None
    value: float
    as_of: str | None = None
    """The day the feed last reported this position."""
    priced_text: str | None = None
    """"priced 11/16" — that day, as printed beside the position."""
    qty_text: str | None = None
    """The quantity as printed (three decimals); null when the feed sent
    none."""


class Holdings(Shape):
    holdings: list[Holding]
    """Positions at most two days old, of accounts whose positions do not
    add up to well above their balance (data.fresh_holdings)."""
    as_of_text: str | None = None
    """"holdings as of 11/16/26" — the oldest snapshot listed."""
    hidden_text: str | None = None
    """Each account whose holdings were left out, with its own reason."""


class AccountRename(Shape):
    account_id: str
    name: str | None = None
    """An empty string clears the household's name back to the bank's"""


class AccountRenamed(Shape):
    ok: Literal[True]
    name: str | None


class AccountClassify(Shape):
    account_id: str
    kind: str | None = None
    primary_checking: bool | None = None


class AccountExclude(Shape):
    account_id: str
    excluded: bool | None = None


class AccountExcluded(Shape):
    ok: Literal[True]
    account_id: str
    excluded: bool


class AccountAdd(Shape):
    """A manual account; balance as typed."""
    name: str
    kind: str
    balance: float | str | None = None


class AccountAdded(Shape):
    ok: Literal[True]
    account_id: str


class AccountBalance(Shape):
    account_id: str
    balance: float | str


class SimplefinClaim(Shape):
    token: str


class SimplefinConnected(Shape):
    ok: Literal[True]
    accounts: int
    transactions: int


class SyncRan(Shape):
    """POST /api/jobs/sync: each connection's result ("ok …" or
    "error …"), by item; `ok` is false when any failed."""
    ok: bool
    started: bool
    results: dict[str, str]
    reason: str = ""


class ConnectionDisconnected(Shape):
    ok: Literal[True]
    plaid_released: bool
    note: str


class ConnectionReconnect(Shape):
    ok: Literal[True]
    resync_from: str


class AccountHide(Shape):
    hidden: bool


class AccountHidden(Shape):
    ok: Literal[True]
    account_id: str
    hidden: bool
    note: str


class AccountRemove(Shape):
    mode: str
    """hide, archive or purge"""
    force_kept: bool = False


class Purged(Shape):
    accounts: int
    transactions: int
    holdings: int
    crypto_holdings: int
    kept_transactions: int
    note: str = ""
    kept_archived: int = 0


class AccountRemoved(Shape):
    ok: Literal[True]
    mode: str
    account_id: str
    item_id: str
    plaid_released: bool
    note: str
    purged: Purged | None
    affected_accounts: list[str]


class SyncStarted(Shape):
    ok: Literal[True]
    started: bool
    reason: str = ""


class SyncItem(Shape):
    id: str
    name: str
    status: Literal["pending", "running", "ok", "error"]
    transactions: int
    error: str = ""


class SyncCategorize(Shape):
    done: int
    total: int
    failed: int = 0


class SyncProgress(Shape):
    done: int = 0
    total: int = 0
    error: str = ""
    ok: bool = False
    items: list[SyncItem] = []
    results: dict[str, str] = {}
    categorize: SyncCategorize | None = None


class SyncStatus(Shape):
    """GET /api/jobs/sync/status."""
    state: Literal["none", "running", "done", "error"]
    progress: SyncProgress
    started_at: str | None
    updated_at: str | None
