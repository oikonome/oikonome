// Dependency-free money logic shared by the pages that type or total
// money. It imports nothing at runtime, so the server suite can execute
// it under plain node next to its mobile twin.
import type { CustomBucket } from "./api/client";

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

/** the pages' loose amount-field parser: "$1,234.56 " → 1234.56,
 *  "12,50" → 12.5; anything that is not a number reads as 0 */
const numv = (v: string) =>
  Number(normaliseMoneyText(v || "0") ?? "0") || 0;

// ---- money drafts, signed balances, the planner's bucket merge ----
// Mobile keeps a twin of each in mobile/src/lib/pure.ts; the server
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

// The bank feed's "type/subtype" pair, said the way a statement would:
// "depository/money market" is a money market, "credit/credit card" a
// credit card. Phone twin: `kindWords` in mobile/src/lib/pure.ts.
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
 *  Fabrikam) read "Fabrikam Fabrikam (daily)". Phone twin: `accountLabel`
 *  in mobile/src/lib/pure.ts. */
export function accountLabel(a: { institution_name?: string | null;
                                  name: string; mask?: string | null }): string {
  const inst = (a.institution_name ?? "").trim();
  const name = (a.name ?? "").trim();
  const said = inst && name.toLowerCase().startsWith(inst.toLowerCase());
  return `${inst && !said ? `${inst} ` : ""}${name}${a.mask ? ` ${a.mask}` : ""}`;
}

/** Which of the account editor's two budget switches an account is
 *  offered. The SERVER decides (the accounts reply's `can_pin` and
 *  `can_exclude`): the exclusion wherever a row on the account can be
 *  spending, by the predicate the budget itself reads, and the forecast
 *  anchor on the type the forecast picks among. A switch that is already
 *  ON is always offered, so it can be turned off; a server too old to say
 *  gets both switches, never a hidden one. Phone twin:
 *  `accountSwitches` in mobile/src/lib/pure.ts. */
