import type { components } from "./api.generated";
import { busyText, failCause, fetchRead, readFinished, restartingText,
         UNKNOWN_FIGURE } from "../moneymath";
import type { EmailKind } from "../recipients";
import type { AnswerBlock } from "../components/AssistantBlocks";
// the server's documented shapes (api.generated.ts, specs/api-types.md)
type S = components["schemas"];
// Thin typed API client. Session cookie rides automatically (same-origin);
// every 401 funnels to the login screen via the AuthError sentinel.

export class AuthError extends Error {
  /** read by the shared failed-read words and retry rule (moneymath.ts) */
  readonly status = 401;
}

// Session elevation ("sudo mode"): a security-posture route answers 403
// {error: "elevation_required", fresh} instead of taking a password in its
// body; the wrapper asks the mounted ElevationProvider to re-prove identity
// (passkey, else password + code — the same proofs as before, so a session
// thief still cannot elevate) and then retries the request ONCE. `fresh`
// is the two routes that always ask again (delete, the full export).
// a hosted passkey-only account's methods read ["passkey",
// "recovery+password"]: its lost-key fallback is a recovery code sent WITH
// the password
export type Elevation = S["Elevation"];
type Elevator = (fresh: boolean) => Promise<void>;
let elevator: Elevator | null = null;
/** The provider registers itself here; with none mounted the 403 surfaces
 *  as it is. */
export function setElevator(fn: Elevator | null): void { elevator = fn; }

/** `null` unless this 403 body is the elevation sentinel; else its `fresh`.
 *  FastAPI wraps an HTTPException detail as {"detail": {...}}, so both
 *  shapes are read. */
function elevationRequired(body: string): boolean | null {
  try {
    const j = JSON.parse(body);
    const d = j?.detail?.error !== undefined ? j.detail : j;
    return d?.error === "elevation_required" ? !!d.fresh : null;
  } catch { return null; }
}

// Clearing the query cache does not cancel an in-flight fetch: a response
// issued under the previous account could land after a sign-out and its
// onSuccess would write that household's data into the cache the next
// account is already reading. Every request binds to the epoch's signal;
// the auth-lost handler aborts the epoch before it clears the cache.
let epoch = new AbortController();
export function abortRequestEpoch(): void {
  epoch.abort();
  epoch = new AbortController();
}
/** The current epoch's signal — every fetch in this module, the direct
 *  ones included, binds to it. */
export function epochSignal(): AbortSignal { return epoch.signal; }

/** fetch bound to the request epoch — every network call in this module
 *  goes through here, the multipart/form ones included — and the one
 *  place the web puts a limit on a read (specs/screen-states.md, "A read
 *  that never answers"): a GET with no answer in READ_LIMIT_MS fails as a
 *  request that could not reach the server does. A write is never timed
 *  (fetchRead says why). `wait: "unlimited"` is for a file the server
 *  builds whole before its first byte, which is a download the person
 *  asked for and is watching, not a screen left on "Loading…". */
async function rawFetch(path: string, init?: RequestInit,
                        wait: "limited" | "unlimited" = "limited"): Promise<Response> {
  try {
    // the epoch signal wins unless a caller brought its own
    const outer = init?.signal ?? epoch.signal;
    const go = (signal: AbortSignal | undefined) =>
      fetch(path, { credentials: "same-origin", ...init, signal });
    return wait === "unlimited" ? await go(outer)
      : await fetchRead(go, init?.method, outer);
  } catch (e) {
    // an aborted epoch IS a sign-out: surface it as the auth loss it
    // is, never as an "AbortError" toast on whatever was in flight. A
    // read abandoned at the limit is not one: it arrives as a TypeError
    if ((e as { name?: string })?.name === "AbortError")
      throw new AuthError("signed out");
    throw e;
  }
}

/** rawFetch plus the elevation round trip: a 403 elevation_required waits
 *  for the sheet, then the same request goes out once more. Cancelling the
 *  sheet rejects with a plain Error (no status), so a mutation's onError
 *  shows a sentence, not a toast about a 403. Bodies here are strings or
 *  FormData, both replayable. */
async function elevatedFetch(path: string, init?: RequestInit): Promise<Response> {
  const r = await rawFetch(path, init);
  if (r.status !== 403 || !elevator) return r;
  // the elevate endpoint itself can 403 (a script token has no session);
  // that one must not recurse into the sheet
  if (path.startsWith("/api/auth/elevat")) return r;
  const body = await r.clone().text().catch(() => "");
  const fresh = elevationRequired(body);
  if (fresh === null) return r;
  // the refusal has been read; the request that goes out next is its own
  readFinished(r);
  await elevator(fresh);
  return rawFetch(path, init);
}

/** Thrown by a door that carries a single-use code once the elevation
 *  sheet has run in front of it. The sheet's proof can spend the same
 *  authenticator step as the code already typed, so resending the request
 *  could only be refused as a replay; the form asks for the next code. */
export class NextCodeNeeded extends Error {
  constructor() { super("enter the next code from your authenticator app"); }
}

/** Like req, but never replays the request after the sheet: the body holds
 *  a one-time code, so an elevation refusal raises the sheet and then
 *  throws NextCodeNeeded for the caller to collect a fresh code. */
async function reqOneTimeCode<T>(path: string, init: RequestInit): Promise<T> {
  const r = await rawFetch(path, init);
  if (r.status === 403 && elevator) {
    const fresh = elevationRequired(await r.clone().text().catch(() => ""));
    if (fresh !== null) {
      await elevator(fresh);
      throw new NextCodeNeeded();
    }
  }
  return settle<T>(r);
}

// exported for the client add-on's own endpoints (src/ext)
export async function req<T>(path: string, init?: RequestInit): Promise<T> {
  return settle<T>(await elevatedFetch(path, init));
}

async function settle<T>(r: Response): Promise<T> {
  // once the body is read, the read stops watching the epoch for it
  try { return await settled<T>(r); } finally { readFinished(r); }
}

async function settled<T>(r: Response): Promise<T> {
  if (r.status === 401)
    // carry the server detail: callers distinguish "totp_required" /
    // "wrong password" from a plain signed-out 401
    throw new AuthError(await r.text().catch(() => "not signed in"));
  const ct = r.headers.get("content-type") ?? "";
  if (r.ok) return (ct.includes("json") ? r.json() : r.text()) as Promise<T>;
  const body = await r.text().catch(() => "");
  const retryAfter = r.headers.get("Retry-After");
  // A gateway status whose body is not the app's own JSON comes from the
  // proxy in front of it: the app is restarting (a deploy, more capacity)
  // or down, and the proxy answers with its "back in a moment" HTML page.
  // That page is for a browser tab, not for an error sentence.
  // An empty gateway answer is the same thing from a proxy with no page.
  if (GATEWAY.has(r.status) && (!body.trim() || !appBody(ct, body)))
    throw new BusyError(restartingText(retryAfter), retryAfter, r.status, true);
  if (r.status === 503) throw new BusyError(body, retryAfter);
  // any other refusal a proxy wrote as a page (a CDN's 520, an edge's
  // 413) keeps its status for the callers that branch on it, in words
  if (!appBody(ct, body))
    throw new Error(`${r.status}: ${pageText(r.status, body)}`);
  throw new Error(`${r.status}: ${body}`);
}

const GATEWAY = new Set([502, 503, 504]);

/** Whether a refusal's body is the app's own words — JSON, or plain text
 *  that is not a markup document. The proxy labels its page text/html;
 *  the content-type alone is not trusted, because an edge in front can
 *  rewrite it, so a body that opens like markup counts as a page too. */
function appBody(ct: string, body: string): boolean {
  if (/html/i.test(ct)) return false;
  const t = body.trimStart();
  if (t.startsWith("{") || t.startsWith("[")) {
    try { JSON.parse(t); return true; } catch { return false; }
  }
  return !t.startsWith("<");
}

/** The sentence for a refusal that arrived as an error page rather than
 *  in the app's words: a gateway status means the server is restarting,
 *  anything else names the status — empty bodies keep what little they
 *  had, which is nothing worse than the status itself. */
function pageText(status: number, body: string): string {
  if (GATEWAY.has(status)) return restartingText(null);
  if (!body.trim()) return "";
  return "The server answered with an error page — try again.";
}

/** A 503: the server is shedding load (sign-ins hashing at once, the
 *  database full) — not a wrong answer and not an outage. The message
 *  keeps the "503: body" shape every other refusal has; `retryAfter` is
 *  the server's Retry-After header when it sent one.
 *
 *  `restarting` marks the other kind: a 502/503/504 the proxy answered
 *  because the app itself was not there to. Its body was an HTML page, so
 *  the message carries the sentence for it instead, under its own status
 *  (a 502 stays a 502 for anyone reading the prefix). */
export class BusyError extends Error {
  retryAfter: string | null;
  status: number;
  restarting: boolean;
  constructor(body: string, retryAfter: string | null,
              status = 503, restarting = false) {
    super(`${status}: ${body}`);
    this.retryAfter = retryAfter;
    this.status = status;
    this.restarting = restarting;
  }
}

// busyText / restartingText live in the screen-states twin block
// (moneymath.ts), so the failed-read line and the sign-in page say the
// same words as the phone.
export { busyText, restartingText };

/** Say something from anywhere on the page. The toast is fixed to the
 *  viewport, so a result reaches the reader wherever they acted — a note
 *  rendered at the top of a long page is off screen from row 200, which
 *  made approving a bill or unlinking a pair look like a no-op. */
export function toast(text: string, to?: string): void {
  window.dispatchEvent(new CustomEvent("oiko-toast",
                                       { detail: to ? { text, to } : text }));
}

/** The sentence a server error actually contains.
 *
 *  `req` throws `Error("400: {\"detail\":\"…\"}")` so callers keep the
 *  status; rendered verbatim, that is a raw JSON blob in front of the
 *  user. */
export function errText(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e);
  const m = /^(\d{3}):\s*([\s\S]*)$/.exec(raw);
  const body = (m ? m[2] : raw).trim();
  try {
    const parsed = JSON.parse(body);
    const d = parsed?.detail ?? parsed?.message;
    if (typeof d === "string" && d) return d;
    if (Array.isArray(d) && d[0]?.msg) return String(d[0].msg);
  } catch { /* not JSON — the body IS the message */ }
  // settle() already words a page it was handed; this catches a markup
  // body that reached an Error some other way (an add-on door, a caller
  // wrapping a raw response), so no document is ever read out as a
  // sentence
  if (body.startsWith("<")) {
    const status = m ? Number(m[1]) : 0;
    const page = status ? pageText(status, body) : restartingText(null);
    if (page) return page;
  }
  // no answer, a 5xx or a bare refusal: the cause in the app's words,
  // never "TypeError: Failed to fetch" or a status on its own
  return failCause(e, "web") ?? (body || raw);
}

export const json = <T = { ok: boolean }>(path: string, body: unknown) =>
  req<T>(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });

const form = (data: Record<string, string>) => {
  const b = new URLSearchParams();
  for (const [k, v] of Object.entries(data)) b.set(k, v);
  return {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: b.toString(),
  };
};

// the pages a household may open each client on — the setting stores the
// route (web) or tab name (mobile); anything else falls back to Today.
// Shared by App (the redirect) and Settings (the picker).
export const WEB_HOMES: [string, string][] = [
  ["/", "Today"], ["/transactions", "Transactions"], ["/bills", "Bills"],
  ["/accounts", "Accounts"], ["/networth", "Net worth"],
  ["/budget", "Budget"], ["/cashflow", "Cash flow"],
  ["/reimburse", "Reimburse"], ["/business", "Business"],
];
export const MOBILE_HOMES: [string, string][] = [
  ["index", "Today"], ["transactions", "Transactions"], ["bills", "Bills"],
  ["accounts", "Accounts"], ["more", "More"],
];

// The continuity packet's doors, generated from the server's documented
// shapes (specs/api-types.md).
export type Continuity = S["Continuity"];
export type ContinuitySaved = S["ContinuitySaved"];
export type ContinuityEmailed = S["ContinuityEmailed"];

/** GET /api/me: the signed-in login, read on every page load
 *  (web/schemas/household.py). */
export type Me = S["Me"];
export type NotifyStatus = S["NotifyStatus"];
export type Invite = S["InvitePending"];
/** What one household member receives — their email switches (on the
 *  household's mail, muted, which kinds) and their push switch with the
 *  devices it governs. The owner gets every member's; everyone else only
 *  their own row's (null on the rest). */
export type MemberNotify = S["MemberNotify"];
export type Member = S["HouseholdUser"];
// the household activity log: one hand-authored act per row, the sentence
// composed by the server so every client reads the same words
export type ActivityRow = S["ActivityRow"];
export type ActivityPage = S["ActivityPage"];
export type Alert = S["Alert"];
export type AlertRow = S["AlertRow"];
// The ledger row is ONE shape across server, web and mobile: the server's
// Txn model (web/schemas/ledger.py), generated like every other shape.
export type Txn = S["Txn"];
export type Series = S["Series"];
/** One plan row as the server builds it (todayview._plan_rows). */
export type ServerPlanRow = S["ServerPlanRow"];
export type PlanTotal = S["PlanTotal"];
export type RowStatus = S["RowStatus"];

