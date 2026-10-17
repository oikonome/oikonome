import type { Txn } from "./ledger-row.generated";
import type { EmailKind } from "../recipients";
import type { AnswerBlock } from "../components/AssistantBlocks";
// Thin typed API client. Session cookie rides automatically (same-origin);
// every 401 funnels to the login screen via the AuthError sentinel.

export class AuthError extends Error {}

// Session elevation ("sudo mode"): a security-posture route answers 403
// {error: "elevation_required", fresh} instead of taking a password in its
// body; the wrapper asks the mounted ElevationProvider to re-prove identity
// (passkey, else password + code — the same proofs as before, so a session
// thief still cannot elevate) and then retries the request ONCE. `fresh`
// is the two routes that always ask again (delete, the full export).
export interface Elevation {
  elevated: boolean; until: string | null;
  // a hosted passkey-only account reads ["passkey", "recovery+password"]:
  // its lost-key fallback is a recovery code sent WITH the password
  methods: ("passkey" | "password" | "recovery+password")[]; totp: boolean;
}
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
 *  goes through here, the multipart/form ones included. */
async function rawFetch(path: string, init?: RequestInit): Promise<Response> {
  try {
    // the epoch signal wins unless a caller brought its own
    return await fetch(path, { credentials: "same-origin", ...init,
                               signal: init?.signal ?? epoch.signal });
  } catch (e) {
    // an aborted epoch IS a sign-out: surface it as the auth loss it
    // is, never as an "AbortError" toast on whatever was in flight
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
  return `The server answered with an error page (${status}) — try again.`;
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

function inWait(retryAfter?: string | null): string {
  const secs = parseInt(retryAfter ?? "", 10);
  return secs > 0 && secs <= 120
    ? `${secs} second${secs === 1 ? "" : "s"}.`
    : "a moment.";
}

/** "The server is busy — try again in a moment.", or in N seconds when
 *  the server named a short wait. The sign-in page (pages.js) and the
 *  app say the same words. */
export function busyText(retryAfter?: string | null): string {
  return "The server is busy — try again in " + inWait(retryAfter);
}

/** The same shape for the proxy's "back in a moment" answer: the app is
 *  restarting, not overloaded, so it says that. */
export function restartingText(retryAfter?: string | null): string {
  return "The server is restarting — try again in " + inWait(retryAfter);
}

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
    return status ? pageText(status, body) : restartingText(null);
  }
  return body || raw;
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

export interface Continuity {
  note: string; prepared_for: string;
  mail_configured: boolean; members_only: boolean; members: string[];
  note_max: number;
}

export interface Me {
  // the household's daily-email schedule + whether THIS login is muted
  daily_email?: { on: boolean; hour: number; summary: boolean;
                  muted: boolean;
                  // which kinds of the household's mail reach this inbox,
                  // and which cadences the household sends at all
                  prefs?: Partial<Record<EmailKind, boolean>>;
                  cadences?: Partial<Record<EmailKind, boolean>> };
  email: string; tenant_id: string; totp_enabled: boolean;
  role: "owner" | "viewer"; version: string; hosted: boolean;
  /** the IANA zone every scheduled hour is kept in — the household's own
   *  when it has set one, else the instance's — and which of those it is.
   *  Clients name it beside the hour pickers and, for an owner on a
   *  household with none, offer the device's zone once. */
  timezone?: string; timezone_source?: "household" | "instance";
  instance_timezone?: string;
  // The installed gate's plan key for this household, or null when the
  // instance has none (every feature available). `features` mirrors the
  // server's tier_allows.
  plan?: string | null;
  features?: {
    investments?: boolean; liabilities?: boolean; sms?: boolean;
    priority_sync?: boolean; business?: boolean; institution_cap?: number;
    // how many of the cap are spent — counted server-side by the same
    // function the ADD door enforces with, so the figure on screen can
    // never disagree with the refusal
    institutions_used?: number;
    // false when the plan does not include Plaid's recurring cross-check
    plaid_recurring?: boolean;
    // true when no bank can be connected until an owner confirms their
    // email address — the server's own reason for an allowance of 0, so
    // a paying but unconfirmed household is never shown a closed door.
    // Absent from an older server (bankAwaitsConfirm then falls back).
    bank_awaits_confirm?: boolean;
  };
  // true once the tenant has set up a business. Drives whether the
  // Business TAB is shown at all — the /business route stays reachable.
  has_business?: boolean;
  // merchant logos on ledger/merchants/spending/email (Settings; default on)
  merchant_logos?: boolean;
  // "♥ Support development" footer link (null → hidden)
  donate_url?: string | null;
  // operator broadcast banner (null = none); deadline set =
  // reboot countdown — the SPA ticks it down, then overlays + reconnects
  broadcast: { message: string; severity: "info" | "warn";
               deadline?: string | null } | null;
  demo: boolean;
  // the shared demonstration login (an owner whose password is handed
  // out): the server refuses it every posture change and every durable
  // grant, so those controls render switched off (DemoLock's sharedLock)
  shared_login?: boolean;
  // hosted accounts verify their email; banner + resend while false
  verified: boolean;
  // hosted one-door bank linking is live (platform aggregator
  // credentials present server-side)
  bank_link: boolean;
  // account creation time (ISO) — the SPA holds the verify
  // banner until this is ≥3 days old
  created_at: string | null;
  // when the nightly sweep freezes a never-confirmed household (ISO);
  // null unless hosted, unverified and nobody here ever confirmed
  verify_deadline: string | null;
  // a requested sign-in address waiting for its mailbox's confirm button
  // (hosted); the sign-in email stays `email` until then
  pending_email?: string | null;
  pending_email_expires_at?: string | null;
  // hosted account must enroll a second factor before use;
  // recovery_codes_left drives the Settings low-codes nudge
  needs_2fa: boolean;
  recovery_codes_left: number;
  // when a factor-clearing recovery someone asked for lands (ISO), or null;
  // Settings offers the cancel, since an open session is no new sign-in
  factor_reset_pending?: string | null;
  // null while nothing is known to be wrong with this address.
  // Non-null = the mail provider could not deliver, the scheduled sends are
  // held, and the UI says so with the provider's own reason. `did_you_mean`
  // is set when the domain looks like a typo of a major provider — always a
  // suggestion, never a verdict.
  email_delivery: {
    state: "bouncing" | "complained";
    bounce_type: string | null;
    reason: string | null;
    suppressed: boolean;
    since: string | null;
    did_you_mean: string | null;
  } | null;
}
export interface NotifyStatus {
  sms_available: boolean; sms_entitled?: boolean;
  phone: string | null; phone_verified: boolean;
  /** when a credential change paused the stored number (ISO); absent on
   *  servers that predate it, where an unverified stored number is the
   *  only sign of the pause */
  phone_paused_at?: string | null;
  phone_pending: string | null;
  push_available: boolean; vapid_public_key: string | null;
  push_subscribed: boolean;
  /** the push relay is configured, so the mobile apps can be reached even
   *  when this instance has no VAPID key for browser push */
  native_push_available?: boolean;
  /** phones with a live native (mobile-app) push registration, tenant-wide */
  native_push_devices?: number;
  /** this server has no public address, so it cannot mail the invitation
   *  an added recipient must accept; absent on servers that predate it */
  invites_need_base_url?: boolean;
}
export interface Invite {
  token_hash: string; role: string; label: string;
  created_at: string; expires_at: string;
}
export interface Member {
  id: string; email: string; role: string; created_at: string; me: boolean;
}
// the household activity log: one hand-authored act per row, the sentence
// composed by the server so every client reads the same words
export interface ActivityRow {
  id: string; at: string; actor: string; actor_user_id: string | null;
  kind: string; action: string; target: string | null; label: string;
  summary: string; detail: Record<string, unknown> | null;
}
export interface ActivityPage {
  rows: ActivityRow[]; more: boolean; next: string | null;
  actors: string[]; kinds: string[];
}
export interface Alert {
  kind: string; severity: string; message: string;
  // optional in-app path the message points at (e.g. drift → that bill)
  link?: string | null;
}
export interface AlertRow extends Alert {
  first_seen: string; last_seen: string; active: number; dismissed: number;
}
// The ledger row is ONE shape across server, web and mobile — generated
// from specs/ledger-row.schema.json (scripts/gen-ledger-row-types.py).
export type { Txn } from "./ledger-row.generated";
export interface Series {
  series: [string, number][]; min: number; min_date: string;
  negative_date: string | null; end: number; rate: number;
}
export interface TodayFull {
  date: string; yesterday?: string;   // start of the "recent" window
  // back-nav (?date=YYYY-MM-DD): is this the live today, where "now"
  // actually is, and the ledger's first date (the ‹ button's floor)
  is_today: boolean; real_today: string; min_date: string | null;
  day_of_month: number; days_in_month: number;
  verdict: string; variance: number; tolerance: number;
  // which hero face the account shows (today_view setting; the page's
  // toggle writes it back through /api/settings) and the simple face's
  // server-composed pieces, shared with the email so nothing drifts
  today_view: "summary" | "detail";
  simple: { left_today: number;
            chips: { text: string; tone: string }[] };
  variable_actual: number; variable_budget: number;
  // the hero's "$X of $Y left": the month's spendable share (the whole
  // month unless the ledger began inside it) and what is left of it —
  // one server figure, equal to the tiles times the days left
  month_share?: number; variable_budget_paced?: number;
  month_remaining_paced?: number;
  income: number | null; plan_surplus: number | null; avg_bills: number;
  savings_plan?: number;
  // the bills this month is SCHEDULED to owe — the plan chain's middle
  // figure, so in → bills due → to spend adds up even when an early
  // autopay of next month's bill already left as cash (that stays in
  // buckets.fixed.month_budget, the fixed bar's basis). Absent from older
  // servers; fall back to the fixed bucket then.
  scheduled_month?: number;
  // sweep goals: plan cap, month-to-date availability, and the
  // server-composed sentence (identical wording in the daily email)
  sweep_plan?: number;
  sweep_available?: number | null;
  sweep_line?: string | null;
  variable_scale: { factor: number; static_total: number;
                    available: number } | null;
  irregular_bills: { payee: string; amount: number; label: string }[];
  // [payee, amount, due, withdrawn_on] — withdrawn_on is when the bank feed
  // took the posted charge back (null: it never posted); an older server
  // sends only the first three
  overdue_unpaid: [string, number, string, (string | null)?][];
  mtd: [string, number][];
  amazon_subs: [string, number][];
  amazon_total: number;
  allow: Record<string, {
    rate: number; plan: number;
    // today-ledger: allowance fixed at start of day, today's spend deducts
    spent_today: number; today_allowance: number; left_today: number;
    // month scope for the tile meter: full track = month budget, fill =
    // month-to-date spend, pace post tick at today
    month_budget: number; month_spent: number;
    /** "daily reduced from $X to $Y" — plan vs the forward rate */
    daily_note: string | null;
    /** no-spend days that bring a reduced daily back to its plan; null
     *  when nothing to recover or not recoverable this month */
    recover_days: number | null;
    /** the sentence for it — "skip 3 days of spending and the daily is
     *  back to $93" / "can't get back to $93 this month" */
    recover_note: string | null;
  }>;
  allow_kids: { name: string; rate: number; plan: number;
                spent_today: number; today_allowance: number;
                left_today: number; month_budget: number;
                month_spent: number; daily_note: string | null;
                recover_days: number | null;
                recover_note: string | null }[];
  // bills pinned to Today (bill setting "pin to Today") — server-composed
  // cards, same wording in the email HTML and plain text
  bill_cards: { kind: "envelope" | "bill"; label: string; left: number;
                over: boolean; sub: string; frac: number;
                /** today's position in the card's period (0..1) — the
                 *  pace post tick on the meter */
                pace_frac: number;
                status: string | null;
                status_tone: "neg" | "pos" | "warn" | null }[];
  days_left: number; headroom: number | null;
  // food/other additionally carry the custom-bucket decomposition:
  // children carve spend out of the parent for display; display_* is the
  // parent AFTER the carve-outs (equal to actual/expected when no children)
  // paced_*: the budget share the month may spend (less than the whole
  // when the ledger began inside the month) — what the rows measure against
  buckets: Record<string, {
    actual: number; expected: number; month_budget: number;
    paced_budget?: number;
    children?: { name: string; actual: number; expected: number;
                 month_budget: number; paced_budget?: number }[];
    display_actual?: number; display_expected?: number;
    remaining_budget?: number; paced_remaining_budget?: number;
  }>;
  fixed_unpaid_due: number;
  reasons: { bucket: string; variance: number; pace: string; detail: string[] }[];
  // the Why narrative — server-composed sentences, rendered verbatim by
  // the page, the email HTML and the plain text
  /** hero sentences + chips — composed server-side
   *  so the page, email HTML and plain text cannot drift */
  pace_line?: string;
  why_chips?: { payee: string; amount: number; count: number }[];
  why?: { headline: string;
          /** over budget only: the per-day caps that end the month inside
           *  budget (buckets with nothing left get $0 / no-spend days) */
          recovery?: string | null;
          entries: { name: string; tone: "over" | "on" | "under";
                     text: string; spent: number; budget: number;
                     variance: number;
                     top: { payee: string; amount: number;
                            count: number }[] }[] } | null;
  alerts: Alert[];
  /** checking is null (and no_checking true) when the household has no
   *  checking account: an unknown balance, never a $0 one */
  runway: { checking: number | null; card_debt: number; due_total: number;
            balance_unreliable?: boolean; no_checking?: boolean } | null;
  // unpaid bill occurrences through month end; null on past days
  bills_remaining_month: number | null;
  next_paycheck: string | null;
  // full ledger-row shape — Today's recent pane renders the shared
  // TxnTable, identical to the Transactions page
  recent: Txn[];
  // two user-facing card scenarios — pay all cards now, and
  // autopay statement balance (the realistic default; the engine's
  // full-balance-at-due-dates series is no longer exposed)
  forecast: { days: number; checking: number; card_debt: number;
              /** no checking account: the walk starts from nothing, so its
               *  balances and lows are not a forecast of anything */
              no_checking?: boolean;
              pace_now: Series; pace_stmt: Series;
              /** [date, amount, card name] — dated card autopays the cash
               *  chart draws as lines; amount is negative (money leaving) */
              card_autopay: [string, number, string][] } | null;
  forecast_rows: { date: string; amount: number; label: string;
                   bal_now: number | null; bal_stmt: number | null }[];
  savings_goals: SavingsGoalProgress[];
}

// ---- timeframe lenses (/api/lens/*) — views composed from the
//      engine by web/lenses.py ----
// "net" = a closed month with no budget snapshot: the year grid shows net
// income − spending for it instead of a budget verdict
export type LensStatus = "final" | "in_progress" | "future" | "projected"
  | "net";
export interface LensTxn {
  date: string; amount: number; payee: string; category: string;
  account: string | null; pending: boolean; fixed: boolean;
}
export interface WeekBucket {
  actual: number; week_budget: number; expected: number;
  children: { name: string; actual: number; week_budget: number;
              expected: number }[];
  display_actual: number; remaining_budget: number;
}
export interface MonthLensData {
  y: number; m: number; status: LensStatus; as_of: string;
  days_in_month: number;
  // verdict null (and budget_set false): no budget the household saved
  // stands behind the month — the lens shows no verdict
  verdict: string | null; variance: number; tolerance: number;
  budget_set?: boolean;
  projection: { pace_total: number; variance: number; tolerance: number;
                verdict: string } | null;
  // null when unjudged (budget_set false): no spendable to measure against
  variable_actual: number; variable_budget: number | null;
  buckets: Record<string, {
    actual: number; expected: number; month_budget: number;
    children?: { name: string; actual: number; expected: number;
                 month_budget: number }[];
    display_actual?: number; display_expected?: number;
    remaining_budget?: number;
  }>;
  bills: { posted: number; planned: number; awaiting: number;
           paid: { date: string; amount: number; payee: string }[];
           overdue: [string, number, string, (string | null)?][];
           envelopes: { payee: string; used: number; monthly: number;
                        overflow: number }[] };
  // [display label, amount, stored key] — the ledger filter matches the
  // stored key ('FOOD_AND_DRINK'), not the display label ('FOOD AND
  // DRINK'); '?' is uncategorized and null is a row with no category
  // filter behind it (the clustered Amazon parent).
  by_category: [string, number, string | null][];
  // the clustered parent's children — [label, amount, stored key],
  // each key an 'Amazon - …' override the ledger can filter on
  amazon_subs: [string, number, string | null][]; amazon_total: number;
  biggest: LensTxn[];
  income: { actual: number | null; budgeted: number | null };
  spend_total: number;
  variable_scale: { factor: number; static_total: number;
                    available: number } | null;
  // the standing monthly excess-cash figure (Today's source)
  plan_surplus: number | null;
  // the ledger's posted contribution to the standing
  // "Savings" goal within THIS month (null: no goal / future month)
  saved_month: number | null;
  // "snapshot": a closed month judged against its frozen budget; "live":
  // today's config (current/future months, or a closed month predating
  // snapshots — the page notes the retroactive judgment)
  budget_source?: "snapshot" | "live";
  // same flag for the bill schedule — "live" on a closed month means its
  // snapshot predates bill freezing, so today's schedule applied
  bills_source?: "snapshot" | "live";
  first_year: number | null;
}
export interface YearCell {
  m: number; status: LensStatus; verdict: string | null;
  variance: number | null; variable_actual: number | null;
  variable_budget: number | null;
  // "net" cells only — the month's cash flow (income unknown ⇒ null net)
  income?: number | null; spend?: number; net?: number | null;
}
// Budget history (the Budget page's card): each frozen month's variable
// budgets, how it was captured ("close" = frozen as the month ran,
// "backfill" = the owner applied a later budget retroactively), plus how
// many closed months since the ledger began have no snapshot.
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
export interface YearTotals {
  income: number | null; spend: number; saved: number | null;
  rate: number | null;
}
export interface YearLensData {
  y: number; cells: YearCell[];
  totals: YearTotals; prev_totals: YearTotals;
  // [display label, this year, last year, stored key] — the key comes
  // last and is what the ledger filter matches; '?' is uncategorized,
  // null a row with no category filter behind it
  categories: [string, number, number, string | null][];
  // elapsed-months per-bucket aggregates for the year money map
  buckets_annual: Record<"food" | "other" | "fixed",
    { actual: number; expected: number; month_budget: number }>;
  // standing MONTHLY excess-cash figure from the latest elapsed
  // month (null when nothing has elapsed) — the SPA multiplies by the
  // elapsed-month count, matching buckets_annual's coverage
  plan_surplus: number | null;
  // the Savings row's ledger-verified YTD (matched transfers on the
  // standing goal over the budgeted months; null when there are none).
  // Optional — older servers omit it, and the SPA falls back to the
  // rate_90d × elapsed extrapolation, labelled as an estimate.
  saved_ytd?: number | null;
  first_year: number | null;
}

export interface Proposal {
  /** an "attach" (fee) or "merchant" proposal: the bill it would join */
  bill_payee?: string | null;
  id: string; kind: string; bill_type: string | null;
  payee: string; amount: number; cadence: string; summary: string;
  frequency?: string | null; interval?: number | null;
  next_due?: string | null; llm_tag?: string | null; income?: boolean;
}
export interface Bill {
  payee: string; amount: number; income: boolean; cadence: string;
  // ok | stale | mismatch | drifting | misdated | off-ledger | withdrawn
  // (the missed cycle's charge was taken back by the bank feed — amber,
  // never counted as needing attention)
  due_on: string | null; health: string | null; health_dismissed: boolean;
  // the newest ledger charge for this merchant, whatever its amount — a
  // mismatch with too few charges for a fit can still say what it saw
  last_seen?: { date: string; amount: number } | null;
  // the attention queue's raw material: when the bill last matched, its
  // cycle, and the ONE action the server's evidence supports — the
  // clients render the suggestion verbatim, never invent one
  last_match?: string | null;
  cycle_days?: number | null;
  suggestion?: { action: "accept_amount" | "disable" | "review";
                 amount?: number; reason: string } | null;
  disabled: boolean;
  fit: LedgerFit | null; fit_diverges: boolean; hint_dismissed: boolean;
  // this month's occurrence/envelope stats (paid x of y, envelope state)
  occurrences: number; paid: number; paid_amount: number;
  used: number | null; overflow: number | null; next_unpaid: string | null;
  // this month's missed occurrence: the date the bank feed withdrew its
  // charge (still unpaid, worded as a warning rather than overdue). The ONLY
  // date that may turn the row's status amber
  withdrawn_on?: string | null;
  // the earlier cycle behind a "withdrawn" health verdict — for the health
  // pill alone, never the status: a later cycle that was never charged is
  // still overdue
  health_withdrawn_on?: string | null;
  // the pool the envelope draws on. period_months 1 = monthly pool
  // (period_used === used), 12 = calendar-year pool, where `used` is only
  // this month's slice and period_used/left carry the year.
  pool: number | null; period_months: number | null;
  period_used: number | null; period_left: number | null;
  // the edit-form prefill (what /api/bills/bill serves for one bill),
  // carried on every row so a page of N bills is one request, not N+1.
  // `cadence_value` is the FREQ:interval select value; `cadence` above is
  // the human label.
  cadence_value: string; bill_type: string; source: string;
  category: string | null; merchant: string | null; cap: number | null;
  show_today: boolean; match_category: boolean; txn_category: string;
  // the merchants the bill pays, by display name — its identity: the
  // ledger's rows under these names are its charges, whatever the bank's
  // wording. Empty = match by the words of the bill's name.
  merchants: string[];
  // the fees that ride with the payment ("incl. $1 fee"): each
  // attaches to the same occurrence when it lands within window_days
  companions: Companion[]; fee_total: number;
}
export interface Companion { tokens: string; amount: number; window_days: number }
export interface BillsData {
  proposals: Proposal[];
  bills: Bill[];
  // the month the per-bill stats describe (YYYY-MM) and whether it is a
  // closed month read back (the schedule itself is always the live one)
  month: string; month_final: boolean;
  archived: { payee: string; amount: number; frequency: string | null;
              synced_at: string | null }[];
  // the cadence picker's options, once for the page
  cadences: [string, string][];
  // the page's headline: the schedule's monthly load (the budget's own
  // evened-out figure, so the two pages agree), the week ahead, and how many
  // bills are actually unhealthy
  totals?: { bills_monthly: number; income_monthly: number;
             week_total: number; week_count: number; attention: number };
}

// /api/bills/bill — one bill's editable fields + payee-keyed config
export interface BillDetail {
  payee: string; amount: number; income: boolean; cadence: string;
  next_due: string; bill_type: string; source: string;
  category: string | null; merchant: string | null; active: boolean;
  cap: number | null; disabled: boolean;
  // pin to Today: the bill gets a card on the Today page + daily email;
  // match_category (envelopes) also counts rows of the bill's category
  show_today: boolean; match_category: boolean;
  // the TRANSACTION category stamped on the rows this bill matches ("" =
  // none) — for a merchant that sells more than one kind of thing; the
  // merchant's other rows keep their rule
  txn_category: string;
  // the merchants the bill pays (see Bill.merchants)
  merchants: string[];
  cadences: [string, string][];
}

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
  from_logo?: string | null; into_logo?: string | null;
  from_rows?: number | null; into_rows?: number | null;
  from_samples: string[]; into_samples: string[];
  signals: string[]; supports: string[];
}
export interface LedgerFit {
  kind: string; amount: number; cadence: string; next_due: string;
  short: string; label: string; cycle_days?: number; hits?: number;
}
// history rows are the Transactions-page shape (same renderer) plus the
// matcher verdict and a legacy id alias
export type HistoryTxn = Txn & { txn_id: string; hit: boolean };
export interface BillsHistoryData {
  payee: string;
  logo?: string | null;     // the merchant row's logo, when it has one
  txns: HistoryTxn[];
  monthly: { month: string; total: number; count: number }[];
  fit: LedgerFit | null;
  lifetime: { total: number; count: number;
              first: string | null; last: string | null;
              // mean over months with activity (sparse merchants
              // must not be watered down by empty months)
              active_months: number; avg_monthly_active: number };
  inflow: boolean;
  bill: { amount: number; bill_type: string; source: string; cadence: string;
          due_on: string | null; income: boolean; tol: number } | null;
  health: { health: string; last_match: string | null;
            matched?: number; fit_diverges?: boolean;
            // behind a "withdrawn" verdict: when the feed took the charge back
            withdrawn_on?: string | null } | null;
  proposals: Proposal[];
  covered_by: string | null;
  hint_dismissed: boolean;
  // an archived bill sits under this payee (`bill` is null for it): it
  // is restored, not added again — a save here would edit that row
  bill_archived?: boolean;
  // the payee is a name ledger rows display, so the merchant rename/merge
  // applies to it; absent from older servers
  merchant_in_use?: boolean;
  cadences: [string, string][];
}

