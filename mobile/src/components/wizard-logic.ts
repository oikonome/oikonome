// The setup wizard's decisions that need no screen: the step list, the
// history pills, the Adjust form's diff and the Done step's summary
// lines. Kept free of React Native so plain node can test them
// (scripts/pure-logic-tests.mjs) — the words here are the web wizard's,
// and a test pins each one.
import { cadenceWords, money, numv, parseMoneyDraft } from "../lib/pure.ts";

export type WizardStep =
  "appearance" | "connect" | "bills" | "budgets" | "email" | "finish";

// Six steps on every instance. Mail delivery is a choice every household
// makes (email, push, who receives it), so the email step is no longer a
// self-host-only SMTP detour; a self-hosted instance only adds one line
// about its outbound mail account. MUST mirror the web's step keys — the
// done/skipped marks are shared, and a wizard started in a browser
// resumes here at the same step.
export const WIZARD_STEPS: WizardStep[] =
  ["appearance", "connect", "bills", "budgets", "email", "finish"];

export type ThemeChoice = "auto" | "light" | "dark";
export const THEME_LABEL: Record<ThemeChoice, string> =
  { auto: "Auto", light: "Light", dark: "Dark" };

/** The Appearance step's pre-selection: the theme the household pinned,
 *  or Auto when it never chose one — Auto is the default now, so a new
 *  household starts on it rather than on dark. */
export function wizardThemeChoice(stored: unknown): ThemeChoice {
  return stored === "light" || stored === "dark" ? stored : "auto";
}

export type HistoryMonths = 1 | 3 | 6 | 12 | null;

/** The Connect step's "How much history should come in?" pills, in the
 *  web's order; null is "Everything available", the default. */
export const HISTORY_CHOICES: [HistoryMonths, string][] = [
  [1, "Last month"], [3, "3 months"], [6, "6 months"], [12, "1 year"],
  [null, "Everything available"],
];

/** A stored history_months as one of the pills. Anything the pills do
 *  not offer reads as Everything, the server's own default. */
export function historyMonthsOf(v: unknown): HistoryMonths {
  return v === 1 || v === 3 || v === 6 || v === 12 ? v : null;
}

/** The pill's words mid-sentence ("2 connected · 6 months"). */
export function historyPhrase(m: HistoryMonths): string {
  const hit = HISTORY_CHOICES.find(([v]) => v === m);
  const label = hit ? hit[1] : "Everything available";
  return label.charAt(0).toLowerCase() + label.slice(1);
}

/** Is a bank's window narrower than the household's choice? A bank
 *  linked with a shorter window keeps it (Plaid fixes the window when
 *  the Item is created), so a wider choice made afterwards reaches that
 *  bank only through a relink. null = everything. The web's
 *  HistoryChoice.windowNarrower. */
export function windowNarrower(linked: HistoryMonths | undefined,
                               household: HistoryMonths | undefined): boolean {
  if (linked === null || linked === undefined) return false;
  return household === null || household === undefined || linked < household;
}

/** The row's words for a bank's own window ("history: last month"). */
export function historyRowNote(m: HistoryMonths | undefined): string {
  return `history: ${historyPhrase(m ?? null)}`;
}

/** The manual add form's cadences — the four the web's form offers, in
 *  the server's FREQ:interval wire format, never the words. */
export const MANUAL_CADENCES: [string, string][] = [
  ["MONTHLY:1", "monthly"], ["WEEKLY:1", "weekly"],
  ["WEEKLY:2", "every 2 weeks"], ["YEARLY:1", "yearly"],
];

export function ordinal(n: number): string {
  const tens = n % 100;
  if (tens >= 11 && tens <= 13) return `${n}th`;
  return `${n}${({ 1: "st", 2: "nd", 3: "rd" } as Record<number, string>)[
    n % 10] ?? "th"}`;
}

/** The day of the month a YYYY-MM-DD falls on, or null. */
export function dueDayOf(iso: string | null | undefined): number | null {
  if (!iso || !/^\d{4}-\d{2}-\d{2}/.test(iso)) return null;
  return Number(iso.slice(8, 10)) || null;
}

