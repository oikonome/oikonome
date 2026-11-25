import type { Glance } from "./glance-pure";
import type { AnswerBlock, HistoryCursor } from "./answer-pure";

/** where one AI task's data goes, judged by the server from the endpoint
 *  that task would call: on this server or its network, to a remote AI
 *  service (`host` named only when the household configured it), or
 *  nowhere. Every "stays on this instance" line reads this — absent
 *  (loading, an older server) is never read as "stays here". */
export type AiDestination = S["AiDestination"];
export type AssistantStatus =
  Omit<OlderServer<S["AssistantStatus"], "host" | "roles">, "roles">
  & { roles?: Partial<S["AiRoles"]> };

export type AssistantTurn = OlderServer<S["AssistantTurn"], "thread_id">;
import type { components } from "./api.generated";
import { downloadsToSweep, isPickerScratch, isReauthReply, isSharedDownload,
         PICKER_SCRATCH, busyText, failCause, readFinished,
         restartingText } from "./pure.ts";
import type { MintGrant } from "./pure.ts";
import { fetchStamped } from "./connection.ts";
// the server's documented shapes (api.generated.ts, specs/api-types.md)
type S = components["schemas"];
/** `T` with the named fields optional: what an older self-hosted
 *  server, which does not send them yet, may hand this build. */
type OlderServer<T, K extends keyof T> = Omit<T, K> & Partial<Pick<T, K>>;
/** A factor, password or sheet door's answer with the rotation report's
 *  counts optional: an older server answers those doors without some of
 *  them. `Keep` names the keys this build needs present. */
type Rotated<T, Keep extends keyof T = never> = OlderServer<T,
  Exclude<keyof T & keyof S["PasswordChanged"], "ok" | Keep>>;
/** One multipart file part, in the shape the runtime's fetch can send.
 *  React Native's `{ uri, name, type }` descriptor is refused by Expo's
 *  fetch ("Unsupported FormDataPart implementation"): it serialises a part
 *  from `bytes()` plus `name`/`type`, so the file is read from disk here and
 *  the picker's own name and type ride along. Every upload goes through
 *  this one door; a missing type falls back to the file's own. */
async function filePart(uri: string, name?: string, type?: string): Promise<Blob> {
  // loaded on first use: this module is also read by the pure-logic tests
  // under plain Node, which cannot load a native package
  const { File: FsFile } = await import("expo-file-system");
  const f = new FsFile(uri);
  return {
    bytes: async () => new Uint8Array(await f.arrayBuffer()),
    name: name || f.name || "upload",
    type: type || f.type || "application/octet-stream",
  } as unknown as Blob;
}
// Thin API client. Every number the app shows comes from the server's
// /api — the app holds no business logic. Types mirror webapp/src/api's
// contract for the endpoints the app consumes.

// The ledger row is ONE shape across server, web and mobile: the server's
// Txn model (web/schemas/ledger.py), generated like every other shape.
export type Txn = S["Txn"];
export type ForecastScorePanel = S["ForecastScorePanel"];
export type ForecastMiss = S["ForecastMiss"];

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

export type Alert =
  OlderServer<S["Alert"], "link">;

/** One plan row as the server builds it (todayview._plan_rows). */
export type ServerPlanRow =
  OlderServer<S["ServerPlanRow"], "status">;
export type PlanTotal = OlderServer<S["PlanTotal"], "status">;
export type RowStatus = S["RowStatus"];

/** Today's cash notice: `kind` picks the sentence, `amount` the dollar it
 *  quotes, `until` the date it runs to; `ask` (no paycheck scheduled)
 *  replaces the alarm with the missing setup step. */
export type CashNotice =
  S["CashNotice"];

export type TodayFull =
  Omit<OlderServer<S["TodayFull"],
      "real_today" | "min_date" | "month_share" | "variable_budget_paced"
      | "paced_from" | "month_remaining_paced" | "savings_plan"
      | "plan_goals" | "plan_line" | "plan_legs" | "avg_bills_count" | "plan_rows"
      | "plan_total"
      | "after_cards_bills" | "after_cards_bills_paychecks" | "cash_notice"
      | "day_meter" | "recent_variable_spending" | "avg_bills"
      | "plan_variable" | "scheduled_month" | "income_received"
      | "income_expected" | "income_source" | "sweep_plan"
      | "sweep_available" | "sweep_line" | "fixed_unpaid_due"
      | "irregular_bills" | "overdue_unpaid" | "income_due_dates"
      | "income_due_total" | "income_due_note" | "pace_line" | "why"
      | "why_chips">, "recent">
  & { recent: Txn[] };

// what the ledger holds under one side of a merge offer, read live
export interface MergeSideFacts {
  rows: number; total: number; first?: string | null; last?: string | null;
  typical?: number | null; category?: string | null; city?: string | null;
  accounts: string[];
  recent: { date: string; amount: number; line: string; account?: string | null }[];
}
export interface MergeProposal {
  id: string; from: string; into: string; from_id: string; into_id: string;
  // the surviving merchant's own name — `into` is the brand on a chain offer
  into_name?: string;
  from_facts?: MergeSideFacts | null; into_facts?: MergeSideFacts | null;
  // the "After merge" box as the server adds it: both sides together and
  // the signed change the folded side makes; absent from an older server
  merged?: S["MergedFacts"] | null;
  from_logo?: string | null; into_logo?: string | null;
  from_rows?: number | null; into_rows?: number | null;
  from_samples: string[]; into_samples: string[];
  signals: string[]; supports: string[];
  // "Merge all" may take it (one name is the other cut short); any other
  // offer carries the caution it is shown with, one at a time
  bulk: boolean; caution: string | null;
}
export interface MerchantCatalog {
  // `variant_count` is the TRUE number of source strings behind a display
  // name; `variants` is capped by the server, so its length is not the
  // count and only `variant_count` may be printed as a fact.
  merchants: { display: string; rows: number; total: number;
               first: string; last: string; variants: string[];
               variant_count?: number; business_rows?: number;
               logo?: string | null; website?: string | null;
               kind?: string | null; plaid?: boolean; parent?: string | null;
               // the total with its direction ("$1,250 spent")
               total_label?: string | null }[];
  // `label` / `line`: the change in words, and the bank line when it is
  // neither name (merchant_dedup.rename_words)
  recent: { id: number; raw_merchant: string; from_canonical: string | null;
            to_canonical: string; at: string;
            label?: string | null; line?: string | null }[];
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
  // `label`: what the rule does for new charges, in words
  rule?: { category_primary: string; source: string;
           label?: string | null } | null;
  locations: { city: string | null; region: string | null;
               address: string | null; postal: string | null;
               lat: number | null; lon: number | null;
               n: number; last: string }[];
  /** the total with its direction ("$1,250 spent") */
  total_label?: string | null;
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
  /** with `bucket`: the day a past Today screen was read on */
  as_of?: string;
  /** with `cat` and y+m or one whole year: the rows a lens category
   *  label summed, totalled as the label */
  counted?: string;
  /** a Spending figure's rows over one range key (with `cat`, `merchant`
   *  or neither; `side` "previous" for the window before), totalled as
   *  the figure */
  spend?: string; merchant?: string; side?: string;
}

export type TxnPage =
  OlderServer<S["TxnPage"], "biz_count" | "per_day" | "per_day_days">;

// script token scope: push = the import doors (a collector); read = the
// integrations doors (a scraper, a sensor, an MCP server). Never both.
export type TokenScope = S["ApiToken"]["scope"];

// outbound webhooks — the secret appears only in the create response
export type Webhook = S["Webhook"];
export type WebhookEvent = S["IntegrationEvent"];

// the household activity log: one hand-authored act per row, the sentence
// composed by the server so every client reads the same words
export type ActivityRow =
  S["ActivityRow"];
export type ActivityPage =
  S["ActivityPage"];
// The continuity packet's doors, generated from the server's documented
// shapes (specs/api-types.md).
export type Continuity = S["Continuity"];
export type ContinuitySaved = S["ContinuitySaved"];
export type ContinuityEmailed = S["ContinuityEmailed"];

/** One member's notices: their email switches and their push switch with
 *  the devices it governs (the web's MemberNotify). */
export type MemberNotify = OlderServer<S["MemberNotify"], "mail_listed">;
/** GET /api/me (web/schemas/household.py). The fields marked here are
 *  absent from an older server, the daily-email kinds and cadences
 *  included. */
export type Me =
  Omit<OlderServer<S["Me"],
      "day_counts" | "push_muted" | "shared_login" | "bank_link" | "timezone"
      | "timezone_source" | "instance_timezone" | "donate_url"
      | "totp_enabled" | "verified" | "created_at" | "verify_deadline"
      | "pending_email" | "pending_email_expires_at" | "recovery_codes_left"
      | "factor_reset_pending" | "has_business" | "features"
      | "merchant_logos" | "needs_2fa" | "email_delivery" | "plan">,
    "daily_email">
  & { daily_email?: OlderServer<S["MeDailyEmail"], "prefs" | "cadences"> };

export type Account =
  OlderServer<S["Account"],
      "bank_name" | "item_id" | "inst_logo_url" | "inst_color" | "inst_url"
      | "owner" | "entity_id" | "aggregator" | "card_due" | "card_statement"
      | "user_removed_at" | "link" | "script" | "goal_destination"
      | "can_exclude" | "can_pin">;

/** Whether an account's balance belongs in a money aggregate.
 *
 *  Linked sources feed ONE real account — the healthiest live source
 *  serves, the rest are shadows of the same money. The server excludes
 *  them from every aggregate it computes, so a screen that adds
 *  balances itself has to exclude them too or a dual-sourced account
 *  lands in the total twice. The rows still render (badged "backup") —
 *  only the arithmetic drops them.
 *
 *  A hidden account never counts: hiding a manual or imported account
 *  keeps its last balance (there is no feed to restore it on unhide),
 *  so a sum that only checked the link would still add money the owner
 *  switched off. Mirrors the web's countsInTotals (bizmath.ts). */
export const countsInTotals = (
  a: Pick<Account, "link"> & { user_removed_at?: string | null },
) => (!a.link || a.link.primary) && !a.user_removed_at;

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

export type Connection =
  OlderServer<S["Connection"],
      "bank_updated_at" | "bank_failed_at" | "status_kind"
      | "archived_reason" | "institution_health" | "product_issues"
      | "accounts" | "history_months">;
export type Holding =
  S["Holding"];
export type AccountRemoveMode =
  "hide" | "disconnect" | "disconnect_purge" | "purge";

export type Proposal =
  OlderServer<S["BillProposal"],
      "bill_type" | "income" | "next_due" | "llm_tag" | "frequency"
      | "interval" | "by_month_day" | "bill_payee">;
/** The Adjust form's changes to a proposal, sent with its approve. */
export type ProposalAdjust = { name?: string; amount?: number;
                               cadence?: string; due_day?: number;
                               by_month_day?: number[] };

/** GET /api/onboarding — the wizard's counts, step marks and the three
 *  first-run choices it asks for. */
export type Onboarding =
  OlderServer<S["Onboarding"],
      "primary_checking_id" | "checking_candidates" | "history_months"
      | "setup_mode" | "bill_detection_auto" | "budgets_saved_by_user">;

export type Bill =
  OlderServer<S["Bill"],
      "companions" | "fee_total" | "merchants" | "disabled" | "last_seen"
      | "last_match" | "cycle_days" | "suggestion" | "cadence_value"
      | "by_month_day" | "fit" | "fit_diverges" | "hint_dismissed"
      | "withdrawn_on" | "health_withdrawn_on">;
export type BillsData =
  Omit<OlderServer<S["BillsData"],
      "month" | "month_final" | "totals" | "cadences">, "bills" | "proposals">
  & { bills: Bill[]; proposals: Proposal[] };

export type PLRow = S["CoinbasePl"] | S["InvestedPl"];
/** The Accounts screen's figures, server-decided (the web's AccountTotals):
 *  the headline is Net Worth's Financial accounts figure; loans are listed
 *  as owed; section and institution subtotals per pane. */
export type AccountTotals =
  Omit<S["AccountTotals"], "sections">
  & { sections: Record<string, Record<string, SectionTotal>> };
/** A section's figures; `shown` (the whole dollars the screen prints,
 *  adding up as shown) is left out by an older server. */
export type SectionTotal = OlderServer<S["SectionTotal"], "shown">;
export type AccountsData = Omit<S["AccountsData"], "totals">
  & { totals: AccountTotals };
/** One Over-time timeframe over `full_trend` (the web's TrendWindow). */
export type TrendWindow = S["TrendWindow"];
/** A breakdown list's figures; `shown` (whole dollars and shares, adding
 *  up as shown) is left out by an older server. */
export type RingFigures = OlderServer<S["RingFigures"], "shown">;
// fields an older server leaves out stay optional (specs/api-types.md)
export type NetWorthReport =
  Omit<OlderServer<S["NetWorthReport"],
    "full_trend" | "month_change" | "financial_month_change"
    | "property_month_change" | "mortgage_payoff"
    | "full_trend_estimated_until" | "trend_windows"
    | "snapshot_trend" | "savings_trend" | "_built_at">,
    "property_items" | "rings">
  & { property_items: OlderServer<S["PropertyItem"],
        "source" | "entered_text" | "update_due">[];
      rings?: { by_asset_class: RingFigures; by_institution: RingFigures } };
export type FeesReport =
  OlderServer<S["FeesReport"], "_built_at" | "platform_counts">;
export type ManualAsset =
  OlderServer<S["ManualAsset"], "kind" | "as_of">;
