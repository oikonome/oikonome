// Cash Flow — the money-report page: where the money came from and where it
// went (Overview), where it went in detail (Spending), where it came from
// in detail (Income).
//
// Spending is a TAB here rather than its own top-level page: split, the two
// print the same per-year spend figure in two different tables, and cash
// flow IS income − spend, so reasoning about one number means hopping
// pages. Behind tabs, not concatenated: a dozen cards on one scroll is
// too long to navigate.
//
// The Overview opens with the flow picture (paychecks / interest / other on
// the left, fixed bills / variable spend / saved on the right) for a period
// you pick, states the conclusion in a sentence, then shows what was saved
// each month with the amount on every bar. The mobile page is the same
// layout (mobile/src/app/cashflow.tsx) — keep them in step.
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useSearchParams } from "react-router";
import { api, money, type CashCostsReport, type CashflowReport, type FlowKey,
         type FlowPeriod }
  from "../api/client";
import AreaChart from "../components/AreaChart";
import Bars from "../components/Bars";
import { FlowChart, NetBars } from "../components/CashFlowCharts";
import MerchantAvatar from "../components/MerchantAvatar";
import { RANGES, cashflowCutoffMonth, RANGE_MONTHS, RangePicker,
         type Range }
  from "../components/RangePicker";
import SpendingSection, { Delta, PREV_NAME } from "./Spending";

export type CFTab = "overview" | "spending" | "income";

const CF_TABS: { key: CFTab; label: string }[] = [
  { key: "overview", label: "Overview" },
  { key: "spending", label: "Spending" },
  { key: "income", label: "Income" },
];
// derived, never hand-listed: validating ?tab= against a hand-kept subset
// leaves a real tab unreachable the moment the two lists drift
const CF_TAB_KEYS: readonly CFTab[] = CF_TABS.map((x) => x.key);

export default function CashFlow() {
  // tab AND timeframe live in the URL, so both are linkable, survive a
  // reload — and survive switching tabs: a range picked on Spending is
  // still selected after a trip to Income and back
  const [sp, setSp] = useSearchParams();
  const tab = (CF_TAB_KEYS.includes(sp.get("tab") as CFTab)
               ? sp.get("tab") : "overview") as CFTab;
  const setTab = (v: CFTab) => {
    if (v === "overview") sp.delete("tab"); else sp.set("tab", v);
    setSp(sp, { replace: true });
  };
  const range = (RANGES.includes(sp.get("range") as Range)
                 ? sp.get("range") : "1y") as Range;
  const setRange = (v: Range) => {
    if (v === "1y") sp.delete("range"); else sp.set("range", v);
    setSp(sp, { replace: true });
  };

  return (
    <>
      <h1>Cash Flow</h1>
      {/* each tab owns its query, so opening Cash Flow never pays for the
          spending report (category × year matrix + top-25 merchants) */}
      {tab === "overview" &&
        <OverviewSection tab={tab} onTab={setTab}
                         range={range} setRange={setRange} />}
      {tab === "spending" && <>
        <TabStrip tab={tab} onTab={setTab} />
        <SpendingSection range={range} setRange={setRange} /></>}
      {tab === "income" && <>
        <TabStrip tab={tab} onTab={setTab} />
        <IncomeSection range={range} setRange={setRange} /></>}
    </>
  );
}

function TabStrip({ tab, onTab }: { tab: CFTab; onTab: (v: CFTab) => void }) {
  return (
    <div style={{ display: "flex", gap: ".3rem", flexWrap: "wrap",
                  marginBottom: ".8rem" }}>
      {CF_TABS.map((x) => (
        <button key={x.key} className={tab === x.key ? "pri" : ""}
                onClick={() => onTab(x.key)}>{x.label}</button>
      ))}
    </div>
  );
}

// '2026-03' → 'Mar'
const MON = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
             "Oct", "Nov", "Dec"];
const pretty = (s: string) => MON[Number(String(s).slice(5, 7))] || String(s);

/** Shared cashflow payload — Overview and Income both read it, and TanStack
 *  dedupes on the key, so switching between them refetches nothing. */
