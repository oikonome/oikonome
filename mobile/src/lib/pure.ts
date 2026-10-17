// Dependency-free money and merge logic. This module must import no
// React Native code: mobile has no JS test runner, so plain node
// (`node --experimental-strip-types scripts/pure-logic-tests.mjs`)
// executes the unit tests for the two behaviours here that fail
// silently — a $1 rounding drift off the web/email, and a savings
// goal lost to a concurrent edit. The server suite runs that script
// too, so `make test` goes red on a regression.
import type { AccountRemoveMode, CustomBucket, ElevationProof,
              ElevationStatus, IncomeScenario, SavingsGoal } from "./api";

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
                  counts_as_spend?: boolean | null;
                  spend_amount?: number | null };

/** What one ledger row adds to the day's spending.
 *
 *  The server sends the verdict's own figure (spend_amount: net of a
 *  partial reimbursement, a split row's matching parts' share under the
 *  search's category filter, 0 for any row the verdict does not count — transfers, money in, excluded
 *  or hidden accounts, linked shadows). Summing that, not the amount
 *  column, is what makes a day subtotal agree with the month hero; the
 *  client must not re-derive spend semantics, because the display
 *  category can't see overrides collapsing to transfers or loan-account
 *  rows.
 *
 *  Fallbacks exist only for a NEW app against an OLDER server: the
 *  counts_as_spend flag with the gross amount, and before that a category
 *  rule — without them every day would total $0. Mirrors the web's
 *  TxnTable rowSpend exactly. */
export const rowSpend = (t: DatedRow): number => {
  if (typeof t.spend_amount === "number") return t.spend_amount;
  const counts = typeof t.counts_as_spend === "boolean"
    ? t.counts_as_spend
    : t.amount > 0 && !(t.category || "").includes("TRANSFER");
  return counts ? t.amount : 0;
};

/** What one day of the ledger cost: the sum of rowSpend, never the
 *  amount column (see rowSpend). Mirrors the web's TxnTable daySpend. */
export const daySpend = (group: readonly DatedRow[]) =>
  group.reduce((sum, t) => sum + rowSpend(t), 0);

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

/** Did approving an offer find the bill already tracked? The server then
 *  withdraws the offer itself, so the card can neither be approved nor
 *  dismissed and must leave the list. Read from the reply's flag, or from
 *  a refusal (an ApiError: `detail` its sentence, `body` the parsed reply)
 *  that carries the flag or says "already tracked" — a server that
 *  predates the flag. The web keeps the same rule in api/client.ts. */
export function proposalSuperseded(replyOrError: unknown): boolean {
  if (!replyOrError || typeof replyOrError !== "object") return false;
  const o = replyOrError as { superseded?: unknown; detail?: unknown;
                              body?: unknown; message?: unknown };
  if (o.superseded === true) return true;
  const b = o.body as { superseded?: unknown } | null | undefined;
  if (b && typeof b === "object" && b.superseded === true) return true;
  const text = typeof o.detail === "string" ? o.detail
    : typeof o.message === "string" ? o.message : "";
  return /already tracked/i.test(text);
}

/** The longest category name the server stores; it refuses longer ones,
 *  so the box a custom name is typed into stops there. */
export const CATEGORY_MAX = 64;

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
/** The classify write an account editor owes, or null when neither the
 *  kind nor the primary toggle changed. `primary` is null when the person
 *  left the toggle alone: the key is then omitted and the server keeps the
 *  household's anchor where it is, rather than this device re-sending a
 *  pin it read from a cache another device may have moved since. */
export function classifyChange(kindBefore: string, kindAfter: string,
                               primary: boolean | null):
    { kind: string; primary?: boolean } | null {
  if (kindAfter === kindBefore && primary === null) return null;
  return primary === null ? { kind: kindAfter }
                          : { kind: kindAfter, primary };
}

/** The SVG markup inside an `image/svg+xml` data URI (percent-encoded or
 *  base64), or null for anything else. The native image views decode no
 *  SVG, so the enrolment QR is drawn from this markup instead. A root with
 *  only width/height gains a matching viewBox, so the drawing scales to the
 *  box it is given rather than being cropped. */
export function svgFromDataUri(uri: string): string | null {
  const m = /^data:image\/svg\+xml(;[^,]*)?,([\s\S]*)$/i.exec(uri);
  if (!m) return null;
  let xml: string;
  try {
    xml = /;base64/i.test(m[1] ?? "") ? atob(m[2]) : decodeURIComponent(m[2]);
  } catch {
    return null;
  }
  const root = /<svg\b[^>]*>/i.exec(xml);
  if (!root) return null;
  if (!/\bviewBox=/i.test(root[0])) {
    const w = /\bwidth=["']?([\d.]+)/i.exec(root[0]);
    const h = /\bheight=["']?([\d.]+)/i.exec(root[0]);
    if (w && h)
      xml = xml.replace(/<svg\b/i, `<svg viewBox="0 0 ${w[1]} ${h[1]}"`);
  }
  return xml;
}

/** The sentence for a refused one-time code, or null when the refusal is
 *  about something else. A code from a step already spent is not a clock
 *  problem, so it gets its own sentence and is checked first. */
export function oneTimeCodeSentence(text: string): string | null {
  if (/code_used|already (been )?used/i.test(text))
    return "That code was already used — enter the next one from your app.";
  if (/one-time code|doesn't match/i.test(text))
    return "That code didn't match — check your app's clock.";
  return null;
}

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

// ---- an address still to confirm closes the hosted bank door ----
// Hosted only: until an owner of the household has confirmed, the plan's
// institution allowance is zero and the server refuses the link, so every
// add-a-bank door shows the confirm card and disables itself first.
// `verified` absent (self-host, an older server) is never "unconfirmed".
// Statement and CSV imports stay open. Mirrors webapp awaitsEmailConfirm.
export function awaitsEmailConfirm(
    me: { hosted?: boolean; verified?: boolean } | undefined): boolean {
  return !!me?.hosted && me.verified === false;
}

// Whether the add-a-bank doors are closed until an owner confirms. The
// server says so (`features.bank_awaits_confirm`), because only it knows
// why the allowance is 0: an ordinary free trial waits on confirmation, a
// paying household does not, whether or not its owner has confirmed. Only
// a server too old to say falls back to awaitsEmailConfirm. Mirrors
// webapp bankAwaitsConfirm.
export function bankAwaitsConfirm(
    me: { hosted?: boolean; verified?: boolean;
          features?: { bank_awaits_confirm?: boolean } } | undefined):
    boolean {
  const said = me?.features?.bank_awaits_confirm;
  return typeof said === "boolean" ? said : awaitsEmailConfirm(me);
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
 *  onward — the ledger shows what is there to edit, so it keeps the month
 *  in progress that Cash Flow's complete-month windows leave out; "This
 *  month" is from the 1st;
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
// would drop May from a "3m" window whose server figures cover
// May+Jun+Jul, computing the caption's best and worst month over a window
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

// How many COMPLETE months each cash-flow range holds — the server's
// reporting.FLOW_RANGES, read through `_range_window`: every key but "cur"
// is N complete months ending with LAST month, and "cur" is the month in
// progress alone (no closed month at all). Kept apart from RANGE_MONTHS on
// purpose: that table counts back from the current month INCLUSIVE, which
// is right for the ledger filter and the net-worth trend (both keep the
// month in progress) and one month short for a complete-months window.
const CASHFLOW_COMPLETE_MONTHS: Record<TrendRange, number | null> =
  { cur: 0, "1m": 1, "3m": 3, "6m": 6, "1y": 12, "3y": 36, "5y": 60, all: null };

/** The first month a cash-flow range covers, anchored on the household's
 *  month rather than the clock here. `today` is only the fallback for a
 *  report with no forecast tail to read the household's month from.
 *  In October, "3m" opens in July (Jul+Aug+Sep, the server's window), "1m"
 *  in September and "cur" in October — whose history is empty, because the
 *  cash graph's history stops before the current month.
 *  The twin is `cashflowCutoffMonth` in webapp/src/components/RangePicker.tsx — keep them identical. */
export function cashflowCutoffMonth(
  graph: { points: [string, number][]; forecast_from: number },
  range: TrendRange, today: Date = new Date(),
): string | null {
  const complete = CASHFLOW_COMPLETE_MONTHS[range];
  if (complete === null) return null;
  const cur = graphCurrentMonth(graph)
    ?? new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), 1))
      .toISOString().slice(0, 7);
  // a window of `complete` months whose last month is the one before `cur`
  return monthsBackFrom(cur, complete + 1);
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

/** What to say after a removal. A purge that kept rows (the bank feed
 *  already relies on them) explains so in the purge's own note, beside
 *  the removal's note; the same sentence is never said twice. */
export function accountRemoveNote(r: {
  note?: string | null; plaid_released?: boolean;
  purged?: { note?: string | null } | null;
}): string {
  const parts: string[] = [];
  if (r.note) parts.push(r.note);
  const kept = r.purged?.note;
  if (kept && !(r.note ?? "").includes(kept)) parts.push(kept);
  return (parts.join(" ") || "Removed.")
    + (r.plaid_released ? " Plaid connection released." : "");
}

/** The question a delete refused with "kept_rows_unconfirmed" asks, or
 *  null for any other error. The server keeps imported rows a bank feed
 *  may have set its own copy aside for; when every kept row is held only
 *  by a note that names no feed account, nothing would ever release them,
 *  so it asks the owner instead and a "delete anyway" re-sends with
 *  `force_kept`. Structural, so node can pin it without the API module.
 *  The web twin is in webapp/src/components/RemoveAccountPanel.tsx. */
export function keptRowsQuestion(e: unknown): string | null {
  const x = e as { status?: unknown; detail?: unknown; body?: unknown };
  const body = x?.body as { error?: unknown; detail?: unknown } | undefined;
  if (x?.status !== 409 || body?.error !== "kept_rows_unconfirmed")
    return null;
  return typeof body.detail === "string" && body.detail ? body.detail
    : typeof x.detail === "string" ? x.detail : null;
}

/** The note an undo refused with "kept_rows" carries: a member's undo
 *  that would leave such rows is told why and offered nothing, because
 *  deleting them anyway is the owner's call. Null for any other error.
 *  The web twin is in webapp/src/components/RemoveAccountPanel.tsx. */
export function keptRowsNote(e: unknown): string | null {
  const x = e as { status?: unknown; detail?: unknown; body?: unknown };
  const body = x?.body as { error?: unknown; detail?: unknown } | undefined;
  if (x?.status !== 409 || body?.error !== "kept_rows") return null;
  return typeof body.detail === "string" && body.detail ? body.detail
    : typeof x.detail === "string" && x.detail ? x.detail : null;
}

/** Did a purge keep rows, and so keep the account to hold them? */
export const purgeKeptRows = (r: {
  purged?: { kept_transactions?: number } | null;
}): boolean => (r.purged?.kept_transactions ?? 0) > 0;

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
  /** the Schedule C line the user chose; a tax payment with one is a
   *  business cost again (see bizPeriodProfit) */
  sched_c_line?: string | null;
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
 *    cost dated before the business opened is a start-up cost.
 *  - An owner's tax payment (and a tax refund) is not a business cost:
 *    income and self-employment tax belong on the owner's return, so it is
 *    skipped like a transfer unless the user gave it a Schedule C line. */
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
    const ownerTax = !t.sched_c_line
      && (t.cat_detailed === "GOVERNMENT_AND_NON_PROFIT_TAX_PAYMENT"
        || t.cat_detailed === "INCOME_TAX_REFUND");
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
      if (cat.includes("TRANSFER") || card || ownerTax
          || (wasTransfer && cat !== "INCOME")) continue;
      if (BIZ_REFUND_CATEGORIES.has(cat)) {
        // net is negative: the refund lowers the cost it reverses
        if (!capitalized) operating += net;
      } else revenue += -net;
      continue;
    }
    if (cat === "TRANSFER_OUT" || cat === "TRANSFER_IN" || card
        || ownerTax) continue;
    if (capitalized) continue;
    if (net > 0) operating += net;
  }
  return pyround((revenue - operating) * 100) / 100;
}

