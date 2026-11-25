// Guided first-time setup for Retire: a few plain questions → birthdate +
// plan assumptions saved, then the full projection UI takes over.
//
// Each step is saved as it is answered — its mark in ret_wizard_steps, its
// answers in ret_wizard_answers — so a reload or a second device opens on
// the first unanswered question with the earlier answers filled in.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type MutableRefObject, useEffect, useRef, useState } from "react";
import { errText, api } from "../api/client";
import { saveSettings } from "../api/cache";
import { parseMoneyDraft, SAVE_WAITS_FAILED } from "../moneymath";
import { RET_WIZARD_BOUNDS } from "../retWizard";

const STEPS = [
  { key: "age", title: "How old are you?",
    blurb: "We use this (and optionally a birthdate) as the starting age for the plan." },
  { key: "spend", title: "What do you want to spend in retirement?",
    blurb: "Annual spending in today's dollars — housing, food, travel, the whole lifestyle." },
  { key: "saving", title: "How much are you saving each month?",
    blurb: "Split tax-deferred employer accounts (401k/403b), Roth accounts and taxable brokerage if you like." },
  { key: "assumptions", title: "A few market assumptions",
    blurb: "Sensible defaults — change them if you already have a view." },
  { key: "done", title: "You're set",
    blurb: "We'll open the full retirement model with these numbers. Tweak anytime." },
] as const;
type StepKey = typeof STEPS[number]["key"];
type AnswerKey = "age" | "spend" | "employer_mo" | "roth_mo" | "taxable_mo"
  | "ret" | "infl" | "end" | "ssage";
type Marks = Record<string, string | null> | null | undefined;

// ---- saved progress. Mobile carries a twin of each helper in
// mobile/src/lib/pure.ts, pinned by the same pure-logic tests' shape check.

/** the answers each question owns */
const STEP_ANSWERS: Record<StepKey, readonly AnswerKey[]> = {
  age: ["age"], spend: ["spend"],
  saving: ["employer_mo", "roth_mo", "taxable_mo"],
  assumptions: ["ret", "infl", "end", "ssage"], done: [],
};
const MONEY = new Set<AnswerKey>(["spend", "employer_mo", "roth_mo",
                                   "taxable_mo"]);

/** The step to open on: the first question with no saved mark, or the
 *  summary once all are answered. A finished or skipped walkthrough opened
 *  again starts from the top, with its answers prefilled. */
export function retWizardResumeAt(marks: Marks): number {
  if (!marks || marks.done) return 0;
  const i = STEPS.findIndex((s) => s.key !== "done" && !marks[s.key]);
  return i === -1 ? STEPS.length - 1 : i;
}

/** Started and not finished. The first answer saves the birthdate, and
 *  "no birthdate yet" is what offers the walkthrough on a first visit —
 *  without this, a person who stopped after step one never saw it again. */
export function retWizardMidway(marks: Marks): boolean {
  return !!marks && !marks.done
    && STEPS.some((s) => s.key !== "done" && !!marks[s.key]);
}

/** One step's save: its mark and EVERY box on it, as the person's answers
 *  (walking the wizard is answering it; each box shows what the projection
 *  would use, so leaving it as it is confirms it). A cleared box is the
 *  server's default, so it clears the saved answer (null); text that is not
 *  a number does the same instead of being stored as $0. `ret_answers_v: 2`
 *  marks the answers as typed into this walkthrough, so the projection may
 *  read them; it, the claim age and the Roth saving ride BESIDE
 *  `ret_wizard_answers`, as top-level fields an older server ignores (it
 *  refuses a whole save over an answer key it does not know). */
function retWizardStepBody(
  step: StepKey, draft: Record<AnswerKey, string>,
): Record<string, unknown> {
  const answers: Record<string, number | null> = {};
  let ssage: number | null | undefined;
  let roth: number | null | undefined;
  for (const k of STEP_ANSWERS[step]) {
    const t = (draft[k] ?? "").trim();
    const n = t === "" ? null : MONEY.has(k) ? parseMoneyDraft(t) : Number(t);
    const v = n !== null && Number.isFinite(n) ? n : null;
    if (k === "ssage") ssage = v;
    // a draft with no Roth box (a phone on an older server) leaves the
    // stored Roth answer alone; a blank Roth box is an answered $0, so
    // "answered nothing" and "never asked" stay apart
    else if (k === "roth_mo") { if (draft[k] !== undefined) roth = v ?? 0; }
    else answers[k] = v;
  }
  const out: Record<string, unknown> = { ret_wizard_steps: { [step]: "done" } };
  if (Object.keys(answers).length || ssage !== undefined
      || roth !== undefined) {
    out.ret_answers_v = 2;
    if (Object.keys(answers).length) out.ret_wizard_answers = answers;
    if (ssage !== undefined) out.ret_wizard_ssage = ssage;
    if (roth !== undefined) out.ret_wizard_roth_mo = roth;
  }
  return out;
}

