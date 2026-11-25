// /retirement/setup — the retirement walkthrough as its OWN ROUTE, matching
// the onboarding and business wizards.
//
// Progress held in memory inside the Retirement page could not resume and
// never knew when the wizard was finished, so the marks live in tenant
// config (ret_wizard_steps).
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";
import { Navigate, useNavigate } from "react-router";
import { isViewer } from "../role";
import { api } from "../api/client";
import { saveSettings } from "../api/cache";
import { SAVE_WAITS_FAILED } from "../moneymath";
import RetireWizard from "../components/RetireWizard";

export default function RetirementSetup() {
  // the wizard's exits WRITE the setup mark — a viewer would fill in the
  // whole questionnaire and 403 on the last step (the embedded wizard
  // already excludes viewers; the standalone route must too)
  const meQ = useQuery({ queryKey: ["me"], queryFn: api.me });
  const nav = useNavigate();
  const qc = useQueryClient();
  const s = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const r = useQuery({ queryKey: ["retirement", {}],
                       queryFn: () => api.retirement({}) });
  // the wizard reports each birthdate keystroke here so Skip can save it
  const birthRef = useRef("");
  // The wizard's step saves and the exits' mark share one chain: a mark
  // sent beside a step still in flight can be answered first, and the
  // step's reply (a whole document without the mark) would then seed the
  // cache last and offer the walkthrough again.
  const wizSaves = useRef<Promise<unknown>>(Promise.resolve());
  const markWizard = (v: "done" | "skipped", birthdate = "") => {
    wizSaves.current = wizSaves.current
      .then(() => saveSettings(qc, { ret_wizard_steps: { done: v } }))
      .catch(() => {});
    if (birthdate)
      wizSaves.current = wizSaves.current
        .then(() => saveSettings(qc, { birthdate }))
        // a birthdate saved on the way out changes the model's starting
        // age; the done/skipped mark alone moves nothing in it
        .then(() => qc.invalidateQueries({ queryKey: ["retirement"] }))
        .catch(() => {});
  };

  if (s.isPending || r.isPending)
    return <div className="centered"><span className="mut">loading…</span></div>;
  // the wizard prefills from the saved answers and the projection's
  // inputs; opened without them it starts on generic defaults, and its
  // step saves would write those over what is stored
  if (!s.data || !r.data)
    return (
      <div className="centered">
        <span className="mut">{SAVE_WAITS_FAILED}</span>{" "}
        <button onClick={() => { void s.refetch(); void r.refetch(); }}>
          Try again</button>
      </div>
    );

  const inp = r.data?.inputs;
  if (isViewer(meQ)) return <Navigate to="/retirement" replace />;
  return (
    <>
      <h1>Retirement wizard</h1>
      <p className="mut" style={{ maxWidth: "36rem" }}>
        A short walkthrough so the model starts from <b>where you are</b>,
        not blank assumptions. You can skip and edit everything later.
      </p>
      <RetireWizard
        saves={wizSaves}
        splitSeed={!!inp?.saving_hint}
        defaults={inp ? {
          age: inp.age, spend: inp.spend,
          employer_mo: inp.td_mo, roth_mo: inp.roth_mo,
          taxable_mo: inp.taxable_mo,
          ret: inp.ret, infl: inp.infl, end: inp.end, ssage: inp.ssage,
        } : undefined}
        onBirthChange={(b) => { birthRef.current = b; }}
        onDone={(p) => {
          // finishing is a real, recorded state — that is what lets the
          // Settings entry stop implying the work is outstanding
          markWizard("done");
          // the projection reads the answers just saved
          qc.invalidateQueries({ queryKey: ["retirement"] });
          const qs = new URLSearchParams(p).toString();
          nav(qs ? `/retirement?${qs}` : "/retirement");
        }} />
      <p className="sub" style={{ marginTop: ".8rem" }}>
        <button className="linklike mut"
                style={{ background: "none", border: "none",
                         cursor: "pointer", padding: 0,
                         color: "var(--blue)", textDecoration: "underline" }}
                onClick={() => {
                  // Skip abandons the remaining questions, not an answer
                  // already given — a typed birthdate still lands in
                  // settings
                  // settings — in its own save, because the server refuses
                  // a whole body over one bad field and a refused birthdate
                  // must not take the skip mark down with it
                  const b = birthRef.current.trim();
                  markWizard("skipped", b);
                  nav("/retirement");
                }}>
          Skip — open the full model
        </button>
      </p>
    </>
  );
}