/** Today's cash notice: `kind` picks the sentence, `amount` the dollar it
 *  quotes, `until` the date it runs to; `ask` (no paycheck scheduled)
 *  replaces the alarm with the missing setup step. */
export type CashNotice = S["CashNotice"];

export type TodayFull = S["TodayFull"];

// ---- timeframe lenses (/api/lens/*) — views composed from the
//      engine by web/lenses.py ----
// "net" = a closed month with no budget snapshot: the year grid shows net
// income − spending for it instead of a budget verdict
export type LensStatus = "final" | "in_progress" | "future" | "projected"
  | "net";
export type LensTxn = S["LensTxn"];
export interface WeekBucket {
  actual: number; week_budget: number; expected: number;
  children: { name: string; actual: number; week_budget: number;
              expected: number }[];
  display_actual: number; remaining_budget: number;
}
export type MonthLensData = S["MonthLensData"];
export type YearCell = S["YearCell"];
// Budget history (the Budget page's card): each frozen month's variable
// budgets, how it was captured ("close" = frozen as the month ran,
// "backfill" = the owner applied a later budget retroactively), plus how
// many closed months since the ledger began have no snapshot.
export type BudgetSnapshots = S["BudgetSnapshots"];
export type YearTotals = S["YearTotals"];
export type YearLensData = S["YearLensData"];

export type Proposal = S["BillProposal"];
export type Bill = S["Bill"];
export type Companion = S["Companion"];
export type BillsData = S["BillsData"];

// /api/bills/bill — one bill's editable fields + payee-keyed config
export type BillDetail = S["BillDetail"];

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
  // the signed change the folded side makes
  merged?: S["MergedFacts"] | null;
  from_logo?: string | null; into_logo?: string | null;
  from_rows?: number | null; into_rows?: number | null;
  from_samples: string[]; into_samples: string[];
  signals: string[]; supports: string[];
  // "Merge all" may take it (one name is the other cut short); any other
  // offer carries the caution it is shown with, one at a time
  bulk: boolean; caution: string | null;
}
export type LedgerFit = S["LedgerFit"];
// history rows are the Transactions-page shape (same renderer) plus the
// matcher verdict and a legacy id alias
export type HistoryTxn = S["HistoryTxn"];
export type BillsHistoryData = S["BillsHistory"];

// ---- reports (/api/reports/*) ----
// Generated from the server's documented shapes (specs/api-types.md).
/** A P/L row of the Net Worth page: a crypto account against the cash put
 *  in and taken out, or a long-term account against the money invested. */
export type PLRow = S["CoinbasePl"] | S["InvestedPl"];
export type InvestedGroup = S["InvestedGroup"];
/** The Accounts page's figures, server-decided (reporting.account_totals):
 *  the headline is the Net Worth page's Financial accounts figure; loans
 *  are listed under it as owed; section and institution subtotals per
 *  pane, as the bank states each balance. */
export type AccountTotals = S["AccountTotals"];
/** One Over-time timeframe over `full_trend`: the points drawn
 *  (start..end), the gain between them and its percent. */
export type TrendWindow = S["TrendWindow"];
export type RingFigures = S["RingFigures"];
export type NetWorthReport = S["NetWorthReport"];
export type SpendingReport = S["SpendingReport"];
// where the money came from and where it went, for one period — income by
// kind on the left, fixed / variable / saved on the right; same predicates
// as every other figure on the page
export type FlowPeriod = S["FlowPeriod"];
// served per range (the site-wide 3m/6m/1y/3y/5y/all vocabulary), not in
// the report — the fixed split walks months and 'all' costs seconds
export type FlowKey = "cur" | "1m" | "3m" | "6m" | "1y" | "3y" | "5y" | "all";
// the Spending tab's windowed view — ranked lists each carry the previous
// equal window's figure (null when the range is 'all': no previous window)
export type SpendingWindow = S["SpendingWindow"];
export type Change = S["Change"];
export type SpendingMover = S["SpendingMover"];
export type IncomeKinds = S["IncomeKinds"];
export type IncomeWindow = S["IncomeWindow"];
export type SourcePaycheck = S["SourcePaycheck"];
export type CashflowReport = S["CashflowReport"];
/** Investment funding month-to-date and year-to-date; 0 when a server
 *  does not send them. */
export const brokerageFunding = (cf: CashflowReport) => ({
  mtd: cf.brokerage_funding_mtd ?? 0,
  ytd: cf.brokerage_funding_ytd ?? 0,
});
export type FeesReport = S["FeesReport"];
/** Interest and fees paid on bank and card accounts, by year — what
 *  holding money costs, net of what the bank refunded. Empty `by_year`
 *  means the section has nothing to say. */
export type CashCostYear = S["CashCostYear"];
export type CashCostsReport = S["CashCostsReport"];

// ---- retirement (/api/retirement) and debt (/api/debt-plan) ----
// The Retirement and Debt pages' doors, generated from the server's
// documented shapes (specs/api-types.md). Where the projection's spend and
// saving came from: a what-if, the saved answers, the ledger's year, or
// (spend) "needed": nothing to plan for, nothing projected.
export type RetirementData = S["RetirementData"];
export type RetirementInputs = S["RetirementInputs"];
export type RetSpendSource = RetirementInputs["spend_source"];
export type RetSavingSource = RetirementInputs["saving_source"];
export type DebtPlan = S["DebtPlan"];
export type DebtPlanSave = S["DebtPlanSave"];
export type DebtRow = S["DebtRow"];
export type DebtSchedule = S["DebtSchedule"];
export type DebtMethod = DebtPlan["method"];

export type BizData = S["BizData"];

export type PendingReimb = S["PendingReimb"];
export type ReimbCandidate = S["ReimbCandidate"];
/** The candidates door's anchor: what it still needs (`left_amount`) and
 *  what already came back to it (`received`), the figures the matcher
 *  words its rows and its gap line by. */
export type ReimbAnchor = S["ReimbAnchor"];
export type ReimbPairsPage = S["ReimbPairsPage"];
export type ReimbPair = S["ReimbPair"];

// An account a refusal asks the person to choose, described by what tells
// two cards sharing their last four apart when their names read the same.
export type AccountChoice = S["AccountChoice"];
export type ImportResult = S["ImportResult"];
export type RestoreProgress = S["RestoreProgress"];
// A folder import runs as a background job: the run door answers with its
// status at once and the page polls it until it settles.
export type BulkJobFile = S["BulkJobFile"];
export type BulkJob = S["BulkJob"];
export type BulkJobStatus = BulkJob | { state: "idle" };
export const bulkJobLive = (s?: { state: string } | null): boolean =>
  !!s && (s.state === "queued" || s.state === "running"
          || s.state === "cancelling");
// A CSV whose columns could not be read: the person maps them.
export type ColumnMappingNeeded = S["ColumnMappingNeeded"];
// One YNAB account a register names; `name` is the key the answer uses
// ("" for lines with no account name), `label` what the person reads.
export type YnabAccountGroup = S["YnabAccountGroup"];
// A YNAB register of several accounts: the person says where each goes.
export type YnabAccountsNeeded = S["YnabAccountsNeeded"];
export type MappingNeeded = ColumnMappingNeeded | YnabAccountsNeeded;
// Where one YNAB account goes: an account id, "skip", or a new account.
// an account id, "skip", a new account, or an account picked from those a
// typed new-account name could mean, with that name (remembered under it)
export type YnabDestination = string | { new: string }
  | { id: string; typed: string };
export type ImportOutcome = S["ImportOutcome"];
export type Batch = S["ImportBatch"];

export type CalendarData = S["CalendarData"];
export type ForecastScorePanel = S["ForecastScorePanel"];
export type ForecastMiss = S["ForecastMiss"];

export type WizardStep =
  "appearance" | "connect" | "bills" | "budgets" | "email" | "finish"
  // legacy marks still accepted by the API if present in old configs
  | "sync" | "import";
// script tokens — plaintext appears only in the mint response.
// scope: push = the import doors (a collector); read = the integrations
// doors (a scraper, a sensor, an MCP server). Never both.
export type TokenScope = S["ApiToken"]["scope"];
export type ApiToken = S["ApiToken"];

// outbound webhooks (Settings → Integrations) — the secret appears only
// in the create and rotate responses
export type Webhook = S["Webhook"];
export type WebhookEvent = S["IntegrationEvent"];
export type WebhookDelivery = S["WebhookDelivery"];

export type Onboarding = S["Onboarding"];
/** How far back a new bank link (and a file import) reaches, in months;
 *  null = everything the source offers. */
export type HistoryMonths = 1 | 3 | 6 | 12 | null;
/** How bills and budgets are set up: "auto" = the finder proposes and the
 *  person approves; "manual" = typed in by hand, and the finder stays off
 *  (bill_detection_auto false) until turned on from Bills. */
export type SetupMode = "auto" | "manual";
/** What an Adjust changes on a proposal before approving it. cadence is
 *  the FREQ:interval wire format the bill editor sends; due_day is the
 *  day of the month (1-31). */
export type ProposalAdjust = S["ProposalAdjust"];
// live progress of the background sync the wizard starts
export type SyncProgressItem = S["SyncItem"];
export type BackfillStatus = S["BackfillStatus"];

export type SyncStatus = S["SyncStatus"];
export type HistoryCoverage = S["HistoryCoverage"];

export type Session = S["AuthSession"];

export type MobileDevice = S["MobileDevice"];

export type DoctorCheck = S["DoctorCheck"];

/** One part of a hand-split charge (Txn.split) */
export type SplitPart = S["SplitPart"];

export type ReceiptItem = S["ReceiptItem"];
// tax & income documents
export type TaxdocRow = S["TaxdocRow"];
export type W2Match = S["W2Match"];
export type TaxdocPlan = S["TaxdocPlan"];
// a stored W-2; "unassigned" = nobody has said whose it is yet, and it
// counts toward nobody's wages until someone does
/** What the stored list showed for a W-2, sent back with a removal or an
 *  owner change so a reused id never acts on another form. */
export const w2Seen = (w: StoredW2) => ({
  year: w.year, box1: w.box1, employer: w.employer, ein: w.ein,
  owner: w.owner });

export type StoredW2 = S["StoredW2"];
// every stored tax and income document, in the server's words
export type StoredDoc = S["StoredDoc"];
export type StoredDocs = S["StoredDocs"];

// items view
export type ReceiptItemRow = S["ReceiptItemRow"];
export type ReceiptItemGroup = S["ReceiptItemGroup"];
/** What an unknown store is called on the Receipts page. */
export const UNKNOWN_STORE = "Unknown store";

export type Receipt = S["Receipt"];
export type ReceiptTwin = S["ReceiptTwin"];

// receipts snapped before their transaction reached the ledger
export type WaitingCandidate = S["WaitingCandidate"];
export type WaitingReceipt = S["WaitingReceipt"];
export type RecentMatch = S["RecentMatch"];
export type WaitingReceipts = S["WaitingReceipts"];

export type AccountLinkInfo = S["AccountLinkInfo"];
export type LinkSuggestion = S["LinkSuggestion"];
export type LinkGroupMember = S["LinkGroupMember"];
export type LinkGroup = S["LinkGroup"];

/** The linked-sources card is status the owner may put away; it comes
 *  back by itself when there is something to decide — a pair that looks
 *  like the same account, or a linked source that went down. */
export const linkCardVisible = (d: { groups: LinkGroup[];
    suggestions: LinkSuggestion[]; card_dismissed: boolean }) =>
  d.suggestions.length > 0
  || d.groups.some((g) => g.members.some((m) => !m.healthy))
  || (!d.card_dismissed && d.groups.length > 0);

export interface TestingState {
  enabled: boolean; can_email: boolean; feedback_to: string;
}

export type ScriptHeartbeat = S["ScriptHeartbeat"];

export type Account = S["Account"];
export type AccountRemoveMode =
  | "hide" | "disconnect" | "disconnect_purge" | "purge";
export interface AccountRemoveResult {
  ok: boolean; mode: AccountRemoveMode; account_id: string;
  item_id: string | null; plaid_released: boolean; note: string;
  purged: { accounts: number; transactions: number;
            // rows the bank feed relies on are kept, and the note says so
            kept_transactions?: number; note?: string } | null;
  affected_accounts: string[];
}
export type Entity = S["Entity"];
export type EntityDeleteImpact = S["EntityDeleteImpact"];
export type Capital = S["Capital"];
export type EquityMovement = S["EquityMovement"];
export type BizTxn = S["BizTxn"];
export type Deduction = S["Deduction"];
export type Pnl = S["Pnl"];
export type Obligation = S["Obligation"];
export type EstimatedTax = S["EstimatedTax"];
export type BalanceSheet = S["BalanceSheet"];
export type MileageTrip = S["MileageTrip"];
export type Vendor = S["Vendor"];
export type BusinessSummary = S["BusinessSummary"];
export type Connection = S["Connection"];
export type Holding = S["Holding"];
// config manual_assets[] — property/vehicle/mortgage entries, valued by
// hand on Net Worth
export type ManualAsset = S["ManualAsset"];
// config custom_buckets[]: a named carve-out of a parent variable
// bucket — categories + merchant overrides route spend into it for display
export type CustomBucket = S["CustomBucket"];
// /api/budgets/suggest — trailing 6-month medians (current month excluded,
// outlier months >2× the median excluded), one number per bucket
export type BudgetSuggestions = S["BudgetSuggestions"];
export type Settings = S["SettingsView"];

