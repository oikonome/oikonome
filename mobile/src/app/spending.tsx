// Spending — one timeframe (the site-wide 3m/6m/1y/3y/5y/all chips), the
// conclusion in a sentence, what CHANGED vs the previous equal window,
// then where it went and to whom, ranked with share and pace. The archival
// tables (spend by year, Amazon, category × year matrix) live behind
// "Records" and are fetched only when opened. Mirrors the web's Spending
// tab (webapp/src/pages/Spending.tsx) — keep them in step.
import { useQuery } from "@tanstack/react-query";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View, useWindowDimensions } from "react-native";
import PullRefresh from "../components/pull-refresh";

import MerchantAvatar from "../components/merchant-avatar";
import Sparkline from "../components/sparkline";
import { EmptyState, FailedLine, NoAccountsLine, StaleLine, useNoAccounts }
  from "../components/loaded";
import { Card, H, KV, Skeleton } from "../components/ui";
import type { Change, FlowKey } from "../lib/api";
import { catLabel, changeLook, EMPTY_LINES, rangeCaption, spendDrillOffered,
         spendDrillParams,
         trendRangeOf, windowLabel, windowSoFar }
  from "../lib/pure";
import { useSession } from "../lib/session";
import { C, money } from "../lib/theme";
import { disclosure } from "../lib/a11y";
import { RangePicker } from "../components/segmented-control";

const m$ = (n: number) => money(n, false);
// What the window is compared against, in the server's words ("vs Oct
// 1–17", "vs the year before"). A server that sends none gets none: a
// caption worded here would be a second definition of the comparison.
const prevText = (w: { prev_text?: string | null }) => w.prev_text ?? "";

/** "+$2,410 (+12%)", colored by whether the move is good — the server's
 *  change (reporting.change; web Delta). An older server sends none and
 *  the change is left out. */
