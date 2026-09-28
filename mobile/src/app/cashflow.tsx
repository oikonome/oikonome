// Cash flow — the web page's three tabs: Overview (the flow picture for
// a period, the conclusion in a sentence, saved-by-month with the amount
// on every bar, savings-by-year with per-year drill-down), Income (income
// by month, investment funding, SSA career earnings, reported-income tax
// table). Spending is its own screen; the tab links to it. Mirrors
// webapp/src/pages/CashFlow.tsx — keep them in step.
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { useRouter } from "expo-router";
import { useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View, useWindowDimensions } from "react-native";
import PullRefresh from "../components/pull-refresh";

import FlowChart from "../components/flow-chart";
import MerchantAvatar from "../components/merchant-avatar";
import NetBars from "../components/net-bars";
import Sparkline from "../components/sparkline";
import StaleBanner from "../components/stale-banner";
import { Card, H, HelpLink, KV } from "../components/ui";
import type { CashCostsReport, CashflowReport, FlowKey, IncomeWindow }
  from "../lib/api";
import { TREND_RANGES, cashflowCutoffMonth, RANGE_LABEL, RANGE_MONTHS,
         type TrendRange }
  from "../lib/pure";
import { useSession } from "../lib/session";
import { C, money } from "../lib/theme";

const m$ = (n: number) => money(n, false);
// both pickers speak the site-wide 3m/6m/1y/3y/5y/all vocabulary (pure.ts
// TREND_RANGES ↔ webapp components/RangePicker — keep them in step)
const MONTHS3 = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul",
                 "Aug", "Sep", "Oct", "Nov", "Dec"];
const pretty = (v: string) =>
  MONTHS3[Number(String(v).slice(5, 7))] || String(v);

/** The saved-per-month series for a range — the web's savedSeries. */
function savedSeries(cf: CashflowReport, range: TrendRange):
    { points: [string, number][]; estimatedFrom: number } {
  const graph = cf.cash_graph;
  const months = RANGE_MONTHS[range];
  // Cut on the household's month, which the report itself names (its
  // first forecast point) — the device's clock is in a different month
  // for hours around every boundary, and every server figure on this
  // screen is windowed from the household's date.
  const cutoff = cashflowCutoffMonth(graph, range) ?? "";
  const history = graph.points.filter((p, i) => i < graph.forecast_from
                                      && p[0] >= cutoff);
  const tail = graph.points.slice(graph.forecast_from);
  const all = [...history, ...tail];
  const label = (key: string, i: number) => {
    const m = pretty(key);
    if (months !== null && months <= 12) return m;
    const jan = key.slice(5, 7) === "01" || i === 0;
    return jan ? `${m} ${key.slice(2, 4)}'` : m;
  };
  return { points: all.map((p, i) => [label(p[0], i), p[1]]),
           estimatedFrom: history.length };
}

