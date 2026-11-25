// Debt — every card and loan on one payoff schedule: the debt-free date
// hero, the method (avalanche / snowball) and extra-payment what-ifs, the
// balance curve under both methods, and the debts in payoff order with
// their rate and minimum editable in place. The plan (method, extra,
// overrides) saves per household; method and extra also run unsaved as
// what-ifs, the retirement page's pattern.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { failLine, failureStatus, parseMoneyDraft, rebaseFields, rebaseKeyed }
  from "../moneymath";
import { FailedLine, NoAccountsLine, useNoAccounts } from "../components/Loaded";
import { isViewer } from "../role";
import { api, errText, money, type DebtMethod, type DebtPlan, type DebtPlanSave,
         type DebtRow } from "../api/client";
import AreaChart from "../components/AreaChart";
import PageSkeleton from "../components/PageSkeleton";
import SegmentedControl from "../components/SegmentedControl";
import Note from "../components/Note";

const NAMES: Record<DebtMethod, string> = { avalanche: "Avalanche", snowball: "Snowball" };
const OTHER: Record<DebtMethod, DebtMethod> = { avalanche: "snowball", snowball: "avalanche" };
const KIND: Record<DebtRow["kind"], string> = { card: "card", mortgage: "mortgage", loan: "loan" };
// where the walked minimum came from, as the row's caption says it
const MIN_FROM: Record<DebtRow["min_basis"], string> = {
  you: "yours", issuer: "issuer", estimate: "estimate",
  ledger: "from your payments",
};

// '2029-03-01' → 'Mar 2029': the schedule walks month firsts, and the
// day would read as a fact the walk never claimed
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export const monthYear = (iso: string | null | undefined) => {
  if (!iso) return "";
  const m = Number(iso.slice(5, 7)) - 1;
  return `${MONTHS[m] ?? ""} ${iso.slice(0, 4)}`;
};
// whether a schedule ends inside the fifty-year walk; one that does not
// carries fifty years of compounding as its interest, which nobody pays
export const finishes = (x: { finishes?: boolean; debt_free: string | null }) =>
  x.finishes ?? x.debt_free !== null;
// what to say in place of an interest figure for a schedule that never ends
export const noEnd = (x: { growing: boolean }) =>
  x.growing ? "grows — the minimums don't cover the interest"
    : "not paid off within fifty years";

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
type PlanFields = { method: DebtMethod; extra_monthly: number | string };

const draftOf = (rows: DebtRow[]): Draft => Object.fromEntries(rows.map((d) => [d.id, {
  // the box shows what the household typed; an issuer/estimated value
  // sits in the placeholder so clearing the box means "back to that"
  apr: d.apr_source === "you" ? String(d.apr) : "",
  min_payment: d.min_source === "you" ? String(d.min_payment) : "",
  skip: d.skip,
}]));

// The planner reply also carries every stored override (the shared
// type predates it, so it is optional here).
type WithOverrides = DebtPlan & { overrides?: DebtPlanSave["debts"] };

