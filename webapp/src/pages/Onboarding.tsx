import { useState } from "react";
import { Link } from "react-router";
import { type Onboarding as OnboardingData } from "../api/client";
import { checklistSteps } from "../checklist";
import PrimaryCheckingPick from "../components/PrimaryCheckingPick";

/** Guided first-run checklist, shown by Today while the ledger is empty. */
export default function Onboarding({ data, hosted }:
                                   { data: OnboardingData; hosted?: boolean }) {
  const steps = checklistSteps(data, hosted);
  const doneCount = steps.filter((s) => s.done).length;
  const left = steps.filter((s) => !s.done);
  // Nearly done, the full card is mostly ticked rows sitting above the
  // verdict the page exists to show: it folds to one line naming what is
  // left, and opens again on request (or to make the checking choice).
  const [open, setOpen] = useState(false);
  if (!open && left.length > 0 && left.length <= 2) {
    return (
      // the slim needs-you anatomy (one line, the steps ellipsized on a
      // phone) — a card here sat ~90px tall above the day's number
      <div className="needsrow setup">
        <b style={{ flexShrink: 0 }}>
          {left.length === 1 ? "1 setup step left" : "2 setup steps left"}</b>
        <span className="needsrow-msg">
          {left.map((s, i) => (
            <span key={s.key}>{i > 0 && " · "}
              {s.key === "checking" ? (
                <button className="linkish"
                        onClick={() => setOpen(true)}>{s.title}</button>
              ) : (
                <Link to={s.to}>{s.title}</Link>
              )}</span>
          ))}
        </span>
        <button className="linkish" style={{ flexShrink: 0 }}
                onClick={() => setOpen(true)}>show all</button>
      </div>
    );
  }

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
