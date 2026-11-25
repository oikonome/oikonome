// Retirement — what-if assumption form, 4 KPI cards, growth chart,
// tax-treatment table, historical replay, stress table, retire-at-each-age
// table. Plus a first-run wizard that walks through plain questions before
// the full model.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { useSearchParams } from "react-router";
import { isViewer } from "../role";
import { api, errText, money, pyround, type RetirementData,
         type RetirementInputs, type RetSpendSource } from "../api/client";
import { saveSettings } from "../api/cache";
import AreaChart from "../components/AreaChart";
import RetireWizard, { retWizardMidway } from "../components/RetireWizard";
import { parseMoneyDraft } from "../moneymath";
import { rebaseSsDraft, ssDraftOf } from "../ssDraft";
import { SS_CLAIM_AGES, ssEstimatesBody } from "../ssEstimates";
import PageSkeleton from "../components/PageSkeleton";
import { FailedLine } from "../components/Loaded";
import { keyRetireAges } from "../keyages";
import Note from "../components/Note";

// /api/retirement takes the dollar boxes as whole numbers and refuses
// "250.50" or "60,000" outright, so they are parsed and rounded before
// they ride the query — the form, the wizard and a /retirement/setup URL
// all pass through here. Text that is not an amount rides as typed and
// the refusal shows beside the form.
const MONEY_KEYS = ["spend", "employer_mo", "roth_mo", "taxable_mo"];
const whatIfParams = (p: Record<string, string>) =>
  Object.fromEntries(Object.entries(p).map(([k, v]) => {
    if (!MONEY_KEYS.includes(k)) return [k, v];
    const n = parseMoneyDraft(v);
    return [k, n === null ? v : String(pyround(n))];
  }));

function Fld({ name, label, value, step = "1", w = "6rem", ph, text, onChange }: {
  name: string; label: string; value: number | string; step?: string; w?: string;
  // a saved amount: a number box reads "2,400" as empty in some browsers
  text?: boolean;
  // what an empty field means (the server's default), shown in its place
  ph?: string;
  onChange: (name: string, v: string) => void;
}) {
  return (
    <label style={{ display: "flex", flexDirection: "column", fontSize: 12,
                    color: "var(--mut)", gap: ".15rem" }}>
      {label}
      <input type={text ? "text" : "number"} name={name} value={value}
             step={text ? undefined : step}
             inputMode={text ? "decimal" : undefined}
             placeholder={ph}
             style={{ width: w }} onChange={(e) => onChange(name, e.target.value)} />
    </label>
  );
}

// Unlike the what-if form above it, this one is saved: the estimates are the
// projection's only source of Social Security income.
function SsEstimates({ saved }: { saved: Record<string, number> | null }) {
  const qc = useQueryClient();
  const seeded = ssDraftOf(saved);
  const [draft, setDraft] = useState(seeded);
  // the schedule the untouched boxes show; a newer one moves them over
  const [base, setBase] = useState(seeded);
  if (JSON.stringify(base) !== JSON.stringify(seeded)) {
    setBase(seeded);
    setDraft(rebaseSsDraft(draft, base, seeded));
  }
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const save = (e: React.FormEvent) => {
    e.preventDefault();
    setDone(false);
    const b = ssEstimatesBody(draft, parseMoneyDraft);
    if (!b.ok) { setErr(b.error); return; }
    setErr(null);
    setBusy(true);
    saveSettings(qc, { ss_estimates: b.body })
      .then(() => {
        setDone(true);
        // what went up is now what the boxes were based on: the server's
        // copy replaces it, and anything typed since stays
        setBase(draft);
        qc.invalidateQueries({ queryKey: ["retirement"] });
      })
      .catch((x) => setErr(errText(x)))
      .finally(() => setBusy(false));
  };
  return (
    <form onSubmit={save} noValidate
          style={{ background: "var(--hover)", borderRadius: "var(--rs)",
                   padding: ".6rem .8rem", marginTop: ".5rem", fontSize: 13 }}>
      <div className="sub" style={{ marginBottom: ".4rem" }}>
        Social Security $/mo by claim age</div>
      <div style={{ display: "flex", gap: ".7rem", flexWrap: "wrap",
                    alignItems: "flex-end" }}>
        {SS_CLAIM_AGES.map((age) => (
          <Fld key={age} name={`ss_${age}`} label={`at ${age}`}
               value={draft[age] ?? ""} w="4.5rem" text
               onChange={(_, v) => {
                 setDone(false);
                 setDraft((d) => ({ ...d, [age]: v }));
               }} />
        ))}
        <button className="pri" disabled={busy}>
          {busy ? "Saving…" : "Save"}</button>
        {done && <span className="pos">Saved</span>}
      </div>
      {err && <Note tone="bad" style={{ marginTop: ".5rem" }}>{err}</Note>}
    </form>
  );
}

