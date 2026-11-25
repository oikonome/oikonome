// Cash flow — the web page's three tabs: Overview (the flow picture for
// a period, the conclusion in a sentence, saved-by-month with the amount
// on every bar, savings-by-year with per-year drill-down), Income (income
// by month, investment funding, SSA career earnings, reported-income tax
// table). Spending is its own screen; the tab links to it. Mirrors
// webapp/src/pages/CashFlow.tsx — keep them in step.
import { useMutation, useQuery, useQueryClient,
         type UseQueryResult } from "@tanstack/react-query";
import { useRouter } from "expo-router";
import { useState } from "react";
import { Alert, Pressable, ScrollView, StyleSheet, Text, View, useWindowDimensions } from "react-native";
import PullRefresh from "../components/pull-refresh";

import FlowChart from "../components/flow-chart";
import MerchantAvatar from "../components/merchant-avatar";
import NetBars from "../components/net-bars";
import Sparkline from "../components/sparkline";
import { EmptyState, FailedLine, StaleLine } from "../components/loaded";
import { Card, H, HelpLink, KV, Skeleton } from "../components/ui";
import type { CashCostYear, CashCostsReport, CashflowReport, Change, FlowKey,
              IncomeWindow, SourcePaycheck } from "../lib/api";
import { errText } from "../lib/api";
import { useViewer } from "../lib/viewer";
import { TREND_RANGES, changeLook, fillMonthKeyGaps, graphWindow,
         fillMonthLabelGaps, RANGE_MONTHS, rangeCaption,
         savedFigure, type TrendRange, windowLabel, windowSoFar,
         DEFAULT_TREND_RANGE, changeWords, monthWithYear,
         EMPTY_LINES, netSign, signedMoney, signedPct }
  from "../lib/pure";
import { useSession } from "../lib/session";
import { C, money } from "../lib/theme";
import { disclosure } from "../lib/a11y";
import SegmentedControl, { RangePicker } from "../components/segmented-control";

const m$ = (n: number) => money(n, false);
// both pickers speak the site-wide 3m/6m/1y/3y/5y/all vocabulary (pure.ts
// TREND_RANGES ↔ webapp components/RangePicker — keep them in step)
const MONTHS3 = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul",
                 "Aug", "Sep", "Oct", "Nov", "Dec"];
const pretty = (v: string) =>
  MONTHS3[Number(String(v).slice(5, 7))] || String(v);

/** The left-over-per-month series for a range — the web's savedSeries:
 *  the slots of the graph the server says the range draws. */
function savedSeries(cf: CashflowReport, range: TrendRange):
    { points: [string, number | null][]; keys: string[];
      estimatedFrom: number; note: string } {
  const graph = cf.cash_graph;
  const months = RANGE_MONTHS[range];
  const w = graphWindow(graph, range);
  const drawn = (key: string) => key >= w.first && key <= w.last;
  // a month no bank feed covered keeps its place as an empty slot
  // the month in progress (saved so far) is a solid bar after them
  const history = fillMonthKeyGaps([...graph.points.filter(
    (p, i) => i < graph.forecast_from && drawn(p[0])),
    ...(graph.current && drawn(graph.current[0]) ? [graph.current] : [])]);
  // the month in progress is drawn once: its saved-so-far bar, not a
  // forecast bar beside it as well (the web's rule)
  const tail = graph.points.slice(graph.forecast_from)
    .filter((p) => drawn(p[0])
      && (!graph.current || p[0] !== graph.current[0]));
  const all = [...history, ...tail];
  // up to a year a bare month; past a year every label names its year
  // ("Mar '24"), since only every nth label is written there (the web's
  // rule)
  const label = (key: string) =>
    months !== null && months <= 12 ? pretty(key) : monthWithYear(key);
  return { points: all.map((p) => [label(p[0]), p[1]]),
           keys: all.map((p) => p[0]), estimatedFrom: history.length,
           note: w.note };
}

// years "Left over by year" shows before "Show all" (web: CashFlow.tsx)
const SAVINGS_YEARS_SHORT = 5;

