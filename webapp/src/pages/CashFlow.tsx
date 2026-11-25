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
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useSearchParams } from "react-router";
import { api, errText, money, toast, type CashCostsReport, type CashflowReport,
         type FlowKey, type FlowPeriod, type SourcePaycheck }
  from "../api/client";
import { canEdit } from "../role";
import AreaChart from "../components/AreaChart";
import Bars from "../components/Bars";
import { FailedLine, StaleLine } from "../components/Loaded";
import EmptyState from "../components/EmptyState";
import { changeWords, EMPTY_LINES, monthWithYear, netSign, signedMoney,
         signedPct }
  from "../moneymath";
import { FlowChart, NetBars } from "../components/CashFlowCharts";
import MerchantAvatar from "../components/MerchantAvatar";
import { DEFAULT_RANGE, RANGES, RANGE_MONTHS, RangePicker,
         fillMonthKeyGaps, rangeCaption,
         type Range }
  from "../components/RangePicker";
import SpendingSection, { Delta } from "./Spending";
import PageSkeleton from "../components/PageSkeleton";
import SegmentedControl from "../components/SegmentedControl";
import { ShowAllButton } from "../components/ShowMore";

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
                 ? sp.get("range") : DEFAULT_RANGE) as Range;
  const setRange = (v: Range) => {
    if (v === DEFAULT_RANGE) sp.delete("range"); else sp.set("range", v);
    setSp(sp, { replace: true });
  };

  return (
    <>
      <h1>Cash Flow</h1>
      {/* The tabs sit in ONE place for all three, above the cards: they
          used to be inside the Overview card but above the Spending and
          Income cards, so switching tabs moved the control under the
          pointer. Each tab owns its query, so opening Cash Flow never pays
          for the spending report (category × year matrix + top-25
          merchants). */}
      <TabStrip tab={tab} onTab={setTab} />
      {tab === "overview" &&
        <OverviewSection range={range} setRange={setRange} />}
      {tab === "spending" &&
        <SpendingSection range={range} setRange={setRange} />}
      {tab === "income" &&
        <IncomeSection range={range} setRange={setRange} />}
    </>
  );
}

