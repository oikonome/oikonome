"""The signed-in person and their household: who they are (GET /api/me),
who else is here and what they receive (invites, the roster), their own
mail and push switches, leaving or erasing the account, and moving the
sign-in address (web/app.py).

They describe what the doors already send; nothing here changes who may
read or change what.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from . import Shape
from .access import StepUp
from .settings import RecipientPrefs, RemovedMember
from .today import Reason

Role = Literal["owner", "member", "viewer"]
AssignableRole = Literal["member", "viewer"]


# ---- GET /api/me -----------------------------------------------------------

class MeDayCounts(Shape):
    """The day counts the Accounts copy prints. Empty when they could not
    be read."""
    plaid_reap_days: int | None = None
    """How long a failing connection lasts before it is removed; null
    while the instance does not remove them."""
    released_hold_days: int = 0
    """How long a released bank keeps its place under the allowance."""


class MeFeatures(Shape):
    """What the installed plan gate allows, and the bank allowance. Every
    key is absent when no gate answers (every feature is then available)
    or the lookup failed."""
    investments: bool = True
    liabilities: bool = True
    sms: bool = True
    priority_sync: bool = True
    business: bool = True
    enrich: bool = True
    plaid_recurring: bool = True
    """False when the plan or the operator leaves out the bank's recurring
    cross-check."""
    institution_cap: int = 0
    """How many banks the household may connect; absent when uncapped."""
    institutions_used: int = 0
    """How many of the cap are spent, counted as the add door counts."""
    bank_awaits_confirm: bool = False
    """True when no bank can be connected until an owner confirms their
    address."""
    bank_link_line: str = ""
    """The line under the add-a-bank buttons, printed as it is: how many
    more banks the household may connect, and whether several can be
    connected in one sign-in. Absent when there is nothing to say."""


class MeCadences(Shape):
    """Which cadences the household sends at all."""
    daily: bool
    monthly: bool = False
    yearly: bool = False


class MeDailyEmail(Shape):
    """The household's daily-email schedule, and this login's own place
    on it."""
    on: bool
    hour: int
    summary: bool
    muted: bool
    """Whether THIS login's address is muted."""
    prefs: RecipientPrefs
    """Which kinds of the household's mail reach this inbox."""
    cadences: MeCadences


class MeEmailDelivery(Shape):
    """The mail provider could not deliver to this address: scheduled
    sends are held."""
    state: Literal["bouncing", "complained"]
    bounce_type: str | None
    reason: str | None
    """The provider's own words."""
    suppressed: bool
    since: str | None
    did_you_mean: str | None
    """The address this looks like a typo of, as a suggestion."""


class MeBroadcast(Shape):
    """The operator's banner; a deadline is a reboot countdown."""
    message: str
    severity: Literal["info", "warn"]
    """The admin console sets one of the two."""
    deadline: str | None


class Me(Shape):
    """GET /api/me: the signed-in login, read on every page load."""
    email: str
    tenant_id: str
    plan: str | None
    """The installed gate's plan key, or null with no gate."""
    features: MeFeatures
    day_counts: MeDayCounts
    has_business: bool
    merchant_logos: bool
    donate_url: str | None
    totp_enabled: bool
    verified: bool
    created_at: str | None
    verify_deadline: str | None
    """When the nightly sweep freezes a never-confirmed household."""
    pending_email: str | None
    """A sign-in address waiting for its mailbox's confirm button."""
    pending_email_expires_at: str | None
    email_delivery: MeEmailDelivery | None
    """Null while nothing is known to be wrong with this address."""
    daily_email: MeDailyEmail
    push_muted: bool
    needs_2fa: bool
    recovery_codes_left: int
    factor_reset_pending: str | None
    """When a factor-clearing recovery asked for on this account lands."""
    role: Role
    broadcast: MeBroadcast | None
    version: str
    hosted: bool
    timezone: str
    timezone_source: Literal["household", "instance"]
    instance_timezone: str
    bank_link: bool
    demo: bool
    shared_login: bool
    """The shared demonstration login: posture changes and durable grants
    are refused to it."""


# ---- invites ---------------------------------------------------------------

class InviteCreate(StepUp):
    """POST /api/invites: who it is for (an address on a hosted
    instance) and the role it grants (viewer when left out)."""
    label: str | None = None
    role: str | None = None


class InviteCreated(Shape):
    url: str | None
    """The claim link; null on a hosted instance, where only the mail to
    the invitee carries it."""
    role: AssignableRole
    label: str
    expires_at: str
    emailed: bool


