// Spending — one timeframe (the site-wide 3m/6m/1y/3y/5y/all chips), the
// conclusion in a sentence, what CHANGED vs the previous equal window,
// then where it went and to whom, ranked with share and pace. The archival
// tables (spend by year, Amazon, category × year matrix) live behind
// "Records" and are fetched only when opened. Mirrors the web's Spending
// tab (webapp/src/pages/Spending.tsx) — keep them in step.
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View, useWindowDimensions } from "react-native";
import PullRefresh from "../components/pull-refresh";

import MerchantAvatar from "../components/merchant-avatar";
import Sparkline from "../components/sparkline";
import StaleBanner from "../components/stale-banner";
import { Card, H, KV } from "../components/ui";
import type { FlowKey } from "../lib/api";
import { TREND_RANGES, catLabel, RANGE_LABEL } from "../lib/pure";
import { useSession } from "../lib/session";
import { C, money } from "../lib/theme";

const m$ = (n: number) => money(n, false);
// how the story names the window being compared against (web PREV_NAME)
const PREV_NAME: Record<FlowKey, string> = {
  cur: "month before", "1m": "month before that",
  "3m": "3 months before", "6m": "6 months before", "1y": "year before",
  "3y": "3 years before", "5y": "5 years before", all: "",
};

/** "+$2,410 (+12%)", colored by whether the move is good. */
function Delta({ cur, prev, downIsGood }: {
  cur: number; prev: number | null; downIsGood: boolean;
}) {
  if (prev === null) return null;
  const d = cur - prev;
  if (Math.abs(d) < 0.005)
    return <Text style={{ color: C.mut, fontSize: 12 }}>unchanged</Text>;
  const good = downIsGood ? d < 0 : d > 0;
  const sign = d >= 0 ? "+" : "−";
  const p = prev ? Math.round((100 * d) / prev) : null;
  return (
    <Text style={{ color: good ? C.good : C.bad, fontSize: 12,
                   fontWeight: "600" }}>
      {sign}{m$(Math.abs(d))}{p !== null && Math.abs(p) < 1000
        ? ` (${sign}${Math.abs(p)}%)` : ""}
    </Text>
  );
}

function RangeChips({ value, onChange }: {
  value: FlowKey; onChange: (r: FlowKey) => void;
}) {
  return (
    <View style={{ flexDirection: "row", gap: 4, flexWrap: "wrap" }}>
      {TREND_RANGES.map((r) => (
        <Pressable key={r} style={[s.chip, value === r && s.chipOn]}
                   onPress={() => onChange(r)}>
          <Text style={{ color: value === r ? C.text : C.mut, fontSize: 11 }}>
            {RANGE_LABEL[r]}</Text>
        </Pressable>
      ))}
    </View>
  );
}

function BarRow({ label, value, max, right, sub, logo }: {
  label: string; value: number; max: number; right?: string;
  sub?: React.ReactNode; logo?: string | null;
}) {
  return (
    <View style={s.barRow}>
      <View style={s.barMeta}>
        {logo !== undefined && (
          <View style={{ marginRight: 6 }}>
            <MerchantAvatar name={label} logo={logo} size={18} />
          </View>
        )}
        <Text style={s.barLabel} numberOfLines={1}>{label}</Text>
        <View style={{ flexDirection: "row", gap: 8, alignItems: "baseline" }}>
          {sub}
          <Text style={s.barValue}>{m$(value)}{right ? ` ${right}` : ""}</Text>
        </View>
      </View>
      <View style={s.track}>
        <View style={[s.fill,
          { width: `${Math.max(2, (value / (max || 1)) * 100)}%` }]} />
      </View>
    </View>
  );
}