export type SpendingReport = OlderServer<S["SpendingReport"], "_built_at">;
// where the money came from and where it went, for one period — income by
// kind on the left, fixed / variable / saved on the right (mirrors the web)
// each side's `shown` (the nodes as printed) is left out by an older server
export type FlowPeriod =
  Omit<OlderServer<S["FlowPeriod"],
    "live_month" | "income_clipped" | "income_gap_months" | "income_unknown"
    | "incomplete_months" | "dating" | "saved_months_known"
    | "saved_months_total" | "invested" | "invested_text" | "_built_at">,
    "in" | "out">
  & { in: OlderServer<S["FlowIn"], "shown">;
      out: OlderServer<S["FlowOut"], "shown" | "variable_scheduled"
        | "unsplit" | "unsplit_text" | "split_shown" | "fixed_basis_text"> };
// served per range (the site-wide 3m/6m/1y/3y/5y/all vocabulary), not in
// the report — the fixed split walks months and 'all' costs seconds
export type FlowKey = "cur" | "1m" | "3m" | "6m" | "1y" | "3y" | "5y" | "all";
// the Spending tab's windowed view — ranked lists each carry the previous
// equal window's figure (null when the range is 'all': no previous window)
export type SpendingWindow =
  OlderServer<S["SpendingWindow"],
    "live_month" | "incomplete_months" | "dating" | "dropped_merchants"
    | "category_shares" | "per_month" | "per_month_by_category" | "change"
    | "category_changes" | "merchant_changes" | "movers" | "_built_at">;
export type Change = S["Change"];
export type SpendingMover = S["SpendingMover"];
export type IncomeKinds = S["IncomeKinds"];
export type IncomeWindow =
  OlderServer<S["IncomeWindow"],
    "live_month" | "per_month" | "paycheck_share" | "incomplete_months"
    | "dropped_sources" | "source_shares" | "change" | "kind_changes"
    | "source_changes" | "source_paychecks" | "kinds_shown" | "_built_at">;
/** One income-by-source row's paycheck verdict (specs/paychecks.md). */
export type SourcePaycheck = S["SourcePaycheck"];
/** Interest and fees paid on bank and card accounts, by year — what
 *  holding money costs, net of what the bank refunded. Empty `by_year`
 *  means the section has nothing to say. */
/** A year of interest and fees; an older server sends neither its
 *  printed cells (`shown`) nor `biggest_text`. */
export type CashCostYear =
  OlderServer<S["CashCostYear"], "shown" | "biggest_text">;
export type CashCostsReport =
  Omit<OlderServer<S["CashCostsReport"],
    "vs_earned_ytd" | "_built_at" | "paid_ytd" | "shown">, "by_year" | "ytd">
  & { by_year: CashCostYear[]; ytd: CashCostYear | null };
export type CashflowReport =
  Omit<OlderServer<S["CashflowReport"],
    "savings_by_year_months" | "savings_by_year_notes" | "months_by_year"
    | "incomplete_months" | "incomplete_label" | "dating"
    | "brokerage_funding_mtd"
    | "brokerage_funding_ytd"
    | "_built_at">, "cash_graph">
  & { cash_graph: OlderServer<S["CashGraph"],
        "current" | "forecast_days" | "extremes" | "windows"> };

export interface AlertRow {
  kind: string; severity: string; message: string; link?: string | null;
  first_seen: string; last_seen: string; active: number; dismissed: number;
  /** the history's name for the kind, and the chip that lists it */
  kind_label?: string | null; group?: string | null;
  /** the words the history prints (`message` stays the dismiss/restore
   *  key); absent from an older server */
  text?: string;
}

export interface EmailSchedule {
  daily?: { on: boolean; hour: number; sms?: boolean; push?: boolean;
            summary?: boolean };
  monthly?: { on: boolean; hour: number; sms?: boolean; push?: boolean };
  yearly?: { on: boolean; hour: number; sms?: boolean; push?: boolean };
}

export type MobileDevice = S["MobileDevice"];

/** What an unknown store is called on the Receipts screen (the web
 *  client's UNKNOWN_STORE). */
export const UNKNOWN_STORE = "Unknown store";
export type ReceiptItem =
  S["ReceiptItem"];
/** Another receipt of a receipt's slip, or one read alike that is not */
export type ReceiptTwin = S["ReceiptTwin"];
/** One part of a hand-split charge (Txn.split) */
export type SplitPart = S["SplitPart"];

export type Receipt =
  Omit<OlderServer<S["Receipt"],
      "has_optimized" | "amount_mismatch" | "match_method" | "matched_at"
      | "parse_started_at" | "parse_stale" | "duplicate_of" | "lines_gap">,
      "items">
  & { items: ReceiptItem[] };

// receipts snapped before their transaction reached the ledger (the web
// client's WaitingReceipts shapes, field for field)
export type WaitingCandidate =
  S["WaitingCandidate"];
export type WaitingReceipt =
  Omit<OlderServer<S["WaitingReceipt"],
      "released" | "parse_started_at" | "parse_stale">, "candidates">
  & { candidates: WaitingCandidate[] };
export type RecentMatch =
  S["RecentMatch"];
export type WaitingReceipts =
  Omit<OlderServer<S["WaitingReceipts"], "limit" | "offset">, "waiting" | "recent">
  & { waiting: WaitingReceipt[]; recent: RecentMatch[] };

export type BillDetail =
  OlderServer<S["BillDetail"],
      "by_month_day" | "txn_category" | "companions" | "fee_total"
      | "merchants">;
export type Companion = S["Companion"];
export type LedgerFit =
  OlderServer<S["LedgerFit"], "cycle_days" | "hits" | "by_month_day">;
// history rows are the Transactions-page shape plus the matcher verdict
export type HistoryTxn = S["HistoryTxn"];
export type BillsHistoryData =
  Omit<OlderServer<S["BillsHistory"], "logo" | "bill_archived" | "merchant_in_use">, "txns" | "fit">
  & { txns: HistoryTxn[]; fit: LedgerFit | null };

export type PendingReimb =
  OlderServer<S["PendingReimb"],
    "partial" | "expected" | "received" | "flagged_at" | "received_pct">;
export type ReimbPending =
  Omit<OlderServer<S["ReimbPending"], "owed">, "pending">
  & { pending: PendingReimb[] };
export type ReimbCandidate =
  OlderServer<S["ReimbCandidate"], "pending" | "left_amount">;
/** The candidates door's anchor: what it still needs (`left_amount`) and
 *  what already came back to it (`received`), the figures the matcher
 *  words its rows and its gap line by. */
export type ReimbAnchor =
  OlderServer<S["ReimbAnchor"], "left_amount" | "received">;
export type ReimbPairsPage = OlderServer<S["ReimbPairsPage"], "more" | "page">;
export type ReimbPair = S["ReimbPair"];
export interface RuleRow {
  merchant: string; category_primary: string; source: string;
  disabled: boolean; count: number;
}
export interface RulesPage {
  rules: RuleRow[]; total: number; page: number; per_page: number;
  counts: { user: number; llm: number; seed: number; model?: number };
}
// The Retirement and Debt pages' doors, generated from the server's
// documented shapes (specs/api-types.md). A self-hosted server may be
// older than this build, so the fields an older server does not send stay
// optional here (OlderServer); the web, always served by its own server,
// takes the generated types as they are. Where the projection's spend and
// saving came from: a what-if, the saved answers, the ledger's year, or
// (spend) "needed": nothing to plan for, nothing projected.
export type RetirementInputs =
  OlderServer<S["RetirementInputs"], "spend_source" | "saving_source"
                                     | "saving_basis" | "saving_mo"
                                     | "saving_found_mo" | "roth_mo" | "td_mo"
                                     | "saving_parts" | "saving_hint">;
export type RetSpendSource = S["RetirementInputs"]["spend_source"];
export type RetSavingSource = S["RetirementInputs"]["saving_source"];
export type RetirementData =
  Omit<OlderServer<S["RetirementData"], "catch_up" | "stress_floor_spend"
                                        | "stress_floor_vs_spend" | "verdict"
                                        | "growth_path_retire_age">,
       "inputs" | "buckets" | "rows">
  & { inputs: RetirementInputs;
      buckets: OlderServer<S["RetirementBuckets"], "cards_from_cash"
                           | "cards_from_taxable" | "cards_from_td"
                           | "cards_from_roth">;
      rows: OlderServer<S["RetirementRow"], "hist_lasts">[] };
export type DebtRow =
  OlderServer<S["DebtRow"], "apr_default" | "min_default" | "estimated"
                            | "payment_reported" | "due_now" | "origin"
                            | "min_basis" | "rate_needed">;
export type DebtSchedule = OlderServer<S["DebtSchedule"], "finishes">;
export type DebtPlan =
  Omit<OlderServer<S["DebtPlan"], "saves_vs_minimums" | "alt_extra_interest"
                                  | "overrides" | "paid_in_full"
                                  | "alt_same_path">,
       "debts" | "plan" | "alt" | "minimums">
  & { debts: DebtRow[]; plan: DebtSchedule; alt: DebtSchedule;
      minimums: DebtSchedule };
export type DebtPlanSave = S["DebtPlanSave"];
export type DebtMethod = S["DebtPlan"]["method"];

export type Batch =
  OlderServer<S["ImportBatch"], "annotations">;
// a picked file as RN's networking layer wants it
export interface PickedFile { uri: string; name: string; mime?: string }
export type RestoreProgress =
  OlderServer<S["RestoreProgress"], "started_at">;
// An account a refusal asks the person to choose, described by what tells
// two cards sharing their last four apart when their names read the same.
export type AccountChoice =
  S["AccountChoice"];
export type ImportResult =
  OlderServer<S["ImportResult"],
      "error" | "imported" | "skipped_duplicates" | "matched" | "choices"
      | "choice_for" | "token" | "expired" | "restore_started" | "job"
      | "source" | "confidence" | "warnings" | "note" | "split_accounts"
      | "corrected" | "removed_earlier" | "kept_annotated" | "foreign_left"
      | "skipped_older" | "credentials_dropped">;
// A CSV whose columns could not be read: the person maps them.
export type ColumnMappingNeeded =
  OlderServer<S["ColumnMappingNeeded"], "kind" | "sample">;
// One YNAB account a register names; `name` is the key the answer uses
// ("" for lines with no account name), `label` what the person reads.
export type YnabAccountGroup =
  S["YnabAccountGroup"];
// A YNAB register of several accounts: the person says where each goes.
export type YnabAccountsNeeded =
  S["YnabAccountsNeeded"];
export type MappingNeeded = ColumnMappingNeeded | YnabAccountsNeeded;
// Where one YNAB account goes: an account id, "skip", or a new account.
// an account id, "skip", a new account, or an account picked from those a
// typed new-account name could mean, with that name (remembered under it)
export type YnabDestination = string | { new: string }
  | { id: string; typed: string };
export type ImportOutcome =
  S["ImportOutcome"];
export interface PlanFile {
  index: number; name: string; kind: string; size: number;
  action: "import" | "skip"; account_id: string | null;
  new_account_name: string; amount_sign: string;
  sign_certain: boolean; note: string; oversized?: boolean;
  // an export ZIP too large to read here names the door that takes it
  restore_door?: string;
  // the accounts the file could mean when the server cannot say which
  // (two cards sharing their last four): the person picks one
  // each described (AccountChoice)
  choices?: AccountChoice[];
}
export type TaxdocRow =
  OlderServer<S["TaxdocRow"],
      "exists" | "total_income" | "agi" | "taxable_income" | "tax_paid"
      | "wages" | "ss_earnings" | "medicare_earnings" | "employer" | "ein"
      | "boxes" | "box1" | "owner" | "matches" | "replaces">;
export type W2Match =
  S["W2Match"];
export type TaxdocPlan =
  S["TaxdocPlan"];
// a stored W-2; "unassigned" = nobody has said whose it is yet, and it
// counts toward nobody's wages until someone does
/** What the stored list showed for a W-2, sent back with a removal or an
 *  owner change so a reused id never acts on another form. */
export const w2Seen = (w: StoredW2) => ({
  year: w.year, box1: w.box1, employer: w.employer, ein: w.ein,
  owner: w.owner });

export type StoredW2 =
  S["StoredW2"];
// every stored tax and income document, in the server's words
export type StoredDoc = S["StoredDoc"];
export type SyncStatus =
  S["SyncStatus"];
export type Entity =
  OlderServer<S["Entity"],
      "archived_at" | "state" | "formation_date" | "business_start_date"
      | "ein_last4" | "home_office_sqft" | "income_tax_rate"
      | "tax_reserve_account_id" | "members">;
export type EntityDeleteImpact =
  S["EntityDeleteImpact"];
export type BizData =
  S["BizData"];
export type Capital =
  OlderServer<S["Capital"], "net_income" | "household_share_pct" | "household_capital"
      | "paid_personally">;
export type Pnl =
  OlderServer<S["Pnl"], "estimated_tax_paid" | "this_month" | "cash">;
export type BizTxn =
  OlderServer<S["BizTxn"],
      "note" | "cat_original" | "net_amount" | "partly_claimed"
      | "effective_bucket" | "role" | "paid_personally" | "owner_recorded"
      | "ledger_line" | "account_id">;
