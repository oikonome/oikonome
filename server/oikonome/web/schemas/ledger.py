"""The ledger: one transaction row as every ledger surface receives it, the
transactions page and its edits, owners, categories, merchants and rules
(web/api_ledger.py).

`Txn` is THE ledger row: /api/transactions rows, Today's recent pane and a
merchant's bill history all send exactly these keys (the conformance hook
holds every reply to it, and test_ledger_row_contract holds the key set).
Both clients' Txn type is generated from it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from . import Shape


class SplitPart(Shape):
    """One part of a split: a stored category key and its cent-exact
    amount."""
    category: str
    amount: float


class Txn(Shape):
    """One transaction as every ledger surface receives it."""
    id: str
    """source-stable transaction id"""
    date: str
    """posted date, ISO"""
    amount: float
    """positive = money out"""
    payee: str
    """the merchant as the ledger shows it (merchant row name → alias →
    raw)"""
    merchant_id: str | None = None
    """the merchant ROW (engine/merchant_identity)"""
    merchant_logo: str | None = None
    """Plaid's logo for the merchant, when it has one"""
    category: str
    """the effective category in DISPLAY form (spaces, never underscores)"""
    account: str | None
    """institution + account name + mask"""
    mask: str | None = None
    pending: int
    """1 = still pending at the bank"""
    counts_as_spend: bool | None = None
    """the verdict's own rule: does this row count as household spend"""
    spend_amount: float | None = None
    """what this row adds to spending by the verdict's own math (net of a
    partial reimbursement; under the search's category filter, a split
    row's matching parts' share — the same figure the search's spend sum
    totals); 0 when it does not count. Day subtotals sum this, not
    amount"""
    reimb: bool
    """paired with a reimbursing deposit"""
    reimb_flag: bool
    """flagged as reimbursement expected"""
    biz_flag: bool
    """Schedule C tag on a household charge"""
    entity: str | None = None
    """the business this row BELONGS to (a business-entity account)"""
    has_receipt: bool
    note: str | None = None
    """free-text per-transaction note"""
    owner: str | None = None
    """household owner (per-txn override else the account's)"""
    recurring_bill: str | None = None
    """the active bill whose matcher counts this row (⟳)"""
    item_summary: str | None = None
    """what a matched store order or receipt contained, by what was bought;
    null for every other merchant"""
    check_number: str | None = None
    payment_channel: str | None = None
    """in store | online | other"""
    payment_processor: str | None = None
    """the payment app in front of the merchant (PayPal, Square…)"""
    location_city: str | None = None
    location_region: str | None = None
    location_address: str | None = None
    location_store: str | None = None
    location_lat: float | None = None
    location_lon: float | None = None
    mcc: str | None = None
    """merchant category code"""
    authorized_date: str | None = None
    bank_text: str | None = None
    """the bank's own descriptor"""
    category_source: str | None = None
    """who decided category_primary: plaid | import | flow | fuel | mcc |
    rule_user | rule_llm | rule_seed | rule_model | rule"""
    category_why: str | None = None
    """the provenance in words, composed by the server"""
    category_key: str | None = None
    """category_primary as stored (a key)"""
    category_override: str | None = None
    """the override as stored, when any"""
    category_detailed: str | None = None
    category_plaid: str | None = None
    """Plaid's own primary"""
    category_plaid_detailed: str | None = None
    category_plaid_confidence: str | None = None
    """VERY_HIGH | HIGH | MEDIUM | LOW"""
    override_bill_id: str | None = None
    """the bill that stamped the override, when a bill did"""
    override_bill: str | None = None
    """that bill's payee"""
    override_manual: bool | None = None
    """true when a person set the override"""
    override_is_fee: bool | None = None
    """true when the row is a companion charge — the fee that rides with
    override_bill's payment"""
    split: list[SplitPart] | None = None
    """a split of this charge across categories, in order — stored category
    keys and cent-exact amounts summing to `amount`; null when the row
    is not split. `category` stays the whole row's own; the rollups
    count the parts. `split_source` says whether a person or a store
    receipt wrote them"""
    split_source: str | None = None
    """who wrote `split`: 'user' for a hand split, 'amazon' / 'costco' for
    the parts a matched store receipt's items name (recomputed on every
    match; a person's edit replaces them, a person's remove pins the row
    whole); null when the row is not split"""
    splittable: bool | None = None
    """true when the split door will take this row — the spend test the
    rollups use (money out, not a transfer, not a card payment, not on a
    loan account); clients offer Split and Edit split only when true"""
    needs_payee: bool | None = None
    """true for a written check filed under Checks (it counts as spending)
    that nobody has said the payee of yet — no note, no check image read,
    no person's category. Clients ask "who was this check to?" once and
    send the answer to POST /api/transactions/{id}/check-payee"""
    check_payee: str | None = None
    """whom a written check paid, as its read check image says; null for
    every other row. Clients show it beside the bank's "CHECK PAID" line,
    and a search finds the check by it"""