export default function CashFlow() {
  const { client } = useSession();
  const { width } = useWindowDimensions();
  const router = useRouter();
  const [tab, setTab] = useState<"overview" | "income">("overview");
  // ONE timeframe for the whole screen — the flow picture, saved-by-month
  // and the Income tab all follow it (the web page does the same via the
  // URL), so switching tabs never resets a picked range
  const [range, setRange] = useState<TrendRange>(DEFAULT_TREND_RANGE);
  const period: FlowKey = range;
  const [open, setOpen] = useState<Record<string, boolean>>({});
  // Left over by year opens on the latest few years (as on the web)
  const [allYears, setAllYears] = useState(false);
  const query = useQuery({
    queryKey: ["report", "cashflow"],
    queryFn: () => client!.reportCashflow(),
    enabled: !!client,
  });
  // each timeframe is its own fetch (the fixed split walks months
  // server-side); TanStack keeps every range once seen
  const flowQ = useQuery({
    queryKey: ["report", "cashflow-flow", period],
    queryFn: () => client!.reportCashflowFlow(period),
    enabled: !!client,
    // previous window stays on screen while a wide one computes
    placeholderData: (prev) => prev,
  });
  // hoisted out of IncomeTab so pull-to-refresh can retry it; only fetched
  // while that tab is showing (refetch() ignores `enabled`, hence the gate
  // in onRefresh too)
  const iq = useQuery({
    queryKey: ["report", "income-window", range],
    queryFn: () => client!.reportIncomeWindow(range),
    enabled: !!client && tab === "income",
    placeholderData: (prev) => prev,
  });
  // interest & fees paid — the Income tab's last living card; same gate
  const ccq = useQuery({
    queryKey: ["report", "cash-costs"],
    queryFn: () => client!.reportCashCosts(),
    enabled: !!client && tab === "income",
  });
  const d = query.data;
  const flow = flowQ.data;
  const series = d ? savedSeries(d, range) : null;
  // The window's best and worst month are the server's (reporting.
  // _saved_extremes), as on the web: finished months of this timeframe
  // only, neither when the window is a single month. An older server
  // sends none, and none is printed — this screen never ranks the bars.
  const { best = null, worst = null } = d?.cash_graph.extremes?.[range] ?? {};
  const fig = (v: number) => {
    const cw = changeWords(v, m$);
    return { text: cw.text,
             color: cw.tone === "pos" ? C.good : cw.tone === "neg" ? C.bad
               : C.text };
  };
  const savings = [...(d?.savings_by_year ?? [])].reverse();
  // a year's row in the whole dollars the server prints it in
  const yd = (y: string, inc: number | null, sp: number, net: number | null) =>
    d?.savings_by_year_dollars !== undefined
      ? d.savings_by_year_dollars[y]
      // older-server path: stands in for `savings_by_year_dollars`
      : { income: inc, spend: sp, saved: net };
  const chartW = width - 24 - 28;

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      // every query on screen, else a failed
                                      // flow/income fetch has no retry short
                                      // of leaving the screen
                                      onRefresh={() => Promise.all([
                                        query.refetch(), flowQ.refetch(),
                                        ...(tab === "income"
                                          ? [iq.refetch(), ccq.refetch()]
                                          : [])])} />}>
      <StaleLine query={query} />
      {query.isPending && <Skeleton />}
      {/* the tabs and the range picker stand while the overview loads
          and when its read fails, as the web's strip does: the Income tab
          is its own read, and another window is one tap away */}
      {(
        <Card>
          <SegmentedControl label="Cash Flow view" value={tab}
            onChange={(k) => k === "spending"
              // the picked window rides along, as the web's URL keeps it
              ? router.push(`/spending?range=${range}` as never)
              : setTab(k)}
            options={([["overview", "Overview"], ["spending", "Spending"],
                       ["income", "Income"]] as const).map(([k, l]) => (
                         { value: k, label: l }))} />
          {tab === "overview" && (
            <>
              <RangePicker value={range} onChange={setRange}
                           style={{ marginTop: 8 }} />
              {/* a failed switch keeps the previous window's picture
                  (placeholderData): say so above it, as the web does */}
              <StaleLine query={flowQ} />
              {query.isError && !d && (
                <FailedLine q={query} what="cash flow" />)}
              {flow && (
                // while a wide window computes the previous picture stays,
                // dimmed, with an honest line — the web does the same
                <View style={flowQ.isFetching ? { opacity: 0.45 } : undefined}>
                  <View style={{ marginTop: 10 }}>
                    <FlowChart flow={flow} width={chartW} height={190} />
                  </View>
                  {flowQ.isFetching && (
                    <Text style={[s.mut, { marginTop: 4 }]}>
                      computing {period === "all" ? "all history" : period}…
                    </Text>
                  )}
                  {/* income unknown (no bank feed covers the window):
                      say so, and claim no saved figure or rate */}
                  {(flow.income_unknown || flow.in.total === null
                    || flow.saved === null) ? (flow.out.total > 0 && (
                    <Text style={[s.mut, { marginTop: 6 }]}>
                      {windowLabel(flow)}:{" "}
                      <Text style={s.big}>{m$(flow.out.total)}</Text> went out
                      {" "}(income unknown for this range)
                    </Text>
                  )) : (flow.in.total > 0 || flow.out.total > 0) && (
                    <Text style={[s.mut, { marginTop: 6 }]}>
                      {windowLabel(flow)}:{" "}
                      <Text style={s.big}>{m$(flow.in.total)}</Text> came in →{" "}
                      <Text style={s.big}>{m$(flow.out.total)}</Text> went out →{" "}
                      {/* what was left over as printed: $0 is even,
                          untoned, with no rate (the web's rule) */}
                      <Text style={[s.big, netSign(flow.saved) !== 0
                        && { color: netSign(flow.saved) === 1 ? C.good : C.bad }]}>
                        {signedMoney(netSign(flow.saved) === 0 ? 0 : flow.saved,
                                     m$, true)}
                      </Text>{" "}{netSign(flow.saved) >= 0 ? "left over" : "overspent"}
                      {savedOver(flow.saved_months_known, flow.saved_months_total)
                        ? ` ${savedOver(flow.saved_months_known,
                                         flow.saved_months_total)} with bank data`
                        : ""}
                      {/* the web's order: the paychecks still due, then
                          the rate */}
                      {flow.income_due ? ` · ${flow.income_due.text}` : ""}
                      {flow.rate !== null && netSign(flow.saved) === 1
                        ? ` · ${flow.rate}% of income` : ""}
                    </Text>
                  )}
                  {/* what moved to investments is its own figure, never
                      part of what was left over (the server's line; an
                      older server sends none) */}
                  {flow.invested_text ? (
                    <Text style={[s.mut, { marginTop: 2 }]}>
                      {flow.invested_text}</Text>) : null}
                  <IncompleteNote months={flow.incomplete_months} />
                  {flow.out.unsplit_text ? (
                    <Text style={[s.mut, { marginTop: 4, fontSize: 12 }]}>
                      {flow.out.unsplit_text}</Text>) : null}
                  {/* what Fixed means on a window before most of today's
                      bills began, as the server says it.
                      older-server path: stands in for `fixed_basis_text`
                      — nothing is printed in its place */}
                  {flow.out.fixed_basis_text ? (
                    <Text style={[s.mut, { marginTop: 4, fontSize: 12 }]}>
                      {flow.out.fixed_basis_text}</Text>) : null}
                </View>
              )}
              {flowQ.isError && !flow && (
                <FailedLine q={flowQ} what={`the flow (${
                  rangeCaption(period)})`} />)}
            </>
          )}
        </Card>
      )}

      {d && tab === "overview" && series && (
        <>
          {series.points.length > 0 && (
            <Card>
              <View style={{ flexDirection: "row", alignItems: "center",
                             gap: 6, flexWrap: "wrap" }}>
                {/* follows the screen's one timeframe — the chips in the
                    flow card above own it */}
                <H>Left over, by month</H>
              </View>
              <NetBars width={chartW} points={series.points}
                       estimatedFrom={series.estimatedFrom}
                       missing={d.incomplete_label} />
              <Text style={s.mut}>
                {best ? <>best {monthWithYear(best[0])}{" "}
                  <Text style={{ color: fig(best[1]).color,
                                 fontWeight: "700" }}>
                    {fig(best[1]).text}</Text></> : null}
                {best && worst ? " · " : ""}
                {worst ? <>worst {monthWithYear(worst[0])}{" "}
                  <Text style={{ color: fig(worst[1]).color,
                                 fontWeight: "700" }}>
                    {fig(worst[1]).text}</Text></> : null}
                {series.points.length > series.estimatedFrom
                  ? `${best || worst ? " · " : ""}${forecastCaption(
                      d.cash_graph.forecast_days)}`
                  : ""}
              </Text>
              {/* the server's line: it names this month only for a
                  range whose bars reach it */}
              {series.note ? (
                <Text style={[s.mut, { fontSize: 11, marginTop: 2 }]}>
                  {series.note}
                </Text>) : null}
            </Card>
          )}
          <Card>
            <View style={{ flexDirection: "row", alignItems: "baseline",
                           justifyContent: "space-between", flexWrap: "wrap" }}>
              <H>Left over by year</H>
              <Text style={s.mut}>▸ open a year for its months</Text>
            </View>
            <View style={s.tRow}>
              <Text style={[s.tHead, { flex: 1, textAlign: "left" }]}>
                Year
              </Text>
              <Text style={s.tHead}>In</Text>
              <Text style={s.tHead}>Out</Text>
              <Text style={s.tHead}>Left over</Text>
              <Text style={[s.tHead, { width: 50 }]}>Rate</Text>
            </View>
            {(allYears ? savings : savings.slice(0, SAVINGS_YEARS_SHORT))
              .map(([y, inc, sp, net, rt]) => (
              <View key={y}>
                <Pressable style={s.tRow}
                           accessibilityRole="button"
                           accessibilityState={d.monthly_by_year[y]
                             ? { expanded: !!open[y] } : { disabled: true }}
                           onPress={() => d.monthly_by_year[y]
                             && setOpen((o) =>
                               ({ ...o, [y]: !o[y] }))}>
                  {/* a year holding a month with incomplete income is
                      greyed and carries the server's note (the web's
                      YearRow) */}
                  <Text style={{ color: d.savings_by_year_notes?.[y]
                                   ? C.mut : C.text,
                                 fontSize: 13, flex: 1 }}>
                    {d.monthly_by_year[y]
                      ? (open[y] ? "▾ " : "▸ ") : ""}{y}
                  </Text>
                  <Text style={[s.tNum, !!d.savings_by_year_notes?.[y]
                    && { color: C.mut }]}>
                    {/* the server's whole dollars (reporting.year_dollars,
                        the Year lens's rule) */}
                    {inc !== null ? m$(yd(y, inc, sp, net).income ?? inc)
                      : "no data"}
                  </Text>
                  <Text style={[s.tNum, !!d.savings_by_year_notes?.[y]
                    && { color: C.mut }]}>{m$(yd(y, inc, sp, net).spend)}</Text>
                  <Text style={[s.tNum, net !== null
                    && { color: d.savings_by_year_notes?.[y] ? C.mut
                           : netSign(yd(y, inc, sp, net).saved ?? net) === 1
                             ? C.good
                             : netSign(yd(y, inc, sp, net).saved ?? net) === -1
                               ? C.bad : C.text }]}>
                    {net !== null ? m$(yd(y, inc, sp, net).saved ?? net)
                      : "no data"}
                    {d.savings_by_year_notes?.[y]
                      ? `\n${d.savings_by_year_notes[y]}` : ""}
                  </Text>
                  <Text style={[s.tNum, { width: 50 },
                                d.savings_by_year_notes?.[y]
                                  ? { color: C.mut }
                                  : rt !== null && rt < 0
                                    && { color: C.bad }]}>
                    {/* the rate as the Year screen prints it */}
                    {rt !== null && (net === null
                      || netSign(yd(y, inc, sp, net).saved ?? net) !== 0)
                      ? signedPct(rt) : "·"}
                  </Text>
                </Pressable>
                {open[y] && d.monthly_by_year[y] && (
                  <View style={{ paddingVertical: 6 }}>
                    <Text style={s.mut}>Left over, by month — {y}</Text>
                    {/* the server's slots, an incomplete month null */}
                    <NetBars width={chartW}
                             // older-server path: stands in for `months_by_year`
                             points={d.months_by_year?.[y]
                               ?? fillMonthLabelGaps(d.monthly_by_year[y])}
                             missing={d.incomplete_label}
                             height={150} subject={`Left over per month in ${y}`} />
                  </View>
                )}
              </View>
            ))}
            {savings.length > SAVINGS_YEARS_SHORT && (
              <Pressable accessibilityRole="button"
                         accessibilityState={{ expanded: allYears }}
                         onPress={() => setAllYears(!allYears)}
                         style={{ paddingVertical: 8 }}>
                <Text style={{ color: C.accent, fontSize: 13 }}>
                  {allYears ? "Show fewer"
                    : `Show all ${savings.length} years`}
                </Text>
              </Pressable>
            )}
          </Card>
        </>
      )}

      {tab === "income" && (
        <IncomeTab chartW={chartW} range={range} setRange={setRange}
                   iq={iq} />
      )}
      {tab === "income" && ccq.data && <CashCosts r={ccq.data} />}
      {/* a failed read keeps the card's place: gone, it read as nothing
          paid */}
      {tab === "income" && ccq.data === undefined && ccq.isError && (
        <Card>
          <H>Interest &amp; fees paid</H>
          <FailedLine q={ccq} what="the interest and fees paid" />
        </Card>
      )}
      {d && tab === "income" && (
        <IncomeRecords d={d} chartW={chartW} />
      )}
      <HelpLink topic="cash-flow" />
    </ScrollView>
  );
}

