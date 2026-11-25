// Year lens: 12-cell verdict grid (final verdicts; current month
// projected; future blank), annual totals, category totals vs prior year.
// Data: /api/lens/year (web/lenses.py).
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { api, money, type Change, type YearLensData, catLabel }
  from "../api/client";
import MoneyMap from "../components/MoneyMap";
import { sentPlanRows, sentPlanTotal } from "../components/PlanBars";
import { VERDICT } from "./Today";
import { changeLook, netSign, signedMoney, signedPct } from "../moneymath";
import PageSkeleton from "../components/PageSkeleton";
import { FailedLine, StaleLine } from "../components/Loaded";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// The prior year's column label: "2025" when both years are whole, and
// "2025 (to Jun 30)" while the lens year is in progress — the server sets
// it against that same span of the year before
function priorYearLabel(d: YearLensData): string {
  const [, m, day] = (d.prev_through ?? "").split("-").map(Number);
  return m ? `${d.y - 1} (to ${MONTHS[m - 1]} ${day})` : `${d.y - 1}`;
}

export default function YearLens({ y, onMonth }: {
  y: number; onMonth: (m: number) => void;
}) {
  const q = useQuery({ queryKey: ["lens-year", y],
                       queryFn: () => api.lensYear(y),
                       // a step to another period keeps this one on screen,
                       // dimmed, until it answers (the phone's lens rule)
                       placeholderData: (prev) => prev });
  if (q.isPending) return <PageSkeleton />;
  if (q.data === undefined) return <FailedLine q={q} what="the year" />;
  const d: YearLensData = q.data;
  const t = d.totals, pt = d.prev_totals;
  const prevLabel = priorYearLabel(d);
  // the left over's sign as printed: $0 is even, untoned, with no rate
  const leftSign = t.dollars.saved !== null ? netSign(t.dollars.saved) : null;

  return (
    <div style={q.isPlaceholderData ? { opacity: 0.5, pointerEvents: "none" } : undefined}>
      {/* a failed refetch says the year below is from before it (the
          phone's line) */}
      <StaleLine query={q} />
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
          // the server's words for a cell (lenses.cell_words): the pill
          // of a cell that is not a verdict, the figure under it, and the
          // note on a month whose income the bank feed does not measure
          const toneCls = c.tone === "pos" ? "pos" : c.tone === "neg" ? "neg" : "mut";
          return (
            <button key={c.m} className="ycell" disabled={c.status === "future"}
                    onClick={() => onMonth(c.m)}
                    style={c.note ? { opacity: 0.6 } : undefined}
                    title={c.status === "future" ? ""
                      : isNet
                        ? `${MONTHS[c.m - 1]} — ${c.label ?? "net"}`
                          + (c.figure ? ` ${c.figure}` : "")
                          + (c.income != null
                             ? `; income ${money(c.income)} − spent ${money(c.spend)}` : "")
                          + (c.note ? ` (${c.note})` : "")
                        : `${MONTHS[c.m - 1]} — spent ${money(c.variable_actual)} of ${money(c.variable_budget)}`
                          + (c.note ? ` (${c.note})` : "")}>
              <div style={{ display: "flex", justifyContent: "space-between",
                            alignItems: "baseline" }}>
                <b>{MONTHS[c.m - 1]}</b>
                {c.status === "projected" && <span className="sub" style={{ fontSize: 10 }}>proj.</span>}
              </div>
              {v ? (
                <>
                  <span className={`pill ${v.cls}`} style={{ marginTop: 4 }}>{v.label}</span>
                  {/* the margin in the server's words ("over by $300"):
                      on this page only a net carries a sign */}
                  {c.figure && (
                    <div className={`sub ${toneCls}`}
                         style={{ marginTop: 3, fontVariantNumeric: "tabular-nums" }}>
                      {c.figure}
                    </div>)}
                </>
              ) : isNet ? (
                <>
                  <span className="pill m" style={{ marginTop: 4 }}>{c.label ?? "net"}</span>
                  {/* the server's figure and tone; nothing of our own */}
                  <div className={`sub ${toneCls}`}
                       style={{ marginTop: 3, fontVariantNumeric: "tabular-nums" }}>
                    {c.figure ?? "—"}</div>
                  {c.note && <div className="mut" style={{ fontSize: 10 }}>{c.note}</div>}
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
      {/* the server's rows over the elapsed budgeted months
          (lenses.year_summary → plan_rows; null: nothing was budgeted) */}
      {d.plan_rows && (
        <div className="card">
          <h2>Yearly budget</h2>
          {/* the server's words for which months the card covers */}
          {d.plan_caption && <div className="sub" style={{ marginBottom: ".4rem" }}>
            {d.plan_caption}</div>}
          <MoneyMap rows={sentPlanRows(d.plan_rows)}
                    total={d.plan_total ? sentPlanTotal(d.plan_total) : null}
                    footnote={d.excess_text && (
                      // the server's sentence: what an ordinary month
                      // leaves, absent under a year over budget
                      <div className="sub" style={{ fontSize: 12, marginTop: ".5rem" }}>
                        {d.excess_text}</div>)} />
        </div>
      )}

      {/* ---- annual totals ---- */}
      <div className="card">
        <h2>{d.y} totals</h2>
        <div style={{ display: "flex", gap: "1.8rem", flexWrap: "wrap" }}>
          {/* the server's whole dollars (lenses.totals_dollars): left
              over is the printed income less the printed spend */}
          <div><div className="kpi sm">{money(t.dollars.income)}</div><div className="sub">income</div></div>
          <div><div className="kpi sm">{money(t.dollars.spend)}</div><div className="sub">spend</div></div>
          <div><div className={`kpi sm ${leftSign === 1 ? "pos" : leftSign === -1 ? "neg" : ""}`}>
              {t.dollars.saved !== null ? signedMoney(t.dollars.saved, money, true) : money(null)}</div>
            <div className="sub">left over</div></div>
          <div><div className="kpi sm">{t.rate !== null && leftSign !== 0 ? signedPct(t.rate) : "·"}</div>
            <div className="sub">savings rate</div></div>
        </div>
        {/* the server's words when a month's income is incomplete:
            income, left over and the rate then count the other months */}
        {t.incomplete_text && (
          <div className="sub" style={{ marginTop: ".4rem", fontSize: 11 }}>
            {t.incomplete_text}</div>)}
        {d.dating === "bill_month" && (
          <div className="sub" style={{ marginTop: ".4rem", fontSize: 11 }}>
            the sum of the months above; a bill counts in the month it
            belongs to — Cash Flow dates spending by the day it was paid</div>)}
        {t.unknown_text && (
          <div className="sub" style={{ marginTop: ".4rem" }}>
            {t.unknown_text}
          </div>
        )}
      </div>

      {/* ---- category totals vs prior year ---- */}
      <div className="card">
        <h2>Spending by category — {d.y} vs {prevLabel}</h2>
        {d.categories.length === 0 && <p className="sub">No spending.</p>}
        {d.categories.length > 0 && (
          <table>
            {/* the heading names the prior year's window, so its column
                says the year alone (the window wrapped it into three lines
                at phone width); the change is the table's point and shows
                at every width */}
            <thead><tr><th>Category</th><th className="num">{d.y}</th>
              <th className="num">{d.y - 1}</th><th className="num">Δ</th></tr></thead>
            <tbody>
              {d.categories.map(([cat, cur, prev, key], i) => (
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
                  <YearChange ch={d.changes.categories[i]} />
                </tr>
              ))}
              {pt.spend > 0 && (
                <tr>
                  <td><b>Total spend</b></td>
                  <td className="num"><b>{money(t.spend)}</b></td>
                  <td className="num mut">{money(pt.spend)}</td>
                  <YearChange ch={d.changes.spend} />
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

/** One "Δ" cell: the server's change from last year (reporting.change,
 *  the printed figure minus the printed figure), "unchanged" at $0. */
function YearChange({ ch }: { ch: Change | null }) {
  const c = changeLook(ch, true);
  if (!c) return <td className="num" />;
  if (c.unchanged) return <td className="num mut">unchanged</td>;
  return (
    <td className={`num ${c.good ? "pos" : "neg"}`}>
      {c.sign}{money(c.dollars)}</td>
  );
}
