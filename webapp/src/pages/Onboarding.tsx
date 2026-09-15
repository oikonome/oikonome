import { Link } from "react-router";
import { type Onboarding as OnboardingData } from "../api/client";

/** Guided first-run checklist, shown by Today while the ledger is empty. */
export default function Onboarding({ data, hosted }:
                                   { data: OnboardingData; hosted?: boolean }) {
  const steps: { done: boolean; title: string; text: string;
                 to: string; label: string }[] = [
    {
      done: data.accounts > 0,
      title: "Connect your bank — or add an account",
      // hosted connects through Plaid — a hosted account has no way to use
      // SimpleFIN, so it must never be pointed at one
      text: hosted
        ? "Sign in to your bank once and transactions, balances and cards flow in automatically. Prefer not to connect? A manual account plus file imports works completely."
        : "SimpleFIN pulls everything automatically (hourly). No aggregator? A manual account plus file imports works completely.",
      to: "/accounts", label: "Accounts",
    },
    {
      done: data.transactions > 0,
      title: "Bring in your history",
      text: "CSV, OFX/QFX, QIF, PDF statements, or Mint / YNAB / Monarch / Copilot / Simplifi exports. The more history, the smarter the bill detection.",
      to: "/import", label: "Import",
    },
    {
      done: data.bills > 0,
      title: "Find your recurring bills",
      text: "One click proposes every bill and paycheck it can prove from your ledger — approve what's right, reject what isn't.",
      to: "/bills", label: "Bills & Income",
    },
    {
      done: data.budgets_set,
      title: "Set your two budgets",
      text: "Food and everything-else, monthly. That's all the verdict needs to tell you every morning whether you're on track.",
      // the Budget page owns the plan — Settings has no budgets editor
      to: "/budget", label: "Budget",
    },
    // the cash forecast is blind until a primary checking account is
    // picked, and nothing else on this checklist says so
    {
      done: data.primary_checking_set,
      title: "Pick your primary checking",
      text: "The cash forecast anchors on it — runway, low point and the timeline all read from this one account.",
      to: "/accounts", label: "Accounts",
    },
  ];
  const doneCount = steps.filter((s) => s.done).length;

  return (
    <div className="card">
      <h2>Welcome — five steps to your first verdict</h2>
      <p className="mut">{doneCount}/{steps.length} done. Every step is
        reversible and nothing counts until you approve it.</p>
      {data.pending_proposals > 0 && (
        <p className="mut" style={{ color: "var(--amber)" }}>
          {data.pending_proposals} detected bill
          {data.pending_proposals === 1 ? "" : "s"} await review on
          Bills &amp; Income.
        </p>
      )}
      {steps.map((s, i) => (
        <div className="onboard-step" key={i}>
          <span className={"onboard-mark " + (s.done ? "done" : "")}>
            {s.done ? "✓" : i + 1}
          </span>
          <div style={{ flex: 1 }}>
            <b className={s.done ? "mut" : ""}>{s.title}</b>
            {!s.done && <div className="mut" style={{ fontSize: ".9rem" }}>{s.text}</div>}
          </div>
          {!s.done && <Link className="btn-link" to={s.to}>{s.label} →</Link>}
        </div>
      ))}
    </div>
  );
}