export type EstimatedTax =
  OlderServer<S["EstimatedTax"],
      "household" | "business_total" | "covered_profit" | "covered_tax_paid"
      | "household_unavailable" | "next_deadline" | "next_payment"
      | "annualized" | "vehicle_expenses_added_back" | "vehicle_method"
      | "mileage_logged" | "basis" | "annualized_taxable_profit"
      | "startup_label" | "loss_note"
      | "annualized_total" | "se_tax_applies" | "covers" | "structure"
      | "members" | "set_aside">;
export type Obligation =
  OlderServer<S["Obligation"], "id">;
export type MileageTrip =
  S["MileageTrip"];
export type Vendor =
  S["Vendor"];

export type CustomBucket =
  S["CustomBucket"];
export type IncomeScenario =
  S["IncomeScenario"];
export type SavingsGoal =
  OlderServer<S["SavingsGoal"], "target_date" | "account_id" | "mode">;
// the slice of /api/settings the budget screens read/write
/** GET /api/settings — and what every settings write returns. */
// the settings document (web/schemas/settings.py). Every key is optional
// here: which keys a reader gets depends on their role and on the server's
// age, and the budget half is typed by BudgetSettings. The open record
// lets the generic setting readers take it.
export type SettingsView = BudgetSettings & Record<string, unknown>
  & Partial<S["SettingsView"]>;

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
export type BudgetSuggestions =
  OlderServer<S["BudgetSuggestions"],
      "bucket_candidates" | "avg_bills_monthly" | "bills_count"
      | "history_days" | "history_scaled" | "min_rate_days"
      | "not_enough_history">;

export type Series =
  OlderServer<S["Series"], "first_neg_amount">;

/** a member an owner's credential change (or recovery) removed: they
 *  joined in the last days through the owner's own invites */
export interface RemovedMember { email: string; role: string; joined: string }

export type SavingsGoalProgress =
  OlderServer<S["SavingsGoalProgress"],
      "saved_90d" | "contributed_90d" | "needs_account" | "mode"
      | "swept_this_month" | "sweep_satisfied" | "saved_this_month">;

export type LensTxn =
  S["LensTxn"];
export type MonthLensData =
  Omit<OlderServer<S["MonthLensData"],
      "days_in_month" | "tolerance" | "budget_set" | "variable_scale"
      | "amazon_subs" | "dating" | "saved_month" | "savings_goal"
      | "plan_surplus" | "budget_source" | "bills_source" | "first_year"
      | "remaining" | "net">, "bills">
  // the bills card's count words are left out by an older server
  & { bills: OlderServer<S["LensBills"], "posted_counts"> };
export type YearCell =
  OlderServer<S["YearCell"], "income" | "spend" | "net">;
export type YearTotals =
  OlderServer<S["YearTotals"],
    "saved_months_known" | "saved_months_total" | "unknown_text"
    | "incomplete_text">;
// Budget history (the Budget page's card) — each frozen month's variable
// budgets, how it was captured ("close" as the month ran, "backfill" =
// applied retroactively by the owner), and how many closed months since
// the ledger began have no snapshot.
export type BudgetSnapshots =
  S["BudgetSnapshots"];
export type YearLensData =
  OlderServer<S["YearLensData"],
      "dating" | "prev_through" | "buckets_annual" | "saved_ytd"
      | "savings_goal" | "plan_surplus" | "changes">;


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
// methods: ["passkey"], ["password"], or ["passkey", "password"] (passkey
// first) for an account holding both a passkey and a password+TOTP. A
// hosted passkey-only account reads ["passkey", "recovery+password"]: its
// lost-key fallback is a recovery code sent WITH the password.
export type ElevationStatus = S["Elevation"];

/** POST /api/auth/elevate — exactly one proof per call. */
export type ElevationProof =
  | { password: string; totp_code?: string }
  // a lost authenticator: the recovery code stands in for the second
  // factor beside the password, as it does at login — and for the lost
  // passkey on a hosted passkey-only account. A bare code is only an
  // older server's passkey-only shape and is refused everywhere else.
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

/** Thrown by a door that carries a single-use code once the sheet has run
 *  in front of it. The sheet's proof can spend the same authenticator step
 *  as the code already typed, so resending the request could only be
 *  refused as a replay; the form asks for the next code instead. */
export class NextCodeNeeded extends Error {
  constructor() { super("enter the next code from your authenticator app"); }
}

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
  /** set on a device-mint refusal (the device-limit 409): the ticket and
   *  session from the login that just succeeded, still good on the
   *  server, so the retry spends them instead of signing in again */
  mintGrant?: MintGrant;
  /** the server's Retry-After header, when it sent one (a 503 shed) */
  retryAfter?: string | null;
  /** a 502/503/504 the reverse proxy answered because the app was not
   *  there to (a deploy, a restart): `detail` then holds the restarting
   *  sentence, since the proxy's page and status line say nothing a
   *  person can act on */
  restarting?: boolean;
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

/** The export's restore warnings: a JSON list of sentences in a header.
 *  Anything unreadable still counts as a warning, so a refused archive is
 *  never reported as a clean one. */
export function exportWarnings(raw: string | null): string[] {
  if (!raw) return [];
  try {
    const v: unknown = JSON.parse(raw);
    if (Array.isArray(v)) return v.map(String).filter((t) => t.trim());
  } catch { /* fall through */ }
  return [raw];
}

/** The sentence to show a person for a failed call — the web's errText.
 *
 *  `String(e)` prints "Error: 400: display too long (200 chars max)",
 *  which reads as a crash rather than as an answer. Prefer the server's
 *  own detail, and fall back to whatever the failure carried. */
// The last /api/me answer's day counts. Kept here, beside the one fetch
// that delivers them, so every sentence that names a number of days — the
// allowance note in the extension slot included, which is not a hook —
// reads the instance's value instead of writing its own.
let dayCounts: Me["day_counts"] = undefined;
const posInt = (v: unknown): number | null =>
  typeof v === "number" && Number.isInteger(v) && v > 0 ? v : null;
/** Days of failure before a connection is removed; null when the
 *  instance does not remove them. An older server says nothing: 30, the
 *  default. */
export function plaidReapDays(): number | null {
  if (!dayCounts || !("plaid_reap_days" in dayCounts)) return 30;
  return posInt(dayCounts.plaid_reap_days);
}
/** Days a released bank keeps its place under the allowance. */
export function releasedHoldDays(): number {
  return posInt(dayCounts?.released_hold_days) ?? 30;
}
/** What the Accounts screens say about a connection the server removed,
 *  by the reason it was removed. A removal whose reason the server did
 *  not send names no cause rather than guessing one. */
export function removedConnectionNote(
    c: { archived_reason?: string | null }): string {
  switch (c.archived_reason) {
    case "plaid-gone": return "connection no longer exists at Plaid";
    case "sub-ended": return "connection removed when the subscription ended";
    case "auto-reap": {
      const n = plaidReapDays();
      return n ? `connection removed after ${n} days of failure`
               : "connection removed after failing for weeks";
    }
    default: return "connection removed";
  }
}

