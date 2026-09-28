// Guided first-time setup for Retire: a few plain questions → birthdate +
// plan assumptions saved, then the full projection UI takes over.
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { errText, api } from "../api/client";

const STEPS = [
  { key: "age", title: "How old are you?",
    blurb: "We use this (and optionally a birthdate) as the starting age for the plan." },
  { key: "spend", title: "What do you want to spend in retirement?",
    blurb: "Annual spending in today's dollars — housing, food, travel, the whole lifestyle." },
  { key: "saving", title: "How much are you saving each month?",
    blurb: "Split employer retirement accounts (401k/403b) and taxable brokerage if you like." },
  { key: "assumptions", title: "A few market assumptions",
    blurb: "Sensible defaults — change them if you already have a view." },
  { key: "done", title: "You're set",
    blurb: "We'll open the full retirement model with these numbers. Tweak anytime." },
] as const;

export default function RetireWizard({
  defaults, onDone, onBirthChange,
}: {
  defaults?: { age?: number; spend?: number; employer_mo?: number;
               taxable_mo?: number; ret?: number; infl?: number; end?: number };
  onDone: (params: Record<string, string>) => void;
  // hosts render their own Skip button outside this component; surfacing
  // the typed birthdate lets Skip save it — skipping abandons the
  // remaining questions, not an answer already given
  onBirthChange?: (birth: string) => void;
}) {
  const qc = useQueryClient();
  const [i, setI] = useState(0);
  const [age, setAge] = useState(String(defaults?.age ?? 40));
  const [birth, setBirth] = useState("");
  const [spend, setSpend] = useState(String(defaults?.spend ?? 60000));
  const [employer, setEmployer] = useState(String(defaults?.employer_mo ?? 0));
  const [taxable, setTaxable] = useState(String(defaults?.taxable_mo ?? 0));
  const [ret, setRet] = useState(String(defaults?.ret ?? 7));
  const [infl, setInfl] = useState(String(defaults?.infl ?? 3));
  const [end, setEnd] = useState(String(defaults?.end ?? 95));
  const [err, setErr] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: async () => {
      const body: Record<string, unknown> = {};
      if (birth.trim()) body.birthdate = birth.trim();
      // age is derived from birthdate when present; retirement API still
      // takes query params for the what-if form
      return api.settingsSave(body);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["settings"] });
      // a birthdate saved here moves the model's starting age; the
      // projection fetched before it is the one the page would keep showing
      qc.invalidateQueries({ queryKey: ["retirement"] });
      // A cleared box means "use the default", so it is left out of the
      // query rather than sent as an empty value — the adjust panel does
      // the same.
      const params: Record<string, string> = Object.fromEntries(
        Object.entries({
          age, spend, employer_mo: employer, taxable_mo: taxable,
          ret, infl, end,
        }).map(([k, v]) => [k, v.trim()] as [string, string])
          .filter(([, v]) => v !== ""));
      onDone(params);
    },
    onError: (e) => setErr(errText(e)),
  });

  const step = STEPS[i];
  const next = () => {
    setErr(null);
    if (i === 0) {
      const a = Number(age);
      if (!Number.isFinite(a) || a < 18 || a > 100) {
        setErr("Enter an age between 18 and 100.");
        return;
      }
    }
    if (i === 1) {
      const s = Number(spend);
      if (!Number.isFinite(s) || s < 0) {
        setErr("Enter a non-negative annual spend.");
        return;
      }
    }
    if (i >= STEPS.length - 2) {
      // last content step → save then done
      if (i === STEPS.length - 2) {
        save.mutate();
        setI(i + 1);
        return;
      }
    }
    setI(Math.min(i + 1, STEPS.length - 1));
  };
  const back = () => { setErr(null); setI(Math.max(0, i - 1)); };

  const field = (label: string, value: string, set: (v: string) => void,
                 hint?: string, type = "number") => (
    <label style={{ display: "flex", flexDirection: "column", gap: ".25rem",
                    fontSize: 13, color: "var(--mut)", minWidth: "10rem" }}>
      {label}
      <input type={type} value={value} onChange={(e) => set(e.target.value)}
             style={{ padding: ".5rem", background: "var(--hover)",
                      border: "1px solid var(--line)", borderRadius: "var(--rs)",
                      color: "var(--ink)", fontSize: 15 }} />
      {hint && <span className="mut" style={{ fontSize: 12 }}>{hint}</span>}
    </label>
  );

  return (
    <div className="card" style={{ maxWidth: "36rem", margin: "1rem 0" }}>
      <div className="mut" style={{ fontSize: 12, marginBottom: ".4rem" }}>
        Step {i + 1} of {STEPS.length}</div>
      <h2 style={{ marginTop: 0 }}>{step.title}</h2>
      <p className="mut" style={{ marginTop: 0 }}>{step.blurb}</p>

      {step.key === "age" && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: "1rem" }}>
          {field("Current age", age, setAge)}
          {field("Birthdate (optional)", birth,
                 (v) => { setBirth(v); onBirthChange?.(v); },
                 "YYYY-MM-DD preferred", "date")}
        </div>
      )}
      {step.key === "spend" &&
        field("Target spend $/year", spend, setSpend, "Today's dollars")}
      {step.key === "saving" && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: "1rem" }}>
          {field("Employer retirement $/mo", employer, setEmployer,
                 "401(k), 403(b), pension…")}
          {field("Taxable saving $/mo", taxable, setTaxable,
                 "Brokerage, excess cash…")}
        </div>
      )}
      {step.key === "assumptions" && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: "1rem" }}>
          {field("Expected return %", ret, setRet)}
          {field("Inflation %", infl, setInfl)}
          {field("Plan to age", end, setEnd)}
        </div>
      )}
      {step.key === "done" && (
        <p>Age <b>{age}</b> · spend <b>${Number(spend).toLocaleString()}/yr</b>
          {" "}· saving <b>${(Number(employer) + Number(taxable)).toLocaleString()}/mo</b>
          {" "}· return {ret}% / inflation {infl}% · to age {end}.</p>
      )}

      {err && <p style={{ color: "var(--red)" }}>{err}</p>}
      <div style={{ display: "flex", justifyContent: "flex-end", gap: ".6rem",
                    marginTop: "1.1rem" }}>
        {i > 0 && step.key !== "done" && (
          <button type="button" onClick={back}>← Back</button>)}
        {step.key !== "done" ? (
          <button type="button" className="pri" disabled={save.isPending}
                  onClick={next}>
            {i === STEPS.length - 2
              ? (save.isPending ? "saving…" : "Finish →")
              : "Continue →"}
          </button>
        ) : (
          <button type="button" className="pri"
                  onClick={() => onDone({
                    age, spend, employer_mo: employer, taxable_mo: taxable,
                    ret, infl, end,
                  })}>
            Open my plan →
          </button>
        )}
      </div>
    </div>
  );
}