// income scenarios + ledger-driven savings goals
export type IncomeScenario = S["IncomeScenario"];
export type SavingsGoal = S["SavingsGoal"];
/** a member an owner's credential change (or recovery) removed: they
 *  joined in the last days through the owner's own invites */
export interface RemovedMember { email: string; role: string; joined: string }

export type SavingsGoalProgress = S["SavingsGoalProgress"];

// the server's shape (web/schemas/ledger.py MerchantDetail), generated
export type MerchantDetail = S["MerchantDetail"];

// The server applies up to 10,000 ids in one bulk write, all or nothing,
// so a selection up to that size goes in ONE request: split, a failure
// part-way would leave the first pieces applied and the rest not. Past
// BULK_MAX it is not offered at all: a write that size is a rule, not a
// selection. BULK_CHUNK stays the server's cap so the loop below can only
// split what one request could never carry.
export const BULK_MAX = 10000;
export const BULK_CHUNK = BULK_MAX;

// The last /api/me answer's day counts. Kept here, beside the one fetch
// that delivers them, so every sentence that names a number of days — the
// allowance note in the extension slot included, which is not a hook —
// reads the instance's value instead of writing its own.
let dayCounts: Me["day_counts"] | undefined = undefined;
const posInt = (v: unknown): number | null =>
  typeof v === "number" && Number.isInteger(v) && v > 0 ? v : null;
/** Days of failure before a connection is removed; null when the
 *  instance does not remove them. An older server says nothing: 30, the
 *  default. */
/** One bank a Plaid Link session came back with: connected, or not and
 *  why (the server's sentence). */
export type PlaidLinkBank = { institution: string | null; connected: boolean;
                              error?: string };

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