function useCashflow() {
  return useQuery({ queryKey: ["report", "cashflow"],
                    queryFn: api.reportCashflow });
}

function Loading() {
  return <p className="mut" style={{ marginTop: "2rem" }}>loading…</p>;
}

/** The saved-per-month series for a site-wide range (3m/6m/1y/3y/5y/all):
 *  the last N months from the continuous graph, plus the forecast tail
 *  (always kept, whatever the range — its length is the server's
 *  forecast horizon). Short ranges label bare month names; ranges past a
 *  year name the year at each January instead. */
function savedSeries(cf: CashflowReport, range: Range):
    { points: [string, number][]; estimatedFrom: number } {
  const graph = cf.cash_graph;
  const months = RANGE_MONTHS[range];
  // Cut on the household's month, which the report itself names (its
  // first forecast point) — the browser's clock is in a different month
  // for hours around every boundary, and every server figure on this page
  // is windowed from the household's date.
  const cutoff = cashflowCutoffMonth(graph, range) ?? "";
  const history = graph.points.filter((p, i) => i < graph.forecast_from
                                      && p[0] >= cutoff);
  const tail = graph.points.slice(graph.forecast_from);
  const label = (key: string, i: number) => {
    const m = pretty(key);
    if (months !== null && months <= 12) return m;
    // wide ranges: name the year at each January (and the first point)
    const jan = key.slice(5, 7) === "01" || i === 0;
    return jan ? `${m} ${key.slice(2, 4)}'` : m;
  };
  const all = [...history, ...tail];
  return { points: all.map((p, i) => [label(p[0], i), p[1]]),
           estimatedFrom: history.length };
}