/** A typed due day, 1–31, or null when it is not one. */
export function parseDueDay(s: string): number | null {
  const t = s.trim();
  if (!/^\d{1,2}$/.test(t)) return null;
  const n = Number(t);
  return n >= 1 && n <= 31 ? n : null;
}

const pad = (n: number) => String(n).padStart(2, "0");

/** The next date a bill due on `day` falls on, counting today: the bill
 *  save takes a date, the form asks for a day of the month. A day the
 *  month lacks (the 31st in April) lands on the month's last day, as the
 *  server's own month rollover does. */
export function nextDueForDay(day: number, todayIso: string): string {
  const y = Number(todayIso.slice(0, 4));
  const m = Number(todayIso.slice(5, 7));
  const d = Number(todayIso.slice(8, 10));
  const last = (yy: number, mm: number) => new Date(yy, mm, 0).getDate();
  let yy = y, mm = m;
  if (Math.min(day, last(y, m)) < d) {
    mm = m === 12 ? 1 : m + 1;
    yy = m === 12 ? y + 1 : y;
  }
  return `${yy}-${pad(mm)}-${pad(Math.min(day, last(yy, mm)))}`;
}

/** A proposal's cadence as the select value the bill forms use, or null
 *  when it has none to edit (an envelope pools by month, no cycle). */
export function proposalCadenceValue(p: {
  frequency?: string | null; interval?: number | null;
  bill_type?: string | null }): string | null {
  if (!p.frequency || p.bill_type === "envelope") return null;
  const n = p.interval && p.interval > 0 ? p.interval : 1;
  return `${p.frequency.toUpperCase()}:${n}`;
}

/** Whether a bill on this cadence has a day of the month to set. Only
 *  monthly and yearly bills are anchored to one; a weekly or daily cycle
 *  repeats every so many days, which a day of the month does not pin, a
 *  one-time bill has a date rather than a day, and an envelope (or a
 *  proposal with no cadence) has no due date at all. */
export function cadenceTakesDueDay(cadence: string | null | undefined):
    boolean {
  const freq = (cadence ?? "").split(":")[0].toUpperCase();
  return freq === "MONTHLY" || freq === "YEARLY";
}

/** Whether a finder offer can be adjusted before it is approved. Only an
 *  offer to ADD a bill or income has fields of its own; a fee, merchant,
 *  removal or amount-change offer changes a bill that already exists, and
 *  the server refuses an adjust on any of them. Those keep Approve and
 *  Dismiss, as on the web. */
export function proposalAdjustable(p: { kind?: string | null }): boolean {
  return p.kind === "add";
}

/** One setting's saves, one at a time, the last tap winning.
 *
 *  Two saves of the same setting sent together race on the server: the
 *  one that commits last wins, which need not be the one tapped last, so
 *  the stored value can differ from the screen — and the history window
 *  is what a bank link's token is minted from, for good. So a tap made
 *  while a save is in flight waits behind it, and a later tap replaces
 *  any tap still waiting: at most one request is ever in flight and the
 *  last one sent is the last one tapped. `settled` runs once the queue
 *  drains, for that last value only — an earlier save's failure is moot,
 *  because the one after it rewrites the setting. */
export function latestWins<T>(
  send: (v: T) => Promise<unknown>,
  hooks: { busy?: (b: boolean) => void;
           settled?: (v: T, failed: boolean, error?: unknown) => void } = {},
): { save: (v: T) => void; pending: () => boolean } {
  let inFlight = false;
  let waiting: { v: T } | null = null;
  const run = (v: T) => {
    inFlight = true;
    hooks.busy?.(true);
    let p: Promise<unknown>;
    try { p = Promise.resolve(send(v)); } catch (e) { p = Promise.reject(e); }
    p.then(() => done(v, false), (e) => done(v, true, e));
  };
  const done = (v: T, failed: boolean, error?: unknown) => {
    inFlight = false;
    if (waiting) {
      const next = waiting.v;
      waiting = null;
      run(next);
      return;
    }
    hooks.busy?.(false);
    hooks.settled?.(v, failed, error);
  };
  return {
    save: (v: T) => { if (inFlight) waiting = { v }; else run(v); },
    pending: () => inFlight,
  };
}