export function errText(e: unknown): string {
  // no answer, a 5xx or a bare refusal: the cause in the app's words,
  // never "TypeError: Network request failed" or a status on its own
  const cause = failCause(e, "phone");
  if (cause) return cause;
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

/** What a what-if screen shows at its fields when a recalculation fails,
 *  or null when the failure is the connection's. A 4xx is the server
 *  answering about what was sent, so the screen keeps its last good
 *  numbers and says so beside the inputs — replacing it with "couldn't
 *  reach the server" is untrue, and pulling to refresh re-sends the same
 *  refused request forever. A 401 is not the input's fault: the session
 *  handler already takes that one back to sign-in. */
export function whatIfRefusal(e: unknown): string | null {
  if (!(e instanceof ApiError) || e.status < 400 || e.status >= 500
      || e.status === 401) return null;
  return e.detail ? e.detail : failCause(e, "phone") ?? "The server refused the request.";
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

// ---- the bill form's ledger-fit hand-off ----
// "Edit with these values" opens /bill with the amount, cadence and due
// date the ledger suggests. /bill is deep-linkable like every screen, so
// those values in the route would let an outside link pre-fill a REAL
// bill's form, which the next Save writes. The route carries only the
// payee; the values wait here, where only the in-app button can put them,
// and are taken once.
export type BillPrefill = { amount: string; cadence: string;
                            next_due: string;
                            by_month_day?: number[] | null };
let billPrefill: { payee: string; values: BillPrefill } | null = null;

/** bill-history deposits the fit here right before pushing /bill. */
export function setBillPrefill(payee: string, values: BillPrefill): void {
  billPrefill = { payee, values };
}

/** The fit handed over for this payee, consumed — or null when /bill was
 *  reached any other way (the plain Edit button, a link, a cold start). */
export function takeBillPrefill(payee: string | undefined):
    BillPrefill | null {
  const h = billPrefill;
  billPrefill = null;
  return payee && h && h.payee === payee ? h.values : null;
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

/** The sentence a refused download carries: FastAPI's `detail`, a string
 *  or an object with a `message`; "download failed" when the body says
 *  nothing readable. */
export function downloadRefusal(body: string): string {
  try {
    const d = (JSON.parse(body) as { detail?: unknown })?.detail;
    if (typeof d === "string" && d.trim()) return d;
    const m = d && typeof d === "object" ? (d as { message?: unknown }).message : null;
    if (typeof m === "string" && m.trim()) return m;
  } catch { /* not JSON */ }
  return "download failed";
}

async function parseError(r: Response): Promise<ApiError> {
  let detail = r.statusText;
  let structured: unknown;
  const ct = r.headers.get("content-type") ?? "";
  const text = await r.text().catch(() => "");
  const retryAfter = r.headers.get("Retry-After");
  // A gateway status whose body is not the app's own JSON comes from the
  // proxy in front of it while the app restarts: its "back in a moment"
  // HTML page, or nothing at all. The status line is no better — it reads
  // "Service Unavailable", or is empty over HTTP/2 — so say what is
  // happening, with the web's words.
  if (GATEWAY.has(r.status) && !appJson(ct, text)) {
    const err = new ApiError(r.status, restartingText(retryAfter));
    err.retryAfter = retryAfter;
    err.restarting = true;
    return err;
  }
  try {
    const body = JSON.parse(text);
    if (typeof body?.detail === "string") {
      detail = body.detail;
      // a refusal can carry flags beside its sentence (an add over an
      // archived bill says so, to offer Restore)
      structured = body;
    } else if (body?.detail && typeof body.detail === "object") {
      // a refusal that carries the means to act on it
      structured = body.detail;
      const m = (body.detail as { message?: unknown }).message;
      if (typeof m === "string") detail = m;
      // the step-up marker sits at the top of the body, beside detail;
      // keep it, or a structured refusal would read as a lost session
      if (typeof body.error === "string" && !("error" in body.detail))
        structured = { ...body.detail, error: body.error };
    }
  } catch { /* non-JSON error body — keep the status text */ }
  const err = new ApiError(r.status, detail, structured);
  err.retryAfter = retryAfter;
  return err;
}

const GATEWAY = new Set([502, 503, 504]);

/** Whether an error body is the app's own JSON. The proxy labels its page
 *  text/html, but an edge in front can rewrite the content-type, so a
 *  body that opens like markup, or is empty, counts as the proxy too. */
function appJson(ct: string, text: string): boolean {
  if (/html/i.test(ct)) return false;
  const t = text.trimStart();
  if (!t || t.startsWith("<")) return false;
  try { JSON.parse(t); return true; } catch { return false; }
}

// busyText / restartingText live in the screen-states twin block
// (pure.ts), so the failed-read line and the sign-in screen say the web's
// words.
export { busyText, restartingText };

/** The sign-in screen's sentence for a failure: a 503 is the server
 *  shedding load (sign-ins hashing at once, or the database full) — never
 *  a wrong password and never a raw status — so it says "busy", with the
 *  server's own wait when it named a short one. A restart the proxy
 *  answered for is neither, and keeps its own sentence. */
export function signInErrText(e: unknown): string {
  if (e instanceof ApiError && e.restarting)
    return restartingText(e.retryAfter);
  if (e instanceof ApiError && e.status === 503)
    return busyText(e.retryAfter);
  return errText(e);
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
  // the same line (main.tsx); both read the wording list from a pure
  // helper (isReauthReply) the logic suite runs against each copy.
  const checkAuth = async (r: Response, method?: string): Promise<void> => {
    if (r.status === 401) {
      const err = await parseError(r);
      if (!isReauthReply(err.detail, err.body)) onAuthLost();
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
    // previous account/session can never land in the cache (see query.ts).
    // no-store keeps the reply out of the platform's own HTTP disk cache
    // (OkHttp's on Android, URLCache on iOS), which nothing here can
    // encrypt or wipe at sign-out; expo/fetch takes no `cache` option.
    const r = await fetchStamped(baseUrl + path, {
      ...init,
      signal: init?.signal ?? epochSignal(),
      headers: { Authorization: `Bearer ${token}`,
                 Accept: "application/json",
                 "Cache-Control": "no-store",
                 ...(init?.headers || {}) },
    });
    try {
      await checkAuth(r, init?.method ?? "GET");
      if (!r.ok) throw await parseError(r);
      return parseOkBody<T>(r.status, r.headers.get("content-type"),
                            await r.text());
    } finally {
      // the body is read: the read stops watching the epoch for it
      readFinished(r);
    }
  };
  // step-up: an inline passkey assertion mints a 5-minute single-use
  // ticket that the destructive endpoints redeem out of the same
  // recovery_code field they already take. Sent through `send`, never
  // `req` — the step-up doors are what answers a step-up refusal, so
  // routing them back through the retry would be circular.
  const stepupPasskeyOptions = () =>
    send<S["PasskeyAssertOptions"]>(
      "/api/stepup/passkey/options", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: "{}" });
  const stepupPasskey = (challenge_id: string, credential: unknown) =>
    send<S["StepUpTicket"]>("/api/stepup/passkey", {
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
  // withElevation without the retry, for a body holding a one-time code:
  // the sheet is raised, then NextCodeNeeded hands the form back.
  const sendOneTimeCode = async <T>(path: string,
                                    init: RequestInit): Promise<T> => {
    try {
      return await send<T>(path, init);
    } catch (e) {
      if (!isElevationRequired(e)) throw e;
      try {
        await elevate(refusalWantsFresh(e));
      } catch (cancel) {
        throw new ApiError(403, cancel instanceof Error && cancel.message
          ? cancel.message : ELEVATION_CANCELLED,
          (e as ApiError).body);
      }
      throw new NextCodeNeeded();
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
      // had ten minutes to be consumed; the last one goes at the next
      // launch or at sign-out (sweepDownloads).
      const dir = FileSystem.cacheDirectory!;
      await sweepDownloads(SHARE_GRACE_S);
      const dest = dir + filename;
      const r = await FileSystem.downloadAsync(baseUrl + path, dest,
        { headers: { Authorization: `Bearer ${token}` } });
      if (r.status !== 200) {
        // downloadAsync writes the refusal's body to dest like any other
        // body; read the server's sentence from it (a multi-tenant
        // instance, a missing pg_dump) so the owner is not left retrying
        // a refusal that reads like a network blip, then drop the file
        let text = "";
        try {
          text = await FileSystem.readAsStringAsync(r.uri);
        } catch { /* nothing readable: the generic sentence stands */ }
        await FileSystem.deleteAsync(r.uri, { idempotent: true }).catch(() => {});
        throw new ApiError(r.status, downloadRefusal(text));
      }
      // the full export names what a restore of it would refuse in a
      // header; header-name case differs between platforms
      const h = Object.entries(r.headers ?? {}).find(
        ([k]) => k.toLowerCase() === "x-oikonome-export-warnings");
      await Sharing.shareAsync(r.uri);
      return { warnings: exportWarnings(h ? String(h[1]) : null) };
    },
    me: () => req<Me>("/api/me").then((m) => {
      dayCounts = m?.day_counts; return m; }),
    // who changed which category, bill, note… and when — newest first;
    // `before` pages by the last row's timestamp, kind/who filter
    activity: (params: Record<string, string> = {}) =>
      req<ActivityPage>("/api/activity"
        + (Object.keys(params).length
          ? "?" + new URLSearchParams(params) : "")),
    todayFull: (date?: string) =>
      req<TodayFull>("/api/today/full"
        + (date ? `?date=${encodeURIComponent(date)}` : "")),
    // how close the cash forecast has landed (specs/forecast-grading.md);
    // an older server has neither door, and the phone offers no panel
    forecastScore: () => req<ForecastScorePanel>("/api/today/forecast-score"),
    forecastMiss: (day: string, horizon = 30) =>
      req<ForecastMiss>("/api/today/forecast-score/miss?day="
        + encodeURIComponent(day) + `&horizon=${horizon}`),
    onboarding: () => req<Onboarding>("/api/onboarding"),
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
      req<S["BackfillStatus"]>(
        "/api/onboarding/backfill/nudge", { method: "POST",
          headers: { "Content-Type": "application/json" }, body: "{}" }),
    // a one-shot ticket the export download redeems within a minute. The
    // door wants FRESH elevation (the whole household's data leaves in one
    // file), so the sheet shows even inside an elevated window.
    // the continuity packet (web: client.ts continuity*)
    continuity: () => req<Continuity>("/api/continuity"),
    continuitySave: (body: { note?: string; prepared_for?: string }) =>
      req<ContinuitySaved>("/api/continuity", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) }),
    continuityEmail: (to: string) =>
      req<ContinuityEmailed>("/api/continuity/email", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ to }) }),
    exportTicket: (kind: "zip" | "dump" | "continuity") =>
      req<S["ExportTicket"]>(
        "/api/export/token", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ kind }) }),
    transactions: (params: TxnQuery = {}) =>
      req<TxnPage>("/api/transactions?" + new URLSearchParams(
        Object.entries(params)
          .filter(([, v]) => v !== undefined) as [string, string][])),
    /** ONE ledger row by id, the list door's row exactly (GET
     *  /api/transactions/one). A 404 is "this household holds no such
     *  row" — or a server older than the door, which callers treat the
     *  same: nothing to show, never an error. */
    txn: (id: string) =>
      req<Txn>("/api/transactions/one?" + new URLSearchParams({ id })),
    /** ONE ledger row found through its own DAY's listing — the fallback
     *  for a server older than /api/transactions/one (`txn` above). Null
     *  when the row is not in that listing (a hidden account's row, or a
     *  day longer than the pages walked here): "keep what you have". */
    txnById: async (id: string, date: string): Promise<Txn | null> => {
      const day = date.slice(0, 10);
      let seen = 0;
      for (let page = 1; page <= 10; page++) {
        const p = await req<TxnPage>("/api/transactions?"
          + new URLSearchParams({ date_from: day, date_to: day,
                                  page: String(page) }));
        const hit = (p.rows ?? []).find((r) => r.id === id);
        if (hit) return hit;
        seen += (p.rows ?? []).length;
        if (!p.rows?.length || p.total == null || seen >= p.total) break;
      }
      return null;
    },
    // primary_checking_id: the account the cash forecast starts from (the
    // live pin, else the largest-balance pick) — the ledger heads itself
    // with its balance; absent from older servers. primary_pin_unused: why
    // the saved pin is not that account (the web's)
    accounts: () => req<AccountsData>("/api/accounts"),
    // `month` (YYYY-MM) reads a past month's paid/received per bill
    bills: (month?: string) =>
      req<BillsData>(month ? `/api/bills?month=${month}` : "/api/bills"),
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
    reportCashCosts: () => req<CashCostsReport>("/api/reports/cash-costs"),
    alertsHistory: () => req<{ alerts: AlertRow[] }>("/api/alerts/history"),
    alertDismiss: (kind: string, message: string) =>
      req<S["AlertsDismissed"]>("/api/alerts/dismiss", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind, message }),
      }),
    alertRestore: (kind: string, message: string) =>
      req<S["AlertsRestored"]>("/api/alerts/restore", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind, message }),
      }),
    devices: () => req<S["MobileDevices"]>("/api/devices"),
    // ---- security self-service ----
    // Every posture change below is elevation-gated server-side: the
    // session proves itself once in the sheet and these calls carry no
    // password of their own. The current password on a CHANGE is the
    // change's own input, not re-auth.
    passwordChange: (current_password: string, new_password: string) =>
      // the server reports what the rotation removed, so a phone or
      // passkey that stops working right after is explained, not a mystery
      // recovery_codes: the rotation voided the recovery sheet; this is
      // the one that stands (null when the account has no second factor)
      req<Rotated<S["PasswordChanged"]>>(
        "/api/password/change", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // this app shows a reissued sheet; one that does not omits it and
        // keeps its codes
        body: JSON.stringify({ current_password, new_password,
                               reissue_codes: true }),
      }),
    // on hosted the address does not move yet: `email` is still the
    // sign-in address and `pending_email` the one awaiting its button
    emailChange: (new_email: string) =>
      req<S["EmailChanged"]>(
        "/api/email/change", {
        method: "POST",
        headers: { "Content-Type":
                     "application/x-www-form-urlencoded" },
        // this app shows a reissued sheet; one that does not omits it and
        // keeps its codes
        body: new URLSearchParams({ new_email,
                                    reissue_codes: "1" }).toString(),
      }),
    totpEnroll: () =>
      req<S["TotpEnrollment"]>("/api/totp/enroll", {
        method: "POST",
        headers: { "Content-Type":
                     "application/x-www-form-urlencoded" },
        body: "",
      }),
    totpConfirm: (secret: string, code: string) =>
      req<Rotated<S["TotpConfirmed"], "recovery_codes">>(
        "/api/totp/confirm", {
          method: "POST",
          headers: { "Content-Type":
                       "application/x-www-form-urlencoded" },
          body: new URLSearchParams({ secret, code }).toString(),
        }),
    // the live code is the route's own input (it proves the authenticator
    // being switched off is still in hand), kept beside elevation; being
    // single-use, it is never resent after the sheet
    totpDisable: (code: string) =>
      // turned off while a passkey stays, the old recovery sheet is voided
      // and the new one returned (null when no factor remains)
      sendOneTimeCode<Rotated<S["TotpDisabled"]>>(
        "/api/totp/disable", {
        method: "POST",
        headers: { "Content-Type":
                     "application/x-www-form-urlencoded" },
        // this client shows a reissued sheet; an older build omits it
        // and keeps its codes
        body: new URLSearchParams({ code, reissue_codes: "1" }).toString(),
      }),
    // stop a factor-clearing recovery on this account (no step-up:
    // cancelling is the safe direction)
    // withdraw a pending sign-in address change
    emailChangeCancel: () =>
      req<S["EmailChangeCancelled"]>(
        "/api/email/change/cancel", { method: "POST" }),
    cancelPendingRecovery: () =>
      req<S["RecoveryCancelled"]>(
        "/api/recovery/cancel-pending", { method: "POST" }),
    // the old sheet is voided, and what it could have reached is swept
    // with it — the same fields turning 2FA off answers with
    recoveryRegenerate: () =>
      req<Rotated<S["RecoveryRegenerated"], "recovery_codes">>(
        "/api/totp/recovery-regenerate", {
          method: "POST",
          headers: { "Content-Type":
                       "application/x-www-form-urlencoded" },
          body: "",
        }),
    sessions: () =>
      // `hidden`: withheld for a demo or a login every visitor shares
      req<S["AuthSessions"]>("/api/sessions"),
    // signing another session out is a posture change — elevation-gated
    sessionRevoke: (body: { all_others?: boolean; id?: string }) =>
      req<S["SessionsRevoked"]>("/api/sessions/revoke", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    deviceRevoke: (body: { id: string }) =>
      req<S["DeviceRevoked"]>("/api/devices/revoke", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    // a household member removes their OWN login (owner + data untouched)
    accountLeave: () =>
      req<S["Ok"]>("/api/account/leave", {
        method: "POST",
        headers: { "Content-Type":
                     "application/x-www-form-urlencoded" },
        body: "",
      }),
    // this login's own daily-email delivery (viewers may)
    meEmail: (muted: boolean) =>
      req<S["MeEmailChanged"]>("/api/me/email", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ muted }) }),
    // one kind of the household's mail on or off for my own inbox
    // this login's own switch for the household's push notices (every
    // role); the phone stays registered, account notices still arrive
    mePush: (muted: boolean) =>
      req<S["MePushChanged"]>("/api/me/push", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ muted }) }),
    meEmailPref: (kind: string, on: boolean) =>
      req<S["MeEmailChanged"]>("/api/me/email", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind, on }) }),
    // wants FRESH elevation: the sheet shows even inside a live window
    accountDelete: () =>
      req<S["Ok"]>("/api/account/delete", {
        method: "POST",
        headers: { "Content-Type":
                     "application/x-www-form-urlencoded" },
        body: "",
      }),
    supportAccess: () =>
      req<S["SupportAccess"]>("/api/support-access"),
    supportAccessGrant: (hours: number, reason: string) =>
      req<S["SupportAccessGranted"]>("/api/support-access", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ hours, reason }),
      }),
    supportAccessRevoke: () =>
      req<S["Ok"]>("/api/support-access/revoke", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }),
    // ---- script tokens (self-host collectors, and the read half the
    // integrations use) ----
    tokens: () =>
      req<{ tokens: OlderServer<S["ApiToken"], "scope">[] }>("/api/tokens"),
    tokenMint: (name: string, scope: TokenScope = "push") =>
      req<S["ApiTokenMinted"]>(
        "/api/tokens", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, scope }),
        }),
    // ---- outbound webhooks (Settings → Integrations on the web) ----
    webhooks: () =>
      req<S["Webhooks"]>("/api/webhooks"),
    webhookCreate: (body: { url: string; events: string[]; name: string }) =>
      req<S["WebhookWithSecret"]>("/api/webhooks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    webhookUpdate: (id: string, body: Partial<Pick<Webhook,
                    "url" | "events" | "name" | "enabled">>) =>
      req<Webhook>(`/api/webhooks/${id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    webhookDelete: (id: string) =>
      req<S["Ok"]>(`/api/webhooks/${id}/delete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }),
    webhookTest: (id: string) =>
      req<S["WebhookTested"]>(
        `/api/webhooks/${id}/test`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        }),
    tokenRevoke: (id: string) =>
      req<S["Ok"]>("/api/tokens/revoke", {
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
        const r0 = await fetchStamped(baseUrl + "/api/connections/export", {
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
      // every stored credential of the instance, sealed — swept like
      // every other download rather than left in the cache for good
      await sweepDownloads(SHARE_GRACE_S);
      const dest = FileSystem.cacheDirectory
        + (m0?.[1] ?? "oikonome-config.oikx");
      await FileSystem.writeAsStringAsync(dest, b64,
        { encoding: "base64" });
      await Sharing.shareAsync(dest);
    },
    connectionsImport: async (file: PickedFile, passphrase: string) => {
      const fd = new FormData();
      fd.append("passphrase", passphrase);
      fd.append("file", await filePart(file.uri, file.name, "application/octet-stream"));
      return withElevation(async () => {
        const r = await fetchStamped(baseUrl + "/api/connections/import",
          { method: "POST", body: fd, signal: epochSignal(),
            headers: { Authorization: `Bearer ${token}` } });
        await checkAuth(r, "POST");
        if (!r.ok) throw await parseError(r);
        return parseOkBody<{ ok: boolean; config: string[];
                             items: number;
                             credentials_dropped?: string[] }>(
          r.status, r.headers.get("content-type"), await r.text());
      });
    },
    // `sent: false` (with `reason`) when the server held the mail back;
    // an older server leaves `sent` out and always sent
    // the owner confirms the paused relay / recipients / AI endpoint are theirs
    outboundResume: (version?: string) =>
      req<S["OutboundResumed"]>("/api/settings/outbound/resume",
        { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ version }) }),
    recipientInviteResend: (email: string) =>
      req<S["RecipientResent"]>("/api/settings/recipients/resend", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      }),
    smtpTest: () =>
      req<S["SmtpTested"]>(
        "/api/settings/smtp-test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        }),
    verifyResend: () =>
      req<S["VerifyResent"]>(
        "/api/verify-email/resend", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        }),
    // ---- household members + invites ----
    members: () =>
      // what each person receives: the owner gets every member's,
      // everyone else only their own row's
      req<Omit<S["HouseholdUsers"], "users">
          & { users: (Omit<S["HouseholdUser"], "notify">
                      & { notify?: MemberNotify | null })[] }>("/api/users"),
    // the owner sets one member's switches — the same places the member's
    // own doors write, so the roster and their screen agree
    memberNotify: (id: string, body: { push_muted?: boolean;
                                       email_muted?: boolean;
                                       email_pref?: { kind: string;
                                                      on: boolean } }) =>
      req<Omit<S["UserNotifyChanged"], "notify"> & { notify: MemberNotify }>(
        `/api/users/${encodeURIComponent(id)}/notify`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body) }),
    // move somebody between "member" (can edit the household's money) and
    // "viewer" (reads everything, changes nothing). Elevation-gated, like
    // every other durable grant.
    memberSetRole: (id: string, role: string) =>
      req<S["UserRoleChanged"]>(
        `/api/users/${encodeURIComponent(id)}/role`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ role }),
        }),
    memberRemove: (id: string) =>
      req<S["Ok"]>(`/api/users/${encodeURIComponent(id)}`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }),
    invites: () =>
      req<S["Invites"]>("/api/invites"),
    // `url` is null on hosted: the server mails the claim link to the
    // address on the invite and keeps it out of the reply, because holding
    // a claimable link is the mailbox proof the claim door runs on. Render
    // the outcome through `inviteOutcome`, never the URL alone.
    inviteCreate: (label: string, role = "viewer") =>
      req<S["InviteCreated"]>("/api/invites", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label, role }),
      }),
    inviteRevoke: (tokenHash: string) =>
      req<S["Ok"]>(
        `/api/invites/${encodeURIComponent(tokenHash)}`,
        { method: "DELETE" }),
    revokeSelf: (id: string) =>
      req<S["DeviceRevoked"]>("/api/devices/revoke", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id }),
      }),
    // the home-screen widget's own credential (one door, dies with this
    // device) and the payload that door serves — the app reads it too,
    // to refresh the widget from the numbers it just showed
    widgetToken: () =>
      req<S["WidgetToken"]>("/api/devices/widget", { method: "POST" }),
    glance: () => req<S["TodayGlance"]>("/api/today/glance"),
    registerPush: (token: string, platform: "fcm" | "apns") =>
      req<S["DevicePushSaved"]>("/api/devices/push", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, platform }),
      }),
    txnBulk: (ids: string[], action: string, category?: string) =>
      req<S["TxnBulkDone"]>(
        "/api/transactions/bulk", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ids, action, category }),
        }),
    categories: () =>
      req<S["Categories"]>("/api/categories"),
    // scope "all" also teaches the merchant (a Rules-page rule); the
    // picker asks explicitly, same as the web
    setCategory: (id: string, category: string, scope: "one" | "all") =>
      req<S["TxnCategorySet"]>(
        `/api/transactions/${encodeURIComponent(id)}/category`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ category, scope }),
        }),
    reimbFlag: (id: string,
                opts: { partial?: boolean;
                        expected?: number | null } = {}) =>
      req<S["ReimbFlagged"]>(`/api/reimburse/${encodeURIComponent(id)}/flag`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(opts),
      }),
    reimbUnflag: (id: string) =>
      req<S["ReimbUnflagged"]>(
        `/api/reimburse/${encodeURIComponent(id)}/unflag`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        }),
    // every pending proposal in one click (the wizard's bills step)
    // `pids` are the offers on screen: one a scan files after the list
    // loaded is never approved unseen
    billsApproveAll: (pids: string[]) =>
      req<{ ok: boolean; approved: number; payees: string[];
            // superseded: withdrawn as already tracked (an older server
            // sends no flag)
            failed: { pid: string; error: string; superseded?: boolean }[];
            notes?: { payee: string; note: string }[] }>(
        "/api/bills/proposals/approve-all", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ pids }),
        }),
    // `note` is present when the approve changed something the person set
    // (a pinned amount cleared) and must be shown to them
    // `adjust` (approve only) is what the person changed in the wizard's
    // Adjust form before approving — only the fields they moved; the
    // finder's evidence stays attached to the bill it creates
    billsProposal: (pid: string, action: "approve" | "reject",
                    adjust?: ProposalAdjust) =>
      req<{ ok: boolean; payee: string; superseded?: boolean; note?: string }>("/api/bills/proposal", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(adjust && action === "approve"
          && Object.keys(adjust).length ? { pid, action, adjust }
                                        : { pid, action }),
      }),
    lensMonth: (y: number, m: number) =>
      req<MonthLensData>(`/api/lens/month?y=${y}&m=${m}`),
    lensYear: (y: number) => req<YearLensData>(`/api/lens/year?y=${y}`),
    // BYO provider keys (self-host): validated live before saving,
    // clearable — the web ConnectHub's endpoints, verbatim
    plaidValidate: (client_id: string, secret: string, env: string) =>
      req<S["KeysSaved"]>("/api/accounts/plaid/validate", {
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
      req<S["KeysSaved"]>("/api/accounts/plaid/keys",
                           { method: "DELETE" }),
    mxKeys: (client_id: string, api_key: string, env: string) =>
      req<{ ok: boolean; env: string }>("/api/accounts/mx/keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ client_id, api_key, env }),
      }),
    mxKeysClear: () =>
      req<S["MxKeysCleared"]>("/api/accounts/mx/keys", { method: "DELETE" }),
    mxConnect: () =>
      req<S["MxConnect"]>("/api/accounts/mx/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }),
    mxSyncNow: () =>
      req<S["MxSynced"]>("/api/accounts/mx/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }),
    budgetSnapshots: () => req<BudgetSnapshots>("/api/budget/snapshots"),
    budgetBackfill: () =>
      req<S["SnapshotsBackfilled"]>("/api/budget/snapshots/backfill",
                                  { method: "POST" }),
    reimbPending: () =>
      req<ReimbPending>("/api/reimburse/pending"),
    reimbCandidates: (id: string, q = "") =>
      req<OlderServer<S["ReimbCandidates"], "pairs" | "anchor">
        & { anchor?: ReimbAnchor; candidates: ReimbCandidate[] }>(
        `/api/reimburse/${encodeURIComponent(id)}/candidates` +
        (q ? `?q=${encodeURIComponent(q)}` : "")),
    reimbLink: (txn_id: string, other_ids: string[], partial = false) =>
      req<OlderServer<S["ReimbLinked"], "txn_id">>("/api/reimburse/link", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ txn_id, other_ids, partial }),
      }),
    // the line beside Pair for the ticked rows, by the link rule (nothing
    // is linked); an older server has no such door
    reimbSelection: (txn_id: string, other_ids: string[], partial = false) =>
      req<S["ReimbSelection"]>("/api/reimburse/selection", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ txn_id, other_ids, partial }),
      }),
    // newest first; `page` (from 1) reaches older pairs, `q` finds one by
    // either side's merchant
    reimbPairs: (page = 1, q = "") => req<ReimbPairsPage>(
      `/api/reimburse/pairs?page=${page}`
      + (q ? `&q=${encodeURIComponent(q)}` : "")),
    reimbUnlink: (expense_id: string, reimburse_id: string) =>
      req<S["ReimbUnlinked"]>("/api/reimburse/unlink", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expense_id, reimburse_id }),
      }),
    // Merchants: what the ledger calls each payee, the raw strings under
    // it, and the journal of user-made changes so each one is undoable.
    // `order` is the screen's sort toggle, honored server-side so the
    // 200-row window is the top of the order asked for, not a re-sorted
    // slice of some other one
    // `sort` and `filter` carry the screen's full choice so a server that
    // applies them before its cap answers over every merchant; one that
    // ignores them still gets `order`, and the screen filters what came back.
    merchantCatalog: (q = "", order: "count" | "alpha" = "count",
                      opts: { sort?: string; filter?: string; limit?: number } = {}) =>
      req<MerchantCatalog>("/api/merchants/catalog?q=" + encodeURIComponent(q)
                           + "&order=" + order
                           + (opts.sort ? "&sort=" + encodeURIComponent(opts.sort) : "")
                           + (opts.filter && opts.filter !== "all"
                              ? "&filter=" + encodeURIComponent(opts.filter) : "")
                           + (opts.limit ? "&limit=" + opts.limit : "")),
    merchantDetail: (display: string) =>
      req<MerchantDetail>("/api/merchants/detail?display="
                          + encodeURIComponent(display)),
    // rename and merge are one call: naming an existing merchant merges
    merchantRename: (display: string, to: string) =>
      req<S["MerchantRenamed"]>("/api/merchants/rename", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display, to }),
      }),
    // approve = merge through the journalled rename (undo works); reject =
    // keep apart, never offered again
    // bulk = "Merge all": a pair the batch already changed answers 409
    // into_id = the survivor the list showed: an offer a sync turned since
    // answers 409 and nothing merges (web: client.ts)
    merchantMerge: (pid: string, action: "approve" | "reject",
                    into_id: string, bulk = false) =>
      req<S["MerchantMerged"]>("/api/merchants/merge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(bulk ? { pid, action, into_id, bulk }
                                  : { pid, action, into_id }),
      }),
    merchantUndo: (id: number) =>
      req<S["MerchantUndone"]>(
        "/api/merchants/undo", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id }),
        }),
    rules: (source: "user" | "llm" | "seed" | "model", q = "", page = 1) =>
      req<S["RulesPage"]>(`/api/rules?source=${source}&q=${
        encodeURIComponent(q)}&page=${page}`),
    ruleDisable: (merchant: string, disabled: boolean) =>
      req<S["Ok"]>("/api/rules/disable", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ merchant, disabled }),
      }),
    ruleDelete: (merchant: string) =>
      req<S["RuleDeleted"]>("/api/rules/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ merchant }),
      }),
    // rename a free-form custom category everywhere it appears
    categoryRename: (oldName: string, newName: string) =>
      req<S["CategoryRenamed"]>(
        "/api/categories/rename", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ old: oldName, new: newName }),
        }),
    ruleSet: (merchant: string, category: string) =>
      req<S["RuleSet"]>("/api/rules/set", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ merchant, category }),
      }),
    // ask_spend: this build asks for a spend when none is set, so the
    // server may answer without projecting one (an older build cannot)
    retirement: (params: Record<string, string> = {}) =>
      req<RetirementData>("/api/retirement?"
        + new URLSearchParams({ ...params, ask_spend: "1" })),
    // debt payoff planner — method/extra are what-if overrides, unsaved
    debtPlan: (params: Record<string, string> = {}) =>
      req<DebtPlan>("/api/debt-plan"
        + (Object.keys(params).length
          ? "?" + new URLSearchParams(params) : "")),
    debtPlanSave: (body: DebtPlanSave) =>
      req<DebtPlan>("/api/debt-plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
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
                       ownership_pct?: number; is_manager?: boolean;
                       is_household?: boolean }) =>
      req<NonNullable<Entity["members"]>[number]>(
        `/api/business/entities/${encodeURIComponent(id)}/members`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
    // ownership_pct null clears the share
    entityUpdateMember: (id: string, memberId: string,
                         body: { member_name?: string;
                                 ownership_pct?: number | null;
                                 is_manager?: boolean;
                                 is_household?: boolean }) =>
      req<NonNullable<Entity["members"]>[number]>(
        `/api/business/entities/${encodeURIComponent(id)}/members/`
          + encodeURIComponent(memberId), {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
    entityRemoveMember: (id: string, memberId: string) =>
      req<{ ok: boolean }>(
        `/api/business/entities/${encodeURIComponent(id)}/members/`
          + encodeURIComponent(memberId), { method: "DELETE" }),
    entityGet: (id: string) =>
      req<Entity>(`/api/business/entities/${encodeURIComponent(id)}`),
    entityPnl: (id: string, year?: number) =>
      req<Pnl>(`/api/business/entities/${encodeURIComponent(id)}/pnl`
        + (year ? `?year=${year}` : "")),
    bizTxns: (id: string, year?: number) =>
      req<{ transactions: BizTxn[] }>(
        `/api/business/entities/${encodeURIComponent(id)}/transactions`
        + (year ? `?year=${year}` : "")),
    /** One page of the worksheet, newest first. `total` is every row in
     *  scope; a server that predates paging omits it and sends them all. */
    bizTxnsPage: (id: string, offset: number, limit: number, year?: number) =>
      req<{ transactions: BizTxn[]; total?: number; limit?: number;
            offset?: number }>(
        `/api/business/entities/${encodeURIComponent(id)}/transactions`
        + `?limit=${limit}&offset=${offset}` + (year ? `&year=${year}` : "")),
    balanceSheet: (id: string) =>
      req<{ assets: { name: string; amount: number }[];
            liabilities: { name: string; amount: number }[];
            total_assets: number; total_liabilities: number; net: number;
            capital_account: number;
            /** the household member's share of capital_account */
            household_share_pct?: number | null;
            household_capital?: number | null;
            /** why capital and net assets differ, the server's sentences
             *  (absent from an older server) */
            reconcile?: string[];
            capital_gap?: number }>(
        `/api/business/entities/${encodeURIComponent(id)}/balance-sheet`),
    entityEquity: (id: string) =>
      req<{ movements: { id: string; kind: string; amount: number;
              date: string | null; form: string | null }[];
            capital: Capital;
            /** owner money moved in or out, offered as a contribution or
             *  a draw (absent from an older server) */
            owner_transfers?: S["OwnerTransfer"][] }>(
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
    vendorList: (id: string, year?: number) =>
      req<{ vendors: Vendor[]; threshold?: number }>(
        `/api/business/entities/${encodeURIComponent(id)}/vendors` +
        (year ? `?year=${year}` : "")),
    // the door stores the TIN it is sent and an omitted one clears it, so
    // a caller toggling `reportable` sends the TIN already on file back
    vendorMark: (id: string, body: { merchant: string;
                   reportable: boolean; tin_last4?: string | null }) =>
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
            // the server's words when the total may hold a purchase twice
            note?: string | null;
            rows: { date: string; payee: string; description: string;
                    qty: number | null; amount: number;
                    receipt_id: string; txn_id: string;
                    suspected?: boolean; note?: string | null }[] }>(
        `/api/receipts/report?tag=${encodeURIComponent(tag)}&y=${y}&m=${m}`),
    bizSuggestions: (id: string) =>
      req<{ lines: string[]; suggestions: {
        txn_id: string; date: string | null; amount: number; payee: string;
        suggested_line: string | null;
        suggested_bucket: string }[]; total?: number }>(
        `/api/business/entities/${encodeURIComponent(id)}/suggestions`),
    // `sched_c_line` null clears the line; leaving it out keeps the stored one
    bizClassify: (id: string, txnId: string,
                  body: { bucket: string; sched_c_line?: string | null }) =>
      req<{ ok: boolean }>(
        `/api/business/entities/${encodeURIComponent(id)}/transactions/${
          encodeURIComponent(txnId)}/class`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
    importBatches: () => req<S["ImportBatches"]>("/api/import/batches"),
    // `forceKept`: the owner's "undo anyway" after the server asked about
    // rows only a note naming no feed account would keep
    importRollback: (batch_id: string, forceKept = false) =>
      req<S["RolledBack"]>(
        "/api/import/rollback", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(forceKept ? { batch_id, force_kept: true }
                                         : { batch_id }),
        }),
    bulkAnalyze: async (files: PickedFile[]) => {
      const fd = new FormData();
      for (const f of files)
        fd.append("files", await filePart(f.uri, f.name, f.mime || "application/octet-stream"));
      const r = await fetchStamped(baseUrl + "/api/import/bulk/analyze",
        { method: "POST", body: fd, signal: epochSignal(),
          headers: { Authorization: `Bearer ${token}` } });
      await checkAuth(r, "POST");
      if (r.status === 413) {
        // the app words its own refusal; a proxy's is an HTML page that
        // says nothing — and one archive is not a folder to split, it
        // belongs on the restore door
        const own = await parseError(r);
        const oneZip = files.length === 1 && /\.zip$/i.test(files[0].name);
        // the app's refusal names the restore door; kept on the error's
        // body so the screen can send the archive there
        if (oneZip && (own.body as { restore_door?: string } | undefined)
              ?.restore_door)
          throw new ApiError(413, "this export is too large to read here — "
            + "restore it on its own (the account owner's)", own.body);
        if (own.detail && own.detail !== r.statusText) throw own;
        throw new ApiError(413, oneZip
          ? "this export is too large to read here — restore it on its own "
            + "(the account owner's)"
          : "these files are too large to send at once — import them in "
            + "smaller batches");
      }
      if (!r.ok) throw await parseError(r);
      // `restore_door` at the top: the batch is one export ZIP and nothing
      // else, and that door restores it whatever its size
      return parseOkBody<{ token: string; files: PlanFile[];
                           restore_door?: string }>(
        r.status, r.headers.get("content-type"), await r.text());
    },
    restoreProgress: () => req<RestoreProgress>("/api/restore/progress"),
    // submits the plan; the files import in the background, and the
    // screen follows the job with bulkJob until it settles
    bulkRun: (planToken: string, files: PlanFile[]) =>
      req<import("./pure.ts").BulkJob>(
        "/api/import/bulk/run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token: planToken, files,
                                 background: true }),
        }),
    // one job by id, or — with none — the household's job in progress
    // (or the one that finished within the hour), else {state: "idle"}
    bulkJob: (jobId?: string) =>
      req<import("./pure.ts").BulkJobStatus>(jobId
        ? `/api/import/bulk/jobs/${encodeURIComponent(jobId)}`
        : "/api/import/bulk/jobs/current"),
    // the file in hand finishes; the rest are skipped
    bulkCancel: (jobId: string) =>
      req<import("./pure.ts").BulkJob>(
        `/api/import/bulk/jobs/${encodeURIComponent(jobId)}/cancel`,
        { method: "POST" }),
    importFile: async (f: PickedFile, accountId: string,
                       amountSign: string, newAccountName?: string) => {
      const fd = new FormData();
      fd.append("file", await filePart(f.uri, f.name, f.mime || "application/octet-stream"));
      fd.append("account_id", accountId);
      fd.append("amount_sign", amountSign);
      if (newAccountName) fd.append("new_account_name", newAccountName);
      // this app can ask where each account of a multi-account YNAB
      // register goes, so the server parks such a file and asks
      fd.append("ynab_choice", "1");
      const r = await fetchStamped(baseUrl + "/api/import",
        { method: "POST", body: fd, signal: epochSignal(),
          headers: { Authorization: `Bearer ${token}` } });
      await checkAuth(r, "POST");
      if (!r.ok) throw await parseError(r);
      return parseOkBody<ImportOutcome>(
        r.status, r.headers.get("content-type"), await r.text());
    },
    // A whole-ledger restore: POST /api/import streams a .zip to disk under
    // the restore cap. The door is step-up gated, so the window is opened
    // before the archive is sent rather than after a refused upload.
    restoreArchive: async (f: PickedFile) => {
      const st = await elevationStatus().catch(() => null);
      if (st && !st.elevated) await elevate(false);
      return withElevation(async () => {
        const fd = new FormData();
        fd.append("file", await filePart(f.uri, f.name, f.mime || "application/zip"));
        fd.append("account_id", "");
        fd.append("amount_sign", "bank");
        const r = await fetchStamped(baseUrl + "/api/import",
          { method: "POST", body: fd, signal: epochSignal(),
            headers: { Authorization: `Bearer ${token}` } });
        await checkAuth(r, "POST");
        if (r.status === 413)
          throw new ApiError(413, "This export is larger than the instance "
            + "accepts — restores take up to 500 MB, and a proxy in front "
            + "of the instance may cap uploads lower.");
        if (!r.ok) throw await parseError(r);
        return parseOkBody<ImportOutcome>(
          r.status, r.headers.get("content-type"), await r.text());
      });
    },
    importMapped: (mapToken: string, cols: Record<string, string>) =>
      req<S["ImportResultReply"]>("/api/import/mapped", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: mapToken, ...cols }),
      }),
    importYnabAccounts: (mapToken: string,
                         accounts: Record<string, YnabDestination>,
                         removeEarlier: Record<string, boolean>) =>
      req<S["ImportResultReply"]>("/api/import/ynab-accounts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: mapToken, accounts,
                               remove_earlier: removeEarlier }),
      }),
    taxdocAnalyze: async (f: PickedFile) => {
      const fd = new FormData();
      fd.append("file", await filePart(f.uri, f.name, f.mime || "application/octet-stream"));
      const r = await fetchStamped(baseUrl + "/api/import/taxdoc/analyze",
        { method: "POST", body: fd, signal: epochSignal(),
          headers: { Authorization: `Bearer ${token}` } });
      await checkAuth(r, "POST");
      if (!r.ok) throw await parseError(r);
      return parseOkBody<TaxdocPlan>(
        r.status, r.headers.get("content-type"), await r.text());
    },
    taxdocCommit: (planToken: string, rows: TaxdocRow[]) =>
      req<S["TaxdocCommitted"]>("/api/import/taxdoc/commit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: planToken, rows }),
      }),
    taxdocW2s: () => req<S["StoredW2s"]>("/api/import/taxdoc/w2s"),
    // a removal or owner change names the W-2 by what the list showed: an
    // id comes back after a removal, and a stale list's id may name another
    // form, which the server refuses (409). In the body, never the URL.
    taxdocW2Delete: (w: StoredW2) =>
      req<{ ok: boolean }>(`/api/import/taxdoc/w2s/${w.id}`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ seen: w2Seen(w) }),
      }),
    taxdocW2Owner: (w: StoredW2, owner: "primary" | "spouse") =>
      req<{ ok: boolean }>(`/api/import/taxdoc/w2s/${w.id}/owner`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ owner, seen: w2Seen(w) }),
      }),
    // the stored documents: a removal or a put-back names the document by
    // what the list showed (`seen`, in the body), so a stale list is
    // refused (web same)
    taxdocStored: () => req<S["StoredDocs"]>("/api/import/taxdoc/stored"),
    taxdocStoredEffect: (d: StoredDoc) =>
      req<S["StoredDocEffect"]>(`/api/import/taxdoc/stored/${d.id}/effect`),
    taxdocStoredDelete: (d: StoredDoc) =>
      req<S["StoredDocRemoved"]>(`/api/import/taxdoc/stored/${d.id}`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ seen: d.seen }),
      }),
    taxdocStoredRestore: (d: StoredDoc) =>
      req<S["StoredDocRemoved"]>(`/api/import/taxdoc/stored/${d.id}/restore`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ seen: d.seen }),
      }),
    // the payer's name on a stored document, under the stored name listed
    // (a server older than the door lists no `payer`, so it is not offered)
    taxdocStoredPayer: (d: StoredDoc, name: string) =>
      req<S["StoredDocRemoved"]>(`/api/import/taxdoc/stored/${d.id}/payer`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ seen: d.seen, payer: d.payer, name }),
      }),
    syncStart: () =>
      req<S["SyncStarted"]>(
        "/api/jobs/sync/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        }),
    syncStatus: () => req<SyncStatus>("/api/jobs/sync/status"),
    connections: () =>
      req<S["Connections"]>(
        "/api/connections"),
    holdings: () => req<S["Holdings"]>("/api/holdings"),
    // `forceKept`: the owner's "delete anyway" after the server asked
    // about rows only a note naming no feed account keeps
    accountRemove: (accountId: string, mode: AccountRemoveMode,
                    forceKept = false) =>
      req<{ ok: boolean; mode: string; plaid_released: boolean;
            note: string; affected_accounts: string[];
            // a purge's counts; rows the bank feed relies on are kept,
            // and its note says so
            purged?: { kept_transactions?: number; note?: string } | null }>(
        `/api/accounts/${encodeURIComponent(accountId)}/remove`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(forceKept ? { mode, force_kept: true }
                                         : { mode }),
        }),
    connectionDisconnect: (id: string) =>
      req<{ ok: boolean; plaid_released: boolean; note: string }>(
        `/api/connections/${encodeURIComponent(id)}/disconnect`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        }),
    // bring a disconnected MX or SimpleFIN bank back; MX fetches the gap on
    // its next sync, SimpleFIN pulls it at once (refused if the bridge lost it)
    connectionReconnect: (id: string) =>
      req<{ ok: boolean; resync_from: string }>(
        `/api/connections/${encodeURIComponent(id)}/reconnect`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        }),
    // per-institution sync. A failed or refused pull comes back as an
    // error status, so it lands in the caller's onError as an error notice
    // rather than a success notice that happens to say "failed". A server
    // older than the JSON door answers a bare 404; that one falls back to
    // the form door and its ?msg= redirect.
    accountSyncItem: async (itemId: string) => {
      try {
        return await req<{ ok: boolean; message: string }>(
          "/api/accounts/sync", {
            method: "POST", signal: epochSignal(),
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ item_id: itemId }) });
      } catch (e) {
        if (!(e instanceof ApiError && e.status === 404
              && e.detail === "Not Found")) throw e;
      }
      const r = await fetchStamped(baseUrl + "/accounts/sync", {
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
    // relinkItemId: a linked bank to link afresh with the household's
    // (wider) history window; it stays live until the new link of the
    // same bank is saved (409 while a relink of it is already open)
    plaidLinkStart: (itemId = "", manageAccounts = false,
                     relinkItemId = "") =>
      req<{ link_token: string; hosted_link_url: string;
            kind: "add" | "update"; institution: string | null }>(
        "/accounts/plaid/link", {
          method: "POST",
          headers: { "Content-Type":
                       "application/x-www-form-urlencoded" },
          body: new URLSearchParams({
            ...(itemId ? { item_id: itemId } : {}),
            ...(manageAccounts ? { manage_accounts: "1" } : {}),
            ...(relinkItemId && !itemId ? { relink_item_id: relinkItemId } : {}),
          }).toString(),
        }),
    plaidLinkStatus: (linkToken: string) =>
      req<{ done: boolean;
            kind?: "add" | "add_failed" | "update" | "update_failed" | "busy"
                 | "exited";
            institution?: string; error?: string;
            // "pending": saved, its first pull queued on the worker
            first_sync?: "pending" | "ok";
            // a session that came back with several banks: each one, and
            // the sentence that words them all (also on a busy answer
            // while the person may still add another)
            results?: { institution: string | null; connected: boolean;
                        error?: string }[];
            summary?: string;
            // a busy several-bank session: the banks connected so far, as
            // the row under the button says it
            progress?: string;
            // answered because the session went quiet: the bank window
            // may still be open (a bank added there is taken later)
            quiet?: boolean }>(
        // v=2: this app keeps its in-app browser open on a quiet answer
        `/accounts/plaid/link/${encodeURIComponent(linkToken)}/status?v=2`),
    simplefinConnect: (sfToken: string) =>
      req<S["SimplefinConnected"]>(
        "/api/accounts/simplefin", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token: sfToken }),
        }),
    accountLinks: () =>
      req<S["AccountLinks"]>(
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
      req<S["AccountAdded"]>("/api/accounts/add", {
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
      req<S["Ok"]>("/api/accounts/balance", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_id, balance }),
      }),
    // answers the label as stored: a typed card number keeps its last four
    accountRename: (account_id: string, name: string) =>
      req<S["AccountRenamed"]>("/api/accounts/rename", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_id, name }),
      }),
    // primary_checking left out = the household's anchor stays put
    accountClassify: (account_id: string, kind: string,
                      primary_checking?: boolean) =>
      req<S["Ok"]>("/api/accounts/classify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_id, kind,
          ...(primary_checking === undefined ? {} : { primary_checking }) }),
      }),
    accountExclude: (account_id: string, excluded: boolean) =>
      req<S["AccountExcluded"]>("/api/accounts/exclude", {
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
      req<S["AccountOwnerSet"]>(
        `/api/accounts/${encodeURIComponent(account_id)}/owner`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ owner }),
        }),
    owners: () => req<S["Owners"]>("/api/owners"),
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
      req<S["Merchants"]>("/api/merchants"),
    billsBill: (payee: string) =>
      req<BillDetail>(`/api/bills/bill?payee=${encodeURIComponent(payee)}`),
    billsSave: (body: {
      payee: string; amount: string | number; cadence?: string;
      by_month_day?: number[];
      next_due?: string; income?: boolean; orig_payee?: string;
      cap?: string | number | null; category?: string | null;
      show_today?: boolean; match_category?: boolean;
      txn_category?: string; companions?: Companion[];
      merchants?: string[]; restore?: boolean }) =>
      req<{ ok: boolean; payee: string; restored?: boolean }>(
        "/api/bills/save", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
    // `disabled` names the target state and is required: a bare flip is
    // decided against whatever the server holds, so a stale screen or a
    // retried request would toggle the bill the opposite way to the button
    // the person pressed (web too)
    // the household's word on whether one payer's deposits are paychecks
    // ("auto": the rules decide) — the web's incomePayer
    incomePayer: (key: string, count_as: "paycheck" | "never" | "auto",
                  name?: string) =>
      req<S["IncomePayerSaved"]>("/api/income/payer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key, count_as, name }),
      }),
    billsToggle: (payee: string, disabled: boolean) =>
      req<{ ok: boolean; disabled: boolean }>("/api/bills/toggle", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ payee, disabled }),
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
    billsHistory: (payee: string, window: string = "all") =>
      req<BillsHistoryData>(
        `/api/bills/history?payee=${encodeURIComponent(payee)}&window=${encodeURIComponent(window)}`),
    billsHint: (payee: string, action: "dismiss" | "restore" = "dismiss",
                target: "fit" | "health" = "fit") =>
      req<{ ok: boolean; dismissed: boolean }>("/api/bills/hint", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ payee, action, target }),
      }),
    // bulk category override for a whole (canonical) merchant —
    // preview → confirm-with-count → apply → undo, like the web.
    // `family: false` sends `?canon_only=1` (the server's flag), scoping
    // the preview to the single canonical merchant with no family
    // expansion — the scope the txn screen's "All <payee>" write uses. The
    // query-arg name is the server contract; the two must match. Default
    // (family scope) is what the bill-history "Apply to whole merchant"
    // previews.
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
    // who a written check paid, asked once (specs/ledger-figures.md):
    // written as its note, and run through the check-image machinery
    checkPayee: (id: string, payee: string) =>
      req<S["CheckPayeeSaved"]>(
        `/api/transactions/${encodeURIComponent(id)}/check-payee`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ payee }),
        }),
    setTxnNote: (id: string, note: string) =>
      req<S["TxnNoteSaved"]>(
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
      req<S["PendingRetired"]>(
        "/api/transactions/retire-pending", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id }),
        }),
    // an owner transfer on a business account, recorded as a contribution
    // (money in) or a draw (money out) at the row's own amount and date
    bizOwnerTransfer: (entityId: string, txnId: string) =>
      req<S["EquityMovement"]>(
        `/api/business/entities/${encodeURIComponent(entityId)}/owner-transfer`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ txn_id: txnId }),
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
    // receipts on a transaction, with the charge's own amount and date
    // (null when it is gone) — the web client's shape
    receipts: (txnId: string) =>
      req<{ receipts: Receipt[];
            txn?: { amount: number; date: string } | null }>(
        `/api/transactions/${encodeURIComponent(txnId)}/receipts`),
    receiptUpload: async (txnId: string, uri: string,
                          kind: "receipt" | "check" = "receipt",
                          name = "receipt.jpg", type = "image/jpeg") => {
      const fd = new FormData();
      // React Native's FormData takes a file descriptor object; name/type
      // default to the camera JPEG and carry a picked PDF's real identity
      fd.append("file", await filePart(uri, name, type));
      fd.append("kind", kind);
      const r = await fetchStamped(
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
                    // null on a receipt still waiting for its charge;
                    // its store may be unknown too
                    txn_id: string | null; date: string;
                    payee: string | null;
                    // false: listed, not added up (a copy of a slip
                    // counted on another receipt, or off the ledger);
                    // an older server sends neither
                    counts?: boolean;
                    why?: "same_slip" | "off_ledger" | null;
                    // counted, and another counted receipt reads alike:
                    // the totals may hold it twice; `note` is the words
                    // printed beside the line, decided by the server
                    suspected?: boolean;
                    note?: string | null }[];
            // key null: the store group for an unknown store
            // bought: quantity; receipts: the slips (receipts.search_items)
            // note: the server's words when the total may hold a
            // purchase twice
            groups: { key: string | null; n: number; bought: number;
                      receipts: number; total: number;
                      last_date?: string | null;
                      note?: string | null }[];
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
    // a receipt snapped before its transaction: stored waiting, parsed in
    // the background, paired with its charge when the charge lands
    receiptSnap: async (uri: string, name = "receipt.jpg",
                        type = "image/jpeg") => {
      const fd = new FormData();
      fd.append("file", await filePart(uri, name, type));
      const r = await fetchStamped(`${baseUrl}/api/receipts`,
        { method: "POST", body: fd, signal: epochSignal(),
          headers: { Authorization: `Bearer ${token}` } });
      if (!r.ok) throw await parseError(r);
      return parseOkBody<WaitingReceipts & { id: string }>(
        r.status, r.headers.get("content-type"), await r.text());
    },
    receiptsWaiting: (offset = 0) => req<WaitingReceipts>(
      "/api/receipts/waiting" + (offset > 0 ? `?offset=${offset}` : "")),
    receiptMatch: (id: string, txnId: string) =>
      req<WaitingReceipts>(
        `/api/receipts/${encodeURIComponent(id)}/match`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ txn_id: txnId }),
        }),
    // a receipt's total, date and (optionally) store typed in by hand; a
    // waiting one is matched at once, and txn_id says where it went
    receiptDetails: (id: string, body: { total: number; date: string;
                                         merchant?: string }) =>
      req<WaitingReceipts & { id: string; txn_id: string | null }>(
        `/api/receipts/${encodeURIComponent(id)}/details`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
    // a person's answer about two receipts read alike: one slip (counted
    // once) or two purchases (each counted); stored, never asked again
    receiptSlipAnswer: (id: string, other: string, same: boolean) =>
      req<S["ReceiptSlipAnswered"]>(
        `/api/receipts/${encodeURIComponent(id)}/`
          + (same ? "same-slip" : "two-purchases"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ other }),
        }),
    receiptUnmatch: (id: string) =>
      req<WaitingReceipts & { txn_id: string }>(
        `/api/receipts/${encodeURIComponent(id)}/unmatch`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        }),
    // one charge, several categories: the parts a person wrote for a row
    // (stored category keys + cent-exact amounts summing to the charge).
    // PUT replaces the whole split; a 400 carries the reason in words.
    txnSplitSet: (txnId: string, parts: SplitPart[]) =>
      req<S["SplitSaved"]>(
        `/api/transactions/${encodeURIComponent(txnId)}/split`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ parts }),
        }),
    txnSplitClear: (txnId: string) =>
      req<S["SplitSaved"]>(
        `/api/transactions/${encodeURIComponent(txnId)}/split`,
        { method: "DELETE" }),
    receiptParse: (id: string) =>
      req<{ status: string }>(`/api/receipts/${id}/parse`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }),
    notifyTest: (channel: "sms" | "push") =>
      req<S["NotifyTested"]>("/api/notify/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ channel }),
      }),
    calendar: (days = 35) =>
      req<{ days: number; start: string;
            // came_in / went_out: absent from an older server
            calendar: { date: string; total: number; came_in?: number;
                        went_out?: number;
                        // name / note: absent from an older server
                        events: { label: string; amount: number;
                                  kind: string; name?: string;
                                  note?: string | null }[] }[] }>(
        `/api/calendar?days=${days}`),
    doctor: () =>
      req<S["DoctorReport"]>("/api/doctor"),
    // capital contributions and reimbursements whose charge was deleted
    doctorUnlinked: () =>
      req<{ movements: { id: string; entity_id: string;
            entity_name: string | null; kind: string; amount: number;
            date: string | null }[] }>(
        "/api/doctor/unlinked-movements"),
    doctorUnlinkedRemove: (id: string) =>
      req<{ ok: boolean }>(
        `/api/doctor/unlinked-movements/${encodeURIComponent(id)}`,
        { method: "DELETE" }),
    doctorScripts: () =>
      req<S["ScriptHeartbeats"]>("/api/doctor/scripts"),
    doctorScriptUpdate: (source: string,
                         body: { expected_hours?: number;
                                 alerts?: boolean }) =>
      req<S["Ok"]>(
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
        fd.append("screenshot", await filePart(shot.uri, shot.name, shot.mime || "image/jpeg"));
      const r = await fetchStamped(baseUrl + "/api/testing/feedback",
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
    // an older server's topics carry no group
    help: () => req<{ topics: OlderServer<S["HelpTopic"], "group">[] }>(
      "/api/help"),
    // which delivery channels can actually deliver — gates the SMS/push
    // toggles the way the web Settings page does
    notifyStatus: () =>
      req<OlderServer<S["NotifyStatus"], "sms_entitled" | "phone_paused_at"
              | "native_push_available" | "native_push_devices"
              | "invites_need_base_url">>(
        "/api/notify"),
    notifyPhone: (number: string, consent: string[] = []) =>
      req<S["NotifyPhoneSet"]>("/api/notify/phone", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ number, consent }),
      }),
    notifyPhoneVerify: (code: string) =>
      req<S["Ok"]>("/api/notify/phone/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
      }),
    jobsEmail: () =>
      req<S["EmailSent"]>("/api/jobs/email", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }),
    settingsFull: () =>
      req<SettingsView>("/api/settings"),
    // Plaid Enrich for imported history — status + a capped run (owner)
    enrichStatus: () =>
      req<S["EnrichStatus"]>("/api/import/enrich"),
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
    // the same, with the planner's take-home box as typed: the server
    // measures the savings suggestion on it only when history names no
    // income (an older server ignores it and sends no suggestion)
    budgetsSuggestTyped: (income: string) =>
      req<BudgetSuggestions>("/api/budgets/suggest?"
                             + new URLSearchParams({ income })),
    // the household's zone ("" = follow the instance); the server refuses a
    // name it cannot load, so a typo never silently moves the clock
    settingsSaveTimezone: (timezone: string, adopted = false) =>
      req<SettingsView>("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // an automatic adopt says so: the server refuses it once the
        // household chose a zone by hand
        body: JSON.stringify(adopted ? { timezone, timezone_adopted: true }
                                     : { timezone }),
      }),
    // local = the bundled/env backend or a loopback/compose host: nothing
    // leaves the box; otherwise host names where the questions go
    assistantStatus: () =>
      req<AssistantStatus>("/api/assistant"),
    // `answer` is the prose alone; `blocks` the ordered text + chart /
    // table pieces the screen draws (components/answer-blocks); absent
    // on an older server
    // the household's answered questions, newest first — the same log
    // the web shows, so a question asked there is here too
    // the log pages by (asked_at, id): `next` is the cursor for the page
    // after this one, and both halves go back on the next read
    assistantHistory: (cursor?: HistoryCursor | null) =>
      req<{ turns: AssistantTurn[]; more: boolean; next?: HistoryCursor | null }>(
        "/api/assistant/history"
        + (cursor ? `?before=${encodeURIComponent(cursor.before)}`
           + (cursor.before_id
             ? `&before_id=${encodeURIComponent(cursor.before_id)}` : "") : "")),
    // drops one conversation — every turn of the thread (a 404 when
    // nothing was left to drop: someone removed it first)
    assistantForgetThread: (threadId: string) =>
      req<{ ok: boolean; deleted: number }>(
        `/api/assistant/threads/${encodeURIComponent(threadId)}`, { method: "DELETE" }),
    // drops one turn: a server that predates conversations has only this
    // door, and there a conversation's id is its one turn's id
    assistantForgetTurn: (turnId: string) =>
      req<{ ok: boolean; deleted: number }>(
        `/api/assistant/history/${encodeURIComponent(turnId)}`, { method: "DELETE" }),
    assistantClear: () =>
      req<{ ok: boolean; deleted: number }>(
        "/api/assistant/history", { method: "DELETE" }),
    // a follow-up names the conversation it continues (`threadId`); the
    // model then sees that thread's earlier turns and nothing else. A 404
    // means that conversation no longer exists; `saved: false` or
    // `removed: true` means the answer was kept in no conversation
    assistantAsk: (question: string, threadId?: string | null) =>
      req<{ answer: string; blocks?: AnswerBlock[]; tools_used: string[];
            id?: string | null; thread_id?: string | null;
            saved?: boolean; removed?: boolean }>(
        "/api/assistant", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(threadId ? { question, thread_id: threadId } : { question }),
        }),
    // one answer as a self-contained HTML page (the server re-validates
    // the blocks and draws the charts), written to the cache and handed
    // to the share sheet like every other download — swept later, not
    // deleted under the target app
    // a whole conversation — its questions and answers in order — as
    // one self-contained HTML file on the share sheet
    // `partial`: the conversation's start is not loaded, so the page
    // says earlier questions are missing. The first turn also rides as
    // the single-answer body (`question` + `blocks`) a server older
    // than conversations understands — it then renders that answer
    // alone instead of refusing with "nothing to report".
    assistantReport: async (turns: { question: string; blocks: AnswerBlock[] }[],
                            partial = false) => {
      const r = await fetchStamped(baseUrl + "/api/assistant/report", {
        method: "POST", signal: epochSignal(),
        headers: { Authorization: `Bearer ${token}`,
                   "Content-Type": "application/json" },
        body: JSON.stringify({ turns, partial, question: turns[0]?.question ?? "",
                               blocks: turns[0]?.blocks ?? [] }),
      });
      await checkAuth(r, "POST");
      if (!r.ok) throw await parseError(r);
      const page = await r.text();
      const FileSystem = await import("expo-file-system/legacy");
      const Sharing = await import("expo-sharing");
      const m0 = /filename="?([^";]+)/.exec(r.headers.get("content-disposition") ?? "");
      await sweepDownloads(SHARE_GRACE_S);
      const dest = FileSystem.cacheDirectory + (m0?.[1] ?? "oikonome-conversation.html");
      await FileSystem.writeAsStringAsync(dest, page);
      await Sharing.shareAsync(dest, { mimeType: "text/html", UTI: "public.html" });
    },
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
  /** the sign-in code the confirmed emailed link showed — only a sign-in
   *  carrying it spends the unlock, so a stranger who knows the address
   *  cannot spend it first */
  unlockProof = "",
): Promise<{ token: string; id: string }> {
  const login = await fetchStamped(baseUrl + "/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded",
               Accept: "application/json" },
    body: new URLSearchParams({
      email, password, totp_code: totpCode,
      ...(unlockProof ? { unlock_proof: unlockProof } : {}) }).toString(),
  });
  if (!login.ok) throw await parseError(login);
  return mintDeviceFromSession(baseUrl, login, deviceName, platform,
                               replaceDeviceId);
}

