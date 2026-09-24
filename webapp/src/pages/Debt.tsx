// Debt — every card and loan on one payoff schedule: the debt-free date
// hero, the method (avalanche / snowball) and extra-payment what-ifs, the
// balance curve under both methods, and the debts in payoff order with
// their rate and minimum editable in place. The plan (method, extra,
// overrides) saves per household; method and extra also run unsaved as
// what-ifs, the retirement page's pattern.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { isViewer } from "../role";
import { api, errText, money, type DebtMethod, type DebtPlan, type DebtRow,
         type DebtSchedule } from "../api/client";
import AreaChart from "../components/AreaChart";

const NAMES: Record<DebtMethod, string> = { avalanche: "Avalanche", snowball: "Snowball" };
const OTHER: Record<DebtMethod, DebtMethod> = { avalanche: "snowball", snowball: "avalanche" };
const KIND: Record<DebtRow["kind"], string> = { card: "card", mortgage: "mortgage", loan: "loan" };

// '2029-03-01' → 'Mar 2029': the schedule walks month firsts, and the
// day would read as a fact the walk never claimed
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export const monthYear = (iso: string | null | undefined) => {
  if (!iso) return "";
  const m = Number(iso.slice(5, 7)) - 1;
  return `${MONTHS[m] ?? ""} ${iso.slice(0, 4)}`;
};
export const span = (months: number | null) => {
  if (months === null) return "not within fifty years";
  if (months === 0) return "today";
  const y = Math.floor(months / 12), m = months % 12;
  const parts = [];
  if (y) parts.push(`${y} yr`);
  if (m) parts.push(`${m} mo`);
  return parts.join(" ");
};

