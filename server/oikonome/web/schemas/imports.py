"""Imports: the file import and its column mapping, YNAB, the folder (bulk)
import, a restore's progress, tax documents, batches and rollback,
onboarding and history coverage, Plaid enrichment, and the host-side
collectors' pushes (web/api_imports.py).

An import's reply has one shape per outcome (rows stored, an account to
choose, a mapping to finish, a refusal); the fields an outcome does not
use are absent, so they are optional here.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict

from . import Shape

HistoryMonths = Literal[1, 3, 6, 12] | None


class AccountChoice(Shape):
    """An account a refusal asks the person to choose, described by what
    tells two cards sharing their last four apart."""
    id: str
    name: str
    mask: str | None
    rows: int
    first_date: str | None
    last_date: str | None
    balance_cents: int | None


class RestoreSkip(Shape):
    """Rows of one table a restore could not read and left out."""
    table: str
    count: int
    sample: list[str]
    """the first few, by id (a uuid) or by line in the file"""
    sentence: str
    """the same, said in words; also the first of the result's warnings"""


class ImportResult(Shape):
    """What one import did, or why it did not."""
    error: str = ""
    imported: int = 0
    matched: int = 0
    """lines already stored, left as they were or updated in place"""
    rows: int = 0
    skipped_duplicates: int = 0
    corrected: int = 0
    skipped_older: int = 0
    """rows the history window left out"""
    choices: list[AccountChoice] = []
    choice_for: str = ""
    token: str = ""
    header: list[str] = []
    filename: str = ""
    expired: bool = False
    restore_started: bool = False
    job: str = ""
    source: str = ""
    confidence: float = 0.0
    warnings: list[str] | None = None
    """lines whose direction was read from their wording; a restore's
    result sends null"""
    period: list[str] = []
    note: str = ""
    split_accounts: dict[str, int] = {}
    removed_earlier: int = 0
    kept_annotated: int = 0
    foreign_left: int = 0
    credentials_dropped: list[str] = []
    skipped: list[RestoreSkip] = []
    """a restore's rows no reader could read, per table; an older server
    sends none"""


class YnabAccountGroup(Shape):
    name: str
    label: str
    rows: int
    first_date: str
    last_date: str
    stored_on_pick: int


class ColumnMappingNeeded(Shape):
    """The file's columns could not be read on their own: pick them."""
    token: str
    filename: str
    header: list[str]
    kind: Literal["columns"] = "columns"
    """absent: the server leaves the columns case unlabelled"""
    sample: list[list[str]] = []


class YnabAccountsNeeded(Shape):
    """A YNAB export: say where each of its accounts goes."""
    kind: Literal["ynab_accounts"]
    token: str
    filename: str
    pick: str
    accounts: list[YnabAccountGroup]


class ImportOutcome(Shape):
    """POST /api/import."""
    result: ImportResult | None
    mapping_needed: YnabAccountsNeeded | ColumnMappingNeeded | None


class ImportResultReply(Shape):
    """POST /api/import/mapped and /api/import/ynab-accounts."""
    result: ImportResult


class MappedImport(Shape):
    """The columns the person picked for a file the importer could not
    read on its own."""
    token: str
    date_col: str | None = None
    name_col: str | None = None
    amount_col: str | None = None
    debit_col: str | None = None
    credit_col: str | None = None
    merchant_col: str | None = None
    category_col: str | None = None


class YnabAccountsAnswer(Shape):
    """Where each YNAB account goes: an account id, "skip", {new: name},
    or {id, typed}."""
    token: str
    accounts: dict[str, Any]
    remove_earlier: Any = None


# ---- the folder import ------------------------------------------------------

class BulkFilePlan(Shape):
    index: int
    name: str
    kind: str
    size: int
    action: Literal["import", "skip"]
    account_id: str | None
    new_account_name: str
    amount_sign: str
    sign_certain: bool
    note: str
    oversized: bool = False
    restore_door: str = ""
    choices: list[AccountChoice] = []


class BulkFileUnread(Shape):
    """A file the folder import could not read at all."""
    kind: str


