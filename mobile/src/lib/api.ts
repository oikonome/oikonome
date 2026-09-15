import type { Txn } from "./ledger-row.generated";
// Thin API client. Every number the app shows comes from the server's
// /api — the app holds no business logic. Types mirror webapp/src/api's
// contract for the endpoints the app consumes.

// The ledger row is ONE shape across server, web and mobile — generated
// from specs/ledger-row.schema.json (scripts/gen-ledger-row-types.py).
export type { Txn } from "./ledger-row.generated";

// ---- cache-epoch abort hook ----
// query.ts binds this at startup so every request carries the CURRENT
// cache epoch's AbortSignal: a sign-out or the biometric lock aborts the
// epoch, and a response issued under the previous account/session can
// never land in the cache. A late-bound hook (not an import) keeps this
// module loadable in plain Node for scripts/pure-logic-tests.mjs.
let epochSignal: () => AbortSignal | undefined = () => undefined;
export function bindEpochSignal(fn: () => AbortSignal): void {
  epochSignal = fn;
}

export interface Alert {
  kind: string; severity: string; message: string; link?: string | null;
}

export interface TodayFull {
  date: string; is_today: boolean;
  real_today?: string; min_date?: string | null;
  day_of_month: number; days_in_month: number;
  verdict: string; variance: number; tolerance: number;
  // which hero face the account shows (today_view setting; the page's
  // toggle writes it back through /api/settings) and the simple face's
  // server-composed pieces, shared with web + email so nothing drifts
  today_view: "summary" | "detail";
  simple: { left_today: number;
            chips: { text: string; tone: string }[] };
  variable_actual: number; variable_budget: number;
  income: number | null; plan_surplus: number | null;
  savings_plan?: number;
  // sweep goals: plan cap, month-to-date availability, and the
  // server-composed sentence (identical wording in the daily email)
  sweep_plan?: number;
  sweep_available?: number | null;
  sweep_line?: string | null;
  variable_scale: { factor: number; static_total: number;
                    available: number } | null;
  // food/other may carry the custom-bucket decomposition: children carve
  // spend out of the parent for display; display_* / remaining_budget are
  // the parent AFTER the carve-outs
  buckets: Record<string, {
    actual: number; expected: number; month_budget: number;
    children?: { name: string; actual: number; expected: number;
                 month_budget: number }[];
    display_actual?: number; display_expected?: number;
    remaining_budget?: number;
  }>;
  fixed_unpaid_due?: number;
  irregular_bills?: { payee: string; amount: number; label: string }[];
  overdue_unpaid?: [string, number, string][];
  mtd: [string, number][];
  bills_remaining_month: number | null;
  forecast_rows: { date: string; amount: number; label: string;
                   bal_now: number | null; bal_stmt: number | null }[];
  pace_line?: string;
  allow: Record<string, {
    rate: number; plan: number;
    spent_today: number; today_allowance: number; left_today: number;
    month_budget: number; month_spent: number;
    daily_note: string | null;
    recover_days: number | null; recover_note: string | null;
  }>;
  bill_cards: { kind: string; label: string; left: number; over: boolean;
                sub: string; frac: number; pace_frac: number;
                status: string | null; status_tone: "neg" | "pos" | null }[];
  days_left: number; headroom: number | null;
  runway: { checking: number; card_debt: number; due_total: number;
            balance_unreliable?: boolean } | null;
  next_paycheck: string | null;
  recent: Txn[];
  allow_kids: { name: string; rate: number; plan: number;
                spent_today: number; today_allowance: number;
                left_today: number; month_budget: number;
                month_spent: number; daily_note: string | null;
                recover_days: number | null;
                recover_note: string | null }[];
  why?: { headline: string; recovery?: string | null;
          entries: { name: string; tone: "over" | "on" | "under";
                     text: string }[] } | null;
  why_chips?: { payee: string; amount: number; count: number }[];
  savings_goals: SavingsGoalProgress[];
  alerts: Alert[];
  forecast: { days: number; checking: number; card_debt: number;
              pace_now: Series; pace_stmt: Series;
              /** [date, amount, card name] — dated card autopays the cash
               *  chart draws as lines; amount is negative (money leaving) */
              card_autopay: [string, number, string][] } | null;
}

export interface MergeProposal {
  id: string; from: string; into: string; from_id: string; into_id: string;
  from_logo?: string | null; into_logo?: string | null;
  from_rows?: number | null; into_rows?: number | null;
  from_samples: string[]; into_samples: string[];
  signals: string[]; supports: string[];
}
export interface MerchantCatalog {
  // `variant_count` is the TRUE number of source strings behind a display
  // name; `variants` is capped by the server, so its length is not the
  // count and only `variant_count` may be printed as a fact.
  merchants: { display: string; rows: number; total: number;
               first: string; last: string; variants: string[];
               variant_count?: number; business_rows?: number;
               logo?: string | null; website?: string | null;
               kind?: string | null; plaid?: boolean; parent?: string | null }[];
  recent: { id: number; raw_merchant: string; from_canonical: string | null;
            to_canonical: string; at: string }[];
  // merchants the ledger thinks are one business (web: "Looks like one
  // merchant"), for the person to merge or keep apart
  merges?: MergeProposal[];
}

/** One merchant opened on the merchants screen: the merchant row's facts,
 *  the category the ledger shows for it (and the rule behind it), every
 *  source name, and the places it has been seen — the web's
 *  MerchantDetailPanel, one for one. */
export interface MerchantDetail {
  display: string; rows: number; total: number; first: string; last: string;
  variants: string[]; logo?: string | null; website?: string | null;
  phone?: string | null; kind?: string | null; mcc?: string | null;
  plaid?: boolean; parent?: string | null; category?: string | null;
  rule?: { category_primary: string; source: string } | null;
  locations: { city: string | null; region: string | null;
               address: string | null; postal: string | null;
               lat: number | null; lon: number | null;
               n: number; last: string }[];
}

/** Everything /api/transactions accepts. Two modes: a month browse (y+m
 *  alone) and the search path (any filter, paged). `bucket` is the
 *  search path asked a plan question instead of a category one — the
 *  rows the month verdict counted in that bucket — and needs y+m.
 *  `cat` with y+m and no date range is scoped to that month by the
 *  server; "?" means uncategorized. */
export interface TxnQuery {
  q?: string; acct?: string; cat?: string; bucket?: string;
  y?: string; m?: string; date_from?: string; date_to?: string;
  page?: string; channel?: string; checks?: string; reimb?: string;
  owner?: string; scope?: string;
}

export interface TxnPage {
  mode: string; rows: Txn[]; total?: number;
  // a plan-bucket question (bucket=food|other|<carve-out name> with y+m)
  // answers in the search shape and says which bucket it answered and
  // what to call it ("Food", "Everything else", the carve-out's name)
  bucket?: string; bucket_label?: string;
  // month mode: the month's cash flow by the Cash Flow page's definitions
  // (spend / income / income − spend) — transfers and card payments are
  // excluded, so these are not row sums
  out_sum?: number; in_sum?: number; net_sum?: number;
  // listed/matched rows that belong to a business (not in the totals
  // unless the business scope was asked for)
  biz_count?: number;
  amount_sum?: number;
  // a mixed Amazon box counts the whole charge — the true per-item spend
  // travels beside search totals
  amazon_items?: { count: number; sum: number; unpriced: number } | null;
  // search only: what one purchase from this merchant costs on average,
  // computed server-side over the whole result set and over spending rows
  // only. null when nothing matched counts as spending.
  spend_count?: number; spend_sum?: number; spend_avg?: number | null;
  // search only: accounts the term matched by name (rename or the bank's
  // own) — via_bank marks a renamed account found through the bank's name,
  // so the screen can explain those rows
  account_hits?: { id: string; label: string; bank_name: string;
                   via_bank: boolean }[];
}

export interface Me {
  email: string; tenant_id: string; role: "owner" | "viewer";
  version: string; hosted: boolean; demo: boolean;
  // hosted one-door bank linking: true once the PLATFORM's aggregator
  // credentials are live. False means the door is honest about not being
  // switched on yet rather than opening onto a failure.
  bank_link?: boolean;
  // the IANA zone every scheduled hour is kept in — the household's own
  // when set, else the instance's — and which it is (web: client.ts Me)
  timezone?: string; timezone_source?: "household" | "instance";
  instance_timezone?: string;
  donate_url?: string | null;
  totp_enabled?: boolean; verified?: boolean;
  created_at?: string | null;
  recovery_codes_left?: number;
  has_business?: boolean;
  // the household's allowance. institution_cap is how many banks the
  // instance allows it and institutions_used how many are spent — both counted
  // server-side, because the cap has a per-tenant override an operator
  // can raise and a hardcoded client number would then be a lie.
  features?: { institution_cap?: number | null;
               institutions_used?: number; sms?: boolean };
  // merchant logos on ledger/merchants/spending (Settings; default on)
  merchant_logos?: boolean;
  needs_2fa?: boolean;
  // the household's daily-email schedule + whether THIS login is muted
  daily_email?: { on: boolean; hour: number; summary: boolean;
                  muted: boolean };
  email_delivery?: { state: "bouncing" | "complained";
    bounce_type: string | null; reason: string | null;
    did_you_mean: string | null } | null;
  broadcast: { message: string; severity: "info" | "warn";
               deadline?: string | null } | null;
}

export interface Account {
  id: string; name: string; mask: string | null; kind: string;
  /** The aggregator's own name, kept under any rename — the editor shows
   *  it beside a custom display name and reverting restores it. */
  bank_name?: string | null;
  balance_current: number | null; balance_available: number | null;
  item_id?: string | null;
  institution_name: string; status: string | null;
  // institution branding (Plaid optional metadata): base64 PNG + brand hex
  // the bank's logo as a server path (/api/institutions/<item>/logo);
  // the phone fetches it with its bearer header and caches the image
  inst_logo_url?: string | null; inst_color?: string | null; inst_url?: string | null;
  owner?: string | null; entity_id?: string | null; txns: number;
  aggregator?: string | null;
  // a card's next due date + statement balance from the liabilities pull
  card_due?: string | null; card_statement?: number | null;
  user_removed_at?: string | null;
  link?: { group_id: string; home_rank: number; primary: boolean;
           healthy: boolean } | null;
  script?: { source: string; last_push: string; ok: boolean;
             token_revoked_at?: string | null } | null;
}

/** Whether an account's balance belongs in a money aggregate.
 *
 *  Linked sources feed ONE real account — the healthiest live source
 *  serves, the rest are shadows of the same money. The server excludes
 *  them from every aggregate it computes, so a screen that adds
 *  balances itself has to exclude them too or a dual-sourced account
 *  lands in the total twice. The rows still render (badged "backup") —
 *  only the arithmetic drops them. */
export const countsInTotals = (a: Pick<Account, "link">) =>
  !a.link || a.link.primary;

/** The linked-sources card is status the owner may put away; it comes
 *  back by itself when there is something to decide — a pair that looks
 *  like the same account, or a linked source that went down. Mirrors
 *  the web's linkCardVisible. */
export const linkCardVisible = (d: {
  groups: { members: { healthy: boolean }[] }[];
  suggestions: unknown[]; card_dismissed: boolean }) =>
  d.suggestions.length > 0
  || d.groups.some((g) => g.members.some((m) => !m.healthy))
  || (!d.card_dismissed && d.groups.length > 0);

/** Whether a business transaction belongs in the profit figure.
 *
 *  Profit is revenue minus OPERATING expenses: §248 organizational and
 *  §195 start-up costs are capitalized and reported on their own lines,
 *  which is why the server's net_operating leaves them out. An expense
 *  with no explicit bucket falls back the way the server's does — dated
 *  before the business opened, it is pre-operating start-up cost.
 *  Money in (negative amount) is revenue, never bucketed, always counts. */
export const inOperatingProfit = (
  x: Pick<BizTxn, "amount" | "date" | "bucket">,
  businessStart?: string | null,
): boolean => x.amount < 0
  || (x.bucket
      || (businessStart && x.date && x.date < businessStart
          ? "startup_195" : "operating")) === "operating";

export interface Connection {
  id: string; aggregator: string; institution_name: string | null;
  status: string | null; last_ok: string | null;
  // last_ok is when WE last read Plaid's copy; bank_updated is when the
  // BANK last refreshed that copy. Showing only the first makes a correct
  // no-op sync look like a broken one.
  bank_updated_at?: string | null; bank_failed_at?: string | null;
  // classified server-side: ok | retrying (bank outage, nothing to do) |
  // reauth (sign in again / share accounts) | gone | attention. Never
  // re-derive it from `status` — an outage is not a broken login.
  status_kind?: "ok" | "retrying" | "reauth" | "gone" | "attention";
  // Plaid's fleet-wide login health for the bank (HEALTHY | DEGRADED |
  // DOWN), cached hourly server-side; stale readings are dropped there
  institution_health?: string | null;
  // why a billed product returns nothing for this item: 'consent' (never
  // granted — a re-link can attach it) vs 'not_supported' (the bank
  // cannot provide it). Explains a forecast with no autopay line.
  product_issues?: Record<string, { cause: "consent" | "not_supported";
                                    code?: string }> | null;
  accounts?: number;
}
export interface Holding {
  acct: string; symbol: string; fund: string | null;
  qty: number | null; price: number | null; value: number;
}
export type AccountRemoveMode =
  "hide" | "disconnect" | "disconnect_purge" | "purge";