/** The tax years the year-end package and the 1099 list offer, newest
 *  first: this year back to the year the business was formed or started,
 *  whichever is earlier (pre-opening costs sit in the formation year), at
 *  most six; three when neither date is known, the same reach as the
 *  P&L's own year list. */
export function bizTaxYears(
  today: Date, formed: string | null | undefined,
  started: string | null | undefined,
): number[] {
  const now = today.getFullYear();
  const known = [formed, started]
    .map((d) => Number((d || "").slice(0, 4)))
    .filter((y) => y > 1900 && y <= now);
  const oldest = known.length ? Math.max(Math.min(...known), now - 5)
    : now - 2;
  const out: number[] = [];
  for (let y = now; y >= oldest; y--) out.push(y);
  return out;
}

/** The year a tax control opens on. From January through April last
 *  year's return is the one being filed (the accountant's package, the
 *  1099-NEC due January 31), so it opens on last year when the business
 *  had one; otherwise on this year. */
export function bizDefaultTaxYear(
  today: Date, years: readonly number[],
): number {
  const now = today.getFullYear();
  return today.getMonth() <= 3 && years.includes(now - 1) ? now - 1 : now;
}

/** What the year's tax estimate still asks for: the estimate less what the
 *  business already paid toward it (the P&L's estimated_tax_paid), never
 *  below zero. A tax reserve is measured against this, not the whole
 *  year's figure, or paying the estimates out of it reads as a shortfall
 *  that grows each quarter. */