class BulkAnalysis(Shape):
    """POST /api/import/bulk/analyze: what each file in the folder is and
    what the run would do with it."""
    files: list[BulkFilePlan | BulkFileUnread]
    token: str = ""
    restore_door: str = ""


BulkFileState = Literal["pending", "running", "done", "failed", "skipped"]


class BulkJobFile(Shape):
    index: int
    name: str
    state: BulkFileState
    ok: bool = False
    imported: int = 0
    error: str = ""
    matched: int = 0
    account_id: str | None = None
    warnings: list[str] = []
    corrected: int = 0
    skipped_older: int = 0
    restore_started: bool = False


BulkJobState = Literal["queued", "running", "cancelling", "cancelled",
                       "stopped", "done", "error"]


class BulkJob(Shape):
    """A folder import job, as it runs and when it is done."""
    job_id: str
    state: BulkJobState
    stop_reason: str | None
    total: int
    done: int
    started_at: str | None
    updated_at: str | None
    files: list[BulkJobFile]
    restore_started: bool = False
    waiting: bool = False
    note: str = ""


class BulkRunFile(Shape):
    index: int
    name: str
    ok: bool
    pending: bool = False
    error: str = ""
    imported: int = 0
    matched: int = 0
    account_id: str | None = None
    warnings: list[str] = []
    corrected: int = 0
    skipped_older: int = 0
    restore_started: bool = False


class BulkRunDone(Shape):
    """POST /api/import/bulk/run without `background`: the finished run;
    with it, the job as it starts (202)."""
    job_id: str
    state: str
    results: list[BulkRunFile] = []
    total: int = 0
    done: int = 0
    files: list[BulkJobFile] = []
    stop_reason: str | None = None
    started_at: str | None = None
    updated_at: str | None = None
    restore_started: bool = False
    waiting: bool = False
    note: str = ""


class BulkRun(Shape):
    """POST /api/import/bulk/run: the analysis token and each file's
    answers (account, sign, action), handed whole to the runner."""
    token: str
    files: list[dict[str, Any]]
    background: bool = False


class RestoreState(Shape):
    table: str | None = None
    rows: int = 0
    tables_done: int = 0
    error: str = ""
    result: ImportResult | None = None


class RestoreProgress(Shape):
    """GET /api/restore/progress."""
    state: Literal["idle", "running", "done", "error"]
    progress: RestoreState
    started_at: str | None = None


# ---- tax documents ------------------------------------------------------------

class W2Match(Shape):
    id: int
    owner: Literal["primary", "spouse", "unassigned"]
    employer: str | None
    box1: float | None


class TaxdocRow(Shape):
    """One year a document reads as, with what is already stored."""
    year: int
    exists: bool = False
    total_income: float | None = None
    agi: float | None = None
    taxable_income: float | None = None
    tax_paid: float | None = None
    wages: float | None = None
    ss_earnings: float | None = None
    medicare_earnings: float | None = None
    primary_wages: float | None = None
    employer: str | None = None
    ein: str | None = None
    boxes: dict[str, float] = {}
    box1: float | None = None
    owner: Literal["primary", "spouse"] | None = None
    """whose a W-2 is: the form does not say, so the preview asks"""
    matches: list[W2Match] = []
    replaces: int | None = None


class W2Person(Shape):
    """One person a W-2 can be filed under, as the whose-picker offers it
    (taxdocs.w2_people)."""
    owner: Literal["primary", "spouse"]
    label: str


class TaxdocPlan(Shape):
    """POST /api/import/taxdoc/analyze: what the document reads as, before
    anything is stored."""
    token: str
    kind: str
    rows: list[TaxdocRow]
    w2_people: list[W2Person] = []
    """the people a W-2 row's whose-picker offers, in order
    (taxdocs.w2_people): the primary earner and the spouse"""


class TaxdocCommit(Shape):
    token: str
    rows: list[dict[str, Any]]


class TaxdocCommitted(Shape):
    kind: str
    updated: int
    created: int
    documents: int
    replaced: int = 0


class StoredW2(Shape):
    id: int
    year: int
    employer: str | None
    ein: str | None
    owner: Literal["primary", "spouse", "unassigned"]
    box1: float | None


class StoredW2s(Shape):
    w2s: list[StoredW2]


