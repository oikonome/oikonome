// Dependency-free money and merge logic. This module must import no
// React Native code: mobile has no JS test runner, so plain node
// (`node --experimental-strip-types scripts/pure-logic-tests.mjs`)
// executes the unit tests for the two behaviours here that fail
// silently — a $1 rounding drift off the web/email, and a savings
// goal lost to a concurrent edit. The server suite runs that script
// too, so `make test` goes red on a regression.
import type { AccountChoice, AccountRemoveMode, CustomBucket,
              ElevationProof, ElevationStatus, IncomeScenario,
              SavingsGoal } from "./api";

// A typed amount's separators, read by the server's own rules for an
// amount cell (csvimport.parse_money_text), so a client never sends a
// different number than the one the server would read from the same text:
// "1,234.56" (thousands comma), "1.234,56" (thousands dot, decimal comma),
// "12,50" (a bare decimal comma, one or two digits). Any other comma is
// ambiguous and refused — stripping it turned "12,50" into 1250.
const THOUSANDS_COMMA = /^\d{1,3}(?:,\d{3})+(?:\.\d+)?$/;
const THOUSANDS_DOT_DECIMAL_COMMA = /^\d{1,3}(?:\.\d{3})+,\d{1,2}$/;
const DECIMAL_COMMA = /^\d+,\d{1,2}$/;

/** `v` without "$" or spaces, a typographic minus as "-", and its
 *  separators normalised to a plain decimal point; null when a comma in
 *  it fits none of the server's conventions. */
function normaliseMoneyText(v: string): string | null {
  const t = (v ?? "").replace(/[$\s]/g, "").replace(/\u2212/g, "-");
  const sign = t.startsWith("-") ? "-" : "";
  let body = t.slice(sign.length);
  if (body.includes(",")) {
    if (THOUSANDS_COMMA.test(body)) body = body.replace(/,/g, "");
    else if (THOUSANDS_DOT_DECIMAL_COMMA.test(body))
      body = body.replace(/\./g, "").replace(",", ".");
    else if (DECIMAL_COMMA.test(body)) body = body.replace(",", ".");
    else return null;
  }
  return sign + body;
}

/** the screens' loose amount-field parser: "$1,234.56 " → 1234.56,
 *  "12,50" → 12.5; anything that is not a number reads as 0 */
export const numv = (v: string) => {
  // a typed run of digits past what a number holds reads as nothing typed
  const n = Number(normaliseMoneyText(v || "0") ?? "0");
  return Number.isFinite(n) ? n : 0;
};

/** A typed amount as a what-if query value. The GET doors type their
 *  amounts as numbers, so "1,000" from a phone keyboard is refused
 *  outright where the save door (which strips "$" and ",") would take
 *  it; parsing here first makes the what-if read the same amount the
 *  save would store. `whole` rounds for a door that takes whole dollars.
 *  A blank box is 0; text that is not an amount rides AS TYPED, so the
 *  server refuses it and the screen shows why — as the web does — rather
 *  than quietly projecting $0 (numv's reading, kept for its own callers). */