export type AdjustForm = { name: string; amount: string; cadence: string;
                           dueDay: string };
export type ProposalAdjust = { name?: string; amount?: number;
                               cadence?: string; due_day?: number };

/** What the Adjust form changed, as the approve's `adjust` — only the
 *  fields the person actually moved, so an untouched field keeps the
 *  finder's own value (and its evidence) exactly. */
export function adjustOf(
  p: { payee: string; amount: number; next_due?: string | null;
       frequency?: string | null; interval?: number | null;
       bill_type?: string | null },
  f: AdjustForm,
): { adjust: ProposalAdjust } | { error: string } {
  const out: ProposalAdjust = {};
  const name = f.name.trim();
  if (!name) return { error: "Give it a name." };
  if (name !== p.payee) out.name = name;
  const amt = numv(f.amount);
  if (!(amt > 0)) return { error: "Enter an amount above zero." };
  if (Math.round(amt * 100) !== Math.round(Math.abs(p.amount) * 100))
    out.amount = Math.round(amt * 100) / 100;
  if (f.cadence && f.cadence !== proposalCadenceValue(p))
    out.cadence = f.cadence;
  // a due day the form hid for this cadence is not sent: it would move
  // the date of a bill whose schedule a day of the month does not set
  if (f.dueDay.trim()
      && cadenceTakesDueDay(f.cadence || proposalCadenceValue(p))) {
    const day = parseDueDay(f.dueDay);
    if (day === null) return { error: "Due day is a day of the month, 1–31." };
    if (day !== dueDayOf(p.next_due)) out.due_day = day;
  }
  return { adjust: out };
}

/** A bill's line in the wizard's lists: "monthly · $1,000.00 · 1st". */
export function billLine(amount: number, cadence: string,
                         due: string | null | undefined): string {
  const words = /^[A-Z_]+:\d+$/.test(cadence) ? cadenceWords(cadence)
                                               : cadence;
  const day = dueDayOf(due);
  return [words, money(Math.abs(amount)), day ? ordinal(day) : null]
    .filter(Boolean).join(" · ");
}

const plural = (n: number, one: string, many: string) =>
  `${n} ${n === 1 ? one : many}`;

/** The manual pane's tally under the added list. */
export function manualTally(bills: number, income: number): string {
  return `${plural(bills, "bill", "bills")} · ${income} income.`;
}

/** "7:00 am" — the send-at hour as the web writes it. */
export function clockHour(h: number): string {
  const hh = ((h % 24) + 24) % 24;
  const twelve = hh % 12 === 0 ? 12 : hh % 12;
  return `${twelve}:00 ${hh < 12 ? "am" : "pm"}`;
}

export type Delivery = "email" | "push" | "both" | "off";

/** The Done step's blurb: when the first message arrives, named for what
 *  was chosen — a push-only household gets a notification, not an email,
 *  and one that turned delivery off is promised nothing. */
export function finishBlurb(delivery: Delivery, hour: number): string {
  return "That's everything. The Today page carries your daily verdict "
    + "from here on"
    + (delivery === "off" ? ""
      : `; the first ${delivery === "push" ? "notification" : "email"} `
        + `arrives tomorrow at ${clockHour(hour)}`)
    + ".";
}

/** The Today screen's first-run checklist — ONE definition, read by the
 *  Today card, by the wizard's Done step (so what the card will say is
 *  shown before the wizard is left) and by Today's decision to show the
 *  card at all. Each step is a fact about the household's data, never a
 *  wizard mark: a step walked past with nothing done stays open here.
 *  The web's src/checklist.ts, same five steps in the same order. */
export type ChecklistInput = {
  accounts: number; transactions: number; bills: number;
  budgets_set: boolean; primary_checking_set: boolean;
  /** the checking accounts that could be pinned as primary; absent from
   *  an older server */
  checking_candidates?: string[];
};
export type ChecklistStep = { key: string; done: boolean; label: string;
                              href: string };