// ---- reports (/api/reports/*) ----
export interface PLRow { name: string; value: number; pl: number; pct: number | null;
                         invested?: number; basis?: number; contributed?: number;
                         cash_in?: number; cash_out?: number }
export interface NetWorthReport {
  current_total: number;
  property_items: { name: string; kind: string; value: number; source: string }[];
  property_net: number;
  full_total: number;
  by_institution: [string, number][];
  by_asset_class: [string, number][];
  trend: [string, number][];
  // the same months with the property layer added, ending at full_total
  // (absent from an older server)
  full_trend?: [string, number][];
  coinbase_pl: PLRow[];
  retirement_combined: {
    groups?: { provider: string; accounts: PLRow[]; invested: number;
               value: number; pl: number; pct: number | null }[];
    total?: { invested: number; value: number; pl: number; pct: number | null };
  };
  trend_estimated_until: number;
  snapshot_trend: [string, number][];
  savings_trend: [string, number][];
  _built_at?: string;
}
export interface SpendingReport {
  by_year: [string, number, number][];
  by_month: [string, number][];
  // [payee, amount, count, logo_url|null]
  top_merchants: [string, number, number, (string | null)?][];
  amazon: [string, number, number][];
  category_totals: [string, number][];
  yoy_years: string[];
  yoy_cats: string[];
  yoy_matrix: (string | number)[][];
  _built_at?: string;
}
// where the money came from and where it went, for one period — income by
// kind on the left, fixed / variable / saved on the right; same predicates
// as every other figure on the page
export interface FlowPeriod {
  from: string; to: string; label: string;
  // true when the household's ledger starts this month, so the range shows
  // the month so far instead of complete months
  live_month?: boolean;
  // true when the window was cut to start at the first month with known
  // income (the bank feed's first); `label` already names the cut range
  income_clipped?: boolean;
  // months inside the window left out on both sides because no bank feed
  // covers them (their income is unknown, not $0); `label` names them
  income_gap_months?: number;
  // true when no bank feed covers the window (none linked, or it starts
  // after the window): income is not known, so `in.*`, `saved` and
  // `rate` are null rather than a misleading 0
  income_unknown?: boolean;
  in: { total: number | null; paychecks: number | null;
        interest: number | null; other: number | null };
  out: { total: number; fixed: number; variable: number };
  saved: number | null; rate: number | null;
}
// served per range (the site-wide 3m/6m/1y/3y/5y/all vocabulary), not in
// the report — the fixed split walks months and 'all' costs seconds
export type FlowKey = "cur" | "1m" | "3m" | "6m" | "1y" | "3y" | "5y" | "all";
// the Spending tab's windowed view — ranked lists each carry the previous
// equal window's figure (null when the range is 'all': no previous window)
export interface SpendingWindow {
  from: string; to: string; label: string; months: number;
  // true when the household's ledger starts this month: the range shows
  // the month so far, and there is no previous window to compare with
  live_month?: boolean;
  total: number; prev_total: number | null;
  by_month: [string, number][];
  // ranked by this window; categories spent on only in the previous
  // window follow at 0, so a drop to nothing shows as a change
  categories: [string, number, number | null][];
  // [payee, amount, visits, prev-window amount|null, logo]
  merchants: [string, number, number, number | null, string | null][];
  // same shape, merchants paid only in the previous window (amount 0)
  dropped_merchants?: [string, number, number, number | null, string | null][];
}
export interface IncomeKinds { paychecks: number; interest: number; other: number }
export interface IncomeWindow {
  from: string; to: string; label: string; months: number;
  // true when the household's ledger starts this month: the range shows
  // the month so far, and there is no previous window to compare with
  live_month?: boolean;
  total: number; prev_total: number | null;
  kinds: IncomeKinds; prev_kinds: IncomeKinds | null;
  by_month: [string, number][];
  // [payer, amount, deposits, prev-window amount|null, logo]
  sources: [string, number, number, number | null, string | null][];
  // same shape, payers seen only in the previous window (amount 0)
  dropped_sources?: [string, number, number, number | null, string | null][];
}
export interface CashflowReport {
  savings_by_year: [string, number | null, number, number | null, number | null][];
  monthly_by_year: Record<string, [string, number][]>;
  // continuous monthly net-cash-flow — history + 90d forecast
  // (forecast months derived from the Today forecast's balance series)
  cash_graph: { points: [string, number][]; forecast_from: number };
  reported_income: [number, number | null, number | null, number | null,
                    number | null, boolean | number | null][];
  career_income: [number, number][];
  income_by_month: [string, number][];
  investment_funding: [string, number][];
  // money pulled from investments this month / year to date
  brokerage_funding_mtd?: number;
  brokerage_funding_ytd?: number;
  // the same figures under their earlier names — read through
  // brokerageFunding(), which prefers the new keys
  wf_funding_mtd?: number;
  wf_funding_ytd?: number;
  _built_at?: string;
}
/** Investment funding month-to-date and year-to-date, from the current
 *  keys, falling back to the earlier names a server may still send. */
