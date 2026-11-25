"""The settings document: the read, the save, and the recipient, outbound
and relay doors around it (web/api_settings.py).

What a reader is sent depends on who they are: web/settings_exposure.py
decides key by key (a viewer gets the household's plan, a non-owner gets
`smtp_user_set` instead of the relay user, write-only credentials reach
nobody), and a hosted instance withholds the operator's relay and keys.
So every key that some reader can go without is optional here (the
household's setup is the owner's alone to read: where its mail goes, the
AI endpoint, the bank-connection keys); the keys every reader is always
sent are required.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict

from . import Shape
from .access import StepUp


class CustomBucket(Shape):
    name: str
    parent: Literal["food", "other"]
    monthly: float
    merchants: list[str] = []
    categories: list[str] = []
    """either may be absent from a bucket stored before it was used"""


class ManualAsset(Shape):
    name: str
    value: float
    kind: str | None = None
    # the day the value was last stated; a value edit restamps it
    as_of: str | None = None
    # the day the item entered the household (its "owned since"): the
    # month it enters the net-worth trend; a value edit never moves it
    acquired_on: str | None = None


class ScheduleEntry(Shape):
    on: bool
    hour: int
    sms: bool = False
    push: bool = False
    summary: bool = False


class CadenceReach(Shape):
    """Who one scheduled cadence reaches (notify/reach.cadence_reach)."""
    on: bool
    """any channel is scheduled"""
    email: int
    """addresses the worker would mail: mutes, per-kind choices,
    invitations and held addresses applied"""
    push: int
    """browsers and phones the household push reaches, when the cadence's
    push is on"""
    sms: bool
    """a text would go"""
    receiving: bool
    warn: bool
    """on, and nobody receiving it"""
    text: str
    """the Settings list line: "daily 9am", "daily 9am · +1 recipient",
    "daily 9am · nobody receiving it", or "daily email off"."""


class EmailReach(Shape):
    daily: CadenceReach
    monthly: CadenceReach
    yearly: CadenceReach


class EmailSchedule(Shape):
    daily: ScheduleEntry | None = None
    monthly: ScheduleEntry | None = None
    yearly: ScheduleEntry | None = None


class IncomeScenario(Shape):
    name: str
    take_home: float
    cadence: str
    monthly: float | None = None
    """the month this take-home makes, by the conversion the plan's income
    uses (budget.scenario_monthly); sent on every read, ignored on a save"""


class SavingsGoal(Shape):
    name: str
    target: float
    tokens: list[str] = []
    account_id: str | None = None
    monthly_plan: float
    start_balance: float = 0.0
    mode: Literal["monthly", "sweep"] | None = None
    target_date: str | None = None


class AggregatorKey(Shape):
    provider: Literal["plaid", "mx"]
    client_id: str
    env: str
    saved_at: str | None


class RemovedMember(Shape):
    email: str
    role: str
    joined: str


class OutboundPaused(Shape):
    """Outbound destinations held after an owner change or a recovery,
    until the new owner resumes them."""
    at: str
    reason: Literal["rotation", "recovery"]
    smtp: bool
    recipients: list[str]
    llm: bool
    aggregator: bool = False
    aggregator_keys: list[AggregatorKey] = []
    removed_members: list[RemovedMember] = []
    removed_members_count: int = 0
    version: str = ""


class LlmBundled(Shape):
    url: str
    model: str
    local: bool


class LlmBackend(Shape):
    id: str
    name: str
    url: str
    model: str
    vision_model: str
    extra_body: str
    extra_body_set: bool
    api_key_set: bool
    local: bool


class LlmEffective(Shape):
    source: str
    url: str
    model: str
    local: bool
    name: str = ""
    label: str | None = None
    """the model in words (api_settings.model_words), for the places that
    name it without editing it"""


class RecipientPrefs(Shape):
    daily: bool = True
    monthly: bool = True
    yearly: bool = True
    alerts: bool = True


class RecipientStatus(Shape):
    """One address on the household's mail, and what becomes of it."""
    email: str
    owner: bool
    member: bool
    muted: bool = False
    prefs: RecipientPrefs
    verified: bool | None
    delivery: Literal["bouncing", "complained"] | None
    delivery_reason: str | None
    mailed: bool = False
    not_mailed: str | None = None
    invite: Literal["invited", "accepted", "declined", "expired"] | None = None
    invited_at: str | None = None
    via: Literal["owner", "member", "invited"] | None = None