/** "over 7 of 9 months" — saved and the rate cover only the months with
 *  bank data (the web's savedOver). */
function savedOver(known?: number, total?: number): string {
  return known !== undefined && total !== undefined && known < total
    ? `over ${known} of ${total} months` : "";
}

/** The hollow bars' caption, naming the server's forecast horizon (the
 *  web's forecastCaption). */
function forecastCaption(days?: number): string {
  return days ? `hollow bars are the ${days}-day forecast`
              : "hollow bars are the forecast";
}

/** The months of a window the bank feed misses: still counted, so the
 *  screen says their income is incomplete (the web's IncompleteNote). */
function IncompleteNote({ months }: { months?: string[] }) {
  if (!months || months.length === 0) return null;
  const name = (k: string) => `${MONTHS3[Number(k.slice(5, 7))]} ${k.slice(0, 4)}`;
  const shown = months.length <= 3 ? months.map(name).join(", ")
    : `${name(months[0])} – ${name(months[months.length - 1])}`;
  return (
    <Text style={[s.mut, { marginTop: 4, fontSize: 12 }]}>
      <Text style={{ color: C.warn, fontWeight: "600" }}>incomplete</Text>
      {" "}{months.length === 1 ? "1 month" : `${months.length} months`} without
      a bank feed ({shown}) — counted, but income there is missing or partial
    </Text>
  );
}

