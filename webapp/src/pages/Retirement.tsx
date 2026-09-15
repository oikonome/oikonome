// Retirement — what-if assumption form, 4 KPI cards, growth chart,
// tax-treatment table, historical replay, stress table, retire-at-each-age
// table. Plus a first-run wizard that walks through plain questions before
// the full model.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { useSearchParams } from "react-router";
import { isViewer } from "../role";
import { api, money, type RetirementData } from "../api/client";
import { saveSettings } from "../api/cache";
import AreaChart from "../components/AreaChart";
import RetireWizard from "../components/RetireWizard";

function Fld({ name, label, value, step = "1", w = "6rem", onChange }: {
  name: string; label: string; value: number | string; step?: string; w?: string;
  onChange: (name: string, v: string) => void;
}) {
  return (
    <label style={{ display: "flex", flexDirection: "column", fontSize: 12,
                    color: "var(--mut)", gap: ".15rem" }}>
      {label}
      <input type="number" name={name} value={value} step={step}
             style={{ width: w }} onChange={(e) => onChange(name, e.target.value)} />
    </label>
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
    queryKey: ["retirement", params],
    queryFn: () => api.retirement(params),
    placeholderData: (prev) => prev,
  });
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const meQ = useQuery({ queryKey: ["me"], queryFn: api.me });
  const viewer = isViewer(meQ);
  const qc = useQueryClient();
  // the wizard reports each birthdate keystroke here so Skip can save it
  const birthRef = useRef("");
  // persist both wizard exits (done AND skipped) — the needsWizard gate
  // below reads this mark, and every exit has to write it
  const markWizard = (v: "done" | "skipped",
                      extra: Record<string, unknown> = {}) => {
    saveSettings(qc, { ret_wizard_steps: { done: v }, ...extra })
      .then(() => {
        // a birthdate saved on the way out changes the model's starting age
        if (extra.birthdate)
          qc.invalidateQueries({ queryKey: ["retirement"] });
      })
      .catch(() => {});
  };
  const [form, setForm] = useState<Record<string, number | string> | null>(null);

  if (q.isPending) return <p className="mut" style={{ marginTop: "2rem" }}>loading…</p>;
  if (q.isError) return <div className="note bad">Couldn't load: {String(q.error)}</div>;
  const r: RetirementData = q.data;
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
                  markWizard("skipped", b ? { birthdate: b } : {});
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
  };
  const set = (name: string, v: string) => setForm({ ...f, [name]: v });
  const recalc = (e: React.FormEvent) => {
    e.preventDefault();
    setParams(Object.fromEntries(Object.entries(f).map(([k, v]) => [k, String(v)])));
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
            <div className="sub">→ ~{money(r.grow_to_67)} by 67 at{" "}
              {(r.real_return * 100).toFixed(1)}%/yr real
              {!inp.saving_yr && " (saving nothing)"}</div>
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
            · to {inp.end} · SS at {inp.ssage}
            {inp.has_sched ? ` (${money(inp.your_ss)}/mo)` : " (no SSA statement)"}
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
        {adjust && (
          <form onSubmit={recalc}
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