class HistoryTxn(Txn):
    """A bill history's row: the ledger row, the matcher's verdict, and the
    id again under the name older clients read."""
    txn_id: str
    hit: bool


class AmazonItems(Shape):
    count: int
    sum: float
    unpriced: int


class AccountHit(Shape):
    """An account whose name or bank matches the search."""
    id: str
    label: str
    bank_name: str
    via_bank: bool
    rows_listed: bool = True
    """true when this account's rows are in the result (the term matched
    no row's own text, so it named the account); false when the rows the
    term matched by text are the result and the account's are offered as
    a link of their own"""


class LedgerFlow(Shape):
    """A search's money out / in / net over EVERY row it matched, by Cash
    Flow's definitions (engine.reporting.flows): card payments, transfers,
    loan accounts and business money are in neither side, partial
    reimbursements are netted. A date range alone reads exactly as Cash
    Flow's figures for the same days."""
    money_out: float
    """spending among the matched rows"""
    money_in: float
    """income among the matched rows"""
    net: float
    """money_in less money_out"""


class RangeDates(Shape):
    """The days one range button covers, inclusive ISO dates; "" both for
    no date bound."""
    start: str
    end: str


class TxnPage(Shape):
    """GET /api/transactions: a month (mode "month") or a search (mode
    "search"); each mode's figures are absent from the other."""
    mode: Literal["month", "search"]
    rows: list[Txn]
    per_day: float | None
    per_day_days: int | None
    biz_count: int
    day_spend: dict[str, float] = {}
    """Each listed day's spending (ISO date → amount) over ALL of that
    day's rows in the result, pages and "show more" chunks not yet shown
    included, by the rule each row's spend_amount follows: the figure a
    day divider prints."""
    # search
    total: int = 0
    amount_sum: float = 0.0
    amazon_items: AmazonItems | None = None
    page: int = 0
    spend_count: int = 0
    spend_sum: float = 0.0
    spend_avg: float | None = None
    account_hits: list[AccountHit] = []
    flow: LedgerFlow | None = None
    """the matched rows' money out / in / net — a plain search's headline
    (the month tiles' definitions); null for a budget bucket's or a counted
    category's rows (their headline is the label's own number) and under
    the business-only scope"""
    ranges: dict[str, RangeDates] = {}
    """what each range button (cur, 1m, 3m, 6m, 1y, 3y, 5y, all) sets as
    the date filter, by Cash Flow's own windows (engine.reporting.
    ledger_ranges): complete months for every key but "cur" (this month
    to today) and "all" (no bound). Present in every mode"""
    # month
    year: int = 0
    month: int = 0
    out_sum: float = 0.0
    listed_spend: float = 0.0
    """What the listed rows spent (their spend_amount summed): the line a
    month narrowed by a lens prints. When every listed row is a
    business's, that business spending instead (see listed_scope)."""
    listed_scope: Literal["household", "business"] = "household"
    """Whose spending listed_spend counts: "business" when every listed row
    is a business's by the household test the aggregates use, else
    "household"."""
    in_sum: float | None = None
    in_expected: float | None = None
    net_sum: float | None = None
    # a budget bucket's rows
    bucket: str = ""
    bucket_label: str = ""
    bucket_amount: float = 0.0
    bucket_scope: Literal["month", "year"] | None = None
    counted: bool = False
    drill_label: str | None = None
    """a Spending figure's rows (`spend=`): what the chip over them says
    they are, with the days they were read over — "Groceries · Dec
    2025–Nov 2026"; amount_sum is that figure. Absent otherwise"""