function Delta({ ch, downIsGood }: {
  ch: Change | null | undefined; downIsGood: boolean;
}) {
  const c = changeLook(ch, downIsGood);
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

// `onPress` opens the rows the figure summed (spendDrillParams); `onHistory`
// puts the small "history" link under a merchant's bar — its whole
// history, refunds and holds included (web: the same two links). The link
// is the row's sibling, never inside it: a control inside a labelled
// pressable is one control to a screen reader, and the link would be lost
function BarRow({ label, value, max, right, sub, logo, onPress, onHistory }: {
  label: string; value: number; max: number; right?: string;
  sub?: React.ReactNode; logo?: string | null;
  onPress?: () => void; onHistory?: () => void;
}) {
  return (
    <View style={s.barRow}>
    <Pressable onPress={onPress} disabled={!onPress}
               accessibilityRole={onPress ? "button" : undefined}
               accessibilityLabel={onPress
                 ? `${label}, ${m$(value)}: the charges it counts` : undefined}>
      <View style={s.barMeta}>
        {logo !== undefined && (
          <View style={{ marginRight: 6 }}>
            <MerchantAvatar name={label} logo={logo} size={18} />
          </View>
        )}
        <Text style={s.barLabel} numberOfLines={1}>{label}</Text>
        <View style={{ flexDirection: "row", gap: 8, alignItems: "baseline",
                       marginLeft: "auto" }}>
          {sub}
          <Text style={s.barValue}>{m$(value)}{right ? ` ${right}` : ""}</Text>
        </View>
      </View>
      <View style={s.track}>
        <View style={[s.fill,
          { width: `${Math.max(2, (value / (max || 1)) * 100)}%` }]} />
      </View>
    </Pressable>
    {onHistory && (
      <Text style={{ color: C.accent, fontSize: 11, alignSelf: "flex-start",
                     paddingVertical: 2 }}
            accessibilityRole="link" onPress={onHistory}
            accessibilityLabel={`${label}: every charge filed under this name`}>
        history</Text>
    )}
    </View>
  );
}

export default function Spending() {
  const { client } = useSession();
  const { width } = useWindowDimensions();
  // opened from Cash Flow, the window picked there (trendRangeOf)
  const linked = useLocalSearchParams<{ range?: string }>().range;
  const [range, setRange] = useState<FlowKey>(() => trendRangeOf(linked));
  const [allMerch, setAllMerch] = useState(false);
  const router = useRouter();
  const query = useQuery({
    queryKey: ["report", "spending-window", range],
    queryFn: () => client!.reportSpendingWindow(range),
    enabled: !!client,
    // previous window stays on screen while a wide one computes
    placeholderData: (prev) => prev,
  });
  const w = query.data;
  // a figure opens the Transactions tab on exactly the rows it summed —
  // when the server says it lists them; a server from before the drill
  // would answer some other list, so there the figures are plain text
  const canDrill = spendDrillOffered(w);
  const drill = (what: Parameters<typeof spendDrillParams>[1] = {}) =>
    canDrill ? () => router.push({ pathname: "/(tabs)/transactions",
                                   params: spendDrillParams(range, what) } as never)
      : undefined;
  // a window with nothing in it is one line, not "$0 spent" over empty
  // cards; the default 1y on a ledger that starts this month asks whether
  // this month has spend, so it can say "nothing closed yet" (the web's
  // rule)
  const noAccounts = useNoAccounts();
  const emptyWindow = !!w && !w.total && !(w.by_month ?? []).length
    && !w.live_month;
  const emptyDefault = emptyWindow && range === "1y";
  const curQ = useQuery({
    queryKey: ["report", "spending-window", "cur"],
    queryFn: () => client!.reportSpendingWindow("cur"),
    enabled: !!client && emptyDefault,
  });
  const thisMonth = curQ.data && (curQ.data.total
    || (curQ.data.by_month ?? []).length) ? curQ.data : null;
  // the per-month figure, every share and change and the movers are the
  // SERVER's (spending_window); an older server sends none of them and
  // the screen leaves them out rather than work them out
  const perMonth = w?.per_month;
  // the server follows the ranked categories with ones spent on only in
  // the previous window, at 0: they belong in "what changed", never in a
  // ranking of where this window's money went
  const spent = (w?.categories ?? [])
    .map(([c, v], i) => ({ c, v, sh: w?.category_shares?.[i] ?? null,
                           pm: w?.per_month_by_category?.[i] ?? null,
                           ch: w?.category_changes?.[i] }))
    .filter((r) => r.v > 0);
  const top = spent[0];
  const movers = w?.movers ?? [];
  const cats = spent.slice(0, 15);
  const merchants = ((allMerch ? w?.merchants : w?.merchants.slice(0, 10))
    ?? []).map((m, i) => [m, w?.merchant_changes?.[i]] as const);
  const maxCat = cats[0]?.v || 1;
  const maxMerch = w?.merchants[0]?.[1] || 1;

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => query.refetch()} />}>
      <StaleLine query={query} />
      {query.isPending && <Skeleton />}
      {/* a failed range keeps its card and the range picker */}
      {query.isError && !w && (
        <Card>
          <View style={{ flexDirection: "row", alignItems: "center",
                         flexWrap: "wrap", gap: 6 }}>
            <H>Spending</H>
            <View style={{ marginLeft: "auto" }}>
              <RangePicker value={range} onChange={setRange} />
            </View>
          </View>
          <FailedLine q={query} what={`spending (${rangeCaption(range)})`} />
        </Card>
      )}
      {w && emptyWindow && (
        <Card>
          <View style={{ flexDirection: "row", alignItems: "center",
                         flexWrap: "wrap", gap: 6 }}>
            <H>Spending</H>
            <View style={{ marginLeft: "auto" }}>
              <RangePicker value={range} onChange={setRange} />
            </View>
          </View>
          {emptyDefault && curQ.isPending ? <Skeleton />
            : emptyDefault && curQ.data === undefined
              ? <FailedLine q={curQ} what="this month's spending" />
            : emptyDefault && thisMonth ? (
              <Text style={[s.mut, { marginTop: 8 }]}>
                {w.label} holds complete months only, and your ledger
                starts this month — {m$(thisMonth.total)} spent so far.{" "}
                <Text style={{ color: C.accent, fontWeight: "600" }}
                      accessibilityRole="button"
                      onPress={() => setRange("cur")}>See this month →</Text>
              </Text>)
            : noAccounts ? <NoAccountsLine />
            : <EmptyState title={EMPTY_LINES.spendingRange} />}
        </Card>
      )}
      {w && !emptyWindow && (
        <Card>
          <View style={{ flexDirection: "row", alignItems: "center",
                         flexWrap: "wrap", gap: 6 }}>
            <H>Spending</H>
            <View style={{ marginLeft: "auto" }}>
              <RangePicker value={range} onChange={setRange} />
            </View>
          </View>
          <Text style={[s.mut, { marginTop: 8 }]}>
            {/* the month so far: no per-month pace, which would only
                repeat the total (as on the web) */}
            {windowLabel(w)}:{" "}
            <Text style={s.big}
                  accessibilityRole={canDrill ? "link" : undefined}
                  onPress={drill()}>{m$(w.total)}</Text> spent
            {!windowSoFar(w) && perMonth != null ? <>{" "}·{" "}
              <Text style={{ color: C.text, fontWeight: "600" }}>
                {m$(perMonth)}</Text>/mo
              {/* the server's words for what "/mo" divides by, as the
                  web prints them; an older server sends none */}
              {w.per_month_basis ? ` ${w.per_month_basis}` : ""}</> : null}
            {top ? <> · biggest{" "}
              <Text style={{ color: C.text, fontWeight: "600" }}>
                {catLabel(top.c)}</Text>{" "}
              ({m$(top.v)}{top.sh !== null ? ` · ${top.sh}%` : ""})</> : null}
          </Text>
          {w.dating === "paid" && (
            <Text style={[s.mut, { marginTop: 2, fontSize: 11 }]}>
              dated by the day each charge was paid — the Month and Year
              lenses count a bill in the month it belongs to
            </Text>
          )}
          {w.change && (
            <Text style={[s.mut, { marginTop: 2 }]}>
              <Delta ch={w.change} downIsGood />{" "}
              {prevText(w)}
            </Text>
          )}
          {w.by_month.length >= 4 && (
            // the window's monthly pace as a line, like the web
            <View style={{ marginTop: 8 }}>
              <Sparkline width={width - 24 - 28} height={140}
                         data={w.by_month} subject="Spending by month" />
            </View>
          )}
        </Card>
      )}

      {movers.length > 0 && w && (
        <Card>
          <H>What changed{" "}
            <Text style={{ color: C.mut, fontSize: 12, fontWeight: "400" }}>
              — {prevText(w)}</Text></H>
          {movers.map((m) => {
            const up = m.change.amount > 0;
            const sign = up ? "+" : "−";
            return (
              // a category opens its charges this window — the window
              // before when it fell to nothing; a merchant here was paid
              // only before, so it opens the window before (web same)
              <Text key={`${m.kind}:${m.label}`}
                    style={{ fontSize: 13, marginVertical: 2 }}
                    accessibilityRole={canDrill ? "link" : undefined}
                    onPress={drill(m.kind === "category"
                      ? { cat: m.label, previous: m.current === 0 }
                      : { merchant: m.label, previous: true })}>
                <Text style={{ color: up ? C.bad : C.good,
                               fontWeight: "700" }}>
                  {up ? "▲" : "▼"}{" "}
                  {m.kind === "category" ? catLabel(m.label) : m.label}{" "}
                  {sign}{m$(Math.abs(m.change.amount))}
                </Text>
                <Text style={{ color: C.mut }}>
                  {"  "}{m$(m.previous)} → {m$(m.current)}
                  {m.change.pct !== null
                    ? ` · ${sign}${Math.abs(m.change.pct)}%` : ""}
                </Text>
              </Text>
            );
          })}
        </Card>
      )}

      {cats.length > 0 && w && (
        <Card>
          <H>Where it went{" "}
            <Text style={{ color: C.mut, fontSize: 12, fontWeight: "400" }}>
              — share of the window&apos;s spend</Text></H>
          {cats.map(({ c, v, sh, pm, ch }) => (
            // the server's share and "/mo" per row, as the web prints them
            <BarRow key={c} label={catLabel(c)} value={v} max={maxCat}
                    onPress={drill({ cat: c })}
                    right={(sh !== null ? `· ${sh}%` : "")
                      + (pm !== null ? ` · ${m$(pm)}/mo` : "")}
                    sub={<Delta ch={ch} downIsGood />} />
          ))}
        </Card>
      )}

      {merchants.length > 0 && w && (
        <Card>
          <H>Top merchants</H>
          {merchants.map(([[name, v, n, , logo], ch]) => (
            <BarRow key={name} label={name} value={v} max={maxMerch}
                    // a row with no name names no merchant the server can
                    // find again, so it opens nothing (web same)
                    onPress={name ? drill({ merchant: name }) : undefined}
                    onHistory={name ? () => router.push({
                      pathname: "/bill-history",
                      params: { payee: name, window: range } } as never)
                      : undefined}
                    right={`· ${n}×`} logo={logo ?? null}
                    sub={<Delta ch={ch} downIsGood />} />
          ))}
          {(w.merchants.length > 10) && (
            <Pressable accessibilityRole="button" onPress={() => setAllMerch(!allMerch)}>
              <Text style={{ color: C.accent, fontSize: 12, marginTop: 6 }}>
                {allMerch ? "top 10 only" : `all ${w.merchants.length} →`}
              </Text>
            </Pressable>
          )}
        </Card>
      )}

      <Records />
    </ScrollView>
  );
}