export interface Proposal {
  id: string; kind: string; bill_type?: string | null;
  payee: string; amount: number;
  cadence: string; summary: string; income?: boolean;
  next_due?: string | null; llm_tag?: string | null;
  /** an "attach" (fee) or "merchant" proposal: the bill it would join */
  bill_payee?: string | null;
}
export interface Bill {
  // the fees that ride with the payment ("incl. $1 fee")
  companions?: Companion[]; fee_total?: number;
  // the merchants the bill pays, by display name — its identity (web:
  // "Merchants" on the edit form)
  merchants?: string[];
  payee: string; amount: number; income: boolean; cadence: string;
  disabled?: boolean;
  due_on: string | null; health: string | null; health_dismissed: boolean;
  // the newest ledger charge for this merchant, whatever its amount
  last_seen?: { date: string; amount: number } | null;
  // the attention queue's raw material: when the bill last matched, its
  // cycle, and the ONE action the server's evidence supports — rendered
  // verbatim, never invented client-side (mirrors webapp)
  last_match?: string | null;
  cycle_days?: number | null;
  suggestion?: { action: "accept_amount" | "disable" | "review";
                 amount?: number; reason: string } | null;
  cadence_value?: string;
  fit?: LedgerFit | null; fit_diverges?: boolean;
  hint_dismissed?: boolean;
  occurrences: number; paid: number; paid_amount: number;
  used: number | null; overflow: number | null; next_unpaid: string | null;
  pool: number | null; period_months: number | null;
  period_used: number | null; period_left: number | null;
}
export interface BillsData {
  proposals: Proposal[]; bills: Bill[];
  archived: { payee: string; amount: number; frequency?: string | null;
              synced_at?: string | null }[];
  totals?: { bills_monthly: number; income_monthly: number;
             week_total: number; week_count: number; attention: number };
}

export interface PLRow {
  name: string; value: number; pl: number; pct: number | null;
  invested?: number; basis?: number; contributed?: number;
  cash_in?: number; cash_out?: number;
}
export interface NetWorthReport {
  current_total: number; property_net: number; full_total: number;
  property_items: { name: string; kind: string; value: number;
                    source?: string }[];
  by_institution: [string, number][];
  by_asset_class: [string, number][];
  trend: [string, number][];
  coinbase_pl: PLRow[];
  retirement_combined: {
    groups?: { provider: string; accounts: PLRow[]; invested: number;
               value: number; pl: number; pct: number | null }[];
    total?: { invested: number; value: number; pl: number;
              pct: number | null };
  };
  trend_estimated_until: number;
  _built_at?: string;
}
export interface FeesReport {
  by_platform: [string, number, number][];
  by_year: [string, number][];
  total: number;
}
export interface ManualAsset {
  name: string; value: number; kind?: string | null;
  as_of?: string | null; auto?: string | null;
}
export interface SpendingReport {
  by_year: [string, number, number][];
  by_month: [string, number][];
  // [payee, amount, count, logo_url|null]
  top_merchants: [string, number, number, (string | null)?][];
  category_totals: [string, number][];
  amazon: [string, number, number][];
  yoy_years: string[];
  yoy_cats: string[];
  yoy_matrix: (string | number)[][];
  _built_at?: string;
}
// where the money came from and where it went, for one period — income by
// kind on the left, fixed / variable / saved on the right (mirrors the web)
export interface FlowPeriod {
  from: string; to: string; label: string;
  in: { total: number; paychecks: number; interest: number; other: number };
  out: { total: number; fixed: number; variable: number };
  saved: number; rate: number | null;
}
// served per range (the site-wide 3m/6m/1y/3y/5y/all vocabulary), not in
// the report — the fixed split walks months and 'all' costs seconds
export type FlowKey = "cur" | "1m" | "3m" | "6m" | "1y" | "3y" | "5y" | "all";
// the Spending tab's windowed view — ranked lists each carry the previous
// equal window's figure (null when the range is 'all': no previous window)
export interface SpendingWindow {
  from: string; to: string; label: string; months: number;
  total: number; prev_total: number | null;
  by_month: [string, number][];
  categories: [string, number, number | null][];
  // [payee, amount, visits, prev-window amount|null, logo]
  merchants: [string, number, number, number | null, string | null][];
}
export interface IncomeKinds { paychecks: number; interest: number; other: number }
export interface IncomeWindow {
  from: string; to: string; label: string; months: number;
  total: number; prev_total: number | null;
  kinds: IncomeKinds; prev_kinds: IncomeKinds | null;
  by_month: [string, number][];
  // [payer, amount, deposits, prev-window amount|null, logo]
  sources: [string, number, number, number | null, string | null][];
}
export interface CashflowReport {
  savings_by_year: [string, number | null, number,
                    number | null, number | null][];
  monthly_by_year: Record<string, [string, number][]>;
  income_by_month: [string, number][];
  investment_funding: [string, number][];
  career_income: [number, number][];
  reported_income: [number, number | null, number | null, number | null,
                    number | null, boolean | number | null][];
  cash_graph: { points: [string, number][]; forecast_from: number };
  _built_at?: string;
}

export interface AlertRow {
  kind: string; severity: string; message: string;
  first_seen: string; last_seen: string; active: number; dismissed: number;
}

export interface EmailSchedule {
  daily?: { on: boolean; hour: number; sms?: boolean; push?: boolean;
            summary?: boolean };
  weekly?: { on: boolean; hour: number; weekday: number;
             sms?: boolean; push?: boolean };
  monthly?: { on: boolean; hour: number; sms?: boolean; push?: boolean };
  yearly?: { on: boolean; hour: number; sms?: boolean; push?: boolean };
}

export interface MobileDevice {
  id: string; device_name: string; platform: string;
  created_at: string; last_seen: string | null; current: boolean;
}

export interface ReceiptItem {
  line: number; description: string; qty: number | null;
  amount: number; tag: string;
}
export interface Receipt {
  id: string; mime: string; has_optimized?: boolean;
  kind: "receipt" | "check";
  status: "uploaded" | "parsing" | "parsed" | "failed";
  parsed: { merchant?: string | null; date?: string | null;
            total?: number | null; tax?: number | null;
            tip?: number | null;
            // check-kind fields (front-of-check parse)
            check_number?: string | null; payee?: string | null;
            amount?: number | null; memo?: string | null;
            bank?: string | null } | null;
  // parsed check amount disagrees with the transaction (display only)
  amount_mismatch?: boolean;
  error: string | null; created_at: string; items?: ReceiptItem[];
}

export interface BillDetail {
  payee: string; amount: number; income: boolean; cadence: string;
  next_due: string; bill_type: string; source: string;
  category: string | null; merchant: string | null; active: boolean;
  cap: number | null; disabled: boolean;
  show_today: boolean; match_category: boolean;
  // the TRANSACTION category stamped on the rows this bill matches ("" =
  // none) — the web's "matched charges are …"
  txn_category?: string;
  // the fees that ride with the payment (web: "Fees & add-ons")
  companions?: Companion[]; fee_total?: number;
  // the merchants the bill pays (see Bill.merchants)
  merchants?: string[];
  cadences: [string, string][];
}
export interface Companion { tokens: string; amount: number; window_days: number }
export interface LedgerFit {
  kind: string; amount: number; cadence: string; next_due: string;
  short: string; label: string; cycle_days?: number; hits?: number;
}
// history rows are the Transactions-page shape plus the matcher verdict
export type HistoryTxn = Txn & { txn_id: string; hit: boolean };
export interface BillsHistoryData {
  payee: string;
  logo?: string | null;
  txns: HistoryTxn[];
  monthly: { month: string; total: number; count: number }[];
  fit: LedgerFit | null;
  lifetime: { total: number; count: number;
              first: string | null; last: string | null;
              active_months: number; avg_monthly_active: number };
  inflow: boolean;
  bill: { amount: number; bill_type: string; source: string;
          cadence: string; due_on: string | null; income: boolean } | null;
  health: { health: string; last_match: string | null;
            matched?: number; fit_diverges?: boolean } | null;
  proposals: Proposal[];
  covered_by: string | null;
  hint_dismissed: boolean;
  cadences: [string, string][];
}

export interface PendingReimb {
  id: string; date: string; amount: number; payee: string;
  category: string; pending: number; account: string | null;
  partial?: number; expected?: number | null; received?: number;
}
export interface ReimbCandidate {
  id: string; date: string; amount: number; payee: string;
  account: string | null; why?: string | null;
}
export interface ReimbPair {
  expense_id: string; reimburse_id: string; partial: number;
  received: number | null; created_at: string;
  expense_date: string; expense_amount: number; expense_payee: string;
  deposit_date: string; deposit_amount: number; deposit_payee: string;
}
export interface RuleRow {
  merchant: string; category_primary: string; source: string;
  disabled: boolean; count: number;
}
export interface RulesPage {
  rules: RuleRow[]; total: number; page: number; per_page: number;
  counts: { user: number; llm: number; seed: number; model?: number };
}
export interface RetirementInputs {
  age: number; spend: number; ret: number; infl: number; end: number;
  ssage: number; employer_mo: number; taxable_mo: number; resume: number;
  stockpct: number; saving_yr: number; your_ss: number;
  has_sched: boolean;
}
export interface RetirementData {
  buckets: { cash: number; taxable: number; td: number; roth: number;
             total: number };
  earliest: number | null;
  real_return: number;
  spend: number; spend_now: number; grow_to_67: number;
  growth_path: [string, number][];
  rows: { age: number; at_retire: number; feasible: boolean;
          fail_age: number | null; max_spend: number;
          hist_pct: number | null }[];
  stress: { label: string; earliest: number | null;
            max_spend_at_ref: number }[];
  stress_ref_age: number;
  hist: { ok: number; n: number; pct: number; spend90: number;
          spend100: number; stock_frac: number } | null;
  inputs: RetirementInputs;
}
export interface Batch {
  id: string; created_at: string; filename: string | null;
  source: string; row_count: number | null;
  // receipts, notes, pairings, tags and overrides people added to this
  // batch's rows since — a rollback deletes them too
  annotations?: number;
}
// a picked file as RN's networking layer wants it
export interface PickedFile { uri: string; name: string; mime?: string }
export interface RestoreProgress {
  state: "idle" | "running" | "done" | "error";
  started_at?: string | null;
  progress: {
    table?: string | null; rows?: number; tables_done?: number;
    error?: string; result?: ImportResult;
  };
}
export interface ImportResult {
  error?: string; imported?: number; skipped_duplicates?: number;
  // Mapped-CSV refusals only. `token` is a fresh claim token for the same
  // upload — the mapping is correctable, submit again with it. `expired`
  // says the upload is gone for good and the file has to be re-uploaded.
  token?: string; expired?: boolean;
  restore_started?: boolean; job?: string;
  source?: string; confidence?: number; warnings?: string[];
  note?: string; split_accounts?: Record<string, number>;
}
export interface ImportOutcome {
  result: ImportResult | null;
  mapping_needed: { token: string; header: string[]; filename: string;
                    sample?: string[][] } | null;
}
export interface PlanFile {
  index: number; name: string; kind: string; size: number;
  action: "import" | "skip"; account_id: string | null;
  new_account_name: string; amount_sign: string;
  sign_certain: boolean; note: string; oversized?: boolean;
}
export interface TaxdocRow {
  year: number; exists?: boolean;
  total_income?: number | null; agi?: number | null;
  taxable_income?: number | null; tax_paid?: number | null;
  wages?: number | null; ss_earnings?: number | null;
  medicare_earnings?: number | null;
  employer?: string | null; ein?: string | null;
  box1?: number | null;
}
export interface TaxdocPlan { token: string; kind: string; rows: TaxdocRow[] }
export interface SyncStatus {
  state: "none" | "running" | "done" | "error";
  progress: { ok?: boolean; error?: string };
}
export interface Entity {
  id: string; name: string; structure: string; status: string;
  /** when the business was closed (null while active) */
  archived_at?: string | null;
  state?: string | null; formation_date?: string | null;
  business_start_date?: string | null; ein_last4?: string | null;
  home_office_sqft?: number | null; income_tax_rate?: number | null;
  tax_reserve_account_id?: string | null;
  members?: { id: string; member_name: string;
              ownership_pct: number | null; is_manager: boolean }[];
}
export interface EntityDeleteImpact {
  id: string; name: string;
  /** rows the CASCADE FKs destroy with the entity, counted per table */
  destroyed: { members: number; equity_movements: number;
               compliance_obligations: number; mileage_trips: number;
               vendors_1099: number };
  /** rows that survive with the assignment cleared (back to personal) */
  detached: { accounts: number; transactions: number };
}
export interface BizData {
  rows: { id: string; date: string; amount: number; payee: string;
          category: string; account: string | null; pending: number;
          note?: string | null }[];
  total: number; count: number; years: number[];
}
export interface Capital {
  contributions: number; draws: number; distributions: number;
  reimbursements: number; capital_balance: number; net_income?: number;
}
export interface Pnl {
  year: number | null; revenue: number; operating_expenses: number;
  operating_by_line: Record<string, number>; net_operating: number;
  organizational: number; startup_195: number;
  organizational_deduction: { total: number; immediate: number;
    amortizable: number; monthly_amortization: number };
  startup_deduction: { total: number; immediate: number;
    amortizable: number; monthly_amortization: number };
  capital: Capital;
}
export interface BizTxn {
  id: string; date: string | null; amount: number; payee: string;
  category: string | null; note?: string | null;
  cat_detailed: string | null;
  /** the aggregator's original category — a TRANSFER_IN later renamed by
   *  an override is still not revenue (books._pnl_core's was_transfer) */
  cat_original?: string | null;
  bucket: string | null; sched_c_line: string | null;
}
export interface EstimatedTax {
  year: number; net_profit: number; mileage_deduction: number;
  home_office_deduction: number; taxable_profit: number; se_tax: number;
  income_tax_rate: number | null; income_tax: number | null;
  total_estimated: number; quarterly: number; deadlines: string[];
}
export interface Obligation {
  id?: string; title: string; due: string; fee: number | null;
  url: string | null; recurrence: string;
}
export interface MileageTrip {
  id: string; date: string | null; miles: number;
  purpose: string | null;
}
export interface Vendor {
  merchant: string; paid: number; reportable: boolean;
  needs_1099: boolean;
}