export const api = {
  me: () => req<Me>("/api/me").then((m) => {
    dayCounts = m?.day_counts; return m; }),
  // pre-auth; only a single-user demo-locked instance ever returns creds
  // how a person without an account (or locked out of one) gets help
  // on this instance — public, read before any login
  access: () => req<S["AccessInfo"]>("/api/access"),
  demoLogin: () =>
    req<S["DemoLogin"]>("/api/demo-login"),
  login: (email: string, password: string, totp?: string) =>
    req<S["SignedIn"]>("/api/login",
      form({ email, password, ...(totp ? { totp_code: totp } : {}) })),
  // Sign-out goes through signOut() in push.ts, which names this
  // browser's push endpoint so the server drops its subscription too.
  logout: (push_endpoint?: string) =>
    json<S["Ok"]>("/api/logout", push_endpoint ? { push_endpoint } : {}),
  // date (optional, YYYY-MM-DD) renders a past day — omit for live today
  todayFull: (date?: string) =>
    req<TodayFull>(`/api/today/full${date ? `?date=${date}` : ""}`),
  // how close the cash forecast has landed (specs/forecast-grading.md)
  forecastScore: () => req<ForecastScorePanel>("/api/today/forecast-score"),
  forecastMiss: (day: string, horizon = 30) =>
    req<ForecastMiss>(`/api/today/forecast-score/miss?day=${day}&horizon=${horizon}`),
  // timeframe lenses
  lensMonth: (y: number, m: number) =>
    req<MonthLensData>(`/api/lens/month?y=${y}&m=${m}`),
  lensYear: (y: number) => req<YearLensData>(`/api/lens/year?y=${y}`),
  budgetSnapshots: () => req<BudgetSnapshots>("/api/budget/snapshots"),
  budgetBackfill: () =>
    req<S["SnapshotsBackfilled"]>("/api/budget/snapshots/backfill",
                                { method: "POST" }),
  // A selection larger than one request (past the server's cap) goes in
  // pieces of at most BULK_CHUNK, one after another, with the tallies added
  // up. A piece that fails stops the walk and the error says how many were
  // already written, because the pieces before it stay applied.
  txnBulkChunked: async (ids: string[], action: string, category?: string,
                         onProgress?: (done: number, total: number) => void) => {
    const tally = { applied: 0, missing: 0, flow: 0 };
    let done = 0;
    onProgress?.(0, ids.length);
    for (let i = 0; i < ids.length; i += BULK_CHUNK) {
      const piece = ids.slice(i, i + BULK_CHUNK);
      let r: { applied: number; missing: number; flow: number };
      try {
        r = await api.txnBulk(piece, action, category);
      } catch (e) {
        if (done === 0) throw e;
        throw new Error(`stopped after ${done} of ${ids.length} — ${errText(e)}`);
      }
      tally.applied += r.applied ?? 0;
      tally.missing += r.missing ?? 0;
      tally.flow += r.flow ?? 0;
      done += piece.length;
      onProgress?.(done, ids.length);
    }
    return tally;
  },
  txnBulk: (ids: string[], action: string, category?: string) =>
    json<S["TxnBulkDone"]>(
      "/api/transactions/bulk", { ids, action, category }),
  // An authorization the bank left pending and never posted or released.
  // Sync can't drop it — an absent row is indistinguishable from one the
  // feed simply didn't return this time — so only the user can retire it.
  // The server refuses a row that is not pending or is younger than two
  // weeks; its detail text is what the caller shows.
  retirePending: (id: string) =>
    json<S["PendingRetired"]>(
      "/api/transactions/retire-pending", { id }),
  transactions: (params: Record<string, string>) =>
    req<S["TxnPage"]>(
      "/api/transactions?" + new URLSearchParams(params)),
  // Merchants: the display name, the raw strings under it, and the
  // journal of user-made changes so each is undoable.
  // `order` is the screen's sort toggle, honored server-side so the
  // 200-row window is the top of the order asked for, not a re-sorted
  // slice of some other one. `variant_count` is the TRUE number of source
  // strings; `variants` is capped by the server, so the count can exceed
  // the array's length and only it may be printed as a fact.
  // `sort` and `filter` carry the screen's full choice so a server that
  // applies them before its cap answers over every merchant; one that
  // ignores them still gets `order`, and the page filters what came back.
  merchantCatalog: (q = "", order: "count" | "alpha" = "count",
                    opts: { sort?: string; filter?: string; limit?: number } = {}) =>
    req<S["MerchantCatalog"]>(
      "/api/merchants/catalog?" + new URLSearchParams({
        q, order,
        ...(opts.sort ? { sort: opts.sort } : {}),
        ...(opts.filter && opts.filter !== "all" ? { filter: opts.filter } : {}),
        ...(opts.limit ? { limit: String(opts.limit) } : {}) })),
  // approve = merge through the journalled rename (undo works); reject =
  // keep apart, never offered again
  // bulk = "Merge all": a pair the batch already changed answers 409
  // into_id = the survivor the list showed: an offer a sync turned since
  // answers 409 and nothing merges
  merchantMerge: (pid: string, action: "approve" | "reject",
                  into_id: string, bulk = false) =>
    json<S["MerchantMerged"]>("/api/merchants/merge",
                              bulk ? { pid, action, into_id, bulk }
                                   : { pid, action, into_id }),
  // one merchant, opened: facts, category, and where it has been seen
  merchantDetail: (display: string) =>
    req<S["MerchantDetail"]>("/api/merchants/detail?"
                        + new URLSearchParams({ display })),
  // rename and merge are one call: naming an existing merchant merges
  merchantRename: (body: { display: string; to: string }) =>
    json<S["MerchantRenamed"]>("/api/merchants/rename", body),
  merchantUndo: (id: number) =>
    json<S["MerchantUndone"]>(
      "/api/merchants/undo", { id }),
  // categories: in use by this tenant (filters). plaid_*: the standard
  // Plaid primaries, for pickers that set a category.
  categories: () => req<S["Categories"]>("/api/categories"),
  // Rename a free-form / custom category string everywhere it appears
  // (overrides, merchant rules, budget carve-outs). Owner-only.
  categoryRename: (oldName: string, newName: string) =>
    json<S["CategoryRenamed"]>(
      "/api/categories/rename", { old: oldName, new: newName }),
  // primary_checking_id: the account the cash forecast starts from (the
  // live pin, else the largest-balance pick) — the ledger heads itself
  // with its balance; absent from older servers. primary_pin_unused: why
  // the saved pin is not that account, in the page's words (null when it is)
  accounts: () => req<S["AccountsData"]>("/api/accounts"),
  // primary_checking left out = the household's anchor stays put
  accountClassify: (account_id: string, kind: string,
                    primary_checking?: boolean) =>
    json<S["Ok"]>("/api/accounts/classify", { account_id, kind,
      ...(primary_checking === undefined ? {} : { primary_checking }) }),
  accountAdd: (name: string, kind: string, balance = "") =>
    json<S["AccountAdded"]>("/api/accounts/add",
                                              { name, kind, balance }),
  // answers the label as stored: a typed card number keeps its last four
  accountRename: (account_id: string, name: string) =>
    json<S["AccountRenamed"]>("/api/accounts/rename",
                                                { account_id, name }),
  accountExclude: (account_id: string, excluded: boolean) =>
    json<S["AccountExcluded"]>(
      "/api/accounts/exclude", { account_id, excluded }),
  /** Hide/unhide one account without touching its institution connection.
   *  Hidden = excluded from every total, list and sync (see the endpoint). */
  accountSetHidden: (account_id: string, hidden: boolean) =>
    json<{ ok: boolean; account_id: string; hidden: boolean; note: string }>(
      `/api/accounts/${encodeURIComponent(account_id)}/hidden`, { hidden }),
  // `forceKept`: the owner's "delete anyway" after the server asked about
  // rows only a note naming no feed account keeps
  accountRemove: (account_id: string, mode: AccountRemoveMode,
                  forceKept = false) =>
    json<AccountRemoveResult>(
      `/api/accounts/${encodeURIComponent(account_id)}/remove`,
      forceKept ? { mode, force_kept: true } : { mode }),
  // household ownership attribution
  owners: () => req<S["Owners"]>("/api/owners"),
  setAccountOwner: (account_id: string, owner: string) =>
    json<S["AccountOwnerSet"]>(`/api/accounts/${encodeURIComponent(account_id)}/owner`, { owner }),
  setTxnOwner: (txn_id: string, owner: string) =>
    json<S["TxnOwnerSet"]>(`/api/transactions/${encodeURIComponent(txn_id)}/owner`, { owner }),
  connections: () =>
    req<S["Connections"]>(
      "/api/connections"),
  holdings: () => req<S["Holdings"]>("/api/holdings"),
  // manual per-connection pull. A failed or refused pull is an error
  // status, so it lands in the caller's onError, not its success notice
  accountSync: (item_id: string) =>
    json<S["AccountSynced"]>("/api/accounts/sync", { item_id }),
  accountBalance: (account_id: string, balance: string) =>
    json<S["Ok"]>("/api/accounts/balance", { account_id, balance }),
  simplefinConnect: (token: string) =>
    json<S["SimplefinConnected"]>(
      "/api/accounts/simplefin", { token }),
  settings: () => req<Settings>("/api/settings"),
  // Plaid Enrich for imported history — status + a capped run (owner)
  // awaits_confirm: the run waits on the owner confirming their address
  // (remaining is then 0), as connecting a bank does
  enrichStatus: () => req<S["EnrichStatus"]>("/api/import/enrich"),
  enrichRun: (limit = 100) =>
    json<S["EnrichRun"]>("/api/import/enrich", { limit }),
  onboarding: () => req<Onboarding>("/api/onboarding"),
  // script tokens (bearer credentials for host-side collectors)
  tokens: () => req<S["ApiTokens"]>("/api/tokens"),
  // both are elevation-gated (a durable credential): no password in the
  // body — the wrapper's sheet is the proof
  tokenMint: (name: string, scope: TokenScope = "push") =>
    json<S["ApiTokenMinted"]>("/api/tokens", { name, scope }),
  tokenRevoke: (id: string) => json<S["Ok"]>("/api/tokens/revoke", { id }),
  // outbound webhooks — create and a new URL are elevation-gated (the
  // ledger gets posted there); the rest are plain owner doors
  webhooks: () => req<S["Webhooks"]>("/api/webhooks"),
  webhookCreate: (body: { url: string; events: string[]; name: string }) =>
    json<S["WebhookWithSecret"]>("/api/webhooks", body),
  webhookUpdate: (id: string, body: Partial<Pick<Webhook,
                  "url" | "events" | "name" | "enabled">>) =>
    json<Webhook>(`/api/webhooks/${id}`, body),
  webhookDelete: (id: string) =>
    json<S["Ok"]>(`/api/webhooks/${id}/delete`, {}),
  webhookTest: (id: string) =>
    json<S["WebhookTested"]>(`/api/webhooks/${id}/test`, {}),
  webhookRotate: (id: string) =>
    json<S["WebhookWithSecret"]>(`/api/webhooks/${id}/rotate`, {}),
  webhookDeliveries: (id: string) =>
    req<S["WebhookDeliveries"]>(`/api/webhooks/${id}/deliveries?limit=20`),
  // `hidden`: the roster is withheld (a demo, or a login every visitor
  // shares) — say nothing rather than an empty "signed in" list
  sessions: () => req<S["AuthSessions"]>("/api/sessions"),
  // mobile devices (long-lived app credentials, revocable like sessions)
  devices: () => req<S["MobileDevices"]>("/api/devices"),
  // signing another device out is a posture change — elevation-gated
  deviceRevoke: (body: { id: string }) =>
    json<S["DeviceRevoked"]>("/api/devices/revoke", body),
  // notification channels (SMS / web push)
  notifyStatus: () => req<NotifyStatus>("/api/notify"),
  // `consent` is the list of message programs ticked ("summary", "alerts"),
  // sent with the number so the server records WHEN consent was given, for
  // WHICH number, and for WHAT. Each program is its own checkbox — carriers
  // require consent per program. The server refuses an empty list: carriers
  // want evidence, not a UI gate.
  notifyPhone: (number: string, consent: string[] = []) =>
    json<S["NotifyPhoneSet"]>("/api/notify/phone", { number, consent }),
  notifyPhoneVerify: (code: string) =>
    json<S["Ok"]>("/api/notify/phone/verify", { code }),
  pushSubscribe: (sub: unknown, user_agent: string) =>
    json("/api/notify/push/subscribe",
         { ...(sub as Record<string, unknown>), user_agent }),
  // The same browser re-sending the subscription it already holds, to tie
  // it to the current session. Never raises the step-up sheet: this runs
  // unprompted at load, so a refusal is reported for the caller to explain.
  pushRebind: async (sub: unknown, user_agent: string)
      : Promise<"ok" | "step-up" | "failed"> => {
    try {
      const r = await rawFetch("/api/notify/push/subscribe", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...(sub as Record<string, unknown>),
                               user_agent }) });
      if (r.ok) return "ok";
      if (r.status === 403 && elevationRequired(
            await r.text().catch(() => "")) !== null) return "step-up";
      return "failed";
    } catch { return "failed"; }
  },
  pushUnsubscribe: (endpoint: string) =>
    json<S["Ok"]>("/api/notify/push/unsubscribe", { endpoint }),
  notifyTest: (channel: "sms" | "push") =>
    json<S["NotifyTested"]>("/api/notify/test", { channel }),
  // consented support access
  supportAccess: () => req<S["SupportAccess"]>("/api/support-access"),
  // a durable grant — elevation-gated
  supportAccessGrant: (hours: number, reason: string) =>
    json<S["SupportAccessGranted"]>("/api/support-access", { hours, reason }),
  supportAccessRevoke: () => json<S["Ok"]>("/api/support-access/revoke", {}),
  // change the login email/username — elevation-gated
  // (Form endpoint, like totp/disable — NOT the json() helper)
  emailChange: (new_email: string, keep_push_endpoint?: string) =>
    // `hint` is the address they probably meant when the domain
    // looks like a typo of a major provider. Advisory. On hosted the
    // address does not move yet: `email` is still the sign-in address and
    // `pending_email` the one waiting for its confirm button.
    req<S["EmailChanged"]>(
      "/api/email/change",
      // this client shows a reissued sheet; one that does not omits it
      // and keeps its codes
      form({ new_email, reissue_codes: "1",
             ...(keep_push_endpoint ? { keep_push_endpoint } : {}) })),
  // withdraw a pending sign-in address change (no step-up: it only
  // takes something away)
  emailChangeCancel: () =>
    json<S["EmailChangeCancelled"]>("/api/email/change/cancel", {}),
  // a household member removes their OWN login (owner + data untouched)
  accountLeave: () =>
    req<S["Ok"]>("/api/account/leave", form({})),
  // this login's own daily-email delivery (viewers may)
  meEmail: (muted: boolean) =>
    json<S["MeEmailChanged"]>("/api/me/email", { muted }),
  // one kind of the household's mail on or off for my own inbox
  meEmailPref: (kind: EmailKind, on: boolean) =>
    json<S["MeEmailChanged"]>("/api/me/email", { kind, on }),
  // this login's own switch for the household's push notices (every role);
  // the devices stay registered, account notices still arrive
  mePush: (muted: boolean) =>
    json<S["MePushChanged"]>("/api/me/push", { muted }),
  // the owner sets one member's switches — the same places the member's
  // own doors write, so the roster and their screen agree
  memberNotify: (id: string, body: { push_muted?: boolean;
                                     email_muted?: boolean;
                                     email_pref?: { kind: EmailKind;
                                                    on: boolean } }) =>
    json<S["UserNotifyChanged"]>(
      `/api/users/${encodeURIComponent(id)}/notify`, body),
  // full erasure — FRESH elevation (the sheet asks again even inside an
  // open window; Form endpoint)
  accountDelete: () =>
    req<S["Ok"]>("/api/account/delete", form({})),
  // re-send the email-verification link (hosted)
  verifyResend: () =>
    json<S["VerifyResent"]>("/api/verify-email/resend", {}),
  // the current password is the CHANGE's own input, not re-auth; the
  // route is elevation-gated on top. totp_code stays for a server that
  // still answers totp_required on this one form
  passwordChange: (current_password: string, new_password: string,
                   totp_code?: string, keep_push_endpoint?: string) =>
    // recovery_codes: the rotation voided the recovery sheet; this is the
    // one that stands (null when the account has no second factor)
    json<S["PasswordChanged"]>(
      "/api/password/change", { current_password, new_password,
      // this client shows a reissued sheet; one that does not omits it
      // and keeps its codes
      reissue_codes: true,
      ...(totp_code ? { totp_code } : {}),
      ...(keep_push_endpoint ? { keep_push_endpoint } : {}) }),
  // receipts
  // receipts on a transaction, with the charge's own amount and date (null
  // when it is gone) — what the waiting receipts are ranked against
  receipts: (txnId: string) =>
    req<S["TxnReceipts"]>(
      `/api/transactions/${encodeURIComponent(txnId)}/receipts`),
  // line items across all receipts (search + grouping)
  receiptItems: (q = "", group = "item") =>
    req<S["ReceiptItems"]>(
      `/api/receipts/items?q=${encodeURIComponent(q)}&group=${group}`),
  receiptUpload: async (txnId: string, file: File,
                        kind: "receipt" | "check" = "receipt") => {
    const fd = new FormData();
    fd.set("file", file);
    fd.set("kind", kind);
    const r = await rawFetch(
      `/api/transactions/${encodeURIComponent(txnId)}/receipt`,
      { method: "POST", body: fd, credentials: "same-origin" });
    if (!r.ok) await settle<never>(r);
    return (await r.json()) as { id: string; receipts: Receipt[] };
  },
  receiptDelete: (id: string) =>
    req<S["Ok"]>(`/api/receipts/${id}`, { method: "DELETE" }),
  // a receipt snapped before its transaction: stored waiting, parsed in
  // the background, paired with its charge when the charge lands
  receiptSnap: async (file: File) => {
    const fd = new FormData();
    fd.set("file", file);
    const r = await rawFetch("/api/receipts",
      { method: "POST", body: fd, credentials: "same-origin" });
    if (!r.ok) await settle<never>(r);
    return (await r.json()) as WaitingReceipts & { id: string };
  },
  receiptsWaiting: (offset = 0) => req<S["WaitingReceipts"]>(
      "/api/receipts/waiting" + (offset > 0 ? `?offset=${offset}` : "")),
  receiptMatch: (id: string, txnId: string) =>
    json<S["ReceiptMatched"]>(`/api/receipts/${id}/match`, { txn_id: txnId }),
  // a receipt's total, date and (optionally) store typed in by hand; a
  // waiting one is matched at once, and txn_id says where it went
  receiptDetails: (id: string,
                   body: { total: number; date: string; merchant?: string }) =>
    json<S["ReceiptDetailsSaved"]>(
      `/api/receipts/${id}/details`, body),
  // a person's answer about two receipts read alike: one slip (counted
  // once) or two purchases (each counted); stored, never asked again
  receiptSameSlip: (id: string, other: string) =>
    json<S["ReceiptSlipAnswered"]>(`/api/receipts/${id}/same-slip`,
                                   { other }),
  receiptTwoPurchases: (id: string, other: string) =>
    json<S["ReceiptSlipAnswered"]>(`/api/receipts/${id}/two-purchases`,
                                   { other }),
  receiptUnmatch: (id: string) =>
    json<S["ReceiptMatched"]>(
      `/api/receipts/${id}/unmatch`, {}),
  // one charge, several categories: the parts a person wrote for a row
  // (stored category keys + cent-exact amounts summing to the charge). PUT
  // replaces the whole split; the server answers 400 with the reason in
  // words when the parts do not add up or one is a flow category.
  txnSplitSet: (txnId: string, parts: SplitPart[]) =>
    req<S["SplitSaved"]>(
      `/api/transactions/${encodeURIComponent(txnId)}/split`, {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ parts }),
      }),
  txnSplitClear: (txnId: string) =>
    req<S["SplitSaved"]>(
      `/api/transactions/${encodeURIComponent(txnId)}/split`,
      { method: "DELETE" }),
  receiptParse: (id: string) =>
    json<S["ReceiptParsing"]>(`/api/receipts/${id}/parse`, {}),
  receiptTag: (id: string, line: number, tag: string) =>
    json<S["Ok"]>(`/api/receipts/${id}/items/${line}/tag`, { tag }),
  receiptReport: (tag: string, y: number, m: number) =>
    req<S["ReceiptReport"]>(
      `/api/receipts/report?tag=${encodeURIComponent(tag)}&y=${y}&m=${m}`),
  // bulk historical import
  bulkAnalyze: async (files: File[]) => {
    const fd = new FormData();
    for (const f of files) fd.append("files", f, (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name);
    const r = await rawFetch("/api/import/bulk/analyze",
                          { method: "POST", body: fd,
                            credentials: "same-origin" });
    if (r.status === 413) {
      // the app words its own refusal (JSON detail, shown as is); a proxy
      // in front of it refuses an oversized folder with an HTML page or an
      // empty body, which says nothing to the person — say what to do
      const body = await r.text();
      let parsed: { detail?: unknown; restore_door?: string } = {};
      try { parsed = /^\s*\{/.test(body) ? JSON.parse(body) : {}; }
      catch { /* not the app's own refusal */ }
      // one archive is not a folder to split: it belongs on the restore
      // door, which takes far larger files than this one. The app names
      // that door on its refusal; the error carries it so the page can
      // send the archive there
      const oneZip = files.length === 1 && /\.zip$/i.test(files[0].name);
      if (oneZip && parsed.restore_door)
        throw Object.assign(new Error(
          "this export is too large to read here — restore it on its own "
          + "(the account owner's)"), { restoreDoor: parsed.restore_door });
      if (typeof parsed.detail === "string") throw new Error(parsed.detail);
      throw new Error(oneZip
        ? "this export is too large to read here — restore it on its own "
          + "(the account owner's)"
        : "these files are too large to send at once — import the folder "
          + "in smaller batches");
    }
    if (!r.ok) await settle<never>(r);
    return (await r.json()) as {
      token: string;
      // set when the batch is one export ZIP and nothing else: the door
      // (POST /api/import) that restores it, whatever its size
      restore_door?: string;
      files: { index: number; name: string; kind: string; size: number;
               action: "import" | "skip"; account_id: string | null;
               new_account_name: string; amount_sign: string;
               sign_certain: boolean; note: string; oversized?: boolean;
               // an export ZIP too large to read here names the door
               // that takes it
               restore_door?: string;
               // the accounts the file could mean when the server cannot
               // say which (two cards sharing their last four)
               // each described (AccountChoice)
               choices?: AccountChoice[] }[] };
  },
  // submits the plan; the files import in the background (bulkJob)
  bulkRun: (token: string, files: unknown[]) =>
    json<BulkJob>("/api/import/bulk/run",
                  { token, files, background: true }),
  bulkJobCurrent: () => req<BulkJobStatus>("/api/import/bulk/jobs/current"),
  bulkJob: (jobId: string) =>
    req<BulkJob>(`/api/import/bulk/jobs/${encodeURIComponent(jobId)}`),
  bulkCancel: (jobId: string) =>
    json<BulkJob>(
      `/api/import/bulk/jobs/${encodeURIComponent(jobId)}/cancel`, {}),
  restoreProgress: () => req<RestoreProgress>("/api/restore/progress"),
  // provider setup
  plaidValidate: (client_id: string, secret: string, env: string) =>
    json<S["KeysSaved"]>("/api/accounts/plaid/validate",
                                       { client_id, secret, env }),
  // Hosted Link: create session → open Plaid URL only (no oikonome waiting
  // tab). SPA stays on Welcome/Accounts and polls plaidLinkStatus until done.
  // manage_accounts (update mode only) opens Link's shared-accounts
  // checklist — deselecting an account there is the only lever that stops
  // Plaid billing it; a local hide can't change what the Item shares.
  // relink_item_id: a linked bank to relink with the household's (wider)
  // history window. It stays live until the new link of the same bank is
  // saved, and is released only then; a sign-in at another bank just adds
  // it. A second relink of a bank already being relinked is refused (409).
  plaidLinkStart: (item_id = "", manage_accounts = false,
                   relink_item_id = "") =>
    req<{ link_token: string; hosted_link_url: string;
          kind: string; institution: string | null }>(
      "/accounts/plaid/link", {
        method: "POST",
        headers: {
          "content-type": "application/x-www-form-urlencoded",
          accept: "application/json",
        },
        body: new URLSearchParams({
          ...(item_id ? { item_id } : {}),
          ...(manage_accounts ? { manage_accounts: "1" } : {}),
          ...(relink_item_id ? { relink_item_id } : {}),
        }).toString(),
      }),
  plaidLinkStatus: (link_token: string) =>
    req<{ done: boolean; kind?: string; institution?: string; error?: string;
          // "pending": saved, its first pull queued on the worker
          first_sync?: "pending" | "ok";
          // a session that came back with several banks: each one, and
          // the sentence that words them all (also on a busy answer
          // while the person may still add another)
          results?: PlaidLinkBank[]; summary?: string;
          // a busy several-bank session: the banks connected so far, as
          // the row under the button says it
          progress?: string;
          // answered because the session went quiet: the bank window may
          // still be open, and a bank added there is taken later
          quiet?: boolean }>(
      // v=2: this client keeps the bank window open on a quiet answer
      `/accounts/plaid/link/${encodeURIComponent(link_token)}/status?v=2`),
  plaidKeys: (client_id: string, secret: string, env: string) =>
    json<S["KeysSaved"]>("/api/accounts/plaid/keys",
                                       { client_id, secret, env }),
  mxKeys: (client_id: string, api_key: string, env: string) =>
    json<S["KeysSaved"]>("/api/accounts/mx/keys",
                                       { client_id, api_key, env }),
  // portable, passphrase-sealed full-config bundle (provider keys,
  // LLM, SMTP, live bank links) for moving to a fresh environment
  // both bundle doors are elevation-gated (export: fresh) — a stolen
  // session alone must not exfiltrate or plant the credential set
  connectionsExport: async (passphrase: string) => {
    const r = await elevatedFetch("/api/connections/export", {
      method: "POST", credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ passphrase }),
    });
    if (!r.ok) await settle<never>(r);
    const cd = r.headers.get("content-disposition") ?? "";
    const name = /filename="([^"]+)"/.exec(cd)?.[1]
      ?? "oikonome-config.oikx";
    return { blob: await r.blob(), filename: name };
  },
  // the full-data ZIP and the database dump are downloads (a GET the
  // browser saves), but a bare session cookie must not be enough to start
  // one — a cross-site link would ride the Lax cookie. So the browser
  // first steps up here (same proof as the connections bundle) for a
  // one-shot ticket, then navigates to the returned URL within a minute.
  exportTicket: (kind: "zip" | "dump" | "continuity") =>
    json<S["ExportTicket"]>("/api/export/token", { kind }),
  // Would the download this ticket opens be refused? Asked before the
  // browser navigates, so a refusal shows in the app instead of replacing
  // it with a bare error page. Never spends the ticket; throws the
  // download's own refusal.
  exportPrecheck: async (kind: "zip" | "dump" | "continuity",
                         token: string): Promise<void> => {
    await req<string>(`/api/export/precheck?kind=${kind}&t=`
                      + encodeURIComponent(token));
  },
  // When the household's last full-data export was built and what a
  // restore would refuse in it. A navigation download never shows the page
  // its headers, so the page asks here afterwards. null when the server
  // cannot say (an older server without the route, or not the owner).
  exportLast: async (): Promise<{ at: string | null;
                                  warnings: string[] } | null> => {
    try {
      const r = await rawFetch("/api/export/last");
      try {
        if (!r.ok) return null;
        const j = await r.json() as { at?: string | null; warnings?: unknown };
        return { at: j.at ?? null, warnings: Array.isArray(j.warnings)
          ? j.warnings.map(String).filter((t) => t.trim()) : [] };
      } finally { readFinished(r); }
    } catch { return null; }
  },
  // the continuity packet: the household's money on paper for whoever is
  // left to run it. Two fields the owner writes, plus what the email door
  // will accept; the PDF itself rides the export ticket (kind
  // "continuity") and the emailed copy steps up like the download.
  continuity: () => req<Continuity>("/api/continuity"),
  continuitySave: (body: { note?: string; prepared_for?: string }) =>
    req<ContinuitySaved>("/api/continuity", {
      method: "PUT", headers: { "content-type": "application/json" },
      body: JSON.stringify(body) }),
  continuityEmail: (to: string) =>
    json<ContinuityEmailed>("/api/continuity/email", { to }),
  connectionsImport: async (file: File, passphrase: string) => {
    const fd = new FormData();
    fd.set("passphrase", passphrase);
    fd.set("file", file);
    const r = await elevatedFetch("/api/connections/import", {
      method: "POST", credentials: "same-origin", body: fd,
    });
    if (!r.ok) await settle<never>(r);
    return r.json() as Promise<{ ok: boolean; config: string[];
                                items: number;
                                credentials_dropped?: string[] }>;
  },
  mxConnect: () => json<S["MxConnect"]>("/api/accounts/mx/connect", {}),
  plaidKeysClear: () =>
    req<S["PlaidKeysCleared"]>("/api/accounts/plaid/keys", { method: "DELETE" }),
  mxKeysClear: () =>
    req<S["MxKeysCleared"]>("/api/accounts/mx/keys", { method: "DELETE" }),
  mxSyncNow: () =>
    json<S["MxSynced"]>("/api/accounts/mx/sync", {}),
  // multi-source account links
  accountLinks: () =>
    req<S["AccountLinks"]>("/api/accounts/links"),
  accountLinkCreate: (account_ids: string[]) =>
    json<S["AccountLinkCreated"]>("/api/accounts/links",
                               { account_ids, auto_order: true }),
  accountLinkDelete: (groupId: string) =>
    req<{ ok: boolean }>(`/api/accounts/links/${groupId}`,
                         { method: "DELETE" }),
  accountLinkOrder: (groupId: string, account_ids: string[]) =>
    json(`/api/accounts/links/${groupId}/order`, { account_ids }),
  accountLinkDismiss: (key: string) =>
    json("/api/accounts/links/dismiss", { key }),
  // put the standing linked-sources card away (or bring it back)
  accountLinkCard: (dismissed: boolean) =>
    json("/api/accounts/links/dismiss", { card: dismissed }),
  // passkeys
  // elevation is checked BEFORE the challenge is minted — the
  // authenticator saves the credential during the ceremony, so a refusal
  // caught only afterwards would leave an orphan passkey in the user's
  // vault that the server never kept
  passkeyRegisterOptions: () =>
    json<S["PasskeyRegisterOptions"]>(
      "/api/passkeys/options", {}),
  // adding/removing a factor is elevation-gated — a stolen session must
  // not be able to enroll its own key or strip the owner's
  passkeyRegister: (challenge_id: string, credential: unknown,
                    label: string) =>
    // recovery_codes comes back only when this passkey is the account's
    // FIRST strong factor (server issues one-time codes then) — the UI shows
    // them once for lockout protection
    json<S["PasskeyAdded"]>("/api/passkeys",
                         { challenge_id, credential, label }),
  passkeys: () =>
    req<S["Passkeys"]>("/api/passkeys"),
  passkeyDelete: (id: string) =>
    // a key removed while another factor stays voids the old recovery
    // sheet and returns the new one (null when nothing was minted)
    req<S["PasskeyRemoved"]>(`/api/passkeys/${id}`, {
      method: "DELETE",
      headers: { "content-type": "application/json" },
      // this client shows a reissued sheet; one that does not omit it and
      // keeps its codes
      body: JSON.stringify({ reissue_codes: true }),
    }),
  // inline WebAuthn re-assertion on a passkey account — verify mints a
  // 5-minute single-use ticket that /api/auth/elevate redeems (stepup.ts)
  stepupPasskeyOptions: () =>
    json<S["PasskeyAssertOptions"]>(
      "/api/stepup/passkey/options", {}),
  stepupPasskey: (challenge_id: string, credential: unknown) =>
    json<S["StepUpTicket"]>(
      "/api/stepup/passkey", { challenge_id, credential }),
  // email optional — omit for discoverable/usernameless passkey login
  passkeyLoginOptions: (email?: string) =>
    json<S["PasskeyAssertOptions"]>(
      "/api/login/passkey/options", email ? { email } : {}),
  passkeyLogin: (challenge_id: string, credential: unknown) =>
    json<S["SignedIn"]>("/api/login/passkey", { challenge_id, credential }),
  // family members + invites
  // `role` is what the claimed account becomes: "member" (can edit the
  // household's money) or "viewer" (reads everything, changes nothing).
  // elevation-gated: a durable member principal
  // `url` is null on hosted: the server mails the claim link to the address
  // on the invite and keeps it out of the reply, because holding a
  // claimable link is the only mailbox proof the pre-auth claim door gets.
  // Render what the response says happened — never the URL alone, which is
  // nothing to look at on hosted.
  inviteCreate: (label: string, role = "viewer") =>
    json<S["InviteCreated"]>("/api/invites", { label, role }),
  invites: () => req<S["Invites"]>("/api/invites"),
  inviteRevoke: (tokenHash: string) =>
    req<S["Ok"]>(`/api/invites/${tokenHash}`, { method: "DELETE" }),
  invitePeek: (token: string) =>
    req<S["InvitePeek"]>(
      `/api/invite/peek?token=${encodeURIComponent(token)}`),
  inviteClaim: (token: string, email: string, password: string) =>
    json<S["SignedIn"]>("/api/invite/claim", { token, email, password }),
  members: () => req<S["HouseholdUsers"]>("/api/users"),
  // who changed which category, bill, note… and when — newest first;
  // `before` pages by the last row's timestamp, kind/who/target filter
  activity: (params: Record<string, string> = {}) =>
    req<ActivityPage>("/api/activity"
      + (Object.keys(params).length ? "?" + new URLSearchParams(params) : "")),
  // moving somebody between member and view-only. Elevation-gated like
  // the other durable grants — the same proof removing them asks for.
  memberSetRole: (id: string, role: string) =>
    json<S["UserRoleChanged"]>(`/api/users/${id}/role`, { role }),
  // removing a member is elevation-gated
  memberRemove: (id: string) =>
    req<S["Ok"]>(`/api/users/${id}`,
      { method: "DELETE", body: JSON.stringify({}),
        headers: { "content-type": "application/json" } }),
  // a server-made file (a CSV door) handed to the browser as a download.
  // Fetched rather than followed as a plain link so a refusal — an
  // owner-only door answering a page left open after a role change —
  // arrives as the server's sentence for the caller to toast, instead of
  // navigating the tab to a bare JSON body.
  saveFile: async (path: string, fallbackName: string) => {
    const r = await rawFetch(path, { credentials: "same-origin" }, "unlimited");
    if (!r.ok) await settle<never>(r);
    const name = /filename="?([^";]+)/.exec(r.headers.get("content-disposition") ?? "")?.[1]
      ?? fallbackName;
    const url = URL.createObjectURL(await r.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    URL.revokeObjectURL(url);
  },
  testing: () => req<TestingState>("/api/testing"),
  // returns the feedback number; when the package can't be emailed the
  // zip comes back and is handed to the browser as a download here
  testingFeedback: async (message: string, screenshot: File | null,
                          kind: "feedback" | "bug" = "feedback") => {
    const fd = new FormData();
    fd.set("message", message);
    fd.set("kind", kind);
    if (screenshot) fd.set("screenshot", screenshot);
    const r = await rawFetch("/api/testing/feedback",
                          { method: "POST", body: fd,
                            credentials: "same-origin" });
    if (!r.ok) await settle<never>(r);
    if ((r.headers.get("content-type") ?? "").includes("zip")) {
      const number = r.headers.get("x-feedback-number") ?? "feedback";
      const url = URL.createObjectURL(await r.blob());
      const a = document.createElement("a");
      a.href = url;
      a.download = `oikonome-feedback-${number}.zip`;
      a.click();
      URL.revokeObjectURL(url);
      return { number, delivery: "downloaded" as const };
    }
    return (await r.json()) as { number: string; delivery: "emailed" };
  },
  doctor: () => req<S["DoctorReport"]>("/api/doctor"),
  // shipped docs + FAQ as help topics
  help: () => req<S["HelpTopics"]>("/api/help"),
  doctorScripts: () =>
    req<S["ScriptHeartbeats"]>("/api/doctor/scripts"),
  doctorScriptUpdate: (source: string,
                       body: { expected_hours?: number; alerts?: boolean }) =>
    json<S["Ok"]>(`/api/doctor/scripts/${encodeURIComponent(source)}`, body),
  // kicking another session is elevation-gated, as on deviceRevoke.
  // keep_push_endpoint: this browser's own web-push endpoint, so "sign out
  // everywhere else" drops the OTHER browsers' subscriptions and not ours
  sessionRevoke: (body: { all_others?: boolean; id?: string;
                          keep_push_endpoint?: string }) =>
    json<S["SessionsRevoked"]>("/api/sessions/revoke", body),
  settingsSave: (body: Record<string, unknown>) =>
    json<Settings>("/api/settings", body),
  // send this recipient's invitation again (rotates the link, so
  // the previous one stops working)
  // the owner confirms the paused relay / recipients / AI endpoint are theirs
  outboundResume: (version?: string) =>
    json<S["OutboundResumed"]>("/api/settings/outbound/resume",
                                            { version }),
  recipientInviteResend: (email: string) =>
    // `sent: false` (with `reason`) when the server held the mail back;
    // an older server leaves `sent` out and always sent
    json<S["RecipientResent"]>(
      "/api/settings/recipients/resend", { email }),
  smtpTest: () =>
    json<S["SmtpTested"]>(
      "/api/settings/smtp-test", {}),
  budgetsSuggest: () => req<BudgetSuggestions>("/api/budgets/suggest"),
  // the same, with the planner's take-home box as typed: the server
  // measures the savings suggestion on it only when history names no income
  budgetsSuggestTyped: (income: string) => req<BudgetSuggestions>(
    "/api/budgets/suggest?" + new URLSearchParams({ income })),
  // maintenance jobs (Settings card): run the worker task bodies now
  jobsSync: () =>
    json<S["SyncRan"]>("/api/jobs/sync", {}),
  // archive a connection (Plaid: also releases the Item = frees a slot)
  connectionDisconnect: (id: string) =>
    json<{ ok: boolean; plaid_released: boolean; note: string }>(
      `/api/connections/${encodeURIComponent(id)}/disconnect`, {}),
  // bring a disconnected MX or SimpleFIN bank back; MX fetches the gap on
  // its next sync, SimpleFIN pulls it at once (refused if the bridge lost it)
  connectionReconnect: (id: string) =>
    json<{ ok: boolean; resync_from: string }>(
      `/api/connections/${encodeURIComponent(id)}/reconnect`, {}),
  // wizard Sync step — background sweep + polled progress
  jobsSyncStart: () =>
    json<S["SyncStarted"]>(
      "/api/jobs/sync/start", {}),
  jobsSyncStatus: () => req<SyncStatus>("/api/jobs/sync/status"),
  // wizard step-2 gate: per-Plaid-item historical-backfill readiness.
  // Read-only (viewers may poll it); the nudge that starts a background
  // pull when one has gone quiet is the owner-only POST below.
  onboardingBackfill: () =>
    req<BackfillStatus>("/api/onboarding/backfill"),
  onboardingBackfillNudge: () =>
    json<S["BackfillStatus"]>("/api/onboarding/backfill/nudge", {}),
  historyCoverage: () => req<HistoryCoverage>("/api/history/coverage"),
  jobsEmail: () =>
    json<S["EmailSent"]>("/api/jobs/email", {}),
  alertDismiss: (kind: string, message: string) =>
    json<S["AlertsDismissed"]>("/api/alerts/dismiss", { kind, message }),
  alertRestore: (kind: string, message: string) =>
    json<S["AlertsRestored"]>("/api/alerts/restore", { kind, message }),
  alertsHistory: () => req<{ alerts: AlertRow[] }>("/api/alerts/history"),
  // TOTP endpoints are form-encoded (they predate the JSON surface)
  // the FIRST enrol is elevation-gated; a re-enrol proves the current
  // authenticator instead (stronger), so the code is sent when there is one
  totpEnroll: (current_code?: string) =>
    req<S["TotpEnrollment"]>("/api/totp/enroll",
      form(current_code ? { current_code } : {})),
  totpConfirm: (secret: string, code: string, current_code?: string) =>
    // first-factor enrolment returns one-time recovery codes
    req<S["TotpConfirmed"]>(
      "/api/totp/confirm",
      form({ secret, code, ...(current_code ? { current_code } : {}) })),
  // mint a fresh recovery-code batch — elevation-gated (the live code a
  // TOTP account owes was collected at elevate time)
  // stop a factor-clearing recovery on this account (no step-up: cancelling
  // is the safe direction)
  cancelPendingRecovery: () =>
    json<S["RecoveryCancelled"]>(
      "/api/recovery/cancel-pending", {}),
  recoveryRegenerate: () =>
    req<S["RecoveryRegenerated"]>(
      "/api/totp/recovery-regenerate", form({})),
  // turning 2FA off is a security downgrade — elevation-gated AND the
  // route's own live code (code alone let a stolen session drop 2FA)
  // The code is single-use, so this door is never resent after the sheet
  totpDisable: (code: string) =>
    // turned off while a passkey stays, the old recovery sheet is voided
    // and the new one returned (null when no factor remains)
    reqOneTimeCode<S["TotpDisabled"]>(
      "/api/totp/disable", form({ code, reissue_codes: "1" })),
  // session elevation — see the Elevation type at the top of this file
  elevation: () => req<Elevation>("/api/auth/elevation"),
  elevate: (body: { password: string; totp_code: string }
                // a lost authenticator: the recovery code stands in for the
                // second factor beside the password, as it does at login
                | { password: string; recovery_code: string }
                | { stepup_ticket: string } | { recovery_code: string }) =>
    json<Elevation>("/api/auth/elevate", body),
  checkPayee: (id: string, payee: string) =>
    json<S["CheckPayeeSaved"]>(
      `/api/transactions/${encodeURIComponent(id)}/check-payee`, { payee }),
  setTxnNote: (id: string, note: string) =>
    json<S["TxnNoteSaved"]>(
      `/api/transactions/${encodeURIComponent(id)}/note`, { note }),
  bizFlag: (id: string, on: boolean) =>
    json(`/api/business/${encodeURIComponent(id)}/${on ? "flag" : "unflag"}`, {}),
  bizList: (year?: number, limit?: number) => {
    const p = new URLSearchParams();
    if (year) p.set("year", String(year));
    if (limit) p.set("limit", String(limit));
    const qs = p.toString();
    return req<BizData>("/api/business" + (qs ? `?${qs}` : ""));
  },
  // business entities
  businessSummary: () => req<BusinessSummary>("/api/business/summary"),
  businessCombine: (on: boolean) =>
    json<{ combined: boolean }>("/api/business/combine", { on }),
  entityCreate: (body: Partial<Entity> & { name: string; structure: string;
                 ein?: string }) => json<Entity>("/api/business/entities", body),
  entityGet: (id: string) =>
    req<Entity>(`/api/business/entities/${encodeURIComponent(id)}`),
  entityUpdate: (id: string, body: Record<string, unknown>) =>
    req<Entity>(`/api/business/entities/${encodeURIComponent(id)}`,
      { method: "PATCH", headers: { "content-type": "application/json" },
        body: JSON.stringify(body) }),
  // archive is the everyday "remove" — every record survives, restorable;
  // delete-forever is the made-this-by-mistake case and must carry the
  // entity's exact typed name (the server refuses anything else)
  entityArchive: (id: string) =>
    json<Entity>(`/api/business/entities/${encodeURIComponent(id)}/archive`,
                 {}),
  entityRestore: (id: string) =>
    json<Entity>(`/api/business/entities/${encodeURIComponent(id)}/restore`,
                 {}),
  entityDeleteImpact: (id: string) =>
    req<EntityDeleteImpact>(
      `/api/business/entities/${encodeURIComponent(id)}/delete-impact`),
  entityDeleteForever: (id: string, name: string) =>
    json<{ ok: boolean } & EntityDeleteImpact>(
      `/api/business/entities/${encodeURIComponent(id)}/delete-forever`,
      { name }),
  entityStatus: (id: string, status: string) =>
    json<Entity>(`/api/business/entities/${encodeURIComponent(id)}/status`,
      { status }),
  entityAddMember: (id: string, body: { member_name: string;
                    ownership_pct?: number; is_manager?: boolean;
                    is_household?: boolean }) =>
    json(`/api/business/entities/${encodeURIComponent(id)}/members`, body),
  // ownership_pct null clears the share
  entityUpdateMember: (id: string, memberId: string,
                       body: { member_name?: string;
                               ownership_pct?: number | null;
                               is_manager?: boolean;
                               is_household?: boolean }) =>
    req<NonNullable<Entity["members"]>[number]>(
      `/api/business/entities/${encodeURIComponent(id)}/members/`
        + encodeURIComponent(memberId),
      { method: "PATCH", headers: { "content-type": "application/json" },
        body: JSON.stringify(body) }),
  entityRemoveMember: (id: string, memberId: string) =>
    req<{ ok: boolean }>(
      `/api/business/entities/${encodeURIComponent(id)}/members/`
        + encodeURIComponent(memberId), { method: "DELETE" }),
  entityImportFlags: (id: string) =>
    json<{ ok: boolean; moved: number }>(
      `/api/business/entities/${encodeURIComponent(id)}/import-flags`, {}),
  entityEquity: (id: string) =>
    req<S["EquityPage"]>(
      `/api/business/entities/${encodeURIComponent(id)}/equity`),
  equityRecord: (id: string, body: { kind: string; amount: number;
                 date: string; form?: string; note?: string }) =>
    json<EquityMovement>(
      `/api/business/entities/${encodeURIComponent(id)}/equity`, body),
  equityDelete: (id: string, movementId: string) =>
    req<{ ok: boolean }>(
      `/api/business/entities/${encodeURIComponent(id)}/equity/${movementId}`,
      { method: "DELETE" }),
  // an owner transfer on a business account, recorded as a contribution
  // (money in) or a draw (money out) at the row's own amount and date
  bizOwnerTransfer: (id: string, txnId: string) =>
    json<EquityMovement>(
      `/api/business/entities/${encodeURIComponent(id)}/owner-transfer`,
      { txn_id: txnId }),
  equityReimburse: (id: string, body: { txn_id: string;
                    mode: "contribute" | "reimburse" }) =>
    json<EquityMovement>(
      `/api/business/entities/${encodeURIComponent(id)}/reimburse`, body),
  // Schedule C suggestions (suggest-only; nothing is written until a human
  // confirms). `source` says where each came from: "merchant" is this
  // entity's own prior decision, "kind" the vendor's business kind, null =
  // no basis.
  bizSuggestions: (id: string) =>
    req<S["LineSuggestions"]>(
      `/api/business/entities/${encodeURIComponent(id)}/suggestions`),
  bizTxns: (id: string, year?: number) =>
    req<{ transactions: BizTxn[] }>(
      `/api/business/entities/${encodeURIComponent(id)}/transactions` +
      (year ? `?year=${year}` : "")),
  /** One page of the worksheet, newest first. `total` is every row in
   *  scope; a server that predates paging omits it and sends them all. */
  bizTxnsPage: (id: string, offset: number, limit: number, year?: number) =>
    req<{ transactions: BizTxn[]; total?: number; limit?: number;
          offset?: number }>(
      `/api/business/entities/${encodeURIComponent(id)}/transactions` +
      `?limit=${limit}&offset=${offset}` + (year ? `&year=${year}` : "")),
  // `sched_c_line` null clears the line; leaving it out keeps the stored one
  bizClassify: (id: string, txnId: string, body: { bucket: string;
                sched_c_line?: string | null }) =>
    json(`/api/business/entities/${encodeURIComponent(id)}/transactions/` +
      `${encodeURIComponent(txnId)}/class`, body),
  entityPnl: (id: string, year?: number) =>
    req<Pnl>(`/api/business/entities/${encodeURIComponent(id)}/pnl` +
      (year ? `?year=${year}` : "")),
  entityCompliance: (id: string) =>
    req<{ obligations: Obligation[] }>(
      `/api/business/entities/${encodeURIComponent(id)}/compliance`),
  complianceAdd: (id: string, body: { title: string; due_date: string;
                  recurrence?: string; fee?: number; url?: string }) =>
    json<{ id: string }>(
      `/api/business/entities/${encodeURIComponent(id)}/compliance`, body),
  complianceDelete: (id: string, oblId: string) =>
    req<{ ok: boolean }>(
      `/api/business/entities/${encodeURIComponent(id)}/compliance/${oblId}`,
      { method: "DELETE" }),
  // self-employed pack
  estTax: (id: string, year?: number) =>
    req<EstimatedTax>(`/api/business/entities/${encodeURIComponent(id)}/` +
      `estimated-tax` + (year ? `?year=${year}` : "")),
  balanceSheet: (id: string) =>
    req<BalanceSheet>(
      `/api/business/entities/${encodeURIComponent(id)}/balance-sheet`),
  mileageList: (id: string, year?: number) =>
    req<{ trips: MileageTrip[]; summary: { year?: number; miles: number;
      rate: number; rates?: { rate: number; miles: number }[];
      deduction: number } }>(
      `/api/business/entities/${encodeURIComponent(id)}/mileage` +
      (year ? `?year=${year}` : "")),
  mileageAdd: (id: string, body: { date: string; miles: number;
               purpose?: string }) =>
    json(`/api/business/entities/${encodeURIComponent(id)}/mileage`, body),
  mileageDelete: (id: string, tripId: string) =>
    req<{ ok: boolean }>(
      `/api/business/entities/${encodeURIComponent(id)}/mileage/${tripId}`,
      { method: "DELETE" }),
  vendorList: (id: string, year?: number) =>
    req<{ vendors: Vendor[]; threshold?: number }>(
      `/api/business/entities/${encodeURIComponent(id)}/vendors` +
      (year ? `?year=${year}` : "")),
  // the door stores the TIN it is sent and an omitted one clears it, so a
  // caller toggling `reportable` sends the TIN already on file back
  vendorMark: (id: string, body: { merchant: string; reportable: boolean;
                                   tin_last4?: string | null }) =>
    json(`/api/business/entities/${encodeURIComponent(id)}/vendors`, body),
  accountAssignEntity: (accountId: string, entityId: string | null) =>
    json(`/api/accounts/${encodeURIComponent(accountId)}/entity`,
      { entity_id: entityId }),
  txnAssignEntity: (txnId: string, entityId: string | null) =>
    json(`/api/transactions/${encodeURIComponent(txnId)}/entity`,
      { entity_id: entityId }),
  reimbPending: () =>
    req<S["ReimbPending"]>("/api/reimburse/pending"),
  reimbCandidates: (id: string, q = "") =>
    req<S["ReimbCandidates"]>(
      `/api/reimburse/${encodeURIComponent(id)}/candidates` +
      (q ? `?q=${encodeURIComponent(q)}` : "")),
  reimbLink: (txn_id: string, other_ids: string[], partial = false) =>
    json<S["ReimbLinked"]>(
      "/api/reimburse/link", { txn_id, other_ids, partial }),
  // the line beside Pair for the ticked rows, by the link rule (nothing
  // is linked)
  reimbSelection: (txn_id: string, other_ids: string[], partial = false) =>
    json<S["ReimbSelection"]>(
      "/api/reimburse/selection", { txn_id, other_ids, partial }),
  // newest first; `page` (from 1) reaches older pairs, `q` finds one by
  // either side's merchant
  reimbPairs: (page = 1, q = "") => req<ReimbPairsPage>(
    `/api/reimburse/pairs?page=${page}`
    + (q ? `&q=${encodeURIComponent(q)}` : "")),
  reimbUnlink: (expense_id: string, reimburse_id: string) =>
    json<S["ReimbUnlinked"]>("/api/reimburse/unlink", { expense_id, reimburse_id }),
  reimbUnflag: (id: string) =>
    json<S["ReimbUnflagged"]>(`/api/reimburse/${encodeURIComponent(id)}/unflag`, {}),
  reimbFlag: (id: string,
              opts: { partial?: boolean; expected?: number | null } = {}) =>
    json<S["ReimbFlagged"]>(`/api/reimburse/${encodeURIComponent(id)}/flag`, opts),
  calendar: (days = 35) =>
    req<CalendarData>(`/api/calendar?days=${days}`),
  // tax & income documents (analyze -> preview -> commit)
  taxdocAnalyze: async (file: File) => {
    const fd = new FormData();
    fd.set("file", file);
    const r = await rawFetch("/api/import/taxdoc/analyze",
      { method: "POST", body: fd, credentials: "same-origin" });
    if (!r.ok) await settle<never>(r);
    return (await r.json()) as TaxdocPlan;
  },
  taxdocCommit: (token: string, rows: TaxdocRow[]) =>
    json<S["TaxdocCommitted"]>("/api/import/taxdoc/commit",
      { token, rows }),
  taxdocW2s: () => req<S["StoredW2s"]>("/api/import/taxdoc/w2s"),
  // a removal or owner change names the W-2 by what the list showed: an id
  // comes back after a removal, and a stale list's id may name another
  // form, which the server refuses (409). In the body, never the URL.
  taxdocW2Delete: (w: StoredW2) =>
    req<{ ok: boolean }>(`/api/import/taxdoc/w2s/${w.id}`, {
      method: "DELETE", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ seen: w2Seen(w) }) }),
  taxdocW2Owner: (w: StoredW2, owner: "primary" | "spouse") =>
    json<{ ok: boolean }>(`/api/import/taxdoc/w2s/${w.id}/owner`,
      { owner, seen: w2Seen(w) }),
  // the stored documents: a removal or a put-back names the document by
  // what the list showed (`seen`, in the body), so a stale list is refused
  taxdocStored: () => req<StoredDocs>("/api/import/taxdoc/stored"),
  taxdocStoredEffect: (d: StoredDoc) =>
    req<S["StoredDocEffect"]>(`/api/import/taxdoc/stored/${d.id}/effect`),
  taxdocStoredDelete: (d: StoredDoc) =>
    req<S["StoredDocRemoved"]>(`/api/import/taxdoc/stored/${d.id}`, {
      method: "DELETE", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ seen: d.seen }) }),
  taxdocStoredRestore: (d: StoredDoc) =>
    json<S["StoredDocRemoved"]>(`/api/import/taxdoc/stored/${d.id}/restore`,
      { seen: d.seen }),
  // the payer's name on a stored document, under the stored name listed
  taxdocStoredPayer: (d: StoredDoc, name: string) =>
    json<S["StoredDocRemoved"]>(`/api/import/taxdoc/stored/${d.id}/payer`,
      { seen: d.seen, payer: d.payer, name }),
  importFile: (file: File, account_id: string, amount_sign: string,
               new_account_name = "") => {
    const fd = new FormData();
    fd.set("file", file);
    fd.set("account_id", account_id);
    fd.set("amount_sign", amount_sign);
    if (new_account_name) fd.set("new_account_name", new_account_name);
    // this client can ask where each account of a multi-account YNAB
    // register goes, so the server parks such a file and asks
    fd.set("ynab_choice", "1");
    return req<ImportOutcome>("/api/import", { method: "POST", body: fd });
  },
  importMapped: (token: string, cols: Record<string, string>) =>
    json<S["ImportResultReply"]>("/api/import/mapped", { token, ...cols }),
  importYnabAccounts: (token: string,
                       accounts: Record<string, YnabDestination>,
                       remove_earlier: Record<string, boolean>) =>
    json<S["ImportResultReply"]>("/api/import/ynab-accounts",
      { token, accounts, remove_earlier }),
  importBatches: () => req<S["ImportBatches"]>("/api/import/batches"),
  // `forceKept`: the owner's "undo anyway" after the server asked about
  // rows only a note naming no feed account would keep
  importRollback: (batch_id: string, forceKept = false) =>
    json<S["RolledBack"]>(
      "/api/import/rollback",
      forceKept ? { batch_id, force_kept: true } : { batch_id }),
  // `month` (YYYY-MM) reads a past month's paid/received per bill
  bills: (month?: string) =>
    req<BillsData>(month ? `/api/bills?month=${month}` : "/api/bills"),
  billsDetect: () =>
    req<S["BillsDetected"]>(
      "/api/bills/detect", { method: "POST" }),
  // `adjust` (approve only) changes the name, amount, cadence or due day
  // before the bill is created; the finder's evidence stays attached
  billsProposal: (pid: string, action: "approve" | "reject",
                  adjust?: ProposalAdjust) =>
    // `note` is present when the approve changed something the person set
    // (a pinned amount cleared) and must be shown to them
    req<S["ProposalAnswered"]>("/api/bills/proposal", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(adjust && action === "approve"
        ? { pid, action, adjust } : { pid, action }),
    }),
  // the offers on screen in one click (the wizard's bills step, the Bills
  // page): `pids` are the ones shown, so an offer a scan files after the
  // page loaded is never approved unseen
  billsApproveAll: (pids: string[]) =>
    req<S["ProposalsApproved"]>("/api/bills/proposals/approve-all", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ pids }),
    }),
  // reports — the four analytics payloads
  reportNetworth: () => req<NetWorthReport>("/api/reports/networth"),
  reportSpending: () => req<SpendingReport>("/api/reports/spending"),
  reportCashflow: () => req<CashflowReport>("/api/reports/cashflow"),
  reportCashflowFlow: (range: FlowKey) =>
    req<FlowPeriod>(`/api/reports/cashflow/flow?range=${range}`),
  reportSpendingWindow: (range: FlowKey) =>
    req<SpendingWindow>(`/api/reports/spending/window?range=${range}`),
  reportIncomeWindow: (range: FlowKey) =>
    req<IncomeWindow>(`/api/reports/income/window?range=${range}`),
  reportFees: () => req<FeesReport>("/api/reports/fees"),
  reportCashCosts: () => req<CashCostsReport>("/api/reports/cash-costs"),
  // retirement what-if — params are query-string overrides of the inputs
  // ask_spend: this client asks for a spend when none is set, so the
  // server may answer without projecting one (an older phone cannot)
  retirement: (params: Record<string, string> = {}) =>
    req<RetirementData>("/api/retirement?" +
      new URLSearchParams({ ...params, ask_spend: "1" })),
  // debt payoff planner — method/extra are what-if overrides, unsaved
  debtPlan: (params: Record<string, string> = {}) =>
    req<DebtPlan>("/api/debt-plan" +
      (Object.keys(params).length ? "?" + new URLSearchParams(params) : "")),
  debtPlanSave: (body: DebtPlanSave) => json<DebtPlan>("/api/debt-plan", body),
  // bill CRUD (/api/bills/*)
  billsBill: (payee: string) =>
    req<BillDetail>(`/api/bills/bill?payee=${encodeURIComponent(payee)}`),
  billsSave: (body: { payee: string; amount: string | number;
                          // absent keeps the bill's stored cadence
                          cadence?: string; by_month_day?: number[];
                          next_due?: string; income?: boolean;
                          orig_payee?: string; cap?: string | number | null;
                          merchant?: string; show_today?: boolean;
                          match_category?: boolean;
                          category?: string | null;
                          txn_category?: string;
                          companions?: Companion[];
                          merchants?: string[]; restore?: boolean }) =>
    json<S["BillSaved"]>(
      "/api/bills/save", body),
  // known merchants (canonical-resolved) for the manual-bill picker
  merchants: () => req<S["Merchants"]>("/api/merchants"),
  billsDelete: (payee: string, mode: "archive" | "purge" = "archive") =>
    json<S["BillDeleted"]>(
      "/api/bills/delete", { payee, mode }),
  billsRestore: (payee: string) =>
    json<S["BillRestored"]>("/api/bills/restore", { payee }),
  // `disabled` names the target state and is required: a bare flip is
  // decided against whatever the server holds, so a stale screen or a
  // retried request would toggle the bill the opposite way to the button
  // the person pressed
  // the household's word on whether one payer's deposits are paychecks
  // ("auto": the rules decide) — specs/paychecks.md
  incomePayer: (key: string, count_as: "paycheck" | "never" | "auto",
                name?: string) =>
    json<S["IncomePayerSaved"]>("/api/income/payer", { key, count_as, name }),
  billsToggle: (payee: string, disabled: boolean) =>
    json<S["BillToggled"]>(
      "/api/bills/toggle", { payee, disabled }),
  billsHint: (payee: string, action: "dismiss" | "restore" = "dismiss",
              target: "fit" | "health" = "fit") =>
    json<S["BillHinted"]>(
      "/api/bills/hint", { payee, action, target }),
  billsHistory: (payee: string, window: string = "all") =>
    req<BillsHistoryData>(
      `/api/bills/history?payee=${encodeURIComponent(payee)}&window=${encodeURIComponent(window)}`),
  // scope "one" = this row only, no rule written (the default the server
  // also uses); "all" = also teach the merchant, which then appears on the
  // Rules page. A generic payee ("Check Paid") must never get "all" by
  // accident — one such edit would claim every row sharing that name.
  setCategory: (id: string, category: string, scope: "one" | "all" = "one") =>
    req<S["TxnCategorySet"]>(
      `/api/transactions/${encodeURIComponent(id)}/category`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ category, scope }),
      }),
  // bulk category override for a whole (canonical) merchant. The preview
  // names what the write does beyond the count: `flow_rows` are rows the
  // bank labelled transfer/income/loan that a merchant rule WILL move into
  // spend; `skipped_override` are hand-set rows it leaves alone.
  // `family: false` sends `?canon_only=1` (the server's flag), scoping the preview to the single
  // canonical merchant with no family expansion — the scope the row-menu
  // "All <payee>" write uses. The query-arg name is the server contract;
  // the two must match. Default (family scope) is what "Apply to whole
  // merchant" on the bill-history page previews.
  merchantCategoryPreview: (payee: string, category: string,
                            opts?: { family?: boolean }) =>
    req<S["MerchantCategoryPreviewed"]>(
      "/api/bills/merchant-category/preview"
      + (opts?.family === false ? "?canon_only=1" : ""), {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ payee, category }),
      }),
  merchantCategorySet: (payee: string, category: string) =>
    req<S["MerchantCategorySet"]>(
      "/api/bills/merchant-category", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ payee, category }),
      }),
  merchantCategoryUndo: (undo: unknown) =>
    req<S["MerchantCategoryUndone"]>(
      "/api/bills/merchant-category/undo", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ undo }),
      }),
  // the learned-rules surface — provenance groups load
  // lazily with server-side search + pagination (25 a page)
  rules: (source: "user" | "llm" | "seed" | "model", q = "", page = 1) =>
    req<S["RulesPage"]>(`/api/rules?source=${source}&q=${encodeURIComponent(q)}` +
                   `&page=${page}`),
  ruleSet: (merchant: string, category: string) =>
    req<S["RuleSet"]>("/api/rules/set", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ merchant, category }),
    }),
  ruleDisable: (merchant: string, disabled: boolean) =>
    req<S["Ok"]>("/api/rules/disable", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ merchant, disabled }),
    }),
  ruleDelete: (merchant: string) =>
    req<S["RuleDeleted"]>("/api/rules/delete", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ merchant }),
    }),
};