const ANSWER_KEYS: readonly AnswerKey[] =
  ["age", "spend", "employer_mo", "roth_mo", "taxable_mo", "ret", "infl",
   "end", "ssage"];

/** the boxes: a saved answer where there is one, else the figure the
 *  projection used (the server's own default), else blank — a blank box
 *  is the server's default too, so the wizard keeps no defaults of its own */
function retWizardPrefill(
  saved: Record<string, unknown> | null | undefined,
  defaults: Partial<Record<AnswerKey, number | null>>,
  splitSeed = false,
): Record<AnswerKey, string> {
  const out = {} as Record<AnswerKey, string>;
  const marked = saved?.answers_v2;
  const has = (k: string) => Array.isArray(marked) && marked.includes(k);
  // The projection's figures can be a what-if, so a saved answer wins.
  // One exception: a saved tax-deferred figure with no Roth answer beside
  // it may hold the Roth saving, and when the server says its figures are
  // that saved total split by the ledger (`splitSeed`: the reply carries
  // `saving_hint`), both saving boxes open on them, so saving them stores
  // a split of the same total (specs/retirement-figures.md). A saved block
  // with no Roth answer otherwise opens its Roth box on the $0 it is
  // projected as, never on a what-if's Roth figure.
  const seeded = splitSeed && typeof defaults.roth_mo === "number"
    && !has("roth_mo");
  const savedSaving = typeof defaults.roth_mo === "number" && !has("roth_mo")
    && (has("employer_mo") || has("taxable_mo"));
  for (const k of ANSWER_KEYS) {
    const v = seeded && (k === "employer_mo" || k === "roth_mo") ? undefined
      : has(k) ? saved?.[k]
      : k === "roth_mo" && savedSaving ? 0
      : undefined;
    const d = defaults[k];
    out[k] = typeof v === "number" && Number.isFinite(v) ? String(v)
      : typeof d === "number" && Number.isFinite(d) ? String(d) : "";
  }
  return out;
}

type Props = {
  // the inputs the projection used (null spend: none to plan for yet)
  defaults?: Partial<Record<AnswerKey, number | null>>;
  // the reply's saving figures are a saved total split by the ledger (it
  // carries saving_hint), not a what-if: the saving boxes open on them
  splitSeed?: boolean;
  // the answers are saved as each step is left, and the projection reads
  // them, so this hands over only what did NOT save (a read-only login, a
  // dropped connection) as what-ifs for the page to run with
  onDone: (params: Record<string, string>) => void;
  // hosts render their own Skip button outside this component; surfacing
  // the typed birthdate lets Skip save it — skipping abandons the
  // remaining questions, not an answer already given
  onBirthChange?: (birth: string) => void;
  // the host's chain of settings saves: every step save goes on it, and
  // the host's exits put their done/skipped mark on it after them
  saves: MutableRefObject<Promise<unknown>>;
};

// The wizard opens on the saved progress, so it waits for settings (both
// hosts have them cached already; a direct load shows this for a moment).
export default function RetireWizard(props: Props) {
  const s = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  if (s.isPending)
    return <div className="card mut" style={{ maxWidth: "36rem" }}>loading…</div>;
  // a failed read must not open the wizard on generic defaults: its step
  // saves would write them over the stored answers
  if (!s.data)
    return (
      <div className="card" style={{ maxWidth: "36rem" }}>
        <span className="mut">{SAVE_WAITS_FAILED}</span>{" "}
        <button onClick={() => void s.refetch()}>Try again</button>
      </div>
    );
  return <WizardBody {...props} marks={s.data?.ret_wizard_steps}
                     answers={s.data?.ret_wizard_answers} />;
}