export default function CashFlow() {
  const { client } = useSession();
  const { width } = useWindowDimensions();
  const router = useRouter();
  const [tab, setTab] = useState<"overview" | "income">("overview");
  // ONE timeframe for the whole screen — the flow picture, saved-by-month
  // and the Income tab all follow it (the web page does the same via the
  // URL), so switching tabs never resets a picked range
  const [range, setRange] = useState<TrendRange>("1y");
  const period: FlowKey = range;
  const [open, setOpen] = useState<Record<string, boolean>>({});
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
  const hist = series ? series.points.slice(0, series.estimatedFrom) : [];
  const best = hist.length ? hist.reduce((a, b) => (b[1] > a[1] ? b : a)) : null;
  const worst = hist.length ? hist.reduce((a, b) => (b[1] < a[1] ? b : a)) : null;
  const savings = [...(d?.savings_by_year ?? [])].reverse();
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
      <StaleBanner query={query} />
      {query.isPending && <Text style={s.center}>Loading…</Text>}
      {query.isError && !d && (
        <Text style={s.center}>Couldn&apos;t reach the server.</Text>
      )}
      {d && (
        <Card>
          <View style={{ flexDirection: "row", gap: 6, flexWrap: "wrap" }}>
            {([["overview", "Overview"], ["spending", "Spending"],
               ["income", "Income"]] as const).map(([k, l]) => (
              <Pressable key={k}
                         style={[s.chip, tab === k && s.chipOn]}
                         onPress={() => k === "spending"
                           ? router.push("/spending" as never)
                           : setTab(k)}>
                <Text style={{ color: tab === k ? C.text : C.mut,
                               fontSize: 12 }}>{l}</Text>
              </Pressable>
            ))}
          </View>
          {tab === "overview" && (
            <>
              <View style={{ flexDirection: "row", gap: 4, marginTop: 8,
                             flexWrap: "wrap" }}>
                {TREND_RANGES.map((r) => (
                  <Pressable key={r}
                             style={[s.chip, range === r && s.chipOn]}
                             onPress={() => setRange(r)}>
                    <Text style={{ color: range === r ? C.text : C.mut,
                                   fontSize: 11 }}>
                      {RANGE_LABEL[r]}</Text>
                  </Pressable>
                ))}
              </View>
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
                  {(flow.in.total > 0 || flow.out.total > 0) && (
                    <Text style={[s.mut, { marginTop: 6 }]}>
                      {flow.label}:{" "}
                      <Text style={s.big}>{m$(flow.in.total)}</Text> in →{" "}
                      <Text style={s.big}>{m$(flow.out.total)}</Text> out →{" "}
                      <Text style={[s.big, { color: flow.saved >= 0 ? C.good : C.bad }]}>
                        {flow.saved >= 0 ? "+" : ""}{m$(flow.saved)}
                      </Text>{" "}{flow.saved >= 0 ? "stayed" : "overspent"}
                      {flow.rate !== null && flow.rate >= 0
                        ? ` · ${flow.rate}% of income` : ""}
                    </Text>
                  )}
                </View>
              )}
              {flowQ.isError && !flow && (
                <Text style={s.center}>
                  Couldn&apos;t load the flow picture — pull to retry.
                </Text>
              )}
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
                <H>Saved, by month</H>
              </View>
              <NetBars width={chartW} points={series.points}
                       estimatedFrom={series.estimatedFrom} />
              <Text style={s.mut}>
                {best ? <>best {best[0]}{" "}
                  <Text style={{ color: C.good, fontWeight: "700" }}>
                    +{m$(best[1])}</Text></> : null}
                {best && worst ? " · " : ""}
                {worst ? <>worst {worst[0]}{" "}
                  <Text style={{ color: worst[1] < 0 ? C.bad : C.text,
                                 fontWeight: "700" }}>{m$(worst[1])}</Text></> : null}
                {series.points.length > series.estimatedFrom
                  ? " · hollow bars are the 60-day forecast" : ""}
              </Text>
            </Card>
          )}
          <Card>
            <H>Savings by year</H>
            <View style={s.tRow}>
              <Text style={[s.tHead, { flex: 1, textAlign: "left" }]}>
                Year
              </Text>
              <Text style={s.tHead}>In</Text>
              <Text style={s.tHead}>Out</Text>
              <Text style={s.tHead}>Saved</Text>
              <Text style={[s.tHead, { width: 44 }]}>Rate</Text>
            </View>
            {savings.map(([y, inc, sp, net, rt]) => (
              <View key={y}>
                <Pressable style={s.tRow}
                           onPress={() => d.monthly_by_year[y]
                             && setOpen((o) =>
                               ({ ...o, [y]: !o[y] }))}>
                  <Text style={{ color: C.text, fontSize: 13, flex: 1 }}>
                    {d.monthly_by_year[y]
                      ? (open[y] ? "▾ " : "▸ ") : ""}{y}
                  </Text>
                  <Text style={s.tNum}>
                    {inc !== null ? m$(inc) : "no data"}
                  </Text>
                  <Text style={s.tNum}>{m$(sp)}</Text>
                  <Text style={[s.tNum, net !== null
                    && { color: net >= 0 ? C.good : C.bad }]}>
                    {net !== null ? m$(net) : "no data"}
                  </Text>
                  <Text style={[s.tNum, { width: 44 },
                                rt !== null && rt < 0
                                  && { color: C.bad }]}>
                    {rt !== null ? `${rt}%` : "·"}
                  </Text>
                </Pressable>
                {open[y] && d.monthly_by_year[y] && (
                  <View style={{ paddingVertical: 6 }}>
                    <Text style={s.mut}>Saved, by month — {y}</Text>
                    <NetBars width={chartW} points={d.monthly_by_year[y]}
                             height={150} />
                  </View>
                )}
              </View>
            ))}
          </Card>
        </>
      )}

      {tab === "income" && (
        <IncomeTab chartW={chartW} range={range} setRange={setRange}
                   iq={iq} />
      )}
      {tab === "income" && ccq.data && <CashCosts r={ccq.data} />}
      {d && tab === "income" && (
        <IncomeRecords d={d} chartW={chartW} />
      )}
      <HelpLink topic="cash-flow" />
    </ScrollView>
  );
}

