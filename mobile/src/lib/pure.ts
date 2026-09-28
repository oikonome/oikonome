// Dependency-free money and merge logic. This module must import no
// React Native code: mobile has no JS test runner, so plain node
// (`node --experimental-strip-types scripts/pure-logic-tests.mjs`)
// executes the unit tests for the two behaviours here that fail
// silently — a $1 rounding drift off the web/email, and a savings
// goal lost to a concurrent edit. The server suite runs that script
// too, so `make test` goes red on a regression.
import type { AccountRemoveMode, CustomBucket, ElevationProof,
              ElevationStatus, SavingsGoal } from "./api";

/** the screens' loose amount-field parser: "$1,234.56 " → 1234.56 */
export const numv = (v: string) =>
  Number((v || "0").replace(/[$,\s]/g, "")) || 0;

/** A typed amount as a what-if query value. The GET doors type their
 *  amounts as numbers, so "1,000" from a phone keyboard is refused
 *  outright where the save door (which strips "$" and ",") would take
 *  it; parsing here first makes the what-if read the same amount the
 *  save would store. `whole` rounds for a door that takes whole dollars. */
export const moneyParam = (v: string, whole = false) => {
  const n = numv(v);
  return String(whole ? pyround(n) : n);
};

/** A what-if form as query params: a blank box means "the server's
 *  default" and stays off the query, the money boxes go through
 *  moneyParam, and anything else rides as typed (trimmed). */
export function whatIfQuery(fields: Record<string, string>,
                            moneyKeys: readonly string[], whole = false):
    Record<string, string> {
  return Object.fromEntries(Object.entries(fields)
    .filter(([, v]) => v.trim() !== "")
    .map(([k, v]) => [k, moneyKeys.includes(k)
      ? moneyParam(v, whole) : v.trim()]));
}

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

// Does this write hand the row BACK to the automatic layers? The empty
// category is how the server spells "drop the override", and what the row
// then reads as — the bank's own label, a bill's stamp, a merchant rule —
// is decided there and cannot be guessed here. So a reset is the one
// category write with NO local echo: a caller that shows what it sent
// shows the empty string, which catLabel renders as "Uncategorized" —
// the exact opposite of what "use the automatic category" promises. The
// row is read back from the server instead.
export const isCategoryReset = (categoryForTheServer: string) =>
  categoryForTheServer === "";

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
 *  so does a hosted passkey-only account (methods "recovery+password"):
 *  there the code stands in for the passkey and the password is still
 *  owed. Only an older server's bare ["passkey"] takes the code alone. Sending a bare code on a password account is refused
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
  if (!recoveryNeedsPassword(status)) return { recovery_code: code };
  return pw ? { password: pw, recovery_code: code } : null;
}

/** Whether the recovery form has to collect the password too: on any
 *  account that signs in with one ("password"), and on a hosted
 *  passkey-only account, whose server says "recovery+password" because a
 *  recovery code alone must not stand in for the passkey there. Only an
 *  older server's bare ["passkey"] takes the code alone. */
