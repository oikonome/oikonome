// Dependency-free money and merge logic. This module must import no
// React Native code: mobile has no JS test runner, so plain node
// (`node --experimental-strip-types scripts/pure-logic-tests.mjs`)
// executes the unit tests for the two behaviours here that fail
// silently — a $1 rounding drift off the web/email, and a savings
// goal lost to a concurrent edit. The server suite runs that script
// too, so `make test` goes red on a regression.
import type { AccountRemoveMode, ElevationProof, ElevationStatus,
              SavingsGoal } from "./api";

/** the screens' loose amount-field parser: "$1,234.56 " → 1234.56 */
export const numv = (v: string) =>
  Number((v || "0").replace(/[$,\s]/g, "")) || 0;

// Python-style banker's rounding on the SIGNED value — the server,
// the web and the daily email all round this way, so half-away-from-zero
// here would disagree with them by a dollar on a .5 value
export const pyround = (v: number) => {
  const f = Math.floor(v), diff = v - f;
  if (diff > 0.5) return f + 1;
  if (diff < 0.5) return f;
  return f % 2 === 0 ? f : f + 1;
};

export const money = (n: number, cents = true) => {
  if (cents)
    return (n < 0 ? "-$" : "$") + Math.abs(n).toLocaleString("en-US",
      { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const r = pyround(n);
  return (r < 0 ? "-$" : "$") + Math.abs(r).toLocaleString("en-US");
};

// the category picker's "reset to source" sentinel. The server clears
// an override on the EMPTY string — sending the literal would write an
// override actually named __clear__, so every call site maps through
// here instead of repeating the comparison.
export const CLEAR_CATEGORY = "__clear__";
export const categoryForServer = (category: string) =>
  category === CLEAR_CATEGORY ? "" : category;

// /api/connections' last_sync is a server-rendered PHRASE ("synced
// 12m ago"), never a timestamp — it must reach the screen verbatim.
// Fed to Date it renders "Invalid Date", so the passthrough lives here
// where node can pin the contract.
export const lastSyncPhrase = (v: string | null | undefined): string | null =>
  v || null;

// free-text YYYY-MM-DD fields need a real calendar check, not just a
// shape check — "2026-02-30" matches the pattern but the server (or
// worse, the books) ends up with garbage. The parts must round-trip
// through Date exactly for the date to exist.
export const isValidYmd = (v: string): boolean => {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v);
  if (!m) return false;
  const y = Number(m[1]), mo = Number(m[2]), d = Number(m[3]);
  const dt = new Date(Date.UTC(y, mo - 1, d));
  return dt.getUTCFullYear() === y && dt.getUTCMonth() === mo - 1
    && dt.getUTCDate() === d;
};

/** one Savings-goals card row as drafted on screen (strings, unsaved) */
export type GoalDraft = { name: string; target: string; target_date: string;
                          monthly_plan: string; account_id: string;
                          tokens: string; start_balance: string;
                          mode: string; touched?: boolean };

// POST /api/settings replaces savings_goals wholesale, so this merge
// is the ONLY thing keeping a second device's concurrent edit alive.
// Untouched rows defer to the server's live copy; a baseline name
// (what this card saw at mount) missing from the live list means the
// goal was deleted elsewhere and stays deleted; live goals the
// baseline never knew were added elsewhere and are carried through.
export function mergeSavingsGoals(
  rows: readonly GoalDraft[],
  live: readonly SavingsGoal[],
  baselineNames: readonly string[],
): SavingsGoal[] {
  const curBy = new Map(live.map((g) => [g.name, g] as const));
  const baseNames = new Set(baselineNames);
  const out: SavingsGoal[] = [];
  for (const r of rows) {
    const name = r.name.trim();
    if (!name) continue;
    if (!r.touched) {
      const cur = curBy.get(name);
      if (cur) { out.push(cur); continue; }
      if (baseNames.has(name)) continue;
    }
    out.push({ name, target: numv(r.target),
               target_date: r.target_date || null,
               monthly_plan: numv(r.monthly_plan),
               account_id: r.account_id || null,
               tokens: r.tokens.split(",").map((t) => t.trim())
                 .filter(Boolean),
               start_balance: numv(r.start_balance),
               mode: r.mode === "sweep" ? "sweep" : "monthly" });
  }
  for (const g of live)
    if (!baseNames.has(g.name) && !out.some((o) => o.name === g.name))
      out.push(g);
  return out;
}

/** Which hero face the Today page shows.
 *
 *  Precedence: the tap you just made, then — on a demo instance, and only
 *  for someone who can actually change it — this device's remembered
 *  choice, then the account's saved face.
 *
 *  A demo refuses every settings write (its login is shared and printed),
 *  so on one the per-device value is the only place a visitor's choice can
 *  live. Everywhere else the account IS the durable value and this device
 *  must never speak over it, which is why `demo` gates the middle term.
 *  A viewer has no choice of their own to remember and sees the
 *  household's saved face — the same contract the hidden toggle states.
 *
 *  The web mirrors this in Today.tsx; keep the two in step.
 */
export function resolveFace(o: {
  override?: "summary" | "detail" | null,
  perDevice?: "summary" | "detail" | null,
  saved?: "summary" | "detail" | null,
  demo?: boolean,
  viewer?: boolean,
}): "summary" | "detail" {
  const perDevice = o.demo && !o.viewer ? (o.perDevice ?? null) : null;
  return (o.override ?? perDevice ?? o.saved ?? "summary") === "detail"
    ? "detail" : "summary";
}

/** A day divider in the ledger, carrying that day's spend. */
export type DayHead = { kind: "day"; key: string; label: string;
                        count: number; spent: number };

/** A ledger row, as far as day grouping cares. */
type DatedRow = { id: string; date: string; amount: number;
                  category?: string | null;
                  counts_as_spend?: boolean | null };

/** What one day of the ledger cost.
 *
 *  The house rule, not a sum of the column: transfers and card payments
 *  move money between the user's own accounts, and money in is not
 *  spending. Summing raw amounts would print a figure that disagrees with
 *  the verdict, the daily email and the Business worksheet, which all
 *  already exclude exactly these.
 *
 *  The server decides which rows count (counts_as_spend, computed from the
 *  same SQL the verdict uses) — the client must not re-derive spend
 *  semantics: the display category can't see overrides collapsing to
 *  transfers or loan-account rows, so any local rule diverges. The
 *  category fallback below exists only for a NEW app against an OLD
 *  server that doesn't send the field yet — without it every day would
 *  total $0. Mirrors the web's TxnTable daySpend exactly. */
export const daySpend = (group: readonly DatedRow[]) =>
  group.reduce((sum, t) =>
    sum + ((typeof t.counts_as_spend === "boolean"
            ? t.counts_as_spend
            : t.amount > 0 && !(t.category || "").includes("TRANSFER"))
           ? t.amount : 0), 0);

/** "Thu · Aug 13" — the weekday is the reason the divider exists.
 *
 *  MM/DD never says whether an expensive day was a Saturday, and the row
 *  itself already prints the digits. Parsed field by field: the string
 *  form new Date("2026-08-13") is parsed as UTC and renders as the 12th
 *  everywhere west of Greenwich, which would mislabel every divider. */
export const dayLabel = (iso: string) => {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  if (!y || !m || !d) return iso;
  const dt = new Date(y, m - 1, d);
  return `${dt.toLocaleDateString(undefined, { weekday: "short" })} · `
    + dt.toLocaleDateString(undefined, { month: "short", day: "numeric" });
};

/** Interleave a day header before each date change in a date-ordered list.
 *
 *  Returns one flat array so the virtualized list keeps recycling cells;
 *  a header carries its own kind so the renderer can branch on it. Rows
 *  are grouped exactly as given — the caller owns the ordering, and
 *  grouping a list that is not in date order would produce a header per
 *  row, which is why the ledger's sort and this feature travel together.
 *  Mirrors the web's TxnTable dayGroups. */
export function withDayHeads<T extends DatedRow>(
  rows: readonly T[],
): (T | DayHead)[] {
  const out: (T | DayHead)[] = [];
  let start = 0;
  for (let i = 0; i <= rows.length; i++) {
    if (i < rows.length
        && rows[i].date.slice(0, 10) === rows[start].date.slice(0, 10))
      continue;
    const group = rows.slice(start, i);
    if (group.length) {
      // keyed by the day's FIRST ROW, not by the date: an unsorted list
      // can revisit a date, and two headers sharing a key is a duplicate
      // key in the list, not just a cosmetic problem
      out.push({ kind: "day", key: `day:${group[0].id}`,
                 label: dayLabel(group[0].date), count: group.length,
                 spent: daySpend(group) });
      out.push(...group);
    }
    start = i;
  }
  return out;
}

// ---- plain-http server addresses ----
// The app talks only to the server a person typed on the connect screen,
// so this is the one place a cleartext connection can be born. Android
// cannot express "cleartext for private ranges only" (its network
// security config matches hostnames, not CIDRs), so the range rule lives
// here: http:// is refused outright for anything routable from the
// internet, and allowed — with a warning the person has to read — for
// the LAN / loopback addresses a self-hosted box actually lives at.

const IPV4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/;

/** loopback, RFC1918, link-local, CGNAT and mDNS/.local hosts — the
 *  addresses a self-hosted instance on the same network answers at */
export function isPrivateHost(hostRaw: string): boolean {
  const host = hostRaw.trim().toLowerCase().replace(/^\[|\]$/g, "");
  if (!host) return false;
  if (host === "localhost" || host.endsWith(".localhost")) return true;
  if (host.endsWith(".local") || host.endsWith(".internal")
      || host.endsWith(".lan") || host.endsWith(".home.arpa")) return true;
  // IPv6 literals only — the unique-local (fc00::/7) and link-local
  // (fe80::/10) prefixes are tested on addresses, never on a hostname
  // that merely starts with those letters (fdbackup.example.org is a
  // public host and must stay "http-public")
  if (host.includes(":")) {
    if (host === "::1" || host.startsWith("fe80:")
        || /^f[cd][0-9a-f]{2}:/.test(host)) return true;
    return false;
  }
  const m = IPV4.exec(host);
  if (!m) return false;
  const [a, b] = [Number(m[1]), Number(m[2])];
  if (a === 127 || a === 10 || a === 0) return true;
  if (a === 192 && b === 168) return true;
  if (a === 172 && b >= 16 && b <= 31) return true;
  if (a === 169 && b === 254) return true;
  if (a === 100 && b >= 64 && b <= 127) return true;
  return false;
}

/** the host part of an origin, without a scheme, port, path or userinfo */
export function hostOf(url: string): string {
  const m = /^[a-z][a-z0-9+.-]*:\/\/(?:[^@/]*@)?(\[[^\]]*\]|[^:/?#]*)/i
    .exec(url.trim());
  return m ? m[1].replace(/^\[|\]$/g, "") : "";
}

export type ServerUrlVerdict = "ok" | "http-private" | "http-public";

/** Whether an origin may be connected to, and how loudly to warn:
 *  "ok" for https, "http-private" for cleartext to a LAN/loopback host
 *  (allowed, warned), "http-public" for cleartext to anything else
 *  (refused — a password and TOTP would cross the internet readable). */
export function serverUrlVerdict(url: string): ServerUrlVerdict {
  if (!/^http:\/\//i.test(url.trim())) return "ok";
  return isPrivateHost(hostOf(url)) ? "http-private" : "http-public";
}

/** Display form of a category (or any underscored enum key such as a
 *  payment kind): `FOOD_AND_DRINK` → `FOOD AND DRINK`. Custom names
 *  pass through untouched. Every surface that shows a category goes
 *  through here so the same value never reads two ways. */
export const catLabel = (c: string | null | undefined) =>
  // the server spells "no category at all" as "?" (EFF_CAT) — a reader
  // shouldn't have to know that
  !c || c === "?" ? "Uncategorized" : c.replace(/_/g, " ");

/** The category picker's option filter. The typed text is matched
 *  against each option's DISPLAY label, so "food and" finds
 *  FOOD_AND_DRINK — but the text itself must never go through catLabel:
 *  that helper spells an EMPTY value as "Uncategorized", so an empty
 *  filter box would keep only the options containing that word, i.e.
 *  none. Empty filter = every option. */
export const filterCategories = (all: string[], filter: string): string[] => {
  const q = filter.trim().toLowerCase().replace(/_/g, " ");
  if (!q) return all;
  return all.filter((c) => catLabel(c).toLowerCase().includes(q));
};

/** stable 0-359 hue from a string — same merchant, same colour (web hueOf) */
export const hueOf = (s: string): number => {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h % 360;
};

/** "Riverton Pantry" → "RP", "Northwind" → "No", "4-Corners" → "4C" (web monogram) */
export const monogram = (name: string): string => {
  const words = (name || "").replace(/[^A-Za-z0-9 ]+/g, " ").trim().split(/\s+/)
    .filter(Boolean);
  if (words.length === 0) return "?";
  if (words.length === 1) return words[0].slice(0, 2);
  return (words[0][0] + words[1][0]).toUpperCase();
};

/** The two Plaid CDNs /api/logo will proxy — the server's own allowlist
 *  (server/oikonome/web/logos.py). Anything else is a guaranteed 400. */
const LOGO_HOSTS = new Set(["plaid-merchant-logos.plaid.com",
                            "plaid-counterparty-logos.plaid.com"]);

/** A row's `logo_url` as a URL of this instance's logo cache, or null when
 *  the server would refuse it. Collector rows and restored data carry logo
 *  URLs from anywhere; asking the proxy for one is a certain 400 that still
 *  spends the shared rate limit — so decide here and draw the monogram
 *  instead. Mirrors the web's logoProxyUrl and the server's `allowed()`. */
export function logoProxyUrl(baseUrl: string,
                             logo: string | null | undefined): string | null {
  if (!logo || logo.length >= 400) return null;
  // no query and no fragment: the server matches on the exact URL string
  const m = /^https:\/\/([^/?#]+)(\/[^?#]*)$/.exec(logo);
  if (!m || !LOGO_HOSTS.has(m[1]) || !m[2].endsWith(".png")) return null;
  return `${baseUrl}/api/logo?u=${encodeURIComponent(logo)}`;
}

/** What "Sign out on this device" may actually do, given the state of
 *  the devices fetch. The confirm dialog promises the server-side token
 *  is revoked — so the local-only path must never be a silent fallback:
 *  - this device is in the list → revoke that token (then sign out);
 *  - a fetch SUCCEEDED and this device isn't listed → the token is
 *    already gone server-side, plain local sign-out keeps the promise;
 *  - the list was never fetched (fast tap, failed refetch) → ask — an
 *    explicit choice that names the live 90-day bearer left behind. */
export function signOutAction(
  fetched: boolean,
  devices: readonly { id: string; current?: boolean }[] | undefined,
): { kind: "revoke"; id: string } | { kind: "local" } | { kind: "ask" } {
  const mine = (devices ?? []).find((d) => d.current);
  if (mine) return { kind: "revoke", id: mine.id };
  return fetched ? { kind: "local" } : { kind: "ask" };
}


/** Put a step-up proof into the request the server just refused with
 *  `recovery_required`, matching the encoding the body already used.
 *
 *  Every destructive endpoint already accepts a `recovery_code`, and a
 *  passkey step-up ticket rides that same field (the server redeems
 *  either), so the retry needs no new parameter — only the same body
 *  shape back. Form bodies are edited as text rather than through
 *  URLSearchParams, whose read/write methods React Native's polyfill does
 *  not carry.
 *
 *  Returns null when a retry would be wrong or impossible: a call that
 *  already carried a code (the person typed one — spending it is their
 *  choice), or a body this cannot extend (multipart, a blob, none at
 *  all). Null means "surface the original refusal", which is what makes
 *  the card's recovery-code field appear. */
export function withRecoveryCode(
  init: RequestInit | undefined, code: string,
): RequestInit | null {
  const body = init?.body;
  if (!code || typeof body !== "string") return null;
  const h = (init?.headers ?? {}) as Record<string, string>;
  const ct = String(h["Content-Type"] ?? h["content-type"] ?? "");
  if (ct.includes("application/json")) {
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { return null; }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
      return null;
    const obj = parsed as Record<string, unknown>;
    if (obj.recovery_code) return null;
    return { ...init, body: JSON.stringify({ ...obj,
                                             recovery_code: code }) };
  }
  if (ct.includes("application/x-www-form-urlencoded")) {
    if (/(^|&)recovery_code=./.test(body)) return null;
    return { ...init, body: (body ? body + "&" : "")
      + "recovery_code=" + encodeURIComponent(code) };
  }
  return null;
}

/** Which proof the elevation sheet sends for what the person typed, or
 *  null while it is not yet answerable.
 *
 *  A recovery code stands in for ONE factor, never for the whole proof:
 *  an account that has a password sends the code BESIDE it (the
 *  {password, recovery_code} shape POST /api/auth/elevate documents), and
 *  only a passkey-only account — which has no password to send — sends
 *  the code alone. Sending a bare code on a password account is refused
 *  with password_required, so a password account with a lost
 *  authenticator must be able to reach the recovery form too, not only
 *  the passkey path.
 *
 *  Null means the Confirm button stays disabled. It is the same rule the
 *  button and the submit use, so the two cannot disagree about whether
 *  there is enough to send. */
export function elevationProof(
  form: "password" | "recovery",
  status: Pick<ElevationStatus, "methods" | "totp"> | null,
  typed: { password: string; code: string; recoveryCode: string },
): ElevationProof | null {
  const pw = typed.password;
  const totp = !!status?.totp;
  if (form === "password") {
    if (!pw || (totp && typed.code.trim().length !== 6)) return null;
    return totp ? { password: pw, totp_code: typed.code.trim() }
                : { password: pw };
  }
  const code = typed.recoveryCode.trim();
  if (!code) return null;
  if (!status?.methods.includes("password")) return { recovery_code: code };
  return pw ? { password: pw, recovery_code: code } : null;
}


/** Which provider doors the Connect hub offers, given where the app runs.
 *  Hosted never shows the SimpleFIN door: the platform runs the
 *  aggregators under its own credentials and the server refuses BYO
 *  SimpleFIN outright — the web's hosted ConnectHub shows no SimpleFIN
 *  row at all, and its onboarding copy says the same, so a hosted account
 *  is never told about a door it cannot use. Self-host keeps every
 *  door. */
export function showsProviderDoor(key: string, hosted: boolean): boolean {
  return !(hosted && key === "simplefin");
}

/** Card autopays as chart marks: the forecast series is one point per day
 *  starting today, so a due date is found by its own index. A date the
 *  series does not cover is dropped rather than clamped to an edge — a line
 *  on the last day would claim a payment lands there. Several cards due the
 *  same day collapse into one line naming them all, because two labels at
 *  the same x are a smear. Mirrors `autopayMarks` in the web's Today.tsx;
 *  the two must agree, which is why this one is tested. */
export function autopayMarks(series: [string, number][],
                             autopay: [string, number, string][] | undefined) {
  const at = new Map<number, string[]>();
  for (const [date, , name] of autopay ?? []) {
    const i = series.findIndex(([d]) => d === date);
    if (i < 0) continue;
    at.set(i, [...(at.get(i) ?? []), name]);
  }
  return [...at.entries()].map(([i, names]) => ({
    i, label: names.length > 2 ? `${names.length} autopays`
      : names.join(" + ") + " autopay",
  }));
}

/** A SecureStore key scoped to a value that may contain anything.
 *
 *  SecureStore accepts only alphanumerics, '.', '-' and '_' in a key, and
 *  REJECTS anything else — so a key built by interpolating a server URL
 *  ("…leftWizard.https://oikonome.example.com") throws on every read and every
 *  write. Callers that swallow storage errors then see a flag that never
 *  persists and never reads back, which is invisible until some feature
 *  quietly stops working. Every scoped key goes through here. */
export const secureKey = (base: string, scope?: string) =>
  scope ? `${base}.${scope.replace(/[^A-Za-z0-9._-]/g, "_")}` : base;

// ---- the plan's institution allowance ----
// One predicate for every door that adds a bank, so the Accounts card and
// both connect hubs agree on whether another institution fits. A door left
// live past the cap offers a connection the server then refuses — and on
// the hosted link path that refusal can land AFTER the bank has been
// authorized, so the household would watch a connection it just made
// disappear. Nothing here knows what the cap IS: it arrives on /api/me
// (an operator can raise one household's), and self-host reports no cap
// at all — which is genuinely uncapped, so an unknown cap must never
// close a door. Mirrors webapp ConnectCard's `full`.
export function institutionAllowance(cap?: number | null, used?: number):
    { known: boolean; full: boolean } {
  const known = typeof cap === "number" && typeof used === "number";
  return { known, full: known && used! >= cap! };
}

// ---- net-worth trend timeframe toggles (mirrors webapp NetWorth.tsx) ----
// The window is anchored to the LAST point's date (not today) so a
// briefly-stale report still selects a full window. `start` indexes the
// baseline point — the last point at or before the cutoff — so the drawn
// line begins at the value the gain is measured from.
export const TREND_RANGES =
  ["3m", "6m", "1y", "3y", "5y", "all"] as const;
// Cash Flow adds two short windows in front: "This month" (the household's
// month so far) and "1m" (the last complete month) — cash-flow surfaces
// only (overview, spending, income). Web twin: CASHFLOW_RANGES.
export const CASHFLOW_RANGES = ["cur", "1m", ...TREND_RANGES] as const;
export type TrendRange = (typeof CASHFLOW_RANGES)[number];
export const RANGE_LABEL: Record<TrendRange, string> =
  { cur: "This month", "1m": "1m", "3m": "3m", "6m": "6m", "1y": "1y",
    "3y": "3y", "5y": "5y", all: "All" };
// the site-wide vocabulary in months (null = all); the web twin is
// webapp/src/components/RangePicker.tsx — keep them identical. "1m"
// counts two so a window cut on the current month reaches the last
// complete one (the cash graph's history stops before the current month).
export const RANGE_MONTHS: Record<TrendRange, number | null> =
  { cur: 1, "1m": 2, "3m": 3, "6m": 6, "1y": 12, "3y": 36, "5y": 60, all: null };

/** The ledger's quick date ranges — the same six windows instead of a
 *  vocabulary of its own. A range is a from-date on the first of the
 *  window's opening month with an open end: "3m" in September is Jul 1
 *  onward, the same three calendar months Cash Flow's 3m covers, so the
 *  two pages agree on what a window holds. "all" is no date filter at
 *  all. A calendar date is device-LOCAL (the `ymd` rule in dates.ts: never
 *  through UTC), and `today` is passed in so the boundaries are testable.
 *  The web twin is `ledgerRangeFrom` in
 *  webapp/src/components/RangePicker.tsx — keep them identical. */
export function ledgerRange(range: TrendRange, today: Date):
    { from: string; to: string } {
  const months = RANGE_MONTHS[range];
  if (months === null) return { from: "", to: "" };
  // Date's own month arithmetic on the 1st: no day-of-month to overflow
  const d = new Date(today.getFullYear(), today.getMonth() - (months - 1), 1);
  return { from: `${d.getFullYear()}-${
             String(d.getMonth() + 1).padStart(2, "0")}-01`, to: "" };
}

// ---- counting months backwards, safely ----
// Date#setUTCMonth keeps the day-of-month, so shifting a 31st back into a
// 30-day month rolls FORWARD into the next one (Mar 31 − 1 month = Mar 3).
// Every window built that way silently loses its oldest month on the last
// days of a long month. Both helpers below anchor or clamp the day first,
// which is what the server does (engine/reporting.py `_range_window`).
// The web twins are in webapp/src/components/RangePicker.tsx — keep them
// identical.

/** The first month a range's window includes, as "YYYY-MM", counted back
 *  from `today`. null for "all", which has no cutoff. */
export function rangeCutoffMonth(range: TrendRange, today: Date = new Date()):
    string | null {
  const months = RANGE_MONTHS[range];
  if (months === null) return null;
  return monthsBackFrom(
    // anchored to the 1st, so the day-of-month cannot overflow
    new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), 1))
      .toISOString().slice(0, 7),
    months);
}

/** The first month of a `months`-long window whose LAST month is `anchor`
 *  ("YYYY-MM"). A 3-month window ending 2026-08 starts 2026-06. */
export function monthsBackFrom(anchor: string, months: number): string {
  const d = new Date(Date.UTC(Number(anchor.slice(0, 4)),
                              Number(anchor.slice(5, 7)) - 1, 1));
  d.setUTCMonth(d.getUTCMonth() - (months - 1));
  return d.toISOString().slice(0, 7);
}

// ---- whose "now" a window is cut on ----
// The device's clock is not the household's. Every figure on the cash-flow
// screen is windowed server-side from the household's own date
// (localtime.now_local → reporting._range_window), and a phone in a
// different zone reads a different month for hours at each month boundary:
// a household in Los Angeles at 6pm on Aug 31 is still in August, while the
// device's UTC month is already September. Cutting the chart on the device
// would drop June from a "3m" window whose server figures cover
// Jun+Jul+Aug, computing the caption's best and worst month over a window
// one month shorter than the one being read.
//
// The report itself names the household's month: the cash graph's history
// stops BEFORE the current month and the forecast tail opens ON it, so the
// first forecast point is the household's today, straight from the server.

/** The household's current month ("YYYY-MM") as the cash graph names it —
 *  its first forecast point. null when the report carries no forecast
 *  tail, which is the only case with nothing in it to anchor on. */
export function graphCurrentMonth(
  graph: { points: [string, number][]; forecast_from: number },
): string | null {
  return graph.points[graph.forecast_from]?.[0] ?? null;
}

/** The first month a cash-flow range covers, anchored on the household's
 *  month rather than the device's. `today` is only the fallback for a
 *  report with no forecast tail to read the household's month from. */
export function cashflowCutoffMonth(
  graph: { points: [string, number][]; forecast_from: number },
  range: TrendRange, today: Date = new Date(),
): string | null {
  const months = RANGE_MONTHS[range];
  if (months === null) return null;
  const cur = graphCurrentMonth(graph);
  return cur ? monthsBackFrom(cur, months) : rangeCutoffMonth(range, today);
}

// ---- what a removal moved ----
// Removing an account can free an institution slot, and the allowance
// ("6 of 6 connected") rides /api/me, not the accounts list. The screen
// that offers the removal is usually the screen showing the allowance, so
// ["me"] must be refreshed too, or a household that disconnects a bank to
// make room finds the Connect button still disabled behind a cached count.
// A disconnect archives its item and a purge deletes what is left of one,
// so both change the figure; hiding an account changes nothing the server
// counts.

/** The query keys a completed account removal can have moved. The web's
 *  twin is `accountRemoveMoved` in webapp/src/components/RemoveAccountPanel
 *  — the same rule over that client's key names (its ledger is ["txns"],
 *  and its Accounts page never mounts it). */
export function accountRemoveMoved(mode: AccountRemoveMode): string[] {
  const keys = ["accounts", "today", "transactions"];
  if (mode !== "hide") keys.push("connections", "me");
  // a purge can take the checking anchor or an excluded account with it,
  // and the bill calendar's balances
  if (mode.endsWith("purge")) keys.push("settings", "calendar");
  return keys;
}

/** `iso` moved `months` months earlier, in the shape it came in: a
 *  "YYYY-MM-DD" keeps its day, clamped to the target month's length (May 31
 *  − 3 months is Feb 28, not Mar 3); a "YYYY-MM" month key — the net-worth
 *  trend's points — stays a month key. A month key has no day to read, and
 *  treating the missing day as a number would make an invalid Date whose
 *  toISOString() throws. The web twin is in
 *  webapp/src/components/RangePicker.tsx. */
export function shiftMonthsUtc(iso: string, months: number): string {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  if (!Number.isFinite(d)) {
    return new Date(Date.UTC(y, m - 1 - months, 1)).toISOString().slice(0, 7);
  }
  // day 0 of the month AFTER the target = the target's last day
  const last = new Date(Date.UTC(y, m - months, 0)).getUTCDate();
  return new Date(Date.UTC(y, m - 1 - months, Math.min(d, last)))
    .toISOString().slice(0, 10);
}

export function trendWindow(
  points: [string, number][], range: TrendRange,
): { start: number; gain: number | null; pct: number | null; full: boolean } {
  const n = points.length;
  if (n < 2) return { start: 0, gain: null, pct: null, full: true };
  let start = 0;
  if (range !== "all") {
    const months = RANGE_MONTHS[range]!;
    // clamped, not rolled: a series whose last point is a 31st would
    // otherwise land the cutoff in the month AFTER the one asked for and
    // measure the gain over a window short by a month
    const cutoff = shiftMonthsUtc(points[n - 1][0], months);
    // last point at or before the cutoff; none = the data is shorter
    // than the range and the window IS the whole series
    for (let i = n - 1; i >= 0; i--) {
      if (points[i][0] <= cutoff) { start = i; break; }
    }
  }
  const base = points[start][1];
  const gain = points[n - 1][1] - base;
  return { start, gain,
           pct: base > 0 ? (100 * gain) / base : null,
           full: start === 0 };
}

// Where the business wizard reopens, given the step marks the server holds.
//
// A mark has to mean "this step's answers reached the server", not merely
// that Continue was pressed. The two question steps ("what" collects the
// name and structure, "when" the state, EIN and dates) keep their answers
// in component state until the entity is created at the end of the SECOND
// one, so both are marked there, together. A run interrupted between them,
// or a mark on the first step alone, would otherwise reopen on "when" with
// the name blank and the field that
// collects it a step behind. So an interrupted pair reopens at its first
// step. Steps at or past the accounts step are only reachable once the
// entity exists, and the caller pins those itself.
//
// Returns -1 when every step is finished — there is nothing to reopen at,
// and the caller leaves the wizard where it already is.
//
// Mirrors the same rule in the web's BusinessWizard.tsx.
export function bizWizardResumeStep(
  steps: readonly { key: string }[],
  marks: Record<string, string>,
): number {
  const open = steps.findIndex((s) => s.key !== "done" && !marks[s.key]);
  return open === 1 ? 0 : open;
}

// ---- what a household-invite mint actually did ----
//
// The mint door answers two different ways. Self-host hands the claim link
// back to the owner to share however they like — one household, no shared
// address namespace, and frequently no SMTP to deliver through. Hosted
// mails the link to the address on the invite and WITHHOLDS it from the
// response: holding a claimable link is the only mailbox proof the
// pre-auth claim door ever gets, so the minter must not be handed one.
//
// Which of the two happened is read off the response, never off a
// build-time hosted flag — a client that guessed would tell an owner their
// invite was mailed on an instance that handed back a link. Rendering the
// URL alone is worse still: on hosted it is null, so a successful mint
// drew nothing at all and the owner had no way to tell the invitation from
// a dead button.
export type InviteOutcome =
  | { kind: "link"; url: string; emailed: boolean; label: string }
  | { kind: "emailed"; label: string };

export function inviteOutcome(
  minted: { url?: string | null; label?: string | null;
            emailed?: boolean | null },
): InviteOutcome {
  const label = (minted.label ?? "").trim();
  // No URL means the server kept it, which it only ever does after the
  // mail is away — an invite it could not send is destroyed and the mint
  // fails, so there is no silent third case to render.
  if (!minted.url) return { kind: "emailed", label };
  return { kind: "link", url: minted.url, emailed: !!minted.emailed, label };
}

/** The feedback form's bug kind folds three prompts into one message with
 *  labelled sections — the server stores and mails a single text either
 *  way. Empty sections are left out so a two-line report is not padded
 *  with blank headings. Mirrors the web page's composer word for word. */
export function composeBugReport(happened: string, expected: string,
                                 where: string): string {
  const parts: string[] = [];
  if (happened.trim()) parts.push(`What happened:\n${happened.trim()}`);
  if (expected.trim()) parts.push(`What I expected:\n${expected.trim()}`);
  if (where.trim()) parts.push(`Where:\n${where.trim()}`);
  return parts.join("\n\n");
}


// ---- the daily verdict's delivery, read as one choice ----------------------
// The schedule entry carries two flags: `on` (email — the back-compat name)
// and `push`. The wizard and Settings present them as one choice.
export type Delivery = "email" | "push" | "both" | "off";
export function deliveryOf(on: boolean, push: boolean): Delivery {
  return on && push ? "both" : on ? "email" : push ? "push" : "off";
}
export function flagsOf(d: Delivery): { on: boolean; push: boolean } {
  return { on: d === "email" || d === "both", push: d === "push" || d === "both" };
}

// ---- Merchants list: the web page's orders and filters, cut client-side ---
// The server orders by count or name; spend and recency are sorted from the
// loaded page here, and so are the filters (webapp/src/pages/Merchants.tsx).
export const MERCHANT_ORDERS = [
  ["count", "most frequent"], ["alpha", "a → z"],
  ["spend", "biggest spend"], ["recent", "recently seen"],
] as const;
export type MerchantOrder = (typeof MERCHANT_ORDERS)[number][0];
export const serverOrder = (o: MerchantOrder): "count" | "alpha" =>
  o === "alpha" ? "alpha" : "count";
export const MERCHANT_FILTERS = [
  ["all", "all"], ["oneoff", "one-off"], ["unnamed", "unnamed"], ["month", "seen this month"],
] as const;
export type MerchantFilter = (typeof MERCHANT_FILTERS)[number][0];
// a name the aggregator never resolved: a digits run, a terminal's * or #,
// or an all-caps string no one would call a name
export const looksUnnamed = (name: string) =>
  /\d{3,}|[*#]/.test(name) || (/[A-Z]{4,}/.test(name) && name === name.toUpperCase());
export function sortMerchants<T extends { display: string; rows: number; total: number;
                                          last?: string | null }>(
    rows: T[], order: MerchantOrder, flt: MerchantFilter, today: Date): T[] {
  let r = rows;
  if (flt === "oneoff") r = r.filter((m) => m.rows === 1);
  else if (flt === "unnamed") r = r.filter((m) => looksUnnamed(m.display));
  else if (flt === "month") {
    const m0 = today.toISOString().slice(0, 7);
    r = r.filter((m) => (m.last ?? "").slice(0, 7) >= m0);
  }
  if (order === "alpha") r = [...r].sort((a, b) => a.display.localeCompare(b.display));
  else if (order === "spend") r = [...r].sort((a, b) => b.total - a.total);
  else if (order === "recent")
    r = [...r].sort((a, b) => (b.last ?? "").localeCompare(a.last ?? ""));
  return r;
}