export interface RuleRow {
  merchant: string;
  category_primary: string;
  source: string;      // user | llm | seed
  disabled: boolean;
  count: number;
}
// one provenance group's page + the q-filtered per-source totals
export interface RulesPage {
  rules: RuleRow[]; total: number; page: number; per_page: number;
  counts: { user: number; llm: number; seed: number; model?: number };
}

// local ledger assistant
export type AssistantTurn = S["AssistantTurn"];
/** where the next older page of the log starts */
export type AssistantCursor = S["HistoryCursor"];
/** where one AI task's data goes, judged by the server from the endpoint
 *  that task would call: on this server or its network, to a remote AI
 *  service (`host` named only when the household configured it), or
 *  nowhere. Every "stays on this instance" line reads this — absent
 *  (loading, an older server) is never read as "stays here". */
export type AiDestination = S["AiDestination"];
export type AssistantStatus = S["AssistantStatus"];
export const assistant = {
  // local=false means answers (and the ledger figures the tools return)
  // travel to `host` — the UI must say so instead of claiming "local"
  status: () => req<AssistantStatus>("/api/assistant"),
  // `answer` is the prose alone; `blocks` the ordered text + chart/table
  // pieces the page draws (components/AssistantBlocks)
  // a follow-up names the conversation it continues (`threadId`); the
  // model then sees that thread's earlier turns and nothing else. A 404
  // means that conversation no longer exists; `saved: false` or
  // `removed: true` means the answer was kept in no conversation
  ask: (question: string, threadId?: string | null) =>
    req<S["AssistantAnswer"]>("/api/assistant", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify(threadId ? { question, thread_id: threadId } : { question }),
    }),
  // the household's answered questions, newest first; every member
  // reads the same log, so a chart drawn on the phone is here too
  // it pages by (asked_at, id): `next` is the cursor for the page after
  // this one, and both halves go back on the next read
  history: (cursor?: AssistantCursor | null) =>
    req<S["AssistantHistory"]>(
      "/api/assistant/history"
      + (cursor ? `?before=${encodeURIComponent(cursor.before)}`
         + (cursor.before_id ? `&before_id=${encodeURIComponent(cursor.before_id)}` : "")
        : "")),
  // drops one conversation — every turn of the thread (a 404 when
  // nothing was left to drop: someone removed it first)
  forgetThread: (threadId: string) =>
    req<S["AssistantCleared"]>(
      `/api/assistant/threads/${encodeURIComponent(threadId)}`, { method: "DELETE" }),
  // drops one turn: a server that predates conversations has only this
  // door, and there a conversation's id is its one turn's id
  forgetTurn: (turnId: string) =>
    req<S["AssistantForgotten"]>(
      `/api/assistant/history/${encodeURIComponent(turnId)}`, { method: "DELETE" }),
  clear: () =>
    req<S["AssistantCleared"]>("/api/assistant/history", { method: "DELETE" }),
  // a whole conversation — its questions and answers in order — as one
  // self-contained HTML page, handed to the browser as a download; the
  // server re-validates every block and draws the charts
  // `partial`: the conversation's start is not loaded, so the page says
  // earlier questions are missing. The first turn also rides as the
  // single-answer body (`question` + `blocks`) a server older than
  // conversations understands — it then renders that answer alone
  // instead of refusing with "nothing to report".
  report: async (turns: { question: string; blocks: AnswerBlock[] }[], partial = false) => {
    const r = await rawFetch("/api/assistant/report", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ turns, partial, question: turns[0]?.question ?? "",
                             blocks: turns[0]?.blocks ?? [] }),
      credentials: "same-origin",
    });
    if (!r.ok) await settle<never>(r);
    const name = /filename="?([^";]+)/.exec(r.headers.get("content-disposition") ?? "")?.[1]
      ?? "oikonome-conversation.html";
    const url = URL.createObjectURL(await r.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    URL.revokeObjectURL(url);
  },
};