export function recoveryNeedsPassword(
  status: Pick<ElevationStatus, "methods"> | null,
): boolean {
  const m = status?.methods ?? [];
  return m.includes("password") || m.includes("recovery+password");
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
// line begins at the value the gain is measured from; `end` is the last
// point drawn — the newest for every window that runs to now, the
// previous month's point for "1m", the last complete month. The series is
// one point per month, so the two short windows are two points each.
// The site-wide timeframe vocabulary: every over-time toggle offers the
// same eight windows — This month · 1m · 3m · 6m · 1y · 3y · 5y · All.
// "This month" is the household's month so far; "1m" is the last COMPLETE
// month, the one window that closes before today. Web twin: RANGES in
// webapp/src/components/RangePicker.tsx — keep them identical.
export const TREND_RANGES =
  ["cur", "1m", "3m", "6m", "1y", "3y", "5y", "all"] as const;
export type TrendRange = (typeof TREND_RANGES)[number];
export const RANGE_LABEL: Record<TrendRange, string> =
  { cur: "This month", "1m": "1m", "3m": "3m", "6m": "6m", "1y": "1y",
    "3y": "3y", "5y": "5y", all: "All" };
/** How a caption names the window a figure was measured over ("+$120
 *  over this month"). Web twin: `rangeCaption` in RangePicker.tsx. */
export function rangeCaption(range: TrendRange): string {
  return range === "all" ? "all time" : range === "cur" ? "this month"
    : range === "1m" ? "last month" : range;
}
// the site-wide vocabulary in months (null = all); the web twin is
// webapp/src/components/RangePicker.tsx — keep them identical. "1m"
// counts two so a window cut on the current month reaches the last
// complete one (the cash graph's history stops before the current month).
export const RANGE_MONTHS: Record<TrendRange, number | null> =
  { cur: 1, "1m": 2, "3m": 3, "6m": 6, "1y": 12, "3y": 36, "5y": 60, all: null };

/** The ledger's quick date ranges — the same eight windows instead of a
 *  vocabulary of its own. A range is a from-date on the first of the
 *  window's opening month with an open end: "3m" in September is Jul 1
 *  onward, the same three calendar months Cash Flow's 3m covers, so the
 *  two pages agree on what a window holds; "This month" is from the 1st;
 *  "1m" is the last complete month CLOSED on its last day (in September:
 *  Aug 1 – Aug 31). "all" is no date filter at all. A calendar date is
 *  device-LOCAL (the `ymd` rule in dates.ts: never through UTC), and
 *  `today` is passed in so the boundaries are testable. The web twin is
 *  `ledgerRange` in webapp/src/components/RangePicker.tsx — keep them
 *  identical. */
export function ledgerRange(range: TrendRange, today: Date):
    { from: string; to: string } {
  const months = RANGE_MONTHS[range];
  if (months === null) return { from: "", to: "" };
  const ymd = (d: Date) => `${d.getFullYear()}-${
    String(d.getMonth() + 1).padStart(2, "0")}-${
    String(d.getDate()).padStart(2, "0")}`;
  if (range === "1m") {
    // day 0 of this month = the last day of the month before
    return { from: ymd(new Date(today.getFullYear(), today.getMonth() - 1, 1)),
             to: ymd(new Date(today.getFullYear(), today.getMonth(), 0)) };
  }
  // Date's own month arithmetic on the 1st: no day-of-month to overflow
  const d = new Date(today.getFullYear(), today.getMonth() - (months - 1), 1);
  return { from: ymd(d), to: "" };
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
): { start: number; end: number; gain: number | null; pct: number | null;
     full: boolean } {
  const n = points.length;
  if (n < 2) return { start: 0, end: n - 1, gain: null, pct: null, full: true };
  // last point at or before an ISO cutoff; -1 when the data starts later
  const at = (cutoff: string) => {
    for (let i = n - 1; i >= 0; i--) if (points[i][0] <= cutoff) return i;
    return -1;
  };
  let start = 0, end = n - 1;
  if (range !== "all") {
    const months = RANGE_MONTHS[range]!;
    // clamped, not rolled: a series whose last point is a 31st would
    // otherwise land the cutoff in the month AFTER the one asked for and
    // measure the gain over a window short by a month
    // none = the data is shorter than the range and the window IS the
    // whole series
    start = Math.max(0, at(shiftMonthsUtc(points[n - 1][0], months)));
    if (range === "1m") {
      // the last COMPLETE month closes at the point before the current
      // one; a series too short to close it falls back to the open window
      const close = at(shiftMonthsUtc(points[n - 1][0], 1));
      if (close > start) end = close;
    }
  }
  const base = points[start][1];
  const gain = points[end][1] - base;
  return { start, end, gain,
           pct: base > 0 ? (100 * gain) / base : null,
           full: start === 0 };
}

// ---- the business wizard and the Business page's own arithmetic ----
//
// Copied verbatim from the web's webapp/src/bizmath.ts; the pure-logic
// suite runs both copies over the same cases. (countsInTotals, the
// third rule there, lives in ./api beside the Account type.)

/** Which existing business a guided-setup run continues.
 *
 *  The caller's choice when it names one that is still active; otherwise
 *  the oldest active business, so a bare "Business wizard" never offers to
 *  create a second one next to a business that exists. An archived business
 *  is closed and read-only, so it is never continued — with nothing active
 *  the run starts fresh. */
export function bizSetupEntity<E extends { id: string; status?: string | null }>(
  entities: readonly E[], wanted?: string | null,
): E | undefined {
  const active = entities.filter((e) => e.status === "active");
  return (wanted ? active.find((e) => e.id === wanted) : undefined)
    ?? active[0];
}

/** The step the business wizard opens at.
 *
 *  With a business in hand: its accounts step, the first one that needs
 *  the entity. Without one: the first question, whatever the step marks
 *  say. The marks are household-wide and outlive the business they were
 *  written for (a deleted one, or another business altogether), and every
 *  step after the questions writes against the entity — opened with none,
 *  the accounts step would "assign" accounts back to personal and report
 *  success. Nor can a run resume between the two questions: their answers
 *  live in the screen until the entity is created at the end of the second,
 *  so reopening there would ask for dates with the name already gone. */
export function bizWizardEntryStep(
  steps: readonly { key: string }[], hasEntity: boolean,
): number {
  return hasEntity ? steps.findIndex((s) => s.key === "accounts") : 0;
}

/** A business transaction as the P&L reads it (books.entity_transactions). */
export interface BizProfitRow {
  date: string | null; amount: number;
  /** the amount net of partial reimbursement links, ledger sign kept */
  net_amount?: number | null;
  category: string | null; cat_detailed: string | null;
  cat_original?: string | null;
  /** a deposit whose links claim only part of it — still revenue */
  partly_claimed?: boolean | null;
  bucket: string | null;
  /** the bucket the server's P&L counted this row in: a refund takes the
   *  bucket of the cost it reverses, which only the server can find (the
   *  charge may sit in another period or year). Null for money in that is
   *  not a refund; absent from an older server. */
  effective_bucket?: string | null;
}

/** Money IN still carrying one of these spending categories is a merchant
 *  giving money back — a refund that reverses a cost in its own bucket, not
 *  revenue. The server's list (books._REFUND_CATEGORIES); the mobile
 *  pure-logic suite checks the two clients' copies against it. */
export const BIZ_REFUND_CATEGORIES: ReadonlySet<string> = new Set([
  "BANK_FEES", "ENTERTAINMENT", "FOOD_AND_DRINK", "GENERAL_MERCHANDISE",
  "GENERAL_SERVICES", "GOVERNMENT_AND_NON_PROFIT", "HOME_IMPROVEMENT",
  "MEDICAL", "PERSONAL_CARE", "RENT_AND_UTILITIES", "TRANSPORTATION",
  "TRAVEL", "PROFESSIONAL_SERVICES", "INSURANCE",
]);

/** Operating profit over the rows whose date starts with `period`
 *  ("2026-08" for a month), by the server's P&L rules, so a period figure
 *  shown under the year's profit is the same arithmetic.
 *
 *  - Amounts are NET of reimbursement links: a charge paid back counts only
 *    what was not repaid, a deposit only the part no charge claimed. Gross
 *    figures would count one client check that repays two charges as both
 *    revenue and a cost.
 *  - Money in is revenue unless it is a transfer or a card payment, and
 *    money whose ORIGINAL category was a transfer stays capital unless it
 *    was made INCOME outright — except a partly-claimed deposit, whose
 *    unclaimed part is revenue whatever its category says.
 *  - Money in that still carries a spending category is a refund: it
 *    reverses a cost in its own bucket, so a start-up or organizational
 *    refund leaves the period untouched (its cost was capitalized) and an
 *    operating refund lowers operating cost. The row's `effective_bucket`
 *    names that bucket when the server sent it — a refund dated after the
 *    business opened can reverse a start-up purchase. Counted as revenue, a
 *    returned pre-opening purchase would show as profit.
 *  - Money out is an operating cost only in the operating bucket;
 *    organizational and start-up costs are capitalized, and an unbucketed
 *    cost dated before the business opened is a start-up cost. */
export function bizPeriodProfit(
  rows: readonly BizProfitRow[], businessStart: string | null | undefined,
  period: string,
): number {
  let revenue = 0;
  let operating = 0;
  for (const t of rows) {
    if (!t.date || !t.date.startsWith(period)) continue;
    const net = t.net_amount ?? t.amount;
    const cat = (t.category || "").toUpperCase();
    const card = (t.cat_detailed || "") === "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT";
    // the server's own bucket first, so a refund nets against the cost it
    // reverses rather than being re-bucketed by its own date
    const bucket = t.effective_bucket ?? (t.bucket
      || (businessStart && t.date < businessStart
        ? "startup_195" : "operating"));
    const capitalized = bucket === "organizational"
      || bucket === "startup_195";
    if (t.amount < 0) {
      if (t.partly_claimed) { revenue += -net; continue; }
      const wasTransfer = (t.cat_original || "").toUpperCase()
        .includes("TRANSFER");
      if (cat.includes("TRANSFER") || card
          || (wasTransfer && cat !== "INCOME")) continue;
      if (BIZ_REFUND_CATEGORIES.has(cat)) {
        // net is negative: the refund lowers the cost it reverses
        if (!capitalized) operating += net;
      } else revenue += -net;
      continue;
    }
    if (cat === "TRANSFER_OUT" || cat === "TRANSFER_IN" || card) continue;
    if (capitalized) continue;
    if (net > 0) operating += net;
  }
  return pyround((revenue - operating) * 100) / 100;
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

// ---- the debt planner's two labels (web: pages/Debt.tsx) ------------------

// '2029-03-01' → 'Mar 2029': the schedule walks month firsts, and the
// day would read as a fact the walk never claimed
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export const monthYear = (iso: string | null | undefined) => {
  if (!iso) return "";
  const m = Number(iso.slice(5, 7)) - 1;
  return `${MONTHS[m] ?? ""} ${iso.slice(0, 4)}`;
};

// months → "2 yr 5 mo"; null is the walk's "not within fifty years"
export const span = (months: number | null) => {
  if (months === null) return "not within fifty years";
  if (months === 0) return "today";
  const y = Math.floor(months / 12), m = months % 12;
  const parts = [];
  if (y) parts.push(`${y} yr`);
  if (m) parts.push(`${m} mo`);
  return parts.join(" ");
};

/** The Today hero's verdict sentence — one copy for the hero, the lens
 *  pages and the home-screen widget. */
export const heroLabel = (verdict: string): string =>
  verdict === "OVER BUDGET" ? "Over budget"
    : verdict === "UNDER BUDGET" ? "Under budget" : "On budget";


// ---- money drafts, signed balances, the planner's bucket merge ----
// The web keeps a twin of each in webapp/src/moneymath.ts; the server
// suite runs one case table against both, so the two cannot drift.

/** A typed signed amount, or null when the text is not one yet.
 *
 *  Money fields keep the TEXT while it is typed and parse only here, at
 *  save. Parsing on every keystroke made the field unusable: "-" and
 *  "0-" are NaN (the field then showed "NaN" and stayed stuck there),
 *  and "1234." re-rendered as "1234", so a decimal point could never
 *  be typed at the end. "$", "," and spaces are dropped; a typographic
 *  minus counts as "-". Blank is the caller's call (0, or "cleared"). */
export function parseMoneyDraft(v: string): number | null {
  const t = (v ?? "").replace(/[$,\s]/g, "").replace(/\u2212/g, "-");
  if (!/^-?(\d+\.?\d*|\.\d+)$/.test(t)) return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}

/** A receipt's total, date and store as typed on a waiting receipt, ready
 *  for POST /api/receipts/{id}/details — or the reason it is not yet.
 *  The total is money in: positive, rounded to the cent ("$27.35",
 *  "1,204.5"). The date is a real calendar day, YYYY-MM-DD. The store is
 *  optional, trimmed, 80 characters at most; blank sends none. */
export function receiptDetailsDraft(total: string, date: string,
                                    merchant = ""):
    { ok: true; body: { total: number; date: string; merchant?: string } }
  | { ok: false; error: string } {
  const n = parseMoneyDraft(total);
  if (n === null) return { ok: false, error: "Enter the total, like 27.35" };
  // banker's rounding like every other money figure here. n * 100 lands
  // a hair under the true value (4.015 → 401.4999…), which would tip a
  // half the wrong way, so the product is squared up to four decimals
  // before the half is judged
  const cents = pyround(Number((n * 100).toFixed(4)));
  if (cents <= 0) return { ok: false, error: "The total must be more than zero" };
  const d = (date ?? "").trim();
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(d);
  const real = m && (() => {
    const t = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
    return t.getUTCFullYear() === +m[1] && t.getUTCMonth() === +m[2] - 1
      && t.getUTCDate() === +m[3];
  })();
  if (!real) return { ok: false, error: "Enter the date on the receipt" };
  const store = (merchant ?? "").trim();
  if (store.length > 80)
    return { ok: false, error: "The store name is 80 characters at most" };
  return { ok: true, body: { total: cents / 100, date: d,
                             ...(store ? { merchant: store } : {}) } };
}

/** An account's contribution to a headline total. Card and loan
 *  balances are stored as the positive amount OWED (every aggregator is
 *  normalised to that), so summing balance_current as-is ADDED debt to
 *  what the household has. `kind` is "type/subtype". */
export function signedBalance(a: { kind: string;
                                   balance_current: number | null }): number {
  const bal = a.balance_current ?? 0;
  const t = (a.kind || "").split("/")[0];
  return t === "credit" || t === "loan" ? -bal : bal;
}

/** one budget-planner row as drafted on screen */
export type PlannerRow = {
  name: string; monthly: string; food?: boolean; category?: string;
  cats?: string[]; merch?: string[];
  /** the server bucket this row was seeded from (absent: added here) */
  orig?: string;
  /** name or amount edited in the planner since it was seeded */
  touched?: boolean;
};

/** The custom_buckets list the budget planner saves.
 *
 *  POST /api/settings replaces the list wholesale, and the planner's rows
 *  were seeded at mount — the Bucket-rules card or another device may
 *  have saved since. The planner owns only the NAME and AMOUNT of the
 *  "other" buckets, so against the server's CURRENT list: food buckets
 *  are carried through; an untouched row follows its live copy (deleted
 *  elsewhere → stays deleted); a touched row writes its name and amount
 *  over the live copy's categories and merchants; a baseline bucket the
 *  person removed here stays removed; and buckets added elsewhere since
 *  mount are kept. Rebuilding every row from the seed silently reverted
 *  rule edits saved in between. */
export function mergePlannerBuckets(
  rows: readonly PlannerRow[],
  live: readonly CustomBucket[],
  baselineNames: readonly string[],
): CustomBucket[] {
  const liveBy = new Map(live.filter((b) => b.parent === "other")
    .map((b) => [b.name, b] as const));
  const baseNames = new Set(baselineNames);
  const out: CustomBucket[] = live.filter((b) => b.parent === "food");
  const taken = new Set(out.map((b) => b.name));
  const push = (b: CustomBucket) => {
    if (taken.has(b.name)) return;
    taken.add(b.name); out.push(b);
  };
  for (const r of rows) {
    if (r.food) continue;
    const cur = r.orig !== undefined ? liveBy.get(r.orig) : undefined;
    if (r.orig !== undefined && !r.touched) {
      if (cur) push(cur);
      continue;                    // untouched: live copy, or deleted
    }
    const name = r.name.trim();
    const monthly = numv(r.monthly);
    if (!name || monthly <= 0) continue;
    push({ name, parent: "other", monthly,
           categories: cur?.categories
             ?? r.cats ?? (r.category ? [r.category] : []),
           merchants: cur?.merchants ?? r.merch ?? [] });
  }
  const kept = new Set(rows.map((r) => r.orig).filter(Boolean));
  for (const b of liveBy.values())
    if (!baseNames.has(b.name) && !kept.has(b.name)) push(b);
  return out;
}

/** Whether a file in the app cache is one this app downloaded or wrote
 *  for the share sheet: the full-data export, the database dump
 *  (.sql.gz), CSVs, the year-end ZIP, the continuity PDF, the compliance
 *  calendar, receipt images and the passphrase-sealed connections bundle
 *  (.oikx). Everything else in the cache (the platform's own
 *  subdirectories, image-picker scratch) is not ours to delete. */
export const isSharedDownload = (name: string): boolean =>
  /\.(zip|csv|sql|gz|pdf|ics|jpe?g|png|webp|oikx)$/i.test(name);

/** Which of those files a sweep deletes. `maxAgeS` 0 deletes every one
 *  (sign-out: nothing of this household's may stay behind); a positive
 *  age spares files younger than that, because on Android the share
 *  sheet hands off before the target app has read the file, and deleting
 *  it at once truncates the share it followed. */
export function downloadsToSweep(
  files: readonly { name: string; mtimeS: number | null | undefined }[],
  nowS: number, maxAgeS: number,
): string[] {
  return files
    .filter((f) => isSharedDownload(f.name)
      && (maxAgeS <= 0 || (f.mtimeS ?? 0) < nowS - maxAgeS))
    .map((f) => f.name);
}

/** The expected-payback amount typed into "edit expected". Blank or 0
 *  clears the expectation (the web's reading of the same field); a
 *  positive amount sets it; anything else — a negative, or text that is
 *  not a number, such as a second decimal point — is refused so the
 *  editor can say so, instead of being sent and either rejected unseen
 *  or read as "clear". */
export function expectedFromDraft(draft: string):
    { ok: true; expected: number | null } | { ok: false } {
  if (!(draft ?? "").trim()) return { ok: true, expected: null };
  const n = parseMoneyDraft(draft);
  if (n === null || n < 0) return { ok: false };
  return { ok: true, expected: n === 0 ? null : n };
}

/** The pairing sheet's running total. It sums the chosen deposits the
 *  sheet has SEEN, not only the ones the current filter shows: a deposit
 *  ticked and then filtered out of view is still sent with Pair, so a
 *  total that dropped it would show one outcome while the server records
 *  another. `hidden` counts the chosen deposits the list no longer
 *  shows, so the sheet can say they are still in. */
export function pairSelection(
  chosen: ReadonlyMap<string, { amount: number; left_amount?: number | null }>,
  visibleIds: readonly string[],
): { total: number; count: number; hidden: number } {
  const vis = new Set(visibleIds);
  let total = 0, hidden = 0;
  for (const [id, c] of chosen) {
    // what each row has LEFT to give (reimbLeft), as the link will draw it
    total += reimbLeft(c);
    if (!vis.has(id)) hidden += 1;
  }
  return { total, count: chosen.size, hidden };
}

// ---- reimbursements, the category pickers, local dates, the device zone ----
// The web keeps a twin of each in webapp/src/moneymath.ts under the same
// name; the shared case table (scripts/money-cases.mjs) runs against both.

/** What is still owed back across the charges awaiting reimbursement.
 *
 *  Each charge's target is its EXPECTED amount when one is set (a partial
 *  expectation — the insurer's share of a bill), else the charge itself;
 *  receipts already linked come off it, floored at 0. This is the figure
 *  the Today strip and the daily email state, and the one the link path
 *  clears a flag against, so the page's headline must not sum the charges'
 *  face value instead. */
export function reimbOwed(pending: readonly {
  amount: number; expected?: number | null; received?: number | null;
}[]): number {
  return pending.reduce((a, t) => {
    // the signed amount, as the server takes it: a flagged deposit (money
    // that came IN) with no expectation owes nothing back, where its
    // absolute value would add the whole deposit to the headline. An
    // expectation is capped at the charge, as the server caps it: no link
    // can bring back more than the charge, and a flag saved before that
    // rule (or restored from an archive) may still expect more.
    const target = Math.min(t.expected ?? t.amount, t.amount);
    return a + Math.max(0, target - (t.received ?? 0));
  }, 0);
}

/** The categories a merchant rule or a split part can take, grouped.
 *
 *  Transfers, income and loan payments are not offered: a rule re-files
 *  every charge from a merchant and a split carves one charge into spend,
 *  and the server refuses a flow category for both — offering them only
 *  produced a 400 after the pick. Custom names the household made itself
 *  stay selectable, except one that spells a flow category in another
 *  case (the split endpoint compares upper-cased). */
export function spendPickerGroups(c: {
  categories?: readonly string[]; plaid_spend?: readonly string[];
  plaid_flow?: readonly string[];
} | undefined): { spend: string[]; custom: string[] } {
  const spend = [...(c?.plaid_spend ?? [])];
  const flow = new Set((c?.plaid_flow ?? []).map((f) => f.toUpperCase()));
  const std = new Set(spend);
  const custom = (c?.categories ?? []).filter((x) => x && !std.has(x)
    && !flow.has(x.toUpperCase()));
  return { spend, custom };
}

/** A date as the LOCAL calendar day, "YYYY-MM-DD".
 *
 *  toISOString() is the UTC day: west of Greenwich it is already tomorrow
 *  for the whole evening, so a form seeded with it dates a trip or an owner
 *  contribution a day late — on Dec 31, in the next tax year. */
export function localYmd(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-`
    + String(d.getDate()).padStart(2, "0");
}

/** Whether a client adopts its device's zone as the household's, unasked.
 *
 *  Only on a hosted instance: there the instance clock is the operator's,
 *  so a household west of it would get its morning mail at 2am. A
 *  self-hosted instance's zone (OIKONOME_TZ) is the operator's deliberate
 *  setting for every household without one, and pinning a zone silently
 *  would make it stop applying — there Settings offers the device zone
 *  instead. Never for a member or on a demo, never over a zone already
 *  chosen, and never a UTC/Etc zone: that is what a privacy-hardened
 *  browser reports, not where anyone is. */
export function adoptsDeviceZone(m: {
  role: string; demo?: boolean; hosted?: boolean;
  timezone?: string; timezone_source?: string;
} | undefined, device: string): boolean {
  if (!m || m.role !== "owner" || m.demo || !m.hosted) return false;
  if (m.timezone_source !== "instance") return false;
  if (!device || device === m.timezone) return false;
  return !/^(Etc\/|UTC$|GMT$|Universal$|Zulu$)/.test(device);
}

/** What a sign-in left behind when its device mint was refused at the
 *  account's device ceiling: the one-shot mint ticket (and the session
 *  cookie, for servers older than the ticket) from that login. The server
 *  rolls a refused mint back, so both stay good for the retry until the
 *  ticket's five minutes run out. */
export type MintGrant = { ticket: string; cookie: string; issuedAt: number };

/** The server's mint-ticket lifetime, less a margin so a retry is never
 *  sent with a ticket that dies in flight. */
export const MINT_GRANT_USABLE_MS = 5 * 60 * 1000 - 30 * 1000;

/** How the device-limit picker carries out the person's choice.
 *
 *  "mint": spend the grant from the login that just succeeded — the only
 *  way through for an account with a second factor, because that login
 *  already used its one-time code (a TOTP code is burned on use and a
 *  recovery code is gone), so running the login again with the same code
 *  is refused every time and the picker could never be passed.
 *  "login": no usable grant, and the credentials can simply be sent again
 *  (no one-time code was involved, or a passkey — which signs a fresh
 *  challenge each time).
 *  "ask-code": no usable grant, and the only credentials on hand include a
 *  spent one-time code — ask for a fresh one rather than send a code the
 *  server has already refused to take twice. */
export function deviceLimitRetry(grant: MintGrant | null, nowMs: number,
                                 usedOneTimeCode: boolean):
    "mint" | "login" | "ask-code" {
  if (grant && (grant.ticket || grant.cookie)
      && nowMs - grant.issuedAt >= 0
      && nowMs - grant.issuedAt < MINT_GRANT_USABLE_MS)
    return "mint";
  return usedOneTimeCode ? "ask-code" : "login";
}

/** A retried mint the server refused because the grant is no longer good
 *  (ticket spent, expired, or burned by a sign-out elsewhere): the
 *  picker falls back as if there had been no grant at all. */
export const mintGrantRefused = (status: number) => status === 401;

/** What a list screen shows in place of its rows. A list that never
 *  loaded is not an empty list: a first fetch that failed with nothing
 *  cached must say it failed, or "No accounts yet — connect your bank"
 *  tells someone with accounts that they have none. Data on hand (even
 *  stale, even empty) is the answer; the stale banner covers its age. */
export type ListPhase = "loading" | "failed" | "empty" | "rows";
export function listPhase(q: { isError: boolean; hasData: boolean },
                          count: number): ListPhase {
  if (count > 0) return "rows";
  if (q.hasData) return "empty";
  return q.isError ? "failed" : "loading";
}

// ---- reimbursement matcher: twin block, byte-identical in
// webapp/src/moneymath.ts and mobile/src/lib/pure.ts ----

/** A matcher figure, always in cents: the full-pair allowance is a dollar,
 *  so a whole-dollar "$0 short" would hide the gap it is about. */
const cents2 = (n: number) => "$" + Math.abs(n).toLocaleString("en-US",
  { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const round2 = (n: number) => pyround(n * 100) / 100;

/** What a candidate row can still give: the remainder the server reports
 *  (`left_amount` — a deposit already spent on other charges has only the
 *  rest to give), else its face value. */
export const reimbLeft = (r: { amount: number; left_amount?: number | null }) =>
  Math.abs(r.left_amount ?? r.amount);

/** What the anchor still needs: its remainder once earlier links are
 *  netted off, or its face value when nothing is left (the server ranks
 *  such an anchor's candidates by face value too). */
export const reimbNeed = (a: { amount: number; left_amount?: number | null }) =>
  reimbLeft(a) > 0.005 ? reimbLeft(a) : Math.abs(a.amount);

/** Why a row is offered as the other side of a pair: how what it has LEFT
 *  compares with what the anchor still NEEDS — the figures the server
 *  ranks and links by — and how far from the anchor's date it landed. By
 *  face value a $40 deposit is "$60 short" of a $100 charge that already
 *  got $60 back, when it is the exact match for what is still owed. */
export function reimbCandidateWhy(
  c: { amount: number; date: string; left_amount?: number | null },
  anchor: { amount: number; date: string; left_amount?: number | null },
): string {
  const has = reimbLeft(c);
  const need = reimbNeed(anchor);
  const gap = round2(Math.abs(has - need));
  const days = pyround(
    (new Date(c.date + "T00:00:00").getTime()
     - new Date(anchor.date + "T00:00:00").getTime()) / 86_400_000);
  const amt = gap < 0.005 ? "exact amount"
    : has < need ? `${cents2(gap)} short` : `${cents2(gap)} over`;
  // Candidates span 180 days either side, so a row well before the
  // anchor says how far before: lumping it in with "same day" made the
  // least likely refund look like the closest match.
  const when = days === 0 ? "same day"
    : days === -1 ? "1 day earlier"
    : days < 0 ? `${-days} days earlier`
    : days === 1 ? "1 day later" : `${days} days later`;
  return `${amt} · ${when}`;
}

/** The server pairs a charge in FULL when a lone deposit misses what it
 *  still needs — short or over — by no more than this, and records only
 *  what came back otherwise (kept equal to the server's full-link slack). */
export const FULL_LINK_SLACK = 1.0;

/** The matcher's line under "selected $X of $Y": what Pair will really do.
 *
 *  `lefts` are the ticked rows' remainders in the order they are sent.
 *  The server links them one at a time, so this replays its rule rather
 *  than comparing one total: a deposit pairs the whole charge only when
 *  nothing has come back to it yet (`received`), "partial" is unticked and
 *  what the deposit has left is within the slack of what the charge still
 *  needs; every other link records min(still needed, deposit left). So
 *  two deposits $0.50 short of a charge leave $0.50 as spend, and a
 *  deposit matching what a partly repaid charge still needs clears it.
 *
 *  A flagged DEPOSIT as anchor states what of it the ticked charges leave
 *  as income, or what of them it leaves as spend. */
export function pairGapHint(
  lefts: readonly number[],
  anchor: { amount: number; left_amount?: number | null;
            received?: number | null },
  partial: boolean,
): string {
  const need = reimbNeed(anchor);
  const sel = round2(lefts.reduce((a, x) => a + Math.abs(x), 0));
  if (anchor.amount < 0) {
    const gap = round2(need - sel);
    if (Math.abs(gap) < 0.005) return " — exact ✓";
    return gap > 0 ? ` — ${cents2(gap)} of this deposit stays income`
      : ` — ${cents2(-gap)} of those charges stays as spend`;
  }
  let owed = need;
  let got = anchor.received ?? 0;
  // what the links really draw from the ticked deposits; the rest of them
  // stays income
  let used = 0;
  for (const raw of lefts) {
    const left = Math.abs(raw);
    if (owed <= 0.005) break;
    if (!partial && got <= 0.005 && Math.abs(owed - left) <= FULL_LINK_SLACK) {
      const gap = round2(owed - left);
      if (lefts.length === 1) {
        if (Math.abs(gap) < 0.005) return " — exact ✓";
        return gap > 0
          ? ` — ${cents2(gap)} short, close enough: the whole charge counts`
            + " as repaid"
          : ` — ${cents2(-gap)} over the charge`;
      }
      used += Math.min(left, owed);
      got += owed;
      owed = 0;
      break;
    }
    const take = Math.min(owed, left);
    owed = round2(owed - take);
    got += take;
    used += take;
  }
  const extra = round2(sel - used);
  if (owed < 0.005 && extra < 0.005) return " — exact ✓";
  const parts: string[] = [];
  if (extra >= 0.005)
    parts.push(`${cents2(extra)} over the charge; the rest stays income`);
  if (owed >= 0.005) parts.push(`${cents2(owed)} stays as spend`);
  return " — " + parts.join("; ");
}

// ---- end of the reimbursement matcher twin block ----

/** The Rules card's line when a page has no rows, as the web words it:
 *  the first-run explanation only when there are no rules at all; a
 *  search with no hits and an empty tab say so instead. */
export function rulesEmptyLine(counts: Record<string, number | undefined>,
                               search: string):
    "first-run" | "No matches in this group." | "No rules here yet." {
  const all = Object.values(counts).reduce<number>((a, n) => a + (n ?? 0), 0);
  if (all === 0) return "first-run";
  return search.trim() ? "No matches in this group." : "No rules here yet.";
}

/** The last page a list of `total` rows has — 1 for an empty list. A
 *  page number past it (rows deleted from the last page) is pulled back
 *  here rather than showing an empty page as if the list were empty. */
export const lastPage = (total: number, perPage: number): number =>
  Math.max(1, Math.ceil(total / Math.max(1, perPage)));

/** Whether this install's push registration can be left alone at
 *  launch: the token is the one last sent to this server AND the server
 *  still counts this device's push live. A row the relay marked dead
 *  ("dead" — the platform refused the token once, or the relay was
 *  pointed at the wrong project) or one holding no token (null) only
 *  heals by registering again, since every register clears the dead
 *  mark. No roster (an older server, a failed read, the demo's hidden
 *  one) means re-send, which is what every launch did before. */
export function pushStillLive(
    lastSent: string | null, sending: string,
    devices: readonly { current: boolean; push?: string | null }[]
      | null | undefined): boolean {
  if (lastSent !== sending || !devices) return false;
  return devices.find((d) => d.current)?.push === "on";
}