// where the projection's spend and saving came from, as the assumptions
// line words it (the server names the source; the page only says it)
const SPEND_FROM: Record<RetSpendSource, string> = {
  what_if: "what-if", saved: "your answer", ledger: "your last 12 months",
  needed: "not set",
};
const SAVING_FROM: Record<RetirementInputs["saving_basis"], string> = {
  what_if: "what-if", saved: "your answer", ledger: "from your last 12 months",
  none: "none entered",
};

// the catch-up's closing words: what the extra adds to, as the
// assumptions line counts it (specs/retirement-figures.md)
const onTopOf = (inp: RetirementInputs) => inp.saving_mo > 0
  ? ` — starting now, on top of the ${money(inp.saving_mo)}/mo already counted (${SAVING_FROM[inp.saving_basis]}).`
  : " — starting now.";

// No saved spend and too little history to read one from: the plan needs
// one before it can project anything. Saved with the walkthrough's
// answers, so every device opens on it.
function SpendAsk({ onWizard }: { onWizard: () => void }) {
  const qc = useQueryClient();
  const [v, setV] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const save = (e: React.FormEvent) => {
    e.preventDefault();
    const n = parseMoneyDraft(v);
    if (n === null) { setErr("Enter an amount, like 60,000."); return; }
    setErr(null);
    setBusy(true);
    saveSettings(qc, { ret_wizard_answers: { spend: n }, ret_answers_v: 2 })
      .then(() => qc.invalidateQueries({ queryKey: ["retirement"] }))
      .catch((x) => setErr(errText(x)))
      .finally(() => setBusy(false));
  };
  return (
    <form onSubmit={save} noValidate
          style={{ display: "flex", gap: ".6rem", flexWrap: "wrap",
                   alignItems: "flex-end", marginTop: ".6rem" }}>
      <Fld name="spend" label="spend $/yr in retirement" value={v} text
           w="9rem" onChange={(_, x) => setV(x)} />
      <button className="pri" disabled={busy}>
        {busy ? "Saving…" : "Plan with this"}</button>
      <button type="button" className="linklike"
              style={{ background: "none", border: "none", cursor: "pointer",
                       color: "var(--blue)", fontSize: 13 }}
              onClick={onWizard}>or answer the walkthrough</button>
      {err && <Note tone="bad" style={{ flex: "1 1 100%" }}>{err}</Note>}
    </form>
  );
}

// No saved saving answer: the projection counts no new saving (the
// server's "none") and the page asks for one, the spend ask's pattern.
// Blank box = $0 there; at least one amount is needed to save.
function SavingAsk() {
  const qc = useQueryClient();
  const [emp, setEmp] = useState("");
  const [roth, setRoth] = useState("");
  const [tax, setTax] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const save = (e: React.FormEvent) => {
    e.preventDefault();
    const blank = (v: string) => v.replace(/[$\s]/g, "") === "";
    if (blank(emp) && blank(roth) && blank(tax)) {
      setErr("Enter an amount, like 500."); return;
    }
    const e1 = blank(emp) ? 0 : parseMoneyDraft(emp);
    const r1 = blank(roth) ? 0 : parseMoneyDraft(roth);
    const t1 = blank(tax) ? 0 : parseMoneyDraft(tax);
    if (e1 === null || r1 === null || t1 === null) {
      setErr("Enter an amount, like 500."); return;
    }
    setErr(null);
    setBusy(true);
    // the Roth figure rides beside the answers: an older server refuses a
    // whole save over an answer key it does not know
    saveSettings(qc, { ret_wizard_answers: { employer_mo: e1, taxable_mo: t1 },
                    ret_wizard_roth_mo: r1, ret_answers_v: 2 })
      .then(() => qc.invalidateQueries({ queryKey: ["retirement"] }))
      .catch((x) => setErr(errText(x)))
      .finally(() => setBusy(false));
  };
  return (
    <form onSubmit={save} noValidate
          style={{ display: "flex", gap: ".6rem", flexWrap: "wrap",
                   alignItems: "flex-end", marginTop: ".5rem" }}>
      <Fld name="employer_mo" label="employer retirement $/mo" value={emp} text
           w="8rem" onChange={(_, x) => setEmp(x)} />
      <Fld name="roth_mo" label="Roth retirement $/mo" value={roth} text
           w="8rem" onChange={(_, x) => setRoth(x)} />
      <Fld name="taxable_mo" label="taxable saving $/mo" value={tax} text
           w="8rem" onChange={(_, x) => setTax(x)} />
      <button className="pri" disabled={busy}>
        {busy ? "Saving…" : "Save"}</button>
      {err && <Note tone="bad" style={{ flex: "1 1 100%" }}>{err}</Note>}
    </form>
  );
}

