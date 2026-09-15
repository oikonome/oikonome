// /retirement/setup — the retirement walkthrough as its OWN ROUTE, matching
// the onboarding and business wizards.
//
// Progress held in memory inside the Retirement page could not resume and
// never knew when the wizard was finished, so the marks live in tenant
// config (ret_wizard_steps).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";
import { Navigate, useNavigate } from "react-router";
import { isViewer } from "../role";
import { api } from "../api/client";
import { saveSettings } from "../api/cache";
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
  const mark = useMutation({
    mutationFn: (body: Record<string, unknown>) => saveSettings(qc, body),
    onSuccess: (_s, body) => {
      // a birthdate saved on the way out changes the model's starting age;
      // the done/skipped mark alone moves nothing in it
      if (body.birthdate) qc.invalidateQueries({ queryKey: ["retirement"] });
    },
  });

  if (s.isPending || r.isPending)
    return <div className="centered"><span className="mut">loading…</span></div>;

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
        defaults={inp ? {
          age: inp.age, spend: inp.spend,
          employer_mo: inp.employer_mo, taxable_mo: inp.taxable_mo,
          ret: inp.ret, infl: inp.infl, end: inp.end,
        } : undefined}
        onBirthChange={(b) => { birthRef.current = b; }}
        onDone={(p) => {
          // finishing is a real, recorded state — that is what lets the
          // Settings entry stop implying the work is outstanding
          mark.mutate({ ret_wizard_steps: { done: "done" } });
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
                  const b = birthRef.current.trim();
                  mark.mutate({ ret_wizard_steps: { done: "skipped" },
                                ...(b ? { birthdate: b } : {}) });
                  nav("/retirement");
                }}>
          Skip — open the full model
        </button>
      </p>
    </>
  );
}