// Stored overrides whose debt is not in the map being saved.
const keptOverrides = (stored: DebtPlanSave["debts"] | undefined,
                       saving: Record<string, unknown>): DebtPlanSave["debts"] =>
  Object.fromEntries(Object.entries(stored ?? {})
    .filter(([id]) => !Object.prototype.hasOwnProperty.call(saving, id)));

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
  // the overrides as they were when the first box was edited
  const [opened, setOpened] = useState<Draft | null>(null);
  const noAccounts = useNoAccounts();
  const [err, setErr] = useState<string | null>(null);
  const save = useMutation({
    // The server replaces the whole plan, and the draft is a copy from
    // when this page loaded: another device may have saved since. Read
    // the stored plan now and lay only this page's changes over it.
    mutationFn: async (v: { plan: PlanFields; planBase: PlanFields;
                            mine: Draft; base: Draft }) => {
      const fresh: WithOverrides = await api.debtPlan();
      const plan = rebaseFields(v.plan, v.planBase, fresh.saved);
      const debts = rebaseKeyed(v.mine, v.base, draftOf(fresh.debts));
      return api.debtPlanSave({
        method: plan.method, extra_monthly: plan.extra_monthly,
        // the list holds only debts carrying a balance; a stored override
        // of one off it (paid to zero, excluded) rides along untouched, or
        // this save would erase it
        debts: { ...keptOverrides(fresh.overrides, debts), ...Object.fromEntries(Object.entries(debts).map(([id, x]) => [id, {
          apr: x.apr === "" ? null : x.apr,
          min_payment: x.min_payment === "" ? null : x.min_payment,
          skip: x.skip,
        }])) },
      });
    },
    onSuccess: (d) => {
      // the reply IS the saved plan: seed the unparameterised key and
      // drop the what-if so the page reads what it just wrote
      qc.setQueryData(["debt-plan", {}], d);
      qc.invalidateQueries({ queryKey: ["debt-plan"] });
      setParams({}); setExtra(null); setDraft(null); setOpened(null);
      setErr(null);
    },
    onError: (e) => setErr(errText(e)),
  });

  // A failed what-if leaves q.data empty (placeholderData only covers the
  // pending fetch), and every control lives under `r`. Keep the last plan
  // the server did send, so a refused amount is corrected in place, beside
  // the fields, instead of taking the whole page with it (the phone's rule).
  const [lastGood, setLastGood] = useState<DebtPlan | undefined>();
  if (q.data && q.data !== lastGood) setLastGood(q.data);

  if (q.isPending) return <PageSkeleton />;
  const kept = q.data ?? lastGood;
  if (kept === undefined) return <FailedLine q={q} what="the debt plan" />;
  const r: DebtPlan = kept;
  // a refusal (a 4xx about what was sent) is the server's own sentence, as
  // on the phone; anything else is the failed line's words
  const whatIfStatus = failureStatus(q.error);
  const whatIfErr = q.isError && !q.data
    ? (whatIfStatus !== null && whatIfStatus >= 400 && whatIfStatus < 500
       && whatIfStatus !== 401
       ? errText(q.error)
       : failLine(q.error, "the debt plan", "web").text)
    : null;
  const method = r.method;
  const plan = r.plan, alt = r.alt, base = r.minimums;
  const live = r.debts.filter((d) => !d.skip);
  // the server's figures: what the plan starts from, and which debts it
  // walks at a typical rate or an estimated minimum
  const owed = plan.starting;
  const guessed = live.filter((d) => d.estimated).length;
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
  const setRow = (id: string, patch: Partial<Draft[string]>) => {
    if (draft === null) setOpened(d);
    setDraft({ ...d, [id]: { ...d[id], ...patch } });
  };
  // a typed "1,000" compares equal to the stored 1000 and "12,50" reads
  // as twelve-fifty, as the server reads it; text that is not a number
  // goes out as typed so the server's refusal names it
  const extraN = extraStr.replace(/[$\s]/g, "") === "" ? 0
    : parseMoneyDraft(extraStr) ?? NaN;
  const persist = () => save.mutate({
    plan: { method,
            extra_monthly: Number.isFinite(extraN) ? extraN : extraStr },
    planBase: { method: r.saved.method, extra_monthly: r.saved.extra_monthly },
    mine: d, base: opened ?? d,
  });

  // with nothing linked "Nothing owed" reads as a true zero: say what is
  // missing instead
  if (r.debts.length === 0 && noAccounts) {
    return (
      <>
        <h1>Debt</h1>
        <div className="card" style={{ marginTop: 0 }}><NoAccountsLine /></div>
      </>
    );
  }
  if (r.debts.length === 0) {
    return (
      <>
        <h1>Debt</h1>
        <div className="card" style={{ marginTop: 0 }}>
          <div className="kpi sm pos">{r.paid_in_full.length
            ? "Nothing to pay off" : "Nothing owed"}</div>
          <div className="sub">{r.paid_in_full.length
            ? "Every card is paid in full each month, so none of it costs interest."
            : "No card or loan carries a balance."} A debt on an account the
            Accounts page excludes stays out of the plan too.</div>
        </div>
        <PaidInFull rows={r.paid_in_full} />
      </>
    );
  }

  return (
    <>
      <h1>Debt</h1>
      {/* HERO: the date the page exists to produce */}
      <div className="card" style={{ marginTop: 0 }}>
        <div className="sub">Debt-free — {NAMES[method].toLowerCase()}
          {r.extra_monthly > 0 ? `, ${money(r.extra_monthly)}/mo extra`
            : ", no extra — each paid-off debt's minimum moves to the next"}
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
        {guessed > 0 ? (
          <div className="sub" style={{ marginTop: ".3rem" }}>
            <span className="pill a">estimated</span>{" "}
            {guessed === 1 ? "1 debt is" : `${guessed} debts are`} walked at a
            typical rate or an estimated minimum
            {viewer ? "." : " — add the APR and minimum below for an exact date."}
          </div>
        ) : null}
        <div className="grid cols3" style={{ gap: ".6rem", marginTop: ".8rem" }}>
          <div className="tile">
            <div className="sub">Paying monthly</div>
            <div className="kpi sm">{money(plan.monthly)}</div>
            <div className="sub">every minimum + {money(plan.extra)} extra</div>
          </div>
          <div className="tile">
            <div className="sub">Interest to pay{r.estimated ? " (estimated)" : ""}</div>
            {finishes(plan) ? (<>
            <div className="kpi sm">{money(plan.total_interest)}</div>
            <div className={`sub ${(r.saves_vs_minimums ?? 0) > 0 ? "pos" : ""}`}>
              {r.saves_vs_minimums !== null && r.saves_vs_minimums > 0
                ? (r.extra_monthly > 0
                  ? `saves ${money(r.saves_vs_minimums)} vs minimums only`
                  : `saves ${money(r.saves_vs_minimums)} by moving each paid-off minimum on`)
                : base.debt_free
                  ? `minimums only: ${money(base.total_interest)}`
                  : "minimums only never finish"}
            </div>
            </>) : (<>
            <div className="kpi sm neg">no end</div>
            <div className="sub neg">{noEnd(plan)}</div>
            </>)}
          </div>
          <div className="tile">
            <div className="sub">{NAMES[OTHER[method]]} instead</div>
            <div className="kpi sm">{alt.debt_free ? monthYear(alt.debt_free) : "never"}</div>
            {!finishes(alt) ? (
              <div className="sub neg">{noEnd(alt)}</div>
            ) : !finishes(plan) ? (
              <div className="sub pos">{money(alt.total_interest)} interest</div>
            ) : (
            <div className={`sub ${(r.alt_extra_interest ?? 0) > 0 ? "" : "pos"}`}>
              {money(alt.total_interest)} interest
              {r.alt_extra_interest !== null && r.alt_extra_interest !== 0 && (
                r.alt_extra_interest > 0
                  ? ` — ${money(r.alt_extra_interest)} more`
                  : ` — ${money(-r.alt_extra_interest)} less`)}
            </div>)}
          </div>
        </div>

        {/* The server validates the amount; the spinner step is only a
            convenience and must not block a cent-precise what-if. */}
        <form onSubmit={recalc} className="assume" noValidate
              style={{ display: "flex", gap: ".7rem", flexWrap: "wrap", alignItems: "flex-end" }}>
          <SegmentedControl label="Payoff method" value={method} onChange={pick}
            options={(["avalanche", "snowball"] as DebtMethod[]).map((m) => (
              { value: m, label: NAMES[m] }))} />
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
        {whatIfErr && <Note tone="bad" style={{ marginTop: ".5rem" }}>
          {whatIfErr}</Note>}
        {err && <Note tone="bad" style={{ marginTop: ".5rem" }}>{err}</Note>}
      </div>

      <div className="card">
        <h2>Balance owed <span className="sub" style={{ fontWeight: 400 }}>
          — {r.alt_same_path
            ? `${NAMES[method].toLowerCase()} and ${NAMES[OTHER[method]].toLowerCase()} pay these debts down the same way`
            : `${NAMES[method].toLowerCase()} vs ${NAMES[OTHER[method]].toLowerCase()}, same money`}</span></h2>
        <AreaChart points={plan.series}
                   second={r.alt_same_path ? undefined : alt.series}
                   subject={`Balance owed, ${NAMES[method].toLowerCase()}`}
                   labels={[NAMES[method], NAMES[OTHER[method]]]} xticks="years" />
      </div>

      <div className="card stickyhead">
        <h2>In payoff order</h2>
        {/* .debt-table + c-*: at phone width each debt restacks into a
            small grid (index.css) — eight columns with two inputs used to
            scroll the APR box half out of the card */}
        <table className="debt-table">
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
                  <td className="mut c-ord">{p ? p.order : "—"}</td>
                  {/* the table scrolls sideways at a phone width; the name
                      keeps a readable column instead of one word a line */}
                  <td className="c-name" style={{ minWidth: "10rem" }}>{x.name}{x.mask ? <span className="mut"> ···{x.mask}</span> : null}
                    <div className="sub">{x.institution ? `${x.institution} · ` : ""}{KIND[x.kind]}
                      {x.origin === "hand_entered" ? " · entered on Net Worth" : ""}</div></td>
                  <td className="num c-bal">{money(x.balance)}</td>
                  <td className="num c-apr">
                    {viewer ? x.apr.toFixed(2) : (
                      <input type="number" step="0.01" min="0" max="100" value={row.apr}
                             placeholder={x.apr_default.toFixed(2)}
                             aria-label={`APR for ${x.name}`}
                             style={{ width: "5rem" }}
                             onChange={(e) => setRow(x.id, { apr: e.target.value })} />)}
                    <div className="sub">{x.apr_source === "you" ? "yours"
                      : x.apr_source === "issuer" ? "issuer"
                      : x.rate_needed ? (viewer ? "rate not given yet"
                                         : "rate needed — type it once")
                      : viewer ? "est. — typical rate" : "est. — add yours"}</div>
                  </td>
                  <td className="num c-min">
                    {viewer ? money(x.min_payment) : (
                      <input type="number" step="1" min="0" value={row.min_payment}
                             placeholder={x.min_default.toFixed(2)}
                             aria-label={`Minimum payment for ${x.name}`}
                             style={{ width: "5.5rem" }}
                             onChange={(e) => setRow(x.id, { min_payment: e.target.value })} />)}
                    <div className="sub">{MIN_FROM[x.min_basis]}</div>
                    {x.kind === "mortgage" && x.payment_reported != null
                      ? <div className="sub">Reported payment incl. escrow {money(x.payment_reported)}</div>
                      : null}
                  </td>
                  <td className="c-off">{x.skip ? <span className="pill m">left out</span>
                    : p?.paid_off ? <span className="pill g">{monthYear(p.paid_off)}</span>
                    : <span className="pill r">never</span>}</td>
                  {/* a debt the walk never clears has no interest total */}
                  <td className="num c-int">{p?.paid_off ? money(p.interest) : "·"}</td>
                  <td className="c-skip">{viewer ? (x.skip ? "yes" : "") : (
                    <input type="checkbox" checked={row.skip}
                           aria-label={`Leave ${x.name} out of the plan`}
                           onChange={(e) => setRow(x.id, { skip: e.target.checked })} />)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <p className="sub">
          Rates and minimums are the bank's where it reports them, yours
          where you type them, otherwise an estimate.{" "}
          <Link to="/help?topic=debt">How this works</Link>
          {!viewer && draft !== null && " · Changes apply when you save the plan."}
        </p>
      </div>
      <PaidInFull rows={r.paid_in_full} />
    </>
  );
}

// cards cleared every statement: the server leaves them out of the plan
// (specs/debt-plan.md) and says on what evidence
function PaidInFull({ rows }: { rows: DebtPlan["paid_in_full"] }) {
  if (!rows.length) return null;
  return (
    <div className="card">
      <h2>Paid in full monthly <span className="sub" style={{ fontWeight: 400 }}>
        — no interest, so not in the plan</span></h2>
      <table><tbody>
        {rows.map((c) => (
          <tr key={c.id}>
            <td>{c.name}{c.mask ? <span className="mut"> ···{c.mask}</span> : null}
              <div className="sub">{c.basis === "issuer" && c.statement_balance !== null
                ? `last statement ${money(c.statement_balance)} paid in full`
                : "your payments clear it each month"}</div></td>
            <td className="num">{money(c.balance)}</td>
          </tr>
        ))}
      </tbody></table>
    </div>
  );
}