export default function Retirement() {
  // Show the guided wizard until the user finishes it once this visit,
  // or skips — full model always available afterward.
  // `?wizard=1` forces it open so the walkthrough can be re-entered from
  // anywhere (Settings → Setup), not only on a first visit.
  const [search, setSearch] = useSearchParams();
  const forced = search.get("wizard") === "1";
  // /retirement/setup hands its answers over in the QUERY STRING and
  // navigates here. Seed the model from them, or answering every question
  // produces the identical default projection Skip does and the
  // walkthrough is decorative.
  const [params, setParams] = useState<Record<string, string>>(() => {
    const seed: Record<string, string> = {};
    search.forEach((v, k) => { if (k !== "wizard") seed[k] = v; });
    return seed;
  });
  const [wizard, setWizard] = useState<boolean | null>(forced ? true : null);
  // the ten assumption fields open from the hero's summary line
  const [adjust, setAdjust] = useState(false);
  // "Retire at each age" opens on the ages that decide it
  const [everyAge, setEveryAge] = useState(false);
  const q = useQuery({
    queryKey: ["retirement", whatIfParams(params)],
    queryFn: () => api.retirement(whatIfParams(params)),
    placeholderData: (prev) => prev,
  });
  // A failed what-if leaves q.data empty (placeholderData only covers the
  // pending fetch). Keep the last projection the server did send, so a
  // refused answer is corrected in the form instead of taking the page.
  const [lastGood, setLastGood] = useState<RetirementData>();
  if (q.data && q.data !== lastGood) setLastGood(q.data);
  // refetched on every visit: the Social Security editor below waits for a
  // read made since the page opened, and a young cache would never get one
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings,
                              refetchOnMount: "always" });
  const [openedAt] = useState(() => Date.now());
  const ssFresh = settings.dataUpdatedAt >= openedAt;
  const meQ = useQuery({ queryKey: ["me"], queryFn: api.me });
  const viewer = isViewer(meQ);
  const qc = useQueryClient();
  // the wizard reports each birthdate keystroke here so Skip can save it
  const birthRef = useRef("");
  // persist both wizard exits (done AND skipped) — the needsWizard gate
  // below reads this mark, and every exit has to write it
  // A birthdate typed before Skip is saved on its own: the server refuses
  // a whole body over one bad field, so riding with the mark would let a
  // refused birthdate drop the mark and reopen the walkthrough every visit.
  const [birthErr, setBirthErr] = useState<string | null>(null);
  // the "saving not set — add it" box under the assumptions line
  const [askSaving, setAskSaving] = useState(false);
  // The wizard's step saves and these exits share one chain: a mark sent
  // beside a step still in flight can be answered first, and the step's
  // reply (a whole document without the mark) would then seed the cache
  // last and reopen the walkthrough.
  const wizSaves = useRef<Promise<unknown>>(Promise.resolve());
  const markWizard = (v: "done" | "skipped", birthdate = "") => {
    wizSaves.current = wizSaves.current
      .then(() => saveSettings(qc, { ret_wizard_steps: { done: v } }))
      .catch(() => {});
    if (birthdate)
      wizSaves.current = wizSaves.current
        .then(() => saveSettings(qc, { birthdate }))
        // a birthdate saved on the way out changes the model's starting age
        .then(() => qc.invalidateQueries({ queryKey: ["retirement"] }),
              (e) => setBirthErr(errText(e)));
  };
  const [form, setForm] = useState<Record<string, number | string> | null>(null);

  if (q.isPending) return <PageSkeleton />;
  const r: RetirementData | undefined = q.data ?? lastGood;
  if (!r) return <FailedLine q={q} what="the retirement plan" />;
  const whatIfErr = q.isError && !q.data ? errText(q.error) : null;
  const inp = r.inputs;
  // First visit with no birthdate and default-looking age → offer wizard.
  // `ret_wizard_steps.done` is the persisted mark /retirement/setup writes on
  // BOTH exits ("done" and "skipped"); without consulting it this page would
  // re-open the walkthrough on every visit for anyone who deliberately
  // skipped it, which is the one answer it already has.
  // Wait for settings before deciding, so the wizard never flashes up and
  // then vanishes once the mark arrives.
  // Never for a viewer: the wizard's exits WRITE the mark, which a
  // viewer would hit as a 403 on first visit; a viewer sees the page as
  // it stands, with the owner's inputs, or its empty state
  const needsWizard = wizard === null
    && !viewer
    && settings.data !== undefined
    && !settings.data.ret_wizard_steps?.done
    // a walkthrough left halfway re-opens where it stopped: its first
    // answer saved the birthdate, which alone would read as "no need"
    && (!settings.data.birthdate
        || retWizardMidway(settings.data.ret_wizard_steps))
    && Object.keys(params).length === 0;
  const showWizard = wizard === true || (wizard === null && needsWizard);

  if (showWizard) {
    return (
      <>
        <h1>Retire</h1>
        <p className="mut" style={{ maxWidth: "36rem" }}>
          A short walkthrough so the model starts from <b>where you are</b>,
          not blank assumptions. You can skip and edit everything later.
        </p>
        <RetireWizard
          saves={wizSaves}
          splitSeed={!!inp.saving_hint}
          onBirthChange={(b) => { birthRef.current = b; }}
          defaults={{
            age: inp.age, spend: inp.spend,
            // the tax-deferred box is td_mo; employer_mo is the older
            // clients' figure, with the ledger's Roth inside it
            employer_mo: inp.td_mo, roth_mo: inp.roth_mo,
            taxable_mo: inp.taxable_mo, ret: inp.ret, infl: inp.infl, end: inp.end, ssage: inp.ssage,
          }}
          onDone={(p) => {
            // the answers are saved and the projection reads them; `p`
            // holds only answers that did not save, as what-ifs
            setParams(p);
            setWizard(false);
            setForm(null);
            markWizard("done");
            qc.invalidateQueries({ queryKey: ["retirement"] });
            if (forced) { search.delete("wizard"); setSearch(search, { replace: true }); }
          }} />
        <button type="button" className="linklike mut"
                style={{ background: "none", border: "none", cursor: "pointer" }}
                onClick={() => {
                  setWizard(false);
                  // persist the skip like /retirement/setup and mobile do —
                  // without the mark the walkthrough re-offers itself on
                  // every visit to anyone who deliberately skipped it.
                  // Skip abandons the remaining questions, not an answer
                  // already given — a typed birthdate still lands in
                  // settings
                  const b = birthRef.current.trim();
                  markWizard("skipped", b);
                  if (forced) { search.delete("wizard"); setSearch(search, { replace: true }); }
                }}>
          Skip — open the full model
        </button>
      </>
    );
  }

  const f = form ?? {
    age: inp.age, spend: inp.spend ?? "",
    // the server renders these as Python floats — "7.0", "3.0"
    ret: inp.ret.toFixed(1), infl: inp.infl.toFixed(1), end: inp.end,
    ssage: inp.ssage, stockpct: inp.stockpct, employer_mo: inp.td_mo,
    roth_mo: inp.roth_mo, taxable_mo: inp.taxable_mo, resume: inp.resume,
    // blank = the server's default (cash keeps pace with inflation)
    cash: inp.cash == null ? "" : inp.cash.toFixed(1),
  };
  const set = (name: string, v: string) => setForm({ ...f, [name]: v });
  const recalc = (e: React.FormEvent) => {
    e.preventDefault();
    // a blank field is "use the default", so it must not ride the query
    // string as an empty value the server would refuse to parse
    // — except the Roth box, which always rides (blank is $0): a query
    // without it is an older client's, whose employer figure holds the
    // ledger's Roth (specs/retirement-figures.md)
    setParams(Object.fromEntries(Object.entries(f)
      .map(([k, v]) => [k, k === "roth_mo" && String(v).trim() === ""
                              ? "0" : String(v)] as [string, string])
      .filter(([, v]) => v !== "")));
  };

  // the headline is the history replay; the flat rate is "steady markets"
  const v = r.verdict;
  const can = v?.age ?? null;
  // the worst stress scenario's spend and the target's margin under it,
  // both the server's
  const worst = r.stress_floor_spend;
  const margin = r.stress_floor_vs_spend;
  const lastAge = r.rows[r.rows.length - 1]?.age;
  const keyAges = keyRetireAges(r.rows.map((row) => row.age), can,
                                r.stress_ref_age);

  if (r.spend === null || r.spend_now === null || r.stress_ref_age === null) {
    // no saved spend and too little ledger to read one: nothing is
    // projected on an assumed figure — the page asks for one instead
    return (
      <>
        <h1>Retirement</h1>
        <div className="card" style={{ marginTop: 0 }}>
          <div className="sub">Retirement plan</div>
          <div style={{ fontSize: "1.4rem", fontWeight: 700 }}>
            What do you plan to spend a year?</div>
          <p className="mut" style={{ margin: ".3rem 0 0", maxWidth: "36rem" }}>
            Your ledger doesn&apos;t hold a year of spending to start from yet,
            so the plan needs the amount you expect to spend each year in
            retirement, in today&apos;s dollars. Nothing is projected until it
            has one.</p>
          {viewer
            ? <p className="sub">The household&apos;s owner can add it.</p>
            : <SpendAsk onWizard={() => setWizard(true)} />}
          {whatIfErr && (
            <Note tone="bad" style={{ marginTop: ".5rem" }}>
              Couldn&apos;t recalculate: {whatIfErr}</Note>
          )}
        </div>
      </>
    );
  }

  return (
    <>
      <h1>Retirement</h1>
      {/* HERO: the number the page exists to produce, ahead of the ten
          input fields and not one of four equal tiles below two form
          cards. */}
      <div className="card" style={{ marginTop: 0 }}>
        <div className="sub">Earliest retirement at {money(r.spend)}/yr
          <span className="mut"> · spend: {SPEND_FROM[inp.spend_source]}</span></div>
        {v && can !== null ? (
          <div style={{ display: "flex", gap: ".6rem", alignItems: "baseline",
                        flexWrap: "wrap" }}>
            <span style={{ fontSize: "2rem", fontWeight: 700,
                           letterSpacing: "-.02em" }}>age {can}</span>
            <span className="pos">lasts in {v.in_ten} of 10 past markets —
              money lasts to {inp.end}</span>
          </div>
        ) : v && (
          <div style={{ display: "flex", gap: ".6rem", alignItems: "baseline",
                        flexWrap: "wrap" }}>
            <span className="neg" style={{ fontSize: "2rem", fontWeight: 700,
                           letterSpacing: "-.02em" }}>not by {lastAge}</span>
            <span className="neg">lasts in {v.in_ten} of 10 past markets at{" "}
              {v.at_age}</span>
            {(v.catch_up ?? []).length > 0 && (
              <div style={{ flex: "1 1 100%" }}>
                To last in 9 of 10, save{" "}
                {(v.catch_up ?? []).map((c, k) => (
                  <span key={c.age}>
                    {k > 0 && ", or "}
                    <b>{money(c.extra_monthly)}/mo more</b> to retire at {c.age}
                  </span>
                ))}
                <span className="mut">{onTopOf(inp)}</span>
              </div>
            )}
          </div>
        )}
        {v && (
          <div className="sub" style={{ marginTop: ".3rem" }}>
            Steady markets ({(r.real_return * 100).toFixed(1)}% a year after
            inflation, every year):{" "}
            {v.steady_age !== null ? `age ${v.steady_age}` : `not by ${lastAge}`}
            {v.steady_age === null && (r.catch_up ?? []).length > 0 && (
              <> — save {(r.catch_up ?? []).map((c, k) => (
                <span key={c.age}>{k > 0 && " or "}{money(c.extra_monthly)}/mo
                  more to retire at {c.age}</span>))}</>)}
          </div>
        )}
        <div className="grid cols3" style={{ gap: ".6rem", marginTop: ".8rem" }}>
          <div className="tile">
            <div className="sub">Retire today ({inp.age}) supports, steady markets</div>
            <div className="kpi sm">{money(r.spend_now)}
              <span className="sub">/yr</span></div>
            <div className="sub">after-tax, today's dollars</div>
          </div>
          <div className="tile">
            <div className="sub">You have today</div>
            <div className="kpi sm">{money(r.buckets.total)}</div>
            <div className="sub">accounts less card balances — property
              isn&apos;t counted</div>
            {r.grow_to_67 != null && (
              <div className="sub">→ ~{money(r.grow_to_67)} by 67 at{" "}
                {(r.real_return * 100).toFixed(1)}%/yr real
                {!inp.saving_yr && " (saving nothing)"}</div>)}
          </div>
          {worst !== null && margin !== null && (
          <div className="tile">
            <div className="sub">Worst stress scenario, retiring at {r.stress_ref_age}</div>
            <div className="kpi sm">{money(worst)}<span className="sub">/yr</span></div>
            <div className={`sub ${margin >= 0 ? "pos" : "neg"}`}>
              {margin >= 0 ? "+" : "−"}{money(Math.abs(margin))} vs target —
              the most you could spend and survive the worst stress
              scenario</div>
          </div>)}
        </div>

        {/* the two form cards collapse to ONE summary line; their status
            sentences (SS at claim age, saving/paused) fold in here */}
        <div className="assume">
          <span><b style={{ color: "var(--ink)" }}>Assumptions:</b>{" "}
            {inp.ret}% return · {inp.infl}% inflation
            {inp.cash != null ? ` · cash ${inp.cash}%` : ""}
            {" "}· to {inp.end} · SS at {inp.ssage}
            {inp.has_sched ? ` (${money(inp.your_ss)}/mo)`
              : !viewer ? (
                <> (<button type="button" className="linklike"
                            style={{ background: "none", border: "none",
                                     cursor: "pointer", padding: 0,
                                     color: "var(--blue)", fontSize: 13 }}
                            onClick={() => setAdjust(true)}>no SSA statement</button>)</>
              ) : " (no SSA statement)"}
            {" "}· {inp.stockpct}% stocks ·{" "}
            {inp.saving_source === "none" ? (!viewer ? (
                <button type="button" className="linklike"
                        style={{ background: "none", border: "none",
                                 cursor: "pointer", padding: 0,
                                 color: "var(--blue)", fontSize: 13 }}
                        onClick={() => setAskSaving((a) => !a)}>
                  saving not set — add it</button>
              ) : "saving not set")
              : <>{inp.saving_yr
                  ? `saving ${money(inp.saving_yr)}/yr`
                    + (inp.resume > inp.age ? ` from ${inp.resume}` : "")
                  : "saving nothing"}
                {` (${SAVING_FROM[inp.saving_basis]})`}</>}</span>
          <button type="button" className="linklike"
                  style={{ marginLeft: "auto", background: "none",
                           border: "none", cursor: "pointer",
                           color: "var(--blue)", fontSize: 13 }}
                  onClick={() => setAdjust((a) => !a)}>
            {adjust ? "done ▴" : "adjust ▾"}</button>
          <button type="button" className="linklike mut"
                  style={{ background: "none", border: "none",
                           cursor: "pointer", fontSize: 13 }}
                  onClick={() => setWizard(true)}>re-run wizard</button>
        </div>
        {/* each saving figure by its tax treatment, and an offer of the
            ledger's Roth saving a saved answer leaves out — the server's
            words (specs/retirement-figures.md) */}
        {inp.saving_parts.length > 0 && (
          <div className="sub mut" style={{ marginTop: ".3rem" }}>
            {inp.saving_parts.map((p) => p.text).join(" · ")}</div>)}
        {inp.saving_hint && (
          <Note style={{ marginTop: ".4rem" }}>{inp.saving_hint}</Note>)}
        {askSaving && !viewer && inp.saving_source === "none" && <SavingAsk />}
        {birthErr && (
          <Note tone="bad" style={{ marginTop: ".5rem" }}>
            Birthdate not saved: {birthErr}</Note>
        )}
        {whatIfErr && (
          <Note tone="bad" style={{ marginTop: ".5rem" }}>
            Couldn&apos;t recalculate: {whatIfErr}</Note>
        )}
        {adjust && (
          <form onSubmit={recalc} noValidate
                style={{ background: "var(--hover)", borderRadius: "var(--rs)",
                         padding: ".6rem .8rem", marginTop: ".5rem",
                         display: "flex", gap: ".7rem", flexWrap: "wrap",
                         alignItems: "flex-end", fontSize: 13 }}>
            <Fld name="spend" label="spend $/yr" value={f.spend} step="1000"
                 w="7rem" onChange={set} />
            <Fld name="ret" label="return %" value={f.ret} step="0.1" w="4.5rem"
                 onChange={set} />
            <Fld name="infl" label="inflation %" value={f.infl} step="0.1"
                 w="4.5rem" onChange={set} />
            <Fld name="cash" label="cash yield %" value={f.cash} step="0.1"
                 w="5.5rem" ph="= inflation" onChange={set} />
            <Fld name="end" label="to age" value={f.end} w="4.5rem" onChange={set} />
            <Fld name="ssage" label="SS claim" value={f.ssage} w="4.5rem"
                 onChange={set} />
            <Fld name="stockpct" label="stock %" value={f.stockpct} step="5"
                 w="4.5rem" onChange={set} />
            <Fld name="employer_mo" label="employer $/mo" value={f.employer_mo}
                 step="50" w="6rem" onChange={set} />
            <Fld name="roth_mo" label="Roth $/mo" value={f.roth_mo}
                 step="50" w="6rem" onChange={set} />
            <Fld name="taxable_mo" label="taxable $/mo" value={f.taxable_mo}
                 step="50" w="6rem" onChange={set} />
            <Fld name="resume" label="resume at" value={f.resume} w="5rem"
                 onChange={set} />
            <button className="pri">Recalculate</button>
          </form>
        )}
        {/* a cached copy may be older than another device's save, and saving
            these boxes replaces the schedule: they wait for a read made since
            the page opened */}
        {adjust && !viewer && settings.data && ssFresh && (
          <SsEstimates saved={settings.data.ss_estimates} />
        )}
        {adjust && !viewer && !ssFresh && (
          settings.isError
            ? <FailedLine q={settings} what="your Social Security estimates" />
            : <div className="sub" style={{ marginTop: ".5rem" }}>
                Loading your Social Security estimates…</div>
        )}
      </div>

      <div className="card">
        <h2>Projected portfolio <span className="sub" style={{ fontWeight: 400 }}>
          — {r.growth_path_retire_age !== null
            ? `retiring at ${r.growth_path_retire_age}, the middle of past markets, `
            : ""}today's dollars</span></h2>
        <AreaChart points={r.growth_path} xticks="years"
                   subject="Projected portfolio in today's dollars" />
      </div>

      <div className="grid cols2">
        <div className="card">
          <h2>By tax treatment</h2>
          <table><tbody>
            <tr><td>Tax-deferred (401k/403b/457/Trad IRA)</td>
              <td className="num">{money(r.buckets.td)}</td>
              <td className="mut">income tax on withdrawal</td></tr>
            <tr><td>Roth</td><td className="num">{money(r.buckets.roth)}</td>
              <td className="mut">tax-free</td></tr>
            <tr><td>Taxable + crypto</td><td className="num">{money(r.buckets.taxable)}</td>
              <td className="mut">capital gains</td></tr>
            <tr><td>Cash</td><td className="num">{money(r.buckets.cash)}</td>
              <td className="mut">{r.buckets.cards_from_cash > 0
                ? `after ${money(r.buckets.cards_from_cash)} of card balances`
                : "—"}</td></tr>
          </tbody></table>
        </div>
      </div>

      {r.hist && (
        <div className="card">
          <h2>Replayed against history <span className="sub" style={{ fontWeight: 400 }}>
            — 1928–2025, {Math.round(r.hist.stock_frac * 100)}/{Math.round((1 - r.hist.stock_frac) * 100)} stocks/bonds</span></h2>
          <div style={{ display: "flex", gap: "1.6rem", flexWrap: "wrap", alignItems: "baseline" }}>
            <div>
              <span className="sub">retiring at {r.stress_ref_age} on target</span>
              <div className={`kpi sm ${r.hist.pct >= 90 ? "pos" : r.hist.pct < 75 ? "neg" : ""}`}>
                {r.hist.pct.toFixed(1)}%</div>
              <div className="sub">survived {r.hist.ok} of {r.hist.n} historical sequences</div>
            </div>
            <div>
              <span className="sub">spend with 90% historical success</span>
              <div className="kpi sm">{money(r.hist.spend90)}<span className="sub">/yr</span></div>
            </div>
            <div>
              <span className="sub">the most you could spend and survive
                every year since 1928</span>
              <div className="kpi sm">{money(r.hist.spend100)}<span className="sub">/yr</span></div>
            </div>
          </div>
          <div className="sub" style={{ marginTop: ".4rem" }}>
            Every contiguous return sequence since 1928 (real S&amp;P + 10y Treasury,
            wrapping), your exact tax-aware draw order and RMDs — deterministic, not
            simulated randomness.</div>
        </div>
      )}

      <div className="card">
        <h2>Stress test <span className="sub" style={{ fontWeight: 400 }}>— a bad market the day you retire</span></h2>
        <table>
          <thead><tr><th>Scenario</th>
            <th>Earliest at target</th>
            <th className="num">Max spend at {r.stress_ref_age}</th></tr></thead>
          <tbody>
            {r.stress.map((s) => (
              <tr key={s.label}>
                <td>{s.label}</td>
                <td>{s.earliest !== null
                  ? <span className="pill g">age {s.earliest}</span>
                  : <span className="pill r">not by {lastAge}</span>}</td>
                <td className={`num ${s.under_target ? "neg" : ""}`}>
                  {money(s.max_spend_at_ref)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="card">
        <h2>Retire at each age</h2>
        <table>
          <thead><tr>
            <th>Retire at age</th><th className="num hide-m">Portfolio then</th>
            <th className="num">Past markets</th>
            <th>Steady markets</th>
            <th className="num">Max spend/yr</th>
          </tr></thead>
          <tbody>
            {(everyAge ? r.rows : r.rows.filter((row) => keyAges.has(row.age)))
              .map((row) => (
              <tr key={row.age}
                  style={row.age === can ? { background: "rgba(92,181,107,.10)" } : undefined}>
                <td>{row.age}{row.age === can &&
                  <> <span className="pill g">earliest</span></>}</td>
                <td className="num hide-m">{money(row.at_retire)}</td>
                {/* one verdict a row, the headline's: the replay of past
                    markets carries the colour; the steady model is the
                    second reading, in plain text */}
                <td className={`num ${row.hist_lasts ? "pos" : "neg"}`}>
                  {row.hist_pct !== null ? `${row.hist_pct.toFixed(1)}%` : "·"}</td>
                <td className="mut">{row.feasible ? "lasts"
                  : row.fail_age ? `runs out at ${row.fail_age}` : "no"}</td>
                <td className={`num ${row.meets_target ? "pos" : ""}`}>
                  {money(row.max_spend)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {keyAges.size < r.rows.length && (
          <button type="button" className="show-more"
                  aria-expanded={everyAge}
                  onClick={() => setEveryAge(!everyAge)}>
            {everyAge ? "Show key ages only"
              : `Show every age (${r.rows.length})`}</button>)}
        {/* how to read this, in the one place every term in it appears —
            rather than two permanent paragraphs above the table */}
        <p className="sub" style={{ margin: ".6rem 0 0" }}>
          Today's dollars, after tax, drawn cash → taxable → tax-deferred →
          Roth; Social Security from your claim age; RMDs from 72, 73 or 75
          by birth year (IRS Uniform Lifetime Table), with any excess
          reinvested taxable. “Past markets” is the share of every return
          sequence since 1928 that lasted to {inp.end} — green from 9 in 10,
          which is how the earliest age is chosen. “Steady markets” is the
          same plan at {inp.ret}% a year every year; “Max spend” is the
          largest annual draw that just lasts to {inp.end} there.
        </p>
      </div>
    </>
  );
}