/** The emailed answer to the human check. When the server demands a
 *  Turnstile the app cannot render (human_check_failed after a few wrong
 *  passwords), proving control of the account's mailbox passes the same
 *  check: the server's /unlock door mails a one-time link that opens a
 *  short exemption for THAT account, after which the ordinary password
 *  sign-in above works again. It is not a sign-in — the link carries no
 *  session — so the app only asks for the mail and says what to do next.
 *  The server answers 200 whether or not the address exists (the web page
 *  does the same), so there is nothing to read back but the status. The
 *  page the link opens shows a sign-in code; the person types it here and
 *  the sign-in carries it, since only that spends the unlock.
 *
 *  A REFUSAL is not JSON either: /unlock is a page route, so its 429 (the
 *  door's hourly cap) and its 404 (a demo instance, which has no such
 *  door) arrive as the server's HTML error page. parseError would find no
 *  `detail` there and fall back to the status line, which the sign-in
 *  screen would then print raw — so the status is turned into words by
 *  unlockRefusal below, the way every /api door's JSON `detail` already
 *  reaches the screen in words. */
export async function requestUnlock(baseUrl: string, email: string):
    Promise<void> {
  const r = await fetchStamped(baseUrl + "/unlock", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ email }).toString(),
  });
  if (!r.ok) throw new ApiError(r.status, unlockRefusal(r.status));
}