export const brokerageFunding = (cf: CashflowReport) => ({
  mtd: cf.brokerage_funding_mtd ?? cf.wf_funding_mtd ?? 0,
  ytd: cf.brokerage_funding_ytd ?? cf.wf_funding_ytd ?? 0,
});
export interface FeesReport {
  by_platform: [string, number, number][];
  by_year: [string, number][];
  total: number;
  _built_at?: string;
}
/** Interest and fees paid on bank and card accounts, by year — what
 *  holding money costs, net of what the bank refunded. Empty `by_year`
 *  means the section has nothing to say. */
export interface CashCostYear {
  year: string; interest: number; fees: number; refunds: number; net: number;
  // [name, amount, date, kind] of the year's largest charge
  biggest: [string, number, string, "interest" | "fee"] | null;
}
export interface CashCostsReport {
  by_year: CashCostYear[];
  ytd: CashCostYear | null;
  interest_ever: number;
  earned_ytd: number;
  // [name, amount, date, kind, account] of the newest charge
  latest: [string, number, string, "interest" | "fee", string] | null;
  _built_at?: string;
}

// ---- retirement (/api/retirement) ----
export interface RetirementInputs {
  age: number; spend: number; ret: number; infl: number; end: number;
  ssage: number; employer_mo: number; taxable_mo: number; resume: number;
  stockpct: number; saving_yr: number; your_ss: number; has_sched: boolean;
  // the cash bucket's nominal yield in %; null = keeps pace with
  // inflation (0% real), the default
  cash: number | null;
}
// GET /api/debt-plan — every card and loan with a balance, on one schedule
export type DebtMethod = "avalanche" | "snowball";
export interface DebtRow {
  id: string; name: string; kind: "card" | "mortgage" | "loan";
  mask: string | null; institution: string | null;
  balance: number;
  apr: number; apr_source: "issuer" | "you" | "none";
  apr_issuer: number | null;
  min_payment: number; min_source: "issuer" | "you" | "estimate";
  min_issuer: number | null;
  // a mortgage servicer's whole monthly payment, escrow included; the
  // plan walks principal and interest only (min_payment)
  payment_reported?: number | null;
  skip: boolean;
}
export interface DebtSchedule {
  method: DebtMethod; extra: number;
  months: number | null;            // null = not within fifty years
  debt_free: string | null;         // ISO day of the last payment
  total_interest: number; total_paid: number;
  monthly: number;                  // every minimum + the extra
  starting: number;
  growing: boolean;                 // the minimums do not cover the interest
  series: [string, number][];       // today, then each month's first
  debts: { id: string; order: number; paid_off: string | null;
           interest: number; paid: number; remaining: number }[];
}
export interface DebtPlan {
  today: string;
  method: DebtMethod; extra_monthly: number;
  saved: { method: DebtMethod; extra_monthly: number };
  debts: DebtRow[];
  plan: DebtSchedule;               // the chosen method
  alt: DebtSchedule;                // the other method, same money
  minimums: DebtSchedule;           // minimums only, nothing rolled
  estimated: boolean;               // some input is a labelled estimate
}
export interface DebtPlanSave {
  method: DebtMethod; extra_monthly: number | string;
  debts: Record<string, { apr?: number | string | null;
                          min_payment?: number | string | null;
                          skip?: boolean }>;
}

export interface RetirementData {
  // null while some age works; otherwise the extra monthly saving, from
  // now, that would make each listed age work (empty: no saving would)
  catch_up?: { age: number; extra_monthly: number }[] | null;
  buckets: { cash: number; taxable: number; td: number; roth: number; total: number };
  rows: { age: number; at_retire: number; feasible: boolean;
          fail_age: number | null; max_spend: number; hist_pct: number | null }[];
  earliest: number | null;
  real_return: number;
  spend: number;
  spend_now: number;
  growth_path: [string, number][];
  grow_to_67: number;
  stress: { label: string; earliest: number | null; max_spend_at_ref: number }[];
  stress_ref_age: number;
  hist: { ok: number; n: number; pct: number; spend90: number;
          spend100: number; stock_frac: number } | null;
  inputs: RetirementInputs;
}

export interface BizData {
  rows: { id: string; date: string; amount: number; payee: string;
          category: string; account: string | null; pending: number;
          note?: string | null; flagged_at: string }[];
  total: number; count: number; years: number[];
}

export interface PendingReimb {
  id: string; date: string; amount: number; payee: string;
  category: string; pending: number; account: string | null;
  flagged_at?: string | null;
  // partial expectation + progress toward it
  partial?: number; expected?: number | null; received?: number;
}
export interface ReimbCandidate {
  id: string; date: string; amount: number; payee: string;
  account: string | null;
  // 1 while the bank still holds the deposit as pending
  pending?: number;
  // what of it no link has claimed yet — the figure the server ranks by
  left_amount?: number | null;
}
/** The candidates door's anchor: what it still needs (`left_amount`) and
 *  what already came back to it (`received`), the figures the matcher
 *  words its rows and its gap line by. */
export interface ReimbAnchor {
  id: string; date: string; amount: number;
  left_amount?: number | null; received?: number | null;
}
export interface ReimbPairsPage { pairs: ReimbPair[]; more?: boolean }
export interface ReimbPair {
  expense_id: string; reimburse_id: string; partial: number;
  received: number | null; created_at: string;
  expense_date: string; expense_amount: number; expense_payee: string;
  deposit_date: string; deposit_amount: number; deposit_payee: string;
}

export interface ImportResult {
  error?: string; imported?: number; skipped_duplicates?: number;
  // Mapped-CSV refusals only. `token` is a fresh claim token for the same
  // upload — the mapping is correctable, submit again with it. `expired`
  // says the upload is gone for good and the file has to be re-uploaded.
  token?: string; expired?: boolean;
  // a restore that went to the background rather than finishing inline
  restore_started?: boolean; job?: string;
  source?: string; confidence?: number; warnings?: string[];
  // multi-account competitor exports split per vendor account
  note?: string; split_accounts?: Record<string, number>;
  // rows an earlier import stored under a reading this import corrected
  corrected?: number;
  // a YNAB register placed per account: rows an earlier whole-register
  // import left on the picked account that were removed, kept because a
  // person had added something to them, or left because nothing proves
  // they came from this file
  removed_earlier?: number; kept_annotated?: number; foreign_left?: number;
  // rows dated before the household's history window, left out
  skipped_older?: number;
}
export interface RestoreProgress {
  state: "idle" | "running" | "done" | "error";
  started_at?: string | null;
  progress: {
    table?: string | null; rows?: number; tables_done?: number;
    error?: string; result?: ImportResult;
  };
}
// A folder import runs as a background job: the run door answers with its
// status at once and the page polls it until it settles.
export interface BulkJobFile {
  index: number; name: string;
  state: "pending" | "running" | "done" | "failed" | "skipped";
  ok?: boolean; imported?: number; error?: string;
  account_id?: string | null;
  // what the file's import asks the person to check (lines whose direction
  // was read from their wording), and how many rows an earlier import read
  // differently it corrected — the single upload's warnings and corrected
  warnings?: string[]; corrected?: number;
  // rows the history window left out of this file
  skipped_older?: number;
  // an export ZIP in the folder restores on its own background job;
  // RestoreProgress reports how that ends
  restore_started?: boolean;
}
export interface BulkJob {
  job_id: string;
  state: "queued" | "running" | "cancelling" | "cancelled" | "stopped"
    | "done" | "error";
  // why a stopped job stopped: "signed_out" | "role_changed" | …
  stop_reason?: string | null;
  total: number; done: number;
  started_at?: string | null; updated_at?: string | null;
  files: BulkJobFile[];
  restore_started?: boolean;
  // no word from the worker for a while — waiting, not failing
  waiting?: boolean; note?: string;
}
export type BulkJobStatus = BulkJob | { state: "idle" };
export const bulkJobLive = (s?: { state: string } | null): boolean =>
  !!s && (s.state === "queued" || s.state === "running"
          || s.state === "cancelling");
// A CSV whose columns could not be read: the person maps them.
export interface ColumnMappingNeeded {
  kind?: "columns";
  token: string; header: string[]; filename: string;
  // first rows of the file — so the mapping is confirmed against real
  // data instead of column names alone
  sample?: string[][];
}
// One YNAB account a register names; `name` is the key the answer uses
// ("" for lines with no account name), `label` what the person reads.
export interface YnabAccountGroup {
  name: string; label: string; rows: number;
  first_date: string; last_date: string;
  // rows an earlier import of the whole register left on the picked account
  stored_on_pick: number;
}
// A YNAB register of several accounts: the person says where each goes.
export interface YnabAccountsNeeded {
  kind: "ynab_accounts";
  token: string; filename: string; pick: string;
  accounts: YnabAccountGroup[];
}
export type MappingNeeded = ColumnMappingNeeded | YnabAccountsNeeded;
// Where one YNAB account goes: an account id, "skip", or a new account.
export type YnabDestination = string | { new: string };
export interface ImportOutcome {
  result: ImportResult | null;
  mapping_needed: MappingNeeded | null;
}
export interface Batch {
  id: string; created_at: string; filename: string | null;
  source: string; row_count: number | null;
  // receipts, notes, pairings, tags and overrides people added to this
  // batch's rows since — a rollback deletes them too
  annotations?: number;
}

export interface CalendarData {
  days: number; start: string;
  calendar: { date: string; total: number;
              events: { label: string; amount: number; kind: string }[] }[];
}

export type WizardStep =
  "appearance" | "connect" | "bills" | "budgets" | "email" | "finish"
  // legacy marks still accepted by the API if present in old configs
  | "sync" | "import";
// script tokens — plaintext appears only in the mint response.
// scope: push = the import doors (a collector); read = the integrations
// doors (a scraper, a sensor, an MCP server). Never both.
export type TokenScope = "push" | "read";
export interface ApiToken {
  id: string; name: string; created_at: string;
  last_used_at: string | null; revoked_at: string | null;
  scope: TokenScope;
}

// outbound webhooks (Settings → Integrations) — the secret appears only
// in the create and rotate responses
export interface Webhook {
  id: string; url: string; name: string; events: string[]; enabled: boolean;
  created_at: string; created_by: string;
  last_attempt_at: string | null; last_status: number | null;
  last_error: string | null; failures: number; disabled_reason: string | null;
}
export interface WebhookEvent { name: string; description: string }
export interface WebhookDelivery {
  id: string; event: string; state: "pending" | "delivered" | "failed";
  attempts: number; next_attempt_at: string; created_at: string;
  delivered_at: string | null; response_status: number | null;
  last_error: string | null;
}

export interface Onboarding {
  accounts: number; transactions: number; bills: number;
  connections: number; pending_proposals: number;
  budgets_set: boolean; primary_checking_set: boolean;
  wizard_done: boolean;
  wizard_steps: Partial<Record<WizardStep, "done" | "skipped">>;
  // the setup choices the wizard made, read back so a resumed wizard and
  // its closing summary show what was chosen (absent from older servers)
  history_months?: HistoryMonths;
  setup_mode?: SetupMode | null;
  bill_detection_auto?: boolean | null;
}
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
export interface ProposalAdjust {
  name?: string; amount?: number; cadence?: string; due_day?: number;
}
// live progress of the background sync the wizard starts
export interface SyncProgressItem {
  id: string; name: string; status: "pending" | "running" | "ok" | "error";
  transactions: number; error?: string;
}
export interface BackfillStatus {
  items: { id: string; institution: string | null; transactions: number;
           earliest: string | null;
           // 0..1: how much of the two-year window the oldest landed row
           // covers; 1 only once the bank says the window is complete
           progress: number;
           // a pull touched this connection in the last 90s
           active: boolean;
           ready: boolean; stalled: boolean }[];
  all_ready: boolean;
  syncing: boolean;
}

