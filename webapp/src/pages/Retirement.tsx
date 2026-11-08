// Retirement — what-if assumption form, 4 KPI cards, growth chart,
// tax-treatment table, historical replay, stress table, retire-at-each-age
// table. Plus a first-run wizard that walks through plain questions before
// the full model.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { useSearchParams } from "react-router";
import { isViewer } from "../role";
import { api, errText, money, pyround, type RetirementData } from "../api/client";
import { saveSettings } from "../api/cache";
import AreaChart from "../components/AreaChart";
import RetireWizard from "../components/RetireWizard";
import { parseMoneyDraft } from "../moneymath";
import { rebaseSsDraft, ssDraftOf } from "../ssDraft";
import { SS_CLAIM_AGES, ssEstimatesBody } from "../ssEstimates";

// /api/retirement takes the dollar boxes as whole numbers and refuses
// "250.50" or "60,000" outright, so they are parsed and rounded before
// they ride the query — the form, the wizard and a /retirement/setup URL
// all pass through here. Text that is not an amount rides as typed and
// the refusal shows beside the form.
const MONEY_KEYS = ["spend", "employer_mo", "taxable_mo"];
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
      {err && <div className="note bad" style={{ marginTop: ".5rem" }}>{err}</div>}
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
  const markWizard = (v: "done" | "skipped", birthdate = "") => {
    saveSettings(qc, { ret_wizard_steps: { done: v } }).catch(() => {});
    if (birthdate)
      saveSettings(qc, { birthdate })
        // a birthdate saved on the way out changes the model's starting age
        .then(() => qc.invalidateQueries({ queryKey: ["retirement"] }),
              (e) => setBirthErr(errText(e)));
  };
  const [form, setForm] = useState<Record<string, number | string> | null>(null);

  if (q.isPending) return <p className="mut" style={{ marginTop: "2rem" }}>loading…</p>;
  const r: RetirementData | undefined = q.data ?? lastGood;
  if (!r) return <div className="note bad">Couldn't load: {errText(q.error)}</div>;
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
    && !settings.data.birthdate
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
          onBirthChange={(b) => { birthRef.current = b; }}
          defaults={{
            age: inp.age, spend: inp.spend,
            employer_mo: inp.employer_mo, taxable_mo: inp.taxable_mo,
            ret: inp.ret, infl: inp.infl, end: inp.end,
          }}
          onDone={(p) => {
            setParams(p);
            setWizard(false);
            setForm(null);
            markWizard("done");
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
    age: inp.age, spend: inp.spend,
    // the server renders these as Python floats — "7.0", "3.0"
    ret: inp.ret.toFixed(1), infl: inp.infl.toFixed(1), end: inp.end,
    ssage: inp.ssage, stockpct: inp.stockpct, employer_mo: inp.employer_mo,
    taxable_mo: inp.taxable_mo, resume: inp.resume,
    // blank = the server's default (cash keeps pace with inflation)
    cash: inp.cash == null ? "" : inp.cash.toFixed(1),
  };
  const set = (name: string, v: string) => setForm({ ...f, [name]: v });
  const recalc = (e: React.FormEvent) => {
    e.preventDefault();
    // a blank field is "use the default", so it must not ride the query
    // string as an empty value the server would refuse to parse
    setParams(Object.fromEntries(Object.entries(f)
      .filter(([, v]) => String(v) !== "")
      .map(([k, v]) => [k, String(v)])));
  };

  const can = r.earliest;
  const worst = Math.min(...r.stress.filter((s) => s.label !== "Average markets")
                                   .map((s) => s.max_spend_at_ref));
  const margin = worst - r.spend;
  const lastAge = r.rows[r.rows.length - 1]?.age;

  return (
    <>
      <h1>Retirement</h1>
      {/* HERO: the number the page exists to produce, ahead of the ten
          input fields and not one of four equal tiles below two form
          cards. */}
      <div className="card" style={{ marginTop: 0 }}>
        <div className="sub">Earliest retirement at {money(r.spend)}/yr</div>
        {can !== null ? (
          <div style={{ display: "flex", gap: ".6rem", alignItems: "baseline",
                        flexWrap: "wrap" }}>
            <span style={{ fontSize: "2rem", fontWeight: 700,
                           letterSpacing: "-.02em" }}>age {can}</span>
            <span className="pos">feasible — money lasts to {inp.end}</span>
          </div>
        ) : (
          <div style={{ display: "flex", gap: ".6rem", alignItems: "baseline",
                        flexWrap: "wrap" }}>
            <span className="neg" style={{ fontSize: "2rem", fontWeight: 700,
                           letterSpacing: "-.02em" }}>not by {lastAge}</span>
            <span className="neg">not sustainable on this portfolio without
              more saving</span>
            {(r.catch_up ?? []).length > 0 && (
              <div style={{ flex: "1 1 100%" }}>
                To get back on track, save{" "}
                {(r.catch_up ?? []).map((c, k) => (
                  <span key={c.age}>
                    {k > 0 && ", or "}
                    <b>{money(c.extra_monthly)}/mo more</b> to retire at {c.age}
                  </span>
                ))}
                <span className="mut"> — starting now, on top of what the
                  plan already saves.</span>
              </div>
            )}
          </div>
        )}
        <div className="grid cols3" style={{ gap: ".6rem", marginTop: ".8rem" }}>
          <div className="tile">
            <div className="sub">Retire today ({inp.age}) supports</div>
            <div className="kpi sm">{money(r.spend_now)}
              <span className="sub">/yr</span></div>
            <div className="sub">after-tax, today's dollars</div>
          </div>
          <div className="tile">
            <div className="sub">You have today</div>
            <div className="kpi sm">{money(r.buckets.total)}</div>
            {r.grow_to_67 != null && (
              <div className="sub">→ ~{money(r.grow_to_67)} by 67 at{" "}
                {(r.real_return * 100).toFixed(1)}%/yr real
                {!inp.saving_yr && " (saving nothing)"}</div>)}
          </div>
          <div className="tile">
            <div className="sub">Crash-proof spend at {r.stress_ref_age}</div>
            <div className="kpi sm">{money(worst)}<span className="sub">/yr</span></div>
            <div className={`sub ${margin >= 0 ? "pos" : "neg"}`}>
              {margin >= 0 ? "+" : "−"}{money(Math.abs(margin))} vs target —
              survives the worst stress scenario</div>
          </div>
        </div>

        {/* the two form cards collapse to ONE summary line; their status
            sentences (SS at claim age, saving/paused) fold in here */}
        <div className="assume">
          <span><b style={{ color: "var(--ink)" }}>Assumptions:</b>{" "}
            {inp.ret}% return · {inp.infl}% inflation
            {inp.cash != null ? ` · cash ${inp.cash}%` : ""}
            · to {inp.end} · SS at {inp.ssage}
            {inp.has_sched ? ` (${money(inp.your_ss)}/mo)`
              : !viewer ? (
                <> (<button type="button" className="linklike"
                            style={{ background: "none", border: "none",
                                     cursor: "pointer", padding: 0,
                                     color: "var(--blue)", fontSize: 13 }}
                            onClick={() => setAdjust(true)}>no SSA statement</button>)</>
              ) : " (no SSA statement)"}
            {" "}· {inp.stockpct}% stocks ·{" "}
            {inp.saving_yr
              ? `saving ${money(inp.saving_yr)}/yr`
                + (inp.resume > inp.age ? ` from ${inp.resume}` : "")
              : "saving paused"}</span>
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
        {birthErr && (
          <div className="note bad" style={{ marginTop: ".5rem" }}>
            Birthdate not saved: {birthErr}</div>
        )}
        {whatIfErr && (
          <div className="note bad" style={{ marginTop: ".5rem" }}>
            Couldn&apos;t recalculate: {whatIfErr}</div>
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
          <div className="sub" style={{ marginTop: ".5rem" }}>
            {settings.isError
              ? "Couldn't load your Social Security estimates — reload to retry."
              : "Loading your Social Security estimates…"}
          </div>
        )}
      </div>

      <div className="card">
        <h2>Projected portfolio <span className="sub" style={{ fontWeight: 400 }}>
          — today's dollars</span></h2>
        <AreaChart points={r.growth_path} xticks="years" />
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
              <td className="mut">—</td></tr>
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
              <span className="sub">spend that survived ALL of history</span>
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
                <td className={`num ${s.max_spend_at_ref < r.spend ? "neg" : ""}`}>
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
            <th>Retire at age</th><th className="num">Portfolio then</th>
            <th>At target?</th>
            <th className="num">Max spend/yr</th>
            <th className="num">History</th>
          </tr></thead>
          <tbody>
            {r.rows.map((row) => (
              <tr key={row.age}
                  style={row.age === can ? { background: "rgba(92,181,107,.10)" } : undefined}>
                <td>{row.age}{row.age === can &&
                  <> <span className="pill g">earliest</span></>}</td>
                <td className="num">{money(row.at_retire)}</td>
                <td>{row.feasible
                  ? <span className="pill g">yes</span>
                  : <span className="pill r">no{row.fail_age
                      ? ` · runs out at ${row.fail_age}` : ""}</span>}</td>
                <td className={`num ${row.max_spend >= r.spend ? "pos" : ""}`}>
                  {money(row.max_spend)}</td>
                <td className={`num ${(row.hist_pct ?? 0) >= 90 ? "pos"
                    : (row.hist_pct ?? 0) < 75 ? "neg" : ""}`}>
                  {row.hist_pct !== null ? `${row.hist_pct.toFixed(1)}%` : "·"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {/* how to read this, in the one place every term in it appears —
            rather than two permanent paragraphs above the table */}
        <p className="sub" style={{ margin: ".6rem 0 0" }}>
          Today's dollars, after tax, drawn cash → taxable → tax-deferred →
          Roth; Social Security from your claim age; RMDs from 72, 73 or 75
          by birth year (IRS Uniform Lifetime Table), with any excess
          reinvested taxable. “Max spend” is
          the largest annual draw that just lasts to {inp.end}. “History” is
          the share of every return sequence since 1928 that survived.
        </p>
      </div>
    </>
  );
}