// the flow picture's income palette — kinds keep one color everywhere
// (webapp KIND_COLORS twin)
const KIND_COLORS = { paychecks: "#4f9bd6", interest: "#7fb8e6",
                      other: "#3d7fb0" } as const;
const KIND_LABELS = { paychecks: "Paychecks", interest: "Interest & dividends",
                      other: "Other" } as const;

/** "+$420 (+5%)", colored — income going up is good. The server's change
 *  (reporting.change), "unchanged" at $0 as on the web; an older server
 *  sends none and the change is left out. */
function IncDelta({ ch }: { ch: Change | null | undefined }) {
  const c = changeLook(ch, false);
  if (!c) return null;
  if (c.unchanged)
    return <Text style={{ color: C.mut, fontSize: 12 }}>unchanged</Text>;
  return (
    <Text style={{ color: c.good ? C.good : C.bad, fontSize: 12,
                   fontWeight: "600" }}>
      {c.sign}{m$(c.dollars)}{c.pct !== null ? ` (${c.sign}${c.pct}%)` : ""}
    </Text>
  );
}

/** One income source's paycheck chip: whether its deposits count as
 *  paychecks, as the server decided, and — for an editor — the three
 *  choices the web's select offers (automatic, paycheck, not a paycheck).
 *  Mirrors the web's PaycheckChoice; nothing is worked out here.
 *  specs/paychecks.md */
