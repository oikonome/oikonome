// Year lens: 12-cell verdict grid (final verdicts; current month
// projected; future blank), annual totals, category totals vs prior year.
// Data: /api/lens/year (web/lenses.py).
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { api, money, type YearLensData, catLabel } from "../api/client";
import MoneyMap from "../components/MoneyMap";
import { planRows } from "../components/PlanBars";
import { VERDICT } from "./Today";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export default function YearLens({ y, onMonth }: {
  y: number; onMonth: (m: number) => void;
}) {
  const q = useQuery({ queryKey: ["lens-year", y],
                       queryFn: () => api.lensYear(y) });
  // the money map's Savings row — the lens payload carries no savings
  // figure, so the standing goal comes from the Today payload (the Budget
  // page's existing pattern; shares Today's cache key)
  const today = useQuery({ queryKey: ["today", "now"],
                           queryFn: () => api.todayFull() });
  if (q.isPending) return <p className="mut" style={{ marginTop: "2rem" }}>loading…</p>;
  if (q.isError) return <div className="note bad">Couldn't load: {String(q.error)}</div>;
  const d: YearLensData = q.data;
  const goal = (today.data?.savings_goals ?? [])
    .find((g) => g.name === "Savings");
  const t = d.totals, pt = d.prev_totals;

  return (
    <>
      {/* ---- 12-cell verdict grid. "net" cells are months with no
           budget snapshot: they show the month's net cash flow (income −
           spending) — a fact, not a verdict — styled muted so the two
           can't be mistaken for each other ---- */}
      <div className="ygrid">
        {d.cells.map((c) => {
          const v = c.verdict
            ? VERDICT[c.verdict as keyof typeof VERDICT] ?? VERDICT["ON BUDGET"]
            : null;
          const isNet = c.status === "net";
          return (
            <button key={c.m} className="ycell" disabled={c.status === "future"}
                    onClick={() => onMonth(c.m)}
                    title={c.status === "future" ? ""
                      : isNet
                        ? `${MONTHS[c.m - 1]} — no budget set; `
                          + (c.income != null
                             ? `income ${money(c.income)} − spent ${money(c.spend)}`
                             : `spent ${money(c.spend ?? 0)} (income unknown)`)
                        : `${MONTHS[c.m - 1]} — spent ${money(c.variable_actual)} of ${money(c.variable_budget)}`}>
              <div style={{ display: "flex", justifyContent: "space-between",
                            alignItems: "baseline" }}>
                <b>{MONTHS[c.m - 1]}</b>
                {c.status === "projected" && <span className="sub" style={{ fontSize: 10 }}>proj.</span>}
              </div>
              {v ? (
                <>
                  <span className={`pill ${v.cls}`} style={{ marginTop: 4 }}>{v.label}</span>
                  <div className={`sub ${c.variance! > 0 ? "neg" : "pos"}`}
                       style={{ marginTop: 3, fontVariantNumeric: "tabular-nums" }}>
                    {c.variance! > 0 ? "+" : "−"}{money(Math.abs(c.variance!))}
                  </div>
                </>
              ) : isNet ? (
                <>
                  <span className="pill m" style={{ marginTop: 4 }}>net</span>
                  <div className={`sub ${c.net != null ? (c.net >= 0 ? "pos" : "neg") : "mut"}`}
                       style={{ marginTop: 3, fontVariantNumeric: "tabular-nums" }}>
                    {c.net != null
                      ? `${c.net >= 0 ? "+" : "−"}${money(Math.abs(c.net))}`
                      : money(c.spend ?? 0)}
                  </div>
                </>
              ) : (
                <div className="mut" style={{ marginTop: 8 }}>—</div>
              )}
            </button>
          );
        })}
      </div>
      {d.cells.some((c) => c.status === "net") && (
        <p className="sub" style={{ marginTop: ".5rem" }}>
          Months without a budget snapshot show <b>net</b> (income −
          spending) — a cash-flow fact, not a budget verdict. Budgets are
          snapshotted as each month closes, so verdict months are judged
          against the budget that month actually ran under.
        </p>
      )}

      {/* ---- year money map: elapsed-months plan aggregates
           on one dollar scale ---- */}
      {d.buckets_annual && (() => {
        // excess covers the same elapsed BUDGETED months buckets_annual
        // sums — "net" months had no plan, so they don't scale the map
        const elapsed = d.cells.filter(
          (c) => c.status === "final" || c.status === "projected").length;
        if (elapsed === 0) return null;   // nothing was ever budgeted
        return (
          <div className="card">
            <h2>Yearly budget</h2>
            <MoneyMap
              rows={planRows(
                { food: d.buckets_annual.food, other: d.buckets_annual.other },
                { // a label opens the year's bucket: the same months this
                  // map summed, as one listing
                  period: { y: d.y },
                  fixedActual: d.buckets_annual.fixed.actual,
                  fixedBudget: d.buckets_annual.fixed.month_budget,
                  // Savings — standing monthly plan × the same
                  // elapsed months every other row here covers.
                  // actual prefers the lens's ledger-verified YTD
                  // (saved_ytd, matched transfers); rate_90d × elapsed is
                  // the fallback against servers that don't send it yet.
                  savings: goal && elapsed > 0
                    ? { plan: goal.monthly_plan * elapsed,
                        saved: d.saved_ytd
                          ?? (goal.rate_90d === null ? null
                              : goal.rate_90d * elapsed) } : null,
                  excess: d.plan_surplus !== null && elapsed > 0
                    ? d.plan_surplus * elapsed : null })} />
          </div>
        );
      })()}

      {/* ---- annual totals ---- */}
      <div className="card">
        <h2>{d.y} totals</h2>
        <div style={{ display: "flex", gap: "1.8rem", flexWrap: "wrap" }}>
          <div><div className="kpi sm">{money(t.income)}</div><div className="sub">income</div></div>
          <div><div className="kpi sm">{money(t.spend)}</div><div className="sub">spend</div></div>
          <div><div className={`kpi sm ${t.saved !== null ? (t.saved >= 0 ? "pos" : "neg") : ""}`}>
              {money(t.saved)}</div><div className="sub">saved</div></div>
          <div><div className="kpi sm">{t.rate !== null ? `${t.rate}%` : "·"}</div>
            <div className="sub">savings rate</div></div>
        </div>
        {t.income === null && (
          <div className="sub" style={{ marginTop: ".4rem" }}>
            No bank income data for {d.y} — income and savings are unknown, not $0.
          </div>
        )}
      </div>

      {/* ---- category totals vs prior year ---- */}
      <div className="card">
        <h2>Spending by category — {d.y} vs {d.y - 1}</h2>
        {d.categories.length === 0 && <p className="sub">No spending.</p>}
        {d.categories.length > 0 && (
          <table>
            <thead><tr><th>Category</th><th className="num">{d.y}</th>
              <th className="num">{d.y - 1}</th><th className="num hide-m">Δ</th></tr></thead>
            <tbody>
              {d.categories.map(([cat, cur, prev, key]) => (
                <tr key={cat}>
                  {/* the label opens the year's rows in that category. The
                      filter matches the STORED key, which the row carries
                      last — the visible label is the display form
                      ("FOOD AND DRINK") and matches nothing. A row without
                      a key has no addressable filter and stays plain. */}
                  <td>{key
                    ? <Link className="catlink"
                            to={`/transactions?cat=${encodeURIComponent(key)}&date_from=${d.y}-01-01&date_to=${d.y}-12-31&counted=1`}>
                        {catLabel(cat)}</Link>
                    : catLabel(cat)}</td>
                  <td className="num">{money(cur)}</td>
                  <td className="num mut">{money(prev)}</td>
                  <td className={`num hide-m ${cur - prev > 0 ? "neg" : "pos"}`}>
                    {cur - prev >= 0 ? "+" : "−"}{money(Math.abs(cur - prev))}</td>
                </tr>
              ))}
              {pt.spend > 0 && (
                <tr>
                  <td><b>Total spend</b></td>
                  <td className="num"><b>{money(t.spend)}</b></td>
                  <td className="num mut">{money(pt.spend)}</td>
                  <td className={`num hide-m ${t.spend - pt.spend > 0 ? "neg" : "pos"}`}>
                    {t.spend - pt.spend >= 0 ? "+" : "−"}{money(Math.abs(t.spend - pt.spend))}</td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