export function checklistSteps(ob: ChecklistInput): ChecklistStep[] {
  return [
    { key: "accounts", done: ob.accounts > 0,
      label: `Connect or add accounts (${ob.accounts})`, href: "/accounts" },
    { key: "history", done: ob.transactions > 0,
      label: `Bring in transactions (${ob.transactions})`, href: "/imports" },
    { key: "bills", done: ob.bills > 0,
      label: `Confirm recurring bills (${ob.bills})`, href: "/bills" },
    { key: "budgets", done: ob.budgets_set, label: "Set your budgets",
      href: "/budget" },
    // a household with no checking account has nothing to pin, and the
    // step must not hold the checklist (and the wizard) open forever
    { key: "checking",
      done: ob.primary_checking_set
        || (Array.isArray(ob.checking_candidates)
            && ob.checking_candidates.length === 0),
      label: "Pick your primary checking", href: "/accounts" },
  ];
}

/** Is any step still open? Today shows the card while one is. */
export function checklistOpen(ob: ChecklistInput): boolean {
  return checklistSteps(ob).some((s) => !s.done);
}

/** The Done step's one-line account of the checklist: "5/5 done", or
 *  what is still open, by name. */
export function checklistSummary(ob: ChecklistInput): string {
  const steps = checklistSteps(ob);
  const open = steps.filter((s) => !s.done);
  if (!open.length) return `${steps.length}/${steps.length} done`;
  return `${steps.length - open.length}/${steps.length} done — still open: `
    + open.map((s) => s.label.replace(/ \(\d+\)$/, "")).join(", ");
}

/** The wizard's bills step leaves either done or, with proposals still
 *  pending, skipped — so the Today checklist and the wizard's own marks
 *  agree. The button says which. The web's words. */
export function billsContinue(pending: number):
  { label: string; state: "done" | "skipped"; hint?: string } {
  if (pending <= 0) return { label: "Continue →", state: "done" };
  return {
    label: `Leave ${pending} pending →`, state: "skipped",
    hint: `${pending} proposal${pending === 1 ? "" : "s"} still await`
      + " your approve or dismiss — this step finishes when none are left",
  };
}

/** The connect step's way out when the primary checking is a question:
 *  with two or more eligible checking accounts and no pin, the step asks
 *  before it is left; with one, the server pins it on the way out. */
export function connectDoneHint(candidates: number, pinned: boolean):
  string | undefined {
  return candidates > 1 && !pinned
    ? "pick your primary checking below" : undefined;
}

/** Done step: the Accounts line. */
export function accountsSummary(connections: number, accounts: number,
                                history: HistoryMonths): string {
  const head = connections > 0 ? `${connections} connected`
    : accounts > 0 ? `${plural(accounts, "account", "accounts")} from files`
    : "none yet";
  return `${head} · ${historyPhrase(history)}`;
}

/** Done step: the Bills & income line — the tracked (not disabled)
 *  bills and paychecks, however they were added. */
export function billsSummary(bills: number, income: number): string {
  return `${plural(bills, "bill", "bills")}, `
    + `${plural(income, "paycheck", "paychecks")} approved`;
}

const dollars = (v: number | null | undefined) =>
  v && v > 0 ? `$${Math.round(v).toLocaleString("en-US")}` : null;

/** Done step: the Budgets line — the figures only once the household
 *  saved a budget of its own (onboarding's budgets_set); a provisional
 *  seed is the app's guess, not the person's plan. */
export function budgetsSummary(set: boolean, food: number | null | undefined,
                               other: number | null | undefined): string {
  if (!set) return "not set yet (Budget page any time)";
  return `Food ${dollars(food) ?? "$0"} · Everything else `
    + (dollars(other) ?? "$0");
}

/** The stored figures the manual budgets step reads and writes. */
export type StoredBudgets = {
  food_monthly?: number | null; other_monthly?: number | null;
  budgeted_income_monthly?: number | null;
};