function TabStrip({ tab, onTab }: { tab: CFTab; onTab: (v: CFTab) => void }) {
  return (
    <SegmentedControl label="Cash Flow view" value={tab} onChange={onTab}
      options={CF_TABS.map((x) => ({ value: x.key, label: x.label }))}
      style={{ marginBottom: ".8rem" }} />
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
  return <PageSkeleton />;
}

/** The left-over-per-month series for a site-wide range: the slots of the
 *  continuous graph the server says that range draws
 *  (`cash_graph.windows`, reporting._drawn_windows). A range of complete
 *  months ends with its last one; the month in progress and the forecast
 *  follow only a range that includes this month. Short ranges label bare
 *  month names; ranges past a year name each month with its year. */
function savedSeries(cf: CashflowReport, range: Range):
    { points: [string, number | null][]; keys: string[];
      estimatedFrom: number; note: string } {
  const graph = cf.cash_graph;
  const months = RANGE_MONTHS[range];
  const w = graph.windows[range];
  const drawn = (key: string) => key >= w.first && key <= w.last;
  // a month no bank feed covered keeps its place as an empty slot
  // the month in progress (saved so far) is a solid bar after them
  const history = fillMonthKeyGaps([...graph.points.filter(
    (p, i) => i < graph.forecast_from && drawn(p[0])),
    ...(graph.current && drawn(graph.current[0]) ? [graph.current] : [])]);
  // the month in progress is drawn once: its saved-so-far bar, not a
  // forecast bar beside it as well
  const tail = graph.points.slice(graph.forecast_from)
    .filter((p) => drawn(p[0])
      && (!graph.current || p[0] !== graph.current[0]));
  // Up to a year a bare month is enough. Past a year every label names
  // its year ("Mar '24"): the chart writes only every nth label there, and
  // one that named the year only at each January was left, on most
  // windows, with a row of months and no year anywhere.
  const label = (key: string) =>
    months !== null && months <= 12 ? pretty(key) : monthWithYear(key);
  const all = [...history, ...tail];
  return { points: all.map((p) => [label(p[0]), p[1]]),
           keys: all.map((p) => p[0]), estimatedFrom: history.length,
           note: w.note };
}

function OverviewSection({ range, setRange }: {
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
  // Left over by year opens on the latest few years
  const [allYears, setAllYears] = useState(false);
  if (q.isPending) return <Loading />;
  // a failed read keeps the range picker, so another window is one click
  // away
  if (q.data === undefined)
    return (
      <div className="card" style={{ marginTop: 0 }}>
        <div style={{ display: "flex", justifyContent: "flex-end" }}>
          <RangePicker value={range} onChange={setRange} />
        </div>
        <FailedLine q={q} what="cash flow" />
      </div>);
  const cf: CashflowReport = q.data;
  const flow = fq.data;
  const savings = [...cf.savings_by_year].reverse();
  const series = savedSeries(cf, range);
  // The window's best and worst month are the server's (reporting.
  // _saved_extremes): finished months of this timeframe only, never the
  // month in progress, and neither when the window is a single month.
  // Each is named with its year ("Nov '25").
  const { best, worst } = cf.cash_graph.extremes[range];

  return (
    <>
      <div className="card" style={{ marginTop: 0 }}>
        <div style={{ display: "flex", justifyContent: "flex-end" }}>
          <RangePicker value={range} onChange={setRange} />
        </div>
        {fq.isPending && <Loading />}
        {/* the flow endpoint is rate-limited (60/60s) and can 5xx on its
            own while `q` above succeeds — without this the card is just
            blank, no error and no way to retry. Same shape as Transactions:
            placeholderData may still hold the previous window's picture. */}
        <StaleLine query={fq} />
        {fq.data === undefined && fq.isError && (
          <FailedLine q={fq} what={`the flow (${rangeCaption(period)})`} />)}
        {flow && (
          // while a wide window computes (All walks the whole ledger's
          // months server-side) the previous picture stays, dimmed, with
          // an honest line — otherwise the toggle looks dead for seconds
          <div style={fq.isFetching ? { opacity: 0.45 } : undefined}>
            <div style={{ marginTop: ".8rem" }}>
              <FlowChart flow={flow} />
            </div>
            <FlowStory flow={flow} />
            {/* what moved to investments is its own figure, never part
                of what was left over (the server's line) */}
            {flow.invested_text && (
              <p className="sub" style={{ margin: ".2rem 0 0" }}>
                {flow.invested_text}</p>)}
            <IncompleteNote months={flow.incomplete_months} />
            {flow.out.unsplit_text && (
              <p className="sub" style={{ margin: ".3rem 0 0", fontSize: 12 }}>
                {flow.out.unsplit_text}</p>)}
            {/* what Fixed means on a window before most of today's
                bills began (the server's sentence) */}
            {flow.out.fixed_basis_text && (
              <p className="sub" style={{ margin: ".3rem 0 0", fontSize: 12 }}>
                {flow.out.fixed_basis_text}</p>)}
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
            <h2 style={{ margin: 0 }}>Left over, by month</h2>
          </div>
          <div style={{ marginTop: ".5rem" }}>
            <NetBars points={series.points} estimatedFrom={series.estimatedFrom}
                     missing={cf.incomplete_label} />
          </div>
          <div className="sub" style={{ marginTop: ".3rem" }}>
            {best && <>best {monthWithYear(best[0])}{" "}
              <b className={changeWords(best[1], money).tone}>
                {changeWords(best[1], money).text}</b></>}
            {best && worst && " · "}
            {/* signed the way the best month is: one minus sign per line */}
            {worst && <>worst{" "}{monthWithYear(worst[0])}{" "}
              <b className={changeWords(worst[1], money).tone}>
                {changeWords(worst[1], money).text}</b></>}
            {series.points.length > series.estimatedFrom && (
              <>{" "}<span style={{ border: "1px dashed var(--mut)", borderRadius: 999,
                                  padding: "0 .45rem", fontSize: 11,
                                  marginLeft: ".3rem",
                                  display: "inline-block", whiteSpace: "nowrap" }}>
                {forecastCaption(cf.cash_graph.forecast_days)}</span></>)}
          </div>
          {/* the server's line: it names this month only for a range
              whose bars reach it */}
          <div className="sub" style={{ marginTop: ".2rem", fontSize: 11 }}>
            {series.note}
          </div>
        </div>
      )}

      <div className="card">
        <div style={{ display: "flex", justifyContent: "space-between",
                      alignItems: "baseline" }}>
          <h2>Left over by year</h2>
          <span className="sub">▸ open a year for its months</span>
        </div>
        <table>
          <thead><tr><th>Year</th><th className="num">In</th>
            <th className="num">Out</th><th className="num">Left over</th>
            <th className="num">Rate</th></tr></thead>
          <tbody>
            {(allYears ? savings : savings.slice(0, SAVINGS_YEARS_SHORT))
              .map(([yr, inc, , net, rate]) => {
              const chart = cf.monthly_by_year[yr];
              return (
                // the server's whole dollars (reporting.year_dollars, the
                // Year lens's rule): left over is printed in less out
                <YearRow key={yr} yr={yr}
                         inc={inc !== null ? cf.savings_by_year_dollars[yr].income : null}
                         sp={cf.savings_by_year_dollars[yr].spend}
                         net={net !== null ? cf.savings_by_year_dollars[yr].saved : null}
                         rate={rate}
                         note={cf.savings_by_year_notes[yr]}
                         chart={chart} slots={cf.months_by_year[yr]}
                         missing={cf.incomplete_label} open={!!open[yr]}
                         toggle={() => setOpen({ ...open, [yr]: !open[yr] })} />
              );
            })}
          </tbody>
        </table>
        <ShowAllButton total={savings.length} limit={SAVINGS_YEARS_SHORT}
                       all={allYears} setAll={setAllYears} noun="years" />
      </div>
    </>
  );
}

// years "Left over by year" shows before "Show all"
const SAVINGS_YEARS_SHORT = 5;

/** "over 7 of 9 months" — saved and the rate cover only the months with
 *  bank data; said only when that is not every month. */
export function savedOver(known?: number, total?: number): string {
  return known !== undefined && total !== undefined && known < total
    ? `over ${known} of ${total} months` : "";
}

/** The hollow bars' caption, naming the server's forecast horizon. */
export function forecastCaption(days?: number): string {
  return days ? `hollow bars are the ${days}-day forecast`
              : "hollow bars are the forecast";
}

/** The months of a window the bank feed misses: still counted, so the
 *  page says their income is incomplete. The same list on every tab. */
export function IncompleteNote({ months }: { months?: string[] }) {
  if (!months || months.length === 0) return null;
  const name = (k: string) => `${MON[Number(k.slice(5, 7))]} ${k.slice(0, 4)}`;
  const shown = months.length <= 3 ? months.map(name).join(", ")
    : `${name(months[0])} – ${name(months[months.length - 1])}`;
  return (
    <p className="sub" style={{ margin: ".3rem 0 0", fontSize: 12 }}>
      <span className="pill a" style={{ marginRight: ".35rem" }}>incomplete</span>
      {months.length === 1 ? "1 month" : `${months.length} months`} without a
      bank feed ({shown}) — counted, but income there is missing or partial
    </p>
  );
}

/** The page states its conclusion: in → out → left over, in one sentence. */
function FlowStory({ flow }: { flow: FlowPeriod }) {
  // income unknown (no bank feed covers the window): say so, and claim
  // no saved figure or rate — a null read as $0 would invent both
  if (flow.income_unknown || flow.in.total === null || flow.saved === null) {
    if (!flow.out.total) return null;
    return (
      <div style={{ display: "flex", gap: ".5rem", alignItems: "baseline",
                    flexWrap: "wrap", marginTop: ".5rem", fontSize: 13 }}>
        <span className="sub">{flow.heading}:</span>
        {/* the figure first, the caveat after it and whole: wrapped on a
            phone, a separator dot would end a line with nothing after it */}
        <span style={{ whiteSpace: "nowrap" }}>
          <b style={{ fontSize: "1.15rem" }}>{money(flow.out.total)}</b> went out</span>
        <span className="mut">(income unknown for this range)</span>
      </div>
    );
  }
  if (!flow.in.total && !flow.out.total) return null;
  // what was left over as printed: $0 is even, untoned, with no rate
  const sign = netSign(flow.saved);
  const saved = sign === 0 ? 0 : flow.saved;
  return (
    <div style={{ display: "flex", gap: ".5rem", alignItems: "baseline",
                  flexWrap: "wrap", marginTop: ".5rem", fontSize: 13 }}>
      {/* the server's label names the window actually totalled — the
          month so far for a ledger that starts this month, or a range cut
          to the first month with known income */}
      <span className="sub">{flow.heading}:</span>
      <b style={{ fontSize: "1.15rem" }}>{money(flow.in.total)}</b> came in
      <span className="mut">→</span>
      <b style={{ fontSize: "1.15rem" }}>{money(flow.out.total)}</b> went out
      <span className="mut">→</span>
      <b style={{ fontSize: "1.15rem" }}
         className={sign === 1 ? "pos" : sign === -1 ? "neg" : ""}>
        {signedMoney(saved, money, true)}</b>
      {sign >= 0 ? "left over" : "overspent"}
      {savedOver(flow.saved_months_known, flow.saved_months_total) && (
        <span className="sub">
          {savedOver(flow.saved_months_known, flow.saved_months_total)} with
          bank data</span>)}
      {flow.income_due && (
        <span className="sub">· {flow.income_due.text}</span>)}
      {flow.rate !== null && sign === 1 && (
        <span className="sub">· {flow.rate}% of income</span>)}
    </div>
  );
}

// the flow picture's income palette — kinds keep one color everywhere
const KIND_COLORS = { paychecks: "#4f9bd6", interest: "#7fb8e6",
                      other: "#3d7fb0" } as const;
const KIND_LABELS = { paychecks: "Paychecks", interest: "Interest & dividends",
                      other: "Other" } as const;

/** One income source's paycheck control: whether its deposits count as
 *  paychecks, as the server decided (paychecks.payer_verdicts), and the
 *  household's word on it. The automatic choice names what the rules
 *  decided; nothing is worked out here. specs/paychecks.md */
function PaycheckChoice({ payee, verdict, mayEdit }: {
  payee: string; verdict: SourcePaycheck; mayEdit: boolean;
}) {
  const qc = useQueryClient();
  const save = useMutation({
    mutationFn: (word: "paycheck" | "never" | "auto") =>
      api.incomePayer(verdict.key, word, payee),
    onSuccess: () => {
      // the split, the share and the flow picture all move: refetch them
      qc.invalidateQueries({ queryKey: ["report", "income-window"] });
      qc.invalidateQueries({ queryKey: ["report", "cashflow-flow"] });
    },
    onError: (e) => toast(errText(e)),
  });
  const auto = verdict.override === null;
  const shown = verdict.paycheck ? "Paycheck" : "Not a paycheck";
  if (!mayEdit)
    return <span className="sub" style={{ fontSize: 12 }}>{shown}</span>;
  return (
    <select aria-label={`Does ${payee} count as paychecks?`}
            value={verdict.override ?? "auto"}
            disabled={save.isPending}
            onChange={(e) => save.mutate(
              e.target.value as "paycheck" | "never" | "auto")}
            style={{ fontSize: 12 }}>
      <option value="auto">
        {auto ? `${shown} (automatic)` : "Automatic"}</option>
      <option value="paycheck">Paycheck</option>
      <option value="never">Not a paycheck</option>
    </select>
  );
}

function IncomeSection({ range, setRange }: {
  range: Range; setRange: (v: Range) => void;
}) {
  const iq = useQuery({ queryKey: ["report", "income-window", range],
                        queryFn: () => api.reportIncomeWindow(range),
                        placeholderData: (prev) => prev });
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const mayEdit = canEdit(me);
  if (iq.isPending) return <Loading />;
  // a failed range keeps its card and the range picker, so another window
  // is one click away
  if (iq.data === undefined)
    return (
      <div className="card" style={{ marginTop: 0 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: ".8rem",
                      flexWrap: "wrap" }}>
          <h2 style={{ margin: 0 }}>Income</h2>
          <RangePicker value={range} onChange={setRange}
                       style={{ marginLeft: "auto" }} />
        </div>
        <FailedLine q={iq} what={`income (${rangeCaption(range)})`} />
      </div>);
  const w = iq.data;
  const kinds = (Object.keys(KIND_COLORS) as (keyof typeof KIND_COLORS)[])
    .map((k) => ({ k, v: w.kinds[k] })).filter((x) => x.v > 0);
  const maxSrc = w.sources[0]?.[1] || 1;

  return (
    <>
      <div className="card" style={{ marginTop: 0 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: ".8rem",
                      flexWrap: "wrap" }}>
          <h2 style={{ margin: 0 }}>Income</h2>
          <RangePicker value={range} onChange={setRange}
                       style={{ marginLeft: "auto" }} />
        </div>
        {/* nothing came in over the window: one line, not "$0 came in" */}
        {w.total === 0 && w.sources.length === 0
          ? <EmptyState title={EMPTY_LINES.incomeRange} /> : (
        <div style={{ display: "flex", gap: ".5rem", alignItems: "baseline",
                      flexWrap: "wrap", marginTop: ".5rem", fontSize: 13 }}>
          {/* so_far: the month so far (the server's heading says so; no
              per-month pace), with the paychecks still due beside it */}
          <span className="sub">{w.heading}:</span>
          <b style={{ fontSize: "1.15rem" }}>{money(w.total)}</b> came in
          {!w.so_far && <><span className="mut">·</span>
            <b>{money(w.per_month)}</b>/mo
            {/* the server's words for what "/mo" divides by when that is
                fewer months than the heading names */}
            {w.per_month_basis ? ` ${w.per_month_basis}` : ""}</>}
          {w.income_due && <><span className="mut">·</span>
            <span className="sub">{w.income_due.text}</span></>}
          {w.paycheck_share !== null && <><span className="mut">·</span>
            <b>{w.paycheck_share}%</b> <span className="sub">paychecks</span></>}
          {w.change && (
            <><span className="mut">·</span>
              <Delta ch={w.change} downIsGood={false} />
              <span className="sub">{w.prev_text}</span></>
          )}
        </div>)}
        {/* a payer of the previous window who paid nothing in this one, in
            the server's words, beside the change it explains */}
        {w.dropped_text && (
          <div className="sub" style={{ marginTop: ".3rem" }}>{w.dropped_text}</div>)}
        <IncompleteNote months={w.incomplete_months} />
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
              {kinds.map(({ k }, i) => (
                <span key={k}>{i > 0 && " · "}
                  <span style={{ display: "inline-block", width: 8, height: 8,
                                 borderRadius: 2, background: KIND_COLORS[k],
                                 marginRight: 4 }} />
                  {/* the Overview picture's dollars for this kind
                      (reporting.income_kinds_shown), never rounded here */}
                  {KIND_LABELS[k]} {money(w.kinds_shown[k])}
                  {w.kind_changes && <> <Delta ch={w.kind_changes[k]}
                                              downIsGood={false} /></>}
                </span>
              ))}
            </div>
          </>
        )}
        {w.by_month.length >= 4 && (
          // the same pace line the Spending tab opens with
          <div style={{ marginTop: ".6rem" }}>
            <AreaChart points={w.by_month} height={160} subject="Income by month"
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
              {w.sources.map(([payee, amt, n, , logo], i) => (
                <tr key={payee}>
                  <td style={{ maxWidth: "14rem" }}>
                    <MerchantAvatar name={payee} logo={logo}
                                    style={{ marginRight: 7 }} />{payee}</td>
                  {/* the bar and the deposit count step aside at phone
                      width (the Spending tab's rule), so the paycheck
                      control at the row's end stays on screen */}
                  <td className="c-sharebar" style={{ width: "30%" }}>
                    <span style={{ display: "inline-block", height: 9,
                                   borderRadius: 3, background: "#4f9bd6",
                                   verticalAlign: "middle",
                                   width: `${Math.max(2, (100 * amt) / maxSrc)}%` }} />
                  </td>
                  <td className="num">{money(amt)}</td>
                  <td className="num mut" style={{ fontSize: 12 }}>
                    {/* the server's share (income_window → source_shares,
                        adding up to 100 across every payer) */}
                    {w.source_shares[i] != null ? `${w.source_shares[i]}%` : ""}</td>
                  <td className="num mut hide-m" style={{ fontSize: 12 }}>{n}×</td>
                  <td className="num" style={{ fontSize: 12 }}>
                    <Delta ch={w.source_changes[i]} downIsGood={false} /></td>
                  {/* an older server sends no verdicts: no control */}
                  {w.source_paychecks?.[i] && (
                    <td className="num">
                      <PaycheckChoice payee={payee} mayEdit={mayEdit}
                                      verdict={w.source_paychecks[i]} /></td>)}
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
  // a failed read keeps the card's place: gone, it read as nothing paid
  if (!r && q.isError)
    return (
      <div className="card"><h2>Interest &amp; fees paid</h2>
        <FailedLine q={q} what="the interest and fees paid" /></div>);
  if (!r || r.by_year.length === 0) return null;
  const ytd = r.ytd;
  const year = r.by_year[0].year;
  // The tiles and every cell are the server's whole dollars (`shown`,
  // reporting.compute_cash_costs): a year's interest + fees − refunds give
  // its net as printed, and "vs interest earned" is earned less paid as
  // printed. Nothing is rounded here.
  const net = r.shown.paid_ytd;
  const vsEarned = r.shown.vs_earned_ytd;
  return (
    <div className="card">
      <h2>Interest &amp; fees paid <span className="sub" style={{ fontWeight: 400 }}>
        — what holding money costs you, by year</span></h2>
      <div style={{ display: "flex", gap: "1.4rem", flexWrap: "wrap",
                    alignItems: "baseline" }}>
        <div><span className="sub">{ytd ? `${ytd.year} so far` : `${year}`}</span>
          <div className={`kpi sm ${net > 0 ? "neg" : net < 0 ? "pos" : ""}`}>
            {signedMoney(net, money)}</div>
          <div className="sub" style={{ fontSize: 11 }}>
            {ytd ? <>{money(ytd.shown.interest)} interest · {money(ytd.shown.fees)} fees
                     {ytd.shown.refunds > 0 && <> − {money(ytd.shown.refunds)} refunded</>}</>
                 : "nothing charged this year"}</div></div>
        <div><span className="sub">vs interest earned on cash</span>
          <div className={`kpi sm ${vsEarned > 0 ? "pos" : vsEarned < 0 ? "neg" : ""}`}>
            {signedMoney(vsEarned, money, true)}</div>
          <div className="sub" style={{ fontSize: 11 }}>
            earned on cash {money(r.shown.earned_ytd)} · {net < 0 ? `refunded ${money(-net)}` : `paid ${money(net)}`} this year</div></div>
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
              <td className="num">{y.shown.interest ? money(y.shown.interest) : "·"}</td>
              <td className="num">{y.shown.fees ? money(y.shown.fees) : "·"}</td>
              <td className="num pos">{y.shown.refunds ? `−${money(y.shown.refunds)}` : "·"}</td>
              <td className="num"><b>{signedMoney(y.shown.net, money)}</b></td>
              <td className="mut" style={{ fontSize: 12 }}>
                {y.biggest_text ?? "—"}</td>
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
             open={open} className="disclosure" style={{ margin: ".8rem 0" }}>
      <summary className="mut" style={{ fontSize: 13 }}>
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
                         subject="Career earnings, SSA record"
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

function YearRow({ yr, inc, sp, net, rate, note, chart, slots, missing, open,
                   toggle }: {
  yr: string; inc: number | null; sp: number; net: number | null;
  /** the server's words for a year holding a month with incomplete
   *  income; such a row is drawn greyed */
  note?: string;
  rate: number | null; chart?: [string, number][];
  /** the year's months as the server sends them, an incomplete one null */
  slots: [string, number | null][]; missing: string; open: boolean;
  toggle: () => void;
}) {
  return (
    <>
      {/* the whole row opens the chart for a mouse; the keyboard and a
          screen reader get the year as a real button with its state */}
      <tr onClick={chart ? toggle : undefined} className={note ? "mut" : undefined}
          style={chart ? { cursor: "pointer" } : undefined}>
        <td>{chart ? (
          <button type="button" className="as-link" aria-expanded={open}
                  style={{ textDecoration: "none", color: "inherit" }}
                  onClick={(e) => { e.stopPropagation(); toggle(); }}>
            <span className="mut" style={{ display: "inline-block", width: "1em" }}>
              {open ? "▾" : "▸"}</span>{yr}</button>) : yr}</td>
        <td className="num">{inc !== null ? money(inc)
          : <span className="mut" style={{ fontSize: 12 }}>no data</span>}</td>
        <td className="num">{money(sp)}</td>
        {net !== null
          ? <td className={`num ${note ? "" : netSign(net) === 1 ? "pos"
                                   : netSign(net) === -1 ? "neg" : ""}`}>{money(net)}
              {note && <div className="sub" style={{ fontSize: 10 }}>{note}</div>}</td>
          : <td className="num mut" style={{ fontSize: 12 }}>no data
              {note && <div className="sub" style={{ fontSize: 10 }}>{note}</div>}</td>}
        {/* the rate as the Year lens prints the same year's (signedPct);
            none beside a left over that prints $0 */}
        <td className={`num ${rate !== null && rate < 0 ? "neg" : ""}`}>
          {rate !== null && (net === null || netSign(net) !== 0)
            ? signedPct(rate) : "·"}</td>
      </tr>
      {chart && open && (
        <tr>
          <td colSpan={5} style={{ padding: ".2rem .5rem 1rem" }}>
            <div className="sub" style={{ margin: ".2rem 0 .4rem" }}>
              Left over, by month — {yr}</div>
            <NetBars points={slots} height={170}
                     missing={missing}
                     subject={`Left over per month in ${yr}`} />
          </td>
        </tr>
      )}
    </>
  );
}