export interface SyncStatus {
  state: "none" | "running" | "done" | "error";
  progress: {
    items?: SyncProgressItem[];
    categorize?: { done: number; total: number; failed?: number } | null;
    ok?: boolean; error?: string;
  };
}
export interface HistoryCoverage {
  accounts: { id: string; name: string; type: string | null;
              earliest: string | null; latest: string | null;
              transactions: number }[];
}

export interface Session {
  id: string; created_at: string; last_seen: string | null;
  expires_at: string; user_agent: string; ip: string | null;
  current: boolean;
}

export interface MobileDevice {
  id: string; device_name: string; platform: string;
  created_at: string; last_seen: string | null; current: boolean;
  /** "on" = registered for push, "dead" = platform says gone, null = never */
  push?: "on" | "dead" | null;
}

export interface DoctorCheck {
  section: string; name: string; ok: boolean; severity: string;
  progress?: number; busy?: boolean;
  detail: string;
}

/** One part of a hand-split charge (Txn.split) */
export type SplitPart = { category: string; amount: number };

export interface ReceiptItem {
  line: number; description: string; qty: number | null; amount: number;
  tag: string;
}
// tax & income documents
export interface TaxdocRow {
  year: number; exists?: boolean;
  total_income?: number | null; agi?: number | null;
  taxable_income?: number | null; tax_paid?: number | null;
  wages?: number | null; ss_earnings?: number | null;
  medicare_earnings?: number | null; primary_wages?: number | null;
  employer?: string | null; ein?: string | null;
  boxes?: Record<string, number>; box1?: number | null;
}
export interface TaxdocPlan { token: string; kind: string; rows: TaxdocRow[] }

// items view
export interface ReceiptItemRow {
  receipt_id: string; line: number; description: string;
  qty: number | null; amount: number; tag: string;
  // a receipt still waiting for its charge has no transaction yet: its date
  // and store are the ones read off it, and the store may be unknown
  txn_id: string | null; date: string; payee: string | null;
}
export interface ReceiptItemGroup {
  // null: a store group for receipts whose store is unknown
  key: string | null; n: number; total: number; last_date: string | null;
}
/** What an unknown store is called on the Receipts page. */
export const UNKNOWN_STORE = "Unknown store";

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
  // how a receipt snapped before its charge found it: the matcher ('auto')
  // or a person ('manual'); null when attached from the transaction itself
  match_method?: "auto" | "manual" | null;
  // when the current parse attempt began, or the server's own verdict that
  // it has outlived the reclaim window — either lets a stalled read be
  // retried (both absent from older servers)
  parse_started_at?: string | null; parse_stale?: boolean;
  matched_at?: string | null;
  error: string | null; created_at: string; items: ReceiptItem[];
}

// receipts snapped before their transaction reached the ledger
export interface WaitingCandidate {
  txn_id: string; date: string; amount: number; payee: string | null;
  account: string | null; pending: boolean; exact: boolean;
  merchant_match: "same" | "different" | "unknown";
}
export interface WaitingReceipt {
  id: string; mime: string; kind: "receipt";
  status: Receipt["status"]; parsed: Receipt["parsed"];
  error: string | null; created_at: string; has_optimized: boolean;
  // what the matcher reads off it: the parsed total and store, and the
  // printed date (else the day it was snapped)
  total: number | null; date: string | null; merchant: string | null;
  // two charges fit equally well — the matcher leaves the choice to you
  tie: boolean;
  // still waiting stale_days after its date
  stale: boolean;
  // nothing is going to read its total: no AI on this instance, or the AI
  // could not — it waits on a person to type the total and date in
  needs_details: boolean;
  // its total and date were typed in by a person; no parse overwrites them
  manual: boolean;
  // a person had paired it by hand and that charge was later removed: it is
  // back here, and the matcher leaves it for the person to pair again
  released?: boolean;
  // when the current read began, and whether it has outlived the reclaim
  // window (both absent from older servers)
  parse_started_at?: string | null; parse_stale?: boolean;
  candidates: WaitingCandidate[];
}
export interface RecentMatch {
  id: string; mime: string; parsed: Receipt["parsed"];
  has_optimized: boolean; matched_at: string;
  match_method: "auto" | "manual"; txn_id: string; txn_date: string;
  txn_amount: number; payee: string | null;
}
export interface WaitingReceipts {
  count: number; stale_days: number;
  // this instance has a vision model to read receipts with
  ai_available: boolean;
  // `count` is every receipt waiting; `waiting` is one page of them, read
  // from `offset` (at most `limit` per page)
  limit?: number; offset?: number;
  waiting: WaitingReceipt[]; recent: RecentMatch[];
}

export interface AccountLinkInfo {
  group_id: string; home_rank: number; primary: boolean; healthy: boolean;
}
export interface LinkSuggestion {
  key: string;
  a: { id: string; name: string; mask: string; institution_name: string | null };
  b: { id: string; name: string; mask: string; institution_name: string | null };
}
export interface LinkGroupMember {
  account_id: string; home_rank: number; healthy: boolean; primary: boolean;
}
export interface LinkGroup { group_id: string; members: LinkGroupMember[] }

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

export interface ScriptHeartbeat {
  source: string; label: string | null;
  first_push: string; last_push: string; last_rows: number | null;
  expected_hours: number | null; alerts: number;
  age_hours: number; status: "fresh" | "stale" | "off";
}