// Python round() — banker's rounding (half to even). Server-rendered
// figures use Python's round(); Math.round rounds half AWAY
// from zero, so the same half-dollar would read one dollar apart. Match it.
export const pyround = (v: number) => {
  const f = Math.floor(v), diff = v - f;
  if (diff > 0.5) return f + 1;
  if (diff < 0.5) return f;
  return f % 2 === 0 ? f : f + 1;   // exactly .5 → nearest even
};

/** Does this account's `kind` fall under any of these Plaid types?
 *
 *  `/api/accounts` serves `kind` as `type || '/' || subtype` (see
 *  `web/data.py`), so it reads `"depository/checking"` — a bare `=== "depository"`
 *  matches NOTHING and silently yields an empty set. Route every type test
 *  through here so the composite shape only has to be understood in one
 *  place. */
export const kindIs = (a: { kind: string }, ...types: string[]) =>
  types.some((t) => a.kind === t || a.kind.startsWith(t + "/"));

/** Did approving an offer find the bill already tracked? The server then
 *  withdraws the offer itself, so the card can neither be approved nor
 *  dismissed and must leave the list. Read from the reply's flag, or from
 *  a refusal that carries the flag or says "already tracked" (a server
 *  that predates the flag). Mobile keeps the same rule in lib/pure.ts. */