export function bizTaxStillDue(
  estimated: number, paid: number | null | undefined,
): number {
  // The estimate is the tax on profit earned so far, so it already grows
  // with the calendar; the payments are simply taken off it. The books
  // cannot tell April's prior-year balance due from the first estimate
  // paid the same day, so the card says that such a payment is in `paid`
  // rather than guessing a split.
  return pyround(Math.max(0, estimated - (paid ?? 0)) * 100) / 100;
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
// A filter or a spend/recency sort is cut from whatever the capped catalog
// sent, so it asks for the widest window the server allows (500) before
// cutting; the plain orders keep the 200-row page.
export const catalogLimit = (order: MerchantOrder, flt: MerchantFilter): number =>
  flt !== "all" || order === "spend" || order === "recent" ? 500 : 200;
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
    // the local month, as the web's twin reads it: the UTC one is already
    // next month on the evening of the last day west of Greenwich, and
    // "this month" would empty
    const m0 = localYmd(today).slice(0, 7);
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

// ---- a whole-value save, rebased onto what is stored now ----
// Some settings save whole: the server replaces the list or object it is
// sent. An editor's copy is from when it opened, so posting it as-is
// would undo whatever another device saved in between. These lay only
// the person's own changes over the stored value fetched just before the
// save.

const sameValue = (a: unknown, b: unknown) =>
  JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

/** One record after this editor's changes: a field the person changed
 *  since `base` takes their value, every other field the stored (`live`)
 *  one. No `base` means the record was added here: all of it is theirs. */
export function rebaseFields<T extends object>(
  mine: T, base: T | undefined, live: T,
): T {
  const m = mine as Record<string, unknown>;
  const b = (base ?? {}) as Record<string, unknown>;
  const out: Record<string, unknown> = { ...(live as object) };
  for (const k of new Set([...Object.keys(m), ...Object.keys(b)])) {
    if (base !== undefined && sameValue(m[k], b[k])) continue;
    if (m[k] === undefined) delete out[k];
    else out[k] = m[k];
  }
  return out as T;
}

/** A keyed collection, rebased onto the stored one. Per key: changed
 *  here → the person's fields over the stored copy (their own copy when
 *  it was deleted elsewhere); untouched → the stored copy, or gone if it
 *  was deleted elsewhere; removed here → stays removed; added here or
 *  elsewhere → kept. Stored order first, then what was added here. */
export function rebaseKeyed<T extends object>(
  mine: Readonly<Record<string, T>>,
  base: Readonly<Record<string, T>>,
  live: Readonly<Record<string, T>>,
): Record<string, T> {
  const has = (o: object, k: string) => Object.prototype.hasOwnProperty.call(o, k);
  const out: Record<string, T> = {};
  for (const [k, l] of Object.entries(live)) {
    if (!has(base, k)) out[k] = has(mine, k) ? rebaseFields(mine[k], undefined, l) : l;
    else if (has(mine, k)) out[k] = rebaseFields(mine[k], base[k], l);
  }
  for (const [k, v] of Object.entries(mine))
    if (!has(live, k) && (!has(base, k) || !sameValue(v, base[k]))) out[k] = v;
  return out;
}

/** Each row's identity in a list that has no ids: its name, numbered by
 *  occurrence so two rows of one name stay two. */
export function nameKeys(rows: readonly { name: string }[]): string[] {
  const seen = new Map<string, number>();
  return rows.map((r) => {
    const n = seen.get(r.name) ?? 0;
    seen.set(r.name, n + 1);
    return `${r.name}\u0000${n}`;
  });
}

const byNameKey = <T extends { name: string }>(rows: readonly T[]) => {
  const keys = nameKeys(rows);
  return Object.fromEntries(rows.map((r, i) => [keys[i], r])) as Record<string, T>;
};

/** A name-keyed list (the net-worth assets) rebased onto the stored one.
 *  `rows` are the edited rows, each carrying `orig`, its nameKeys() key in
 *  `baseline` (absent: added here); a baseline row missing from `rows`
 *  was removed here. */
export function rebaseNamedList<T extends { name: string }>(
  rows: readonly (T & { orig?: string })[],
  baseline: readonly T[],
  live: readonly T[],
): T[] {
  const mine: Record<string, T> = {};
  rows.forEach((r, i) => {
    const { orig, ...rest } = r;
    mine[orig ?? `\u0001added\u0000${i}`] = rest as unknown as T;
  });
  return Object.values(rebaseKeyed(mine, byNameKey(baseline), byNameKey(live)));
}

/** Whether the stored asset list reads as `sent`, the list a Save wrote:
 *  compared the way the server stores a row (name trimmed and cut to 60,
 *  kind defaulted, value to the cent). A row sent without a date comes
 *  back stamped with today, so its date is compared only when it had one. */
export function assetListLanded(
  sent: readonly { name: string }[], live: readonly { name: string }[],
): boolean {
  const cents = (v: unknown) => pyround(Number(v ?? 0) * 100);
  return sent.length === live.length && sent.every((s, i) => {
    const a = s as Record<string, unknown>;
    const b = live[i] as Record<string, unknown>;
    return String(a.name ?? "").trim().slice(0, 60) === b.name
      && String(a.kind || "other").trim().slice(0, 30) === b.kind
      && cents(a.value) === cents(b.value)
      && (!a.as_of || String(a.as_of) === String(b.as_of));
  });
}

/** rebaseNamedList for a Save that may follow one whose reply never came
 *  (a timeout or a dropped connection after the server had written it).
 *  `sent` is the list that earlier attempt wrote, and each row's `sent`
 *  the place it took there. When the stored list reads as `sent`, that
 *  attempt landed: the stored list becomes the baseline and each row is
 *  keyed by the place it took, so nothing it added or renamed goes in a
 *  second time. Otherwise the rebase runs against `baseline` as if the
 *  attempt never happened. Returns the list to send, the baseline to keep,
 *  and per row its new `orig` and `sent` (null: the row is already gone
 *  from the stored list and leaves the editor). */
export function rebaseNamedSave<T extends { name: string }>(
  rows: readonly (T & { orig?: string; sent?: number })[],
  baseline: readonly T[],
  live: readonly T[],
  sent: readonly T[] | null,
): { list: T[]; baseline: T[];
     rows: ({ orig?: string; sent?: number } | null)[] } {
  const landed = !!sent && assetListLanded(sent, live);
  const base = landed ? live : baseline;
  const liveKeys = nameKeys(live);
  const mine: Record<string, T> = {};
  const keyOf = rows.map((r, i) => {
    const { orig, sent: at, ...rest } = r;
    let k: string | undefined = orig ?? `\u0001added\u0000${i}`;
    if (landed) {
      if (at !== undefined) k = liveKeys[at];
      else if (orig !== undefined) k = undefined;
    }
    if (k !== undefined) mine[k] = rest as unknown as T;
    return k;
  });
  const out = rebaseKeyed(mine, byNameKey(base), byNameKey(live));
  const order = Object.keys(out);
  return {
    list: Object.values(out),
    baseline: [...base],
    rows: rows.map((r, i) => {
      const k = keyOf[i];
      if (k === undefined) return null;
      const at = order.indexOf(k);
      const orig = landed ? (k.startsWith("\u0001") ? undefined : k) : r.orig;
      return { orig, sent: at < 0 ? undefined : at };
    }),
  };
}

/** The list of device tokens still to revoke, after this sign-out's
 *  revoke did not reach its server: the token joins it once, and the list
 *  keeps only tokens the server could still honour (a device token lives
 *  90 days at most) and the newest few, so it stays small. */
export const UNREVOKED_MAX = 5;
export function queueUnrevoked<T extends { token: string; at: number }>(
  list: readonly T[], add: T | null, nowMs: number,
): T[] {
  const live = list.filter((x) => nowMs - x.at < 90 * 86_400_000
                                  && x.token !== add?.token);
  return [...live, ...(add ? [add] : [])].slice(-UNREVOKED_MAX);
}

/** A failed bank-link status poll worth trying again. The poll is what
 *  makes the server exchange the bank's token, so one lost request must
 *  not end it: no answer at all (offline, the connection dropped while
 *  the bank's own app was open, the request cut short), an unreadable
 *  2xx (a Wi-Fi sign-in page), a server or gateway error (a restart, an
 *  edge timeout while the first sync runs), a request timeout or a rate
 *  limit all keep polling. Any other 4xx, a sign-out among them, is
 *  final. */
export function linkPollMayRetry(e: unknown): boolean {
  const status = (e as { status?: unknown } | null)?.status;
  if (typeof status !== "number") return true;
  return status < 400 || status >= 500 || status === 408 || status === 429;
}

/** True when the household has no checking account the cash runway can
 *  start from (card-only or savings-only). The server says so outright;
 *  an older payload is read by its null checking balance. Cash ahead is
 *  then unknown, not $0, so no "short" or "tight" line may be drawn from
 *  it. (web: Today.tsx noChecking) */
export function noCheckingLinked(
  runway: { checking: number | null; no_checking?: boolean } | null
    | undefined,
  forecast: { no_checking?: boolean } | null | undefined,
): boolean {
  return !!forecast?.no_checking || !!runway?.no_checking
    || (!!runway && runway.checking == null);
}

/** What a bank saved but not yet pulled says on its connection row and in
 *  the link's own result line — the web's FIRST_SYNC_RUNNING
 *  (webapp/src/components/ConnectHub.tsx), word for word. */
export const FIRST_SYNC_RUNNING =
  "first sync running — accounts appear in a minute";

/** A live Plaid connection whose first pull has not landed yet: no
 *  last_ok and nothing wrong with it (the web's firstSyncPending). */
export function firstSyncPending(c: {
  aggregator: string; last_ok: string | null; status: string | null;
  status_kind?: string }): boolean {
  if (c.aggregator !== "plaid" || c.last_ok) return false;
  const kind = c.status_kind ?? "ok";
  return kind === "ok" && !(c.status || "").startsWith("error")
    && c.status !== "reaped";
}

/** The line a successful add earns: "Linked Chase ✓", and while its
 *  first pull is queued on the worker, what happens next. */
export function linkedLine(institution: string | undefined,
                           firstSync?: string): string {
  return `Linked ${institution || "bank"} ✓`
    + (firstSync === "pending" ? ` — ${FIRST_SYNC_RUNNING}` : "");
}

/** What the in-app browser's answer says about the bank sign-in window.
 *  iOS resolves only when the window closes ("cancel"/"dismiss"), so that
 *  answer IS the close. Android resolves "opened" the moment the Custom
 *  Tab starts, while the person is still signing in: that is no close at
 *  all, and the close must come from the app returning to the foreground.
 *  A close learned that way is only a guess (the person may have switched
 *  apps mid-OAuth), so it never sets the session aside: the server's own
 *  second look decides whether the link was abandoned. */
export function linkWindowAnswer(type: string | undefined):
    { closed: boolean; mayReleaseSlot: boolean } {
  if (type === "opened") return { closed: false, mayReleaseSlot: false };
  return { closed: true, mayReleaseSlot: true };
}

/** The bucket a business cost shows on the Books control: the one the
 *  server's P&L counts it in (a cost dated before the business started is
 *  start-up there), else the stored class, else operating. A control that
 *  showed "Operating" for a start-up cost could not send Operating: the
 *  choice would already be selected. */
export function bizShownBucket(
  t: { bucket: string | null; effective_bucket?: string | null },
): string {
  return t.effective_bucket || t.bucket || "operating";
}

/** Whether a Books row still needs a Schedule C line: a cost given an
 *  operating bucket and no line. The server keeps such a row in review,
 *  and the Books tab is where it was bucketed, so the line is offered
 *  there too. Start-up and organizational costs are capitalized, not
 *  filed on a line, and a row with no class at all is the review
 *  queue's. */
export function bizNeedsLine(
  t: { amount: number; bucket: string | null; sched_c_line: string | null },
): boolean {
  return t.amount > 0 && t.bucket === "operating" && !t.sched_c_line;
}

/** A typed number for a field that is not money (a tax rate, a floor
 *  area, an ownership share, miles). Blank is null, the field cleared;
 *  "1,200" reads as 1200. Anything else that is not a plain number
 *  ("22,5", "22%") is undefined, refused: sent as Number() it would be
 *  NaN, which JSON writes as null, and the server takes null as "clear"
 *  and wipes the stored value. */
export function numberDraft(v: string): number | null | undefined {
  const t = (v ?? "").trim();
  if (!t) return null;
  const plain = /^\d{1,3}(,\d{3})+(\.\d+)?$/.test(t) ? t.replace(/,/g, "") : t;
  if (!/^-?(\d+\.?\d*|\.\d+)$/.test(plain)) return undefined;
  const n = Number(plain);
  return Number.isFinite(n) ? n : undefined;
}

/** The number fields a save sends: only those whose text differs from
 *  what the form was loaded with, so saving one field never writes back
 *  a stale copy of another that changed elsewhere since. Throws, naming
 *  the field, when its text is not a number (see numberDraft). */
export function numberFieldsPatch(
  fields: Record<string, { draft: string; loaded: string; label: string }>,
): Record<string, number | null> {
  const out: Record<string, number | null> = {};
  for (const [key, f] of Object.entries(fields)) {
    if (f.draft.trim() === f.loaded.trim()) continue;
    const n = numberDraft(f.draft);
    if (n === undefined) throw new Error(`The ${f.label} is not a number`);
    out[key] = n;
  }
  return out;
}

/** Whether a refused member edit or removal means the server has no such
 *  door (an older server): an unknown path answers the framework's bare
 *  "Not Found", a known path asked with another method 405. A 404 that
 *  names the member is the member already gone, not a missing door. */
export function bizMemberDoorMissing(
  status: number | null | undefined, detail: string | null | undefined,
): boolean {
  return status === 405
    || (status === 404 && (detail ?? "").trim() === "Not Found");
}

/** Whether a file in the app cache is one this app downloaded or wrote
 *  for the share sheet: the full-data export, the database dump
 *  (.sql.gz), CSVs and the spreadsheet (.xlsx) the transactions list
 *  downloads, the year-end ZIP, the continuity PDF, the compliance
 *  calendar, receipt images, the diagnostic bundle (.json), the
 *  assistant's answer report (.html — it carries household figures, so it
 *  must not outlive a sign-out or an account switch) and the
 *  passphrase-sealed connections bundle (.oikx). The platform's own subdirectories are not ours to delete;
 *  the pickers' scratch folders are swept on their own (PICKER_SCRATCH). */
export const isSharedDownload = (name: string): boolean =>
  /\.(zip|csv|xlsx|sql|gz|pdf|ics|jpe?g|png|webp|oikx|json|html)$/i.test(name);

/** The folders in the app cache the photo and document pickers copy a
 *  picked file into: a photographed check (its routing and account
 *  number), a tax form, a statement. They are the household's own
 *  documents, so they go once uploaded and at sign-out with the rest. */
export const PICKER_SCRATCH = ["ImagePicker/", "DocumentPicker/"] as const;

/** Whether `uri` is a picked file's copy in one of those folders. */
export const isPickerScratch = (uri: string, cacheDir: string | null):
    boolean =>
  !!cacheDir && PICKER_SCRATCH.some((d) => uri.startsWith(cacheDir + d));

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

/** The local day a timestamp fell on, as YYYY-MM-DD; the text itself when
 *  it is not a time JS can read, so a date-only value still prints. */
export function localDayOf(ts: string): string {
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? ts : localYmd(d);
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
 *  browser reports, not where anyone is.
 *
 *  Not while the account still has to enrol a second factor either: the
 *  server refuses every settings write until it has one, so an attempt
 *  then is spent on a certain refusal. Answering false keeps the attempt
 *  unspent, and the /api/me that follows enrolment says yes. */
export function adoptsDeviceZone(m: {
  role: string; demo?: boolean; hosted?: boolean; needs_2fa?: boolean;
  timezone?: string; timezone_source?: string;
} | undefined, device: string): boolean {
  if (!m || m.role !== "owner" || m.demo || !m.hosted) return false;
  if (m.needs_2fa) return false;
  if (m.timezone_source !== "instance") return false;
  if (!device || device === m.timezone) return false;
  return !/^(Etc\/|UTC$|GMT$|Universal$|Zulu$)/.test(device);
}

/** The zone to adopt now, or null. Only an /api/me answer the network
 *  just gave counts: the phone also restores last session's answer from
 *  disk, and a household zone chosen on another device since then is not
 *  in it — adopting off that snapshot would overwrite the choice. */
export function zoneToAdopt(fresh: Parameters<typeof adoptsDeviceZone>[0] | null,
                            device: string): string | null {
  return fresh && adoptsDeviceZone(fresh, device) ? device : null;
}

/** After a failed adopt, whether the next fresh /api/me may try again.
 *  A refusal (any 4xx — a demo, a zone already chosen) is the server's
 *  answer and is final; a dropped connection or a server error is
 *  retried, a few times per launch, so a flaky first load does not leave
 *  the household on the instance clock until the next cold start. */
export const ZONE_ADOPT_TRIES = 3;
export function retriesZoneAdopt(status: number, attempts: number): boolean {
  if (status >= 400 && status < 500) return false;
  return attempts < ZONE_ADOPT_TRIES;
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

/** The theme an owner's account asks for, from the stored settings value
 *  (the web's reconcile in App.tsx, same rule). Absent means never chosen,
 *  which follows the phone's own setting ("auto"); an explicit light or
 *  dark pins. Anything else is not a theme this build knows, and changes
 *  nothing. */
export function accountTheme(
    stored: unknown): "auto" | "light" | "dark" | null {
  if (stored === undefined || stored === null || stored === "")
    return "auto";
  return stored === "auto" || stored === "light" || stored === "dark"
    ? stored : null;
}

// Bill cadence words and the edit picker's option list. The web keeps an
// identical copy in webapp/src/cadence.ts; the pure-logic suite runs both
// and checks the two are the same text.
/** A FREQ:interval cadence value in the words the bills list uses for it
 *  ("monthly", "every 4 months", "envelope (annual pool)") — the server's
 *  phrasing, so a bill outside the standard list reads the same in the edit
 *  picker as it does in the row it was opened from. */
export function cadenceWords(value: string): string {
  const [freq, raw] = value.toUpperCase().split(":");
  const n = parseInt(raw ?? "1", 10) || 1;
  if (freq === "ENVELOPE")
    return n >= 12 ? "envelope (annual pool)" : "envelope (monthly pool)";
  if (!freq || freq === "ONE_TIME") return "one-time";
  const unit: Record<string, [string, string]> = {
    DAILY: ["daily", "day"], WEEKLY: ["weekly", "week"],
    MONTHLY: ["monthly", "month"], YEARLY: ["yearly", "year"] };
  const u = unit[freq];
  if (!u) return value;
  return n === 1 ? u[0] : `every ${n} ${u[1]}s`;
}

/** The edit picker's options: the server's list, then every cadence the
 *  bill must still be able to land on that the list lacks — the bill's own
 *  saved interval (an "every 4 months" bill), and a ledger-fit prefill or
 *  the current pick. Without the saved one, touching the picker once loses
 *  the bill's real cadence for good. */
export function cadenceChoices(list: [string, string][],
                               keep: (string | null | undefined)[]
                               ): [string, string][] {
  const out = [...list];
  for (const v of keep) {
    if (v && !out.some(([k]) => k === v)) out.push([v, cadenceWords(v)]);
  }
  return out;
}

/** The ADD pickers' short list: the common cadences, one-time, and the
 *  envelopes. Anything unusual (every 2 weeks, every 14 days, every 3
 *  months) is one "Custom interval…" pick away — see CUSTOM_UNITS. The
 *  values are the server's FREQ:interval wire format, never the words. */
export const ADD_CADENCES: [string, string][] = [
  ["DAILY:1", "daily"], ["WEEKLY:1", "weekly"], ["MONTHLY:1", "monthly"],
  ["YEARLY:1", "yearly"], ["ONE_TIME:1", "one-time"],
  ["ENVELOPE:1", "envelope (monthly pool)"],
  ["ENVELOPE:12", "envelope (annual pool)"],
];

/** "Custom interval…" units. Days and weeks are here because a paycheck
 *  every 2 weeks or a bill every 14 days is common, and months/years alone
 *  could not express either. */
export const CUSTOM_UNITS: [string, string][] = [
  ["DAILY", "day(s)"], ["WEEKLY", "week(s)"], ["MONTHLY", "month(s)"],
  ["YEARLY", "year(s)"],
];

/** The largest custom count the server takes for each unit: about 20
 *  years of that unit, because the server bounds an interval by how long
 *  one cycle lasts, not by the bare number. A bill saved past it is
 *  refused, so the picker never sends one. */
export const CUSTOM_MAX: Record<string, number> = {
  DAILY: 7300, WEEKLY: 1043, MONTHLY: 240, YEARLY: 20,
};

/** The cap for one unit. A unit the table does not know gets the smallest
 *  cap, so a guess can never send a count the server refuses. */
export function customMax(unit: string): number {
  return CUSTOM_MAX[unit.toUpperCase()] ?? 20;
}

/** The cadence a custom interval sends: a blank or nonsense count is 1,
 *  and a count past the unit's cap is the cap. */
export function customCadence(unit: string, n: string): string {
  return `${unit}:${Math.min(customMax(unit), Math.max(1, parseInt(n, 10) || 1))}`;
}

// ---- the shared demonstration login: one owner login whose password is
// handed out, so every holder signs in as the same owner. The server refuses it
// every door that changes the login itself or grants something that
// outlives it; these are the sentences those refusals carry, so the
// control that is switched off says what the 403 would have said. ----
export const SHARED_LOGIN_NOTE = "This is a shared demonstration login — "
  + "its password, email, sign-in factors and sessions can't be changed.";
export const SHARED_GRANT_NOTE = "This is a shared demonstration login — "
  + "it can't add people, script tokens, support access or new outbound "
  + "destinations to this household.";
export const SHARED_CREDENTIAL_NOTE = "This is a shared demonstration login — "
  + "it can't connect, disconnect or reconnect a bank or exchange, save "
  + "aggregator keys or take the credential bundle, since those outlive "
  + "the login.";

/** Why a control is switched off for the shared demonstration login, or
 *  null when it is live. `login` is a change to the login's own posture
 *  (password, email, factors, sessions, the account itself); `grant` is a
 *  principal or channel that would outlive it (a member, a token, a hook,
 *  support access, a new mail or AI destination). A demo instance answers
 *  null: it already greys the whole screen under its own banner, and two
 *  notes on one card would say the same thing twice. Unknown answers
 *  null too, like useDemo — the common login is not this one, and every
 *  such control is also owner-gated, which fails closed on the same
 *  query. */
export function sharedLock(
  me: { demo?: boolean; shared_login?: boolean } | undefined,
  kind: "login" | "grant" | "credential",
): string | null {
  if (!me || me.demo || !me.shared_login) return null;
  return kind === "login" ? SHARED_LOGIN_NOTE
    : kind === "credential" ? SHARED_CREDENTIAL_NOTE : SHARED_GRANT_NOTE;
}

/** The body one edit of the daily-mail list posts: the field it changed
 *  and nothing else. Every other field rides from a copy of the settings
 *  loaded earlier, and the server stores whatever it is sent — so a
 *  schedule or a mute list that tagged along would put back a cadence
 *  changed on another device, or a member's own "turn off for me". A mute
 *  or a recipient edit names only its own address; the server applies it
 *  to the stored list. */
export function recipientEditBody(e:
  | { kind: "mute"; email: string; on: boolean; muted: string[] }
  | { kind: "add" | "remove"; email: string; recipients: string[] },
): Record<string, unknown> {
  // The one-address delta is what a current server applies, so a toggle
  // or an edit made from this phone's cached copy of a list can never
  // drop or un-mute an address another device changed since. The whole
  // list from that copy rides along only because a server older than the
  // delta keys knows nothing else; a current server ignores it when a
  // delta is present.
  if (e.kind === "mute") {
    const low = e.email.toLowerCase();
    const rest = e.muted.map((x) => x.toLowerCase()).filter((x) => x !== low);
    return e.on
      ? { email_muted_add: [low], email_muted: [...new Set([...rest, low])] }
      : { email_muted_remove: [low], email_muted: [...new Set(rest)] };
  }
  const low = e.email.toLowerCase();
  const rest = e.recipients.filter((x) => x.toLowerCase() !== low);
  return e.kind === "add"
    ? { email_recipients_added: [e.email],
        email_recipients: [...rest, e.email] }
    : { email_recipients_removed: [e.email], email_recipients: rest };
}

// ---- the recipient add box and the Resend reply ----
// The web keeps the same rules in webapp/src/recipients.ts; the pure-logic
// suite runs every case against both copies.

/** Is this shaped like one mailbox? Never stricter than the server, which
 *  accepts a dotless domain on purpose: a LAN relay's single-label domain
 *  is a real mailbox. The server has the final say. */
export function looksLikeMailbox(v: string): boolean {
  return /^[^\s@]+@[^\s@.]+(\.[^\s@.]+)*$/.test(v);
}

/** Why the add box will not add this address, which is already on the
 *  list, or null when it is new. The owner is mailed by construction and
 *  never invited to their own household's email. */
export function recipientDuplicate(
  v: string, recipients: string[], ownerEmail: string | null | undefined,
): string | null {
  const low = v.toLowerCase();
  if (ownerEmail && ownerEmail.toLowerCase() === low)
    return "That's you — you're already on the list.";
  if (recipients.some((r) => r.toLowerCase() === low))
    return "Already on the list.";
  return null;
}

/** What a member's own daily-email card says about their inbox. The
 *  worker's rule is not "on and not muted": once the owner saves a list,
 *  only the owner and the addresses that ACCEPTED their invitation are
 *  mailed; with no list, every verified user is (on self-host, everyone,
 *  until anyone has verified). So the sentence is worked out from the
 *  same facts. `recipients`/`status` come from the settings view and are
 *  undefined until it loads; `needBaseUrl` from /api/notify, absent on an
 *  older server. */
export function myDailyLine(x: {
  me: string; on: boolean; muted: boolean;
  prefs?: Partial<Record<string, boolean>> | null;
  verified?: boolean | null; hosted?: boolean;
  recipients?: string[] | null;
  status?: { email: string; verified?: boolean | null;
             invite?: string | null; delivery?: string | null }[] | null;
  needBaseUrl?: boolean;
}): { receiving: boolean; text: string } {
  const no = (text: string) => ({ receiving: false, text });
  if (x.muted) return no("You've muted all household mail for yourself.");
  if (!x.on) return no("Off.");
  if (x.prefs?.daily === false) return no("You've turned the daily email off for yourself.");
  const low = x.me.toLowerCase();
  const row = x.status?.find((r) => r.email.toLowerCase() === low);
  if (row?.delivery === "bouncing" || row?.delivery === "complained")
    return no("Held — mail to your address isn't being delivered.");
  if (x.status && x.recipients?.length) {
    if (!row) return no("The owner's mail list doesn't include you.");
    if (row.invite !== "accepted") {
      if (x.needBaseUrl)
        return no("Your invitation can't be sent until the server has a "
          + "public address (OIKONOME_BASE_URL).");
      if (row.invite === "invited")
        return no("Accept the invitation in your inbox to start receiving it.");
      if (row.invite === "declined")
        return no("You declined the invitation — ask the owner to send another.");
      return no("You haven't accepted an invitation yet — ask the owner to "
        + "send one.");
    }
  } else if (x.verified === false
             && (x.hosted || x.status?.some((r) => r.verified === true))) {
    return no("Confirm your email address to receive it.");
  }
  return { receiving: true, text: "You receive it." };
}

/** What the add box says instead of adding, when this server cannot mail
 *  the invitation an added address needs before it is ever mailed. */
export const NEED_BASE_URL =
  "Set OIKONOME_BASE_URL on the server before adding recipients.";

/** Why Resend sent nothing, or null when it sent. The server answers ok
 *  even when it held the mail back, and names why in `reason`; a reply
 *  without `sent` comes from a server that always sent. */
export function resendHeld(
  r: { sent?: boolean | null; reason?: string | null } | null | undefined,
  email: string,
): string | null {
  if (r?.sent !== false) return null;
  if (r.reason === "cooldown")
    return `An invitation went to ${email} a few minutes ago — try again `
      + "shortly.";
  if (r.reason === "cap")
    return "Too many invitations sent for now — try again later.";
  if (r.reason === "no_mail")
    return `The invitation to ${email} was not sent: this instance has no `
      + "way to mail it (no relay, or no OIKONOME_BASE_URL to build the "
      + "link under). Their previous link, if any, still works.";
  if (r.reason === "undelivered")
    return `The mail relay refused the invitation to ${email}. Their `
      + "previous link, if any, still works.";
  if (r.reason === "error")
    return `The invitation to ${email} could not be sent right now — try `
      + "again shortly.";
  return `The invitation to ${email} was not sent.`;
}

// What the bill screen says after Enable/Disable: the state the server
// settled on, worded as the web's toast words it, so the person sees the
// outcome rather than being returned to the list without a word.
export function billToggleNote(disabled: boolean): string {
  return disabled ? "Disabled — excluded from the budget."
                  : "Enabled — counts toward the budget.";
}

// A bill that missed its date reads one of two ways. Usually it is overdue
// (red). But a bank feed can withdraw a charge it already posted, and a
// bill that WAS charged then looks unpaid — the server marks those with the
// date the charge was withdrawn, and the honest word is a warning, not a
// missed payment. Same words as the web Bills row and the Today footnote.
export function missedBillPill(withdrawnOn: string | null | undefined): {
  text: string; tone: "warn" | "bad"; note: string | null;
} {
  if (!withdrawnOn || withdrawnOn.length < 10) {
    return { text: "overdue", tone: "bad", note: null };
  }
  const [y, m, d] = withdrawnOn.slice(0, 10).split("-");
  return { text: "withdrawn", tone: "warn",
           note: `charge withdrawn by the bank feed (${m}/${d}/${y.slice(2)})` };
}

/** Interest a plan saves against paying only the minimums, or 0 when there
 *  is no honest figure. A baseline that never pays off stops at the
 *  simulation horizon, so its interest is a truncation total, not a cost. */
export function interestSaved(
  base: { debt_free: string | null; total_interest: number },
  plan: { total_interest: number },
): number {
  return base.debt_free ? base.total_interest - plan.total_interest : 0;
}

// Which 401 replies are a step-up refusal rather than a lost session. A
// wrong password, a missing or spent one-time code, or a refused passkey on
// an in-app form is a 401 too, and it means "try again", not "your session
// is gone" — treating it as gone wipes the device token and drops to login.
// "one-time code" rather than "bad one-time": the server also says "current
// one-time code required". A code the authenticator app is still showing
// after it was spent answers "code_used: that code has already been used",
// which names neither. The web keeps the same list in webapp/src/reauth.ts;
// the pure-logic suite runs both copies.
const REAUTH = /totp_required|recovery_required|password_required|code_used|already been used|wrong password|one-time code|bad credentials|passkey/i;

/** The server's own marker on a step-up refusal: `error: "reauth"`, at the
 *  top of the body or inside FastAPI's `detail` wrapper. Read before the
 *  wording, which an older server is the only reason to keep. */
function hasReauthMarker(body: unknown): boolean {
  if (typeof body === "string") {
    try { body = JSON.parse(body); } catch { return false; }
  }
  if (!body || typeof body !== "object") return false;
  const b = body as { error?: unknown; detail?: { error?: unknown } | unknown };
  if (b.error === "reauth") return true;
  const d = b.detail;
  return !!d && typeof d === "object"
    && (d as { error?: unknown }).error === "reauth";
}

/** Is this 401 a re-auth refusal the form shows, rather than a session
 *  that is gone? `detail` is the reply's sentence; `body` the parsed reply,
 *  checked for the marker first. */
export function isReauthReply(detail: string, body?: unknown): boolean {
  return hasReauthMarker(body) || hasReauthMarker(detail)
    || REAUTH.test(detail);
}

/** A month's net saved figure as the cash-flow footer prints it: a "+"
 *  only on a gain, so the best month of an all-negative range reads
 *  "-$40" in the loss colour rather than "+-$40" in the gain one. */
export function savedFigure(n: number): { text: string;
                                          tone: "good" | "bad" | "plain" } {
  const r = pyround(n);
  if (r > 0) return { text: "+" + money(n, false), tone: "good" };
  return { text: money(n, false), tone: r < 0 ? "bad" : "plain" };
}

/** When a tapped notification was sent. expo-notifications reports the
 *  date in seconds on iOS and milliseconds on Android, so a value too
 *  small to be milliseconds since 2001 is read as seconds; anything that
 *  still lands outside a sane window (unset, garbage, far future) falls
 *  back to the moment of the tap. */
export function pushSentAt(raw: unknown, now: Date = new Date()): Date {
  if (typeof raw !== "number" || !Number.isFinite(raw) || raw <= 0) return now;
  const ms = raw < 1e12 ? raw * 1000 : raw;
  const lo = Date.UTC(2020, 0, 1), hi = now.getTime() + 86_400_000;
  return ms >= lo && ms <= hi ? new Date(ms) : now;
}

/** The year and month an instant falls in on the household's clock. The
 *  server dates a report by the household's day, so a phone in another
 *  zone (or travelling) must not read the instant on its own clock — late
 *  on the 31st in the household is already the 1st somewhere east. An
 *  unknown or missing zone falls back to the device's. */
export function zonedYearMonth(at: Date, zone?: string): { y: number; m: number } {
  if (zone) {
    try {
      const parts = new Intl.DateTimeFormat("en-US", {
        timeZone: zone, year: "numeric", month: "numeric",
      }).formatToParts(at);
      const y = Number(parts.find((p) => p.type === "year")?.value);
      const m = Number(parts.find((p) => p.type === "month")?.value);
      if (Number.isInteger(y) && Number.isInteger(m)) return { y, m };
    } catch { /* an unknown zone name: read the device clock instead */ }
  }
  return { y: at.getFullYear(), m: at.getMonth() + 1 };
}

/** Where tapping a push lands. The monthly report goes out on the 1st and
 *  the yearly one on Jan 1, and both describe the period that just ENDED —
 *  opening the bare Month or Year screen would show the period that began a
 *  few hours ago, nearly empty. A payload that names the period (`y`, `m`)
 *  wins; otherwise the period is the one before the day the push was sent,
 *  read on the household's clock (`zone`) — the same rule the report
 *  itself uses — so a tap days later, or from another zone, still opens
 *  the report the push announced. */
export function pushRoute(event: string,
                          data: Record<string, unknown> | undefined,
                          sentAt: Date, zone?: string): string {
  const int = (v: unknown, lo: number, hi: number): number | null => {
    const n = typeof v === "number" ? v : typeof v === "string" && v.trim()
      ? Number(v) : NaN;
    return Number.isInteger(n) && n >= lo && n <= hi ? n : null;
  };
  if (event === "alert") return "/alerts";
  if (event === "monthly") {
    let y = int(data?.y, 1900, 2100), m = int(data?.m, 1, 12);
    if (y === null || m === null) {
      const sent = zonedYearMonth(sentAt, zone);
      y = sent.m === 1 ? sent.y - 1 : sent.y;
      m = sent.m === 1 ? 12 : sent.m - 1;
    }
    return `/month?y=${y}&m=${m}`;
  }
  if (event === "yearly") {
    const y = int(data?.y, 1900, 2100) ?? zonedYearMonth(sentAt, zone).y - 1;
    return `/year?y=${y}`;
  }
  return "/";
}

// ---- who receives which kind of email ----
// The web keeps the same rules in webapp/src/recipients.ts; the pure-logic
// suite runs every case against both copies. The kinds are the server's
// (notify/email_prefs.py), in the grid's column order.
export const EMAIL_KINDS = ["daily", "monthly", "yearly", "alerts"] as const;
export const EMAIL_KIND_LABEL: Record<string, string> = {
  daily: "Daily", monthly: "Monthly", yearly: "Yearly", alerts: "Alerts",
};

/** Whether this address receives this kind; a row from a server that
 *  predates the grid carries no prefs, and that means all of them. */
export function prefOn(
  prefs: Partial<Record<string, boolean>> | null | undefined, kind: string,
): boolean {
  return prefs?.[kind] !== false;
}

/** Why a column is dimmed: the household does not send that kind at all
 *  right now, so the boxes under it decide nothing until it is on.
 *  Alerts have no schedule and are never dimmed. */
export function kindOffNote(
  kind: string, sending: Partial<Record<string, boolean>> | null | undefined,
): string | null {
  if (kind === "alerts" || sending?.[kind]) return null;
  return kind === "daily" ? "daily email is off"
    : kind === "yearly" ? "year in review is off"
    : `${kind} report is off`;
}

/** The recipient rows with one box flipped — the cache patch a tap makes
 *  before the server's reply settles it. Matches the address the way the
 *  server does, ignoring case. */
export function withPref<T extends { email: string;
                                     prefs?: Partial<Record<string, boolean>> }>(
  rows: T[], email: string, kind: string, on: boolean,
): T[] {
  const low = email.toLowerCase();
  return rows.map((r) => r.email.toLowerCase() === low
    ? { ...r, prefs: { ...r.prefs, [kind]: on } } : r);
}

/** A whole number from a route param, or null when it is missing, not a
 *  plain integer, or outside lo..hi. Route params arrive from deep links
 *  anyone can write (`oikonome://transactions?m=13`), and a screen that
 *  indexes the month names with one renders nothing at all — so every
 *  numeric param is read through this, never through Number(). A repeated
 *  param reaches the screen as a list; its first value counts. */
export function routeInt(v: unknown, lo: number, hi: number): number | null {
  const raw = Array.isArray(v) ? v[0] : v;
  const s = typeof raw === "number" ? String(raw) : raw;
  if (typeof s !== "string" || !/^\s*-?\d+\s*$/.test(s)) return null;
  const n = Number(s);
  return n >= lo && n <= hi ? n : null;
}

/** The year and month a screen opens on: the route's own, each falling
 *  back to the current one when absent or out of range. */
export function routeYm(
  y: unknown, m: unknown, now: Date,
): { y: number; m: number } {
  return { y: routeInt(y, 1900, 2100) ?? now.getFullYear(),
           m: routeInt(m, 1, 12) ?? now.getMonth() + 1 };
}

/** The sentence a credential change adds when it paused the household's
 *  text number (the reply's `sms_number_needs_verify`), or "" when it
 *  did not. The web twin is smsPauseNote in webapp/src/api/cache.ts. */
export function smsPauseSentence(
  r: { sms_number_needs_verify?: boolean } | null | undefined,
): string {
  return r?.sms_number_needs_verify
    ? " Texts to the household's number are paused until you confirm it "
      + "again (More → Email & Push)."
    : "";
}


// ---- bulk actions: one selection, sent in one request ---------------------
// The server applies up to 10,000 ids in one bulk write, all or nothing, so
// a selection up to that size goes in ONE request: split, a failure part-way
// would leave the first pieces applied and the rest not. A selection past
// BULK_MAX is not offered at all: a write that size is a rule, not a
// selection (webapp/src/components/TxnTable.tsx does the same). BULK_CHUNK
// stays the server's cap, so only what one request could never carry is
// split.
export const BULK_MAX = 10000;
export const BULK_CHUNK = BULK_MAX;
export function chunkIds<T>(ids: readonly T[], size: number = BULK_CHUNK): T[][] {
  const n = Math.max(1, Math.floor(size));
  const out: T[][] = [];
  for (let i = 0; i < ids.length; i += n) out.push(ids.slice(i, i + n));
  return out;
}
export type BulkTally = { applied: number; missing: number; flow: number };
// Sends each piece in turn and adds up what the server reports. A piece
// that fails stops the walk, and the error says how many were already
// written, because the pieces before it stay applied.
export async function runChunked(
    ids: readonly string[],
    send: (piece: string[]) => Promise<Partial<BulkTally>>,
    onProgress?: (done: number, total: number) => void,
    size: number = BULK_CHUNK): Promise<BulkTally> {
  const tally: BulkTally = { applied: 0, missing: 0, flow: 0 };
  let done = 0;
  onProgress?.(0, ids.length);
  for (const piece of chunkIds(ids, size)) {
    let r: Partial<BulkTally>;
    try {
      r = await send(piece);
    } catch (e) {
      if (done === 0) throw e;
      const why = e instanceof Error ? e.message : String(e);
      throw new Error(`stopped after ${done} of ${ids.length} — ${why}`);
    }
    tally.applied += r.applied ?? 0;
    tally.missing += r.missing ?? 0;
    tally.flow += r.flow ?? 0;
    done += piece.length;
    onProgress?.(done, ids.length);
  }
  return tally;
}

/** The heading a windowed report prints before its total. A household
 *  whose ledger starts this month has no complete month for a range of
 *  months to hold, so the server shows the month so far instead and says
 *  `live_month` — the heading names that, so a partial month never reads
 *  as the whole range the picker promised. */
export const windowLabel = (w: { label: string; live_month?: boolean }) =>
  w.live_month ? `${w.label} so far` : w.label;

/** The search list's "Load more" pile, kept as one slot per page so a
 *  later edit can reach it. Each slot is refreshed from that page's cached
 *  response whenever one exists: a single-row edit patches (or drops the
 *  row from) every cached ["transactions"] page, so re-reading them is how
 *  the change lands on rows already shown. A page the cache has since
 *  evicted keeps the copy last seen rather than vanishing from the list.
 *
 *  An edit can also shift the server's pages: a row on page 1 that stops
 *  matching pulls the first row of page 2 onto page 1, and the refetched
 *  page 2 starts one row later while the cached page 1 is still the old
 *  copy. That row would then be in no slot and silently skipped for the
 *  rest of the session, so rows the previous copy of the slot held ahead
 *  of the new first row are kept, unless they are shown elsewhere already
 *  or `removed` says the row is gone (retired, deleted). */
export function mergePilePages<T extends { id: string }>(
  prev: T[][], page: number, incoming: T[],
  cached: (page: number) => T[] | undefined, reset: boolean,
  removed?: ReadonlySet<string>,
): T[][] {
  if (reset) return [incoming];
  const next = prev.slice();
  for (let i = 0; i < next.length; i++) {
    if (i === page - 1) continue;
    const c = cached(i + 1);
    if (c) next[i] = c;
    else if (!next[i]) next[i] = [];
  }
  const old = prev[page - 1] ?? [];
  const inNew = new Set(incoming.map((r) => r.id));
  const firstKept = old.findIndex((r) => inNew.has(r.id));
  const elsewhere = new Set<string>();
  next.forEach((rows, i) => {
    if (i !== page - 1) for (const r of rows ?? []) elsewhere.add(r.id);
  });
  const carried = (firstKept < 0 ? [] : old.slice(0, firstKept)).filter(
    (r) => !elsewhere.has(r.id) && !removed?.has(r.id));
  next[page - 1] = carried.length ? [...carried, ...incoming] : incoming;
  return next;
}

/** Ids of rows this session retired or deleted, so the search pile
 *  never carries one back (see mergePilePages). */
export const goneTxnIds = new Set<string>();

/** The pages in order, each row once (a row can shift across a page
 *  boundary between fetches; the first copy wins). */
export function flattenPile<T extends { id: string }>(pages: T[][]): T[] {
  const seen = new Set<string>();
  const out: T[] = [];
  for (const rows of pages) {
    for (const r of rows ?? []) {
      if (seen.has(r.id)) continue;
      seen.add(r.id);
      out.push(r);
    }
  }
  return out;
}

/** A folder import runs as a background job on the server: the run door
 *  answers with the job at once, and the screen polls it until it
 *  settles. Same shape the web app reads. */
export interface BulkJobFile {
  index: number; name: string;
  state: "pending" | "running" | "done" | "failed" | "skipped";
  ok?: boolean; imported?: number; error?: string;
  account_id?: string | null;
  warnings?: string[];
  corrected?: number;
  // an export ZIP in the folder restores on its own background job
  restore_started?: boolean;
}
export interface BulkJob {
  job_id: string;
  state: "queued" | "running" | "cancelling" | "cancelled" | "stopped"
    | "done" | "error";
  stop_reason?: string | null;
  total: number; done: number;
  started_at?: string | null; updated_at?: string | null;
  files: BulkJobFile[];
  restore_started?: boolean;
  // no word from the worker for a while — waiting, not failing
  waiting?: boolean; note?: string;
}
export type BulkJobStatus = BulkJob | { state: "idle" };

/** Still going: queued, running, or finishing its file after a cancel. */
export function bulkJobLive(s?: { state: string } | null): boolean {
  return !!s && (s.state === "queued" || s.state === "running"
                 || s.state === "cancelling");
}

/** The job query's poll: every two seconds while the job is live, and
 *  never once it has settled (done, cancelled, stopped, error) or there
 *  is no job — a settled job does not change again. */
export function bulkJobPoll(s?: { state: string } | null): number | false {
  return bulkJobLive(s) ? 2000 : false;
}

const BULK_STOPPED_WHY: Record<string, string> = {
  signed_out: "whoever started it was signed out",
  role_changed: "whoever started it may no longer import",
  household_deleted: "this household is being deleted",
  locked_out: "this account is locked right now",
  read_only: "this account is read-only right now",
  second_factor: "the account needs a second factor first",
  unchecked: "the account's standing could not be checked",
  expired: "it waited too long for the server",
};

/** The one line that sums a folder import up, live or settled — the
 *  web Import page's words. */
export function bulkJobHeadline(job: BulkJob): string {
  const ok = job.files.filter((f) => f.ok).length;
  const files = (n: number) => `${n} file${n === 1 ? "" : "s"}`;
  if (bulkJobLive(job))
    return `Importing — ${job.done} of ${files(job.total)} done`;
  if (job.state === "cancelled")
    return `Cancelled — ${ok} of ${files(job.total)} imported.`;
  if (job.state === "stopped")
    return `Stopped — ${BULK_STOPPED_WHY[job.stop_reason ?? ""]
      ?? "the import could not go on"}. ${ok} of ${files(job.total)} `
      + "imported; drop the rest again.";
  if (job.state === "error")
    return `The import stopped unexpectedly — ${ok} of ${files(job.total)} `
      + "imported; drop the rest again.";
  return `${ok}/${job.total} imported.`;
}

/** whether a drafted goal has any match words (the comma list, blanks
 *  ignored) */
export function goalHasWords(r: Pick<GoalDraft, "tokens">): boolean {
  return r.tokens.split(",").some((t) => t.trim() !== "");
}

/** A drafted goal with match words but no account. Such a goal can never
 *  be measured — the words match both legs of a transfer and nothing says
 *  which side is the fund — so the server refuses the shape and the
 *  editors prompt for the account the money lands in. The web editor
 *  (SavingsGoalsCard) applies the same rule. */
export function goalNeedsAccount(
  r: Pick<GoalDraft, "tokens" | "account_id">): boolean {
  return !r.account_id && goalHasWords(r);
}

/** the slice of the Settings view the bank-link hold reads */
export type BankLinkHoldView = { outbound_paused?: {
  reason?: string; aggregator?: boolean;
  aggregator_keys?: readonly { provider: string }[] } | null } | undefined;

/** Whether new bank links through `provider` are held. After a credential
 *  rotation the household's own Plaid / MX client may be one a hijacker
 *  saved, so the server refuses a new link, a re-auth and the MX widget on
 *  it until the owner resumes in the Settings banner or removes the keys.
 *  Only the owner is told which clients are held, so only the owner's
 *  buttons close ahead of the server's refusal (a stale screen still gets
 *  the server's sentence). The web mirrors this as bankLinkHoldNote in
 *  ConnectHub.tsx. */
export function bankLinksHeld(s: BankLinkHoldView, provider: "plaid" | "mx"): boolean {
  const p = s?.outbound_paused;
  return !!p?.aggregator
    && (p.aggregator_keys ?? []).some((k) => k.provider === provider);
}

/** The sentence a held bank-link door shows (the server's own wording),
 *  or null when links through `provider` are open. */
export function bankLinkHoldNote(s: BankLinkHoldView,
                                 provider: "plaid" | "mx"): string | null {
  if (!bankLinksHeld(s, provider)) return null;
  const why = s?.outbound_paused?.reason === "recovery"
    ? "your account was recovered"
    : "your password, login email or second factor changed";
  return `New bank links are on hold since ${why}: confirm the `
    + `${provider === "mx" ? "MX" : "Plaid"} keys saved in Settings are `
    + "yours (Settings, top banner), or disconnect their banks and remove "
    + "the keys.";
}

/** The sentence an owner's password, login-address or second-factor
 *  change adds when it removed members who joined in the last days through
 *  the owner's own invites; empty when nobody was removed. The web says
 *  the same on its Settings security cards. */
export function membersRemovedNote(
  r: { members_removed?: readonly { email: string }[] } | null | undefined,
): string {
  const gone = r?.members_removed ?? [];
  if (!gone.length) return "";
  return ` Removed ${gone.length} ${gone.length === 1 ? "member" : "members"}`
    + ` who joined through your recent invites: `
    + `${gone.map((m) => m.email).join(", ")} — invite back the ones that `
    + "are yours.";
}

/** Every YNAB account of a multi-account register has an answer — an
 *  account, a new account with a name ("__new__" plus a name), or "skip" —
 *  and not every one is skipped. The server refuses the same cases, so the
 *  import button only lights when the answer will be taken. */
export function ynabAllAnswered(
  names: readonly string[],
  dest: Readonly<Record<string, string | undefined>>,
  newName: Readonly<Record<string, string | undefined>>,
): boolean {
  const answered = names.every((n) => !!dest[n]
    && (dest[n] !== "__new__" || !!(newName[n] ?? "").trim()));
  return answered && names.some((n) => !!dest[n] && dest[n] !== "skip");
}

/** A folder import's refusal of a YNAB register of several accounts:
 *  that file is finished on its own, where the accounts can be placed. */
export const YNAB_SEVERAL = /covers \d+ YNAB accounts/;

/** The savings goal the budget planner owns. Goal names are unique
 *  case-insensitively on the server, so a goal typed as "savings" in the
 *  Savings goals card IS the plan's Savings row; an exact-spelling match
 *  would miss it, append a second "Savings", and have every planner save
 *  refused as a duplicate. The web client applies the same predicate. */
export const isPlanSavingsGoal = (name: string): boolean =>
  name.trim().toLowerCase() === "savings";

/** One income-scenario row as the editor holds it. `touched` marks a row
 *  the person changed (or added) on this screen. */
export interface ScenarioDraft {
  name: string; take_home: string; cadence: string; touched?: boolean;
}

/**
 * The income_scenarios list to save, given the editor's rows, the list it
 * was seeded from (`baseline`) and the server's list read just before
 * saving (`current`).
 *
 * The server replaces the whole list, so a save built only from the
 * seeded copy would erase whatever another device or member saved since.
 * Invariant: a row edited here wins; an untouched row takes its live
 * server value; an untouched row that the server no longer has (and was
 * in the baseline) was deleted elsewhere and stays deleted; a row the
 * person removed here stays removed; a scenario added elsewhere since the
 * baseline is carried through. Names compare case-insensitively, the
 * same way the server rejects duplicates. The web client applies the same
 * rule; keep the two in step.
 */
export function mergeIncomeScenarios(rows: readonly ScenarioDraft[],
                                     baseline: readonly IncomeScenario[],
                                     current: readonly IncomeScenario[]):
    IncomeScenario[] {
  const key = (n: string) => n.trim().toLowerCase();
  const baseNames = new Set(baseline.map((s) => key(s.name)));
  const curByName = new Map(current.map((s) => [key(s.name), s]));
  const out: IncomeScenario[] = [];
  const has = (n: string) => out.some((o) => key(o.name) === key(n));
  for (const r of rows) {
    const name = r.name.trim();
    if (!name) continue;
    if (!r.touched) {
      const live = curByName.get(key(name));
      if (live) { out.push(live); continue; }
      if (baseNames.has(key(name))) continue;   // deleted elsewhere
    }
    out.push({ name, take_home: Number(r.take_home.replace(/[$,\s]/g, "")),
               cadence: r.cadence });
  }
  for (const s of current)
    if (!baseNames.has(key(s.name)) && !has(s.name))
      out.push(s);                                // added elsewhere since
  return out;
}

/** What the forced second-factor lockout does after asking the server
 *  again. Enrolling a factor on the web signs every OTHER device out —
 *  this phone's token included — so a 401 here is the expected answer to
 *  "I set it up on the web", not a fault: the person is told to sign in
 *  again. Still required → the phone's own Security screen, where an
 *  authenticator can be set up without losing this sign-in. Cleared → the
 *  lockout simply goes away. Anything else (offline, a 5xx) leaves the
 *  lockout up and says so. */
export type TwoFactorRecheck =
  "sign-in-again" | "set-up-here" | "cleared" | "unreachable";
export const SIGN_IN_AGAIN = "Sign in again to continue";
export function twoFactorRecheck(r: {
  me?: { needs_2fa?: boolean } | null; errorStatus?: number | null;
}): TwoFactorRecheck {
  if (r.errorStatus === 401) return "sign-in-again";
  if (r.errorStatus != null || !r.me) return "unreachable";
  return r.me.needs_2fa ? "set-up-here" : "cleared";
}

/** The planner's line under suggestions taken from under a month of
 *  history: how many days the figures come from, and that they were
 *  scaled to a month. Under the server's settled threshold
 *  (min_rate_days) the rate is a few days' own, so the line says to
 *  expect it to move.
 *  With no finished day of ledger at all it says to come back tomorrow.
 *  null when complete months stand behind the suggestions. Twin:
 *  historyBasisNote in webapp/src/moneymath.ts. */
export function historyBasisNote(s: {
  history_days?: number; history_scaled?: boolean; min_rate_days?: number;
  not_enough_history?: boolean;
} | null | undefined): string | null {
  // not one finished day of ledger: the server offers no suggestion
  if (s?.not_enough_history)
    return "Not enough history yet — come back tomorrow.";
  if (!s || !s.history_scaled || !s.history_days) return null;
  const n = s.history_days;
  const head = `Based on ${n} day${n === 1 ? "" : "s"} of history — scaled to a month`;
  return n < (s.min_rate_days ?? 7)
    ? `${head} — under a week, so expect it to move; revisit after your first full month.`
    : `${head}.`;
}