/** The archival tables — unchanged content, collapsed; the full spending
 *  report is only fetched once this is opened. */
function Records() {
  const { client } = useSession();
  const [open, setOpen] = useState(false);
  const q = useQuery({
    queryKey: ["report", "spending"],
    queryFn: () => client!.reportSpending(),
    enabled: !!client && open,
  });
  const d = q.data;
  const years = [...(d?.by_year ?? [])].reverse();
  const maxYear = Math.max(...years.map(([, v]) => v), 1);
  return (
    <>
      <Pressable onPress={() => setOpen(!open)}
                 {...disclosure(open, "Records — spend by year · category × "
                   + "year matrix · Amazon by category")}>
        <Text style={{ color: C.mut, fontSize: 13, marginHorizontal: 14,
                       marginTop: 10 }}>
          {/* the web's summary words */}
          {open ? "▾" : "▸"} Records — spend by year · category × year
          matrix · Amazon by category
        </Text>
      </Pressable>
      {open && q.isPending && <Text style={s.center}>Loading…</Text>}
      {/* a failed read says so, with a retry, rather than an empty
          disclosure (the web's line) */}
      {open && !d && q.isError && (
        <FailedLine q={q} what="the spending records" />)}
      {open && d && (
        <>
          {years.length > 0 && (
            <Card>
              <H>Spend by year</H>
              {years.map(([y, v, n]) => (
                // the web's Txns column, named
                <BarRow key={y} label={y} value={v} max={maxYear}
                        right={`· ${n} txns`} />
              ))}
            </Card>
          )}
          <Card>
            <H>Amazon by category</H>
            {d.amazon.map(([cat, amt, n]) => (
              <KV key={cat} k={cat} v={`${m$(amt)} · ${n} txns`} tone="mut" />
            ))}
            {d.amazon.length === 0 && (
              <Text style={{ color: C.mut, fontSize: 12 }}>
                No Amazon-categorized spend.
              </Text>
            )}
          </Card>
          {d.yoy_years?.length > 0 && (
            <Card>
              <H>Category × year (YoY)</H>
              <ScrollView horizontal showsHorizontalScrollIndicator={false}>
                <View>
                  <View style={{ flexDirection: "row" }}>
                    <Text style={[s2.cell, s2.head, { width: 150 }]}> </Text>
                    {[...d.yoy_years].reverse().map((y) => (
                      <Text key={y} style={[s2.cell, s2.head]}>{y}</Text>
                    ))}
                  </View>
                  {d.yoy_matrix.map((row, i) => (
                    <View key={i} style={{ flexDirection: "row" }}>
                      <Text style={[s2.cell, { width: 150, textAlign: "left" }]}
                            numberOfLines={1}>
                        {String(row[0])}
                      </Text>
                      {row.slice(1).reverse().map((v, j) => (
                        <Text key={j} style={s2.cell}>
                          {typeof v === "number" && v ? m$(v) : "·"}
                        </Text>
                      ))}
                    </View>
                  ))}
                </View>
              </ScrollView>
            </Card>
          )}
        </>
      )}
    </>
  );
}

const s2 = StyleSheet.create({
  cell: { color: C.mut, fontSize: 11, width: 76, textAlign: "right",
          paddingVertical: 3, fontVariant: ["tabular-nums"] },
  head: { color: C.text, fontWeight: "600" },
});

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  center: { color: C.mut, textAlign: "center", padding: 20 },
  mut: { color: C.mut, fontSize: 13, lineHeight: 19 },
  big: { color: C.text, fontSize: 16, fontWeight: "700" },
  barRow: { marginVertical: 4 },
  barMeta: { flexDirection: "row", justifyContent: "space-between", gap: 8 },
  barLabel: { color: C.text, fontSize: 13, flexShrink: 1 },
  barValue: { color: C.mut, fontSize: 13, fontVariant: ["tabular-nums"] },
  track: { height: 5, borderRadius: 3, backgroundColor: C.hover,
           marginTop: 3, overflow: "hidden" },
  fill: { height: 5, borderRadius: 3, backgroundColor: C.accent },
});