export interface Account {
  link?: AccountLinkInfo | null;
  // present when a collector script feeds this item; ok = a
  // successful push within 26h (Doctor's connection-freshness threshold)
  script?: { label: string | null; via: string | null;
             kind: string | null; source: string; last_push: string;
             ok: boolean;
             // set when a script token named for this source was revoked
             // after the last good push (a password change/reset revokes
             // them all) and no live replacement exists — the one silence
             // the server can explain
             token_revoked_at?: string | null } | null;
  /** Plaid type + subtype joined: `"depository/checking"`, `"credit/credit card"`.
   *  NEVER the bare type — compare with {@link kindIs}, never with `===`. */
  id: string; name: string; mask: string | null; kind: string;
  /** The aggregator's own name, kept under any rename — the editor shows
   *  it beside a custom display name and reverting restores it. */
  bank_name?: string | null;
  balance_current: number | null; balance_available: number | null;
  item_id: string; institution_name: string; status: string | null;
  // institution branding (Plaid optional metadata): base64 PNG, brand hex,
  // site — the Accounts page draws the bank's mark from these
  // the bank's logo as a same-origin URL (/api/institutions/<item>/logo),
  // fetched and cached by the browser rather than shipped base64 per row
  inst_logo_url?: string | null; inst_color?: string | null; inst_url?: string | null;
  owner?: string | null; // household ownership attribution
  entity_id?: string | null; // assigned business entity (null = personal)
  txns: number;
  // a card's next due date + statement balance from the liabilities pull
  card_due?: string | null; card_statement?: number | null;
  /** ISO timestamp when the user soft-removed this account (hide mode). */
  user_removed_at?: string | null;
  aggregator?: string | null;
}
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
export interface Entity {
  id: string; name: string; structure: string; state?: string | null;
  formation_date?: string | null; business_start_date?: string | null;
  ein_last4?: string | null; registered_agent?: string | null;
  fiscal_year_end?: string | null; status: string;
  /** when the business was closed (null while active) */
  archived_at?: string | null;
  home_office_sqft?: number | null; income_tax_rate?: number | null;
  /** The account this entity keeps its tax money in. null = not nominated,
   *  and the set-aside card then measures all business cash and says so. */
  tax_reserve_account_id?: string | null;
  members?: { id: string; member_name: string; ownership_pct: number | null;
              is_manager: boolean;
              /** the member this household is — at most one per business */
              is_household?: boolean }[];
  capital?: Capital;
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
export interface Capital {
  contributions: number; draws: number; distributions: number;
  reimbursements: number; capital_balance: number;
  // capital_balance = contributions + net_income − draws;
  // movements_balance is the narrower contributions − draws sub-total
  net_income?: number; movements_balance?: number;
  /** the household member's ownership % and their share of
   *  capital_balance, when one member is marked "this is us" and carries
   *  a share; null otherwise, absent on older servers */
  household_share_pct?: number | null;
  household_capital?: number | null;
}
export interface EquityMovement {
  id: string; entity_id: string; kind: string; amount: number;
  date: string | null; member_id: string | null; txn_id: string | null;
  form: string | null; note: string | null;
}
export interface BizTxn {
  id: string; date: string | null; amount: number; payee: string;
  category: string | null; note?: string | null;
  /** `transactions.category_detailed` — served by books.entity_transactions
   *  and load-bearing for the P&L exclusions (card payments). */
  cat_detailed: string | null;
  /** the aggregator's original category — a TRANSFER_IN later renamed by
   *  an override is still not revenue (books._pnl_core's was_transfer) */
  cat_original?: string | null;
  /** the amount net of partial reimbursement links (ledger sign kept) —
   *  what the P&L counts; `amount` stays what the bank recorded */
  net_amount?: number | null;
  /** a deposit whose links claim only part of it: still revenue */
  partly_claimed?: boolean | null;
  bucket: string | null; sched_c_line: string | null;
  /** the bucket the P&L counted the row in (a refund: its reversed cost's
   *  bucket); null for money in that is not a refund */
  effective_bucket?: string | null;
}
export interface Deduction {
  total: number; immediate: number; amortizable: number;
  monthly_amortization: number;
}
export interface Pnl {
  year: number | null; revenue: number; operating_expenses: number;
  operating_by_line: Record<string, number>; net_operating: number;
  /** tax the business already paid toward this year (kept off expenses) */
  estimated_tax_paid?: number;
  organizational: number; startup_195: number;
  organizational_deduction: Deduction; startup_deduction: Deduction;
  capital: Capital;
}
export interface Obligation {
  id?: string; title: string; due: string; fee: number | null;
  url: string | null; note: string | null; source: string;
  recurrence: string;
}
export interface EstimatedTax {
  year: number; net_profit: number; mileage_deduction: number;
  home_office_deduction: number;
  /** this year's share of the start-up + organizational allowance */
  startup_deduction: number;
  taxable_profit: number; se_tax: number;
  income_tax_rate: number | null; income_tax: number | null;
  /** se_tax, income_tax and total_estimated are on profit so far in `year` */
  total_estimated: number;
  household?: { member_name: string; ownership_pct: number; share_of_profit: number;
                se_tax: number; income_tax: number | null; total: number } | null;
  /** the business as a whole, beside the household's share */
  business_total?: number | null;
  /** what total_estimated is measured against: the profit it is a rate of
   *  and the estimates already paid toward it — the household member's
   *  share of each when covers is "household", the business's otherwise.
   *  Absent on older servers (use net_profit / estimated_tax_paid). */
  covered_profit?: number;
  covered_tax_paid?: number;
  /** why a figure covering every member is not the household's share:
   *  an S-corp is never split; a marked member whose shares do not total
   *  100% cannot be; or nobody is marked. Null when it is the share. */
  household_unavailable?: "s_corp" | "shares_incomplete" | "none_marked" | null;
  /** a quarter of annualized_total (of total_estimated on older servers) */
  quarterly: number; deadlines: string[];
  /** the next 1040-ES due date, and the installment due on it: from
   *  January 1 to 15 that is last year's fourth, not a quarter of this
   *  year's projection. Null past the year's last deadline; absent on
   *  older servers, which only send `quarterly`. */
  next_deadline?: string | null;
  next_payment?: number | null;
  /** true for the year in progress: the so-far profit projected over the year */
  annualized?: boolean;
  /** actual car costs added back because the standard mileage rate,
   *  which replaces them, was taken; absent on older servers */
  vehicle_expenses_added_back?: number;
  /** which car deduction the estimate took: the standard mileage rate or
   *  the actual costs, whichever is larger; null with neither. Absent on
   *  older servers, as is the mileage log's own figure */
  vehicle_method?: "standard_mileage" | "actual" | null;
  mileage_logged?: number;
  /** what annualized_total stands on: "prior_year" early in the year, when
   *  it is last year's tax rather than a projection of this year's */
  basis?: "projected" | "prior_year" | "year";
  annualized_taxable_profit?: number;
  annualized_total?: number;
  /** false for an S-corp: Social Security and Medicare go through payroll */
  se_tax_applies?: boolean;
  /** whose tax the figure is: the owner's, every member's together, or
   *  the household member's share */
  covers?: "owner" | "all_members" | "household";
  structure?: string | null;
  members?: { member_name: string; ownership_pct: number;
              share_of_profit: number; se_tax: number }[];
}
export interface BalanceSheet {
  assets: { name: string; amount: number }[];
  liabilities: { name: string; amount: number }[];
  total_assets: number; total_liabilities: number; net: number;
  capital_account: number;
  /** the household member's share of capital_account, as on Capital */
  household_share_pct?: number | null;
  household_capital?: number | null;
}
export interface MileageTrip {
  id: string; date: string | null; miles: number; purpose: string | null;
  note: string | null;
}
export interface Vendor {
  merchant: string; paid: number; paid_by_card: number; reportable: boolean;
  tin_last4: string | null; needs_1099: boolean;
}
export interface BusinessSummary {
  entities: Entity[]; flagged_unassigned: number; combined: boolean;
}
export interface Connection {
  id: string; aggregator: string; institution_name: string | null;
  status: string | null; last_ok: string | null;
  // last_ok is when WE last read Plaid's copy; bank_updated is when the
  // BANK last refreshed that copy. Showing only the first makes a correct
  // no-op sync look like a broken one.
  bank_updated_at?: string | null; bank_failed_at?: string | null;
  // what the status MEANS, classified server-side: ok | retrying (bank
  // outage — nothing to do) | reauth (sign in again / share accounts) |
  // gone (Item released) | attention (unclassified). Never re-derive
  // this from `status`: an institution outage is not a broken login,
  // and offering update mode for one is what makes the warning noise.
  status_kind?: "ok" | "retrying" | "reauth" | "gone" | "attention";
  // Plaid's fleet-wide login health for this BANK (HEALTHY | DEGRADED |
  // DOWN), cached by the hourly sync. Distinct from status_kind: the
  // item can be green while the bank's own OAuth is failing for
  // everyone — this is the context for a reconnect that dies on the
  // bank's error page. Absent/stale (>24h) readings are dropped
  // server-side.
  institution_health?: string | null;
  // why a billed product returns nothing for this item, stamped by the
  // sync: 'consent' (never granted — a re-link in update mode can attach
  // it) vs 'not_supported' (the bank cannot provide it). Explains a cash
  // forecast with no autopay line for this card instead of silence.
  product_issues?: Record<string, { cause: "consent" | "not_supported";
                                    code?: string }> | null;
  // accounts this connection feeds (the linked-bank rows show it)
  accounts?: number;
}
export interface Holding {
  acct: string; symbol: string; fund: string | null;
  qty: number | null; price: number | null; value: number;
}
// config manual_assets[] — property/vehicle/mortgage entries, auto-valued
// server-side; edited on Net Worth
export interface ManualAsset {
  name: string; value: number; kind?: string | null; as_of?: string | null;
  auto?: string | null;
  rate?: number | null; monthly_delta?: number | null;
}
// config custom_buckets[]: a named carve-out of a parent variable
// bucket — categories + merchant overrides route spend into it for display
export interface CustomBucket {
  name: string; parent: "food" | "other"; monthly: number;
  categories: string[]; merchants: string[];
}
// /api/budgets/suggest — trailing 6-month medians (current month excluded,
// outlier months >2× the median excluded), one number per bucket
export interface BudgetSuggestions {
  months: string[];
  suggestions: { food_monthly?: number; other_monthly?: number;
                 income_monthly?: number; buckets: Record<string, number> };
  samples: Record<string, [string, number][]>;
  // wizard carve-out chips + the savings-surplus input
  bucket_candidates?: { name: string; category: string; monthly: number }[];
  avg_bills_monthly?: number;
  bills_count?: number;
  // the history behind the suggestions: days of ledger in the window,
  // whether every figure is a partial month scaled up, and the fewest
  // days the scaling ever stretches (the planner's history line)
  history_days?: number; history_scaled?: boolean; min_rate_days?: number;
  // no finished day of ledger yet: no suggestion is offered
  not_enough_history?: boolean;
}
export interface Settings {
  food_monthly: number | null; other_monthly: number | null;
  custom_buckets: CustomBucket[] | null;
  budgeted_income_monthly: number | null;
  dynamic_variable_budget: boolean | null;
  email_recipients: string[] | null; email_send_hour_utc: number | null;
  // addresses that opted out of the scheduled mail (owner included) —
  // lowercased on the server
  email_muted?: string[] | null;
  // the saved recipients, each with whether our mail actually
  // reaches them. Owner first and non-removable (they are mailed by
  // construction). `verified` is null when the address holds no account
  // here — "unverified" would be a claim about someone else's mailbox we
  // have no standing to make.
  email_recipient_status?: {
    email: string; owner: boolean; member: boolean;
    // asked not to receive the scheduled mail (per-person off switch;
    // the owner can mute themselves too)
    muted?: boolean;
    // which kinds of mail this address receives (absent = all of them)
    prefs?: Partial<Record<EmailKind, boolean>>;
    verified: boolean | null;
    delivery: "bouncing" | "complained" | null;
    delivery_reason: string | null;
    // null for the owner (never invited to their own mail) and for
    // an address the server has never been asked to invite; otherwise the
    // state of that invitation. Only "accepted" receives mail.
    invite?: "invited" | "accepted" | "declined" | "expired" | null;
    invited_at?: string | null;
    // why this address receives the mail. "member" means the household has
    // configured NO list, so the worker mails every verified user — those
    // rows were previously not shown at all.
    via?: "owner" | "member" | "invited";
  }[];
  email_schedule: {
    // daily.summary: send just the verdict pane (tiles + pinned bills)
    // instead of the full report
    daily?: { on: boolean; hour: number; sms?: boolean; push?: boolean;
              summary?: boolean };
    monthly?: { on: boolean; hour: number; sms?: boolean; push?: boolean };
    // annual report card + per-cadence SMS/push channel flags
    yearly?: { on: boolean; hour: number; sms?: boolean; push?: boolean };
  } | null;
  checking_account_id: string | null;
  // retirement / income-scenario keys
  birthdate: string | null;
  /** /retirement/setup progress. `done` is "done" or "skipped" — either way
   *  the walkthrough is finished and must not re-open itself. */
  ret_wizard_steps?: Record<string, string | null> | null;
  ss_estimates: Record<string, number> | null;
  retirement_saving: boolean | null;
  income_biweekly_saving: number | null;
  income_biweekly_not_saving: number | null;
  // read-only surfaces (each written by its own feature/page)
  excluded_accounts: string[] | null;
  occurrence_caps: Record<string, number> | null;
  disabled_bills: string[] | null;
  dismissed_hints: Record<string, unknown> | null;
  manual_assets: ManualAsset[] | null;
  // the Net Worth page's investment fee estimate and money-in anchors:
  // {institution-name fragment: yearly rate as a FRACTION}, the masks of
  // fee-free accounts there, {account mask: dollars put in}
  advisory_fees?: Record<string, number> | null;
  advisory_fee_exempt_masks?: string[] | null;
  investment_basis_override?: Record<string, number> | null;
  // smart-categorization LLM backend (key is write-only: presence flag)
  llm_url: string | null; llm_model: string | null;
  /** named AI backends (several at once, local or cloud); keys are
   *  write-only — api_key_set is all the server ever reports. extra_body
   *  is masked the same way (it can carry credentials): the value comes
   *  back "" with extra_body_set as the presence flag; on save, ""/omitted
   *  keeps the stored JSON and an explicit null clears it */
  llm_backends: { id: string; name: string; url: string; model: string;
                  vision_model: string; extra_body: string;
                  extra_body_set: boolean;
                  api_key_set: boolean; local: boolean }[];
  /** task → backend id (or "bundled"); absent task = legacy/env default */
  llm_roles: Record<string, string>;
  /** the operator/env backend (bundled Ollama) — never present on hosted */
  llm_bundled: { url: string; model: string; local: boolean } | null;
  /** the built-in trained classifier is doing first-pass categorization
   *  (seed rules → model → LLM sees only the abstentions) */
  categorizer_active: boolean;
  /** per task, the backend actually answering right now; null = no AI */
  llm_effective: Record<string, { source: string; name?: string; url: string;
                                  model: string; local: boolean } | null>;
  llm_vision_model?: string | null;
  /** "1" = tax documents (W-2/1040, which carry a full SSN) may be sent to a
   *  REMOTE llm_url. Off by default; no effect for a local model. */
  taxdocs_allow_remote_llm?: string | null;
  llm_api_key_set: boolean;
  // the owner's UI theme; "auto" follows the OS, and so does null/absent
  // (never chosen)
  theme: "auto" | "light" | "dark" | null;
  // setup choices (the wizard writes them; Import writes history_months
  // too): see HistoryMonths / SetupMode
  history_months?: HistoryMonths;
  setup_mode?: SetupMode | null;
  bill_detection_auto?: boolean | null;
  /** the household's IANA zone; null = follow the instance (OIKONOME_TZ) */
  timezone: string | null;
  // feedback pipeline on/off (null/absent = on)
  feedback_enabled: boolean | null;
  // merchant logos on the ledger and everywhere a merchant is named
  // (absent/true = on; false = the minimal look)
  merchant_logos?: boolean | null;
  // default landing page per client — the route the web app opens on and
  // the tab the mobile app opens on (absent = Today). Household-level,
  // like theme: the settings document has no per-user half.
  home_web?: string | null;
  home_mobile?: string | null;
  // Plaid's recurring streams as a bill-detection cross-check (absent = on)
  plaid_recurring?: boolean | null;
  merchant_strip_cities?: string[];
  merchant_cities_seen?: string[];
  // provider key presence (secrets write-only)
  plaid_client_id: string | null; plaid_env: string | null;
  plaid_secret_set: boolean;
  mx_client_id: string | null; mx_env: string | null;
  mx_api_key_set: boolean;
  /** legacy single-endpoint JSON options, masked exactly like the
   *  per-backend twin above: the value comes back "" and
   *  llm_extra_body_set is the presence flag */
  llm_extra_body: string | null;
  llm_extra_body_set: boolean;
  // named income scenarios (one active) + savings goals
  income_scenarios: IncomeScenario[];
  active_scenario: string | null;
  savings_goals: SavingsGoal[];
  // web-configured SMTP (password write-only: presence flag)
  smtp_host: string | null; smtp_port: number | null;
  smtp_user: string | null; smtp_from: string | null;
  /** absent = on. Off is for a plain LAN relay that speaks no STARTTLS. */
  smtp_starttls?: boolean | null;
  // what a credential rotation paused until the owner confirms it
  // (null = nothing): the Settings relay, extra recipients, the AI endpoint
  outbound_paused?: { at: string; reason: "rotation" | "recovery";
                      smtp: boolean; recipients: string[]; llm: boolean;
                      /** members an owner's recovery removed (joined in the
                       *  last days through the owner's own invites) */
                      removed_members?: { email: string; role: string;
                                          joined: string }[];
                      /** what a non-owner is told instead of the names */
                      removed_members_count?: number;
                      /** true while new bank links are held on the
                       *  household's own Plaid / MX keys (the bank-link
                       *  hold): a new link, a re-auth and the MX widget are
                       *  refused until the owner resumes or removes the
                       *  keys. Syncs of banks already linked keep running */
                      aggregator?: boolean;
                      /** owner only: which clients are held (ids are not
                       *  secrets); saved_at null = saved before the stamp */
                      aggregator_keys?: { provider: "plaid" | "mx";
                                          client_id: string; env: string;
                                          saved_at: string | null }[];
                      /** a hash of the record; the resume names it so a
                       *  record that changed since it was read is refused */
                      version?: string } | null;
  smtp_password_set: boolean;
}

// income scenarios + ledger-driven savings goals
export interface IncomeScenario {
  name: string; take_home: number; cadence: string;
}
export interface SavingsGoal {
  name: string; target: number; target_date?: string | null;
  monthly_plan: number; account_id?: string | null;
  tokens: string[]; start_balance: number;
  // absent = "monthly" (fixed commitment); "sweep" contributes at month
  // end, only what the month's realized surplus covers
  mode?: "monthly" | "sweep" | null;
}
/** a member an owner's credential change (or recovery) removed: they
 *  joined in the last days through the owner's own invites */
export interface RemovedMember { email: string; role: string; joined: string }

export interface SavingsGoalProgress {
  name: string; target: number; target_date: string | null;
  monthly_plan: number;
  // null on a plan-only goal (no destination account) — with no account,
  // ledger matching counts both legs of every transfer, never the fund
  saved: number | null; rate_90d: number | null;
  // this month's posted contribution — null on a plan-only goal, absent
  // from older servers (the rows then fall back to rate_90d, labelled
  // as the average it is)
  saved_this_month?: number | null;
  pct: number | null; eta: string | null; on_pace: boolean | null;
  plan_only: boolean;
  // a plan-only goal that has match words: it needs an account to be
  // measured (absent from older servers, read as false)
  needs_account?: boolean;
  mode?: "monthly" | "sweep";
  // sweep goals only: matched-transfer evidence this month and whether
  // it already covers the plan (the month is satisfied)
  swept_this_month?: number | null;
  sweep_satisfied?: boolean | null;
}
export const CADENCE_MONTHLY: Record<string, number> = {
  weekly: 4, biweekly: 2, semimonthly: 2, monthly: 1,
};

// engine/budget.active_monthly_income, client-side: monthly budgeted income
// switched by the retirement-saving toggle (biweekly → monthly is ×2)
export const activeMonthlyIncome = (s: Settings): number | null => {
  const scen = s.income_scenarios ?? [];
  if (scen.length) {
    const a = scen.find((x) => x.name === s.active_scenario) ?? scen[0];
    return a.take_home * (CADENCE_MONTHLY[a.cadence] ?? 2);
  }
  return s.budgeted_income_monthly;
};

export type MerchantDetail = {
  display: string; rows: number; total: number;
  first: string; last: string; variants: string[];
  logo?: string | null; website?: string | null; phone?: string | null;
  kind?: string | null; mcc?: string | null; plaid?: boolean;
  parent?: string | null; category?: string | null;
  rule?: { category_primary: string; source: string } | null;
  locations: { city: string | null; region: string | null;
               address: string | null; postal: string | null;
               lat: number | null; lon: number | null;
               n: number; last: string }[];
};

// The server applies up to 10,000 ids in one bulk write, all or nothing,
// so a selection up to that size goes in ONE request: split, a failure
// part-way would leave the first pieces applied and the rest not. Past
// BULK_MAX it is not offered at all: a write that size is a rule, not a
// selection. BULK_CHUNK stays the server's cap so the loop below can only
// split what one request could never carry.
export const BULK_MAX = 10000;
export const BULK_CHUNK = BULK_MAX;

export const api = {
  me: () => req<Me>("/api/me"),
  // pre-auth; only a single-user demo-locked instance ever returns creds
  // how a person without an account (or locked out of one) gets help
  // on this instance — public, read before any login
  access: () => req<{ hosted: boolean; signup_path: string | null;
                      site_url: string | null;
                      support_email: string | null;
                      android_test_url?: string | null }>("/api/access"),
  demoLogin: () =>
    req<{ demo: boolean; email?: string; password?: string }>("/api/demo-login"),
  login: (email: string, password: string, totp?: string) =>
    req<{ ok: boolean }>("/api/login",
      form({ email, password, ...(totp ? { totp_code: totp } : {}) })),
  // Sign-out goes through signOut() in push.ts, which names this
  // browser's push endpoint so the server drops its subscription too.
  logout: (push_endpoint?: string) =>
    json("/api/logout", push_endpoint ? { push_endpoint } : {}),
  // date (optional, YYYY-MM-DD) renders a past day — omit for live today
  todayFull: (date?: string) =>
    req<TodayFull>(`/api/today/full${date ? `?date=${date}` : ""}`),
  // timeframe lenses
  lensMonth: (y: number, m: number) =>
    req<MonthLensData>(`/api/lens/month?y=${y}&m=${m}`),
  lensYear: (y: number) => req<YearLensData>(`/api/lens/year?y=${y}`),
  budgetSnapshots: () => req<BudgetSnapshots>("/api/budget/snapshots"),
  budgetBackfill: () =>
    req<{ backfilled: number }>("/api/budget/snapshots/backfill",
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
    json<{ ok: boolean; applied: number; missing: number; flow: number }>(
      "/api/transactions/bulk", { ids, action, category }),
  // An authorization the bank left pending and never posted or released.
  // Sync can't drop it — an absent row is indistinguishable from one the
  // feed simply didn't return this time — so only the user can retire it.
  // The server refuses a row that is not pending or is younger than two
  // weeks; its detail text is what the caller shows.
  retirePending: (id: string) =>
    json<{ ok: boolean; id: string }>(
      "/api/transactions/retire-pending", { id }),
  transactions: (params: Record<string, string>) =>
    req<{ mode: string; rows: Txn[]; total?: number; amount_sum?: number;
          amazon_items?: { count: number; sum: number;
                           unpriced: number } | null;
          // search only: the per-transaction average over the whole result
          // set, spending rows only. null when nothing matched is spending.
          spend_count?: number; spend_sum?: number; spend_avg?: number | null;
          // average spending per day over the window (a month so far, a
          // bucket's month, a date range); null when there is no window
          per_day?: number | null; per_day_days?: number | null;
          // month mode: the month's cash flow by the Cash Flow page's
          // definitions (spend / income / income − spend) — transfers and
          // card payments are excluded, so these are not row sums. In and
          // net are null for a month before the bank feed starts: its
          // income is unknown, not $0
          out_sum?: number; in_sum?: number | null; net_sum?: number | null;
          // how many of the listed/matched rows belong to a business (not
          // in the totals unless scope=business)
          biz_count?: number;
          // search only: accounts the term matched by name (rename or the
          // bank's own) — via_bank marks a renamed account found through
          // the bank's name, so the page can explain those rows
          account_hits?: { id: string; label: string; bank_name: string;
                           via_bank: boolean }[];
          // bucket listing: the verdict grouping the rows were counted in
          // ('food', 'other', or a custom bucket's name) and the name to
          // show for it, so the page states the scope in the plan's words
          bucket?: string; bucket_label?: string;
          // the figure the label wore (reimbursements netted, envelope
          // overflow counted as the plan counts it) and whether the door
          // was a month's or a whole year's
          bucket_amount?: number; bucket_scope?: "month" | "year" }>(
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
    req<{ merchants: { display: string; rows: number; total: number;
                       first: string; last: string; variants: string[];
                       variant_count?: number; business_rows?: number;
                       // the merchant row's facts (Plaid identity)
                       logo?: string | null; website?: string | null;
                       kind?: string | null; plaid?: boolean;
                       parent?: string | null }[];
          recent: { id: number; raw_merchant: string;
                    from_canonical: string | null; to_canonical: string;
                    at: string }[];
          // merchants the ledger thinks are one business, for the person
          // to merge or keep apart (never merged unasked)
          merges?: MergeProposal[] }>(
      "/api/merchants/catalog?" + new URLSearchParams({
        q, order,
        ...(opts.sort ? { sort: opts.sort } : {}),
        ...(opts.filter && opts.filter !== "all" ? { filter: opts.filter } : {}),
        ...(opts.limit ? { limit: String(opts.limit) } : {}) })),
  // approve = merge through the journalled rename (undo works); reject =
  // keep apart, never offered again
  merchantMerge: (pid: string, action: "approve" | "reject") =>
    json<{ ok: boolean }>("/api/merchants/merge", { pid, action }),
  // one merchant, opened: facts, category, and where it has been seen
  merchantDetail: (display: string) =>
    req<MerchantDetail>("/api/merchants/detail?"
                        + new URLSearchParams({ display })),
  // rename and merge are one call: naming an existing merchant merges
  merchantRename: (body: { display: string; to: string }) =>
    json<{ raws: number; rows: number }>("/api/merchants/rename", body),
  merchantUndo: (id: number) =>
    json<{ raw_merchant: string; restored: string | null }>(
      "/api/merchants/undo", { id }),
  // categories: in use by this tenant (filters). plaid_*: the standard
  // Plaid primaries, for pickers that set a category.
  categories: () => req<{ categories: string[]; plaid_spend: string[];
                          plaid_flow: string[] }>("/api/categories"),
  // Rename a free-form / custom category string everywhere it appears
  // (overrides, merchant rules, budget carve-outs). Owner-only.
  categoryRename: (oldName: string, newName: string) =>
    json<{ old: string; new: string; overrides: number; primaries: number;
           manual: number; rules: number; buckets: number }>(
      "/api/categories/rename", { old: oldName, new: newName }),
  accounts: () => req<{ accounts: Account[] }>("/api/accounts"),
  // primary_checking left out = the household's anchor stays put
  accountClassify: (account_id: string, kind: string,
                    primary_checking?: boolean) =>
    json("/api/accounts/classify", { account_id, kind,
      ...(primary_checking === undefined ? {} : { primary_checking }) }),
  accountAdd: (name: string, kind: string, balance = "") =>
    json<{ ok: boolean; account_id: string }>("/api/accounts/add",
                                              { name, kind, balance }),
  accountRename: (account_id: string, name: string) =>
    json("/api/accounts/rename", { account_id, name }),
  accountExclude: (account_id: string, excluded: boolean) =>
    json<{ ok: boolean; account_id: string; excluded: boolean }>(
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
  owners: () => req<{ owners: string[] }>("/api/owners"),
  setAccountOwner: (account_id: string, owner: string) =>
    json(`/api/accounts/${encodeURIComponent(account_id)}/owner`, { owner }),
  setTxnOwner: (txn_id: string, owner: string) =>
    json(`/api/transactions/${encodeURIComponent(txn_id)}/owner`, { owner }),
  connections: () =>
    req<{ connections: Connection[]; last_sync: string | null;
          syncing: boolean;
          // `phase` names the post-pull stage (products/categorize)
          sync_progress: { done: number; total: number;
                           phase?: string | null } | null }>(
      "/api/connections"),
  holdings: () => req<{ holdings: Holding[] }>("/api/holdings"),
  // manual per-connection pull. A failed or refused pull is an error
  // status, so it lands in the caller's onError, not its success notice
  accountSync: (item_id: string) =>
    json<{ ok: boolean; message: string }>("/api/accounts/sync", { item_id }),
  accountBalance: (account_id: string, balance: string) =>
    json("/api/accounts/balance", { account_id, balance }),
  simplefinConnect: (token: string) =>
    json<{ ok: boolean; accounts: number; transactions: number }>(
      "/api/accounts/simplefin", { token }),
  settings: () => req<Settings>("/api/settings"),
  // Plaid Enrich for imported history — status + a capped run (owner)
  enrichStatus: () => req<{ candidates: number; plaid_configured: boolean;
                            cap: number; used: number; month: string;
                            remaining: number }>("/api/import/enrich"),
  enrichRun: (limit = 100) =>
    json<{ sent: number; enriched: number; cap_hit: boolean;
           error: string | null }>("/api/import/enrich", { limit }),
  onboarding: () => req<Onboarding>("/api/onboarding"),
  // script tokens (bearer credentials for host-side collectors)
  tokens: () => req<{ tokens: ApiToken[] }>("/api/tokens"),
  // both are elevation-gated (a durable credential): no password in the
  // body — the wrapper's sheet is the proof
  tokenMint: (name: string, scope: TokenScope = "push") =>
    json<ApiToken & { token: string }>("/api/tokens", { name, scope }),
  tokenRevoke: (id: string) => json("/api/tokens/revoke", { id }),
  // outbound webhooks — create and a new URL are elevation-gated (the
  // ledger gets posted there); the rest are plain owner doors
  webhooks: () =>
    req<{ webhooks: Webhook[]; events: WebhookEvent[] }>("/api/webhooks"),
  webhookCreate: (body: { url: string; events: string[]; name: string }) =>
    json<Webhook & { secret: string }>("/api/webhooks", body),
  webhookUpdate: (id: string, body: Partial<Pick<Webhook,
                  "url" | "events" | "name" | "enabled">>) =>
    json<Webhook>(`/api/webhooks/${id}`, body),
  webhookDelete: (id: string) => json(`/api/webhooks/${id}/delete`, {}),
  webhookTest: (id: string) =>
    json<{ ok: boolean; status: number | null; error: string | null }>(
      `/api/webhooks/${id}/test`, {}),
  webhookRotate: (id: string) =>
    json<Webhook & { secret: string }>(`/api/webhooks/${id}/rotate`, {}),
  webhookDeliveries: (id: string) =>
    req<{ deliveries: WebhookDelivery[] }>(
      `/api/webhooks/${id}/deliveries?limit=20`),
  // `hidden`: the roster is withheld (a demo, or a login every visitor
  // shares) — say nothing rather than an empty "signed in" list
  sessions: () => req<{ sessions: Session[]; hidden?: boolean }>("/api/sessions"),
  // mobile devices (long-lived app credentials, revocable like sessions)
  devices: () => req<{ devices: MobileDevice[]; hidden?: boolean }>("/api/devices"),
  // signing another device out is a posture change — elevation-gated
  deviceRevoke: (body: { id: string }) =>
    json<{ ok: boolean; revoked: number }>("/api/devices/revoke", body),
  // notification channels (SMS / web push)
  notifyStatus: () => req<NotifyStatus>("/api/notify"),
  // `consent` is the list of message programs ticked ("summary", "alerts"),
  // sent with the number so the server records WHEN consent was given, for
  // WHICH number, and for WHAT. Each program is its own checkbox — carriers
  // require consent per program. The server refuses an empty list: carriers
  // want evidence, not a UI gate.
  notifyPhone: (number: string, consent: string[] = []) =>
    json("/api/notify/phone", { number, consent }),
  notifyPhoneVerify: (code: string) =>
    json("/api/notify/phone/verify", { code }),
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
    json("/api/notify/push/unsubscribe", { endpoint }),
  notifyTest: (channel: "sms" | "push") =>
    json<{ ok: boolean; delivered?: number }>("/api/notify/test",
                                              { channel }),
  // consented support access
  supportAccess: () => req<{ granted: boolean; expires_at: string | null;
                             reason: string | null }>("/api/support-access"),
  // a durable grant — elevation-gated
  supportAccessGrant: (hours: number, reason: string) =>
    json("/api/support-access", { hours, reason }),
  supportAccessRevoke: () => json("/api/support-access/revoke", {}),
  // change the login email/username — elevation-gated
  // (Form endpoint, like totp/disable — NOT the json() helper)
  emailChange: (new_email: string, keep_push_endpoint?: string) =>
    // `hint` is the address they probably meant when the domain
    // looks like a typo of a major provider. Advisory. On hosted the
    // address does not move yet: `email` is still the sign-in address and
    // `pending_email` the one waiting for its confirm button.
    req<{ ok: boolean; email: string; pending_email?: string | null;
          verify_sent?: boolean;
          hint?: string | null; passkeys_removed?: number;
          passkeys_kept?: number; mail_list_entry_dropped?: boolean;
          sms_number_needs_verify?: boolean;
          members_removed?: RemovedMember[] }>(
      "/api/email/change",
      form({ new_email,
             ...(keep_push_endpoint ? { keep_push_endpoint } : {}) })),
  // withdraw a pending sign-in address change (no step-up: it only
  // takes something away)
  emailChangeCancel: () =>
    json<{ ok: boolean; cancelled: boolean }>("/api/email/change/cancel", {}),
  // a household member removes their OWN login (owner + data untouched)
  accountLeave: () =>
    req<{ ok: boolean }>("/api/account/leave", form({})),
  // this login's own daily-email delivery (viewers may)
  meEmail: (muted: boolean) =>
    json<{ ok: boolean; muted: boolean }>("/api/me/email", { muted }),
  // one kind of the household's mail on or off for my own inbox
  meEmailPref: (kind: EmailKind, on: boolean) =>
    json<{ ok: boolean; prefs: Record<EmailKind, boolean> }>(
      "/api/me/email", { kind, on }),
  // full erasure — FRESH elevation (the sheet asks again even inside an
  // open window; Form endpoint)
  accountDelete: () =>
    req<{ ok: boolean }>("/api/account/delete", form({})),
  // re-send the email-verification link (hosted)
  verifyResend: () =>
    json<{ ok: boolean; verified: boolean }>("/api/verify-email/resend", {}),
  // the current password is the CHANGE's own input, not re-auth; the
  // route is elevation-gated on top. totp_code stays for a server that
  // still answers totp_required on this one form
  passwordChange: (current_password: string, new_password: string,
                   totp_code?: string, keep_push_endpoint?: string) =>
    json<{ ok: boolean; passkeys_removed?: number;
           passkeys_kept?: number; sms_number_needs_verify?: boolean;
           members_removed?: RemovedMember[] }>(
      "/api/password/change", { current_password, new_password,
      ...(totp_code ? { totp_code } : {}),
      ...(keep_push_endpoint ? { keep_push_endpoint } : {}) }),
  // receipts
  // receipts on a transaction, with the charge's own amount and date (null
  // when it is gone) — what the waiting receipts are ranked against
  receipts: (txnId: string) =>
    req<{ receipts: Receipt[];
          txn?: { amount: number; date: string } | null }>(
      `/api/transactions/${encodeURIComponent(txnId)}/receipts`),
  // line items across all receipts (search + grouping)
  receiptItems: (q = "", group = "item") =>
    req<{ rows: ReceiptItemRow[]; groups: ReceiptItemGroup[];
          truncated: boolean }>(
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
    req<{ ok: boolean }>(`/api/receipts/${id}`, { method: "DELETE" }),
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
  receiptsWaiting: (offset = 0) => req<WaitingReceipts>(
      "/api/receipts/waiting" + (offset > 0 ? `?offset=${offset}` : "")),
  receiptMatch: (id: string, txnId: string) =>
    json<WaitingReceipts>(`/api/receipts/${id}/match`, { txn_id: txnId }),
  // a receipt's total, date and (optionally) store typed in by hand; a
  // waiting one is matched at once, and txn_id says where it went
  receiptDetails: (id: string,
                   body: { total: number; date: string; merchant?: string }) =>
    json<WaitingReceipts & { id: string; txn_id: string | null }>(
      `/api/receipts/${id}/details`, body),
  receiptUnmatch: (id: string) =>
    json<WaitingReceipts & { txn_id: string }>(
      `/api/receipts/${id}/unmatch`, {}),
  // one charge, several categories: the parts a person wrote for a row
  // (stored category keys + cent-exact amounts summing to the charge). PUT
  // replaces the whole split; the server answers 400 with the reason in
  // words when the parts do not add up or one is a flow category.
  txnSplitSet: (txnId: string, parts: SplitPart[]) =>
    req<{ ok: boolean; split: SplitPart[] }>(
      `/api/transactions/${encodeURIComponent(txnId)}/split`, {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ parts }),
      }),
  txnSplitClear: (txnId: string) =>
    req<{ ok: boolean; split: null }>(
      `/api/transactions/${encodeURIComponent(txnId)}/split`,
      { method: "DELETE" }),
  receiptParse: (id: string) =>
    json<{ status: string }>(`/api/receipts/${id}/parse`, {}),
  receiptTag: (id: string, line: number, tag: string) =>
    json(`/api/receipts/${id}/items/${line}/tag`, { tag }),
  receiptReport: (tag: string, y: number, m: number) =>
    req<{ tag: string; total: number; count: number;
          rows: { date: string; payee: string; description: string;
                  qty: number | null; amount: number; receipt_id: string;
                  txn_id: string }[] }>(
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
               restore_door?: string }[] };
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
    json<{ ok: boolean; env: string }>("/api/accounts/plaid/validate",
                                       { client_id, secret, env }),
  // Hosted Link: create session → open Plaid URL only (no oikonome waiting
  // tab). SPA stays on Welcome/Accounts and polls plaidLinkStatus until done.
  // manage_accounts (update mode only) opens Link's shared-accounts
  // checklist — deselecting an account there is the only lever that stops
  // Plaid billing it; a local hide can't change what the Item shares.
  plaidLinkStart: (item_id = "", manage_accounts = false) =>
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
        }).toString(),
      }),
  plaidLinkStatus: (link_token: string) =>
    req<{ done: boolean; kind?: string; institution?: string; error?: string;
          // "pending": saved, its first pull queued on the worker
          first_sync?: "pending" | "ok" }>(
      `/accounts/plaid/link/${encodeURIComponent(link_token)}/status`),
  plaidKeys: (client_id: string, secret: string, env: string) =>
    json<{ ok: boolean; env: string }>("/api/accounts/plaid/keys",
                                       { client_id, secret, env }),
  mxKeys: (client_id: string, api_key: string, env: string) =>
    json<{ ok: boolean; env: string }>("/api/accounts/mx/keys",
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
    json<{ ok: boolean; token: string; url: string; expires_in: number }>(
      "/api/export/token", { kind }),
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
      if (!r.ok) return null;
      const j = await r.json() as { at?: string | null; warnings?: unknown };
      return { at: j.at ?? null, warnings: Array.isArray(j.warnings)
        ? j.warnings.map(String).filter((t) => t.trim()) : [] };
    } catch { return null; }
  },
  // the continuity packet: the household's money on paper for whoever is
  // left to run it. Two fields the owner writes, plus what the email door
  // will accept; the PDF itself rides the export ticket (kind
  // "continuity") and the emailed copy steps up like the download.
  continuity: () => req<Continuity>("/api/continuity"),
  continuitySave: (body: { note?: string; prepared_for?: string }) =>
    req<Continuity>("/api/continuity", {
      method: "PUT", headers: { "content-type": "application/json" },
      body: JSON.stringify(body) }),
  continuityEmail: (to: string) =>
    json<{ ok: boolean; to: string }>("/api/continuity/email", { to }),
  connectionsImport: async (file: File, passphrase: string) => {
    const fd = new FormData();
    fd.set("passphrase", passphrase);
    fd.set("file", file);
    const r = await elevatedFetch("/api/connections/import", {
      method: "POST", credentials: "same-origin", body: fd,
    });
    if (!r.ok) await settle<never>(r);
    return r.json() as Promise<{ ok: boolean; config: string[];
                                items: number }>;
  },
  mxConnect: () => json<{ url: string }>("/api/accounts/mx/connect", {}),
  plaidKeysClear: () =>
    req<{ ok: boolean }>("/api/accounts/plaid/keys", { method: "DELETE" }),
  mxKeysClear: () =>
    req<{ ok: boolean }>("/api/accounts/mx/keys", { method: "DELETE" }),
  mxSyncNow: () =>
    json<{ ok: boolean; transactions: number }>("/api/accounts/mx/sync", {}),
  // multi-source account links
  accountLinks: () =>
    req<{ groups: LinkGroup[]; suggestions: LinkSuggestion[];
          card_dismissed: boolean }>("/api/accounts/links"),
  accountLinkCreate: (account_ids: string[]) =>
    json<{ group_id: string }>("/api/accounts/links",
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
    json<{ challenge_id: string; options: unknown }>(
      "/api/passkeys/options", {}),
  // adding/removing a factor is elevation-gated — a stolen session must
  // not be able to enroll its own key or strip the owner's
  passkeyRegister: (challenge_id: string, credential: unknown,
                    label: string) =>
    // recovery_codes comes back only when this passkey is the account's
    // FIRST strong factor (server issues one-time codes then) — the UI shows
    // them once for lockout protection
    json<{ id: string; recovery_codes?: string[];
          sms_number_needs_verify?: boolean;
          members_removed?: RemovedMember[] }>("/api/passkeys",
                         { challenge_id, credential, label }),
  passkeys: () =>
    req<{ passkeys: { id: string; label: string; transports: string;
                      created_at: string; last_used: string | null }[] }>(
      "/api/passkeys"),
  passkeyDelete: (id: string) =>
    req<{ ok: boolean; signed_out?: boolean }>(`/api/passkeys/${id}`, {
      method: "DELETE",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({}),
    }),
  // inline WebAuthn re-assertion on a passkey account — verify mints a
  // 5-minute single-use ticket that /api/auth/elevate redeems (stepup.ts)
  stepupPasskeyOptions: () =>
    json<{ challenge_id: string; options: unknown }>(
      "/api/stepup/passkey/options", {}),
  stepupPasskey: (challenge_id: string, credential: unknown) =>
    json<{ stepup_ticket: string }>(
      "/api/stepup/passkey", { challenge_id, credential }),
  // email optional — omit for discoverable/usernameless passkey login
  passkeyLoginOptions: (email?: string) =>
    json<{ challenge_id: string; options: unknown }>(
      "/api/login/passkey/options", email ? { email } : {}),
  passkeyLogin: (challenge_id: string, credential: unknown) =>
    json("/api/login/passkey", { challenge_id, credential }),
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
    json<{ url: string | null; role: string; label: string;
           expires_at: string; emailed: boolean }>(
      "/api/invites", { label, role }),
  invites: () => req<{ invites: Invite[] }>("/api/invites"),
  inviteRevoke: (tokenHash: string) =>
    req<{ ok: boolean }>(`/api/invites/${tokenHash}`, { method: "DELETE" }),
  invitePeek: (token: string) =>
    req<{ role: string; label: string; email?: string }>(
      `/api/invite/peek?token=${encodeURIComponent(token)}`),
  inviteClaim: (token: string, email: string, password: string) =>
    json("/api/invite/claim", { token, email, password }),
  members: () => req<{ users: Member[]; assignable_roles: string[] }>(
    "/api/users"),
  // who changed which category, bill, note… and when — newest first;
  // `before` pages by the last row's timestamp, kind/who/target filter
  activity: (params: Record<string, string> = {}) =>
    req<ActivityPage>("/api/activity"
      + (Object.keys(params).length ? "?" + new URLSearchParams(params) : "")),
  // moving somebody between member and view-only. Elevation-gated like
  // the other durable grants — the same proof removing them asks for.
  memberSetRole: (id: string, role: string) =>
    json<{ ok: boolean; role: string }>(`/api/users/${id}/role`, { role }),
  // removing a member is elevation-gated
  memberRemove: (id: string) =>
    req<{ ok: boolean }>(`/api/users/${id}`,
      { method: "DELETE", body: JSON.stringify({}),
        headers: { "content-type": "application/json" } }),
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
  doctor: () => req<{ ok: boolean; checks: DoctorCheck[] }>("/api/doctor"),
  // shipped docs + FAQ as help topics
  help: () => req<{ topics: { id: string; title: string;
                              body: string }[] }>("/api/help"),
  doctorScripts: () =>
    req<{ scripts: ScriptHeartbeat[] }>("/api/doctor/scripts"),
  doctorScriptUpdate: (source: string,
                       body: { expected_hours?: number; alerts?: boolean }) =>
    json(`/api/doctor/scripts/${encodeURIComponent(source)}`, body),
  // kicking another session is elevation-gated, as on deviceRevoke.
  // keep_push_endpoint: this browser's own web-push endpoint, so "sign out
  // everywhere else" drops the OTHER browsers' subscriptions and not ours
  sessionRevoke: (body: { all_others?: boolean; id?: string;
                          keep_push_endpoint?: string }) =>
    json<{ ok: boolean; revoked: number }>("/api/sessions/revoke", body),
  settingsSave: (body: Record<string, unknown>) =>
    json<Settings>("/api/settings", body),
  // send this recipient's invitation again (rotates the link, so
  // the previous one stops working)
  // the owner confirms the paused relay / recipients / AI endpoint are theirs
  outboundResume: (version?: string) =>
    json<{ ok: boolean; resumed: boolean }>("/api/settings/outbound/resume",
                                            { version }),
  recipientInviteResend: (email: string) =>
    // `sent: false` (with `reason`) when the server held the mail back;
    // an older server leaves `sent` out and always sent
    json<{ ok: boolean; sent?: boolean; reason?: string;
           email_recipient_status: Settings["email_recipient_status"] }>(
      "/api/settings/recipients/resend", { email }),
  smtpTest: () =>
    json<{ ok: boolean; sent_to: string[];
           // on the list but deliberately not mailed, and why
           skipped?: { email: string; reason: string }[];
           failed: { email: string; error: string }[]; via: string }>(
      "/api/settings/smtp-test", {}),
  budgetsSuggest: () => req<BudgetSuggestions>("/api/budgets/suggest"),
  // maintenance jobs (Settings card): run the worker task bodies now
  jobsSync: () =>
    json<{ ok: boolean; started?: boolean; reason?: string;
           results: Record<string, string> }>("/api/jobs/sync", {}),
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
    json<{ ok: boolean; started: boolean; reason?: string }>(
      "/api/jobs/sync/start", {}),
  jobsSyncStatus: () => req<SyncStatus>("/api/jobs/sync/status"),
  // wizard step-2 gate: per-Plaid-item historical-backfill readiness.
  // Read-only (viewers may poll it); the nudge that starts a background
  // pull when one has gone quiet is the owner-only POST below.
  onboardingBackfill: () =>
    req<BackfillStatus>("/api/onboarding/backfill"),
  onboardingBackfillNudge: () =>
    json<BackfillStatus>("/api/onboarding/backfill/nudge", {}),
  historyCoverage: () => req<HistoryCoverage>("/api/history/coverage"),
  jobsEmail: () =>
    json<{ ok: boolean; subject: string }>("/api/jobs/email", {}),
  alertDismiss: (kind: string, message: string) =>
    json("/api/alerts/dismiss", { kind, message }),
  alertRestore: (kind: string, message: string) =>
    json("/api/alerts/restore", { kind, message }),
  alertsHistory: () => req<{ alerts: AlertRow[] }>("/api/alerts/history"),
  // TOTP endpoints are form-encoded (they predate the JSON surface)
  // the FIRST enrol is elevation-gated; a re-enrol proves the current
  // authenticator instead (stronger), so the code is sent when there is one
  totpEnroll: (current_code?: string) =>
    req<{ secret: string; qr: string }>("/api/totp/enroll",
      form(current_code ? { current_code } : {})),
  totpConfirm: (secret: string, code: string, current_code?: string) =>
    // first-factor enrolment returns one-time recovery codes
    req<{ ok: boolean; recovery_codes: string[] | null;
          sms_number_needs_verify?: boolean;
          members_removed?: RemovedMember[] }>(
      "/api/totp/confirm",
      form({ secret, code, ...(current_code ? { current_code } : {}) })),
  // mint a fresh recovery-code batch — elevation-gated (the live code a
  // TOTP account owes was collected at elevate time)
  // stop a factor-clearing recovery on this account (no step-up: cancelling
  // is the safe direction)
  cancelPendingRecovery: () =>
    json<{ ok: boolean; cancelled: number }>(
      "/api/recovery/cancel-pending", {}),
  recoveryRegenerate: () =>
    req<{ ok: boolean; recovery_codes: string[] }>(
      "/api/totp/recovery-regenerate", form({})),
  // turning 2FA off is a security downgrade — elevation-gated AND the
  // route's own live code (code alone let a stolen session drop 2FA)
  // The code is single-use, so this door is never resent after the sheet
  totpDisable: (code: string) =>
    reqOneTimeCode<{ ok: boolean; sms_number_needs_verify?: boolean;
                     members_removed?: RemovedMember[] }>(
      "/api/totp/disable", form({ code })),
  // session elevation — see the Elevation type at the top of this file
  elevation: () => req<Elevation>("/api/auth/elevation"),
  elevate: (body: { password: string; totp_code: string }
                // a lost authenticator: the recovery code stands in for the
                // second factor beside the password, as it does at login
                | { password: string; recovery_code: string }
                | { stepup_ticket: string } | { recovery_code: string }) =>
    json<Elevation>("/api/auth/elevate", body),
  setTxnNote: (id: string, note: string) =>
    json<{ ok: boolean; note: string }>(
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
    req<{ movements: EquityMovement[]; capital: Capital }>(
      `/api/business/entities/${encodeURIComponent(id)}/equity`),
  equityRecord: (id: string, body: { kind: string; amount: number;
                 date: string; form?: string; note?: string }) =>
    json<EquityMovement>(
      `/api/business/entities/${encodeURIComponent(id)}/equity`, body),
  equityDelete: (id: string, movementId: string) =>
    req<{ ok: boolean }>(
      `/api/business/entities/${encodeURIComponent(id)}/equity/${movementId}`,
      { method: "DELETE" }),
  equityReimburse: (id: string, body: { txn_id: string;
                    mode: "contribute" | "reimburse" }) =>
    json<EquityMovement>(
      `/api/business/entities/${encodeURIComponent(id)}/reimburse`, body),
  // Schedule C suggestions (suggest-only; nothing is written until a human
  // confirms). `source` says how much to trust each row: "merchant" is this
  // entity's own prior decision, "category" a weak guess, null = no basis.
  bizSuggestions: (id: string) =>
    req<{ lines: string[]; suggestions: {
      txn_id: string; date: string | null; amount: number; payee: string;
      category: string | null; suggested_line: string | null;
      source: "merchant" | "category" | null;
      suggested_bucket: string }[];
      /** TRUE total needing a decision. `suggestions` is capped, so counting
       *  it under-reports a large backlog. */
      unclassified: number }>(
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
  vendorMark: (id: string, body: { merchant: string; reportable: boolean }) =>
    json(`/api/business/entities/${encodeURIComponent(id)}/vendors`, body),
  accountAssignEntity: (accountId: string, entityId: string | null) =>
    json(`/api/accounts/${encodeURIComponent(accountId)}/entity`,
      { entity_id: entityId }),
  txnAssignEntity: (txnId: string, entityId: string | null) =>
    json(`/api/transactions/${encodeURIComponent(txnId)}/entity`,
      { entity_id: entityId }),
  reimbPending: () =>
    req<{ pending: PendingReimb[] }>("/api/reimburse/pending"),
  reimbCandidates: (id: string, q = "") =>
    req<{ anchor?: ReimbAnchor; candidates: ReimbCandidate[] }>(
      `/api/reimburse/${encodeURIComponent(id)}/candidates` +
      (q ? `?q=${encodeURIComponent(q)}` : "")),
  reimbLink: (txn_id: string, other_ids: string[], partial = false) =>
    json<{ linked: number; errors: string[]; notes?: string[] }>(
      "/api/reimburse/link", { txn_id, other_ids, partial }),
  // newest first; `page` (from 1) reaches older pairs, `q` finds one by
  // either side's merchant
  reimbPairs: (page = 1, q = "") => req<ReimbPairsPage>(
    `/api/reimburse/pairs?page=${page}`
    + (q ? `&q=${encodeURIComponent(q)}` : "")),
  reimbUnlink: (expense_id: string, reimburse_id: string) =>
    json("/api/reimburse/unlink", { expense_id, reimburse_id }),
  reimbUnflag: (id: string) =>
    json(`/api/reimburse/${encodeURIComponent(id)}/unflag`, {}),
  reimbFlag: (id: string,
              opts: { partial?: boolean; expected?: number | null } = {}) =>
    json(`/api/reimburse/${encodeURIComponent(id)}/flag`, opts),
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
    json<{ kind: string; updated: number; created: number;
           documents: number }>("/api/import/taxdoc/commit",
      { token, rows }),
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
    json<{ result: ImportResult }>("/api/import/mapped", { token, ...cols }),
  importYnabAccounts: (token: string,
                       accounts: Record<string, YnabDestination>,
                       remove_earlier: Record<string, boolean>) =>
    json<{ result: ImportResult }>("/api/import/ynab-accounts",
      { token, accounts, remove_earlier }),
  importBatches: () => req<{ batches: Batch[] }>("/api/import/batches"),
  // `forceKept`: the owner's "undo anyway" after the server asked about
  // rows only a note naming no feed account would keep
  importRollback: (batch_id: string, forceKept = false) =>
    json<{ ok: boolean; deleted: number; warning: string | null;
           // rows the bank feed relies on stay, and the note says so
           kept?: number; note?: string | null }>(
      "/api/import/rollback",
      forceKept ? { batch_id, force_kept: true } : { batch_id }),
  // `month` (YYYY-MM) reads a past month's paid/received per bill
  bills: (month?: string) =>
    req<BillsData>(month ? `/api/bills?month=${month}` : "/api/bills"),
  billsDetect: () =>
    req<{ proposed_add: number; proposed_income?: number;
          drift_applied: number }>(
      "/api/bills/detect", { method: "POST" }),
  // `adjust` (approve only) changes the name, amount, cadence or due day
  // before the bill is created; the finder's evidence stays attached
  billsProposal: (pid: string, action: "approve" | "reject",
                  adjust?: ProposalAdjust) =>
    // `note` is present when the approve changed something the person set
    // (a pinned amount cleared) and must be shown to them
    req<{ ok: boolean; payee: string; superseded?: boolean; note?: string }>("/api/bills/proposal", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(adjust && action === "approve"
        ? { pid, action, adjust } : { pid, action }),
    }),
  // every pending proposal in one click (the wizard's bills step)
  billsApproveAll: () =>
    req<{ ok: boolean; approved: number; payees: string[];
          failed: { pid: string; error: string }[];
          notes?: { payee: string; note: string }[] }>(
      "/api/bills/proposals/approve-all", { method: "POST" }),
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
  retirement: (params: Record<string, string> = {}) =>
    req<RetirementData>("/api/retirement" +
      (Object.keys(params).length ? "?" + new URLSearchParams(params) : "")),
  // debt payoff planner — method/extra are what-if overrides, unsaved
  debtPlan: (params: Record<string, string> = {}) =>
    req<DebtPlan>("/api/debt-plan" +
      (Object.keys(params).length ? "?" + new URLSearchParams(params) : "")),
  debtPlanSave: (body: DebtPlanSave) => json<DebtPlan>("/api/debt-plan", body),
  // bill CRUD (/api/bills/*)
  billsBill: (payee: string) =>
    req<BillDetail>(`/api/bills/bill?payee=${encodeURIComponent(payee)}`),
  billsSave: (body: { payee: string; amount: string | number;
                          cadence: string; next_due?: string; income?: boolean;
                          orig_payee?: string; cap?: string | number | null;
                          merchant?: string; show_today?: boolean;
                          match_category?: boolean;
                          category?: string | null;
                          txn_category?: string;
                          companions?: Companion[];
                          merchants?: string[]; restore?: boolean }) =>
    json<{ ok: boolean; payee: string; restored?: boolean }>(
      "/api/bills/save", body),
  // known merchants (canonical-resolved) for the manual-bill picker
  merchants: () => req<{ merchants: string[] }>("/api/merchants"),
  billsDelete: (payee: string, mode: "archive" | "purge" = "archive") =>
    json<{ ok: boolean; mode: string; affected: number }>(
      "/api/bills/delete", { payee, mode }),
  billsRestore: (payee: string) =>
    json<{ ok: boolean; affected: number }>("/api/bills/restore", { payee }),
  // `disabled` names the target state and is required: a bare flip is
  // decided against whatever the server holds, so a stale screen or a
  // retried request would toggle the bill the opposite way to the button
  // the person pressed
  billsToggle: (payee: string, disabled: boolean) =>
    json<{ ok: boolean; payee: string; disabled: boolean }>(
      "/api/bills/toggle", { payee, disabled }),
  billsHint: (payee: string, action: "dismiss" | "restore" = "dismiss",
              target: "fit" | "health" = "fit") =>
    json<{ ok: boolean; payee: string; dismissed: boolean }>(
      "/api/bills/hint", { payee, action, target }),
  billsHistory: (payee: string, window: string = "all") =>
    req<BillsHistoryData>(
      `/api/bills/history?payee=${encodeURIComponent(payee)}&window=${encodeURIComponent(window)}`),
  // scope "one" = this row only, no rule written (the default the server
  // also uses); "all" = also teach the merchant, which then appears on the
  // Rules page. A generic payee ("Check Paid") must never get "all" by
  // accident — one such edit would claim every row sharing that name.
  setCategory: (id: string, category: string, scope: "one" | "all" = "one") =>
    req<{ ok: boolean; scope: string; rule_written: boolean }>(
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
    req<{ merchant: string; count: number; variants?: string[];
          flow_rows?: number; skipped_override?: number }>(
      "/api/bills/merchant-category/preview"
      + (opts?.family === false ? "?canon_only=1" : ""), {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ payee, category }),
      }),
  merchantCategorySet: (payee: string, category: string) =>
    req<{ merchant: string; count: number; undo: unknown }>(
      "/api/bills/merchant-category", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ payee, category }),
      }),
  merchantCategoryUndo: (undo: unknown) =>
    req<{ ok: boolean; restored: number }>(
      "/api/bills/merchant-category/undo", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ undo }),
      }),
  // the learned-rules surface — provenance groups load
  // lazily with server-side search + pagination (~50/page)
  rules: (source: "user" | "llm" | "seed" | "model", q = "", page = 1) =>
    req<RulesPage>(`/api/rules?source=${source}&q=${encodeURIComponent(q)}` +
                   `&page=${page}`),
  ruleSet: (merchant: string, category: string) =>
    req<{ count: number }>("/api/rules/set", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ merchant, category }),
    }),
  ruleDisable: (merchant: string, disabled: boolean) =>
    req<{ ok: boolean }>("/api/rules/disable", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ merchant, disabled }),
    }),
  ruleDelete: (merchant: string) =>
    req<{ ok: boolean; deleted: number }>("/api/rules/delete", {
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
export interface AssistantTurn {
  id: string; asked_at: string; asked_by: string; question: string;
  answer: string; blocks: AnswerBlock[]; tools_used: string[];
  /** the conversation this turn belongs to; absent on an older server */
  thread_id?: string | null;
}
/** where the next older page of the log starts */
export interface AssistantCursor { before: string; before_id?: string | null }
export const assistant = {
  // local=false means answers (and the ledger figures the tools return)
  // travel to `host` — the UI must say so instead of claiming "local"
  status: () => req<{ available: boolean; local: boolean; host: string }>("/api/assistant"),
  // `answer` is the prose alone; `blocks` the ordered text + chart/table
  // pieces the page draws (components/AssistantBlocks)
  // a follow-up names the conversation it continues (`threadId`); the
  // model then sees that thread's earlier turns and nothing else. A 404
  // means that conversation no longer exists; `saved: false` or
  // `removed: true` means the answer was kept in no conversation
  ask: (question: string, threadId?: string | null) =>
    req<{ answer: string; blocks?: AnswerBlock[]; tools_used: string[];
          id?: string | null; thread_id?: string | null;
          saved?: boolean; removed?: boolean }>("/api/assistant", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify(threadId ? { question, thread_id: threadId } : { question }),
    }),
  // the household's answered questions, newest first; every member
  // reads the same log, so a chart drawn on the phone is here too
  // it pages by (asked_at, id): `next` is the cursor for the page after
  // this one, and both halves go back on the next read
  history: (cursor?: AssistantCursor | null) =>
    req<{ turns: AssistantTurn[]; more: boolean; next?: AssistantCursor | null }>(
      "/api/assistant/history"
      + (cursor ? `?before=${encodeURIComponent(cursor.before)}`
         + (cursor.before_id ? `&before_id=${encodeURIComponent(cursor.before_id)}` : "")
        : "")),
  // drops one conversation — every turn of the thread (a 404 when
  // nothing was left to drop: someone removed it first)
  forgetThread: (threadId: string) =>
    req<{ ok: boolean; deleted: number }>(
      `/api/assistant/threads/${encodeURIComponent(threadId)}`, { method: "DELETE" }),
  // drops one turn: a server that predates conversations has only this
  // door, and there a conversation's id is its one turn's id
  forgetTurn: (turnId: string) =>
    req<{ ok: boolean; deleted: number }>(
      `/api/assistant/history/${encodeURIComponent(turnId)}`, { method: "DELETE" }),
  clear: () =>
    req<{ ok: boolean; deleted: number }>("/api/assistant/history", { method: "DELETE" }),
  // one answer as a self-contained HTML page, handed to the browser as a
  // download; the server re-validates the blocks and draws the charts
  report: async (question: string, blocks: AnswerBlock[]) => {
    const r = await rawFetch("/api/assistant/report", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ question, blocks }), credentials: "same-origin",
    });
    if (!r.ok) await settle<never>(r);
    const name = /filename="?([^";]+)/.exec(r.headers.get("content-disposition") ?? "")?.[1]
      ?? "oikonome-answer.html";
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

/** Display form of a category (or any underscored enum key such as a
 *  payment kind): `FOOD_AND_DRINK` → `FOOD AND DRINK`. Custom names
 *  pass through untouched. Every surface that shows a category goes
 *  through here so the same value never reads two ways. */
export const catLabel = (c: string | null | undefined) =>
  // the server spells "no category at all" as "?" (EFF_CAT) — a reader
  // shouldn't have to know that
  !c || c === "?" ? "Uncategorized" : c.replace(/_/g, " ");

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
  if (v === null || v === undefined) return "·";
  if (cents) return `${v < 0 ? "-" : ""}$${_fmtCents.format(Math.abs(v))}`;
  const n = pyround(v);              // round the SIGNED value, as the server does
  return `${n < 0 ? "-" : ""}$${_fmtWhole.format(Math.abs(n))}`;
};

export const mmdd = (iso: string | null | undefined) => {
  if (!iso) return "";
  const [y, m, d] = iso.slice(0, 10).split("-");
  return `${m}/${d}/${y.slice(2)}`;
};