function PaycheckChip({ payee, verdict }: {
  payee: string; verdict: SourcePaycheck;
}) {
  const { client } = useSession();
  const qc = useQueryClient();
  const viewer = useViewer();
  const save = useMutation({
    mutationFn: (word: "paycheck" | "never" | "auto") =>
      client!.incomePayer(verdict.key, word, payee),
    onSuccess: () => {
      // the split, the share and the flow picture all move: refetch them
      qc.invalidateQueries({ queryKey: ["report", "income-window"] });
      qc.invalidateQueries({ queryKey: ["report", "cashflow-flow"] });
    },
    onError: (e) => Alert.alert("Not saved", errText(e)),
  });
  const shown = verdict.paycheck ? "Paycheck" : "Not a paycheck";
  const auto = verdict.override === null;
  // the web select's own words: "Paycheck (automatic)" while the rules
  // decide, the household's choice by itself; a viewer reads the verdict
  if (viewer) return <Text style={{ color: C.mut, fontSize: 12 }}>{shown}</Text>;
  const label = <Text style={{ color: C.mut, fontSize: 12 }}>
    {auto ? `${shown} (automatic)` : shown} ▾</Text>;
  const ask = () => Alert.alert(`Does ${payee} count as paychecks?`,
    undefined,
    [{ text: auto ? `${shown} (automatic)` : "Automatic",
       onPress: () => save.mutate("auto") },
     { text: "Paycheck", onPress: () => save.mutate("paycheck") },
     { text: "Not a paycheck", onPress: () => save.mutate("never") }],
    // Android shows at most three buttons, so there is no Cancel button:
    // Back or a tap outside closes the question and changes nothing
    { cancelable: true });
  return (
    <Pressable onPress={ask} disabled={save.isPending} hitSlop={8}
               accessibilityRole="button"
               accessibilityLabel={`${payee}: ${shown}. Change`}>
      {label}
    </Pressable>
  );
}

/** The Income tab's living half: the windowed story, the kind split, and
 *  income-by-month with the amount on every bar. Mirrors the web's
 *  IncomeSection. */