class W2Seen(Shape):
    """What the stored list showed for the W-2, so a reused id never acts
    on another form."""
    seen: dict[str, Any] | None = None


class W2Owner(W2Seen):
    owner: Literal["primary", "spouse"]


class StoredDocW2(Shape):
    """One W-2 a stored document holds, for the whose-picker (the W-2
    doors act on `id`)."""
    id: int
    year: int
    employer: str | None
    employer_shown: str | None
    """the payer's name as the row prints it: the name in parentheses
    after an IRS name control, else another form's with the same EIN last
    four, else the abbreviation itself (`abbreviated`)"""
    abbreviated: bool
    ein: str | None
    owner: Literal["primary", "spouse", "unassigned"]
    box1: float | None


class StoredDocSeen(Shape):
    kind: str
    years: list[int]
    stored_at: str


class StoredDoc(Shape):
    """One stored tax or income document (engine.taxdocs.list_stored): the
    words the row prints are the server's."""
    id: int
    kind: Literal["ssa", "transcript", "1040", "w2", "document",
                  "legacy_ssa", "legacy_return"]
    group: Literal["earnings", "returns", "w2", "other"]
    kind_label: str
    years: list[int]
    years_label: str
    whose_label: str | None
    filename: str | None
    stored_at: str
    stored_label: str
    removed: bool
    removed_label: str | None
    """"Removed 01/02/25 — can be put back until 02/01/25" for one removed
    in the last 30 days; null otherwise"""
    removable: bool
    restorable: bool
    employer_shown: str | None
    abbreviated: bool
    """the employer shown is the IRS's abbreviation: the client says so"""
    payer: str | None = None
    """the stored name behind `employer_shown`, which the payer rename
    door names the forms by; None for a removed document or one with no
    payer (nothing to rename)"""
    w2s: list[StoredDocW2]
    seen: StoredDocSeen
    """what a removal or a put-back sends back"""


class StoredDocGroup(Shape):
    key: Literal["earnings", "returns", "w2", "other"]
    label: str
    """"W-2s (2)", "Other forms (4)" """
    count: int
    collapsed: bool


class StoredDocs(Shape):
    """GET /api/import/taxdoc/stored: every stored document, newest first."""
    documents: list[StoredDoc]
    groups: list[StoredDocGroup]
    w2_people: list[W2Person] = []
    """the people a stored W-2's whose-picker offers, in order
    (taxdocs.w2_people)"""


class StoredDocEffect(Shape):
    """GET /api/import/taxdoc/stored/{id}/effect: what removing it does,
    in words, for the confirm."""
    effect_line: str
    step_up: bool
    """removing it asks the person to prove it is them first"""


class StoredDocSeenBody(Shape):
    seen: dict[str, Any] | None = None


class StoredDocPayerBody(Shape):
    seen: dict[str, Any] | None = None
    payer: str
    """the document's `payer` as listed"""
    name: str
    """the new name, 120 characters at most"""


class StoredDocRemoved(Shape):
    ok: Literal[True]
    years: list[int]


# ---- batches, onboarding, coverage --------------------------------------------

class ImportBatch(Shape):
    id: str
    created_at: str
    filename: str | None
    source: str
    account_id: str | None
    row_count: int | None
    annotations: int = 0
    """receipts, notes, pairings, tags and overrides added to its rows
    since; a rollback deletes them too"""


class ImportBatches(Shape):
    batches: list[ImportBatch]
    older_imports_line: str | None = None
    """accounts that came from files before undo history existed (a
    restore, an earlier import), in words, printed under the empty list;
    null when there are none"""


class Rollback(Shape):
    batch_id: str
    force_kept: bool = False


class RolledBack(Shape):
    ok: Literal[True]
    deleted: int
    warning: str | None


