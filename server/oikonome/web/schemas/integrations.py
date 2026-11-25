"""The integrations read doors a script token with the `read` scope (or a
session) may call, and the owner's webhook management doors
(web/integrations.py)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from . import Shape
from .access import StepUp


class IntegrationEvent(Shape):
    """One webhook event a receiver can subscribe to."""
    name: str
    description: str


class IntegrationEvents(Shape):
    """GET /api/integrations/events."""
    events: list[IntegrationEvent]


class IntegrationBucketToday(Shape):
    left: float
    allowance: float


class IntegrationToday(Shape):
    left: float
    allowance: float
    spent: float
    by_bucket: dict[str, IntegrationBucketToday]
    """Per variable bucket, keyed by its label."""


class IntegrationMonth(Shape):
    budget: float
    spent: float
    expected: float
    variance: float
    fixed_spent: float
    fixed_budget: float
    days_in_month: int
    days_left: int
    headroom: float | None


class IntegrationCash(Shape):
    checking: float | None
    card_debt: float
    bills_due_14d: float


class IntegrationNetWorth(Shape):
    total: float
    with_property: float


class IntegrationAccount(Shape):
    """An account as the Accounts page lists it; `counted` is false for
    the second copy of a linked account, which the money math skips."""
    id: str
    name: str | None
    institution: str | None
    type: str
    subtype: str
    mask: str | None
    balance: float
    available: float | None
    status: str | None
    counted: bool


class IntegrationSummaryAccount(IntegrationAccount):
    aggregator: str | None


class IntegrationListedAccount(IntegrationAccount):
    transactions: int


class IntegrationBillDue(Shape):
    payee: str
    amount: float
    due: str


class IntegrationAlert(Shape):
    kind: str
    severity: str
    message: str


class IntegrationConnection(Shape):
    id: str
    name: str | None
    aggregator: str
    status: str | None
    last_sync_at: str | None
    last_sync_age_seconds: int | None


class IntegrationCounts(Shape):
    total: int
    uncategorized: int
    pending: int


class IntegrationSummary(Shape):
    """GET /api/integrations/summary: the books at a glance, the same
    figures /metrics renders."""
    generated_at: str
    date: str
    version: str
    verdict: str
    pace: str
    today: IntegrationToday
    month: IntegrationMonth
    cash: IntegrationCash | None
    """null when the household has no cash runway to report."""
    net_worth: IntegrationNetWorth
    accounts: list[IntegrationSummaryAccount]
    bills_due: list[IntegrationBillDue]
    """Unpaid bills due in the next seven days."""
    bills_due_ok: bool
    """false when the bill forecast failed: `bills_due` is then empty
    because it is unknown, not because nothing is due."""
    alerts: list[IntegrationAlert]
    connections: list[IntegrationConnection]
    transactions: IntegrationCounts


class IntegrationAccounts(Shape):
    """GET /api/integrations/accounts."""
    accounts: list[IntegrationListedAccount]


class IntegrationTxn(Shape):
    """A ledger row, cut to the keys a script reads (the full row is the
    Transactions page's `Txn`); a key the row does not carry is absent."""
    id: str
    date: str
    amount: float
    payee: str
    category: str
    account: str | None
    mask: str | None = None
    pending: int
    counts_as_spend: bool | None = None
    note: str | None = None
    entity: str | None = None
    bank_text: str | None = None
    payment_channel: str | None = None
    location_city: str | None = None
    location_region: str | None = None
    recurring_bill: str | None = None
    item_summary: str | None = None


class IntegrationTransactions(Shape):
    """GET /api/integrations/transactions: one page of a ledger search,
    newest first."""
    transactions: list[IntegrationTxn]
    total: int
    amount_sum: float
    spend_sum: float
    page: int
    limit: int


class IntegrationCategoryFigure(Shape):
    amount: float
    count: int


class IntegrationSpendingMonth(Shape):
    month: str
    total: float
    categories: dict[str, IntegrationCategoryFigure]
    """Keyed by the stored category ("?" for none)."""


class IntegrationCategoryTotal(Shape):
    category: str
    amount: float


class IntegrationSpending(Shape):
    """GET /api/integrations/spending."""
    from_: str = Field(alias="from")
    to: str
    months: list[IntegrationSpendingMonth]
    categories: list[IntegrationCategoryTotal]


class IntegrationBill(Shape):
    payee: str | None
    amount: float
    frequency: str | None
    interval: int
    by_month_day: list[int] | None
    """The days of a rule that falls on several days of every month
    ([15, -1]); null for any other rule."""
    next_due: str | None
    income: bool
    category: str | None
    type: str


class IntegrationBills(Shape):
    """GET /api/integrations/bills."""
    from_: str = Field(alias="from")
    to: str
    due: list[IntegrationBillDue]
    bills: list[IntegrationBill]


class IntegrationActiveAlert(IntegrationAlert):
    since: str


class IntegrationAlerts(Shape):
    """GET /api/integrations/alerts."""
    alerts: list[IntegrationActiveAlert]


class IntegrationInstitutionTotal(Shape):
    institution: str
    total: float


class IntegrationClassTotal(Shape):
    asset_class: str
    total: float


class IntegrationSnapshot(Shape):
    date: str
    total: float


class IntegrationNetWorthSeries(Shape):
    """GET /api/integrations/networth: today's figures and the recorded
    nightly series."""
    total: float
    with_property: float
    by_institution: list[IntegrationInstitutionTotal]
    by_asset_class: list[IntegrationClassTotal]
    series: list[IntegrationSnapshot]


# ---- webhooks ----------------------------------------------------------------

class Webhook(Shape):
    """A webhook as its owner manages it; the signing secret is never in
    it."""
    id: str
    url: str
    name: str
    events: list[str]
    """Event names, or "*" alone for every event."""
    enabled: bool
    created_at: str
    created_by: str
    """The address of the member who added it."""
    last_attempt_at: str | None
    last_status: int | None
    last_error: str | None
    failures: int
    disabled_reason: str | None


class WebhookWithSecret(Webhook):
    """The create and rotate replies: the signing secret, shown this
    once."""
    secret: str


class Webhooks(Shape):
    """GET /api/webhooks."""
    webhooks: list[Webhook]
    events: list[IntegrationEvent]


class WebhookCreate(StepUp):
    url: str
    events: list[str]
    name: str = ""


class WebhookUpdate(StepUp):
    """Every key is a change; a new URL and switching on step up."""
    url: str | None = None
    events: list[str] | None = None
    name: str = ""
    enabled: bool = False


class WebhookTested(Shape):
    """POST /api/webhooks/{id}/test: what the receiver said."""
    ok: bool
    status: int | None
    error: str | None
    delivery_id: str


class WebhookDelivery(Shape):
    id: str
    event: str
    state: Literal["pending", "delivered", "failed"]
    attempts: int
    next_attempt_at: str
    created_at: str
    delivered_at: str | None
    response_status: int | None
    last_error: str | None
    events: list[str]
    """Always empty: the row cleaner the webhook rows go through adds the
    key to every row it cleans."""


class WebhookDeliveries(Shape):
    """GET /api/webhooks/{id}/deliveries, newest first."""
    deliveries: list[WebhookDelivery]