class CategoryUndoRef(Shape):
    """The reference an undo hands back."""
    ref: str
    category: str | None


class TxnCategory(Shape):
    """POST /api/transactions/{id}/category: "" clears the override; scope
    "all" also writes the merchant's rule."""
    category: str
    scope: str = "one"
    """one or all"""


class TxnCategorySet(Shape):
    ok: Literal[True]
    category: str | None
    scope: str
    rule_written: bool
    undo: CategoryUndoRef | None


class SplitInput(Shape):
    parts: list[SplitPart]


class SplitSaved(Shape):
    ok: Literal[True]
    split: list[SplitPart] | None


class Owners(Shape):
    owners: list[str]


class OwnerInput(Shape):
    """An empty string clears the owner."""
    owner: str | None = None


class AccountOwnerSet(Shape):
    ok: Literal[True]
    owner: str | None


class TxnOwnerSet(Shape):
    ok: Literal[True]
    owner: str | None
    id: str


class TxnBulk(Shape):
    ids: list[str]
    action: str
    category: str | None = None


class TxnBulkDone(Shape):
    ok: Literal[True]
    applied: int
    missing: int
    flow: int


class RetirePending(Shape):
    id: str


class PendingRetired(Shape):
    ok: Literal[True]
    id: str


class TxnNote(Shape):
    """An empty string or null clears the note."""
    note: str | None = None


class TxnNoteSaved(Shape):
    ok: Literal[True]
    note: str


class CheckPayee(Shape):
    """Who a written check paid, as the person says it."""
    payee: str


class CheckPayeeSaved(Shape):
    ok: Literal[True]
    note: str
    """the row's note after the answer: "paid to …" as its first line,
    ahead of any note it already had (kept as it was when it already
    named a payee)"""
    category: str | None
    """the category the payee filed the check under (its merchant rule, or
    one classification when a model is configured); null when nothing said
    more and the check stays under Checks"""


class Categories(Shape):
    categories: list[str]
    plaid_spend: list[str]
    plaid_flow: list[str]


class Merchants(Shape):
    merchants: list[str]


class CatalogMerchant(Shape):
    display: str
    rows: int
    total: float
    business_rows: int
    first: str
    last: str
    variants: list[str]
    variant_count: int
    logo: str | None
    website: str | None
    kind: str | None
    plaid: bool
    parent: str | None
    total_label: str | None = None
    """the total said with its direction: "$1,250 spent", "$80 received",
    or "$900 in transfers and payments" when every row it counts moves
    money between accounts ("$900, mostly transfers and payments" when
    most of its dollars do), "$15 in interest" for a bank's interest line"""
    moved_rows: int | None = None
    """the rows it counts that are transfers or card/loan payments"""
    interest_rows: int | None = None
    """the rows it counts that are a bank's interest lines"""


class MerchantRename(Shape):
    id: int
    raw_merchant: str
    from_canonical: str | None
    to_canonical: str
    at: str
    label: str | None = None
    """the change in words: "Acme Fuel → Acme Fuel Co" """
    line: str | None = None
    """the bank line, when it is neither name: 'bank line “ACME FUEL CO 12”'"""


class SideRecent(Shape):
    date: str
    amount: float
    line: str
    account: str | None


class MergeSideFacts(Shape):
    """What the ledger holds under one side of a merge offer, read live."""
    rows: int
    total: float
    first: str | None
    last: str | None
    typical: float | None
    category: str | None
    city: str | None
    accounts: list[str]
    recent: list[SideRecent]


class MergedFacts(Shape):
    """What approving a merge offer makes (merchant_merge.merged_facts):
    the two sides together, and what the folded side adds, as printed."""
    rows: int
    total: float
    first: str | None
    last: str | None
    rows_delta: str
    """the folded side's rows, signed, like +12"""
    total_delta: str
    """the change in the survivor's total, signed whole dollars, like
    +$250 or −$50"""