export interface CustomBucket {
  name: string; parent: "food" | "other"; monthly: number;
  categories: string[]; merchants: string[];
}
export interface IncomeScenario {
  name: string; take_home: number; cadence: string;
}
export interface SavingsGoal {
  name: string; target: number; target_date?: string | null;
  monthly_plan: number; account_id?: string | null;
  tokens: string[]; start_balance: number;
  // absent = "monthly" (fixed commitment); "sweep" contributes at month
  // end, only what the month's realized surplus covers (web's naming)
  mode?: "monthly" | "sweep" | null;
}
// the slice of /api/settings the budget screens read/write
/** GET /api/settings — and what every settings write returns. */
export type SettingsView = BudgetSettings & Record<string, unknown>;

export interface BudgetSettings {
  // default landing page per client (absent = Today) — mirrors web
  home_web?: string | null;
  home_mobile?: string | null;
  food_monthly: number | null; other_monthly: number | null;
  budgeted_income_monthly: number | null;
  custom_buckets: CustomBucket[] | null;
  dynamic_variable_budget: boolean | null;
  income_scenarios: IncomeScenario[] | null;
  active_scenario: string | null;
  savings_goals: SavingsGoal[] | null;
  occurrence_caps: Record<string, number> | null;
  disabled_bills: string[] | null;
  dismissed_hints: Record<string, unknown> | null;
}
export interface BudgetSuggestions {
  months: string[];
  suggestions: { food_monthly?: number; other_monthly?: number;
                 income_monthly?: number; buckets: Record<string, number> };
  bucket_candidates?: { name: string; category: string;
                        monthly: number }[];
  avg_bills_monthly?: number; bills_count?: number;
}

export interface Series {
  series: [string, number][]; min: number; min_date: string;
  negative_date: string | null; end: number; rate: number;
  first_neg_amount?: number | null;
}

export interface SavingsGoalProgress {
  name: string; target: number; target_date: string | null;
  monthly_plan: number;
  saved: number | null; rate_90d: number | null;
  pct: number | null; eta: string | null;
  on_pace: boolean | null; plan_only: boolean;
  mode?: "monthly" | "sweep";
  // sweep goals only: matched-transfer evidence this month and whether
  // it already covers the plan (the month is satisfied)
  swept_this_month?: number | null;
  sweep_satisfied?: boolean | null;
}

export interface LensTxn {
  date: string; amount: number; payee: string; category: string;
  account: string | null; pending: boolean; fixed: boolean;
}
export interface MonthLensData {
  y: number; m: number; status: string; as_of: string;
  days_in_month?: number;
  verdict: string; variance: number; tolerance?: number;
  variable_actual: number; variable_budget: number;
  variable_scale?: { factor: number; static_total: number } | null;
  projection: { pace_total: number; variance: number;
                tolerance?: number; verdict: string } | null;
  buckets: Record<string, {
    actual: number; expected: number; month_budget: number;
    children?: { name: string; actual: number; expected: number;
                 month_budget: number }[];
    display_actual?: number; display_expected?: number;
    remaining_budget?: number;
  }>;
  bills: { posted: number; planned: number; awaiting: number;
           paid?: { date: string; amount: number; payee: string }[];
           overdue: [string, number, string][];
           envelopes?: { payee: string; used: number; monthly: number;
                         overflow: number }[] };
  // [display label, amount, the STORED category key the ledger filters
  // on]. The label is the display form ("FOOD AND DRINK"); only the key
  // ("FOOD_AND_DRINK") matches server-side. null on the clustered Amazon
  // parent — no single category stands behind that number — and "?" is
  // uncategorized.
  by_category: [string, number, string | null][];
  amazon_subs?: [string, number, string | null][];
  biggest: LensTxn[];
  income: { actual: number | null; budgeted: number | null };
  spend_total: number;
  saved_month?: number | null;
  plan_surplus?: number | null;
  // "snapshot": closed month judged against its frozen budget; "live":
  // today's config (the page notes the retroactive judgment)
  budget_source?: "snapshot" | "live";
  // same flag for the bill schedule — "live" on a closed month means its
  // snapshot predates bill freezing, so today's schedule applied
  bills_source?: "snapshot" | "live";
  first_year?: number | null;
}
export interface YearCell {
  m: number; status: string; verdict: string | null;
  variance: number | null; variable_actual: number | null;
  variable_budget: number | null;
  // "net" cells only — a closed month with no budget snapshot shows its
  // cash flow (income − spending) instead of a verdict
  income?: number | null; spend?: number; net?: number | null;
}
export interface YearTotals {
  income: number | null; spend: number; saved: number | null;
  rate: number | null;
}
// Budget history (the Budget page's card) — each frozen month's variable
// budgets, how it was captured ("close" as the month ran, "backfill" =
// applied retroactively by the owner), and how many closed months since
// the ledger began have no snapshot.
export interface BudgetSnapshots {
  months: { y: number; m: number; food_monthly: number;
            other_monthly: number; variable_budget: number;
            source: "close" | "backfill"; captured_at: string;
            // the schedule frozen with the budget — absent on snapshots
            // that predate bill freezing
            bills_monthly?: number; income_monthly?: number }[];
  missing: number;
  budget_set: boolean;
}
export interface YearLensData {
  y: number; cells: YearCell[];
  totals: YearTotals; prev_totals: YearTotals;
  // [display label, this year, last year, the STORED category key]; the
  // key is what the ledger filters on, null when no single category
  // stands behind the row
  categories: [string, number, number, string | null][];
  first_year: number | null;
  buckets_annual?: Record<"food" | "other" | "fixed", {
    actual: number; expected: number; month_budget: number;
    children?: { name: string; actual: number; expected: number;
                 month_budget: number }[];
    display_actual?: number; display_expected?: number;
    remaining_budget?: number;
  }>;
  saved_ytd?: number | null;
  plan_surplus?: number | null;
}


// ---- session elevation ("sudo mode") — the controller ----
// Re-proving identity is not a password box on every settings card:
// a guarded call is refused ONCE with 403 elevation_required, the
// person proves it is them in ONE sheet (passkey / device biometric where
// the account has a passkey, else password + a live code on a TOTP
// account), and the session is elevated for ten minutes during which every
// guarded action just works. Two doors always ask again (`fresh`): account
// delete and the download-everything export.
//
// This section owns the promise, not the UI: `withElevation` below awaits
// `elevate(fresh)` before retrying the refused call, and the sheet mounted
// in app/_layout.tsx (components/elevation-modal.tsx) registers itself as
// the prompter that resolves it. It lives in this module rather than its
// own because api.ts is loaded by plain node for
// scripts/pure-logic-tests.mjs, where a runtime import of a sibling .ts
// file does not resolve — every other sibling import here is type-only.

/** GET /api/auth/elevation — and what a successful POST returns. */
export interface ElevationStatus {
  elevated: boolean;
  until: string | null;
  // ["passkey"], ["password"], or ["passkey", "password"] (passkey first)
  // for an account holding both a passkey and a password+TOTP
  methods: ("passkey" | "password")[];
  totp: boolean;
}

/** POST /api/auth/elevate — exactly one proof per call. */
export type ElevationProof =
  | { password: string; totp_code?: string }
  // a lost authenticator: the recovery code stands in for the second
  // factor beside the password, as it does at login. A bare code is the
  // passkey-only account's shape and is refused on a password account.
  | { password: string; recovery_code: string }
  | { stepup_ticket: string }
  | { recovery_code: string };

/** What the sheet is told when it opens: why it is up, and how to answer. */
export interface ElevationPrompt {
  fresh: boolean;
  resolve: () => void;
  reject: (e: unknown) => void;
}

/** The person closed the sheet. Screens show it like any other failure
 *  (errText / explain both fall back to the message), which is the point:
 *  a cancelled confirmation reads as "not done", never as a crash. */
export const ELEVATION_CANCELLED =
  "Cancelled — confirm it's you to make this change.";

/** The sheet the root layout mounts. Null until it mounts, and null again
 *  when it unmounts, so a call made with no UI to answer it fails fast
 *  instead of hanging forever. */
let prompter: ((p: ElevationPrompt) => void) | null = null;

export function registerElevationPrompter(
  fn: ((p: ElevationPrompt) => void) | null,
): void {
  prompter = fn;
}

// One sheet at a time: two guarded calls refused in the same instant (a
// screen that fires a pair of mutations) share the prompt instead of
// stacking two. A `fresh` request never shares — its whole point is a
// proof given for THIS action.
let inflight: Promise<void> | null = null;

/** Wait for the person to prove it is them. Resolves once the session is
 *  elevated (the caller then retries); rejects with the cancel sentence,
 *  or with whatever the sheet could not recover from. */
export function elevate(fresh: boolean): Promise<void> {
  if (!fresh && inflight) return inflight;
  const p = new Promise<void>((resolve, reject) => {
    if (!prompter) {
      reject(new Error(ELEVATION_CANCELLED));
      return;
    }
    prompter({ fresh, resolve, reject });
  });
  if (!fresh) {
    inflight = p;
    p.finally(() => { if (inflight === p) inflight = null; })
      .catch(() => {});
  }
  return p;
}

/** Whether a failure is the server asking for elevation — the one refusal
 *  the api client answers with the sheet and a retry. Structural on
 *  purpose: the body is what the server promised, and the class of the
 *  error object is not part of that promise. */
export function isElevationRequired(e: unknown): boolean {
  const status = (e as { status?: unknown })?.status;
  const body = (e as { body?: { error?: unknown } })?.body;
  return status === 403 && body?.error === "elevation_required";
}

/** The `fresh` flag the refusal carried — true means the sheet must show
 *  even when the session was elevated a moment ago. */
export function refusalWantsFresh(e: unknown): boolean {
  return !!(e as { body?: { fresh?: unknown } })?.body?.fresh;
}

export class ApiError extends Error {
  status: number;
  detail: string;
  /** the parsed error body, when the server sent a structured one. Some
   *  refusals carry what you need to ACT on them — the device-limit 409
   *  ships the device roster so the sign-in screen can offer to sign one
   *  out, rather than naming a Settings page the caller cannot reach
   *  without first getting past that screen. */
  body?: unknown;
  constructor(status: number, detail: string, body?: unknown) {
    super(`${status}: ${detail}`);
    this.status = status;
    this.detail = detail;
    this.body = body;
  }
}

// ---- read-only (402) notice ----
// An installed add-on may make an account read-only: every write is
// refused with 402 while reads keep working — so buttons would look like
// silent no-ops. The web toasts the server's sentence once per short
// window (main.tsx); the mobile client mirrors that. This function is the whole decision —
// pure, exported so node can pin it: only WRITES surface the notice
// (reads keep working under lockout and must never nag), repeated
// blocked writes inside the window collapse into one notice, and the
// server's own sentence wins over the fallback because it names the
// way out.
export const PAYWALL_FALLBACK = "This account is read-only right now.";
export const PAYWALL_NOTICE_WINDOW_MS = 10_000;

export const isWriteMethod = (method: string | undefined): boolean =>
  !!method && !/^(GET|HEAD)$/i.test(method);

/** The sentence to show for a 402, or null when nothing should surface
 *  (a read, or a notice already shown inside the window). */