export const moneyParam = (v: string, whole = false) => {
  if ((v ?? "").replace(/[$\s]/g, "") === "") return "0";
  const n = parseMoneyDraft(v);
  if (n === null) return v.trim();
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

/** A figure as the phone prints it; null or undefined is a figure not
 *  known (a read that has not answered) and prints UNKNOWN_FIGURE, as the
 *  web's money(null) does — never $0. */
export const money = (n: number | null | undefined, cents = true) => {
  if (n === null || n === undefined) return UNKNOWN_FIGURE;
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

/** A typed birthdate as the ISO date the settings door takes, or the
 *  sentence to show. The web wizard's date picker always hands over
 *  YYYY-MM-DD; a phone keyboard gets free text, so the US M/D/YYYY shape
 *  is read too, and an impossible or future date is refused here instead
 *  of by a save nobody sees fail. Blank is "no birthdate" (iso ""). */
export function birthdateIso(text: string, today = new Date()):
    { iso: string } | { error: string } {
  const t = text.trim();
  if (!t) return { iso: "" };
  const m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(t);
  const u = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(t);
  const [y, mo, d] = m ? [+m[1], +m[2], +m[3]]
    : u ? [+u[3], +u[1], +u[2]] : [NaN, NaN, NaN];
  const dt = new Date(y, mo - 1, d);
  if ((!m && !u) || dt.getFullYear() !== y || dt.getMonth() !== mo - 1
      || dt.getDate() !== d)
    return { error: "Enter the birthdate like 1980-01-31 or 1/31/1980." };
  if (dt >= today || y < 1900)
    return { error: "The birthdate must be a real date in the past." };
  const p2 = (n: number) => String(n).padStart(2, "0");
  return { iso: `${y}-${p2(mo)}-${p2(d)}` };
}

/** A typed "Owned since" (the day a hand-entered asset was acquired) as
 *  the ISO date the settings door takes, or the sentence to show — read
 *  like birthdateIso: YYYY-MM-DD or M/D/YYYY, a real day, not ahead of
 *  today. Blank is iso "": the server keeps the stored date (a new item:
 *  today), as the web's cleared date input does. */
export function ownedSinceIso(text: string, today = new Date()):
    { iso: string } | { error: string } {
  const t = text.trim();
  if (!t) return { iso: "" };
  const m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(t);
  const u = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(t);
  const [y, mo, d] = m ? [+m[1], +m[2], +m[3]]
    : u ? [+u[3], +u[1], +u[2]] : [NaN, NaN, NaN];
  const dt = new Date(y, mo - 1, d);
  if ((!m && !u) || dt.getFullYear() !== y || dt.getMonth() !== mo - 1
      || dt.getDate() !== d || y < 1900)
    return { error: "Enter the owned-since date as YYYY-MM-DD or M/D/YYYY." };
  if (dt > today)
    return { error: "The owned-since date can't be in the future." };
  const p2 = (n: number) => String(n).padStart(2, "0");
  return { iso: `${y}-${p2(mo)}-${p2(d)}` };
}

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
// Throws (savedAmount) when a touched row's amount is not an amount.
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
    // a typed amount that is not one refuses the save (savedAmount),
    // as the web card does — never a silent $0
    out.push({ name, target: savedAmount(r.target, `${name} target`),
               target_date: r.target_date || null,
               monthly_plan: savedAmount(r.monthly_plan, `${name} plan`),
               account_id: r.account_id || null,
               tokens: r.tokens.split(",").map((t) => t.trim())
                 .filter(Boolean),
               start_balance: savedAmount(r.start_balance,
                                          `${name} starting balance`),
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
type DatedRow = { id: string; date: string };

/** The words after a narrowed month's listed spending: the server's
 *  `listed_scope` says whose spending it is — a business's when every
 *  listed row is a business's, else the household's. Shared with the web
 *  (webapp/src/moneymath.ts). */
export const listedSpendWord = (scope?: string | null) =>
  scope === "business" ? "spent (business)" : "spent";

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
 *  A header's spend is the server's whole-day figure (`day_spend`, keyed
 *  by ISO date: every row of that day in the result, a page not yet
 *  loaded included), never a sum of the rows listed so far; an older
 *  server sends none and the header carries $0, which prints no amount.
 *  Mirrors the web's TxnTable dayGroups. */
export function withDayHeads<T extends DatedRow>(
  rows: readonly T[],
  daySpend?: Readonly<Record<string, number>> | null,
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
                 spent: daySpend?.[group[0].date.slice(0, 10)] ?? 0 });
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

/** "today" / "tomorrow" for a coming date, null for any other day (the
 *  caller prints its own date form). `today` is the household's day. */
export const nearDay = (iso: string | null | undefined,
                        today: string): "today" | "tomorrow" | null => {
  if (!iso) return null;
  const day = iso.slice(0, 10);
  if (day === today.slice(0, 10)) return "today";
  const t = new Date(today.slice(0, 10) + "T00:00:00Z");
  t.setUTCDate(t.getUTCDate() + 1);
  return day === t.toISOString().slice(0, 10) ? "tomorrow" : null;
};

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

/** The category a merchant's rows carry today — the most common one over
 *  its matched ledger rows (the merchant history card's resting label;
 *  web dominantCategory). null when no row carries a category. */
export const dominantCategory = (txns: { category: string | null }[]): string | null => {
  const n = new Map<string, number>();
  for (const t of txns) if (t.category) n.set(t.category, (n.get(t.category) ?? 0) + 1);
  let best: string | null = null;
  for (const [c, k] of n) if (best === null || k > n.get(best)!) best = c;
  return best;
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

/** the server's card due-date marks (todayview.due_marks) as chart events:
 *  one line per card per due date, its caption and the hovered day's line
 *  in the server's words */
export function dueMarks(series: [string, number][],
                         marks: { date: string; label: string; words: string }[]) {
  return marks.flatMap((m) => {
    const i = series.findIndex(([d]) => d === m.date);
    return i < 0 ? [] : [{ i, label: m.label, note: m.words }];
  });
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

// The days a new account is left alone about its unconfirmed address: the
// tabs banner waits them out, and so does the sentence about freezing — a
// person who signed up a minute ago has not ignored anything yet. An
// unknown age fails toward saying it. And always once the freeze is a week
// away: the deadline is the HOUSEHOLD's, counted from its oldest signup,
// so a member invited yesterday can be days from a freeze they must be
// told about. Mirrors webapp verifyNagDue.
export function verifyNagDue(createdAt: string | null | undefined,
                             deadline?: string | null,
                             now: number = Date.now()): boolean {
  if (deadline) {
    const d = Date.parse(String(deadline));
    if (Number.isFinite(d) && d - now <= 7 * 86_400_000) return true;
  }
  if (!createdAt) return true;
  const t = Date.parse(String(createdAt));
  if (!Number.isFinite(t)) return true;
  return now - t >= 3 * 86_400_000;
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
/** The window every over-time toggle opens on (Net Worth, Cash Flow) —
 *  the web's DEFAULT_RANGE. */
export const DEFAULT_TREND_RANGE: TrendRange = "1y";
/** A range handed over in a link (`?range=`) as a window this screen
 *  knows, else the default — the web reads its URL's range the same way,
 *  so a screen opened from another keeps the window picked there. */
export const trendRangeOf = (v: unknown): TrendRange =>
  TREND_RANGES.includes(v as TrendRange) ? v as TrendRange
    : DEFAULT_TREND_RANGE;
export const RANGE_LABEL: Record<TrendRange, string> =
  { cur: "This month", "1m": "1m", "3m": "3m", "6m": "6m", "1y": "1y",
    "3y": "3y", "5y": "5y", all: "All" };
/** How a caption names the window a figure was measured over ("+$120
 *  over this month"). Web twin: `rangeCaption` in RangePicker.tsx. */
/** The Transactions route params a Spending figure opens: the rows that
 *  figure summed over one range (GET /api/transactions?spend=…, the
 *  server's reporting.spending_window_ledger) — a category, a merchant,
 *  or with neither the window's whole spend; `previous` is the window a
 *  dropped merchant was paid in. Web twin: spendDrillHref in
 *  webapp/src/pages/Spending.tsx. */
export function spendDrillParams(range: TrendRange, what: {
  cat?: string; merchant?: string; previous?: boolean;
} = {}): Record<string, string> {
  return {
    spend: range,
    ...(what.cat ? { cat: what.cat } : {}),
    ...(what.merchant ? { merchant: what.merchant } : {}),
    ...(what.previous ? { side: "previous" } : {}),
  };
}

/** Whether a Spending window's figures may link to their rows: only when
 *  the server says it lists them (`figures_open_rows`). A server from
 *  before the drill drops `spend=` and answers some other list, so its
 *  figures stay plain text. The flag is read structurally so a phone
 *  built against either schema compiles. */
export function spendDrillOffered(
  w: { figures_open_rows?: boolean | null } | object | null | undefined,
): boolean {
  // older-server path: stands in for `figures_open_rows`
  return !!w && "figures_open_rows" in w && w.figures_open_rows === true;
}

/** What the Transactions tab makes of a Spending figure's reply:
 *  "waiting" until the reply answering THIS link is on screen, "listed"
 *  when the server named the rows (`drill_label`), "unsupported" when it
 *  answered without one — a server from before the drill, which ignored
 *  `spend=` and listed something else. */
export function spendDrillState(
  spend: string,
  reply: { drill_label?: string | null } | null | undefined,
  placeholder: boolean,
): "none" | "waiting" | "listed" | "unsupported" {
  if (!spend) return "none";
  if (!reply || placeholder) return "waiting";
  // older-server path: stands in for `drill_label`
  return reply.drill_label ? "listed" : "unsupported";
}

export function rangeCaption(range: TrendRange): string {
  return range === "all" ? "all time" : range === "cur" ? "this month"
    : range === "1m" ? "last month" : range;
}
/** The words after a change measured over a window: "over 1y", "over all
 *  time", and for the two calendar-month windows "this month" / "last
 *  month" ("over this month" reads as nothing a person says). Web twin:
 *  `overCaption` in webapp/src/components/RangePicker.tsx. */
export function overCaption(range: TrendRange): string {
  return range === "cur" || range === "1m" ? rangeCaption(range)
    : `over ${rangeCaption(range)}`;
}
// the site-wide vocabulary in months (null = all); the web twin is
// webapp/src/components/RangePicker.tsx — keep them identical. "1m"
// counts two so a window cut on the current month reaches the last
// complete one (the cash graph's history stops before the current month).
export const RANGE_MONTHS: Record<TrendRange, number | null> =
  { cur: 1, "1m": 2, "3m": 3, "6m": 6, "1y": 12, "3y": 36, "5y": 60, all: null };

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
 *  in September and "cur" in October — whose `points` history is empty,
 *  because it stops before the current month; the month in progress is
 *  the graph's own `current` bar, which the pages add after the history.
 *  Only `graphWindow` below reads it, for a server from before
 *  `cash_graph.windows`; the web is served with its server and has no
 *  such table. */
// older-server path: stands in for `cash_graph.windows` (via graphWindow)
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

/** The months a range draws under "Left over, by month", and the line
 *  under the chart: the server's (`cash_graph.windows`,
 *  reporting._drawn_windows) — a range of complete months ends with its
 *  last one, and the month in progress and the forecast follow only a
 *  range that includes this month. A server from before `windows` sent
 *  none: the chart is then cut as that server's phones cut it (from the
 *  range's first month, the tail always drawn) and there is no line. */
// older-server path: stands in for `cash_graph.windows`
export function graphWindow(
  graph: { points: [string, number][]; forecast_from: number;
           windows?: Record<string, { first: string; last: string;
                                      note: string }> },
  range: TrendRange,
): { first: string; last: string; note: string } {
  return graph.windows?.[range]
    ?? { first: cashflowCutoffMonth(graph, range) ?? "", last: "9999-12",
         note: "" };
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
  // top holdings leaves hidden and removed accounts out
  const keys = ["accounts", "today", "transactions", "holdings"];
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
// The server filters and sorts before it caps the page, and the list is
// printed in its order; a filter or a spend/recency sort still asks for the
// widest page the server allows (500), the plain orders the 200-row one.
export const catalogLimit = (order: MerchantOrder, flt: MerchantFilter): number =>
  flt !== "all" || order === "spend" || order === "recent" ? 500 : 200;

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
 *  be typed at the end. "$" and spaces are dropped, commas are read by
 *  the server's rules (normaliseMoneyText); a typographic minus counts
 *  as "-". Blank is the caller's call (0, or "cleared"). */
export function parseMoneyDraft(v: string): number | null {
  const t = normaliseMoneyText(v);
  if (t === null || !/^-?(\d+\.?\d*|\.\d+)$/.test(t)) return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}

/** A typed amount as the number a settings save stores, or a refusal.
 *  Blank is 0. Text that is not an amount throws, naming `what` — sent
 *  on, it would be stored as $0 without a word, replacing the figure the
 *  person meant to type. The bucket-rule and savings-goal saves on both
 *  clients read their amounts through this one rule. */
export function savedAmount(text: string, what: string): number {
  if ((text ?? "").replace(/[$\s]/g, "") === "") return 0;
  const n = parseMoneyDraft(text);
  if (n === null)
    throw new Error(`${what}: "${text.trim()}" is not an amount — `
                    + "type it like 1,250.50.");
  return n;
}

/** The split editor's arithmetic: each part's cents as typed (null when
 *  the text is not an amount yet), what is left to place against the
 *  charge, and whether the split can be saved — at least two parts, each
 *  with a category and more than zero, no category twice, adding up to
 *  the charge to the cent. */
export function splitDraftCheck(charge: number,
                                parts: readonly { category: string; amount: string }[]):
    { cents: (number | null)[]; left: number; ready: boolean } {
  // parseMoneyDraft, not Number(): a decimal-comma keypad types "12,50",
  // which Number() reads as NaN, so no part with cents could be saved
  const cents = parts.map((p) => {
    const v = parseMoneyDraft(p.amount);
    return v === null ? null : pyround(v * 100);
  });
  const left = pyround(Math.abs(charge) * 100)
    - cents.reduce<number>((a, c) => a + (c ?? 0), 0);
  const ready = parts.length >= 2 && left === 0
    && parts.every((p, i) => !!p.category && (cents[i] ?? 0) > 0)
    && new Set(parts.map((p) => p.category)).size === parts.length;
  return { cents, left, ready };
}

/** A receipt's total, date and store as typed on a waiting receipt, ready
 *  for POST /api/receipts/{id}/details — or the reason it is not yet.
 *  The total is money in: positive, rounded to the cent ("$27.35",
 *  "1,204.5"). The date is a real calendar day, YYYY-MM-DD. The store is
 *  optional, trimmed, 80 characters at most; blank sends "" (clears the
 *  store read off the slip), and none passed — or the `stored` one,
 *  untouched — sends none (keeps it). */
export function receiptDetailsDraft(total: string, date: string,
                                    merchant?: string,
                                    stored?: string | null):
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
  // the store the form was filled with (`stored`), saved untouched, is
  // kept as stored: one read before the server's bound may be longer, and
  // the person never typed it
  const kept = merchant === undefined
    || (store !== "" && store === (stored ?? "").trim());
  // the server's STORE_MAX (engine/receipts.py)
  if (!kept && store.length > 80)
    return { ok: false, error: "The store name is 80 characters at most" };
  // a store field the person emptied is sent empty, which clears the
  // store read off the slip; no field at all keeps it
  return { ok: true, body: { total: cents / 100, date: d,
                             ...(kept ? {} : { merchant: store }) } };
}

// The bank feed's "type/subtype" pair, said the way a statement would:
// "depository/money market" is a money market, "credit/credit card" a
// credit card. Web twin: `kindWords` in webapp/src/moneymath.ts.
const KIND_WORDS: Record<string, string> = {
  cd: "CD", ira: "IRA", roth: "Roth IRA", hsa: "HSA", "401k": "401(k)",
  "401a": "401(a)", "403b": "403(b)", "457b": "457(b)", "529": "529 plan",
  hsa_cash: "HSA", "roth 401k": "Roth 401(k)", sep: "SEP IRA",
};
export function kindWords(kind: string | null | undefined): string {
  const sub = (kind ?? "").split("/").pop()!.trim();
  if (!sub) return "";
  return KIND_WORDS[sub.toLowerCase()]
    ?? sub.charAt(0).toUpperCase() + sub.slice(1);
}

/** An account named with its institution, the institution said once: an
 *  account the bank already names after itself ("Fabrikam (daily)" at
 *  Fabrikam) read "Fabrikam Fabrikam (daily)". Web twin: `accountLabel`
 *  in webapp/src/moneymath.ts. */
export function accountLabel(a: { institution_name?: string | null;
                                  name: string; mask?: string | null }): string {
  const inst = (a.institution_name ?? "").trim();
  const name = (a.name ?? "").trim();
  const said = inst && name.toLowerCase().startsWith(inst.toLowerCase());
  return `${inst && !said ? `${inst} ` : ""}${name}${a.mask ? ` ${a.mask}` : ""}`;
}

/** How a connection's last sync ended, in the words its Accounts row
 *  uses for the same state (the server's `status_kind`), never the raw
 *  status string. */
export function connectionStateWords(c: { status?: string | null;
                                          status_kind?: string }): string {
  const kind = c.status_kind
    ?? (c.status && c.status !== "ok" ? "attention" : "ok");
  return kind === "ok" ? "ok"
    : kind === "retrying" ? "bank having trouble — retrying"
    : kind === "reauth" ? "sign-in expired"
    : kind === "gone" ? "connection removed"
    : "needs attention";
}

/** Which of the account editor's two budget switches an account is
 *  offered. The SERVER decides (the accounts reply's `can_pin` and
 *  `can_exclude`): the exclusion wherever a row on the account can be
 *  spending, by the predicate the budget itself reads, and the forecast
 *  anchor on the type the forecast picks among. A switch that is already
 *  ON is always offered, so it can be turned off; a server too old to say
 *  gets both switches, never a hidden one. Web twin:
 *  `accountSwitches` in webapp/src/moneymath.ts. */
export function accountSwitches(a: { can_pin?: boolean; can_exclude?: boolean;
                                     pinned: boolean; excluded: boolean }):
    { primary: boolean; excluded: boolean } {
  return { primary: a.pinned || (a.can_pin ?? true),
           excluded: a.excluded || (a.can_exclude ?? true) };
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
    const monthly = Math.max(0, numv(r.monthly));
    // $0 on a stored bucket is a plan ("nothing allowed here") that keeps
    // the rules claiming its spending; the row's ✕ is what removes one. A
    // row that was never a bucket has nothing to keep until it has money.
    if (!name || (monthly <= 0 && r.orig === undefined)) continue;
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

/** What the budget planner's bucket rows start from — at open, and again
 *  after each save with the list the server stored: the merge base (the
 *  "other" buckets' names) and their rows, each remembering the bucket it
 *  came from. Food buckets belong to the Bucket-rules card and are never
 *  planner rows. See savedMergeBase for `sent`. */
export function plannerBucketsSaved(saved: readonly CustomBucket[],
                                    sent?: readonly PlannerRow[]): {
  base: string[]; rows: PlannerRow[];
} {
  const rows = saved.filter((b) => b.parent === "other")
    .map((b) => ({ name: b.name, monthly: String(b.monthly || 0),
                   cats: b.categories ?? [], merch: b.merchants ?? [],
                   orig: b.name }));
  return { base: savedMergeBase(rows.map((r) => r.name),
                                sent?.filter((r) => !r.food)), rows };
}

/** The merge base after a save of a whole named list (buckets, goals):
 *  the names the card's rows are now known to stand for. A record added
 *  here and saved is no longer "added elsewhere", so a later removal or
 *  rename must not carry it back through. When the rows are re-seeded
 *  from the saved list (`sent` absent) that is every stored name. When
 *  the save landed mid-edit the rows stay as typed, so:
 *  - of the stored names, only those the rows SENT: a record another card
 *    saved, carried through by this save but absent from the rows, stays
 *    "added elsewhere" — in the base, the next save would read it as
 *    removed here and drop it;
 *  - plus the previous base's names that are no longer stored: they were
 *    this card's and were deleted or renamed elsewhere, and their rows
 *    may still be on screen. In the base, the next merge reads such an
 *    untouched row as "deleted elsewhere" and drops it; out of it, the
 *    row would be written back as new. A row the person edited (touched)
 *    is theirs and is written either way. */
export function savedMergeBase(stored: readonly string[],
                               sent?: readonly { name: string }[],
                               prevBase: readonly string[] = []): string[] {
  if (!sent) return [...stored];
  const mine = new Set(sent.map((r) => r.name.trim()));
  const now = new Set(stored);
  return [...stored.filter((n) => mine.has(n)),
          ...prevBase.filter((n) => !now.has(n))];
}

/** one Bucket-rules card row as drafted on screen: the amount and the
 *  merchant list are text while being typed. `touched` marks a row edited
 *  here since it was seeded. */
export type BucketRuleDraft = {
  name: string; parent: "food" | "other"; monthly: string;
  categories: string[]; merchants: string; touched?: boolean;
};

/** The custom_buckets list the Bucket-rules card saves.
 *
 *  POST /api/settings replaces the list wholesale, and the card's rows
 *  were seeded from `baselineNames`' list — the planner or another device
 *  may have saved since. Against the server's CURRENT list: a touched row
 *  wins; an untouched row follows its live copy (deleted elsewhere →
 *  stays deleted); a bucket the person removed here stays removed; and a
 *  live bucket the base never knew was added elsewhere and is carried
 *  through. `amount` turns a typed amount into a number (and may refuse
 *  it by throwing). */
export function mergeBucketRules(
  rows: readonly BucketRuleDraft[],
  live: readonly CustomBucket[],
  baselineNames: readonly string[],
  amount: (text: string, name: string) => number,
): CustomBucket[] {
  const liveBy = new Map(live.map((b) => [b.name, b] as const));
  const baseNames = new Set(baselineNames);
  const out: CustomBucket[] = [];
  for (const r of rows) {
    const name = r.name.trim();
    if (!name) continue;
    if (!r.touched) {
      const cur = liveBy.get(name);
      if (cur) { out.push(cur); continue; }
      if (baseNames.has(name)) continue;   // deleted elsewhere
    }
    out.push({ name, parent: r.parent, monthly: amount(r.monthly, name),
               categories: r.categories,
               merchants: r.merchants.split(",").map((m) => m.trim())
                 .filter(Boolean) });
  }
  for (const b of live)
    if (!baseNames.has(b.name) && !out.some((o) => o.name === b.name))
      out.push(b);                          // added elsewhere since the base
  return out;
}

/** What the Bucket-rules card starts from — at mount, and again after
 *  each save with the list the server stored: the merge base and the rows
 *  seeded from it. `sent` (and the base it was merged on) is passed when
 *  the save landed mid-edit and the rows stay as typed (see
 *  savedMergeBase). */
export function bucketRulesSaved(saved: readonly CustomBucket[],
                                 sent?: readonly BucketRuleDraft[],
                                 prevBase?: readonly string[]): {
  base: string[]; rows: BucketRuleDraft[];
} {
  return {
    base: savedMergeBase(saved.map((b) => b.name), sent, prevBase),
    rows: saved.map((b) => ({
      name: b.name, parent: b.parent,
      monthly: b.monthly ? String(b.monthly) : "",
      categories: b.categories ?? [],
      merchants: (b.merchants ?? []).join(", ") })),
  };
}

const PICKED_TYPES: Record<string, string> = {
  png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg",
  webp: "image/webp", gif: "image/gif", heic: "image/heic",
  heif: "image/heif" };

/** The upload identity of a photo the image picker returned (picked at
 *  quality < 1, as every receipt picker here is), read from the file it
 *  actually wrote — the server stores the declared type as the file's, so
 *  a label that disagrees with the bytes is kept and served wrong.
 *  expo-image-picker 57, from its source:
 *  - Android re-encodes the photo (CompressionImageExporter): the bytes
 *    are PNG when the written file is .png and JPEG for everything else —
 *    a HEIC, WebP, GIF or BMP source included, even when the file is
 *    named .webp/.gif — while `mimeType` and `fileName` still describe
 *    the SOURCE. So only the written file's extension counts, png or jpeg.
 *  - iOS writes the bytes its extension names and derives `mimeType` from
 *    that same extension, so the written file's extension, then mimeType,
 *    then the camera's JPEG.
 *  (Mobile only: the web's file input carries the browser's own type.) */
export function pickedImagePart(
  a: { uri: string; mimeType?: string | null },
  platform: "ios" | "android" | string,
  kind: "receipt" | "check" = "receipt",
): { uri: string; name: string; type: string } {
  const ext = /\.([a-z0-9]+)(?:[?#].*)?$/i.exec(a.uri)?.[1]
    ?.toLowerCase() ?? "";
  const type = platform === "android"
    ? (ext === "png" ? "image/png" : "image/jpeg")
    : PICKED_TYPES[ext] || a.mimeType || "image/jpeg";
  const named = Object.entries(PICKED_TYPES).find(([, t]) => t === type);
  return { uri: a.uri, name: `${kind}.${named ? named[0] : "jpg"}`, type };
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
      && (!a.as_of || String(a.as_of) === String(b.as_of))
      && (!a.acquired_on || String(a.acquired_on) === String(b.acquired_on));
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

// ---- whole-list settings: compare-and-swap twin block ----
// These settings are stored as whole values the server replaces on save.
// Two devices that each read a list, each change a different item and
// each save would otherwise leave only the second device's change. So a
// save names the version of each such list it was built on; the server
// refuses (409 settings_moved) when the stored list moved since, and the
// client lays the person's own changes over the fresh list and tries
// once more. This block is kept character-for-character equal in
// webapp/src/moneymath.ts and mobile/src/lib/pure.ts, and one case
// table runs against both.

export const WHOLE_LIST_KEYS = ["savings_goals", "custom_buckets",
                                "income_scenarios", "email_schedule",
                                "llm_backends", "llm_roles",
                                "manual_assets"] as const;

/** What a second refusal tells the person: the screen now holds the
 *  stored copy, and their change did not land. */
export const SETTINGS_MOVED_TEXT =
  "Changed on another device. Reloaded the latest; make your change again.";

type Versions = Readonly<Record<string, unknown>> | null | undefined;

/** The list_versions to send with `body`: the version, from the settings
 *  view the edit was built on, of each whole-list key the body carries.
 *  Null when there are none — a body with no whole-list key, or a server
 *  that publishes no versions, saves exactly as before. */
export function listVersionsFor(
  body: Readonly<Record<string, unknown>>, versions: Versions,
): Record<string, string> | null {
  const out: Record<string, string> = {};
  for (const k of WHOLE_LIST_KEYS) {
    const v = versions?.[k];
    if (k in body && typeof v === "string") out[k] = v;
  }
  return Object.keys(out).length ? out : null;
}

/** Each row of a list keyed by `key`, numbered by occurrence so two rows
 *  of one key stay two. */
const keyedRows = <T>(rows: unknown, key: (r: T) => string) => {
  const list = Array.isArray(rows) ? (rows as T[]) : [];
  const seen = new Map<string, number>();
  return Object.fromEntries(list.map((r) => {
    const k = key(r);
    const n = seen.get(k) ?? 0;
    seen.set(k, n + 1);
    return [`${k}\u0000${n}`, r];
  })) as Record<string, T & object>;
};

/** One whole-list setting after this save's changes are laid over the
 *  list stored now. `base` is the list the edit started from, `mine` the
 *  list being saved, `theirs` the fresh stored list: an item added here
 *  is added, one removed here is removed, a field changed here takes this
 *  save's value (it wins when both devices changed the same field), and
 *  everything the other device changed or added stands. Goals and buckets
 *  are identified by name, and so are hand-valued assets; scenarios by
 *  name compared as the server compares them (trimmed, any case); AI
 *  backends by id, and the AI routing per task; the email schedule per
 *  cadence, and only the cadences this save names are sent, since the
 *  server keeps the ones a body leaves out. */
export function remergeWholeList(
  key: string, base: unknown, mine: unknown, theirs: unknown,
): unknown {
  const name = (r: { name?: unknown }) => String(r?.name ?? "");
  if (key === "savings_goals" || key === "custom_buckets"
      || key === "income_scenarios" || key === "manual_assets"
      || key === "llm_backends") {
    type Row = { name?: unknown; id?: unknown };
    const k = key === "income_scenarios"
      ? (r: Row) => name(r).trim().toLowerCase()
      : key === "llm_backends" ? (r: Row) => String(r?.id ?? "") : name;
    return Object.values(rebaseKeyed(keyedRows(mine, k), keyedRows(base, k),
                                     keyedRows(theirs, k)));
  }
  if (key === "llm_roles") {
    const obj = (v: unknown) => (v && typeof v === "object" && !Array.isArray(v)
      ? v : {}) as Record<string, unknown>;
    // one task's route is one field: a task re-routed or unrouted here
    // moves, every other task keeps what is stored
    return mine && typeof mine === "object"
      ? rebaseFields(obj(mine), obj(base), obj(theirs)) : mine;
  }
  if (key === "email_schedule") {
    const obj = (v: unknown) => (v && typeof v === "object" && !Array.isArray(v)
      ? v : {}) as Record<string, object>;
    // a reset (null or {}) means "back to the default" — nothing to merge
    if (!mine || typeof mine !== "object" || !Object.keys(mine).length)
      return mine;
    const m = obj(mine);
    const out = rebaseKeyed(m, obj(base), obj(theirs));
    for (const c of Object.keys(out)) if (!(c in m)) delete out[c];
    return out;
  }
  return mine;
}

/** The keys a 409 settings_moved names, or null for any other failure.
 *  Reads both clients' error shapes: the phone's ApiError (status + the
 *  structured body) and the web's Error("409: <json body>"). */
export function settingsMovedOf(e: unknown): { keys: string[] } | null {
  const o = e as { status?: unknown; body?: unknown; message?: unknown } | null;
  if (!o || typeof o !== "object") return null;
  let status = o.status;
  let body = o.body;
  if (body === undefined && typeof o.message === "string") {
    const m = /^(\d{3}):\s*([\s\S]*)$/.exec(o.message);
    if (m) {
      status = Number(m[1]);
      try { body = JSON.parse(m[2]); } catch { body = null; }
    }
  }
  if (status !== 409 || !body || typeof body !== "object") return null;
  type Moved = { error?: unknown; keys?: unknown; detail?: unknown };
  const b = body as Moved;
  const d = b.error === "settings_moved" ? b
    : (b.detail && typeof b.detail === "object"
       && (b.detail as Moved).error === "settings_moved")
      ? (b.detail as Moved) : null;
  if (!d) return null;
  return { keys: Array.isArray(d.keys)
    ? d.keys.filter((x): x is string => typeof x === "string") : [] };
}

/** A form saves only what it was seeded with from a successful read.
 *  Until then its Save waits and says why: still loading, or the read
 *  failed, so whatever the form holds is a default nobody saw stored. */
export const SAVE_WAITS_LOADING = "Loading…";
export const SAVE_WAITS_FAILED =
  "Couldn't load what's saved, so nothing can be saved over it yet.";

/** Why a form's Save is held, or null once the form was seeded. */
export function saveWaits(seeded: boolean, readFailed: boolean): string | null {
  if (seeded) return null;
  return readFailed ? SAVE_WAITS_FAILED : SAVE_WAITS_LOADING;
}

/** A whole-list save with no settings view under it: the list on screen
 *  was never seeded from what is stored, so sending it would replace the
 *  stored list with a default. Refused before anything is sent. */
export class SettingsUnreadError extends Error {
  constructor() {
    super(SAVE_WAITS_FAILED);
    this.name = "SettingsUnreadError";
  }
}

/** The second refusal: `latest` is the stored view read after it, for the
 *  caller to put on screen. */
export class SettingsMovedError extends Error {
  latest: object | null;
  constructor(latest: object | null) {
    super(SETTINGS_MOVED_TEXT);
    this.name = "SettingsMovedError";
    this.latest = latest;
  }
}

/** Send a settings body under compare-and-swap. `basis` is the settings
 *  view the body was built from (its lists and list_versions); `send`
 *  posts a body, `read` fetches the stored view. A moved list is re-read,
 *  this save's changes are laid over it, and the save goes again ONCE
 *  with the fresh versions; a second refusal throws SettingsMovedError,
 *  never a third try — another device saving in a loop must not keep
 *  this one sending. Any other failure is handed back as it came. */
export async function saveWholeLists<V>(
  body: Record<string, unknown>, basis: object | null | undefined,
  send: (b: Record<string, unknown>) => Promise<V>, read: () => Promise<object>,
): Promise<V> {
  type View = Record<string, unknown> & { list_versions?: Versions };
  // no view at all means the lists in the body were never read: refuse
  // rather than replace what is stored (a view without versions is an
  // older server, and saves as before)
  if (basis == null && WHOLE_LIST_KEYS.some((k) => k in body))
    throw new SettingsUnreadError();
  const was = (basis ?? {}) as View;
  const versions = listVersionsFor(body, was.list_versions);
  if (!versions) return send(body);
  try {
    return await send({ ...body, list_versions: versions });
  } catch (e) {
    if (!settingsMovedOf(e)) throw e;
  }
  const fresh = (await read()) as View;
  const again: Record<string, unknown> = { ...body };
  for (const k of WHOLE_LIST_KEYS) {
    if (!(k in body)) continue;
    // a list whose version did not move is sent exactly as built
    if (versions[k] === fresh.list_versions?.[k]) continue;
    again[k] = remergeWholeList(k, was[k], body[k], fresh[k]);
  }
  const fv = listVersionsFor(again, fresh.list_versions);
  try {
    return await send(fv ? { ...again, list_versions: fv } : again);
  } catch (e) {
    if (!settingsMovedOf(e)) throw e;
    let latest: object | null = fresh;
    try { latest = await read(); } catch { /* keep the read we have */ }
    throw new SettingsMovedError(latest);
  }
}
// ---- end of the whole-list settings twin block ----

// ---- figures that wait for their inputs: twin block ----
// A figure worked out on the client from a query's answer (the planner's
// Bills, the savings left over) may only be shown, or saved, once that
// query has answered. While it loads or after it failed the figure is the
// unknown mark, and Save says why it waits. Kept character-for-character
// equal in webapp/src/moneymath.ts and mobile/src/lib/pure.ts; the shared
// case table runs against both.

/** The mark for a figure that is not known: never $0, which is a claim
 *  about the household. One constant, so the mark is a one-line change. */
export const UNKNOWN_FIGURE = "·";

/** Where a query stands for a figure built from it: it has an answer,
 *  is still loading, or failed with nothing to show. */
export type InputPhase = "ready" | "loading" | "failed";

export function inputPhase(q: { data?: unknown; isError: boolean }): InputPhase {
  if (q.data !== undefined) return "ready";
  return q.isError ? "failed" : "loading";
}

export const PLANNER_BILLS_FAILED =
  "Couldn't load your bills, so the plan can't be worked out or saved yet.";
export const PLANNER_HISTORY_FAILED =
  "Couldn't load your spending history, so the plan can't be filled in or saved yet.";

/** Why the budget planner shows no worked-out figure and holds Save, or
 *  null once every input it computes from has answered. `bills` is the
 *  read the Bills figure comes from; `history` is the suggestion a first
 *  run is filled in from ("ready" when nothing waits on it). */
export function plannerWaits(bills: InputPhase, history: InputPhase):
    string | null {
  if (bills === "failed") return PLANNER_BILLS_FAILED;
  if (history === "failed") return PLANNER_HISTORY_FAILED;
  if (bills === "loading" || history === "loading") return SAVE_WAITS_LOADING;
  return null;
}
// ---- end of the figures-wait twin block ----

// ---- screen states: twin block ----
// What a screen says when a read failed, or when what it shows is the copy
// from before a refetch that failed; when a failed read is asked again; and
// which figure a screen may print from a read. Kept character-for-character
// equal in webapp/src/moneymath.ts and mobile/src/lib/pure.ts; the shared
// case table runs against both (specs/screen-states.md).

/** Which client is speaking. Only the words for a request that got no
 *  answer differ: a phone with no answer is offline, while a browser
 *  cannot tell a dropped connection from a server that is down. */
export type StatesClient = "web" | "phone";

/** "a moment." or "N seconds." — the wait the two sentences below share */
function inWait(retryAfter?: string | null): string {
  const secs = parseInt(retryAfter ?? "", 10);
  return secs > 0 && secs <= 120
    ? `${secs} second${secs === 1 ? "" : "s"}.`
    : "a moment.";
}

/** "The server is busy — try again in a moment.", or in N seconds when
 *  the server named a short wait: a 503 is the server shedding load. The
 *  sign-in page (pages.js) says the same words. */
export function busyText(retryAfter?: string | null): string {
  return "The server is busy — try again in " + inWait(retryAfter);
}

/** The same shape for the proxy's "back in a moment" answer: the app is
 *  restarting, not overloaded, so it says that. */
export function restartingText(retryAfter?: string | null): string {
  return "The server is restarting — try again in " + inWait(retryAfter);
}

/** The status a failed request came back with: the client's own field
 *  (the phone's ApiError; the web's AuthError and BusyError), else the
 *  leading "NNN: " the web puts on every other refusal — never a number
 *  that happens to sit in the reply's text. Null when no answer came. */
export function failureStatus(e: unknown): number | null {
  const s = (e as { status?: unknown } | null | undefined)?.status;
  if (typeof s === "number" && s > 0) return s;
  const m = e instanceof Error ? /^(\d{3}): /.exec(e.message) : null;
  return m ? Number(m[1]) : null;
}

/** expo/fetch's FetchError messages that are not a missing answer: the
 *  request's own signal cancelled it, or the server answered with a
 *  redirect the request refused. */
const FETCH_ERROR_ANSWERED =
  /^fetch failed: (The operation was aborted|Fetch request has been canceled|Redirect is not allowed)/;

/** A request that never got an answer, so no status: the browser's and
 *  React Native's fetch reject with a TypeError ("Failed to fetch",
 *  "Network request failed"); expo/fetch, the phone's fetch on a device,
 *  with its FetchError, a plain Error reading "fetch failed: <the native
 *  cause>". A cancelled request says nothing about the connection. */
export function noAnswer(e: unknown): boolean {
  if (failureStatus(e) !== null) return false;
  if ((e as { name?: unknown } | null | undefined)?.name === "TypeError")
    return true;
  return e instanceof Error && e.name === "Error"
    && e.message.startsWith("fetch failed: ")
    && !FETCH_ERROR_ANSWERED.test(e.message);
}

/** The server's own sentence in a refusal, or "" when it sent none: the
 *  phone's parsed detail, else the JSON body behind the web's "NNN: ". A
 *  markup page is never read out. */
function serverSentence(e: unknown): string {
  const d = (e as { detail?: unknown } | null | undefined)?.detail;
  if (typeof d === "string" && d.trim()) return d.trim();
  const raw = e instanceof Error ? e.message : "";
  const m = /^(\d{3}):\s*([\s\S]*)$/.exec(raw);
  const body = (m ? m[2] : raw).trim();
  try {
    const p = JSON.parse(body);
    const t = p?.detail ?? p?.message;
    if (typeof t === "string") return t.trim();
    if (Array.isArray(t) && typeof t[0]?.msg === "string") return t[0].msg;
    return "";
  } catch { /* not JSON: the body is the sentence */ }
  return body.startsWith("<") ? "" : body;
}

/** `s` as the end of "Couldn't load bills — …": first letter lowered
 *  unless the first word is an initialism, a full stop added if it ends
 *  without one. */
function asReason(s: string): string {
  const t = /^[A-Z](?![A-Z0-9])/.test(s) ? s[0].toLowerCase() + s.slice(1) : s;
  return /[.!?…]$/.test(t) ? t : t + ".";
}

/** One failed read, said once: the words in place of the section that
 *  needed it, and whether a "Try again" beside them can help. `what` names
 *  the section ("bills", "investment fees"). `doing` is the verb when the
 *  request was a write ("send", "save"): "Couldn't send your report — …". */
export function failLine(e: unknown, what: string, client: StatesClient,
                         doing = "load"): { text: string; retry: boolean } {
  const head = `Couldn't ${doing} ${what} — `;
  const status = failureStatus(e);
  const retryAfter = (e as { retryAfter?: unknown } | null | undefined)
    ?.retryAfter;
  const wait = typeof retryAfter === "string" ? retryAfter : null;
  if (status === null) {
    if (!noAnswer(e))
      return { text: head + "something went wrong.", retry: true };
    return { text: head + (client === "phone" ? "you're offline."
                                              : "can't reach the server."),
             retry: true };
  }
  if ((e as { restarting?: unknown }).restarting === true)
    return { text: restartingText(wait), retry: true };
  if (status === 503)
    return { text: busyText(wait), retry: true };
  if (status >= 500)
    return { text: head + "something went wrong on the server.", retry: true };
  if (status === 429)
    return { text: "Too many requests — wait a minute, then try again.",
             retry: true };
  if (status === 408)
    return { text: head + "the server took too long to answer.", retry: true };
  if (status === 401)
    return { text: head + "sign in again.", retry: false };
  const said = serverSentence(e);
  if (status === 403)
    return { text: said ? head + asReason(said)
                        : doing === "load" ? `Your role doesn't include ${what}.`
                        : "Your role doesn't allow that.",
             retry: false };
  if (!said)
    return { text: head + `the server refused the request (${status}).`,
             retry: status === 409 || status < 400 };
  return { text: head + asReason(said), retry: status === 409 || status < 400 };
}

/** A failed request as one sentence of its own, for the lines that say
 *  why a write did not land: the cause when the server sent no sentence
 *  of its own (no answer, a restart, busy, a 5xx, a 429, a bare refusal),
 *  else null and the server's sentence stands. Never the raw text of the
 *  failure: no "TypeError: …", no status, no JSON, no page. */
export function failCause(e: unknown, client: StatesClient): string | null {
  const status = failureStatus(e);
  const retryAfter = (e as { retryAfter?: unknown } | null | undefined)
    ?.retryAfter;
  const wait = typeof retryAfter === "string" ? retryAfter : null;
  if (status === null)
    return noAnswer(e) ? (client === "phone" ? "You're offline."
                                             : "Can't reach the server.")
      : null;
  if ((e as { restarting?: unknown }).restarting === true)
    return restartingText(wait);
  if (status === 503) return busyText(wait);
  const said = serverSentence(e);
  if (said && !/^\d{3}\b/.test(said)) return null;
  if (status >= 500) return "Something went wrong on the server.";
  if (status === 429) return "Too many requests — wait a minute, then try again.";
  if (status === 408) return "The server took too long to answer.";
  return "The server refused the request.";
}

const MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** When a copy was fetched, as the stale line names it: a clock time
 *  today ("9:41 AM"), a date this year ("Mar 3"), else with its year. */
export function shownAt(updatedAt: number, now: number): string {
  const t = new Date(updatedAt), n = new Date(now);
  if (t.getFullYear() === n.getFullYear() && t.getMonth() === n.getMonth()
      && t.getDate() === n.getDate()) {
    const h = t.getHours();
    const mm = String(t.getMinutes()).padStart(2, "0");
    return `${h % 12 || 12}:${mm} ${h < 12 ? "AM" : "PM"}`;
  }
  const day = `${MONTH_ABBR[t.getMonth()]} ${t.getDate()}`;
  return t.getFullYear() === n.getFullYear() ? day
    : `${day}, ${t.getFullYear()}`;
}

/** The one line above data kept from before a refetch that failed. Only
 *  the phone calls a request with no answer offline. */
export function staleLine(e: unknown, updatedAt: number,
                          client: StatesClient, now: number): string {
  const when = updatedAt > 0
    ? `what was here at ${shownAt(updatedAt, now)}.`
    : "what was here before.";
  return (client === "phone" && noAnswer(e) ? "Offline" : "Couldn't refresh")
    + " — showing " + when;
}

/** Whether a failed read is asked again on its own: only a failure that
 *  can heal — no answer at all, a 5xx, a 408 — and at most three times.
 *  Any other 4xx is the server's answer, and asking again only delays it;
 *  a 429 included, since every refused request counts as a strike toward
 *  the address ban. */
export function retryRead(failures: number, e: unknown): boolean {
  if (failures >= 3) return false;
  // each attempt abandoned at the read limit already cost the whole
  // limit: one more, then the failed line with its "Try again"
  if (timedOut(e)) return failures < 1;
  const s = failureStatus(e);
  return s === null || s >= 500 || s === 408;
}

/** The wait before automatic attempt n + 1: 1 s, 2 s, 4 s, capped at 8 s. */
export function retryReadDelay(n: number): number {
  return Math.min(1000 * 2 ** n, 8000);
}

/** The figure a screen may print from a read: the picked value once the
 *  read has an answer on screen, else null, which prints as
 *  UNKNOWN_FIGURE. That answer may be the last one kept after a refetch
 *  failed, or the previous filter's standing in while the next loads;
 *  the screen greys both (figureGreyed). The replacement for
 *  `q.data?.x ?? 0`, which reads a read that never answered as zero. */
export function knownOr<D, T>(q: { data?: D },
                              pick: (d: D) => T | null | undefined): T | null {
  if (q.data === undefined) return null;
  const v = pick(q.data);
  return v === undefined ? null : v;
}

/** A figure is greyed while it is not this read's current answer: the
 *  last one kept after the latest attempt failed, or a previous filter's
 *  standing in while this one loads. */
export function figureGreyed(q: { data?: unknown; isError: boolean;
                                  isPlaceholderData?: boolean }): boolean {
  if (q.data === undefined) return false;
  return q.isError || !!q.isPlaceholderData;
}
/** The empty lines: one short line per screen, the same words on both
 *  clients, with at most one action beside it. A filtered list that
 *  answered empty always says `filtered` and offers nothing — the way out
 *  is the filter just used; `noAccounts` with the `connect` action is
 *  offered only when the household has no accounts at all. */
export const EMPTY_LINES = {
  filtered: "Nothing matches these filters.",
  noAccounts: "No accounts linked yet.",
  connect: "Connect or import",
  monthNoTransactions: "No transactions this month.",
  range: "None in this range.",
  noSpending: "No spending.",
  noSpendingPosted: "No spending posted.",
  spendingRange: "No spending in this range.",
  incomeRange: "No income in this range.",
  businessBooks: "No business transactions yet.",
  merchants: "No merchants yet.",
  imports: "No file imports yet.",
  lineItems: "No parsed line items yet.",
  lineItemsAction: "Add a receipt",
  reimburse: "Nothing waiting to be paid back.",
  reimburseAction: "Go to Transactions",
  assistantOff: "The assistant needs a language model, and none is set up.",
  assistantAction: "Set one up in Settings",
  bills: "No bills or income tracked yet.",
  billsAction: "Find bills & income",
} as const;

/** The household has no accounts at all: only from an accounts read that
 *  answered, never from one that failed or has not answered yet. */
export function noAccountsYet(q: { data?: { accounts?: unknown[] } }): boolean {
  return q.data !== undefined && Array.isArray(q.data.accounts)
    && q.data.accounts.length === 0;
}
/** How long a read waits for the server's first word before it is
 *  abandoned (specs/screen-states.md, "A read that never answers"). */
export const READ_LIMIT_MS = 30_000;

/** Whether a request only asks: a GET or a HEAD, fetch's default included. */
export function isRead(method?: string | null): boolean {
  const m = (method || "GET").toUpperCase();
  return m === "GET" || m === "HEAD";
}

/** Whether a failed read is one this client abandoned at the limit. */
export function timedOut(e: unknown): boolean {
  return (e as { timedOut?: unknown } | null | undefined)?.timedOut === true;
}

// the outer signal each answered read is still tied to, until its body
// has been read (readFinished)
const openReads = new WeakMap<object, () => void>();

/** `send` under the read limit. A read with no answer after `ms` is
 *  cancelled and fails as a request that got no answer does — a TypeError
 *  with no status, marked `timedOut` — so the failed line, the stale line
 *  and the phone's offline gate treat it as the dead connection it looks
 *  like, and never as a sign-out or a cancelled request. The limit ends
 *  when `send` resolves, which for fetch is the response's headers: a
 *  long body or a stream is not cut off. `outer` (the caller's or the
 *  account epoch's signal) still cancels the read, before the answer and
 *  while its body is read; hand the answer to readFinished once that is
 *  done. Signals are combined by hand: AbortSignal.any is not on every
 *  engine the clients run on.
 *
 *  A write is sent with `outer` untouched and is never timed: abandoning
 *  it would leave its outcome unknown and invite a second attempt that
 *  doubles it, and uploads and the assistant's answers run long. */
export async function fetchRead<R extends object>(
    send: (signal: AbortSignal | undefined) => Promise<R>,
    method: string | null | undefined,
    outer: AbortSignal | null | undefined,
    ms: number = READ_LIMIT_MS): Promise<R> {
  if (!isRead(method)) return send(outer ?? undefined);
  const ctl = new AbortController();
  let expired = false;
  const cut = () => ctl.abort();
  if (outer?.aborted) ctl.abort();
  else outer?.addEventListener("abort", cut);
  const unlink = () => outer?.removeEventListener("abort", cut);
  const timer = setTimeout(() => {
    // an outer abort already ended it: that is not a timeout
    if (ctl.signal.aborted) return;
    expired = true;
    ctl.abort();
  }, ms);
  try {
    const r = await send(ctl.signal);
    openReads.set(r, unlink);
    return r;
  } catch (e) {
    unlink();
    if (!expired) throw e;
    throw Object.assign(new TypeError("no answer in time"), { timedOut: true });
  } finally {
    clearTimeout(timer);
  }
}

/** The answer fetchRead returned has been read to its end (or dropped):
 *  stop watching the outer signal for it. Anything else is ignored. */
export function readFinished(r: object | null | undefined): void {
  if (!r) return;
  const unlink = openReads.get(r);
  if (!unlink) return;
  openReads.delete(r);
  unlink();
}

// ---- end of the screen-states twin block ----

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
/** The next link poll's wait: fast for a while after a wake-up (the app
 *  came back to the foreground — the bank step just ended), else the
 *  normal tick. The web's plaidlinks.pollWait. */
export const LINK_POLL_MS = 2500;
export const LINK_POLL_FAST_MS = 1000;
export const LINK_BURST_MS = 30_000;
export function linkPollWait(now: number, burstUntil: number): number {
  return now < burstUntil ? LINK_POLL_FAST_MS : LINK_POLL_MS;
}

/** The pending row's words for a session still open (the web's
 *  plaidlinks.pendingLine). */
export function pendingLinkLine(kind: "add" | "relink" | "fix",
                                cancelled: boolean): string {
  const head = kind === "fix" ? "Finishing the sign-in at your bank…"
    : kind === "relink" ? "Relinking at your bank for more history…"
    : "Finish signing in at your bank…";
  return cancelled ? `${head} (closing)` : head;
}

export function linkPollMayRetry(e: unknown): boolean {
  const status = (e as { status?: unknown } | null)?.status;
  if (typeof status !== "number") return true;
  return status < 400 || status >= 500 || status === 408 || status === 429;
}

/** True when the household has no checking account the cash runway can
 *  start from (card-only or savings-only). The runway says so outright;
 *  an older payload is read by its null checking balance. Cash ahead is
 *  then unknown, not $0, so no "short" or "tight" line may be drawn from
 *  it (and the server sends no forecast for such a household). (web:
 *  Today.tsx noChecking) */
export function noCheckingLinked(
  runway: { checking: number | null; no_checking?: boolean } | null
    | undefined,
): boolean {
  return !!runway?.no_checking || (!!runway && runway.checking == null);
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

/** The line a finished add earns: the server's sentence for a session
 *  that came back with several banks, printed as it is; else the one-bank
 *  line. The web's plaidLinkOutcome. */
export function addedLine(r: { institution?: string; first_sync?: string;
                               summary?: string }): string {
  if (r.summary !== undefined) return r.summary;
  // older-server path: stands in for `summary`
  return linkedLine(r.institution, r.first_sync);
}

/** The line under the add-a-bank buttons: the server's, printed as it is
 *  (how many more banks, and whether several can be connected in one
 *  sign-in). The web's BankLinkLine. */
export function bankLinkLine(me: { features?: { bank_link_line?: string }
                                   | null } | undefined | null
): string | null {
  const v = me?.features?.bank_link_line;
  if (v !== undefined) return v || null;
  // older-server path: stands in for `bank_link_line` — a server that
  // sends none asks Plaid for one bank per sign-in, so nothing is said
  return null;
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
 *  start-up there), else the stored class. A control that showed
 *  "Operating" for a start-up cost could not send Operating: the choice
 *  would already be selected. Money that moved is in no bucket and shows
 *  none. */
export function bizShownBucket(
  t: { bucket: string | null; effective_bucket?: string | null },
): string {
  return t.effective_bucket ?? t.bucket ?? "";
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

/** The calendar day it is where the household lives, as YYYY-MM-DD. The
 *  server answers "today" and "this month" on the household's clock
 *  (/api/me `timezone`), so a device in another zone must read the same
 *  clock: between London's midnight and Los Angeles's, a browser in LA
 *  would otherwise call the household's yesterday "today" and its new
 *  month the future. Before /api/me answers, or with a zone this runtime
 *  cannot read, the device clock stands in. */
export function householdYmd(at: Date, zone?: string): string {
  if (zone) {
    try {
      const parts = new Intl.DateTimeFormat("en-US", {
        timeZone: zone, year: "numeric", month: "numeric", day: "numeric",
      }).formatToParts(at);
      const n = (t: string) => Number(parts.find((p) => p.type === t)?.value);
      const y = n("year"), m = n("month"), d = n("day");
      if (Number.isInteger(y) && Number.isInteger(m) && Number.isInteger(d))
        return `${y}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
    } catch { /* an unknown zone name: read the device clock instead */ }
  }
  return `${at.getFullYear()}-${String(at.getMonth() + 1).padStart(2, "0")}-`
    + String(at.getDate()).padStart(2, "0");
}

/** The household's day as a device-local midnight Date, for the helpers
 *  that take a Date (getFullYear, getMonth, date arithmetic). */
export function householdDay(at: Date, zone?: string): Date {
  const [y, m, d] = householdYmd(at, zone).split("-").map(Number);
  return new Date(y, m - 1, d);
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
// The Alerts screen shows what needs attention first and keeps the rest —
// resolved rows, dismissed ones — behind a history toggle: three live
// alerts at the top of a sixty-row log were the three things the page is
// for, and they read as three more rows. `active` is what needs a person
// now; `history` is every row, for the toggle's count and its list.
// The alert log stores no link, so an alert's own target (a drift alert's
// /bills/history?payee=…) exists only on the live alert Today carries. An
// active log row takes the link of the live alert with the same kind and
// message; anything else falls back to the kind's page.
export function withLiveLinks<
    T extends { kind: string; message: string; active: number | boolean;
                link?: string | null }>(
    rows: T[], live: { kind: string; message: string;
                       link?: string | null }[] | undefined): T[] {
  if (!live?.length) return rows;
  return rows.map((r) => {
    if (r.link || !r.active) return r;
    const hit = live.find((a) => a.kind === r.kind && a.message === r.message);
    return hit?.link ? { ...r, link: hit.link } : r;
  });
}
export type AlertLike = { active: number | boolean; dismissed: number | boolean };
export function alertSections<T extends AlertLike>(rows: T[]): {
  active: T[]; dismissed: T[]; resolved: T[]; history: T[];
} {
  const active = rows.filter((r) => !!r.active && !r.dismissed);
  const dismissed = rows.filter((r) => !!r.active && !!r.dismissed);
  const resolved = rows.filter((r) => !r.active);
  return { active, dismissed, resolved, history: rows };
}

// The history toggle's label: how many rows it hides, so the count on the
// header ("60 resolved") and the toggle never disagree.
export function historyToggleLabel(shown: boolean, count: number): string {
  if (shown) return "Hide history";
  return count === 1 ? "Show history (1 alert)" : `Show history (${count} alerts)`;
}

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

/** Whether this login's theme is the household's (settings.theme, saved
 *  from the picker and adopted at launch) rather than this phone's alone.
 *  The web's rule (App.tsx perDeviceTheme): anyone who may change data,
 *  owner or member, follows the household; a viewer's save would be
 *  refused and the demo refuses every settings write, so both keep a
 *  per-device choice, as does a login not yet known. */
export function themeFollowsHousehold(
    me: { role?: string | null; demo?: boolean | null } | null | undefined,
): boolean {
  return !!me && !!me.role && me.role !== "viewer" && !me.demo;
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
  + "it can't change who is in this household or how they are reached, "
  + "send to its members, or add script tokens, support access or "
  + "outbound destinations.";
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
 *  older server. A member's view carries only their own entry of the
 *  roster, so it also carries `listConfigured` (a list is set) and
 *  `onList` (that list, split as the worker splits it, names them); when
 *  present they decide, and an owner's view or an older server falls back
 *  to the roster. A member on the list gets one status row for their own
 *  mailbox however the entry was stored: `mailed` is the mailer's answer
 *  and `not_mailed` why it says no (`held`, or the invitation's state). */
export function myDailyLine(x: {
  me: string; on: boolean; muted: boolean;
  prefs?: Partial<Record<string, boolean>> | null;
  verified?: boolean | null; hosted?: boolean;
  recipients?: string[] | null;
  listConfigured?: boolean | null; onList?: boolean | null;
  status?: { email: string; verified?: boolean | null;
             invite?: string | null; delivery?: string | null;
             mailed?: boolean | null; not_mailed?: string | null }[]
           | null;
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
  const listed = x.listConfigured != null ? x.listConfigured
    : !!x.recipients?.length;
  if (x.status && listed) {
    if (x.onList === false || !row)
      return no("The owner's mail list doesn't include you.");
    // a member's own row carries the mailer's verdict and, when it does
    // not mail them, why; an older server's row has only the invitation
    const state = row.mailed == null ? row.invite
      : row.mailed ? "accepted" : row.not_mailed ?? row.invite;
    if (state === "held")
      return no("Held until the owner confirms the mail list.");
    if (state !== "accepted") {
      if (x.needBaseUrl)
        return no("Your invitation can't be sent until the server has a "
          + "public address (OIKONOME_BASE_URL).");
      if (state === "invited")
        return no("Accept the invitation in your inbox to start receiving it.");
      if (state === "declined")
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
  // an account-security change opens the screen where it can be reviewed
  // and undone; Alerts has nothing to show for it
  if (event === "security") return "/security";
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

/** Where a push tap goes once the account's state is known. Security
 *  notices travel as their own "security" event, but a server whose relay
 *  predates it sends them as the textless "alert" a budget alert uses, so
 *  that event cannot say which one it was. A recovery that would
 *  strip this account's two-factor is the notice that can still be undone,
 *  and only the Security screen can cancel it — so while one is pending,
 *  an alert tap opens Security instead of Alerts. */
export function pushTapRoute(route: string,
                             me: { factor_reset_pending?: string | null }
                               | null | undefined): string {
  return route === "/alerts" && me?.factor_reset_pending ? "/security"
    : route;
}

/** The line Today and Alerts show while a two-factor recovery is pending
 *  on this account, or null when none is. `lands` is an instant; `fmt`
 *  renders the LOCAL day it falls on the way the screen dates everything
 *  else — its UTC day is tomorrow for a household west of Greenwich. */
export function pendingRecoveryLine(lands: string | null | undefined,
                                    fmt: (iso: string) => string)
    : string | null {
  if (!lands) return null;
  return "Someone asked for a recovery that removes this account's "
    + `two-factor methods. It lands ${fmt(localDayOf(lands))}. Not you? `
    + "Cancel it in Security.";
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
// older-server path: stands in for `heading`
export const windowLabel = (w: { label: string; live_month?: boolean;
                                  heading?: string }) =>
  // the server's heading ("Nov 1–17 so far"); an older server sends none
  w.heading || (w.live_month ? `${w.label} so far` : w.label);

/** The window runs to today (the month so far): no per-month pace beside
 *  it. An older server says so only for a first-month ledger. */
// older-server path: stands in for `so_far`
export const windowSoFar = (w: { live_month?: boolean; so_far?: boolean }) =>
  w.so_far ?? !!w.live_month;

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
  // lines already stored (left as they were or updated in place); absent
  // on an older server, where `imported` counted them too
  matched?: number;
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

/** The plan's Savings line with nothing of a goal's own yet — no account,
 *  no target, no date, no words — which the goals card collapses to its
 *  account picker. One that carries a target, a date or words keeps the
 *  full editor, so those stay readable, editable and removable. */
export const isPlanOnlySavingsGoal = (r: {
  name: string; target: string; target_date: string; account_id: string;
  tokens: string;
}): boolean =>
  isPlanSavingsGoal(r.name) && !r.account_id && !(Number(r.target) > 0)
  && !r.target_date && !r.tokens.split(",").some((t) => t.trim() !== "");

/** A typed take-home by the server's separator rules ("12,50" is
 *  twelve-fifty, "1,234.56" a thousands comma). Blank is 0; text that is
 *  not a number is NaN — fine for a preview, never for a save: NaN
 *  travels as JSON null (see mergeIncomeScenarios). The web's twin is in
 *  webapp/src/incomescenarios.ts. */
export const takeHomeOf = (v: string): number =>
  v.replace(/[$\s]/g, "") === "" ? 0 : parseMoneyDraft(v) ?? NaN;

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
 * same way the server rejects duplicates. A take-home typed here that is
 * not an amount throws, naming the row, so the editor shows it and
 * nothing is sent: NaN would reach the server as JSON null. The web
 * client applies the same rule; keep the two in step.
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
    const take_home = takeHomeOf(r.take_home);
    if (!Number.isFinite(take_home))
      throw new Error(`${name}: "${r.take_home.trim()}" is not an amount — `
                      + "type the take-home like 2,100.50.");
    out.push({ name, take_home, cadence: r.cadence });
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

const choiceDate = (iso: string | null) => {
  if (!iso || iso.length < 10) return "";
  const [y, m, d] = iso.slice(0, 10).split("-");
  return `${m}/${d}/${y.slice(2)}`;
};

/** The line under each account a refusal or a plan row asks the person
 *  to choose: what tells two cards sharing their last four apart when
 *  their names read the same — the web Import page's choiceDetail. */
export const choiceDetail = (c: AccountChoice) =>
  [`${c.rows} row${c.rows === 1 ? "" : "s"}`,
   c.first_date ? `${choiceDate(c.first_date)}–${choiceDate(c.last_date)}`
     : "",
   c.balance_cents === null ? ""
     : `balance ${money(c.balance_cents / 100, true)}`]
    .filter(Boolean).join(" · ");

/** The destination sheet's [id, label] options for a plan row the server
 *  offered `choices` on: "name …mask — " and the line that tells two
 *  cards sharing their last four apart, plus the id only when even that
 *  reads the same — the web Import page's planChoiceOptions. */
export function describedChoiceOptions(choices: AccountChoice[]
                                       ): [string, string][] {
  const label = (c: AccountChoice) =>
    `${c.name}${c.mask ? ` …${c.mask}` : ""} — ${choiceDetail(c)}`;
  const seen = new Map<string, number>();
  for (const c of choices) seen.set(label(c), (seen.get(label(c)) ?? 0) + 1);
  return choices.map((c) => [c.id, (seen.get(label(c)) ?? 0) > 1
    ? `${label(c)} (${c.id})` : label(c)]);
}

/** Plan rows that would import with no destination: an imported file
 *  (not a whole-instance export) naming neither an existing account nor a
 *  new one — a row whose `choices` nobody has picked from yet. The server
 *  refuses the whole plan while one is left, so Import waits for it. */
export function planUnplaced<T extends {
  action: string; kind: string; account_id: string | null;
  new_account_name: string }>(files: T[]): T[] {
  return files.filter((f) => f.action === "import"
    && f.kind !== "Oikonome export ZIP"
    && !f.account_id && !(f.new_account_name ?? "").trim());
}

// ---- copying a one-time secret (recovery codes, tokens, signing secrets) ----
// The clipboard is outside the screen's FLAG_SECURE protection, and
// expo-clipboard cannot mark a clip sensitive (no Android
// EXTRA_IS_SENSITIVE, no iOS localOnly/expiry), so the most the app can do
// is take the secret back off once it has had time to be pasted. That is
// best effort: Android pauses JS timers in the background and refuses
// clipboard reads from an app without focus, so the clear runs on the first
// of the timer firing in the foreground, the app coming back to the
// foreground, or the screen being left — once the minute has passed. If the
// app never comes back (or the OS kills it) the clip stays.
export const SECRET_CLIPBOARD_TTL_MS = 60_000;

export type SecretClipboard = {
  set: (text: string) => Promise<unknown>;
  // absent where reading the clipboard would prompt the user: the clear
  // must know the clip is still ours, so without a reader it never runs
  get?: () => Promise<string>;
};

/** The one pending clear of a copied secret. It is cleared only if the
 *  clipboard still holds exactly that value, so something copied since is
 *  kept. A read that fails or comes back empty (Android without focus)
 *  keeps it pending for the next chance; a later copy replaces it. */
export class SecretClipboardClear {
  private pending: { id: number; text: string; due: number;
                     ops: SecretClipboard & { get: () => Promise<string> } }
                   | null = null;
  private seq = 0;
  // bumped by every copy as it is dispatched, before it lands: a clear
  // whose read straddles a copy must not write after it
  private copies = 0;
  private readonly now: () => number;

  constructor(now: () => number = Date.now) {
    this.now = now;
  }

  /** A copy is about to be dispatched: any clear in flight stands down. */
  copying(): void {
    this.copies += 1;
  }

  /** Record a landed copy; returns the id its own timer expires. */
  arm(ops: SecretClipboard & { get: () => Promise<string> }, text: string,
      ttlMs: number): number {
    this.seq += 1;
    this.pending = { id: this.seq, text, due: this.now() + ttlMs, ops };
    return this.seq;
  }

  /** The copy's own timer fired: its minute is up by definition. */
  expire(id: number): Promise<void> {
    return this.pending?.id === id ? this.run() : Promise.resolve();
  }

  /** Any other chance to clear: runs only once the minute has passed. */
  sweep(): Promise<void> {
    const p = this.pending;
    return p && this.now() >= p.due ? this.run() : Promise.resolve();
  }

  /** Wire to AppState "change": the return to the foreground. */
  appStateChanged(state: string): Promise<void> {
    return state === "active" ? this.sweep() : Promise.resolve();
  }

  /** Wire to the secret screen's unmount. */
  released(): Promise<void> {
    return this.sweep();
  }

  private async run(): Promise<void> {
    const p = this.pending;
    if (!p) return;
    const copies = this.copies;
    let current: string;
    try {
      current = await p.ops.get();
    } catch {
      return;  // refused read: try again at the next chance
    }
    if (current === "") return;  // focusless Android reads as empty
    // a newer copy took over, or was dispatched, while the read was out
    if (this.pending !== p || this.copies !== copies) return;
    this.pending = null;
    if (current !== p.text) return;  // the user copied something since
    try {
      await p.ops.set("");
    } catch {
      // best effort: a refused write leaves the clip
    }
  }
}

/** Copy `text`; true when the copy landed. Never throws — a refused copy
 *  is the caller's cue to leave the secret on screen. When it can read the
 *  clipboard back, it arms `clear` and schedules its timer (see
 *  SecretClipboardClear for the other moments the clear runs). */
export async function copySecretWith(
  ops: SecretClipboard, text: string,
  schedule: (fn: () => Promise<void>, ms: number) => void =
    (fn, ms) => { setTimeout(() => { void fn(); }, ms); },
  ttlMs: number = SECRET_CLIPBOARD_TTL_MS,
  clear: SecretClipboardClear = new SecretClipboardClear(),
): Promise<boolean> {
  clear.copying();
  try {
    if ((await ops.set(text)) === false) return false;
  } catch {
    return false;
  }
  const get = ops.get;
  if (get) {
    const id = clear.arm({ set: ops.set, get }, text, ttlMs);
    schedule(() => clear.expire(id), ttlMs);
  }
  return true;
}

/** The notice after a change the server pairs with signing every other
 *  session out (turning 2FA on or off, replacing the authenticator). */
export function signedOutOthers(lead: string): string {
  return `${lead} — every other session was signed out.`;
}

/** The digits of a 6-digit one-time code (authenticator or SMS) as typed or
 *  pasted. Authenticators and password managers copy the code grouped
 *  ("123 456"), so a raw six-character field cap would cut the paste to
 *  "123 45" before it ever reached the server. Every one-time-code field
 *  runs its input through this instead of capping the raw text. */
export function oneTimeCodeDigits(raw: string): string {
  return raw.replace(/\D/g, "").slice(0, 6);
}


/** A bill scan refused because another pass is running (409): the list
 *  is waiting on that pass, not empty. */
export const SCAN_RUNNING_RETRY_MS = 20_000;
export function scanAlreadyRunning(e: unknown): boolean {
  const st = (e as { status?: number } | null)?.status;
  const m = e instanceof Error ? e.message : String(e ?? "");
  return st === 409 || /already running/i.test(m);
}

// ---- the retirement walkthrough's saved progress ----
// Each step is saved as it is answered — its mark in ret_wizard_steps and
// its own answers in ret_wizard_answers — so a reload or another device
// resumes where the person stopped instead of starting over with blank
// boxes. The web wizard carries a twin of these in RetireWizard.tsx.

/** The range the server holds each walkthrough answer to (the settings
 *  save refuses anything outside it, and the projection clamps age at
 *  the same top), as [lowest, highest]. The checks and the boxes' hints
 *  read it, and the pure-logic suite compares it with the server's table,
 *  so a bound cannot drift on one side. Nothing is clamped against it. */
export const RET_WIZARD_BOUNDS: Record<string, [number, number]> = {
  age: [18, 90],
  end: [19, 110],
  spend: [0, 100_000_000],
  employer_mo: [0, 10_000_000],
  roth_mo: [0, 10_000_000],
  taxable_mo: [0, 10_000_000],
  ret: [-20, 30],
  infl: [-20, 30],
  ssage: [62, 70],
};

export const RET_WIZARD_STEPS =
  ["age", "spend", "saving", "assumptions", "done"] as const;
export type RetWizardStep = typeof RET_WIZARD_STEPS[number];
export type RetWizardMarks = Record<string, string | null> | null | undefined;
export type RetWizardKey = "age" | "spend" | "employer_mo" | "roth_mo"
  | "taxable_mo" | "ret" | "infl" | "end" | "ssage";
export type RetWizardAnswers =
  Partial<Record<RetWizardKey | "answers_v2", unknown>>;
const RET_WIZARD_KEYS: readonly RetWizardKey[] =
  ["age", "spend", "employer_mo", "roth_mo", "taxable_mo", "ret", "infl",
   "end", "ssage"];

/** the answers each question owns, in the order it asks them */
export const RET_STEP_ANSWERS: Record<RetWizardStep, readonly RetWizardKey[]> = {
  age: ["age"], spend: ["spend"],
  saving: ["employer_mo", "roth_mo", "taxable_mo"],
  assumptions: ["ret", "infl", "end", "ssage"], done: [],
};
const RET_MONEY = new Set<RetWizardKey>(["spend", "employer_mo", "roth_mo",
                                         "taxable_mo"]);

/** The step to open on: the first question with no saved mark, or the
 *  summary once all are answered. A finished or skipped walkthrough that
 *  is opened again starts from the top, with its answers prefilled. */
export function retWizardResumeAt(marks: RetWizardMarks): number {
  if (!marks || marks.done) return 0;
  const i = RET_WIZARD_STEPS.findIndex((k) => k !== "done" && !marks[k]);
  return i === -1 ? RET_WIZARD_STEPS.length - 1 : i;
}

/** Started and not finished. The first answer saves the birthdate, and
 *  "no birthdate yet" is what offers the walkthrough on a first visit —
 *  without this, a person who stopped after step one never saw it again. */
export function retWizardMidway(marks: RetWizardMarks): boolean {
  return !!marks && !marks.done
    && RET_WIZARD_STEPS.some((k) => k !== "done" && !!marks[k]);
}

/** One step's save: its mark and EVERY box on it, as the person's answers
 *  (walking the wizard is answering it; each box shows what the projection
 *  would use, so leaving it as it is confirms it). A cleared box is the
 *  server's default, so it clears the saved answer (null); text that is not
 *  a number does the same instead of being stored as $0. `ret_answers_v: 2`
 *  marks the answers as typed into this walkthrough, so the projection may
 *  read them; it, the claim age and the Roth saving ride BESIDE
 *  `ret_wizard_answers`, as top-level fields an older server ignores (it
 *  refuses a whole save over an answer key it does not know). */
export function retWizardStepBody(
  step: RetWizardStep, draft: Partial<Record<RetWizardKey, string>>,
): Record<string, unknown> {
  const answers: Record<string, number | null> = {};
  let ssage: number | null | undefined;
  let roth: number | null | undefined;
  for (const k of RET_STEP_ANSWERS[step]) {
    const t = (draft[k] ?? "").trim();
    const n = t === "" ? null : RET_MONEY.has(k) ? parseMoneyDraft(t) : Number(t);
    const v = n !== null && Number.isFinite(n) ? n : null;
    if (k === "ssage") ssage = v;
    // a draft with no Roth box (a phone on an older server) leaves the
    // stored Roth answer alone; a blank Roth box is an answered $0, so
    // "answered nothing" and "never asked" stay apart
    else if (k === "roth_mo") { if (draft[k] !== undefined) roth = v ?? 0; }
    else answers[k] = v;
  }
  const out: Record<string, unknown> = { ret_wizard_steps: { [step]: "done" } };
  if (Object.keys(answers).length || ssage !== undefined
      || roth !== undefined) {
    out.ret_answers_v = 2;
    if (Object.keys(answers).length) out.ret_wizard_answers = answers;
    if (ssage !== undefined) out.ret_wizard_ssage = ssage;
    if (roth !== undefined) out.ret_wizard_roth_mo = roth;
  }
  return out;
}

/** The wizard's boxes: an answer a current client saved (listed in the
 *  answers' `answers_v2`), else what the projection used, else blank. An
 *  answer an older client saved — it stored every prefilled box — is
 *  never shown: it is not the household's answer. */
export function retWizardPrefill(
  saved: RetWizardAnswers | null | undefined,
  defaults: Partial<Record<RetWizardKey, number | null>>,
  splitSeed = false,
): Record<RetWizardKey, string> {
  const out = {} as Record<RetWizardKey, string>;
  const marked = saved?.answers_v2;
  const has = (k: string) => Array.isArray(marked) && marked.includes(k);
  // The projection's figures can be a what-if, so a saved answer wins.
  // One exception: a saved tax-deferred figure with no Roth answer beside
  // it may hold the Roth saving, and when the server says its figures are
  // that saved total split by the ledger (`splitSeed`: the reply carries
  // `saving_hint`), both saving boxes open on them, so saving them stores
  // a split of the same total (specs/retirement-figures.md). A saved block
  // with no Roth answer otherwise opens its Roth box on the $0 it is
  // projected as, never on a what-if's Roth figure.
  const seeded = splitSeed && typeof defaults.roth_mo === "number"
    && !has("roth_mo");
  const savedSaving = typeof defaults.roth_mo === "number" && !has("roth_mo")
    && (has("employer_mo") || has("taxable_mo"));
  for (const k of RET_WIZARD_KEYS) {
    const v = seeded && (k === "employer_mo" || k === "roth_mo") ? undefined
      : has(k) ? saved?.[k]
      : k === "roth_mo" && savedSaving ? 0
      : undefined;
    const d = defaults[k];
    out[k] = typeof v === "number" && Number.isFinite(v) ? String(v)
      : typeof d === "number" && Number.isFinite(d) ? String(d) : "";
  }
  return out;
}

/** The sentence a finished restore or connections-bundle import adds when
 *  it did not keep a stored credential: an AI key or a mail password is
 *  never carried onto an address the file moved, so it is not sent to a
 *  new destination. `dropped` is the server's `credentials_dropped` (a
 *  list of fixed names); absent, empty or unknown names say nothing. The
 *  phone and the web carry the same words (mobile/src/lib/pure.ts,
 *  webapp/src/moneymath.ts). */
export function droppedCredentialsNote(dropped: unknown): string | null {
  if (!Array.isArray(dropped)) return null;
  const says: [string, string][] = [
    ["ai_key", "The AI service's address changed, so its key was not "
      + "kept. Enter it again under Settings → AI."],
    ["mail_password", "The mail server changed, so its password was not "
      + "kept. Enter it again under Settings → Email & Push."]];
  const out = says.filter(([k]) => dropped.includes(k)).map(([, t]) => t);
  return out.length ? out.join(" ") : null;
}

/** An import's count line, without its closing period. `imported` is the
 *  rows the import ADDED and `matched` the lines already stored (left as
 *  they were, or updated in place); an older server sends no `matched`
 *  and gets exactly the old words. `skipped_duplicates` (lines another
 *  source's row already covers) closes a single upload's line; a
 *  folder's per-file line (`"file"`) carries neither the verb nor that
 *  tail, as it never has. Rows older than the history window are their
 *  own note on both screens, so `skipped_older` is never said here. The
 *  phone and the web carry the same words (mobile/src/lib/pure.ts,
 *  webapp/src/moneymath.ts). */
export function importCountText(
  r: { imported?: number | null; matched?: number | null;
       skipped_duplicates?: number | null; skipped_older?: number | null },
  kind: "upload" | "file" = "upload",
): string {
  const added = r.imported ?? 0;
  const here = r.matched ?? 0;
  const head = here > 0
    ? (added > 0 ? `Imported ${added} new, ${here} already here`
                 : `Nothing new: ${here} already here`)
    : kind === "file" ? `${added} transactions`
      : `Imported ${added} transactions`;
  const covered = kind === "upload" && r.skipped_duplicates
    ? ` · skipped ${r.skipped_duplicates} already covered` : "";
  return head + covered;
}

// ---- months with no bank feed ----
// Cash Flow's month series carry only the months whose income is known: a
// month no bank account posted in (a broken link, a missing statement) is
// left out by the server rather than drawn as a loss. A chart must still
// keep that month's place, or March sits beside July and the gap reads as
// time that never passed. These put each missing month back as a null —
// drawn as an empty, labelled slot ("no bank feed").
/** Fill missing 'YYYY-MM' keys between consecutive points with null. */
export function fillMonthKeyGaps(
  points: [string, number][],
): [string, number | null][] {
  const out: [string, number | null][] = [];
  const next = (k: string) => {
    const y = Number(k.slice(0, 4)), m = Number(k.slice(5, 7));
    return m === 12 ? `${y + 1}-01` : `${y}-${String(m + 1).padStart(2, "0")}`;
  };
  for (const p of points) {
    const prev = out.length ? out[out.length - 1][0] : null;
    if (prev !== null && /^\d{4}-\d{2}$/.test(prev)
        && /^\d{4}-\d{2}$/.test(p[0])) {
      // bounded: a malformed or reversed pair never loops
      for (let k = next(prev), n = 0; k < p[0] && n < 1200; k = next(k), n++) {
        out.push([k, null]);
      }
    }
    out.push(p);
  }
  return out;
}

const MONTH_ABBRS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul",
                     "Aug", "Sep", "Oct", "Nov", "Dec"];

/** One year's bars, labelled 'Jan'..'Dec': fill the missing months between
 *  the first and the last bar with null. */
// older-server path: stands in for `months_by_year`
export function fillMonthLabelGaps(
  points: [string, number][],
): [string, number | null][] {
  const at = new Map(points.map((p) => [p[0], p[1]] as const));
  const idx = points.map((p) => MONTH_ABBRS.indexOf(p[0]));
  if (!points.length || idx.some((i) => i < 0)) return points;
  const out: [string, number | null][] = [];
  for (let i = Math.min(...idx); i <= Math.max(...idx); i++) {
    const m = MONTH_ABBRS[i];
    out.push([m, at.has(m) ? at.get(m)! : null]);
  }
  return out;
}

/** The ages the "Retire at each age" table opens on: today's age, the
 *  earliest age that works and the one just before it (where the answer
 *  turns), the age the stress test is read at, every fifth birthday, and
 *  the last age the plan looks at. Every other age is one tap away.
 *  Mirrored in webapp/src/keyages.ts. */
export function keyRetireAges(ages: readonly number[],
                              earliest: number | null | undefined,
                              refAge?: number | null): Set<number> {
  const keep = new Set<number>();
  if (!ages.length) return keep;
  keep.add(ages[0]);
  keep.add(ages[ages.length - 1]);
  if (earliest != null) { keep.add(earliest); keep.add(earliest - 1); }
  if (refAge != null) keep.add(refAge);
  for (const a of ages) if (a % 5 === 0) keep.add(a);
  return new Set(ages.filter((a) => keep.has(a)));
}

/** The tone of a script source's status on the Doctor screen: the web's
 *  pill (Doctor.tsx) — fresh green, stale amber, and anything else (an
 *  unwatched source is "off") plain, because a source nobody expects to
 *  push has not failed. The row carries `status` and nothing else. */
export type ScriptTone = "good" | "warn" | "plain";

export function scriptStatusTone(status: string): ScriptTone {
  return status === "fresh" ? "good" : status === "stale" ? "warn" : "plain";
}

/** The Accounts screen's institution cards: one per bank LOGIN, as the
 *  web groups them, so each card can print the subtotal the server sends
 *  for that login (`shown.by_item`). Two logins at one bank are two cards
 *  under the same name; an account an older server sends without its
 *  login falls back to its bank's name. Sorted by name, then as listed. */
export function groupByLogin<A extends { item_id?: string | null;
                                         institution_name?: string | null }>(
  list: A[]): { key: string; inst: string; list: A[] }[] {
  const g = new Map<string, { key: string; inst: string; list: A[] }>();
  for (const a of list) {
    const key = a.item_id ?? a.institution_name ?? "Other";
    const cur = g.get(key);
    if (cur) cur.list.push(a);
    else g.set(key, { key, list: [a],
                      inst: a.institution_name || a.item_id || "Other" });
  }
  return [...g.values()].sort((x, y) =>
    x.inst.toLowerCase().localeCompare(y.inst.toLowerCase()));
}

/** The budget planner's "where each dollar goes" shares: each positive
 *  part's whole-percent share of `income`, by largest remainder, exactly
 *  as the server's reporting.shown_shares(parts, base=income) decides
 *  them: the parts' shares add up to their own share of the income in
 *  whole percent (100 when they are all of it, halves to even), each the
 *  whole below its exact share or the one above, the points still missing
 *  going to the largest remainders (earlier parts first on a tie). Null
 *  for a part that is not positive, and for every part when there is no
 *  income. This stays on the client because it echoes figures still
 *  being typed; one case table (mobile/scripts/share-cases.json) runs
 *  through this, its twin and the server's function. */
export function plannerShares(parts: readonly number[],
                              income: number): (number | null)[] {
  // cents as Python's round() takes them: halves to even
  const cent = (v: number) => {
    const x = v * 100, f = Math.floor(x), d = x - f;
    return d > 0.5 ? f + 1 : d < 0.5 ? f : f % 2 === 0 ? f : f + 1;
  };
  const cents = parts.map(cent);
  const out: (number | null)[] = parts.map(() => null);
  const drawn = cents.map((c, i) => [c, i] as const).filter(([c]) => c > 0);
  const den = cent(income);
  // a value typed past what a number holds draws nothing, never a hang
  if (!drawn.length || !(den > 0) || !Number.isFinite(den)
      || drawn.some(([c]) => !Number.isFinite(c))) return out;
  const whole = drawn.reduce((t, [c]) => t + c, 0);
  const q = Math.floor((100 * whole) / den), r = 100 * whole - q * den;
  const target = q + (2 * r > den || (2 * r === den && q % 2 === 1) ? 1 : 0);
  const nums = drawn.map(([c]) => 100 * c);
  const base = nums.map((n) => Math.floor(n / den));
  const order = nums.map((n, j) => [n - base[j] * den, j] as const)
    .sort((a, b) => b[0] - a[0] || a[1] - b[1]).map(([, j]) => j);
  let k = target - base.reduce((t, b) => t + b, 0);
  for (let i = 0, steps = Math.abs(k); i < steps; i++) {
    const j = k > 0 ? order[i % order.length]
                    : order[order.length - 1 - (i % order.length)];
    base[j] += k > 0 ? 1 : -1;
    k += k > 0 ? -1 : 1;
  }
  drawn.forEach(([, i], j) => { out[i] = base[j]; });
  return out;
}

/** How a server change (reporting.change: the shown figure minus the
 *  shown figure) reads: nothing without one (no earlier period, or an
 *  older server that sends none); "unchanged" for $0; otherwise its sign,
 *  whole dollars, whether it is the good way (spend going down, income
 *  going up — the caller says which), and its percent, left out past
 *  ±1000% where it says nothing a reader can use. Prints, never derives. */
export type ChangeLook =
  | { unchanged: true }
  | { unchanged: false; up: boolean; sign: "+" | "−"; dollars: number;
      pct: number | null; good: boolean };
export function changeLook(
  ch: { amount: number; pct: number | null; unchanged: boolean }
    | null | undefined,
  downIsGood: boolean): ChangeLook | null {
  if (!ch) return null;
  if (ch.unchanged) return { unchanged: true };
  const up = ch.amount > 0;
  return { unchanged: false, up, sign: up ? "+" : "−",
           dollars: Math.abs(ch.amount),
           pct: ch.pct !== null && Math.abs(ch.pct) < 1000
             ? Math.abs(ch.pct) : null,
           good: downIsGood ? !up : up };
}

/** A money-map row link's filters as route params. The server writes the
 *  link as a path ("/transactions?bucket=Food&y=2026&m=3"), the tiles as
 *  a bare query ("bucket=Food&y=2026&m=3"); either reads the same. A
 *  bucket is named by the household, so its name can contain "=" — each
 *  pair splits on its FIRST one only, or "Rent = home" loses its tail —
 *  and a bare "%" in it would make decodeURIComponent throw, so the raw
 *  text is kept then. */
export function ledgerParams(to: string): Record<string, string> {
  const q = to.indexOf("?");
  const query = q < 0 ? to : to.slice(q + 1);
  const params: Record<string, string> = {};
  for (const kv of query.split("&")) {
    if (!kv) continue;
    const eq = kv.indexOf("=");
    const k = eq < 0 ? kv : kv.slice(0, eq);
    const raw = eq < 0 ? "" : kv.slice(eq + 1);
    let v = raw;
    try { v = decodeURIComponent(raw); } catch { v = raw; }
    params[k] = v;
  }
  return params;
}

/** The caption a money-map row prints under its bar — the web's
 *  terseCaption (MoneyMap.tsx): the "· not counted in the verdict" tail
 *  is dropped, since which rows carry a pace tick already says it. The
 *  Bills row's caption ("posted $P · plan $Z") is printed when the reply
 *  carries the row's `breakdown` key (null on a past day, whose caption
 *  is printed too, as on the web).
 *  older-server path: stands in for `breakdown` — no Bills caption, the
 *  old one restated the words on its right. */
export function terseCaption(label: string,
                             caption: string | null | undefined,
                             breakdown?: unknown)
  : string | null {
  if (!caption) return null;
  if (label === "Bills" && breakdown === undefined) return null;
  const c = caption.replace(/\s*·\s*not counted in the verdict\s*$/, "");
  return c || null;
}

/** The pages a paged log keeps on screen, by the question they answer.
 *  `key` names the filter, `cursor` the page within it (null = the
 *  first). A first page for a filter replaces what was held; a later page
 *  replaces itself and anything after it (a refetch of a page already
 *  held never appends a copy). A page for another filter than the one
 *  held is never merged into it. */
export function pagesAfter<P>(held: { key: string; pages: { cursor: string | null; page: P }[] },
                              key: string, cursor: string | null, page: P):
    { key: string; pages: { cursor: string | null; page: P }[] } {
  if (cursor === null || held.key !== key)
    return { key, pages: [{ cursor, page }] };
  const at = held.pages.findIndex((x) => x.cursor === cursor);
  const kept = at >= 0 ? held.pages.slice(0, at) : held.pages;
  return { key, pages: [...kept, { cursor, page }] };
}

// ---- the connection, as far as the app can tell (phone only) ----
// No native network module: the app reads what its own requests already
// say. A request that got no answer at all (the shared failed-read cause
// noAnswer) means offline; any answer — a 5xx and a refusal included —
// means the phone reached the server. specs/screen-states.md.

/** What a write button says beside itself while the phone is offline. */
export const OFFLINE_WRITE = "You're offline.";

/** Whether the phone is offline once one request settled: `e` null is an
 *  answer; no answer at all is offline; an answer with a status is
 *  online; anything else (an aborted request, a client-side throw) says
 *  nothing about the connection, so the last state stands. */
export function offlineAfter(wasOffline: boolean, e: unknown): boolean {
  if (e === null) return false;
  if (noAnswer(e)) return true;
  return failureStatus(e) !== null ? false : wasOffline;
}

// ---- the offline copy: which reads the phone keeps on disk ----
// The encrypted persisted cache is the offline read layer: what the next
// launch shows before the network answers, under "Offline — showing what
// was here at 9:41 AM.". It holds each screen's landing view only — never
// every month, year or search paged through, which a week of gcTime would
// grow into a blob re-encrypted on every cache event.

/** The reads kept whole, by the first part of their key. */
export const OFFLINE_KEYS: ReadonlySet<string> = new Set([
  "today", "bills", "accounts", "alerts", "me", "settings", "onboarding",
  "connections", "calendar", "categories", "report", "holdings",
  "account-links", "assistant-status", "reimburse", "help"]);

/** Whether a successful read is kept on disk for offline: the keys above,
 *  the Month lens for the current month and the Year lens for the current
 *  year, and the Transactions tab's opening page (the current month, no
 *  search, filter, bucket or page). `now` is the household's day. */
export function keptOffline(key: readonly unknown[], now: Date): boolean {
  const head = String(key[0]);
  if (OFFLINE_KEYS.has(head)) return true;
  const y = now.getFullYear(), m = now.getMonth() + 1;
  if (head === "lens")
    return (key[1] === "month" && key[2] === y && key[3] === m)
      || (key[1] === "year" && key[2] === y && key.length === 3);
  if (head !== "transactions" || key.length < 2) return false;
  const p = key[1];
  if (!p || typeof p !== "object") return false;
  const q = p as Record<string, unknown>;
  if (q.q || q.page || q.bucket || q.acct || q.cat) return false;
  return Number(q.y) === y && Number(q.m) === m;
}


/** The planner's "where each dollar goes" figures when what it shows IS
 *  the saved plan — nothing typed away from it: the server's plan line in
 *  the whole dollars and shares every other surface prints
 *  (budget.plan_line), so the planner never says $6,001 under a header
 *  saying $6,000, or 24 % where the plan says 25. Null while the figures
 *  being typed differ from the plan (then the live figures show), and
 *  without a plan line. */
export function plannerAtRest(
  pl: { income: number; bills: number; to_spend: number;
        saved: number | null; excess: number;
        shares?: { bills: number | null; to_spend: number | null;
                   saved: number | null; excess: number | null } } | null | undefined,
  live: { income: number; out: number; saved: number; excess: number }):
    { income: number; out: number; saved: number; excess: number;
      outPct: number; savePct: number; overPct: number } | null {
  if (!pl || !pl.shares) return null;
  const near = (a: number, b: number, tol: number) => Math.abs(a - b) <= tol;
  if (!near(live.income, pl.income, 1) || !near(live.out, pl.bills + pl.to_spend, 2)
      || !near(live.saved, pl.saved ?? 0, 1) || !near(live.excess, pl.excess, 1))
    return null;
  const sh = pl.shares;
  return { income: pl.income, out: pl.bills + pl.to_spend,
           saved: pl.saved ?? 0, excess: pl.excess,
           outPct: (sh.bills ?? 0) + (sh.to_spend ?? 0),
           savePct: sh.saved ?? 0, overPct: sh.excess ?? 0 };
}

/** Approve-all's toast. An offer that failed because what it offered is
 *  already tracked (`superseded`) was withdrawn, not left pending: it is
 *  not counted as one that "stayed pending". Web twin: `approveAllWords`
 *  in webapp/src/moneymath.ts. */
export function approveAllWords(r: {
  approved: number;
  failed: { pid: string; error: string; superseded?: boolean }[];
}): string {
  const stayed = r.failed.filter((f) => !f.superseded);
  const gone = r.failed.length - stayed.length;
  const head = stayed.length
    ? `Approved ${r.approved}; ${stayed.length} could not be applied `
      + `and stayed pending (${stayed.map((f) => f.error).join("; ")})`
    : `Approved ${gone ? "" : "all "}${r.approved} proposal${r.approved === 1 ? "" : "s"}`;
  return head + (gone
    ? `; ${gone} withdrawn — already tracked` : "");
}

/** A change as the pages print it: "unchanged" when it rounds to $0, else
 *  signed whole dollars ("+$120", "−$45"), with its tone. Web twin:
 *  `changeWords` in webapp/src/moneymath.ts. */
export function changeWords(v: number, money: (n: number) => string):
    { text: string; tone: "pos" | "neg" | "mut" } {
  if (Math.abs(v) < 0.5) return { text: "unchanged", tone: "mut" };
  return v > 0 ? { text: `+${money(v)}`, tone: "pos" }
               : { text: `−${money(-v)}`, tone: "neg" };
}

/** How wide a bar chart's x label is drawn, from its longest text: a bare
 *  month needs `floor`, a month with its year more. Web twin:
 *  `netBarsLabelWidth` in webapp/src/moneymath.ts. */
export function netBarsLabelWidth(labels: string[], charW: number,
                                  floor: number): number {
  return Math.max(floor, ...labels.map((l) => l.length * charW + 6));
}

/** Whether a bar chart writes each labelled bar's amount on it: only while
 *  a bar is wide enough to carry one (about 6px of bar, a slot of 9.7). On
 *  hundreds of thin bars the amounts land on their neighbours. Web twin:
 *  `netBarsShowValues` in webapp/src/moneymath.ts. */
export function netBarsShowValues(slot: number): boolean {
  return slot * 0.62 >= 6;
}

/** A percent with its sign, the minus a true minus ("+2.5%", "−7.0%") —
 *  the sign the dollar change beside it wears (`changeWords`), so one line
 *  never carries two kinds of minus. Web twin: `signedPct` in
 *  webapp/src/moneymath.ts. */
export function signedPct(p: number, digits = 1): string {
  return `${p >= 0 ? "+" : "−"}${Math.abs(p).toFixed(digits)}%`;
}

/** A dollar figure that may be negative, the minus a true minus
 *  ("−$12"), and a plus on a gain when `plus`. Web twin: `signedMoney`
 *  in webapp/src/moneymath.ts. */
export function signedMoney(v: number, money: (n: number) => string,
                            plus = false): string {
  return v < 0 ? `−${money(-v)}` : `${plus && v > 0 ? "+" : ""}${money(v)}`;
}

/** A net's sign in the whole dollars money() prints it (halves to even):
 *  1, 0 or −1. A net's tone, its words and whether a rate is printed
 *  beside it read this, never the cents: a year that lost under a dollar
 *  prints $0 and is even, neither green nor red, with no rate. Twin:
 *  `netSign` in webapp/src/moneymath.ts. */
export function netSign(v: number): -1 | 0 | 1 {
  const f = Math.floor(v), d = v - f;
  const r = d > 0.5 ? f + 1 : d < 0.5 ? f : f % 2 === 0 ? f : f + 1;
  return r > 0 ? 1 : r < 0 ? -1 : 0;
}

/** "Nov '25" for a "YYYY-MM" key — a month named with its year. Web
 *  twin: `monthWithYear` in webapp/src/moneymath.ts. */
export function monthWithYear(key: string): string {
  const MON = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug",
               "Sep", "Oct", "Nov", "Dec"];
  return `${MON[Number(key.slice(5, 7))] ?? key} '${key.slice(2, 4)}`;
}

/** How many days one cycle of a bill lasts, from its cadence value
 *  ("MONTHLY:1", "WEEKLY:2" — the row's `cadence_value`, never the human
 *  label): what the Bills cycle bar measures the days to the due date
 *  against. 0 for a cadence with no cycle (an envelope) or none sent.
 *  Twin: webapp/src/moneymath.ts ↔ mobile/src/lib/pure.ts. */
export function billCycleDays(cad: string | null | undefined): number {
  const [freq, n] = (cad ?? "").split(":");
  const iv = parseInt(n, 10) || 1;
  const per = freq === "DAILY" ? 1 : freq === "WEEKLY" ? 7
    : freq === "MONTHLY" ? 30.44 : freq === "YEARLY" ? 365.25 : 0;
  return per * iv;
}


/** The Bills row's "How this adds up" table as the phone draws it
 *  (components/bills-table.tsx; the web twin is webapp's BillsTable.tsx):
 *  the server's rows as sent, the total marked, and the late bills below
 *  it under their heading — or null when the reply carries no table (an
 *  older server, or any row but Bills), so no button is drawn. Nothing
 *  here adds or words a figure. */
export interface BillsTableLine { label: string; figure: string; total: boolean }
interface SignedRow { label: string; figure: string; sign: string }
interface PoolLine { label: string; text: string }
interface PoolFigure { label: string; figure: string }
export function billsTableLines(breakdown: {
  items?: unknown;
  table?: {
    rows: (SignedRow | PoolLine)[];
    outside_heading?: string | null;
    outside: SignedRow[] | PoolFigure[] | null;
    total?: PoolFigure;
  } | null;
} | null | undefined): {
  rows: BillsTableLine[]; heading: string | null; outside: BillsTableLine[];
  toggle: [string, string];
} | null {
  const t = breakdown?.table;
  if (!t) return null;
  if (breakdown && "items" in breakdown) {
    // the Envelopes row's list of pools (envelope_breakdown.table): each
    // pool's line as sent, the line for pools used outside the plan's
    // months, and the plan's set-aside — a list, not a sum
    const rows: BillsTableLine[] = (t.rows as PoolLine[]).map((r) =>
      ({ label: r.label, figure: r.text, total: false }));
    // on the Year, why a pool did not draw all of it, by cause
    for (const o of (t.outside as PoolFigure[] | null) ?? [])
      rows.push({ label: o.label, figure: o.figure, total: false });
    if (t.total) rows.push({ label: t.total.label, figure: t.total.figure,
                             total: true });
    return { rows, heading: null, outside: [],
             toggle: ["Each pool", "Hide the pools"] };
  }
  const line = (r: SignedRow) =>
    ({ label: r.label, figure: r.figure, total: r.sign === "=" });
  const outside = (t.outside as SignedRow[] | null) ?? [];
  return { rows: (t.rows as SignedRow[]).map(line),
           heading: outside.length ? t.outside_heading ?? null : null,
           outside: outside.map(line),
           toggle: ["How this adds up", "Hide how this adds up"] };
}

// ---- flagged-business unflag patch: twin block, byte-identical in
// webapp/src/moneymath.ts and mobile/src/lib/pure.ts ----

/** One cached view of the flagged-on-personal-cards worksheet after `row`
 *  was unflagged. `year` is the view's scope (undefined or "all": every
 *  year). The view's `count` and `total` cover its whole scope, not only
 *  the rows fetched, so a view of another year is returned as it was —
 *  none of its rows changed — and a view whose scope holds the row loses
 *  it from both, even when the fetch cap had left it out of `rows`.
 *  A row comes off a view once: a second success for it (two taps, two
 *  requests) finds it gone from `rows` — or, past the fetch cap, listed
 *  in the view's own `unflagged` — and returns the view as it was; so
 *  does a view whose rows are its whole scope and do not hold it. A
 *  refetch replaces the view, and with it that list. */
export function bizViewWithout<V extends {
  rows: { id: string }[]; count: number; total: number;
}>(view: V, year: number | "all" | undefined,
   row: { id: string; date: string; amount: number }): V {
  if (year !== undefined && year !== "all"
      && Number(row.date.slice(0, 4)) !== year) return view;
  const done = (view as { unflagged?: string[] }).unflagged ?? [];
  const fetched = view.rows.some((r) => r.id === row.id);
  if (!fetched && (done.includes(row.id)
                   || view.count <= view.rows.length)) return view;
  return { ...view, rows: view.rows.filter((r) => r.id !== row.id),
           count: Math.max(0, view.count - 1),
           total: view.total - row.amount,
           unflagged: [...done, row.id] };
}
// ---- end of the flagged-business unflag patch twin block ----