class Onboarding(Shape):
    """GET /api/onboarding: what the setup checklist reads."""
    accounts: int
    transactions: int
    bills: int
    connections: int
    pending_proposals: int
    budgets_set: bool
    budgets_saved_by_user: bool
    """the household saved a plan itself (budget.budgets_saved_by_user) —
    unlike budgets_set, never true for the first-run seed the server
    stores on its own; the setup wizard opens on the saved plan and lets
    Continue leave the step only when this holds"""
    primary_checking_set: bool
    primary_checking_id: str | None
    checking_candidates: list[str]
    wizard_done: bool
    wizard_steps: dict[str, Literal["done", "skipped"]]
    setup_complete: bool
    """the wizard was dismissed, or everything it sets up already exists
    (accounts, transactions, bills, budgets, a primary checking or none to
    pick) — the Today checklist's own test, made once here"""
    history_months: HistoryMonths
    setup_mode: Literal["auto", "manual"] | None
    bill_detection_auto: bool | None


class BackfillItem(Shape):
    id: str
    institution: str | None
    transactions: int
    earliest: str | None
    progress: float
    """0..1: how much of the two-year window the oldest row covers"""
    active: bool
    ready: bool
    stalled: bool


class BackfillStatus(Shape):
    items: list[BackfillItem]
    all_ready: bool
    syncing: bool


class CoverageAccount(Shape):
    id: str
    name: str
    type: str | None
    earliest: str | None
    latest: str | None
    transactions: int


class HistoryCoverage(Shape):
    accounts: list[CoverageAccount]


# ---- Plaid enrichment and the collectors ---------------------------------------

class EnrichStatus(Shape):
    candidates: int
    plaid_configured: bool
    cap: int
    used: int
    month: str
    remaining: int
    awaits_confirm: bool


class EnrichLimit(Shape):
    limit: int | str | None = None


class EnrichRun(Shape):
    sent: int
    enriched: int = 0
    cap_hit: bool = False
    error: str | None = None


class CoinbaseLink(Shape):
    key_name: str
    private_key: str
    label: str | None = None


class CoinbaseLinked(Shape):
    ok: Literal[True]
    item_id: str


class CollectorPush(Shape):
    """A collector's push, handed whole to its importer. `kind` labels the
    heartbeat (api | scrape); the rest is the collector's own payload."""
    model_config = ConfigDict(extra="allow")
    kind: str | None = None


class AmazonPush(CollectorPush):
    orders: list[dict[str, Any]] = []


class CostcoPush(CollectorPush):
    receipts: list[dict[str, Any]] = []


class CoinbaseAccountMove(Shape):
    before: int
    after: int
    balance: float


class CoinbasePushed(Shape):
    ok: Literal[True]
    pushed: int
    stale_removed: int = 0
    accounts: dict[str, CoinbaseAccountMove] = {}


class CountMove(Shape):
    before: int
    after: int


class AmazonMatch(Shape):
    amazon_plaid_txns: int = 0
    matched: int = 0
    unmatched: int = 0
    split: int = 0
    skipped: str = ""


class AmazonPushed(Shape):
    ok: Literal[True]
    pushed: int
    new: int = 0
    date_drift_refreshed: int = 0
    items_patched: int = 0
    orders: CountMove | None = None
    amazon_match: AmazonMatch | None = None


class CostcoMatch(Shape):
    costco_plaid_txns: int = 0
    matched: int = 0
    unmatched: int = 0
    split: int = 0
    skipped: str = ""


class CostcoPushed(Shape):
    ok: Literal[True]
    pushed: int
    new: int = 0
    refreshed: int = 0
    receipts: CountMove | None = None
    costco_match: CostcoMatch | None = None


class PlanActivityPush(Shape):
    plan: str
    csv: str | None = None
    balance: float | str | None = None
    institution: str | None = None
    kind: str | None = None
    mask: str | None = None
    provider: str | None = None


class PlanActivityPushed(Shape):
    ok: Literal[True]
    plan: str
    rows: int
    inserted: int
    span: str | None
    holdings_value: float
    balance: float
    balance_only: bool = False


class BulkJobCurrent(Shape):
    """GET /api/import/bulk/jobs/current: the household's latest folder
    import, or {state: "idle"} when there is none."""
    state: Literal["idle", "queued", "running", "cancelling", "cancelled",
                   "stopped", "done", "error"]
    job_id: str = ""
    stop_reason: str | None = None
    total: int = 0
    done: int = 0
    started_at: str | None = None
    updated_at: str | None = None
    files: list[BulkJobFile] = []
    restore_started: bool = False
    waiting: bool = False
    note: str = ""
