import { Link } from "react-router";
import { type Onboarding as OnboardingData } from "../api/client";
import { checklistSteps } from "../checklist";
import PrimaryCheckingPick from "../components/PrimaryCheckingPick";

/** Guided first-run checklist, shown by Today while the ledger is empty. */
export default function Onboarding({ data, hosted }:
                                   { data: OnboardingData; hosted?: boolean }) {
  const steps = checklistSteps(data, hosted);
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
            {/* the choice is made here, on the tap — the Accounts page's
                edit row (pencil → primary checking) is the long way round */}
            {!s.done && s.key === "checking" && (
              <div style={{ marginTop: ".4rem" }}>
                <PrimaryCheckingPick compact
                                     candidates={data.checking_candidates ?? []}
                                     pinned={data.primary_checking_id ?? null} />
              </div>
            )}
          </div>
          {!s.done && <Link className="btn-link" to={s.to}>{s.label} →</Link>}
        </div>
      ))}
    </div>
  );
}