/** The words for an unlock refusal. The one a person actually meets is the
 *  door's own hourly cap — it sends mail, so it is capped — and it gets
 *  what the server's own 429 page says. A demo instance has no such door
 *  at all (404). Anything else names the status, which is at least
 *  something to report, where an empty status line is nothing. */
export function unlockRefusal(status: number): string {
  if (status === 429)
    return "Too many attempts from your connection in a short time — "
      + "wait a few minutes and try again.";
  if (status === 404)
    return "This server doesn't offer emailed sign-in links.";
  return `Couldn't send the sign-in link (error ${status}) — `
    + "try again in a moment.";
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
  return mintDeviceWithGrant(
    baseUrl, { ticket: mintTicket, cookie, issuedAt: Date.now() },
    deviceName, platform, replaceDeviceId);
}

/** Spend a login's grant on a device token. Also the device-limit
 *  picker's retry: the server rolls a refused mint back, so the ticket and
 *  session from the login that hit the ceiling are still good, and the
 *  retry must not run the login again — for an account with a second
 *  factor that login already used its one-time code. A refusal carries
 *  the grant back on the error so the caller can retry with it. */
export async function mintDeviceWithGrant(
  baseUrl: string, grant: MintGrant, deviceName: string, platform: string,
  replaceDeviceId = "",
): Promise<{ token: string; id: string }> {
  const withCookie: Record<string, string> =
    grant.cookie ? { Cookie: grant.cookie } : {};
  const mint = await fetchStamped(baseUrl + "/api/devices", {
    method: "POST",
    headers: { "Content-Type": "application/json",
               Accept: "application/json", ...withCookie },
    body: JSON.stringify({ device_name: deviceName, platform,
                           ...(replaceDeviceId ? { replace: replaceDeviceId } : {}),
                           ...(grant.ticket ? { mint_ticket: grant.ticket } : {}) }),
  });
  if (!mint.ok) {
    const err = await parseError(mint);
    err.mintGrant = grant;
    throw err;
  }
  // through the same guard as every other 2xx: a captive Wi-Fi portal
  // answers this POST with its own 200 HTML sign-in page, and a bare
  // .json() on that throws a SyntaxError that is not an ApiError — the
  // login screen's error branch would print the parser's words instead of
  // the one message written for exactly this situation.
  const minted = parseOkBody<S["DeviceMinted"]>(
    mint.status, mint.headers.get("content-type"), await mint.text());
  // A server that took the ticket has already closed the login session
  // while spending it; this logout closes it on servers older than that.
  // It must never carry the new device token: logout revokes a device
  // token it is shown.
  await fetchStamped(baseUrl + "/api/logout", {
    method: "POST", headers: withCookie,
  }).catch(() => { /* cookie session dies on its own if this misses */ });
  return { token: minted.token, id: minted.id };
}