// the flow picture's income palette — kinds keep one color everywhere
// (webapp KIND_COLORS twin)
const KIND_COLORS = { paychecks: "#4f9bd6", interest: "#7fb8e6",
                      other: "#3d7fb0" } as const;
const KIND_LABELS = { paychecks: "Paychecks", interest: "Interest & dividends",
                      other: "Other" } as const;

/** "+$420 (+5%)", colored — income going up is good. */
function IncDelta({ cur, prev }: { cur: number; prev: number | null }) {
  if (prev === null) return null;
  const d = cur - prev;
  if (Math.abs(d) < 0.005) return null;
  const sign = d >= 0 ? "+" : "−";
  const p = prev ? Math.round((100 * d) / prev) : null;
  return (
    <Text style={{ color: d > 0 ? C.good : C.bad, fontSize: 12,
                   fontWeight: "600" }}>
      {sign}{m$(Math.abs(d))}{p !== null && Math.abs(p) < 1000
        ? ` (${sign}${Math.abs(p)}%)` : ""}
    </Text>
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
  if (iq.isError && !w) {
    return (
      <Text style={s.center}>Couldn&apos;t load income — pull to retry.</Text>
    );
  }
  if (!w) return null;
  // the pace divisor is the SERVER's month count for the window, not a
  // client guess from the range key: "1m" covers one complete month
  // while its chart reaches back two, and only the server knows how far
  // the ledger actually goes (webapp/src/pages/CashFlow.tsx does the same)
  const perMonth = w.total / Math.max(w.months, 1);
  const payShare = w.total ? Math.round((100 * w.kinds.paychecks) / w.total) : 0;
  const kinds = (Object.keys(KIND_COLORS) as (keyof typeof KIND_COLORS)[])
    .map((k) => ({ k, v: w.kinds[k] })).filter((x) => x.v > 0);
  const maxSrc = w.sources[0]?.[1] || 1;
  return (
    <>
      <Card>
        <View style={{ flexDirection: "row", alignItems: "center",
                       flexWrap: "wrap", gap: 6 }}>
          <H>Income</H>
          <View style={{ flexDirection: "row", gap: 4, marginLeft: "auto",
                         flexWrap: "wrap" }}>
            {TREND_RANGES.map((r) => (
              <Pressable key={r} style={[s.chip, range === r && s.chipOn]}
                         onPress={() => setRange(r)}>
                <Text style={{ color: range === r ? C.text : C.mut,
                               fontSize: 11 }}>
                  {RANGE_LABEL[r]}</Text>
              </Pressable>
            ))}
          </View>
        </View>
        <Text style={[s.mut, { marginTop: 8 }]}>
          {w.label}: <Text style={s.big}>{m$(w.total)}</Text> came in ·{" "}
          <Text style={{ color: C.text, fontWeight: "600" }}>
            {m$(perMonth)}</Text>/mo ·{" "}
          <Text style={{ color: C.text, fontWeight: "600" }}>
            {payShare}%</Text> paychecks
        </Text>
        {w.prev_total !== null && (
          <Text style={[s.mut, { marginTop: 2 }]}>
            <IncDelta cur={w.total} prev={w.prev_total} />{" "}
            vs the {range === "1y" ? "year before"
              : range === "cur" ? "month before"
              : range === "1m" ? "month before that"
              : range === "3m" ? "3 months before"
              : range === "6m" ? "6 months before"
              : range === "3y" ? "3 years before" : "5 years before"}
          </Text>
        )}
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
                  {KIND_LABELS[k]} {m$(v)}
                  {w.prev_kinds
                    ? <> <IncDelta cur={v} prev={w.prev_kinds[k]} /></> : null}
                </Text>
              ))}
            </Text>
          </>
        )}
        {w.by_month.length >= 4 && (
          // the same pace line the Spending screen opens with
          <View style={{ marginTop: 8 }}>
            <Sparkline width={chartW} height={140} data={w.by_month} />
          </View>
        )}
      </Card>
      {w.sources.length > 0 && (
        <Card>
          <H>Income by source{" "}
            <Text style={{ color: C.mut, fontSize: 12, fontWeight: "400" }}>
              — who paid, over the window</Text></H>
          {w.sources.map(([payee, amt, n, p, logo]) => (
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
                  <IncDelta cur={amt} prev={p} />
                  <Text style={{ color: C.mut, fontSize: 13,
                                 fontVariant: ["tabular-nums"] }}>
                    {m$(amt)} · {n}×</Text>
                </View>
              </View>
              <View style={{ height: 5, borderRadius: 3,
                             backgroundColor: C.hover, marginTop: 3,
                             overflow: "hidden" }}>
                <View style={{ height: 5, borderRadius: 3,
                               backgroundColor: "#4f9bd6",
                               width: `${Math.max(2, (100 * amt) / maxSrc)}%` }} />
              </View>
            </View>
          ))}
        </Card>
      )}
    </>
  );
}

