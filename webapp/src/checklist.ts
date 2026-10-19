// The Today page's first-run checklist — ONE definition, read by the
// checklist card, by the wizard's Done step (so what the card will say
// is shown before the wizard is left) and by Today's decision to show
// the card at all. Each step is a fact about the household's data, never
// a wizard mark: a step the wizard was walked past with nothing done
// stays open here, which is the point. Mobile keeps the same list in
// components/wizard-logic.ts (checklistSteps).
import type { Onboarding } from "./api/client";

export type ChecklistStep = {
  key: "accounts" | "history" | "bills" | "budgets" | "checking";
  done: boolean; title: string; text: string; to: string; label: string;
};

/** A household with no checking account at all (cards or savings only)
 *  has nothing to pin, so the step must not hold the checklist open
 *  forever. An older server that omits the candidates is not known to
 *  have none: there only the pin counts. */
function checkingDone(data: Onboarding): boolean {
  if (data.primary_checking_set) return true;
  return Array.isArray(data.checking_candidates)
    && data.checking_candidates.length === 0;
}

export function checklistSteps(data: Onboarding, hosted?: boolean):
  ChecklistStep[] {
  return [
    {
      key: "accounts",
      done: data.accounts > 0,
      title: "Connect your bank — or add an account",
      // hosted connects through Plaid — a hosted account has no way to use
      // SimpleFIN, so it must never be pointed at one
      text: hosted
        ? "Sign in to your bank once and transactions, balances and cards flow in automatically. Prefer not to connect? A manual account plus file imports works completely."
        : "SimpleFIN pulls everything automatically (hourly by default). No aggregator? A manual account plus file imports works completely.",
      to: "/accounts", label: "Accounts",
    },
    {
      key: "history",
      done: data.transactions > 0,
      title: "Bring in your history",
      text: "CSV, OFX/QFX, QIF, PDF statements, or Mint / YNAB / Monarch / Copilot / Simplifi exports. The more history, the smarter the bill detection.",
      to: "/import", label: "Import",
    },
    {
      key: "bills",
      done: data.bills > 0,
      title: "Find your recurring bills",
      text: "One click proposes every bill and paycheck it can prove from your ledger — approve what's right, reject what isn't.",
      to: "/bills", label: "Bills & Income",
    },
    {
      key: "budgets",
      done: data.budgets_set,
      title: "Set your two budgets",
      text: "Food and everything-else, monthly. That's all the verdict needs to tell you every morning whether you're on track.",
      // the Budget page owns the plan — Settings has no budgets editor
      to: "/budget", label: "Budget",
    },
    // with two or more checking accounts the cash forecast guesses the
    // largest balance until one is pinned; the Today card offers the pick
    // under this step (Onboarding.tsx), Accounts keeps the switch
    {
      key: "checking",
      done: checkingDone(data),
      title: "Pick your primary checking",
      text: "The cash forecast anchors on it — runway, low point and the timeline all read from this one account. Tap the one your bills and paychecks run through.",
      to: "/accounts", label: "Accounts",
    },
  ];
}

/** Is any step still open? Today shows the card while one is. */
export function checklistOpen(data: Onboarding): boolean {
  return checklistSteps(data).some((s) => !s.done);
}

/** The Done step's one-line account of the checklist: "5/5 done", or
 *  what is still open, by name, so the person leaves the wizard knowing
 *  what the Today page will ask next. */
export function checklistSummary(data: Onboarding): string {
  const steps = checklistSteps(data);
  const open = steps.filter((s) => !s.done);
  if (!open.length) return `${steps.length}/${steps.length} done`;
  return `${steps.length - open.length}/${steps.length} done — still open: `
    + open.map((s) => s.title.replace(/ — .*$/, "")).join(", ");
}

/** The wizard's bills step leaves either done or, with proposals still
 *  pending, skipped — so the Today checklist and the wizard's own marks
 *  agree. The button says which. */
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