function Fld({ label, value, step = "1", w = "6rem", ph, onChange }: {
  label: string; value: number | string; step?: string; w?: string;
  ph?: string; onChange: (v: string) => void;
}) {
  return (
    <label style={{ display: "flex", flexDirection: "column", fontSize: 12,
                    color: "var(--mut)", gap: ".15rem" }}>
      {label}
      <input type="number" value={value} step={step} min="0" placeholder={ph}
             style={{ width: w }} onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}

type Draft = Record<string, { apr: string; min_payment: string; skip: boolean }>;

const draftOf = (rows: DebtRow[]): Draft => Object.fromEntries(rows.map((d) => [d.id, {
  // the box shows what the household typed; an issuer/estimated value
  // sits in the placeholder so clearing the box means "back to that"
  apr: d.apr_source === "you" ? String(d.apr) : "",
  min_payment: d.min_source === "you" ? String(d.min_payment) : "",
  skip: d.skip,
}]));

export default function Debt() {
  const [params, setParams] = useState<Record<string, string>>({});
  const q = useQuery({
    queryKey: ["debt-plan", params],
    queryFn: () => api.debtPlan(params),
    placeholderData: (prev) => prev,
  });
  const meQ = useQuery({ queryKey: ["me"], queryFn: api.me });
  const viewer = isViewer(meQ);
  const qc = useQueryClient();
  const [extra, setExtra] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: (body: Parameters<typeof api.debtPlanSave>[0]) => api.debtPlanSave(body),
    onSuccess: (d) => {
      // the reply IS the saved plan: seed the unparameterised key and
      // drop the what-if so the page reads what it just wrote
      qc.setQueryData(["debt-plan", {}], d);
      qc.invalidateQueries({ queryKey: ["debt-plan"] });
      setParams({}); setExtra(null); setDraft(null); setErr(null);
    },
    onError: (e) => setErr(errText(e)),
  });

  if (q.isPending) return <p className="mut" style={{ marginTop: "2rem" }}>loading…</p>;
  if (q.isError) return <div className="note bad">Couldn't load: {String(q.error)}</div>;
  const r: DebtPlan = q.data;
  const method = r.method;
  const plan = r.plan, alt = r.alt, base = r.minimums;
  const live = r.debts.filter((d) => !d.skip);
  const owed = live.reduce((s, d) => s + d.balance, 0);
  const extraStr = extra ?? String(r.extra_monthly);
  const d = draft ?? draftOf(r.debts);
  const unsaved = method !== r.saved.method || r.extra_monthly !== r.saved.extra_monthly
    || draft !== null;
  const byId = Object.fromEntries(plan.debts.map((x) => [x.id, x]));
  const ordered = [...r.debts].sort((a, b) =>
    (byId[a.id]?.order ?? 999) - (byId[b.id]?.order ?? 999));

  const recalc = (e: React.FormEvent) => {
    e.preventDefault();
    setParams({ method, extra: extraStr === "" ? "0" : extraStr });
  };
  const pick = (m: DebtMethod) => setParams({ method: m, extra: extraStr === "" ? "0" : extraStr });
  const setRow = (id: string, patch: Partial<Draft[string]>) =>
    setDraft({ ...d, [id]: { ...d[id], ...patch } });
  const persist = () => save.mutate({
    method, extra_monthly: extraStr === "" ? 0 : extraStr,
    debts: Object.fromEntries(Object.entries(d).map(([id, v]) => [id, {
      apr: v.apr === "" ? null : v.apr,
      min_payment: v.min_payment === "" ? null : v.min_payment,
      skip: v.skip,
    }])),
  });
  const saved = (s: DebtSchedule) => base.total_interest - s.total_interest;

  if (r.debts.length === 0) {
    return (
      <>
        <h1>Debt</h1>
        <div className="card" style={{ marginTop: 0 }}>
          <div className="kpi sm pos">Nothing owed</div>
          <div className="sub">No card or loan carries a balance. A debt
            on an account the Accounts page excludes stays out of the plan
            too.</div>
        </div>
      </>
    );
  }

  return (
    <>
      <h1>Debt</h1>
      {/* HERO: the date the page exists to produce */}
      <div className="card" style={{ marginTop: 0 }}>
        <div className="sub">Debt-free — {NAMES[method].toLowerCase()}
          {r.extra_monthly > 0 ? `, ${money(r.extra_monthly)}/mo extra` : ", minimums only rolled"}
          {unsaved && <span className="pill a" style={{ marginLeft: ".5rem" }}>unsaved what-if</span>}
        </div>
        {plan.debt_free ? (
          <div style={{ display: "flex", gap: ".6rem", alignItems: "baseline", flexWrap: "wrap" }}>
            <span style={{ fontSize: "2rem", fontWeight: 700, letterSpacing: "-.02em" }}>
              {monthYear(plan.debt_free)}</span>
            <span className="pos">in {span(plan.months)} — {money(owed)} across{" "}
              {live.length} {live.length === 1 ? "debt" : "debts"}</span>
          </div>
        ) : (
          <div style={{ display: "flex", gap: ".6rem", alignItems: "baseline", flexWrap: "wrap" }}>
            <span className="neg" style={{ fontSize: "2rem", fontWeight: 700, letterSpacing: "-.02em" }}>
              not within 50 years</span>
            <span className="neg">{plan.growing
              ? "the minimums don't cover the interest — the balance grows"
              : "at these payments"}</span>
          </div>
        )}
        <div className="grid cols3" style={{ gap: ".6rem", marginTop: ".8rem" }}>
          <div className="tile">
            <div className="sub">Paying monthly</div>
            <div className="kpi sm">{money(plan.monthly)}</div>
            <div className="sub">every minimum + {money(plan.extra)} extra</div>
          </div>
          <div className="tile">
            <div className="sub">Interest to pay</div>
            <div className="kpi sm">{money(plan.total_interest)}</div>
            <div className={`sub ${saved(plan) > 0 ? "pos" : ""}`}>
              {saved(plan) > 0
                ? `saves ${money(saved(plan))} vs minimums only`
                : base.debt_free
                  ? `minimums only: ${money(base.total_interest)}`
                  : "minimums only never finish"}
            </div>
          </div>
          <div className="tile">
            <div className="sub">{NAMES[OTHER[method]]} instead</div>
            <div className="kpi sm">{alt.debt_free ? monthYear(alt.debt_free) : "never"}</div>
            <div className={`sub ${alt.total_interest > plan.total_interest ? "" : "pos"}`}>
              {money(alt.total_interest)} interest
              {alt.total_interest !== plan.total_interest && (
                alt.total_interest > plan.total_interest
                  ? ` — ${money(alt.total_interest - plan.total_interest)} more`
                  : ` — ${money(plan.total_interest - alt.total_interest)} less`)}
            </div>
          </div>
        </div>

        <form onSubmit={recalc} className="assume"
              style={{ display: "flex", gap: ".7rem", flexWrap: "wrap", alignItems: "flex-end" }}>
          <div className="seg" role="radiogroup" aria-label="Payoff method"
               style={{ display: "inline-flex", gap: ".3rem" }}>
            {(["avalanche", "snowball"] as DebtMethod[]).map((m) => (
              <button key={m} type="button" role="radio" aria-checked={method === m}
                      className={method === m ? "pri" : undefined}
                      onClick={() => pick(m)}>{NAMES[m]}</button>
            ))}
          </div>
          <Fld label="extra $/mo" value={extraStr} step="25" w="6rem"
               onChange={setExtra} />
          <button className="pri" type="submit">Recalculate</button>
          {!viewer && (
            <button type="button" onClick={persist} disabled={save.isPending || !unsaved}>
              {save.isPending ? "Saving…" : "Save plan"}</button>
          )}
          <span className="sub" style={{ flex: "1 1 100%" }}>
            {method === "avalanche"
              ? "Avalanche pays the highest rate first — the least interest."
              : "Snowball clears the smallest balance first — the earliest win."}
            {" "}Every minimum keeps being paid; the extra, and each retired
            debt's minimum, roll onto the next target.
          </span>
        </form>
        {err && <div className="note bad" style={{ marginTop: ".5rem" }}>{err}</div>}
      </div>

      <div className="card">
        <h2>Balance owed <span className="sub" style={{ fontWeight: 400 }}>
          — {NAMES[method].toLowerCase()} vs {NAMES[OTHER[method]].toLowerCase()}, same money</span></h2>
        <AreaChart points={plan.series} second={alt.series}
                   labels={[NAMES[method], NAMES[OTHER[method]]]} xticks="years" />
      </div>

      <div className="card">
        <h2>In payoff order</h2>
        <table>
          <thead><tr>
            <th>#</th><th>Debt</th><th className="num">Balance</th>
            <th className="num">APR %</th><th className="num">Min $/mo</th>
            <th>Paid off</th><th className="num">Interest</th><th>Skip</th>
          </tr></thead>
          <tbody>
            {ordered.map((x) => {
              const p = byId[x.id];
              const row = d[x.id];
              return (
                <tr key={x.id} style={x.skip ? { opacity: .5 } : undefined}>
                  <td className="mut">{p ? p.order : "—"}</td>
                  <td>{x.name}{x.mask ? <span className="mut"> ···{x.mask}</span> : null}
                    <div className="sub">{x.institution ? `${x.institution} · ` : ""}{KIND[x.kind]}</div></td>
                  <td className="num">{money(x.balance)}</td>
                  <td className="num">
                    {viewer ? x.apr.toFixed(2) : (
                      <input type="number" step="0.01" min="0" max="100" value={row.apr}
                             placeholder={x.apr_issuer != null ? x.apr_issuer.toFixed(2) : "0"}
                             style={{ width: "5rem" }}
                             onChange={(e) => setRow(x.id, { apr: e.target.value })} />)}
                    <div className="sub">{x.apr_source === "you" ? "yours"
                      : x.apr_source === "issuer" ? "issuer" : "unknown — 0%"}</div>
                  </td>
                  <td className="num">
                    {viewer ? money(x.min_payment) : (
                      <input type="number" step="1" min="0" value={row.min_payment}
                             placeholder={String(Math.round(x.min_issuer ?? x.min_payment))}
                             style={{ width: "5.5rem" }}
                             onChange={(e) => setRow(x.id, { min_payment: e.target.value })} />)}
                    <div className="sub">{x.min_source === "you" ? "yours"
                      : x.min_source === "issuer" ? "issuer" : "estimate"}</div>
                    {x.kind === "mortgage" && x.payment_reported != null
                      ? <div className="sub">Reported payment incl. escrow {money(x.payment_reported)}</div>
                      : null}
                  </td>
                  <td>{x.skip ? <span className="pill m">left out</span>
                    : p?.paid_off ? <span className="pill g">{monthYear(p.paid_off)}</span>
                    : <span className="pill r">never</span>}</td>
                  <td className="num">{p ? money(p.interest) : "·"}</td>
                  <td>{viewer ? (x.skip ? "yes" : "") : (
                    <input type="checkbox" checked={row.skip}
                           onChange={(e) => setRow(x.id, { skip: e.target.checked })} />)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <p className="sub">
          Rates and minimums come from the bank where it reports them, else
          from what you type here; an <b>estimate</b> is 2% of a card's
          balance (at least $25), a five-year payment for a loan, thirty
          years for a mortgage — correct it and save. Minimums are held
          flat for the whole walk, and interest accrues monthly at APR ÷ 12.
          {!viewer && draft !== null && " Changes here apply when you save the plan."}
        </p>
      </div>
    </>
  );
}