const mmdd = (iso: string) => `${iso.slice(5, 7)}/${iso.slice(8, 10)}`;

/** Interest and fees paid — what holding money and carrying a balance cost,
 *  by year, net of refunds, beside what the same banks paid in interest.
 *  Under income by source because interest EARNED is a row up there; this
 *  is the other side of it. Hidden when nothing was ever charged. Mirrors
 *  the web's CashCosts. */
function CashCosts({ r }: { r: CashCostsReport }) {
  if (r.by_year.length === 0) return null;
  const ytd = r.ytd;
  const net = ytd ? ytd.net : 0;
  const vsEarned = r.earned_ytd - net;
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
        <Kpi k={ytd ? `${ytd.year} so far` : r.by_year[0].year}
             v={m$(net)} color={tone(net, true)}
             sub={ytd
               ? `${m$(ytd.interest)} interest · ${m$(ytd.fees)} fees`
                 + (ytd.refunds > 0 ? ` − ${m$(ytd.refunds)} refunded` : "")
               : "nothing charged this year"} />
        <Kpi k="vs interest earned"
             v={`${vsEarned > 0 ? "+" : ""}${m$(vsEarned)}`}
             color={tone(vsEarned, false)}
             sub={`earned ${m$(r.earned_ytd)} · paid ${m$(net)} this year`} />
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
            <Text style={s.tNum}>{y.interest ? m$(y.interest) : "·"}</Text>
            <Text style={s.tNum}>{y.fees ? m$(y.fees) : "·"}</Text>
            <Text style={[s.tNum, { color: C.good }]}>
              {y.refunds ? `−${m$(y.refunds)}` : "·"}</Text>
            <Text style={[s.tNum, { fontWeight: "700" }]}>{m$(y.net)}</Text>
          </View>
          {y.biggest && (
            <Text style={[s.mut, { fontSize: 11, marginTop: -2, marginBottom: 2 }]}>
              biggest: {y.biggest[0]} {m$(y.biggest[1])} · {mmdd(y.biggest[2])}</Text>
          )}
        </View>
      ))}
      {r.latest && (
        <Text style={[s.mut, { marginTop: 6 }]}>
          Latest: <Text style={{ color: C.text }}>{r.latest[4]}</Text>{" "}
          {r.latest[3] === "interest" ? "charged interest of" : `— ${r.latest[0]}`}{" "}
          <Text style={{ color: C.text }}>{money(r.latest[1], true)}</Text>{" "}
          on {mmdd(r.latest[2])}. A new charge is also in the daily email the
          day it posts.
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
      <Pressable onPress={() => setOpen(!open)}>
        <Text style={{ color: C.mut, fontSize: 13, marginHorizontal: 14,
                       marginTop: 10 }}>
          {open ? "▾" : "▸"} Records — career earnings (SSA) · tax returns ·
          investment funding
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
              <Sparkline width={chartW}
                         data={d.career_income.map(([y, v]) =>
                           [String(y), v] as [string, number])} />
            </Card>
          )}
          {d.investment_funding.length > 0 && (
            <Card>
              <H>Investment funding by year</H>
              {[...d.investment_funding].reverse().map(([y, v]) => (
                <KV key={y} k={String(y)} v={m$(v)} tone="mut" />
              ))}
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
                <Text style={s.tHead}>Income</Text>
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
  chip: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
          borderRadius: 999, paddingHorizontal: 10, paddingVertical: 4 },
  chipOn: { borderColor: C.accent, backgroundColor: C.hover },
  tRow: { flexDirection: "row", alignItems: "center", gap: 4,
          borderTopColor: C.border,
          borderTopWidth: StyleSheet.hairlineWidth,
          paddingVertical: 5 },
  tHead: { color: C.mut, fontSize: 10, width: 64, textAlign: "right" },
  tNum: { color: C.text, fontSize: 12, width: 64, textAlign: "right",
          fontVariant: ["tabular-nums"] },
});