class SettingsView(Shape):
    """GET /api/settings (and the save's reply): the settings document as
    the caller may read it."""
    # always sent, to every reader
    food_monthly: float | None
    other_monthly: float | None
    custom_buckets: list[CustomBucket] | None
    budgeted_income_monthly: float | None
    dynamic_variable_budget: bool | None
    checking_account_id: str | None
    birthdate: str | None
    ss_estimates: dict[str, float] | None
    retirement_saving: bool | None
    income_biweekly_saving: float | None
    income_biweekly_not_saving: float | None
    excluded_accounts: list[str] | None
    occurrence_caps: dict[str, float] | None
    disabled_bills: list[str] | None
    dismissed_hints: dict[str, Any] | None
    manual_assets: list[ManualAsset] | None
    theme: Literal["auto", "light", "dark"] | None
    home_web: str | None
    home_mobile: str | None
    timezone: str | None
    today_view: Literal["summary", "detail"] | None
    feedback_enabled: bool | None
    merchant_logos: bool | None
    advisory_fees: dict[str, float] | None
    advisory_fee_exempt_masks: list[str] | None
    investment_basis_override: dict[str, float] | None
    history_months: Literal[1, 3, 6, 12] | None
    setup_mode: Literal["auto", "manual"] | None
    bill_detection_auto: bool | None
    list_versions: dict[str, str]
    """each whole-list setting's version as read, for the save's
    compare-and-swap"""
    biz_wizard_steps: dict[str, str | None] | None
    ret_wizard_steps: dict[str, str | None] | None
    ret_wizard_answers: dict[str, Any] | None
    income_scenarios: list[IncomeScenario]
    active_scenario: str | None
    savings_goals: list[SavingsGoal]
    llm_api_key_set: bool
    llm_extra_body_set: bool
    outbound_paused: OutboundPaused | None
    email_recipient_status: list[RecipientStatus]
    # the household's setup: the owner's alone
    email_recipients: list[str] | None = None
    email_muted: list[str] | None = None
    email_send_hour_utc: int | None = None
    """the daily mail's UTC hour from before email_schedule; it counts,
    and a save of it is taken, only while no email_schedule is saved"""
    llm_url: str | None = None
    llm_model: str | None = None
    llm_vision_model: str | None = None
    taxdocs_allow_remote_llm: str | None = None
    plaid_enrich_cap: int | None = None
    plaid_recurring: bool | None = None
    email_schedule: EmailSchedule | None = None
    email_reach: EmailReach | None = None
    """who each cadence actually reaches — the owner's"""
    llm_extra_body: str | None = None
    llm_backends: list[LlmBackend] = []
    llm_roles: dict[str, str] = {}
    llm_bundled: LlmBundled | None = None
    llm_effective: dict[str, LlmEffective | None] = {}
    categorizer_active: bool = False
    # the owner's alone, and withheld on a hosted instance (the operator's
    # relay and keys)
    plaid_client_id: str | None = None
    plaid_env: str | None = None
    plaid_secret_set: bool = False
    mx_client_id: str | None = None
    mx_env: str | None = None
    mx_api_key_set: bool = False
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_from: str | None = None
    smtp_starttls: bool | None = None
    smtp_password_set: bool = False
    # the relay user: the owner reads it, everyone else whether one is set
    smtp_user: str | None = None
    smtp_user_set: bool = False
    # a reader's own place on the list, and the demo's lock
    email_list_configured: bool = False
    email_on_list: bool = False
    demo_read_only: bool = False


class SettingsSave(StepUp):
    """POST /api/settings: the settings document (any key the read
    returns), plus the write-only credentials (smtp_password, llm_api_key,
    a backend's api_key) and the one-address recipient deltas. A key left
    out keeps its stored value; a non-owner's owner-only keys are
    ignored. Read whole by the save, so it is not itemised here."""
    model_config = ConfigDict(extra="allow")


class RecipientResend(Shape):
    email: str


class RecipientResent(Shape):
    ok: Literal[True]
    sent: bool
    email_recipient_status: list[RecipientStatus]
    reason: str = ""
    """why nothing was sent (cooldown, hourly cap, held)"""


class OutboundResume(StepUp):
    version: str | None = None
    """the paused state's version as read, so a stale tab resumes
    nothing"""


class OutboundResumed(Shape):
    ok: Literal[True]
    resumed: bool


class SmtpSkipped(Shape):
    email: str
    reason: str


class SmtpFailed(Shape):
    email: str
    error: str


class SmtpTested(Shape):
    ok: bool
    sent_to: list[str]
    skipped: list[SmtpSkipped]
    failed: list[SmtpFailed]
    via: str