function IncomeTab({ chartW, range, setRange, iq }: {
  chartW: number; range: TrendRange; setRange: (r: TrendRange) => void;
  iq: UseQueryResult<IncomeWindow>;
}) {
  const w = iq.data;
  if (iq.isPending) return <Text style={s.center}>Loading…</Text>;
  // a failed fetch must not render blank, with no retry
  // a failed range keeps its card and the range picker, so another
  // window is one tap away
  if (iq.isError && !w) {
    return (
      <Card>
        <View style={{ flexDirection: "row", alignItems: "center",
                       flexWrap: "wrap", gap: 6 }}>
          <H>Income</H>
          <RangePicker value={range} onChange={setRange}
                       style={{ marginLeft: "auto", alignSelf: "center" }} />
        </View>
        <FailedLine q={iq} what={`income (${rangeCaption(range)})`} />
      </Card>
    );
  }
  if (!w) return null;
  // the per-month figure and the paycheck share are the SERVER's (an
  // older server sends neither, and the line leaves them out)
  const perMonth = w.per_month;
  const payShare = w.paycheck_share;
  const kinds = (Object.keys(KIND_COLORS) as (keyof typeof KIND_COLORS)[])
    .map((k) => ({ k, v: w.kinds[k] })).filter((x) => x.v > 0);
  const maxSrc = w.sources[0]?.[1] || 1;
  return (
    <>
      <Card>
        <View style={{ flexDirection: "row", alignItems: "center",
                       flexWrap: "wrap", gap: 6 }}>
          <H>Income</H>
          <RangePicker value={range} onChange={setRange}
                       style={{ marginLeft: "auto", alignSelf: "center" }} />
        </View>
        {/* nothing came in over the window: one line, not "$0 came in" */}
        {w.total === 0 && w.sources.length === 0
          ? <EmptyState title={EMPTY_LINES.incomeRange} /> : (
        <Text style={[s.mut, { marginTop: 8 }]}>
          {/* the web's one line, in its order: the pace, the paychecks
              still due, the paycheck share, the change */}
          {windowLabel(w)}: <Text style={s.big}>{m$(w.total)}</Text> came in
          {!windowSoFar(w) && perMonth != null ? <>{" · "}
            <Text style={{ color: C.text, fontWeight: "600" }}>
              {m$(perMonth)}</Text>/mo
            {/* the server's words for what "/mo" divides by, as the web
                prints them; an older server sends none */}
            {w.per_month_basis ? ` ${w.per_month_basis}` : ""}</> : null}
          {w.income_due ? ` · ${w.income_due.text}` : null}
          {payShare != null ? <>{" · "}
            <Text style={{ color: C.text, fontWeight: "600" }}>
              {payShare}%</Text> paychecks</> : null}
          {/* the server's words ("vs Oct 1–17"); a server that sends none
              gets none — a caption worded here would be a second
              definition of the comparison */}
          {w.change ? <>{" · "}<IncDelta ch={w.change} />{" "}
            {w.prev_text ?? ""}</> : null}
        </Text>)}
        {/* a payer of the previous window who paid nothing in this one, in
            the server's words (dropped_text), beside the change it explains;
            an older server sends none and nothing is said */}
        {w.dropped_text ? (
          <Text style={[s.mut, { marginTop: 4 }]}>{w.dropped_text}</Text>
        ) : null}
        <IncompleteNote months={w.incomplete_months} />
        {w.total > 0 && (
          <>
            <View style={{ flexDirection: "row", height: 10, borderRadius: 4,
                           overflow: "hidden", marginTop: 8, gap: 2 }}>
              {kinds.map(({ k, v }) => (
                <View key={k} style={{ flex: v, backgroundColor: KIND_COLORS[k] }} />
              ))}
            </View>
            <Text style={[s.mut, { marginTop: 4 }]}>
              {kinds.map(({ k, v }, i) => (
                <Text key={k}>{i > 0 ? " · " : ""}
                  {/* the Overview picture's dollars for this kind, as
                      on the web */}
                  {/* older-server path: stands in for `kinds_shown` */}
                  {KIND_LABELS[k]} {m$(w.kinds_shown?.[k] ?? v)}
                  {w.kind_changes
                    ? <> <IncDelta ch={w.kind_changes[k]} /></> : null}
                </Text>
              ))}
            </Text>
          </>
        )}
        {w.by_month.length >= 4 && (
          // the same pace line the Spending screen opens with
          <View style={{ marginTop: 8 }}>
            <Sparkline width={chartW} height={140} data={w.by_month}
                       subject="Income by month" />
          </View>
        )}
      </Card>
      {w.sources.length > 0 && (
        <Card>
          <H>Income by source{" "}
            <Text style={{ color: C.mut, fontSize: 12, fontWeight: "400" }}>
              — who paid, over the window</Text></H>
          {w.sources.map(([payee, amt, n, , logo], i) => (
            <View key={payee} style={{ marginVertical: 4 }}>
              <View style={{ flexDirection: "row",
                             justifyContent: "space-between", gap: 8 }}>
                <View style={{ flexDirection: "row", alignItems: "center",
                               flexShrink: 1 }}>
                  <View style={{ marginRight: 6 }}>
                    <MerchantAvatar name={payee} logo={logo} size={18} />
                  </View>
                  <Text style={{ color: C.text, fontSize: 13, flexShrink: 1 }}
                        numberOfLines={1}>{payee}</Text>
                </View>
                <View style={{ flexDirection: "row", gap: 8,
                               alignItems: "baseline" }}>
                  <IncDelta ch={w.source_changes?.[i]} />
                  <Text style={{ color: C.mut, fontSize: 13,
                                 fontVariant: ["tabular-nums"] }}>
                    {/* the web's share column: the server's share of
                        the window (source_shares, adding up to 100); an
                        older server sends none */}
                    {m$(amt)}{w.source_shares?.[i] != null
                      ? ` · ${w.source_shares[i]}%` : ""} · {n}×</Text>
                </View>
              </View>
              <View style={{ height: 5, borderRadius: 3,
                             backgroundColor: C.hover, marginTop: 3,
                             overflow: "hidden" }}>
                <View style={{ height: 5, borderRadius: 3,
                               backgroundColor: "#4f9bd6",
                               width: `${Math.max(2, (100 * amt) / maxSrc)}%` }} />
              </View>
              {/* an older server sends no verdicts: no chip */}
              {w.source_paychecks?.[i] ? (
                <View style={{ marginTop: 2, alignSelf: "flex-start" }}>
                  <PaycheckChip payee={payee}
                                verdict={w.source_paychecks[i]} />
                </View>) : null}
            </View>
          ))}
        </Card>
      )}
    </>
  );
}