export function paywallNotice(
  detail: string | undefined,
  method: string | undefined,
  lastShownAt: number,
  now: number,
): string | null {
  if (!isWriteMethod(method)) return null;
  if (now - lastShownAt < PAYWALL_NOTICE_WINDOW_MS) return null;
  return (detail ?? "").trim() || PAYWALL_FALLBACK;
}

/** The sentence to show a person for a failed call — the web's errText.
 *
 *  `String(e)` prints "Error: 400: display too long (200 chars max)",
 *  which reads as a crash rather than as an answer. Prefer the server's
 *  own detail, and fall back to whatever the failure carried. */
export function errText(e: unknown): string {
  if (e instanceof ApiError && e.detail) return e.detail;
  const raw = e instanceof Error ? e.message : String(e);
  const m = /^(\d{3}):\s*([\s\S]*)$/.exec(raw);
  const body = (m ? m[2] : raw).trim();
  try {
    const parsed = JSON.parse(body);
    const d = parsed?.detail ?? parsed?.message;
    if (typeof d === "string" && d) return d;
    if (Array.isArray(d) && d[0]?.msg) return String(d[0].msg);
  } catch { /* not JSON — the body IS the message */ }
  return body || raw;
}

/** normalize what a person types into an origin the client can call */
export function normalizeServerUrl(input: string): string {
  let u = input.trim().replace(/\/+$/, "");
  if (!/^https?:\/\//i.test(u)) u = `https://${u}`;
  return u;
}

// ---- the login screen's server hand-off ----
// expo-router exposes every screen as a deep link, so /login can be
// opened from OUTSIDE the app (a link in an SMS/email/QR). A URL
// parameter is therefore attacker-writable: oikonome://login?serverUrl=
// https://evil.tld would pre-wire the real login form to POST the
// user's email, password and live TOTP code to a phisher. The login
// screen must never read its server from the URL — the ONLY trusted
// source is this in-memory slot, written by the connect screen after
// its own probe, which no deep link can reach. A deep link straight to
// /login finds the slot empty and is bounced back to connect.
let verifiedServerUrl: string | null = null;

// ---- the pinned server ----
// A distributed build is for ONE server: the publisher's store builds
// point at the hosted service, an operator's own build at their
// instance. Baking the origin in at build time (EXPO_PUBLIC_SERVER_URL,
// read by Metro when the bundle is made) removes the connect screen —
// nobody installing the hosted app should be asked for a URL they were
// never told. Unset, the app is the bring-your-own-server build and
// asks on first run. Normalized once so a trailing slash in the build
// env cannot become a double-slash in every request.
export const PINNED_SERVER_URL: string | null =
  process.env.EXPO_PUBLIC_SERVER_URL?.trim()
    ? normalizeServerUrl(process.env.EXPO_PUBLIC_SERVER_URL)
    : null;

/** connect.tsx deposits its probe-verified server here before
 *  navigating to /login. */
export function setVerifiedServerUrl(url: string): void {
  verifiedServerUrl = url;
}

/** The only server /login may sign into: the build's pinned server, or
 *  the one the in-app connect flow verified this launch, or null (→ go
 *  to connect). The pin wins over the slot so a deep link cannot steer
 *  a pinned build anywhere else. */
export function getVerifiedServerUrl(): string | null {
  return PINNED_SERVER_URL ?? verifiedServerUrl;
}

// ---- the transaction screen's row hand-off ----
// /txn is deep-linkable for the same reason /login is, so a serialized
// row in the URL would let an outside link open the REAL detail screen
// over attacker-chosen payee/amount text, with the edit buttons wired
// to whatever id it named. The screen therefore takes only an id from
// its route and reads the row from this in-memory slot, which only
// in-app navigation (a ledger row tap) can fill. A deep link, or a cold
// start on the route, finds no matching row and shows nothing.
let handedOffTxn: Txn | null = null;

/** A ledger row deposits itself here right before pushing /txn?id=. */
export function setHandedOffTxn(t: Txn): void {
  handedOffTxn = t;
}

/** The row in-app navigation handed over for this id, or null when the
 *  route was reached any other way. */
export function getHandedOffTxn(id: string | undefined): Txn | null {
  return id && handedOffTxn && handedOffTxn.id === id ? handedOffTxn : null;
}

/** What a person is told when a 2xx did not carry an answer. */
export const UNREADABLE_REPLY =
  "The server's reply was not readable. If you are on public Wi-Fi, its "
  + "sign-in page may be answering instead of your server.";

/** The value a successful response actually carries — or the app's normal
 *  refusal.
 *
 *  Every route this client calls answers JSON. A captive Wi-Fi portal, a
 *  misconfigured proxy and a CDN error page all answer 200 with HTML, and
 *  `r.json()` on that throws a bare SyntaxError out of whatever awaited the
 *  call — an unhandled rejection anywhere react-query is not holding the
 *  promise (a directly awaited call in a button handler), with no
 *  ErrorBoundary under it. The web client reads the content-type before
 *  parsing for the same reason (webapp/src/api/client.ts); here the
 *  non-JSON body becomes an ApiError as well, because a caller expecting a
 *  record has no use for a page of HTML and every screen already knows how
 *  to show an ApiError.
 *
 *  Pure and exported so node can pin it. */
export function parseOkBody<T>(status: number, contentType: string | null,
                               body: string): T {
  if ((contentType ?? "").toLowerCase().includes("json")) {
    try {
      return JSON.parse(body) as T;
    } catch {
      // JSON promised, truncated or garbage delivered — same story to tell.
    }
  }
  // keep a snippet: enough for a bug report, never the whole page
  throw new ApiError(status, UNREADABLE_REPLY, body.slice(0, 200));
}

async function parseError(r: Response): Promise<ApiError> {
  let detail = r.statusText;
  let structured: unknown;
  try {
    const body = await r.json();
    if (typeof body?.detail === "string") detail = body.detail;
    else if (body?.detail && typeof body.detail === "object") {
      // a refusal that carries the means to act on it
      structured = body.detail;
      const m = (body.detail as { message?: unknown }).message;
      if (typeof m === "string") detail = m;
    }
  } catch { /* non-JSON error body — keep the status text */ }
  return new ApiError(r.status, detail, structured);
}

// last time the read-only (402) notice fired — module-level (the web
// keeps it module-level in main.tsx too) so a re-created client (unlock,
// token refresh) doesn't reset the collapse window
let lastPaywallNotice = 0;

export function makeClient(baseUrl: string, token: string,
                           onAuthLost: () => void,
                           onPaywall?: (message: string) => void) {
  // Every response — req's and the hand-rolled FormData/blob/redirect
  // fetches below — funnels through here, so a token revoked or idled
  // out mid-request drops the app back to login everywhere instead of
  // surfacing a bare "Unauthorized" alert on some screens only.
  // ...EXCEPT a step-up refusal: a wrong password or one-time code on a
  // "sign out that session" / passkey / export door is a 401 too, and it
  // means "try again", not "your session is gone" — the web client draws
  // the same line (main.tsx). Same wording list, kept in step.
  const REAUTH = /totp_required|recovery_required|password_required|wrong password|one-time code|bad credentials|passkey/i;
  const checkAuth = async (r: Response, method?: string): Promise<void> => {
    if (r.status === 401) {
      const err = await parseError(r);
      if (!REAUTH.test(err.detail)) onAuthLost();
      throw err;
    }
    // An installed add-on may make the account read-only: every WRITE is
    // refused with 402 while reads keep working, so buttons would look
    // like silent no-ops. Surface one notice per short window
    // (paywallNotice, defined above in this module, owns that decision),
    // mirroring the web's toast (main.tsx). NEVER the 401 path: a refused
    // write is not a lost session, so the token — and the readable ledger
    // behind it — must survive. The add-on's own endpoints are exempt
    // server-side, so this never talks over its card's error handling.
    if (r.status === 402) {
      const err = await parseError(r);
      const msg = paywallNotice(err.detail, method, lastPaywallNotice,
                                Date.now());
      if (msg) { lastPaywallNotice = Date.now(); onPaywall?.(msg); }
      throw err;
    }
  };
  const send = async <T>(path: string, init?: RequestInit): Promise<T> => {
    // every request binds to the CURRENT cache epoch: a sign-out or the
    // biometric lock aborts the epoch, so a response issued under the
    // previous account/session can never land in the cache (see query.ts)
    const r = await fetch(baseUrl + path, {
      ...init,
      signal: init?.signal ?? epochSignal(),
      headers: { Authorization: `Bearer ${token}`,
                 Accept: "application/json",
                 ...(init?.headers || {}) },
    });
    await checkAuth(r, init?.method ?? "GET");
    if (!r.ok) throw await parseError(r);
    return parseOkBody<T>(r.status, r.headers.get("content-type"),
                          await r.text());
  };
  // step-up: an inline passkey assertion mints a 5-minute single-use
  // ticket that the destructive endpoints redeem out of the same
  // recovery_code field they already take. Sent through `send`, never
  // `req` — the step-up doors are what answers a step-up refusal, so
  // routing them back through the retry would be circular.
  const stepupPasskeyOptions = () =>
    send<{ challenge_id: string; options: unknown }>(
      "/api/stepup/passkey/options", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: "{}" });
  const stepupPasskey = (challenge_id: string, credential: unknown) =>
    send<{ stepup_ticket: string }>("/api/stepup/passkey", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ challenge_id, credential }) });
  // The two elevation doors go through `send`, never `withElevation`:
  // they are what answers an elevation refusal, so routing them back
  // through the retry would be circular.
  const elevationStatus = () =>
    send<ElevationStatus>("/api/auth/elevation");
  const elevateWith = (proof: ElevationProof) =>
    send<ElevationStatus>("/api/auth/elevate", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(proof) });
  // Session elevation: a guarded route refuses an unelevated session with
  // 403 elevation_required instead of wanting a password in its body.
  // Raise the one sheet (controller above; the UI lives in the root
  // layout), then run the call again — once. A second refusal after a
  // successful proof is a real answer, and a closed sheet becomes the
  // cancel sentence the screens already know how to show.
  const withElevation = async <T>(run: () => Promise<T>): Promise<T> => {
    try {
      return await run();
    } catch (e) {
      if (!isElevationRequired(e)) throw e;
      try {
        await elevate(refusalWantsFresh(e));
      } catch (cancel) {
        throw new ApiError(403, cancel instanceof Error && cancel.message
          ? cancel.message : ELEVATION_CANCELLED,
          (e as ApiError).body);
      }
      return run();
    }
  };
  const req = <T>(path: string, init?: RequestInit): Promise<T> =>
    withElevation(async () => {
      try {
        return await send<T>(path, init);
      } catch (e) {
        // A passkey-only hosted account is refused on every destructive
        // door: the password alone is not a second factor there. Offer
        // the passkey — the proof the account is actually built on —
        // before the person spends one of ten recovery codes on a posture
        // change. The web does the same (webapp/src/stepup.ts); a
        // dismissed sheet, a build without the module or a self-host
        // server all fall through to the original refusal, which is what
        // makes the card's recovery-code field appear.
        if (!(e instanceof ApiError) || !/recovery_required/.test(e.detail))
          throw e;
        const { stepupRetry } = await import("./stepup");
        const retry = await stepupRetry(baseUrl, init, stepupPasskeyOptions,
                                        stepupPasskey);
        if (!retry) throw e;
        return send<T>(path, retry);
      }
    });
  return {
    stepupPasskeyOptions,
    stepupPasskey,
    elevationStatus,
    elevateWith,
    // the instance this client talks to — image URLs (merchant logos via
    // /api/logo) are built from it
    baseUrl,
    // the same bearer every request carries, for the places that build a
    // URL instead of calling req: <Image source={{ uri, headers }}> has no
    // cookie jar to fall back on, so an authenticated image needs it here
    authHeader: { Authorization: `Bearer ${token}` } as Record<string, string>,
    // the raw authenticated GET/POST — for one-off reads (the More
    // page's feedback_enabled peek) that don't warrant a named method
    req,
    // authenticated file download → the OS share sheet (a browser tab
    // can't carry the bearer token)
    download: async (path: string, filename: string) => {
      const FileSystem = await import("expo-file-system/legacy");
      const Sharing = await import("expo-sharing");
      // The export is the household's whole ledger, gated behind fresh
      // elevation on the way in — it must not linger unencrypted in the
      // app cache. But it cannot be deleted the moment the share
      // sheet resolves either: on Android the chooser hands off before
      // the target app (Drive, Gmail, Files) has read the content:// URI,
      // so an immediate unlink truncates the very share it followed.
      // Instead, every download first sweeps the PREVIOUS ones that have
      // had ten minutes to be consumed.
      const dir = FileSystem.cacheDirectory!;
      try {
        const names = await FileSystem.readDirectoryAsync(dir);
        const cutoff = Date.now() / 1000 - 600;
        for (const n of names) {
          // every kind of file this client ever downloads here — the
          // whole-database dump (.sql.gz), business CSVs, the year-end
          // ZIP, the compliance calendar, receipt images — whatever it
          // was named; a prefix match leaves most of them cached for good
          if (!/\.(zip|csv|sql|gz|pdf|ics|jpe?g|png|webp)$/i.test(n)) continue;
          const info = await FileSystem.getInfoAsync(dir + n);
          if (info.exists && (info.modificationTime ?? 0) < cutoff)
            await FileSystem.deleteAsync(dir + n, { idempotent: true });
        }
      } catch { /* a cache we cannot list is not a reason to refuse */ }
      const dest = dir + filename;
      const r = await FileSystem.downloadAsync(baseUrl + path, dest,
        { headers: { Authorization: `Bearer ${token}` } });
      if (r.status !== 200)
        throw new Error(`${r.status}: download failed`);
      await Sharing.shareAsync(r.uri);
    },
    me: () => req<Me>("/api/me"),
    todayFull: (date?: string) =>
      req<TodayFull>("/api/today/full"
        + (date ? `?date=${encodeURIComponent(date)}` : "")),
    onboarding: () =>
      req<{ accounts: number; transactions: number; bills: number;
            connections: number; pending_proposals: number;
            budgets_set: boolean; primary_checking_set: boolean;
            wizard_done: boolean;
            // per-step done/skipped marks — where the wizard resumes
            wizard_steps: Record<string, string> }>("/api/onboarding"),
    // where each bank connection stands in its one-time history backfill;
    // polling it also nudges the server to keep pulling (same door the
    // web's wizard step 2 waits on)
    onboardingBackfill: () =>
      req<{ items: { id: string; institution: string | null;
                     transactions: number; earliest: string | null;
                     // 0..1 of the two-year window landed; 1 only once
                     // the bank says the window is complete
                     progress: number;
                     // a pull touched this connection in the last 90s
                     active: boolean;
                     ready: boolean; stalled: boolean }[];
            all_ready: boolean; syncing: boolean }>(
        "/api/onboarding/backfill"),
    // the GET above is read-only (viewers may poll it); starting a pull
    // when a backfill has gone quiet is this owner-only POST — the wizard
    // fires it once when its gate mounts, and ignores a viewer's 403
    onboardingBackfillNudge: () =>
      req<{ all_ready: boolean; syncing: boolean }>(
        "/api/onboarding/backfill/nudge", { method: "POST",
          headers: { "Content-Type": "application/json" }, body: "{}" }),
    // a one-shot ticket the export download redeems within a minute. The
    // door wants FRESH elevation (the whole household's data leaves in one
    // file), so the sheet shows even inside an elevated window.
    exportTicket: (kind: "zip" | "dump") =>
      req<{ ok: boolean; token: string; url: string; expires_in: number }>(
        "/api/export/token", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ kind }) }),
    transactions: (params: TxnQuery = {}) =>
      req<TxnPage>("/api/transactions?" + new URLSearchParams(
        Object.entries(params)
          .filter(([, v]) => v !== undefined) as [string, string][])),
    accounts: () => req<{ accounts: Account[] }>("/api/accounts"),
    bills: () => req<BillsData>("/api/bills"),
    reportNetworth: () => req<NetWorthReport>("/api/reports/networth"),
    reportFees: () => req<FeesReport>("/api/reports/fees"),
    reportSpending: () => req<SpendingReport>("/api/reports/spending"),
    reportCashflow: () => req<CashflowReport>("/api/reports/cashflow"),
    reportCashflowFlow: (range: FlowKey) =>
      req<FlowPeriod>(`/api/reports/cashflow/flow?range=${range}`),
    reportSpendingWindow: (range: FlowKey) =>
      req<SpendingWindow>(`/api/reports/spending/window?range=${range}`),
    reportIncomeWindow: (range: FlowKey) =>
      req<IncomeWindow>(`/api/reports/income/window?range=${range}`),
    alertsHistory: () => req<{ alerts: AlertRow[] }>("/api/alerts/history"),
    alertDismiss: (kind: string, message: string) =>
      req<{ ok: boolean }>("/api/alerts/dismiss", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind, message }),
      }),
    alertRestore: (kind: string, message: string) =>
      req<{ ok: boolean }>("/api/alerts/restore", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind, message }),
      }),
    devices: () => req<{ devices: MobileDevice[] }>("/api/devices"),
    // ---- security self-service ----
    // Every posture change below is elevation-gated server-side: the
    // session proves itself once in the sheet and these calls carry no
    // password of their own. The current password on a CHANGE is the
    // change's own input, not re-auth.
    passwordChange: (current_password: string, new_password: string) =>
      // the server reports what the rotation removed, so a phone or
      // passkey that stops working right after is explained, not a mystery
      req<{ ok: boolean; passkeys_removed?: number; passkeys_kept?: number;
            devices_revoked?: number;
            tokens_revoked?: number; invites_removed?: number }>(
        "/api/password/change", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ current_password, new_password }),
      }),
    emailChange: (new_email: string) =>
      req<{ ok: boolean; email: string; verify_sent?: boolean;
            hint?: string | null; passkeys_removed?: number;
            passkeys_kept?: number }>(
        "/api/email/change", {
        method: "POST",
        headers: { "Content-Type":
                     "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ new_email }).toString(),
      }),
    totpEnroll: () =>
      req<{ secret: string; qr: string }>("/api/totp/enroll", {
        method: "POST",
        headers: { "Content-Type":
                     "application/x-www-form-urlencoded" },
        body: "",
      }),
    totpConfirm: (secret: string, code: string) =>
      req<{ ok: boolean; recovery_codes: string[] | null }>(
        "/api/totp/confirm", {
          method: "POST",
          headers: { "Content-Type":
                       "application/x-www-form-urlencoded" },
          body: new URLSearchParams({ secret, code }).toString(),
        }),
    // the live code is the route's own input (it proves the authenticator
    // being switched off is still in hand), kept beside elevation
    totpDisable: (code: string) =>
      req<{ ok: boolean }>("/api/totp/disable", {
        method: "POST",
        headers: { "Content-Type":
                     "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ code }).toString(),
      }),
    recoveryRegenerate: () =>
      req<{ ok: boolean; recovery_codes: string[] }>(
        "/api/totp/recovery-regenerate", {
          method: "POST",
          headers: { "Content-Type":
                       "application/x-www-form-urlencoded" },
          body: "",
        }),
    sessions: () =>
      req<{ sessions: { id: string; created_at: string;
            last_seen: string | null; expires_at: string;
            user_agent: string; ip: string | null;
            current: boolean }[] }>("/api/sessions"),
    // signing another session out is a posture change — elevation-gated
    sessionRevoke: (body: { all_others?: boolean; id?: string }) =>
      req<{ ok: boolean; revoked: number }>("/api/sessions/revoke", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    deviceRevoke: (body: { id: string }) =>
      req<{ ok: boolean; revoked: number }>("/api/devices/revoke", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    // a household member removes their OWN login (owner + data untouched)
    accountLeave: () =>
      req<{ ok: boolean }>("/api/account/leave", {
        method: "POST",
        headers: { "Content-Type":
                     "application/x-www-form-urlencoded" },
        body: "",
      }),
    // this login's own daily-email delivery (viewers may)
    meEmail: (muted: boolean) =>
      req<{ ok: boolean; muted: boolean }>("/api/me/email", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ muted }) }),
    // wants FRESH elevation: the sheet shows even inside a live window
    accountDelete: () =>
      req<{ ok: boolean }>("/api/account/delete", {
        method: "POST",
        headers: { "Content-Type":
                     "application/x-www-form-urlencoded" },
        body: "",
      }),
    supportAccess: () =>
      req<{ granted: boolean; expires_at: string | null;
            reason: string | null }>("/api/support-access"),
    supportAccessGrant: (hours: number, reason: string) =>
      req<{ ok: boolean }>("/api/support-access", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ hours, reason }),
      }),
    supportAccessRevoke: () =>
      req<{ ok: boolean }>("/api/support-access/revoke", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }),
    // ---- script tokens (self-host collectors) ----
    tokens: () =>
      req<{ tokens: { id: string; name: string; created_at: string;
            last_used_at: string | null;
            revoked_at: string | null }[] }>("/api/tokens"),
    tokenMint: (name: string) =>
      req<{ id: string; name: string; token: string }>("/api/tokens", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      }),
    tokenRevoke: (id: string) =>
      req<{ ok: boolean }>("/api/tokens/revoke", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id }),
      }),
    // passphrase-sealed credentials bundle (.oikx) — survives a fresh
    // install: banks stay linked, email/LLM stay wired. Hand-rolled fetch
    // (a blob comes back), so the elevation retry wraps it by hand too —
    // this door wants FRESH proof: every credential the instance holds
    // leaves in one file.
    connectionsExport: async (passphrase: string) => {
      const r = await withElevation(async () => {
        const r0 = await fetch(baseUrl + "/api/connections/export", {
          method: "POST", signal: epochSignal(),
          headers: { Authorization: `Bearer ${token}`,
                     "Content-Type": "application/json" },
          body: JSON.stringify({ passphrase }),
        });
        await checkAuth(r0, "POST");
        if (!r0.ok) throw await parseError(r0);
        return r0;
      });
      const blob = await r.blob();
      const b64: string = await new Promise((res, rej) => {
        const fr = new FileReader();
        fr.onload = () => res(String(fr.result).split(",")[1] ?? "");
        fr.onerror = rej;
        fr.readAsDataURL(blob);
      });
      const FileSystem = await import("expo-file-system/legacy");
      const Sharing = await import("expo-sharing");
      const cd = r.headers.get("content-disposition") ?? "";
      const m0 = /filename="?([^";]+)/.exec(cd);
      const dest = FileSystem.cacheDirectory
        + (m0?.[1] ?? "oikonome-config.oikx");
      await FileSystem.writeAsStringAsync(dest, b64,
        { encoding: "base64" });
      await Sharing.shareAsync(dest);
    },
    connectionsImport: async (file: PickedFile, passphrase: string) => {
      const fd = new FormData();
      fd.append("passphrase", passphrase);
      fd.append("file", { uri: file.uri, name: file.name,
        type: "application/octet-stream" } as unknown as Blob);
      return withElevation(async () => {
        const r = await fetch(baseUrl + "/api/connections/import",
          { method: "POST", body: fd, signal: epochSignal(),
            headers: { Authorization: `Bearer ${token}` } });
        await checkAuth(r, "POST");
        if (!r.ok) throw await parseError(r);
        return parseOkBody<{ ok: boolean; config: string[];
                             items: number }>(
          r.status, r.headers.get("content-type"), await r.text());
      });
    },
    recipientInviteResend: (email: string) =>
      req<{ ok: boolean }>("/api/settings/recipients/resend", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      }),
    smtpTest: () =>
      req<{ ok: boolean; sent_to: string[];
            skipped?: { email: string; reason: string }[];
            failed: { email: string; error: string }[]; via: string }>(
        "/api/settings/smtp-test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        }),
    verifyResend: () =>
      req<{ ok: boolean; verified: boolean }>(
        "/api/verify-email/resend", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        }),
    // ---- household members + invites ----
    members: () =>
      req<{ users: { id: string; email: string; role: string;
            created_at: string; me: boolean }[];
            assignable_roles: string[] }>("/api/users"),
    // move somebody between "member" (can edit the household's money) and
    // "viewer" (reads everything, changes nothing). Elevation-gated, like
    // every other durable grant.
    memberSetRole: (id: string, role: string) =>
      req<{ ok: boolean; role: string }>(
        `/api/users/${encodeURIComponent(id)}/role`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ role }),
        }),
    memberRemove: (id: string) =>
      req<{ ok: boolean }>(`/api/users/${encodeURIComponent(id)}`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }),
    invites: () =>
      req<{ invites: { token_hash: string; role: string; label: string;
            created_at: string; expires_at: string }[] }>("/api/invites"),
    // `url` is null on hosted: the server mails the claim link to the
    // address on the invite and keeps it out of the reply, because holding
    // a claimable link is the mailbox proof the claim door runs on. Render
    // the outcome through `inviteOutcome`, never the URL alone.
    inviteCreate: (label: string, role = "viewer") =>
      req<{ url: string | null; role: string; label: string;
            expires_at: string; emailed: boolean }>("/api/invites", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label, role }),
      }),
    inviteRevoke: (tokenHash: string) =>
      req<{ ok: boolean }>(
        `/api/invites/${encodeURIComponent(tokenHash)}`,
        { method: "DELETE" }),
    revokeSelf: (id: string) =>
      req<{ ok: boolean }>("/api/devices/revoke", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id }),
      }),
    registerPush: (token: string, platform: "fcm" | "apns") =>
      req<{ ok: boolean }>("/api/devices/push", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, platform }),
      }),
    txnBulk: (ids: string[], action: string, category?: string) =>
      req<{ ok: boolean; applied: number; missing: number; flow: number }>(
        "/api/transactions/bulk", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ids, action, category }),
        }),
    categories: () =>
      req<{ categories: string[]; plaid_spend: string[];
            plaid_flow: string[] }>("/api/categories"),
    // scope "all" also teaches the merchant (a Rules-page rule); the
    // picker asks explicitly, same as the web
    setCategory: (id: string, category: string, scope: "one" | "all") =>
      req<{ ok: boolean; scope: string; rule_written: boolean }>(
        `/api/transactions/${encodeURIComponent(id)}/category`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ category, scope }),
        }),
    reimbFlag: (id: string,
                opts: { partial?: boolean;
                        expected?: number | null } = {}) =>
      req<{ ok: boolean }>(`/api/reimburse/${encodeURIComponent(id)}/flag`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(opts),
      }),
    reimbUnflag: (id: string) =>
      req<{ ok: boolean }>(
        `/api/reimburse/${encodeURIComponent(id)}/unflag`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        }),
    // every pending proposal in one click (the wizard's bills step)
    billsApproveAll: () =>
      req<{ ok: boolean; approved: number; payees: string[];
            failed: { pid: string; error: string }[] }>(
        "/api/bills/proposals/approve-all", { method: "POST" }),
    billsProposal: (pid: string, action: "approve" | "reject") =>
      req<{ ok: boolean; payee: string }>("/api/bills/proposal", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pid, action }),
      }),
    lensMonth: (y: number, m: number) =>
      req<MonthLensData>(`/api/lens/month?y=${y}&m=${m}`),
    lensYear: (y: number) => req<YearLensData>(`/api/lens/year?y=${y}`),
    // BYO provider keys (self-host): validated live before saving,
    // clearable — the web ConnectHub's endpoints, verbatim
    plaidValidate: (client_id: string, secret: string, env: string) =>
      req<{ ok: boolean; env: string }>("/api/accounts/plaid/validate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ client_id, secret, env }),
      }),
    plaidKeys: (client_id: string, secret: string, env: string) =>
      req<{ ok: boolean; env: string }>("/api/accounts/plaid/keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ client_id, secret, env }),
      }),
    plaidKeysClear: () =>
      req<{ ok: boolean }>("/api/accounts/plaid/keys",
                           { method: "DELETE" }),
    mxKeys: (client_id: string, api_key: string, env: string) =>
      req<{ ok: boolean; env: string }>("/api/accounts/mx/keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ client_id, api_key, env }),
      }),
    mxKeysClear: () =>
      req<{ ok: boolean }>("/api/accounts/mx/keys", { method: "DELETE" }),
    mxConnect: () =>
      req<{ url: string }>("/api/accounts/mx/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }),
    mxSyncNow: () =>
      req<{ ok: boolean; transactions: number }>("/api/accounts/mx/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }),
    budgetSnapshots: () => req<BudgetSnapshots>("/api/budget/snapshots"),
    budgetBackfill: () =>
      req<{ backfilled: number }>("/api/budget/snapshots/backfill",
                                  { method: "POST" }),
    reimbPending: () =>
      req<{ pending: PendingReimb[] }>("/api/reimburse/pending"),
    reimbCandidates: (id: string, q = "") =>
      req<{ candidates: ReimbCandidate[] }>(
        `/api/reimburse/${encodeURIComponent(id)}/candidates` +
        (q ? `?q=${encodeURIComponent(q)}` : "")),
    reimbLink: (txn_id: string, other_ids: string[], partial = false) =>
      req<{ linked: number; errors: string[] }>("/api/reimburse/link", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ txn_id, other_ids, partial }),
      }),
    reimbPairs: () => req<{ pairs: ReimbPair[] }>("/api/reimburse/pairs"),
    reimbUnlink: (expense_id: string, reimburse_id: string) =>
      req<{ ok: boolean }>("/api/reimburse/unlink", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expense_id, reimburse_id }),
      }),
    // Merchants: what the ledger calls each payee, the raw strings under
    // it, and the journal of user-made changes so each one is undoable.
    // `order` is the screen's sort toggle, honored server-side so the
    // 200-row window is the top of the order asked for, not a re-sorted
    // slice of some other one
    merchantCatalog: (q = "", order: "count" | "alpha" = "count") =>
      req<MerchantCatalog>("/api/merchants/catalog?q=" + encodeURIComponent(q)
                           + "&order=" + order),
    merchantDetail: (display: string) =>
      req<MerchantDetail>("/api/merchants/detail?display="
                          + encodeURIComponent(display)),
    // rename and merge are one call: naming an existing merchant merges
    merchantRename: (display: string, to: string) =>
      req<{ raws: number; rows: number }>("/api/merchants/rename", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display, to }),
      }),
    // approve = merge through the journalled rename (undo works); reject =
    // keep apart, never offered again
    merchantMerge: (pid: string, action: "approve" | "reject") =>
      req<{ ok: boolean }>("/api/merchants/merge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pid, action }),
      }),
    merchantUndo: (id: number) =>
      req<{ raw_merchant: string; restored: string | null }>(
        "/api/merchants/undo", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id }),
        }),
    rules: (source: "user" | "llm" | "seed" | "model", q = "", page = 1) =>
      req<RulesPage>(`/api/rules?source=${source}&q=${
        encodeURIComponent(q)}&page=${page}`),
    ruleDisable: (merchant: string, disabled: boolean) =>
      req<{ ok: boolean }>("/api/rules/disable", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ merchant, disabled }),
      }),
    ruleDelete: (merchant: string) =>
      req<{ ok: boolean; deleted: number }>("/api/rules/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ merchant }),
      }),
    // rename a free-form custom category everywhere it appears
    categoryRename: (oldName: string, newName: string) =>
      req<{ old: string; new: string; overrides: number;
            primaries: number; rules: number; buckets: number }>(
        "/api/categories/rename", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ old: oldName, new: newName }),
        }),
    ruleSet: (merchant: string, category: string) =>
      req<{ count: number }>("/api/rules/set", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ merchant, category }),
      }),
    retirement: (params: Record<string, string> = {}) =>
      req<RetirementData>("/api/retirement"
        + (Object.keys(params).length
          ? "?" + new URLSearchParams(params) : "")),
    business: (year?: number, limit?: number) => {
      const p = new URLSearchParams();
      if (year) p.set("year", String(year));
      if (limit) p.set("limit", String(limit));
      const qs = p.toString();
      return req<BizData>("/api/business" + (qs ? `?${qs}` : ""));
    },
    businessSummary: () =>
      req<{ entities: Entity[]; flagged_unassigned: number;
            combined: boolean }>("/api/business/summary"),
    businessCombine: (on: boolean) =>
      req<{ combined: boolean }>("/api/business/combine", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ on }),
      }),
    entityImportFlags: (id: string) =>
      req<{ ok: boolean; moved: number }>(
        `/api/business/entities/${encodeURIComponent(id)}/import-flags`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        }),
    // the reply is the member row itself, so a screen can append it
    entityAddMember: (id: string, body: { member_name: string;
                       ownership_pct?: number }) =>
      req<NonNullable<Entity["members"]>[number]>(
        `/api/business/entities/${encodeURIComponent(id)}/members`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
    entityGet: (id: string) =>
      req<Entity>(`/api/business/entities/${encodeURIComponent(id)}`),
    entityPnl: (id: string, year?: number) =>
      req<Pnl>(`/api/business/entities/${encodeURIComponent(id)}/pnl`
        + (year ? `?year=${year}` : "")),
    bizTxns: (id: string, year?: number) =>
      req<{ transactions: BizTxn[] }>(
        `/api/business/entities/${encodeURIComponent(id)}/transactions`
        + (year ? `?year=${year}` : "")),
    balanceSheet: (id: string) =>
      req<{ assets: { name: string; amount: number }[];
            liabilities: { name: string; amount: number }[];
            total_assets: number; total_liabilities: number; net: number;
            capital_account: number }>(
        `/api/business/entities/${encodeURIComponent(id)}/balance-sheet`),
    entityEquity: (id: string) =>
      req<{ movements: { id: string; kind: string; amount: number;
              date: string | null; form: string | null }[];
            capital: Capital }>(
        `/api/business/entities/${encodeURIComponent(id)}/equity`),
    equityRecord: (id: string, body: { kind: string; amount: number;
                     date: string; form?: string }) =>
      req<unknown>(
        `/api/business/entities/${encodeURIComponent(id)}/equity`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
    equityDelete: (id: string, movementId: string) =>
      req<{ ok: boolean }>(
        `/api/business/entities/${encodeURIComponent(id)}/equity/${
          encodeURIComponent(movementId)}`, { method: "DELETE" }),
    mileageList: (id: string) =>
      req<{ trips: MileageTrip[];
            summary: { year?: number; miles: number; rate: number;
                       rates?: { rate: number; miles: number }[];
                       deduction: number } }>(
        `/api/business/entities/${encodeURIComponent(id)}/mileage`),
    mileageAdd: (id: string, body: { date: string; miles: number;
                   purpose?: string }) =>
      req<{ ok: boolean }>(
        `/api/business/entities/${encodeURIComponent(id)}/mileage`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
    mileageDelete: (id: string, tripId: string) =>
      req<{ ok: boolean }>(
        `/api/business/entities/${encodeURIComponent(id)}/mileage/${
          encodeURIComponent(tripId)}`, { method: "DELETE" }),
    vendorList: (id: string) =>
      req<{ vendors: Vendor[] }>(
        `/api/business/entities/${encodeURIComponent(id)}/vendors`),
    vendorMark: (id: string, body: { merchant: string;
                   reportable: boolean }) =>
      req<{ ok: boolean }>(
        `/api/business/entities/${encodeURIComponent(id)}/vendors`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
    entityCompliance: (id: string) =>
      req<{ obligations: Obligation[] }>(
        `/api/business/entities/${encodeURIComponent(id)}/compliance`),
    complianceAdd: (id: string, body: { title: string; due_date: string;
                      recurrence?: string }) =>
      req<{ id: string }>(
        `/api/business/entities/${encodeURIComponent(id)}/compliance`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
    complianceDelete: (id: string, oblId: string) =>
      req<{ ok: boolean }>(
        `/api/business/entities/${encodeURIComponent(id)}/compliance/${
          encodeURIComponent(oblId)}`, { method: "DELETE" }),
    estTax: (id: string, year?: number) =>
      req<EstimatedTax>(
        `/api/business/entities/${encodeURIComponent(id)}/estimated-tax`
        + (year ? `?year=${year}` : "")),
    receiptReport: (tag: string, y: number, m: number) =>
      req<{ tag: string; total: number; count: number;
            rows: { date: string; payee: string; description: string;
                    qty: number | null; amount: number;
                    receipt_id: string; txn_id: string }[] }>(
        `/api/receipts/report?tag=${encodeURIComponent(tag)}&y=${y}&m=${m}`),
    bizSuggestions: (id: string) =>
      req<{ lines: string[]; suggestions: {
        txn_id: string; date: string | null; amount: number; payee: string;
        suggested_line: string | null;
        suggested_bucket: string }[]; total?: number }>(
        `/api/business/entities/${encodeURIComponent(id)}/suggestions`),
    bizClassify: (id: string, txnId: string,
                  body: { bucket: string; sched_c_line?: string }) =>
      req<{ ok: boolean }>(
        `/api/business/entities/${encodeURIComponent(id)}/transactions/${
          encodeURIComponent(txnId)}/class`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
    importBatches: () => req<{ batches: Batch[] }>("/api/import/batches"),
    importRollback: (batch_id: string) =>
      req<{ ok: boolean; deleted: number; warning: string | null }>(
        "/api/import/rollback", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ batch_id }),
        }),
    bulkAnalyze: async (files: PickedFile[]) => {
      const fd = new FormData();
      for (const f of files)
        fd.append("files", { uri: f.uri, name: f.name,
          type: f.mime || "application/octet-stream" } as unknown as Blob);
      const r = await fetch(baseUrl + "/api/import/bulk/analyze",
        { method: "POST", body: fd, signal: epochSignal(),
          headers: { Authorization: `Bearer ${token}` } });
      await checkAuth(r, "POST");
      if (!r.ok) throw await parseError(r);
      return parseOkBody<{ token: string; files: PlanFile[] }>(
        r.status, r.headers.get("content-type"), await r.text());
    },
    restoreProgress: () => req<RestoreProgress>("/api/restore/progress"),
    bulkRun: (planToken: string, files: PlanFile[]) =>
      req<{ results: { index: number; name: string; ok: boolean;
                       imported?: number; error?: string;
                       // an export ZIP restores in the background
                       restore_started?: boolean }[] }>(
        "/api/import/bulk/run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token: planToken, files }),
        }),
    importFile: async (f: PickedFile, accountId: string,
                       amountSign: string, newAccountName?: string) => {
      const fd = new FormData();
      fd.append("file", { uri: f.uri, name: f.name,
        type: f.mime || "application/octet-stream" } as unknown as Blob);
      fd.append("account_id", accountId);
      fd.append("amount_sign", amountSign);
      if (newAccountName) fd.append("new_account_name", newAccountName);
      const r = await fetch(baseUrl + "/api/import",
        { method: "POST", body: fd, signal: epochSignal(),
          headers: { Authorization: `Bearer ${token}` } });
      await checkAuth(r, "POST");
      if (!r.ok) throw await parseError(r);
      return parseOkBody<ImportOutcome>(
        r.status, r.headers.get("content-type"), await r.text());
    },
    importMapped: (mapToken: string, cols: Record<string, string>) =>
      req<{ result: ImportResult }>("/api/import/mapped", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: mapToken, ...cols }),
      }),
    taxdocAnalyze: async (f: PickedFile) => {
      const fd = new FormData();
      fd.append("file", { uri: f.uri, name: f.name,
        type: f.mime || "application/octet-stream" } as unknown as Blob);
      const r = await fetch(baseUrl + "/api/import/taxdoc/analyze",
        { method: "POST", body: fd, signal: epochSignal(),
          headers: { Authorization: `Bearer ${token}` } });
      await checkAuth(r, "POST");
      if (!r.ok) throw await parseError(r);
      return parseOkBody<TaxdocPlan>(
        r.status, r.headers.get("content-type"), await r.text());
    },
    taxdocCommit: (planToken: string, rows: TaxdocRow[]) =>
      req<{ kind: string; updated: number; created: number;
            documents: number }>("/api/import/taxdoc/commit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: planToken, rows }),
      }),
    syncStart: () =>
      req<{ ok: boolean; started: boolean; reason?: string }>(
        "/api/jobs/sync/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        }),
    syncStatus: () => req<SyncStatus>("/api/jobs/sync/status"),
    connections: () =>
      req<{ connections: Connection[]; last_sync: string | null;
            syncing: boolean;
            sync_progress: { done: number; total: number;
                             phase?: string | null } | null }>(
        "/api/connections"),
    holdings: () => req<{ holdings: Holding[] }>("/api/holdings"),
    accountRemove: (accountId: string, mode: AccountRemoveMode) =>
      req<{ ok: boolean; mode: string; plaid_released: boolean;
            note: string; affected_accounts: string[] }>(
        `/api/accounts/${encodeURIComponent(accountId)}/remove`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode }),
        }),
    connectionDisconnect: (id: string) =>
      req<{ ok: boolean; plaid_released: boolean; note: string }>(
        `/api/connections/${encodeURIComponent(id)}/disconnect`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        }),
    // per-institution sync — a form door that 303s; the message rides
    // the final URL's ?msg= (RN fetch follows redirects and exposes
    // response.url)
    accountSyncItem: async (itemId: string) => {
      const r = await fetch(baseUrl + "/accounts/sync", {
        method: "POST", signal: epochSignal(),
        headers: { Authorization: `Bearer ${token}`,
                   "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ item_id: itemId }).toString(),
      });
      await checkAuth(r, "POST");
      let msg: string | null = null;
      try {
        msg = new URL(r.url).searchParams.get("msg");
      } catch { /* keep the fallback message */ }
      return { ok: r.ok, message: msg ?? (r.ok ? "synced" : "sync failed") };
    },
    // Plaid Hosted Link — the app polls; there is no redirect back
    plaidLinkStart: (itemId = "", manageAccounts = false) =>
      req<{ link_token: string; hosted_link_url: string;
            kind: "add" | "update"; institution: string | null }>(
        "/accounts/plaid/link", {
          method: "POST",
          headers: { "Content-Type":
                       "application/x-www-form-urlencoded" },
          body: new URLSearchParams({
            ...(itemId ? { item_id: itemId } : {}),
            ...(manageAccounts ? { manage_accounts: "1" } : {}),
          }).toString(),
        }),
    plaidLinkStatus: (linkToken: string) =>
      req<{ done: boolean;
            kind?: "add" | "add_failed" | "update" | "update_failed" | "busy"
                 | "exited";
            institution?: string; error?: string }>(
        `/accounts/plaid/link/${encodeURIComponent(linkToken)}/status`),
    simplefinConnect: (sfToken: string) =>
      req<{ ok: boolean; accounts: number; transactions: number }>(
        "/api/accounts/simplefin", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token: sfToken }),
        }),
    accountLinks: () =>
      req<{ groups: { group_id: string;
              members: { account_id: string; home_rank: number;
                         healthy: boolean; primary: boolean }[] }[];
            suggestions: { key: string;
              a: { id: string; name: string; mask: string | null;
                   institution_name: string | null };
              b: { id: string; name: string; mask: string | null;
                   institution_name: string | null } }[];
            card_dismissed: boolean }>(
        "/api/accounts/links"),
    accountLinkCreate: (accountIds: string[]) =>
      req<{ group_id: string }>("/api/accounts/links", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_ids: accountIds,
                               auto_order: true }),
      }),
    accountLinkDelete: (groupId: string) =>
      req<{ ok: boolean }>(
        `/api/accounts/links/${encodeURIComponent(groupId)}`,
        { method: "DELETE" }),
    accountLinkOrder: (groupId: string, accountIds: string[]) =>
      req<{ ok: boolean }>(
        `/api/accounts/links/${encodeURIComponent(groupId)}/order`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ account_ids: accountIds }),
        }),
    accountLinkDismiss: (key: string) =>
      req<{ ok: boolean }>("/api/accounts/links/dismiss", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key }),
      }),
    // put the standing linked-sources card away (or bring it back)
    accountLinkCard: (dismissed: boolean) =>
      req<{ ok: boolean }>("/api/accounts/links/dismiss", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ card: dismissed }),
      }),
    accountAdd: (name: string, kind: string, balance = "") =>
      req<{ ok: boolean; account_id: string }>("/api/accounts/add", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, kind, balance }),
      }),
    accountAssignEntity: (accountId: string, entityId: string | null) =>
      req<{ ok: boolean }>(
        `/api/accounts/${encodeURIComponent(accountId)}/entity`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ entity_id: entityId }),
        }),
    txnAssignEntity: (txnId: string, entityId: string | null) =>
      req<{ ok: boolean }>(
        `/api/transactions/${encodeURIComponent(txnId)}/entity`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ entity_id: entityId }),
        }),
    accountBalance: (account_id: string, balance: string) =>
      req<{ ok: boolean }>("/api/accounts/balance", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_id, balance }),
      }),
    accountRename: (account_id: string, name: string) =>
      req<unknown>("/api/accounts/rename", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_id, name }),
      }),
    accountClassify: (account_id: string, kind: string,
                      primary_checking: boolean) =>
      req<unknown>("/api/accounts/classify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_id, kind, primary_checking }),
      }),
    accountExclude: (account_id: string, excluded: boolean) =>
      req<{ ok: boolean }>("/api/accounts/exclude", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_id, excluded }),
      }),
    accountSetHidden: (account_id: string, hidden: boolean) =>
      req<{ ok: boolean; note: string }>(
        `/api/accounts/${encodeURIComponent(account_id)}/hidden`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ hidden }),
        }),
    accountOwner: (account_id: string, owner: string) =>
      req<unknown>(
        `/api/accounts/${encodeURIComponent(account_id)}/owner`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ owner }),
        }),
    owners: () => req<{ owners: string[] }>("/api/owners"),
    // the slice of /api/settings the Accounts editor needs: which account
    // anchors the cash forecast, and which are excluded from budget math
    settingsAccounts: () =>
      req<{ checking_account_id: string | null;
            excluded_accounts: string[] | null }>("/api/settings")
        .then((s0) => ({
          checking_account_id: s0.checking_account_id ?? null,
          excluded_accounts: s0.excluded_accounts ?? [],
        })),
    // distinct ledger merchants, most-frequent first — the add-a-bill
    // picker, so a manual bill's match key lines up with real payees
    merchants: () =>
      req<{ merchants: string[] }>("/api/merchants"),
    billsBill: (payee: string) =>
      req<BillDetail>(`/api/bills/bill?payee=${encodeURIComponent(payee)}`),
    billsSave: (body: {
      payee: string; amount: string | number; cadence?: string;
      next_due?: string; income?: boolean; orig_payee?: string;
      cap?: string | number | null; category?: string | null;
      show_today?: boolean; match_category?: boolean;
      txn_category?: string; companions?: Companion[];
      merchants?: string[] }) =>
      req<{ ok: boolean; payee: string }>("/api/bills/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    // `disabled` names the target state; omitted = flip. The queue's
    // Disable/Undo send it so a duplicated request is idempotent (web too)
    billsToggle: (payee: string, disabled?: boolean) =>
      req<{ ok: boolean; disabled: boolean }>("/api/bills/toggle", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(disabled === undefined ? { payee }
                                                    : { payee, disabled }),
      }),
    billsDelete: (payee: string, mode: "archive" | "purge" = "archive") =>
      req<{ ok: boolean; mode: string }>("/api/bills/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ payee, mode }),
      }),
    billsRestore: (payee: string) =>
      req<{ ok: boolean }>("/api/bills/restore", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ payee }),
      }),
    billsDetect: () =>
      req<{ proposed_add: number; proposed_income?: number;
            drift_applied?: number }>("/api/bills/detect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }),
    billsHistory: (payee: string) =>
      req<BillsHistoryData>(
        `/api/bills/history?payee=${encodeURIComponent(payee)}`),
    billsHint: (payee: string, action: "dismiss" | "restore" = "dismiss",
                target: "fit" | "health" = "fit") =>
      req<{ ok: boolean; dismissed: boolean }>("/api/bills/hint", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ payee, action, target }),
      }),
    // bulk category override for a whole (canonical) merchant —
    // preview → confirm-with-count → apply → undo, like the web.
    // `family: false` sends `?canon_only=1` (the server's flag), scoping the preview to the single
    // canonical merchant with no family expansion — the scope the txn
    // screen's "All <payee>" write uses. The query-arg name is the server
    // contract; the two must match. Default (family scope) is what the
    // bill-history "Apply to whole merchant" previews.
    merchantCategoryPreview: (payee: string, category: string,
                              opts?: { family?: boolean }) =>
      req<{ merchant: string; count: number; variants?: string[];
            flow_rows?: number; skipped_override?: number }>(
        "/api/bills/merchant-category/preview"
        + (opts?.family === false ? "?canon_only=1" : ""), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ payee, category }),
        }),
    merchantCategorySet: (payee: string, category: string) =>
      req<{ merchant: string; count: number; undo: unknown }>(
        "/api/bills/merchant-category", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ payee, category }),
        }),
    merchantCategoryUndo: (undo: unknown) =>
      req<{ ok: boolean; restored: number }>(
        "/api/bills/merchant-category/undo", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ undo }),
        }),
    setTxnNote: (id: string, note: string) =>
      req<{ ok: boolean; note: string }>(
        `/api/transactions/${encodeURIComponent(id)}/note`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ note }),
        }),
    // An authorization the bank left pending and never posted or
    // released. Sync can't drop it — an absent row is indistinguishable
    // from one the feed simply didn't return — so only the user can
    // retire it. The server refuses a row that is not pending or is
    // younger than two weeks; its detail text is what the caller shows.
    retirePending: (id: string) =>
      req<{ ok: boolean; id: string }>(
        "/api/transactions/retire-pending", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id }),
        }),
    // capitalize / reimburse a personally-paid business expense — assign
    // it to the entity and record the equity movement
    equityReimburse: (entityId: string, txnId: string,
                      mode: "contribute" | "reimburse") =>
      req<{ ok?: boolean }>(
        `/api/business/entities/${encodeURIComponent(entityId)}/reimburse`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ txn_id: txnId, mode }),
        }),
    bizFlag: (id: string, on: boolean) =>
      req<{ ok: boolean }>(
        `/api/business/${encodeURIComponent(id)}/${on ? "flag" : "unflag"}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        }),
    receipts: (txnId: string) =>
      req<{ receipts: Receipt[] }>(
        `/api/transactions/${encodeURIComponent(txnId)}/receipts`),
    receiptUpload: async (txnId: string, uri: string,
                          kind: "receipt" | "check" = "receipt",
                          name = "receipt.jpg", type = "image/jpeg") => {
      const fd = new FormData();
      // React Native's FormData takes a file descriptor object; name/type
      // default to the camera JPEG and carry a picked PDF's real identity
      fd.append("file", { uri, name, type } as unknown as Blob);
      fd.append("kind", kind);
      const r = await fetch(
        `${baseUrl}/api/transactions/${encodeURIComponent(txnId)}/receipt`,
        { method: "POST", body: fd, signal: epochSignal(),
          headers: { Authorization: `Bearer ${token}` } });
      if (!r.ok) throw await parseError(r);
      return parseOkBody<{ id: string; receipts: Receipt[] }>(
        r.status, r.headers.get("content-type"), await r.text());
    },
    receiptItems: (q = "", group = "item") =>
      req<{ rows: { receipt_id: string; line: number; description: string;
                    qty: number | null; amount: number; tag: string;
                    txn_id: string; date: string; payee: string }[];
            groups: { key: string; n: number; total: number;
                      last_date?: string | null }[];
            truncated: boolean }>(
        `/api/receipts/items?q=${encodeURIComponent(q)}&group=${group}`),
    receiptTag: (id: string, line: number, tag: string) =>
      req<{ ok: boolean }>(
        `/api/receipts/${encodeURIComponent(id)}/items/${line}/tag`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tag }),
        }),
    receiptDelete: (id: string) =>
      req<{ ok: boolean }>(`/api/receipts/${id}`, { method: "DELETE" }),
    receiptParse: (id: string) =>
      req<{ status: string }>(`/api/receipts/${id}/parse`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }),
    notifyTest: (channel: "sms" | "push") =>
      req<{ ok: boolean; delivered?: number }>("/api/notify/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ channel }),
      }),
    calendar: (days = 35) =>
      req<{ days: number; start: string;
            calendar: { date: string; total: number;
                        events: { label: string; amount: number;
                                  kind: string }[] }[] }>(
        `/api/calendar?days=${days}`),
    doctor: () =>
      req<{ ok: boolean; checks: { section: string; name: string;
            ok: boolean; severity: string; detail: string }[] }>(
        "/api/doctor"),
    doctorScripts: () =>
      req<{ scripts: { source: string; label?: string;
            last_push: string | null; ok: boolean;
            status?: string; age_hours?: number | null;
            last_rows?: number | null;
            expected_hours: number | null; alerts: number }[] }>(
        "/api/doctor/scripts"),
    doctorScriptUpdate: (source: string,
                         body: { expected_hours?: number;
                                 alerts?: boolean }) =>
      req<unknown>(
        `/api/doctor/scripts/${encodeURIComponent(source)}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
    testing: () =>
      req<{ enabled: boolean; feedback_to?: string | null }>(
        "/api/testing"),
    testingFeedback: async (message: string, shot: PickedFile | null,
                            kind: "feedback" | "bug" = "feedback") => {
      const fd = new FormData();
      fd.append("message", message);
      fd.append("kind", kind);
      if (shot)
        fd.append("screenshot", { uri: shot.uri, name: shot.name,
          type: shot.mime || "image/jpeg" } as unknown as Blob);
      const r = await fetch(baseUrl + "/api/testing/feedback",
        { method: "POST", body: fd, signal: epochSignal(),
          headers: { Authorization: `Bearer ${token}` } });
      await checkAuth(r, "POST");
      if (!r.ok) throw await parseError(r);
      // no SMTP → the server answers a ZIP of the report instead of
      // JSON. The bytes are in THIS response (a re-GET would file a
      // second report), so save them and hand off to the share sheet —
      // the web downloads the same blob. Best-effort: the report exists
      // server-side either way, and the number still gets reported.
      if ((r.headers.get("content-type") ?? "").includes("zip")) {
        const number = r.headers.get("x-feedback-number") ?? "";
        try {
          const FileSystem = await import("expo-file-system/legacy");
          const Sharing = await import("expo-sharing");
          const blob = await r.blob();
          const b64: string = await new Promise((res, rej) => {
            const fr = new FileReader();
            fr.onloadend = () =>
              res(String(fr.result).split(",")[1] ?? "");
            fr.onerror = rej;
            fr.readAsDataURL(blob);
          });
          const dest = FileSystem.cacheDirectory
            + `oikonome-feedback-${number || "report"}.zip`;
          await FileSystem.writeAsStringAsync(dest, b64,
            { encoding: FileSystem.EncodingType.Base64 });
          await Sharing.shareAsync(dest);
        } catch {
          // saving is best-effort; the caller still reports the number
        }
        return { number, delivery: "zip" as const };
      }
      return parseOkBody<{ number: string; delivery: string }>(
        r.status, r.headers.get("content-type"), await r.text());
    },
    entityCreate: (body: { name: string; structure: string;
                           ein?: string }) =>
      req<Entity>("/api/business/entities", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    entityUpdate: (id: string, body: Record<string, unknown>) =>
      req<Entity>(`/api/business/entities/${encodeURIComponent(id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    entityStatus: (id: string, status: string) =>
      req<Entity>(
        `/api/business/entities/${encodeURIComponent(id)}/status`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status }),
        }),
    // archive is the everyday "remove" — every record survives,
    // restorable; delete-forever is the made-this-by-mistake case and
    // must carry the entity's exact typed name (the server refuses
    // anything else)
    entityArchive: (id: string) =>
      req<Entity>(
        `/api/business/entities/${encodeURIComponent(id)}/archive`,
        { method: "POST" }),
    entityRestore: (id: string) =>
      req<Entity>(
        `/api/business/entities/${encodeURIComponent(id)}/restore`,
        { method: "POST" }),
    entityDeleteImpact: (id: string) =>
      req<EntityDeleteImpact>(
        `/api/business/entities/${encodeURIComponent(id)}/delete-impact`),
    entityDeleteForever: (id: string, name: string) =>
      req<{ ok: boolean } & EntityDeleteImpact>(
        `/api/business/entities/${encodeURIComponent(id)}/delete-forever`,
        { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }) }),
    help: () => req<{ topics: { id: string; title: string;
                                body: string }[] }>("/api/help"),
    // which delivery channels can actually deliver — gates the SMS/push
    // toggles the way the web Settings page does
    notifyStatus: () =>
      req<{ sms_available: boolean; sms_entitled?: boolean;
            phone: string | null; phone_verified: boolean;
            phone_pending: string | null;
            // push_available is BROWSER push (VAPID); native_push_available
            // is the relay this app's own pushes ride
            push_available: boolean; native_push_available?: boolean;
            native_push_devices?: number }>(
        "/api/notify"),
    notifyPhone: (number: string, consent: string[] = []) =>
      req<{ ok: boolean }>("/api/notify/phone", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ number, consent }),
      }),
    notifyPhoneVerify: (code: string) =>
      req<{ ok: boolean }>("/api/notify/phone/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
      }),
    jobsEmail: () =>
      req<{ ok: boolean; subject: string }>("/api/jobs/email", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }),
    settingsFull: () =>
      req<SettingsView>("/api/settings"),
    // Plaid Enrich for imported history — status + a capped run (owner)
    enrichStatus: () =>
      req<{ candidates: number; plaid_configured: boolean; cap: number;
            used: number; month: string; remaining: number }>("/api/import/enrich"),
    enrichRun: (limit = 100) =>
      req<{ sent: number; enriched: number; cap_hit: boolean; error: string | null }>(
        "/api/import/enrich", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ limit }) }),
    // PARTIAL body — the server merges keys; every list key is a
    // wholesale replace, and an empty list deletes the key
    // The reply is the full settings view — the same document settingsFull
    // reads — so a caller can seed the cache from it instead of refetching.
    settingsSave: (body: Record<string, unknown>) =>
      req<SettingsView>("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    budgetsSuggest: () =>
      req<BudgetSuggestions>("/api/budgets/suggest"),
    settingsSchedule: () =>
      req<{ email_schedule: EmailSchedule | null }>("/api/settings")
        .then((s0) => s0.email_schedule ?? null),
    // the household's zone ("" = follow the instance); the server refuses a
    // name it cannot load, so a typo never silently moves the clock
    settingsSaveTimezone: (timezone: string) =>
      req<SettingsView>("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ timezone }),
      }),
    settingsSaveSchedule: (email_schedule: EmailSchedule) =>
      req<SettingsView>("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email_schedule }),
      }),
    // local = the bundled/env backend or a loopback/compose host: nothing
    // leaves the box; otherwise host names where the questions go
    assistantStatus: () =>
      req<{ available: boolean; local: boolean; host?: string | null }>(
        "/api/assistant"),
    assistantAsk: (question: string) =>
      req<{ answer: string; tools_used: string[] }>("/api/assistant", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      }),
  };
}

export type Client = ReturnType<typeof makeClient>;

/** Sign in and mint a device token. Runs the normal login (so every
 *  server-side defense applies), uses the resulting session once to mint
 *  the device credential, then closes the session — the phone keeps the
 *  token, not a cookie. */
export async function loginAndMintDevice(
  baseUrl: string, email: string, password: string, totpCode: string,
  deviceName: string, platform: string,
  /** sign this device out to make room, when the account is at its
   *  device limit — the id comes from the 409 the previous attempt
   *  returned, so the choice is always the person's */
  replaceDeviceId = "",
): Promise<{ token: string; id: string }> {
  const login = await fetch(baseUrl + "/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded",
               Accept: "application/json" },
    body: new URLSearchParams({
      email, password, totp_code: totpCode }).toString(),
  });
  if (!login.ok) throw await parseError(login);
  return mintDeviceFromSession(baseUrl, login, deviceName, platform,
                               replaceDeviceId);
}

/** The session→device-token exchange every sign-in flow shares: use the
 *  fresh session cookie once to mint the device credential, then close
 *  the session — the phone keeps the token, not a cookie. */
async function mintDeviceFromSession(
  baseUrl: string, login: Response, deviceName: string, platform: string,
  replaceDeviceId = "",
): Promise<{ token: string; id: string }> {
  // React Native's networking layer keeps the session cookie for the
  // mint + logout below; grab the header too in case a platform drops it
  const setCookie = login.headers.get("set-cookie") || "";
  const cookie = setCookie.split(";")[0] || "";
  const withCookie: Record<string, string> = cookie ? { Cookie: cookie } : {};
  // The ticket is what actually carries the sign-in on iOS: no Set-Cookie
  // reaches this code there, and the platform jar does not send the session
  // on the very next request either, so the mint would arrive
  // unauthenticated and the login would silently not stick. The cookie
  // headers stay for servers older than the ticket.
  let mintTicket = "";
  try {
    mintTicket = ((await login.clone().json()) as
      { mint_ticket?: string }).mint_ticket || "";
  } catch { /* older server: body is not JSON we can read */ }
  const mint = await fetch(baseUrl + "/api/devices", {
    method: "POST",
    headers: { "Content-Type": "application/json",
               Accept: "application/json", ...withCookie },
    body: JSON.stringify({ device_name: deviceName, platform,
                           ...(replaceDeviceId ? { replace: replaceDeviceId } : {}),
                           ...(mintTicket ? { mint_ticket: mintTicket } : {}) }),
  });
  if (!mint.ok) throw await parseError(mint);
  // through the same guard as every other 2xx: a captive Wi-Fi portal
  // answers this POST with its own 200 HTML sign-in page, and a bare
  // .json() on that throws a SyntaxError that is not an ApiError — the
  // login screen's error branch would print the parser's words instead of
  // the one message written for exactly this situation.
  const minted = parseOkBody<{ token: string; id: string }>(
    mint.status, mint.headers.get("content-type"), await mint.text());
  await fetch(baseUrl + "/api/logout", {
    method: "POST", headers: withCookie,
  }).catch(() => { /* cookie session dies on its own if this misses */ });
  return { token: minted.token, id: minted.id };
}

/** WebAuthn request options for a passkey sign-in (discoverable flow —
 *  no email; the credential sheet picks the account). 404 on servers
 *  without passkey login (self-host); the caller reads that as "this
 *  server doesn't offer passkeys". */
export async function passkeyLoginOptions(baseUrl: string): Promise<{
  challengeId: string; publicKey: Record<string, unknown>;
}> {
  const r = await fetch(baseUrl + "/api/login/passkey/options", {
    method: "POST",
    headers: { "Content-Type": "application/json",
               Accept: "application/json" },
    body: "{}",
  });
  if (!r.ok) throw await parseError(r);
  const o = parseOkBody<{ challenge_id: string; options: unknown }>(
    r.status, r.headers.get("content-type"), await r.text());
  // the request options may arrive wrapped in { publicKey } — unwrap the
  // same way the web login does
  const wrapped = o.options as { publicKey?: unknown };
  return { challengeId: o.challenge_id,
           publicKey: (wrapped.publicKey
             ?? o.options) as Record<string, unknown> };
}

/** Complete a passkey sign-in with the platform's signed assertion, then
 *  mint the device token exactly like password login. */
export async function passkeyLoginAndMintDevice(
  baseUrl: string, challengeId: string, credential: unknown,
  deviceName: string, platform: string, replaceDeviceId = "",
): Promise<{ token: string; id: string }> {
  const login = await fetch(baseUrl + "/api/login/passkey", {
    method: "POST",
    headers: { "Content-Type": "application/json",
               Accept: "application/json" },
    body: JSON.stringify({ challenge_id: challengeId, credential }),
  });
  if (!login.ok) throw await parseError(login);
  return mintDeviceFromSession(baseUrl, login, deviceName, platform,
                               replaceDeviceId);
}

/** A server is "an Oikonome instance" when its health door answers. */
export async function probeServer(baseUrl: string): Promise<boolean> {
  try {
    const r = await fetch(baseUrl + "/healthz",
                          { headers: { Accept: "application/json" } });
    if (!r.ok) return false;
    const body = await r.json();
    return body?.ok === true;
  } catch {
    return false;
  }
}