/** Walk away from an unspent grant (the person cancelled the device
 *  picker, the mint was refused for good, or a fall-back sign-in is about
 *  to replace it): spend the mint ticket without minting, which closes
 *  the session that login opened. The ticket is the only handle on that
 *  session where no cookie reached this code (iOS). The logout after it
 *  covers servers older than the abandon door (they answer it 404) and
 *  grants that carry only a cookie. Best-effort: otherwise the ticket
 *  expires in minutes and the session idles out. */
export async function abandonMintGrant(baseUrl: string, grant: MintGrant):
    Promise<void> {
  let closed = false;
  if (grant.ticket)
    closed = await fetchStamped(baseUrl + "/api/devices/abandon", {
      method: "POST",
      headers: { "Content-Type": "application/json",
                 Accept: "application/json" },
      body: JSON.stringify({ mint_ticket: grant.ticket }),
    }).then((r) => r.ok, () => false);
  // Without a cookie of its own the logout would ride whatever session
  // the platform jar holds — a fresh sign-in's, mid-mint — so it is sent
  // bare only when the ticket did not already close the session.
  if (closed && !grant.cookie) return;
  await fetchStamped(baseUrl + "/api/logout", {
    method: "POST", headers: grant.cookie ? { Cookie: grant.cookie } : {},
  }).catch(() => { /* the session expires on its own */ });
}

