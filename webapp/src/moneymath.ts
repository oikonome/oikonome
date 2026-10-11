// Dependency-free money logic shared by the pages that type or total
// money. It imports nothing at runtime, so the server suite can execute
// it under plain node next to its mobile twin.
import type { CustomBucket } from "./api/client";

/** the pages' loose amount-field parser: "$1,234.56 " → 1234.56 */
const numv = (v: string) =>
  Number((v || "0").replace(/[$,\s]/g, "")) || 0;

// ---- money drafts, signed balances, the planner's bucket merge ----
// Mobile keeps a twin of each in mobile/src/lib/pure.ts; the server
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

// ---- reimbursements, the category pickers, local dates, the device zone ----
// Mobile keeps a twin of each in mobile/src/lib/pure.ts under the same
// name; the shared case table runs against both.

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

/** Interest a plan saves against paying only the minimums, or 0 when there
 *  is no honest figure. A baseline that never pays off stops at the
 *  simulation horizon, so its interest is a truncation total, not a cost. */
export function interestSaved(
  base: { debt_free: string | null; total_interest: number },
  plan: { total_interest: number },
): number {
  return base.debt_free ? base.total_interest - plan.total_interest : 0;
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

/** The money map's Savings row figure for a month. `month` is what was
 *  posted toward the plan inside the month, when the payload carries it;
 *  without it the only figure is the goal's trailing 90-day average, and
 *  the row must say that is what it shows rather than call it this
 *  month's transfers. A plan-only goal (saved === null) stays null.
 *  Twin: monthSavings in mobile/src/lib/planner.ts. */
export function monthSavings(
  goal: { monthly_plan: number; rate_90d: number | null },
  month?: number | null,
): { plan: number; saved: number | null; averaged: boolean } {
  if (typeof month === "number" && Number.isFinite(month))
    return { plan: goal.monthly_plan, saved: month, averaged: false };
  return { plan: goal.monthly_plan, saved: goal.rate_90d,
           averaged: goal.rate_90d !== null };
}
