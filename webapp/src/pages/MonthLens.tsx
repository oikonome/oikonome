// Month lens: the report card — FINAL verdict for past months,
// in-progress + pace projection for the current one. Bucket bars, bills
// paid vs planned, by-category totals, biggest transactions, income vs
// spend. Data: /api/lens/month (web/lenses.py) — the future monthly
// email's compose.
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import MoneyMap, { Track } from "../components/MoneyMap";
import { planRows } from "../components/PlanBars";
import { api, mmdd, money, type MonthLensData, catLabel } from "../api/client";
import { VERDICT } from "./Today";

export default function MonthLens({ y, m }: { y: number; m: number }) {
  const q = useQuery({ queryKey: ["lens-month", y, m],
                       queryFn: () => api.lensMonth(y, m) });
  // the money map's Savings row — the lens payload carries no savings
  // figure, so the standing goal comes from the Today payload (the Budget
  // page's existing pattern; shares Today's cache key)
  const today = useQuery({ queryKey: ["today", "now"],
                           queryFn: () => api.todayFull() });
  if (q.isPending) return <p className="mut" style={{ marginTop: "2rem" }}>loading…</p>;
  if (q.isError) return <div className="note bad">Couldn't load: {String(q.error)}</div>;
  const d: MonthLensData = q.data;
  const goal = (today.data?.savings_goals ?? [])
    .find((g) => g.name === "Savings");
  const v = VERDICT[d.verdict as keyof typeof VERDICT] ?? VERDICT["ON BUDGET"];
  const remaining = d.variable_budget - d.variable_actual;
  const p = d.projection;
  const net = d.income.actual !== null ? d.income.actual - d.spend_total : null;

  return (
    <>
      {/* ---- hero card, mirroring Today: verdict,
           the big remaining number, the pace bar; income vs spend
           demoted to one flow line ---- */}
      <div className="card" style={{ marginTop: "1rem" }}>
        <div style={{ display: "flex", alignItems: "center", gap: ".5rem" }}>
          <span className={`pill ${v.cls}`}>{v.label}</span>
          <span className="pill m">
            {d.status === "final" ? "final"
              : d.status === "future" ? "planned" : `day ${new Date(d.as_of + "T00:00").getDate()} of ${d.days_in_month}`}
          </span>
        </div>
        <div className="hero-remaining" style={{ marginTop: ".45rem" }}>
          <b className={remaining < 0 ? "neg" : ""}>{money(Math.abs(remaining))}</b>
          <span className="sub" style={{ fontSize: "1rem", fontWeight: 400 }}>
            {" "}{remaining < 0 ? "over" : d.status === "final" ? "left unspent of" : "left of"}{" "}
            {money(d.variable_budget)} to spend
          </span>
        </div>
        <Track paceLabel row={{ label: "", judged: true, actual: d.variable_actual,
                                expected: d.variable_actual - d.variance,
                                budget: d.variable_budget }} />
        <div className="sub" style={{ marginTop: "1.4rem" }}>
          {d.variance > d.tolerance
            ? <b className="neg">{money(d.variance)} over budget</b>
            : d.variance < -d.tolerance
              ? <b className="pos">{money(-d.variance)} under budget</b>
              : <b style={{ color: "var(--amber)" }}>within tolerance ({money(d.tolerance)})</b>}
          {d.status !== "final" && " so far"}
        </div>
        {/* a closed month with no frozen snapshot is judged against
            TODAY's budget — say so rather than pass it off as history */}
        {d.status === "final" && d.budget_source === "live" && (
          <div className="sub" style={{ marginTop: ".5rem" }}>
            No budget snapshot exists for this month — the verdict compares
            it against the current budget, applied retroactively.
          </div>
        )}
        {p && (
          <div className="sub" style={{ marginTop: ".35rem" }}>
            At the current rate, projected <b>{money(p.pace_total)}</b> by month end —{" "}
            {p.variance > p.tolerance
              ? <b className="neg">{money(p.variance)} over</b>
              : p.variance < -p.tolerance
                ? <b className="pos">{money(-p.variance)} under</b>
                : <b style={{ color: "var(--amber)" }}>on plan</b>}.
          </div>
        )}
        {d.variable_scale && (
          <div className="sub" style={{ marginTop: ".35rem", color: "var(--amber)" }}>
            Heavy bill month: variable budgets auto-scaled from{" "}
            {money(d.variable_scale.static_total)}.</div>
        )}
        {/* income vs spend as one flow line (mirrors Today's plan line) */}
        <div style={{ marginTop: ".9rem", paddingTop: ".7rem",
                      borderTop: "1px solid var(--line)", fontSize: 13,
                      display: "flex", gap: ".55rem", flexWrap: "wrap",
                      alignItems: "baseline" }}>
          <span className="sub"><b style={{ color: "var(--ink)" }}>{money(d.income.actual)}</b> in
            {d.income.budgeted !== null ? ` (budgeted ${money(d.income.budgeted)})` : ""}</span>
          <span style={{ color: "var(--line)" }}>→</span>
          <span className="sub"><b style={{ color: "var(--ink)" }}>{money(d.spend_total)}</b> spent</span>
          <span style={{ color: "var(--line)" }}>→</span>
          <span className="sub">net <b className={net !== null ? (net >= 0 ? "pos" : "neg") : ""}>
            {money(net)}</b></span>
        </div>
      </div>

      {/* ---- month money map — its headline lives in the hero card ---- */}
      <div className="card">
        <h2>Monthly budget</h2>
        <MoneyMap
          rows={planRows(d.buckets, { period: { y: d.y, m: d.m },
            fixedActual: d.bills.posted,
            fixedBudget: d.bills.planned,
            fixedPending: d.bills.awaiting,
            // saved prefers the month's REAL posted contribution when
            // the payload carries it; rate_90d only as fallback (it's a
            // trailing average, not this month's actual)
            savings: goal ? { plan: goal.monthly_plan,
                              saved: d.saved_month ?? goal.rate_90d } : null,
            // the plan's residual, monthly — same row as Today
            excess: d.plan_surplus,
          })} />
      </div>

      {/* ---- bills paid vs planned ---- */}
      <div className="grid2">
        <div className="card">
          <h2>Bills — paid vs planned</h2>
          <div style={{ display: "flex", gap: "1.6rem", flexWrap: "wrap" }}>
            <div><div className="kpi sm">{money(d.bills.posted)}</div>
              <div className="sub">posted ({d.bills.paid.length})</div></div>
            <div><div className="kpi sm">{money(d.bills.planned)}</div>
              <div className="sub">planned</div></div>
            {d.bills.awaiting > 0 && (
              <div><div className="kpi sm" style={{ color: "var(--amber)" }}>
                  {money(d.bills.awaiting)}</div>
                <div className="sub">due, not posted</div></div>
            )}
          </div>
          {d.bills.overdue.length > 0 && (
            <div className="sub" style={{ marginTop: ".4rem" }}>
              Scheduled but not posted:{" "}
              {d.bills.overdue.slice(0, 5).map(([pp, a, due], i) => (
                <span key={i}>{pp} {money(a)} (due {mmdd(due)})
                  {i < Math.min(d.bills.overdue.length, 5) - 1 ? ", " : ""}</span>
              ))}
            </div>
          )}
          {d.bills.paid.length > 0 && (
            <table style={{ marginTop: ".6rem" }}><tbody>
              {d.bills.paid.map((r, i) => (
                <tr key={i}>
                  <td className="mut" style={{ whiteSpace: "nowrap", width: "1%" }}>{mmdd(r.date)}</td>
                  <td>{r.payee} <span className="pill g" style={{ marginLeft: 4 }}>paid ✓</span></td>
                  <td className="num" style={{ whiteSpace: "nowrap" }}>{money(r.amount)}</td>
                </tr>
              ))}
            </tbody></table>
          )}
          {d.bills.envelopes.length > 0 && (
            <div className="sub" style={{ marginTop: ".4rem" }}>
              Envelopes: {d.bills.envelopes.map((e, i) => (
                <span key={i}>{e.payee} {money(e.used)} of {money(e.monthly)}
                  {e.overflow > 0 ? ` (+${money(e.overflow)} overflow)` : ""}
                  {i < d.bills.envelopes.length - 1 ? " · " : ""}</span>
              ))}
            </div>
          )}
          {d.bills.paid.length === 0 && d.bills.overdue.length === 0 &&
            d.bills.envelopes.length === 0 && (
            <p className="sub">No tracked bill activity this month.</p>
          )}
        </div>

        {/* ---- by category ---- */}
        <div className="card">
          <h2>{d.status === "final" ? "Spending by category" : "Month to date by category"}</h2>
          {d.by_category.length === 0 && <p className="sub">No spending.</p>}
          {d.by_category.length > 0 && (
            <table><tbody>
              {d.by_category.map(([cat, amt, key]) => (
                <tr key={cat}>
                  {/* the label opens the month's rows in that category. The
                      filter matches the STORED key, which the row carries
                      beside its display label — sending the label itself
                      ("FOOD AND DRINK") matches nothing. A row with no key
                      is the clustered Amazon parent: an itemised bucket
                      rather than a category, so it has no filter to open
                      and stays plain text. */}
                  <td>{cat === "Amazon"
                    ? <><b>Amazon</b> <span className="sub">({d.amazon_subs.length})</span></>
                    : key
                      ? <Link className="catlink"
                              to={`/transactions?cat=${encodeURIComponent(key)}&y=${d.y}&m=${d.m}`}>
                          {catLabel(cat)}</Link>
                      : catLabel(cat)}</td>
                  <td className="num">{money(amt)}</td>
                </tr>
              ))}
            </tbody></table>
          )}
        </div>
      </div>

      {/* ---- biggest transactions ---- */}
      <div className="card">
        <h2>Biggest transactions</h2>
        {d.biggest.length === 0 && <p className="sub">No spending posted.</p>}
        {d.biggest.length > 0 && (
          <table><tbody>
            {d.biggest.map((r, i) => (
              <tr key={i}>
                <td className="mut" style={{ whiteSpace: "nowrap", width: "1%" }}>{mmdd(r.date)}</td>
                <td className="num" style={{ whiteSpace: "nowrap", width: "1%" }}>{money(-r.amount)}</td>
                <td>{r.payee}</td>
                <td className="sub hide-m">{catLabel(r.category)}</td>
                <td className="mut hide-m" style={{ whiteSpace: "nowrap", textAlign: "right" }}>{r.account}</td>
              </tr>
            ))}
          </tbody></table>
        )}
      </div>
    </>
  );
}