/** How long a file handed to the share sheet is left for the target app
 *  to read before a sweep may delete it. */
export const SHARE_GRACE_S = 600;

/** Delete the files this app left in its cache for the share sheet —
 *  exports, dumps, PDFs, receipts, the connections bundle — that are
 *  older than `maxAgeS` (0: all of them). Downloads sweep their
 *  predecessors, a launch sweeps whatever the last session left, and a
 *  sign-out sweeps everything, so a household's ledger never sits
 *  unencrypted in the cache after the person is done with it. Never
 *  throws: a cache that cannot be listed is not a reason to refuse. */
export async function sweepDownloads(maxAgeS: number): Promise<void> {
  try {
    const FileSystem = await import("expo-file-system/legacy");
    const dir = FileSystem.cacheDirectory;
    if (!dir) return;
    const names = await FileSystem.readDirectoryAsync(dir);
    const files = [];
    for (const name of names) {
      if (!isSharedDownload(name)) continue;
      const info = await FileSystem.getInfoAsync(dir + name);
      if (info.exists && !info.isDirectory)
        files.push({ name, mtimeS: info.modificationTime });
    }
    for (const name of downloadsToSweep(files, Date.now() / 1000, maxAgeS))
      await FileSystem.deleteAsync(dir + name, { idempotent: true });
    // what the pickers copied in and an upload did not clear (the app
    // was closed mid-upload, or the file was picked and never sent). An
    // hour at least, short of a sign-out: the import screen holds its
    // picked files across analyze and run.
    const pickAge = maxAgeS <= 0 ? 0 : Math.max(maxAgeS, 3600);
    for (const sub of PICKER_SCRATCH) {
      const info = await FileSystem.getInfoAsync(dir + sub);
      if (!info.exists || !info.isDirectory) continue;
      for (const name of await FileSystem.readDirectoryAsync(dir + sub)) {
        const f = await FileSystem.getInfoAsync(dir + sub + name);
        if (pickAge <= 0 || (f.exists && (f.modificationTime ?? 0)
                               < Date.now() / 1000 - pickAge))
          await FileSystem.deleteAsync(dir + sub + name, { idempotent: true });
      }
    }
  } catch { /* best-effort */ }
}

/** Delete a picked file's copy in the picker's cache folder once its
 *  upload has settled. Anything outside those folders (a file the app
 *  does not own) is left alone. Never throws. */
export async function discardPicked(uri: string | null | undefined):
    Promise<void> {
  if (!uri) return;
  try {
    const FileSystem = await import("expo-file-system/legacy");
    if (isPickerScratch(uri, FileSystem.cacheDirectory))
      await FileSystem.deleteAsync(uri, { idempotent: true });
  } catch { /* best-effort */ }
}

/** Take this phone's device token off the server as it signs out. The
 *  logout door accepts the token itself and stays reachable under every
 *  account lockout, so this works from the lockout screens too, where
 *  the device list and its revoke door are closed. Best-effort and
 *  bounded: a sign-out must still finish with the server unreachable. */
export async function revokeDeviceToken(baseUrl: string, token: string):
    Promise<boolean> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 5000);
  try {
    const r = await fetchStamped(baseUrl + "/api/logout", {
      method: "POST", signal: ctl.signal,
      headers: { Authorization: `Bearer ${token}` } });
    // a 401 means the server no longer knows the token: already revoked
    // or expired, so there is nothing left to retry
    return r.ok || r.status === 401;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

/** WebAuthn request options for a passkey sign-in (discoverable flow —
 *  no email; the credential sheet picks the account). 404 on servers
 *  without passkey login (self-host); the caller reads that as "this
 *  server doesn't offer passkeys". */
export async function passkeyLoginOptions(baseUrl: string): Promise<{
  challengeId: string; publicKey: Record<string, unknown>;
}> {
  const r = await fetchStamped(baseUrl + "/api/login/passkey/options", {
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
  const login = await fetchStamped(baseUrl + "/api/login/passkey", {
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
    const r = await fetchStamped(baseUrl + "/healthz",
                          { headers: { Accept: "application/json" } });
    if (!r.ok) return false;
    const body = await r.json();
    return body?.ok === true;
  } catch {
    // no answer inside the read limit included: not a server to sign in to
    return false;
  }
}