function OverviewSection({ tab, onTab, range, setRange }: {
  tab: CFTab; onTab: (v: CFTab) => void;
  range: Range; setRange: (v: Range) => void;
}) {
  const q = useCashflow();
  // ONE timeframe for the whole page (flow picture + saved-by-month +
  // the other tabs), held in the URL by the parent.
  const period: FlowKey = range;
  // each timeframe of the flow picture is its own fetch (the fixed split
  // walks months server-side, so 'all' costs seconds) — TanStack caches
  // every range once seen, so a toggle back is free
  // previous window stays on screen while a wide one (All can take a few
  // seconds on a long ledger) computes — same pattern as Transactions
  const fq = useQuery({ queryKey: ["report", "cashflow-flow", period],
                        queryFn: () => api.reportCashflowFlow(period),
                        placeholderData: (prev) => prev });
  // collapsible per-year rows — none expanded by default
  const [open, setOpen] = useState<Record<string, boolean>>({});
  if (q.isPending) return <><TabStrip tab={tab} onTab={onTab} /><Loading /></>;
  if (q.isError) return <><TabStrip tab={tab} onTab={onTab} />
    <div className="note bad">Couldn't load: {String(q.error)}</div></>;
  const cf: CashflowReport = q.data;
  const flow = fq.data;
  const savings = [...cf.savings_by_year].reverse();
  const series = savedSeries(cf, range);
  const hist = series.points.slice(0, series.estimatedFrom);
  const best = hist.length ? hist.reduce((a, b) => (b[1] > a[1] ? b : a)) : null;
  const worst = hist.length ? hist.reduce((a, b) => (b[1] < a[1] ? b : a)) : null;

  return (
    <>
      <div className="card" style={{ marginTop: 0 }}>
        <div style={{ display: "flex", justifyContent: "space-between",
                      alignItems: "center", gap: ".6rem", flexWrap: "wrap" }}>
          <div style={{ display: "flex", gap: ".3rem", flexWrap: "wrap" }}>
            {CF_TABS.map((x) => (
              <button key={x.key} className={tab === x.key ? "pri" : ""}
                      onClick={() => onTab(x.key)}>{x.label}</button>
            ))}
          </div>
          <RangePicker value={range} onChange={setRange} />
        </div>
        {fq.isPending && <Loading />}
        {/* the flow endpoint is rate-limited (60/60s) and can 5xx on its
            own while `q` above succeeds — without this the card is just
            blank, no error and no way to retry. Same shape as Transactions:
            placeholderData may still hold the previous window's picture. */}
        {fq.isError && (
          <div className="note bad">
            <span style={{ flex: 1 }}>
              Couldn't load flow — {flow ? "showing the previous window"
                : "nothing loaded"}: {String(fq.error)}
            </span>
            <button onClick={() => fq.refetch()}>Retry</button>
          </div>
        )}
        {flow && (
          // while a wide window computes (All walks the whole ledger's
          // months server-side) the previous picture stays, dimmed, with
          // an honest line — otherwise the toggle looks dead for seconds
          <div style={fq.isFetching ? { opacity: 0.45 } : undefined}>
            <div style={{ marginTop: ".8rem" }}>
              <FlowChart flow={flow} />
            </div>
            <FlowStory flow={flow} />
            {fq.isFetching && (
              <p className="sub" style={{ margin: ".3rem 0 0" }}>
                computing {period === "all" ? "all history" : period}…</p>
            )}
          </div>
        )}
      </div>

      {series.points.length > 0 && (
        <div className="card">
          <div style={{ display: "flex", alignItems: "baseline", gap: ".8rem",
                        flexWrap: "wrap" }}>
            {/* follows the page's one timeframe — the picker in the flow
                card above owns it */}
            <h2 style={{ margin: 0 }}>Saved, by month</h2>
          </div>
          <div style={{ marginTop: ".5rem" }}>
            <NetBars points={series.points} estimatedFrom={series.estimatedFrom} />
          </div>
          <div className="sub" style={{ marginTop: ".3rem" }}>
            {best && <>best {best[0]}{" "}
              <b className="pos">+{money(best[1])}</b></>}
            {best && worst && " · "}
            {worst && <>worst {worst[0]}{" "}
              <b className={worst[1] < 0 ? "neg" : ""}>{money(worst[1])}</b></>}
            {series.points.length > series.estimatedFrom && (
              <> · <span style={{ border: "1px dashed var(--mut)", borderRadius: 999,
                                  padding: "0 .45rem", fontSize: 11 }}>
                hollow bars are the 60-day forecast</span></>)}
          </div>
        </div>
      )}

      <div className="card">
        <div style={{ display: "flex", justifyContent: "space-between",
                      alignItems: "baseline" }}>
          <h2>Savings by year</h2>
          <span className="sub">▸ open a year for its months</span>
        </div>
        <table>
          <thead><tr><th>Year</th><th className="num">In</th>
            <th className="num">Out</th><th className="num">Saved</th>
            <th className="num">Rate</th></tr></thead>
          <tbody>
            {savings.map(([yr, inc, sp, net, rate]) => {
              const chart = cf.monthly_by_year[yr];
              return (
                <YearRow key={yr} yr={yr} inc={inc} sp={sp} net={net} rate={rate}
                         chart={chart} open={!!open[yr]}
                         toggle={() => setOpen({ ...open, [yr]: !open[yr] })} />
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}

/** The page states its conclusion: in → out → saved, in one sentence. */
function FlowStory({ flow }: { flow: FlowPeriod }) {
  if (!flow.in.total && !flow.out.total) return null;
  const saved = flow.saved;
  return (
    <div style={{ display: "flex", gap: ".5rem", alignItems: "baseline",
                  flexWrap: "wrap", marginTop: ".5rem", fontSize: 13 }}>
      <span className="sub">{flow.label}:</span>
      <b style={{ fontSize: "1.15rem" }}>{money(flow.in.total)}</b> came in
      <span className="mut">→</span>
      <b style={{ fontSize: "1.15rem" }}>{money(flow.out.total)}</b> went out
      <span className="mut">→</span>
      <b style={{ fontSize: "1.15rem" }} className={saved >= 0 ? "pos" : "neg"}>
        {saved >= 0 ? "+" : ""}{money(saved)}</b>
      {saved >= 0 ? "stayed" : "overspent"}
      {flow.rate !== null && flow.rate >= 0 && (
        <span className="sub">· {flow.rate}% of income</span>)}
    </div>
  );
}

// the flow picture's income palette — kinds keep one color everywhere
const KIND_COLORS = { paychecks: "#4f9bd6", interest: "#7fb8e6",
                      other: "#3d7fb0" } as const;
const KIND_LABELS = { paychecks: "Paychecks", interest: "Interest & dividends",
                      other: "Other" } as const;

function IncomeSection({ range, setRange }: {
  range: Range; setRange: (v: Range) => void;
}) {
  const iq = useQuery({ queryKey: ["report", "income-window", range],
                        queryFn: () => api.reportIncomeWindow(range),
                        placeholderData: (prev) => prev });
  if (iq.isPending) return <Loading />;
  if (iq.isError) return <div className="note bad">Couldn't load: {String(iq.error)}</div>;
  const w = iq.data;
  const perMonth = w.total / Math.max(w.months, 1);
  const payShare = w.total ? Math.round((100 * w.kinds.paychecks) / w.total) : 0;
  const kinds = (Object.keys(KIND_COLORS) as (keyof typeof KIND_COLORS)[])
    .map((k) => ({ k, v: w.kinds[k] })).filter((x) => x.v > 0);
  const maxSrc = w.sources[0]?.[1] || 1;

  return (
    <>
      <div className="card" style={{ marginTop: 0 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: ".8rem",
                      flexWrap: "wrap" }}>
          <h2 style={{ margin: 0 }}>Income</h2>
          <span style={{ marginLeft: "auto" }}>
            <RangePicker value={range} onChange={setRange} />
          </span>
        </div>
        <div style={{ display: "flex", gap: ".5rem", alignItems: "baseline",
                      flexWrap: "wrap", marginTop: ".5rem", fontSize: 13 }}>
          <span className="sub">{w.label}:</span>
          <b style={{ fontSize: "1.15rem" }}>{money(w.total)}</b> came in
          <span className="mut">·</span>
          <b>{money(perMonth)}</b>/mo
          <span className="mut">·</span>
          <b>{payShare}%</b> <span className="sub">paychecks</span>
          {w.prev_total !== null && (
            <><span className="mut">·</span>
              <Delta cur={w.total} prev={w.prev_total} downIsGood={false} />
              <span className="sub">vs the {PREV_NAME[range]}</span></>
          )}
        </div>
        {w.total > 0 && (
          <>
            <div style={{ display: "flex", height: 12, borderRadius: 4,
                          overflow: "hidden", marginTop: ".6rem", gap: 2 }}>
              {kinds.map(({ k, v }) => (
                <div key={k} style={{ width: `${(100 * v) / w.total}%`,
                                      background: KIND_COLORS[k] }} />
              ))}
            </div>
            <div className="sub" style={{ marginTop: ".3rem" }}>
              {kinds.map(({ k, v }, i) => (
                <span key={k}>{i > 0 && " · "}
                  <span style={{ display: "inline-block", width: 8, height: 8,
                                 borderRadius: 2, background: KIND_COLORS[k],
                                 marginRight: 4 }} />
                  {KIND_LABELS[k]} {money(v)}
                  {w.prev_kinds && <> <Delta cur={v} prev={w.prev_kinds[k]}
                                            downIsGood={false} /></>}
                </span>
              ))}
            </div>
          </>
        )}
        {w.by_month.length >= 4 && (
          // the same pace line the Spending tab opens with
          <div style={{ marginTop: ".6rem" }}>
            <AreaChart points={w.by_month} height={160}
                       xticks={w.months > 14 ? "years" : undefined} />
          </div>
        )}
      </div>

      {w.sources.length > 0 && (
        <div className="card">
          <h2>Income by source <span className="sub" style={{ fontWeight: 400 }}>
            — who paid, over the window</span></h2>
          <table>
            <tbody>
              {w.sources.map(([payee, amt, n, p, logo]) => (
                <tr key={payee}>
                  <td style={{ maxWidth: "14rem" }}>
                    <MerchantAvatar name={payee} logo={logo}
                                    style={{ marginRight: 7 }} />{payee}</td>
                  <td style={{ width: "30%" }}>
                    <span style={{ display: "inline-block", height: 9,
                                   borderRadius: 3, background: "#4f9bd6",
                                   verticalAlign: "middle",
                                   width: `${Math.max(2, (100 * amt) / maxSrc)}%` }} />
                  </td>
                  <td className="num">{money(amt)}</td>
                  <td className="num mut" style={{ fontSize: 12 }}>
                    {w.total ? `${Math.round((100 * amt) / w.total)}%` : ""}</td>
                  <td className="num mut" style={{ fontSize: 12 }}>{n}×</td>
                  <td className="num" style={{ fontSize: 12 }}>
                    <Delta cur={amt} prev={p} downIsGood={false} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <CashCosts />
      <IncomeRecords />
    </>
  );
}

const MON3 = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug",
              "Sep", "Oct", "Nov", "Dec"];
const mmdd = (iso: string) => `${iso.slice(5, 7)}/${iso.slice(8, 10)}`;
const monYear = (iso: string) => `${MON3[Number(iso.slice(5, 7))]} ${iso.slice(0, 4)}`;

/** Interest and fees paid — what holding money and carrying a balance cost,
 *  by year, net of refunds, beside what the same banks paid in interest.
 *  Sits under income by source because interest EARNED is a row up there;
 *  this is the other side of it. Hidden when nothing was ever charged. */
function CashCosts() {
  const q = useQuery({ queryKey: ["report", "cash-costs"],
                       queryFn: api.reportCashCosts });
  const r: CashCostsReport | undefined = q.data;
  if (!r || r.by_year.length === 0) return null;
  const ytd = r.ytd;
  const year = r.by_year[0].year;
  const net = ytd ? ytd.net : 0;
  const vsEarned = r.earned_ytd - net;
  return (
    <div className="card">
      <h2>Interest &amp; fees paid <span className="sub" style={{ fontWeight: 400 }}>
        — what holding money costs you, by year</span></h2>
      <div style={{ display: "flex", gap: "1.4rem", flexWrap: "wrap",
                    alignItems: "baseline" }}>
        <div><span className="sub">{ytd ? `${ytd.year} so far` : `${year}`}</span>
          <div className={`kpi sm ${net > 0 ? "neg" : net < 0 ? "pos" : ""}`}>
            {money(net)}</div>
          <div className="sub" style={{ fontSize: 11 }}>
            {ytd ? <>{money(ytd.interest)} interest · {money(ytd.fees)} fees
                     {ytd.refunds > 0 && <> − {money(ytd.refunds)} refunded</>}</>
                 : "nothing charged this year"}</div></div>
        <div><span className="sub">vs interest earned</span>
          <div className={`kpi sm ${vsEarned > 0 ? "pos" : vsEarned < 0 ? "neg" : ""}`}>
            {vsEarned > 0 ? "+" : ""}{money(vsEarned)}</div>
          <div className="sub" style={{ fontSize: 11 }}>
            earned {money(r.earned_ytd)} · paid {money(net)} this year</div></div>
        <div><span className="sub">card interest</span>
          <div className={`kpi sm ${r.interest_ever > 0 ? "" : "pos"}`}>
            {money(r.interest_ever)}</div>
          <div className="sub" style={{ fontSize: 11 }}>
            {r.interest_ever > 0 ? "ever, across every account" : "never charged on any account"}</div></div>
      </div>
      <table style={{ marginTop: ".6rem" }}>
        <thead><tr><th>Year</th><th className="num">Interest</th>
          <th className="num">Fees</th><th className="num">Refunds</th>
          <th className="num">Net</th><th>Biggest</th></tr></thead>
        <tbody>
          {r.by_year.map((y) => (
            <tr key={y.year}>
              <td>{y.year}</td>
              <td className="num">{y.interest ? money(y.interest) : "·"}</td>
              <td className="num">{y.fees ? money(y.fees) : "·"}</td>
              <td className="num pos">{y.refunds ? `−${money(y.refunds)}` : "·"}</td>
              <td className="num"><b>{money(y.net)}</b></td>
              <td className="mut" style={{ fontSize: 12 }}>
                {y.biggest ? `${y.biggest[0]} ${money(y.biggest[1])} · ${mmdd(y.biggest[2])}` : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {r.latest && (
        <div className="sub" style={{ fontSize: 12, marginTop: ".5rem" }}>
          Latest: <b>{r.latest[4]}</b> {r.latest[3] === "interest" ? "charged" : "—"}{" "}
          {r.latest[3] === "interest" ? <>interest of <b>{money(r.latest[1], true)}</b></>
            : <>{r.latest[0]} <b>{money(r.latest[1], true)}</b></>}{" "}
          on {mmdd(r.latest[2])} ({monYear(r.latest[2])}). A new charge is also
          in the daily email the day it posts.
        </div>
      )}
    </div>
  );
}

/** The archival income cards — SSA career record, tax returns, investment
 *  funding. They change once a year, so they live behind "Records" and the
 *  full cashflow report is only fetched once this is opened. */
function IncomeRecords() {
  const [open, setOpen] = useState(false);
  const q = useQuery({ queryKey: ["report", "cashflow"],
                       queryFn: api.reportCashflow, enabled: open });
  const cf: CashflowReport | undefined = q.data;
  return (
    <details onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
             open={open} style={{ margin: ".8rem 0" }}>
      <summary className="mut" style={{ cursor: "pointer", fontSize: 13 }}>
        Records — career earnings (SSA) · reported income (tax returns) ·
        investment funding
      </summary>
      {open && q.isPending && <Loading />}
      {cf && (
        <>
          {cf.career_income.length > 0 && (
            <div className="card">
              <h2>Career earnings <span className="sub" style={{ fontWeight: 400 }}>
                — SSA record, {cf.career_income[0][0]} →</span></h2>
              <AreaChart points={cf.career_income.map(([y, v]) => [String(y), v])}
                         xticks="years" />
            </div>
          )}
          {cf.reported_income.length > 0 && (
            <div className="card">
              <h2>Reported income <span className="sub" style={{ fontWeight: 400 }}>
                — tax returns</span></h2>
              <table>
                <thead><tr><th>Year</th><th className="num">Total income</th>
                  <th className="num">Wages</th><th className="num">Investment</th>
                  <th className="num">Tax paid</th></tr></thead>
                <tbody>
                  {cf.reported_income.map(([yr, inc, wages, invest, tax, joint]) => (
                    <tr key={yr}>
                      <td>{yr}{joint ? <> <span className="sub">(joint)</span></> : null}</td>
                      <td className="num">{inc ? money(inc) : "·"}</td>
                      <td className="num">{wages ? money(wages) : "·"}</td>
                      <td className="num">{invest ? money(invest) : "·"}</td>
                      <td className="num">{tax ? money(tax) : "·"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="card">
            <h2>Investment funding by year</h2>
            <Bars rows={[...cf.investment_funding].reverse()} />
          </div>
        </>
      )}
    </details>
  );
}

function YearRow({ yr, inc, sp, net, rate, chart, open, toggle }: {
  yr: string; inc: number | null; sp: number; net: number | null;
  rate: number | null; chart?: [string, number][]; open: boolean;
  toggle: () => void;
}) {
  return (
    <>
      <tr onClick={chart ? toggle : undefined}
          style={chart ? { cursor: "pointer" } : undefined}>
        <td>{chart && (
          <span className="mut" style={{ display: "inline-block", width: "1em" }}>
            {open ? "▾" : "▸"}</span>)}{yr}</td>
        <td className="num">{inc !== null ? money(inc)
          : <span className="mut" style={{ fontSize: 12 }}>no data</span>}</td>
        <td className="num">{money(sp)}</td>
        {net !== null
          ? <td className={`num ${net >= 0 ? "pos" : "neg"}`}>{money(net)}</td>
          : <td className="num mut" style={{ fontSize: 12 }}>no data</td>}
        <td className={`num ${rate !== null && rate < 0 ? "neg" : ""}`}>
          {rate !== null ? `${rate}%` : "·"}</td>
      </tr>
      {chart && open && (
        <tr>
          <td colSpan={5} style={{ padding: ".2rem .5rem 1rem" }}>
            <div className="sub" style={{ margin: ".2rem 0 .4rem" }}>
              Saved, by month — {yr}</div>
            <NetBars points={chart} height={170} />
          </td>
        </tr>
      )}
    </>
  );
}