export function accountSwitches(a: { can_pin?: boolean; can_exclude?: boolean;
                                     pinned: boolean; excluded: boolean }):
    { primary: boolean; excluded: boolean } {
  return { primary: a.pinned || (a.can_pin ?? true),
           excluded: a.excluded || (a.can_exclude ?? true) };
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

/** An email-schedule edit laid over the schedule stored NOW. The server
 *  replaces each cadence a body names, and the copy on screen may be
 *  older than what another device saved since, so only the fields this
 *  edit changed (mine against base) move. The edited cadence is always
 *  sent. The phone's twin is mobile/src/lib/schedule.ts rebaseSched. */
export function rebaseSchedule<S extends object>(
  mine: S, base: S, live: object, edited?: keyof S & string,
): S {
  type R = Record<string, object>;
  const out = rebaseKeyed<object>({ ...(mine as R) }, { ...(base as R) },
                                  { ...(live as R) });
  const m = mine as R;
  if (edited && !out[edited] && m[edited]) out[edited] = m[edited];
  return out as S;
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

// ---- reimbursements, the category pickers, local dates, the device zone ----
// Mobile keeps a twin of each in mobile/src/lib/pure.ts under the same
// name; the shared case table runs against both.

// Banker's rounding, the same as api/client.ts's pyround; copied so this
// module stays free of imports (plain node runs the shared cases over it).
const pyround = (v: number) => {
  const f = Math.floor(v), diff = v - f;
  if (diff > 0.5) return f + 1;
  if (diff < 0.5) return f;
  return f % 2 === 0 ? f : f + 1;
};

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

/** "Everything else" before the person edits it: the variable pool less
 *  the carve-outs, less the savings typed in THIS session. A Savings plan
 *  that was already saved sits beside other_monthly, not inside it (the
 *  saved other_monthly is already net of it), so only the change from the
 *  saved plan comes out of the pool — taking the whole plan out again on
 *  every reopen would cut the stored budget by that much on each save.
 *  Twin: plannerUnallocAuto in mobile/src/lib/planner.ts. */
export function plannerUnallocAuto(o: {
  otherBase: number; catsTotal: number; savingsTouched: boolean;
  savingsTyped: number; savingsSeed: number;
}): number {
  const taken = o.savingsTouched ? o.savingsTyped - o.savingsSeed : 0;
  return Math.max(0, Math.round(o.otherBase - o.catsTotal - taken));
}

/** The planner's preview of the month's excess and savings while values
 *  are typed but not yet saved: the server's `plan_surplus` (income −
 *  bills − planned spending − EVERY goal's monthly plan) and
 *  `savings_plan`, each moved by the difference between what is typed and
 *  what is saved — the planned spending (`plan_variable`), the goal the
 *  planner owns (only when the excess counts it), and a typed income
 *  (only while income is typed). With nothing typed it is exactly the
 *  server's figure. With no server figure yet (no income saved) and an
 *  income box to type in, the preview is the typed income − the server's
 *  bills − the typed planned spending − every goal's plan; with no box,
 *  there is none. Twin: plannerPreview in mobile/src/lib/planner.ts. */
export function plannerPreview(o: {
  planSurplus: number | null; savingsPlan: number;
  savedVariable: number; typedVariable: number;
  savedGoal: number; typedGoal: number; goalCounted: boolean;
  savedIncome: number; typedIncome: number;
  avgBills: number; incomeTyped: boolean;
}): { excess: number | null; saved: number } {
  const goal = o.goalCounted ? o.typedGoal - o.savedGoal : 0;
  const saved = o.savingsPlan + goal;
  if (o.planSurplus === null)
    return { excess: o.incomeTyped
               ? o.typedIncome - o.avgBills - o.typedVariable - saved
               : null,
             saved };
  return {
    excess: o.planSurplus - (o.typedVariable - o.savedVariable) - goal
      + (o.typedIncome - o.savedIncome),
    saved,
  };
}

/** The savings goal the budget planner owns. Goal names are unique
 *  case-insensitively on the server, so a goal typed as "savings" in the
 *  Savings goals card IS the plan's Savings row; an exact-spelling match
 *  would miss it, append a second "Savings", and have every planner save
 *  refused as a duplicate. Mobile mirrors this predicate. */
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

/** The planner's line under suggestions taken from under a month of
 *  history: how many days the figures come from, and that they were
 *  scaled to a month. Under the server's settled threshold
 *  (min_rate_days) the rate is a few days' own, so the line says to
 *  expect it to move.
 *  With no finished day of ledger at all it says to come back tomorrow.
 *  null when complete months stand behind the suggestions. Twin:
 *  historyBasisNote in mobile/src/lib/pure.ts. */
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

/** The words after a narrowed month's listed spending: the server's
 *  `listed_scope` says whose spending it is — a business's when every
 *  listed row is a business's, else the household's. Shared with the
 *  phone (mobile/src/lib/pure.ts). */
export const listedSpendWord = (scope?: string | null) =>
  scope === "business" ? "spent (business)" : "spent";


/** A bill scan refused because another pass is running (409): the list
 *  is not "no proposals", it is waiting on that pass. */
export const SCAN_RUNNING_RETRY_MS = 20_000;
export function scanAlreadyRunning(e: unknown): boolean {
  const m = e instanceof Error ? e.message : String(e ?? "");
  return /^409\b/.test(m) || /already running/i.test(m);
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
 *  not counted as one that "stayed pending". Phone twin: `approveAllWords`
 *  in mobile/src/lib/pure.ts. */
export function approveAllWords(r: {
  approved: number;
  failed: { pid: string; error: string; superseded: boolean }[];
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
 *  signed whole dollars ("+$120", "−$45"), with its tone. Phone twin:
 *  `changeWords` in mobile/src/lib/pure.ts. */
export function changeWords(v: number, money: (n: number) => string):
    { text: string; tone: "pos" | "neg" | "mut" } {
  if (Math.abs(v) < 0.5) return { text: "unchanged", tone: "mut" };
  return v > 0 ? { text: `+${money(v)}`, tone: "pos" }
               : { text: `−${money(-v)}`, tone: "neg" };
}

/** How wide a bar chart's x label is drawn, from its longest text: a bare
 *  month needs `floor`, a month with its year more. Phone twin:
 *  `netBarsLabelWidth` in mobile/src/lib/pure.ts. */
export function netBarsLabelWidth(labels: string[], charW: number,
                                  floor: number): number {
  return Math.max(floor, ...labels.map((l) => l.length * charW + 6));
}

/** Whether a bar chart writes each labelled bar's amount on it: only while
 *  a bar is wide enough to carry one (about 6px of bar, a slot of 9.7). On
 *  hundreds of thin bars the amounts land on their neighbours. Phone twin:
 *  `netBarsShowValues` in mobile/src/lib/pure.ts. */
export function netBarsShowValues(slot: number): boolean {
  return slot * 0.62 >= 6;
}

/** A percent with its sign, the minus a true minus ("+2.5%", "−7.0%") —
 *  the sign the dollar change beside it wears (`changeWords`), so one line
 *  never carries two kinds of minus. Phone twin: `signedPct` in
 *  mobile/src/lib/pure.ts. */
export function signedPct(p: number, digits = 1): string {
  return `${p >= 0 ? "+" : "−"}${Math.abs(p).toFixed(digits)}%`;
}

/** A dollar figure that may be negative, the minus a true minus
 *  ("−$12"), and a plus on a gain when `plus`. Phone twin: `signedMoney`
 *  in mobile/src/lib/pure.ts. */
export function signedMoney(v: number, money: (n: number) => string,
                            plus = false): string {
  return v < 0 ? `−${money(-v)}` : `${plus && v > 0 ? "+" : ""}${money(v)}`;
}

/** A net's sign in the whole dollars money() prints it (halves to even):
 *  1, 0 or −1. A net's tone, its words and whether a rate is printed
 *  beside it read this, never the cents: a year that lost under a dollar
 *  prints $0 and is even, neither green nor red, with no rate. Twin:
 *  `netSign` in mobile/src/lib/pure.ts. */
export function netSign(v: number): -1 | 0 | 1 {
  const f = Math.floor(v), d = v - f;
  const r = d > 0.5 ? f + 1 : d < 0.5 ? f : f % 2 === 0 ? f : f + 1;
  return r > 0 ? 1 : r < 0 ? -1 : 0;
}

/** "Nov '25" for a "YYYY-MM" key — a month named with its year. Phone
 *  twin: `monthWithYear` in mobile/src/lib/pure.ts. */
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