function WizardBody({
  defaults, splitSeed, onDone, onBirthChange, marks, answers, saves,
}: Props & { marks: Marks; answers?: Record<string, unknown> | null }) {
  const qc = useQueryClient();
  // read once, at mount: the saves below seed settings as they land, and
  // re-reading them would move the boxes under the person's cursor
  const [seed] = useState(() => retWizardPrefill(answers, defaults ?? {},
                                                    !!splitSeed));
  const [i, setI] = useState(() => retWizardResumeAt(marks));
  // the step on screen, or none once the wizard has closed: a save's late
  // reply acts only if the person is still on the step that sent it
  const stepNow = useRef(i);
  useEffect(() => {
    stepNow.current = i;
    return () => { stepNow.current = -1; };
  }, [i]);
  const stillOn = (at: number) => stepNow.current === at;
  const [age, setAge] = useState(seed.age);
  const [birth, setBirth] = useState("");
  const [spend, setSpend] = useState(seed.spend);
  const [employer, setEmployer] = useState(seed.employer_mo);
  const [roth, setRoth] = useState(seed.roth_mo);
  const [taxable, setTaxable] = useState(seed.taxable_mo);
  const [ret, setRet] = useState(seed.ret);
  const [infl, setInfl] = useState(seed.infl);
  const [end, setEnd] = useState(seed.end);
  const [ssage, setSsage] = useState(seed.ssage);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // a step whose answers did not land (nothing the person could correct)
  const unsaved = useRef(false);

  // Step saves run one after another: each reply seeds ["settings"] with
  // the whole document, so an earlier reply landing last would put the
  // later step's mark back to missing. The chain is the host's, so the
  // done/skipped mark its exits write lands after every step's reply.
  const saveStep = (k: StepKey) => {
    const body = retWizardStepBody(k, {
      age, spend, employer_mo: employer, roth_mo: roth, taxable_mo: taxable,
      ret, infl, end, ssage });
    const link = saves.current.then(() => saveSettings(qc, body));
    // the chain carries on past a refused link, so one failure never stops
    // the saves after it; the step that made this one reads its refusal
    saves.current = link.catch(() => {});
    return link;
  };
  // A refusal the person can correct (the server names the answer and its
  // range) is shown on its step, which waits. Any other — the demo's
  // read-only login, a dropped connection — is nothing they can fix here
  // and never traps them in the walkthrough. The same rule on every step,
  // the birthdate's included, and on the phone.
  const stepRefusal = (e: unknown): string | null =>
    e instanceof Error && /^4(00|22):/.test(e.message) ? errText(e) : null;
  // A cleared box means "use the default", so it is left out of the query
  // rather than sent as an empty value — the adjust panel does the same.
  const params = (): Record<string, string> => !unsaved.current ? {}
    : Object.fromEntries(Object.entries({
      age, spend, employer_mo: employer, roth_mo: roth, taxable_mo: taxable,
      ret, infl, end, ssage,
    }).map(([k, v]) => [k, k === "roth_mo" && v.trim() === "" ? "0"
                                    : v.trim()] as [string, string])
      .filter(([, v]) => v !== ""));

  const step = STEPS[i];
  const next = async () => {
    setErr(null);
    const at = i;
    if (i === 0) {
      const a = Number(age);
      const [lo, hi] = RET_WIZARD_BOUNDS.age;
      if (!Number.isFinite(a) || a < lo || a > hi) {
        setErr(`Enter an age between ${lo} and ${hi}.`);
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
    // the claim age is a whole year in the server's range: say so here,
    // as the other boxes do, rather than show the server's refusal
    if (step.key === "assumptions" && ssage.trim() !== "") {
      const c = Number(ssage);
      const [lo, hi] = RET_WIZARD_BOUNDS.ssage;
      if (!Number.isInteger(c) || c < lo || c > hi) {
        setErr(`Enter a claim age from ${lo} to ${hi}, in whole years.`);
        return;
      }
    }
    if (i === 0 && birth.trim()) {
      // the birthdate goes up with the first answer, so the starting age
      // it gives survives a reload; a refusal the person can fix (a date
      // in the future, a typo) is shown and the step waits
      setBusy(true);
      try {
        await saveSettings(qc, { birthdate: birth.trim() });
        // it moves the model's starting age; the projection fetched
        // before it is the one the page would keep showing
        qc.invalidateQueries({ queryKey: ["retirement"] });
        if (!stillOn(at)) return;
      } catch (e) {
        if (!stillOn(at)) return;
        const why = stepRefusal(e);
        if (why) { setErr(why); return; }
      } finally { setBusy(false); }
    }
    // the step's answers land before it is left: a value the server
    // refuses is shown here, on the step it belongs to
    setBusy(true);
    try {
      await saveStep(step.key);
      if (!stillOn(at)) return;
    } catch (e) {
      if (!stillOn(at)) return;
      const why = stepRefusal(e);
      if (why) { setErr(why); return; }
      unsaved.current = true;
    } finally { setBusy(false); }
    if (i === STEPS.length - 2) {
      // last question → its answers have landed, so the full model opens
      onDone(params());
      return;
    }
    setI(Math.min(i + 1, STEPS.length - 1));
  };
  const back = () => {
    if (busy) return;             // the step's save is still in flight
    setErr(null); setI(Math.max(0, i - 1));
  };
  const lim = (k: string, at: 0 | 1) => String(RET_WIZARD_BOUNDS[k][at]);

  // min/max/step are the bounds the server holds each answer to; the
  // browser only hints with them (nothing here is clamped), and the
  // server's refusal still names the range when it is crossed
  const field = (label: string, value: string, set: (v: string) => void,
                 hint?: string, type = "number", max?: string,
                 min?: string, step?: string) => (
    <label style={{ display: "flex", flexDirection: "column", gap: ".25rem",
                    fontSize: 13, color: "var(--mut)", minWidth: "10rem" }}>
      {label}
      <input type={type} value={value} max={max} min={min} step={step}
             onChange={(e) => set(e.target.value)}
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
          {field("Current age", age, setAge, undefined, "number",
                 lim("age", 1), lim("age", 0))}
          {field("Birthdate (optional)", birth,
                 (v) => { setBirth(v); onBirthChange?.(v); },
                 undefined, "date",
                 // a birthdate is in the past; the server refuses today on
                 new Date(Date.now() - 864e5).toLocaleDateString("en-CA"))}
        </div>
      )}
      {step.key === "spend" &&
        field("Target spend $/year", spend, setSpend, "Today's dollars",
              "number", lim("spend", 1), lim("spend", 0))}
      {step.key === "saving" && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: "1rem" }}>
          {field("Employer retirement $/mo", employer, setEmployer,
                 "401(k), 403(b), pension…", "number",
                 lim("employer_mo", 1), lim("employer_mo", 0))}
          {field("Roth retirement $/mo", roth, setRoth,
                 "Roth IRA, Roth 401(k)…", "number",
                 lim("roth_mo", 1), lim("roth_mo", 0))}
          {field("Taxable saving $/mo", taxable, setTaxable,
                 "Brokerage, excess cash…", "number",
                 lim("taxable_mo", 1), lim("taxable_mo", 0))}
        </div>
      )}
      {step.key === "assumptions" && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: "1rem" }}>
          {field("Expected return %", ret, setRet, undefined, "number",
                 lim("ret", 1), lim("ret", 0), "0.1")}
          {field("Inflation %", infl, setInfl, undefined, "number",
                 lim("infl", 1), lim("infl", 0), "0.1")}
          {field("Plan to age", end, setEnd, undefined, "number",
                 lim("end", 1), lim("end", 0))}
          {field("Social Security claim age", ssage, setSsage, undefined,
                 "number", lim("ssage", 1), lim("ssage", 0))}
        </div>
      )}
      {step.key === "done" && (
        <p>Age <b>{age}</b> · spend <b>${Number(spend).toLocaleString()}/yr</b>
          {" "}· saving <b>${(Number(employer) + Number(roth) + Number(taxable)).toLocaleString()}/mo</b>
          {" "}· return {ret}% / inflation {infl}% · to age {end}.</p>
      )}

      {err && <p style={{ color: "var(--red)" }}>{err}</p>}
      <div style={{ display: "flex", justifyContent: "flex-end", gap: ".6rem",
                    marginTop: "1.1rem" }}>
        {i > 0 && step.key !== "done" && (
          <button type="button" disabled={busy}
                  onClick={back}>← Back</button>)}
        {step.key !== "done" ? (
          <button type="button" className="pri" disabled={busy}
                  onClick={() => { void next(); }}>
            {busy ? "saving…"
              : i === STEPS.length - 2 ? "Finish →" : "Continue →"}
          </button>
        ) : (
          <button type="button" className="pri"
                  onClick={() => onDone(params())}>
            Open my plan →
          </button>
        )}
      </div>
    </div>
  );
}