export default function Spending() {
  const { client } = useSession();
  const { width } = useWindowDimensions();
  const [range, setRange] = useState<FlowKey>("1y");
  const [allMerch, setAllMerch] = useState(false);
  const query = useQuery({
    queryKey: ["report", "spending-window", range],
    queryFn: () => client!.reportSpendingWindow(range),
    enabled: !!client,
    // previous window stays on screen while a wide one computes
    placeholderData: (prev) => prev,
  });
  const w = query.data;
  const perMonth = w ? w.total / Math.max(w.months, 1) : 0;
  const top = w?.categories[0];
  const movers = !w || w.prev_total === null ? [] :
    w.categories
      .map(([c, v, p]) => ({ c, v, p: p ?? 0, d: v - (p ?? 0) }))
      .filter((m) => Math.abs(m.d) >= Math.max(25, w.total * 0.005))
      .sort((a, b) => Math.abs(b.d) - Math.abs(a.d))
      .slice(0, 4);
  const cats = w?.categories.slice(0, 15) ?? [];
  const merchants = (allMerch ? w?.merchants : w?.merchants.slice(0, 10)) ?? [];
  const maxCat = cats[0]?.[1] || 1;
  const maxMerch = w?.merchants[0]?.[1] || 1;

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => query.refetch()} />}>
      <StaleBanner query={query} />
      {query.isPending && <Text style={s.center}>Loading…</Text>}
      {query.isError && !w && (
        <Text style={s.center}>Couldn&apos;t reach the server.</Text>
      )}
      {w && (
        <Card>
          <View style={{ flexDirection: "row", alignItems: "center",
                         flexWrap: "wrap", gap: 6 }}>
            <H>Spending</H>
            <View style={{ marginLeft: "auto" }}>
              <RangeChips value={range} onChange={setRange} />
            </View>
          </View>
          <Text style={[s.mut, { marginTop: 8 }]}>
            {w.label}: <Text style={s.big}>{m$(w.total)}</Text> spent ·{" "}
            <Text style={{ color: C.text, fontWeight: "600" }}>
              {m$(perMonth)}</Text>/mo
            {top ? <> · biggest{" "}
              <Text style={{ color: C.text, fontWeight: "600" }}>
                {catLabel(top[0])}</Text>{" "}
              ({m$(top[1])}{w.total
                ? ` · ${Math.round((100 * top[1]) / w.total)}%` : ""})</> : null}
          </Text>
          {w.prev_total !== null && (
            <Text style={[s.mut, { marginTop: 2 }]}>
              <Delta cur={w.total} prev={w.prev_total} downIsGood />{" "}
              vs the {PREV_NAME[range]}
            </Text>
          )}
          {w.by_month.length >= 4 && (
            // the window's monthly pace as a line, like the web
            <View style={{ marginTop: 8 }}>
              <Sparkline width={width - 24 - 28} height={140}
                         data={w.by_month} />
            </View>
          )}
        </Card>
      )}

      {movers.length > 0 && w && (
        <Card>
          <H>What changed{" "}
            <Text style={{ color: C.mut, fontSize: 12, fontWeight: "400" }}>
              — vs the {PREV_NAME[range]}</Text></H>
          {movers.map((m) => (
            <Text key={m.c} style={{ fontSize: 13, marginVertical: 2 }}>
              <Text style={{ color: m.d > 0 ? C.bad : C.good,
                             fontWeight: "700" }}>
                {m.d > 0 ? "▲" : "▼"} {catLabel(m.c)}{" "}
                {m.d > 0 ? "+" : "−"}{m$(Math.abs(m.d))}
              </Text>
              <Text style={{ color: C.mut }}>
                {"  "}{m$(m.p)} → {m$(m.v)}
                {m.p ? ` · ${m.d > 0 ? "+" : "−"}${Math.abs(Math.round((100 * m.d) / m.p))}%` : ""}
              </Text>
            </Text>
          ))}
        </Card>
      )}

      {cats.length > 0 && w && (
        <Card>
          <H>Where it went{" "}
            <Text style={{ color: C.mut, fontSize: 12, fontWeight: "400" }}>
              — share of the window&apos;s spend</Text></H>
          {cats.map(([c, v, p]) => (
            <BarRow key={c} label={catLabel(c)} value={v} max={maxCat}
                    right={w.total
                      ? `· ${Math.round((100 * v) / w.total)}%` : ""}
                    sub={<Delta cur={v} prev={p} downIsGood />} />
          ))}
        </Card>
      )}

      {merchants.length > 0 && w && (
        <Card>
          <H>Top merchants</H>
          {merchants.map(([name, v, n, p, logo]) => (
            <BarRow key={name} label={name} value={v} max={maxMerch}
                    right={`· ${n}×`} logo={logo ?? null}
                    sub={<Delta cur={v} prev={p} downIsGood />} />
          ))}
          {(w.merchants.length > 10) && (
            <Pressable onPress={() => setAllMerch(!allMerch)}>
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
      <Pressable onPress={() => setOpen(!open)}>
        <Text style={{ color: C.mut, fontSize: 13, marginHorizontal: 14,
                       marginTop: 10 }}>
          {open ? "▾" : "▸"} Records — spend by year · Amazon ·
          category × year matrix
        </Text>
      </Pressable>
      {open && q.isPending && <Text style={s.center}>Loading…</Text>}
      {open && d && (
        <>
          {years.length > 0 && (
            <Card>
              <H>Spend by year</H>
              {years.map(([y, v, n]) => (
                <BarRow key={y} label={y} value={v} max={maxYear}
                        right={`· ${n}`} />
              ))}
            </Card>
          )}
          <Card>
            <H>Amazon by category</H>
            {d.amazon.map(([cat, amt, n]) => (
              <KV key={cat} k={cat} v={`${m$(amt)} · ${n}`} tone="mut" />
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
  chip: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
          borderRadius: 999, paddingHorizontal: 10, paddingVertical: 4 },
  chipOn: { borderColor: C.accent, backgroundColor: C.hover },
  barRow: { marginVertical: 4 },
  barMeta: { flexDirection: "row", justifyContent: "space-between", gap: 8 },
  barLabel: { color: C.text, fontSize: 13, flexShrink: 1 },
  barValue: { color: C.mut, fontSize: 13, fontVariant: ["tabular-nums"] },
  track: { height: 5, borderRadius: 3, backgroundColor: C.hover,
           marginTop: 3, overflow: "hidden" },
  fill: { height: 5, borderRadius: 3, backgroundColor: C.accent },
});