const mmdd = (iso: string) => `${iso.slice(5, 7)}/${iso.slice(8, 10)}`;
// '2026-11-03' → 'Nov 2026' (the web's monYear)
const monYear = (iso: string) =>
  `${MONTHS3[Number(iso.slice(5, 7))]} ${iso.slice(0, 4)}`;

/** Interest and fees paid — what holding money and carrying a balance cost,
 *  by year, net of refunds, beside what the same banks paid in interest.
 *  Under income by source because interest EARNED is a row up there; this
 *  is the other side of it. Hidden when nothing was ever charged. Mirrors
 *  the web's CashCosts. */
function CashCosts({ r }: { r: CashCostsReport }) {
  if (r.by_year.length === 0) return null;
  const ytd = r.ytd;
  // The tiles and every cell are the server's whole dollars (`shown`,
  // reporting.compute_cash_costs), as on the web: a year's interest + fees
  // − refunds give its net as printed. An older server sends no `shown`;
  // the cent fields it does send are the same figures and are printed
  // rounded one by one, and a tile it sends no figure for is left out.
  // older-server path: stands in for `shown` (the year's and the tiles')
  const cells = (y: CashCostYear) => y.shown ?? y;
  const net = r.shown?.paid_ytd ?? r.paid_ytd ?? ytd?.net;
  const vsEarned = r.shown?.vs_earned_ytd ?? r.vs_earned_ytd;
  const earned = r.shown?.earned_ytd ?? r.earned_ytd;
  const tone = (v: number, upIsBad: boolean) =>
    v === 0 ? C.text : (v > 0) === upIsBad ? C.bad : C.good;
  const Kpi = ({ k, v, color, sub }: { k: string; v: string; color: string;
                                        sub: string }) => (
    <View style={{ minWidth: 96 }}>
      <Text style={s.mut}>{k}</Text>
      <Text style={{ color, fontSize: 20, fontWeight: "700",
                     fontVariant: ["tabular-nums"] }}>{v}</Text>
      <Text style={[s.mut, { fontSize: 11 }]}>{sub}</Text>
    </View>
  );
  return (
    <Card>
      <H>Interest &amp; fees paid{" "}
        <Text style={{ color: C.mut, fontSize: 12, fontWeight: "400" }}>
          — what holding money costs you, by year</Text></H>
      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 14,
                     marginTop: 6 }}>
        {net != null && (
          <Kpi k={ytd ? `${ytd.year} so far` : r.by_year[0].year}
               v={signedMoney(net, m$)} color={tone(net, true)}
               sub={ytd
                 ? `${m$(cells(ytd).interest)} interest · ${m$(cells(ytd).fees)} fees`
                   + (cells(ytd).refunds > 0
                      ? ` − ${m$(cells(ytd).refunds)} refunded` : "")
                 : "nothing charged this year"} />)}
        {vsEarned != null && net != null && (
          <Kpi k="vs interest earned on cash"
               v={signedMoney(vsEarned, m$, true)}
               color={tone(vsEarned, false)}
               sub={`earned on cash ${m$(earned)} · ${net < 0 ? `refunded ${m$(-net)}` : `paid ${m$(net)}`} this year`} />
        )}
        <Kpi k="card interest" v={m$(r.interest_ever)}
             color={r.interest_ever > 0 ? C.text : C.good}
             sub={r.interest_ever > 0 ? "ever, across every account"
                                      : "never charged on any account"} />
      </View>
      <View style={[s.tRow, { marginTop: 8, borderTopWidth: 0 }]}>
        <Text style={[s.tHead, { flex: 1, textAlign: "left" }]}>Year</Text>
        <Text style={s.tHead}>Interest</Text>
        <Text style={s.tHead}>Fees</Text>
        <Text style={s.tHead}>Refunds</Text>
        <Text style={s.tHead}>Net</Text>
      </View>
      {r.by_year.map((y) => (
        <View key={y.year}>
          <View style={s.tRow}>
            <Text style={[s.tNum, { flex: 1, textAlign: "left" }]}>{y.year}</Text>
            <Text style={s.tNum}>{cells(y).interest ? m$(cells(y).interest) : "·"}</Text>
            <Text style={s.tNum}>{cells(y).fees ? m$(cells(y).fees) : "·"}</Text>
            <Text style={[s.tNum, { color: C.good }]}>
              {cells(y).refunds ? `−${m$(cells(y).refunds)}` : "·"}</Text>
            <Text style={[s.tNum, { fontWeight: "700" }]}>
              {signedMoney(cells(y).net, m$)}</Text>
          </View>
          {/* the largest charge in the server's words, dated in full */}
          {y.biggest_text ? (
            <Text style={[s.mut, { fontSize: 11, marginTop: -2, marginBottom: 2 }]}>
              biggest: {y.biggest_text}</Text>
          ) : null}
        </View>
      ))}
      {r.latest && (
        <Text style={[s.mut, { marginTop: 6 }]}>
          Latest: <Text style={{ color: C.text }}>{r.latest[4]}</Text>{" "}
          {r.latest[3] === "interest" ? "charged interest of" : `— ${r.latest[0]}`}{" "}
          <Text style={{ color: C.text }}>{money(r.latest[1], true)}</Text>{" "}
          on {mmdd(r.latest[2])} ({monYear(r.latest[2])}). A new charge is
          also in the daily email the day it posts.
        </Text>
      )}
    </Card>
  );
}