export function proposalSuperseded(replyOrError: unknown): boolean {
  if (replyOrError && typeof replyOrError === "object"
      && (replyOrError as { superseded?: unknown }).superseded === true)
    return true;
  if (!(replyOrError instanceof Error)) return false;
  const t = replyOrError.message;
  return /"superseded"\s*:\s*true/.test(t) || /already tracked/i.test(t);
}

/** The longest category name the server stores; it refuses longer ones. */
export const CATEGORY_MAX = 64;

/** Ask for a custom category name. The browser's prompt cannot cap what
 *  is typed, so a name past the server's limit is refused here with the
 *  same reason, instead of after a round trip. */
export function promptCategoryName(): string | null {
  const name = window.prompt(
    `Custom category name (up to ${CATEGORY_MAX} characters):`)?.trim();
  if (!name) return null;
  if (name.length > CATEGORY_MAX) {
    toast(`Category names can be at most ${CATEGORY_MAX} characters.`);
    return null;
  }
  return name;
}

/** A holding's symbol as people read it: the aggregator spells a cash
 *  position `CUR:USD`, which is its key, not its name. */
export const holdingSymbol = (sym: string | null | undefined) => {
  const m = /^CUR:([A-Z]{3})$/.exec(sym ?? "");
  return m ? `Cash (${m[1]})` : (sym ?? "");
};