class MergeProposal(Shape):
    id: str
    from_: str = Field(alias="from")
    into: str
    into_name: str
    """the surviving merchant's own name; `into` is the brand on a chain
    offer"""
    from_id: str
    into_id: str
    from_logo: str | None
    into_logo: str | None
    from_rows: int | None
    into_rows: int | None
    from_samples: list[str]
    into_samples: list[str]
    signals: list[str]
    supports: list[str]
    from_facts: MergeSideFacts | None
    into_facts: MergeSideFacts | None
    merged: MergedFacts | None = None
    """the "After merge" box's figures; null unless both sides have facts"""
    bulk: bool
    """one name is the other cut short (or one name spelled differently):
    "Merge all" may take it (merchant_merge.offer_shape)"""
    caution: str | None
    """what any other offer says before its reason — merged one at a time"""


class MerchantCatalog(Shape):
    """GET /api/merchants/catalog."""
    merchants: list[CatalogMerchant]
    recent: list[MerchantRename]
    merges: list[MergeProposal]


class MerchantLocation(Shape):
    city: str | None
    region: str | None
    address: str | None
    postal: str | None
    lat: float | None
    lon: float | None
    n: int
    last: str


class MerchantRule(Shape):
    category_primary: str
    source: str
    label: str | None = None
    """what the rule does: "new charges go to Groceries (your rule)" """


class MerchantDetail(Shape):
    """GET /api/merchants/detail."""
    display: str
    rows: int
    total: float
    first: str
    last: str
    variants: list[str]
    logo: str | None
    website: str | None
    phone: str | None
    kind: str | None
    mcc: str | None
    plaid: bool
    parent: str | None
    category: str | None
    locations: list[MerchantLocation]
    rule: MerchantRule | None
    total_label: str | None = None
    """the total said with its direction: "$1,250 spent", "$80 received",
    or in transfers and payments, or in interest, as the list says it"""
    moved_rows: int | None = None
    """the rows it counts that are transfers or card/loan payments"""
    interest_rows: int | None = None
    """the rows it counts that are a bank's interest lines"""


class MerchantMergeAnswer(Shape):
    pid: str
    action: Literal["approve", "reject"]
    # "Merge all": approve only while the offer is still the cut-off pair
    # it was listed as; 409 otherwise, which the batch skips
    bulk: bool = False
    # the survivor's id as the pending list showed it; an offer a sync has
    # turned since answers 409 and nothing merges. Absent (an older
    # client): an offer never turned is decided as it stands; a turned
    # one answers 409, since the survivor shown cannot be told
    into_id: str | None = None


class MerchantMerged(Shape):
    """An approval says what moved; a rejection names the offer."""
    ok: Literal[True]
    rejected: str = ""
    approved: str = ""
    from_: str = Field("", alias="from")
    into: str = ""
    raws: int = 0
    rows: int = 0


class MerchantRenameInput(Shape):
    display: str
    to: str


class MerchantRenamed(Shape):
    raws: int
    rows: int


class MerchantUndo(Shape):
    id: int | str


class MerchantUndone(Shape):
    raw_merchant: str
    restored: str | None


class Rule(Shape):
    merchant: str
    category_primary: str
    source: str
    disabled: bool
    count: int


class RuleCounts(Shape):
    user: int
    llm: int
    seed: int
    model: int


class RulesPage(Shape):
    """GET /api/rules: one page of rules and the per-source totals."""
    rules: list[Rule]
    total: int
    page: int
    per_page: int
    counts: RuleCounts
    counts_text: str
    """the counts in the words every client prints: "12 yours · 340 from
    an AI pass (none set up now) · 45 from your household's trained model ·
    90 built-in"."""


class RuleInput(Shape):
    merchant: str
    category: str


class RuleSet(Shape):
    merchant: str
    count: int
    undo: CategoryUndoRef


class RuleMerchant(Shape):
    merchant: str


class RuleDisable(Shape):
    merchant: str
    disabled: bool = False


class RuleDeleted(Shape):
    ok: Literal[True]
    deleted: int


class CategoryRename(Shape):
    """old/new; from/to are the older names of the same two fields."""
    old: str | None = None
    new: str | None = None
    from_: str | None = Field(None, alias="from")
    to: str | None = None


class CategoryRenamed(Shape):
    old: str
    new: str
    overrides: int
    primaries: int
    manual: int
    rules: int
    splits: int
    buckets: int