/** The archival income cards — SSA career record, tax returns, investment
 *  funding — behind a "Records" toggle, like the web. */
function IncomeRecords({ d, chartW }: { d: CashflowReport; chartW: number }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Pressable onPress={() => setOpen(!open)}
                 {...disclosure(open, "Records — career earnings (SSA) · "
                   + "reported income (tax returns) · investment funding")}>
        <Text style={{ color: C.mut, fontSize: 13, marginHorizontal: 14,
                       marginTop: 10 }}>
          {/* the web's summary words */}
          {open ? "▾" : "▸"} Records — career earnings (SSA) · reported
          income (tax returns) · investment funding
        </Text>
      </Pressable>
      {open && (
        <>
          {d.career_income.length > 0 && (
            <Card>
              <H>
                Career earnings{" "}
                <Text style={[s.mut, { fontWeight: "400" }]}>
                  — SSA record, {d.career_income[0][0]} →
                </Text>
              </H>
              <Sparkline width={chartW} subject="Career earnings, SSA record"
                         data={d.career_income.map(([y, v]) =>
                           [String(y), v] as [string, number])} />
            </Card>
          )}
          {d.reported_income.length > 0 && (
            <Card>
              <H>
                Reported income{" "}
                <Text style={[s.mut, { fontWeight: "400" }]}>
                  — tax returns
                </Text>
              </H>
              <View style={s.tRow}>
                <Text style={[s.tHead, { flex: 1, textAlign: "left" }]}>
                  Year
                </Text>
                <Text style={s.tHead}>Total income</Text>
                <Text style={s.tHead}>Wages</Text>
                <Text style={s.tHead}>Investment</Text>
                <Text style={s.tHead}>Tax paid</Text>
              </View>
              {d.reported_income.map(([y, inc, wages, invest, tax,
                                       joint]) => (
                <View key={y} style={s.tRow}>
                  <Text style={{ color: C.text, fontSize: 12, flex: 1 }}>
                    {y}{joint ? " (joint)" : ""}
                  </Text>
                  <Text style={s.tNum}>{inc ? m$(inc) : "·"}</Text>
                  <Text style={s.tNum}>{wages ? m$(wages) : "·"}</Text>
                  <Text style={s.tNum}>{invest ? m$(invest) : "·"}</Text>
                  <Text style={s.tNum}>{tax ? m$(tax) : "·"}</Text>
                </View>
              ))}
            </Card>
          )}
          {/* after the tax returns and standing when empty, as the web's */}
          <Card>
            <H>Investment funding by year</H>
            {[...d.investment_funding].reverse().map(([y, v]) => (
              <KV key={y} k={String(y)} v={m$(v)} tone="mut" />
            ))}
          </Card>
        </>
      )}
    </>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  center: { color: C.mut, textAlign: "center", padding: 20 },
  mut: { color: C.mut, fontSize: 12, lineHeight: 17 },
  big: { color: C.text, fontSize: 15, fontWeight: "700" },
  tRow: { flexDirection: "row", alignItems: "center", gap: 4,
          borderTopColor: C.border,
          borderTopWidth: StyleSheet.hairlineWidth,
          paddingVertical: 5 },
  tHead: { color: C.mut, fontSize: 10, width: 64, textAlign: "right" },
  tNum: { color: C.text, fontSize: 12, width: 64, textAlign: "right",
          fontVariant: ["tabular-nums"] },
});