/** The manual budgets step's boxes on arrival. A plan this household
 *  already has — saved here, on the Budget page, or before the wizard
 *  was restarted — comes back as it is stored; only a household with no
 *  budget starts blank. A blank box would otherwise save as $0 and wipe
 *  a figure the person could not see. */
export function manualBudgetsPrefill(
  ob: { budgets_set?: boolean; wizard_steps?: Record<string, string> },
  s: StoredBudgets,
): { income: string; food: string; other: string } {
  if (!(ob.wizard_steps?.budgets === "done" || ob.budgets_set))
    return { income: "", food: "", other: "" };
  const str = (v: number | null | undefined) => (v && v > 0 ? String(v) : "");
  return { income: str(s.budgeted_income_monthly), food: str(s.food_monthly),
           other: str(s.other_monthly) };
}

const draftNum = (v: string) => parseMoneyDraft(v) ?? 0;

/** What a box left blank (or at zero) resolves to: the stored figure
 *  when there is a budget to protect, else nothing. Without a stored
 *  budget the settings view can carry the app's provisional seed, which
 *  is not the household's and must not be counted as kept. */
export function keptBudget(stored: number | null | undefined,
                           protect: boolean): number {
  return protect && stored && stored > 0 ? stored : 0;
}

/** The refusal shown when Save is tapped with no budget in the boxes. A
 *  budget is food or everything-else above zero — the verdict's own test
 *  of "the household has one". */
export function manualBudgetsRefusal(food: string, other: string):
    string | null {
  return draftNum(food) <= 0 && draftNum(other) <= 0
    ? "Enter a food or everything-else budget, or tap a suggestion."
    : null;
}

/** The fields the manual budgets save sends, and the Savings goal's
 *  monthly plan computed from the figures that will actually stand.
 *  `hasBudget` is onboarding's budgets_set, read fresh at save time.
 *  Over a stored budget a blank or zero food/everything-else box is left
 *  out of the body so the server keeps that figure; with nothing stored
 *  there is nothing to protect and the empty one is written as none.
 *  Income at zero means "not set" to the server either way, so a blank
 *  one is always left out and a stored figure stands. */
export function manualBudgetsBody(
  draft: { income: string; food: string; other: string },
  stored: StoredBudgets, hasBudget: boolean, bills: number,
): { body: Record<string, number>; monthlyPlan: number } {
  const body: Record<string, number> = {};
  const put = (key: string, v: string, s: number | null | undefined) => {
    if (draftNum(v) > 0) { body[key] = draftNum(v); return draftNum(v); }
    if (!hasBudget) body[key] = 0;
    return keptBudget(s, hasBudget);
  };
  const inc = draftNum(draft.income) > 0 ? draftNum(draft.income)
    : keptBudget(stored.budgeted_income_monthly, true);
  if (draftNum(draft.income) > 0)
    body.budgeted_income_monthly = draftNum(draft.income);
  const f = put("food_monthly", draft.food, stored.food_monthly);
  const ot = put("other_monthly", draft.other, stored.other_monthly);
  return { body, monthlyPlan: Math.max(0, Math.round(inc - bills - f - ot)) };
}

export const DELIVERY_WORDS: Record<Delivery, string> = {
  email: "Email", push: "Push", both: "Email and push", off: "Off" };

/** Done step: the Email & push line. The owner always receives, so the
 *  count is never below one. */
export function deliverySummary(delivery: Delivery, hour: number,
                                recipients: number): string {
  return DELIVERY_WORDS[delivery]
    + (delivery === "off" ? "" : ` at ${clockHour(hour)}`)
    + ` · ${plural(Math.max(1, recipients), "recipient", "recipients")}`;
}

/** "Who gets the email": the word beside each address. */
export function recipientRole(r: { owner?: boolean; via?: string;
                                   member?: boolean;
                                   invite?: string | null }): string {
  return r.owner ? "owner"
    : r.member || r.via === "member" ? "member"
    : r.invite === "accepted" ? "accepted"
    : r.invite === "invited" ? "invitation sent"
    : r.invite ? `invitation ${r.invite}` : "added";
}