class InvitePending(Shape):
    token_hash: str
    role: AssignableRole
    label: str
    created_at: str
    expires_at: str


class Invites(Shape):
    """GET /api/invites (owner): the unclaimed invites. `hidden` for the
    shared demonstration login, which is shown none."""
    invites: list[InvitePending]
    hidden: bool = False


# ---- the roster ------------------------------------------------------------

class MemberNotify(Shape):
    """What one person receives: their mail switches and their push
    switch with the devices it governs."""
    on_mail: bool
    """Exactly whom the scheduled sender would mail."""
    mail_listed: bool
    """On the household's list, whether or not mailed yet."""
    email_muted: bool
    email_prefs: RecipientPrefs
    push_muted: bool
    push_browsers: int
    push_phones: int


class HouseholdUser(Shape):
    id: str
    email: str
    role: Role
    created_at: str
    me: bool
    notify: MemberNotify | None = None
    """The owner sees everyone's; anyone else only their own row's (null
    on the others). Null too when it could not be read, and absent for the
    shared demonstration login."""


class HouseholdUsers(Shape):
    """GET /api/users: the household's people; the shared demonstration
    login sees only itself (`hidden`)."""
    users: list[HouseholdUser]
    assignable_roles: list[AssignableRole]
    hidden: bool = False


class UserRoleSet(StepUp):
    role: str | None = None


class UserRoleChanged(Shape):
    ok: Literal[True]
    role: AssignableRole


class UserRemove(StepUp):
    """DELETE /api/users/{id}: the step-up proof, when an older client
    sends one; the body may be left out."""


class EmailPrefSet(Shape):
    kind: str | None = None
    on: bool | None = None


class UserNotifySet(Shape):
    """POST /api/users/{id}/notify (owner): one or more of the member's
    switches."""
    push_muted: bool | None = None
    email_muted: bool | None = None
    email_pref: EmailPrefSet | None = None


class UserNotifyChanged(Shape):
    ok: Literal[True]
    notify: MemberNotify


# ---- this login's own switches -----------------------------------------------

class MeEmailSet(Shape):
    """POST /api/me/email: either `muted`, or one kind of mail on or off
    (`kind` + `on`)."""
    muted: bool | None = None
    kind: str | None = None
    on: bool | None = None


class MeEmailChanged(Shape):
    """`prefs` answers a kind's switch, `muted` the mute."""
    ok: Literal[True]
    prefs: RecipientPrefs = Field(default_factory=RecipientPrefs)
    muted: bool = False


class MePushSet(Shape):
    muted: bool | None = None


class MePushChanged(Shape):
    ok: Literal[True]
    muted: bool


# ---- moving the sign-in address --------------------------------------------

class EmailChanged(Shape):
    """POST /api/email/change and /resend. A change to the address the
    login already has answers `ok` and `email` only. Otherwise the
    rotation's report comes with it: on a hosted instance the change waits
    for the new mailbox (`pending_email`, `email` still the old address),
    elsewhere it is made at once."""
    ok: Literal[True]
    email: str
    pending_email: str | None = None
    verify_sent: bool = False
    """Always false: no verification went to the sign-in address."""
    mail_list_entry_dropped: bool = False
    resent: bool = False
    """Set on the resend door's answer."""
    hint: str | None = None
    """A "did you mean" for an address that looks like a typo."""
    other_sessions_signed_out: int = 0
    devices_revoked: int = 0
    tokens_revoked: int = 0
    webhooks_paused: int = 0
    invites_removed: int = 0
    members_removed: list[RemovedMember] = []
    support_access_revoked: int = 0
    sms_number_needs_verify: bool = False
    passkeys_removed: int = 0
    passkeys_kept: int = 0
    recovery_codes: list[str] | None = None


class EmailChangeCancelled(Shape):
    ok: Literal[True]
    cancelled: bool


# ---- GET /api/today ----------------------------------------------------------

class VerdictBucket(Shape):
    actual: float
    expected: float
    month_budget: float


class MonthVerdict(Shape):
    """GET /api/today: the month's verdict straight from the engine, for
    scripts."""
    verdict: Literal["ON BUDGET", "OVER BUDGET", "UNDER BUDGET"]
    variance: float
    variable_actual: float
    variable_expected: float
    reasons: list[Reason]
    buckets: dict[str, VerdictBucket]
    days_in_month: int