/** A category as the server SENDS it in a row: the key with its
 *  underscores opened, upper-case for a bank key (`TRANSFER OUT`). This is
 *  the form to test and to write back into a cached row — never catLabel,
 *  which is for reading and changes with how a category is displayed. */
export const catKey = (c: string | null | undefined) =>
  (c ?? "").replace(/_/g, " ");

/** Money moved, not spent or earned: a transfer either way, or a payment
 *  arriving on a card or loan. Such a row is muted in a ledger and is
 *  never offered as a bill. */
export const isMovedMoney = (category: string | null | undefined,
                             amount: number,
                             detailed?: string | null) => {
  // a card payment is money moved on BOTH sides — the checking row that
  // pays the card as much as the card row it lands on — whatever category
  // it shows; the server leaves it out of spending by this same key
  if (detailed === CARD_PAYMENT_DETAILED) return true;
  // a bank key arrives upper-case; a name somebody typed ("Transfer
  // fees") is their own category and real spending, so the case matters
  const k = catKey(category);
  return k.includes("TRANSFER") || (k.includes("LOAN PAYMENTS") && amount < 0);
};

/** The aggregator's detailed key for a credit-card payment — the one key
 *  every spending figure leaves out (SPEND_WHERE). */
export const CARD_PAYMENT_DETAILED = "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT";

/** A ledger row's category as it reads: a card payment says so, rather
 *  than "Loan Payments", the name it shares with a mortgage that IS
 *  spending. Every other row reads its category through catLabel. */
export const rowCategoryLabel = (t: { category: string;
                                      category_detailed?: string | null }) =>
  t.category_detailed === CARD_PAYMENT_DETAILED
    ? "Card payment" : catLabel(t.category);

// the joining words a category title leaves small ("Food and Drink")
const CAT_SMALL_WORDS = new Set(["and", "or", "of", "the", "to", "in", "for",
                                 "a", "an", "at", "on"]);

/** Display form of a category (or any underscored enum key such as a
 *  payment kind): `FOOD_AND_DRINK` → `Food and Drink`. Custom names
 *  pass through untouched. Every surface that shows a category goes
 *  through here so the same value never reads two ways. */
export const catLabel = (c: string | null | undefined) => {
  // the server spells "no category at all" as "?" (EFF_CAT) — a reader
  // shouldn't have to know that
  if (!c || c === "?") return "Uncategorized";
  const s = c.replace(/_/g, " ").trim();
  // a name somebody typed (any lower-case letter) is theirs; a key with no
  // letters to case has nothing to title
  if (s !== s.toUpperCase() || s === s.toLowerCase()) return s;
  // a joining word stays small between two words, never as the last
  // one: "Transfer In" beside "Transfer Out", not "Transfer in"
  const words = s.toLowerCase().split(" ");
  return words.map((w, i) =>
    i && i < words.length - 1 && CAT_SMALL_WORDS.has(w)
      ? w : w.slice(0, 1).toUpperCase() + w.slice(1),
  ).join(" ");
};

/** The two Plaid CDNs /api/logo will proxy — the server's own allowlist
 *  (web/logos.py). Anything else is a guaranteed 400. */
const LOGO_HOSTS = new Set(["plaid-merchant-logos.plaid.com",
                            "plaid-counterparty-logos.plaid.com"]);

/** A row's `logo_url` as a URL of our own logo cache, or null when the
 *  server would refuse it. Collector rows and restored data carry logo
 *  URLs from anywhere; asking the proxy for one is a certain 400 that
 *  still spends the shared rate limit — so decide here and draw the
 *  monogram instead. Mirrors `allowed()` in server/oikonome/web/logos.py. */
export function logoProxyUrl(logo: string | null | undefined): string | null {
  if (!logo || logo.length >= 400) return null;
  // no query and no fragment: the server matches on the exact URL string
  const m = /^https:\/\/([^/?#]+)(\/[^?#]*)$/.exec(logo);
  if (!m || !LOGO_HOSTS.has(m[1]) || !m[2].endsWith(".png")) return null;
  return `/api/logo?u=${encodeURIComponent(logo)}`;
}

/** "2 min ago" — coarse on purpose; the connection rows and the
 *  bank-clock line both read it. */
export function ago(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

// toLocaleString builds an Intl.NumberFormat per call; a ledger paint
// calls money() once per row, so the two shapes are built once
const _fmtCents = new Intl.NumberFormat(undefined, {
  minimumFractionDigits: 2, maximumFractionDigits: 2 });
const _fmtWhole = new Intl.NumberFormat(undefined);
export const money = (v: number | null | undefined, cents = false) => {
  if (v === null || v === undefined) return UNKNOWN_FIGURE;
  if (cents) return `${v < 0 ? "-" : ""}$${_fmtCents.format(Math.abs(v))}`;
  const n = pyround(v);              // round the SIGNED value, as the server does
  return `${n < 0 ? "-" : ""}$${_fmtWhole.format(Math.abs(n))}`;
};

// What an alert's button says. "Fix" is for something broken (a dead
// connection, a funding gap); a queue waiting on a yes or no is reviewed,
// not fixed, and calling it broken makes routine review sound like a fault.
const ALERT_REVIEW = new Set(["proposals", "drift", "merges", "anomaly",
                              "bonus", "reimb", "transfer"]);
// The alert log stores no link, so an alert's own target (a drift alert's
// /bills/history?payee=…) exists only on the live alert Today carries. An
// active log row takes the link of the live alert with the same kind and
// message; anything else falls back to the kind's page.
export function withLiveLinks<T extends AlertRow>(
    rows: T[], live: Alert[] | undefined): T[] {
  if (!live?.length) return rows;
  return rows.map((r) => {
    if (r.link || !r.active) return r;
    const hit = live.find((a) => a.kind === r.kind && a.message === r.message);
    return hit?.link ? { ...r, link: hit.link } : r;
  });
}
export const alertAction = (kind: string) =>
  ALERT_REVIEW.has(kind) ? "Review" : "Fix";

/** A coming date as people say it: "today" and "tomorrow" for the two
 *  days anyone acts on, the date otherwise. `today` is the household's
 *  day (ISO), not the device clock's. */
export const dueLabel = (iso: string | null | undefined, today: string) => {
  if (!iso) return "";
  const day = iso.slice(0, 10);
  if (day === today.slice(0, 10)) return "today";
  const t = new Date(today.slice(0, 10) + "T00:00:00Z");
  t.setUTCDate(t.getUTCDate() + 1);
  return day === t.toISOString().slice(0, 10) ? "tomorrow" : mmdd(iso);
};
export const mmdd = (iso: string | null | undefined) => {
  if (!iso) return "";
  const [y, m, d] = iso.slice(0, 10).split("-");
  return `${m}/${d}/${y.slice(2)}`;
};
