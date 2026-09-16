// Year lens — twelve verdict cells, the year totals against last year,
// and the category ranking. Mirrors the web YearLens page's core.
import { useQuery } from "@tanstack/react-query";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import PullRefresh from "../components/pull-refresh";

import MoneyMap, { planRows } from "../components/money-map";
import LensSwitch from "../components/lens-switch";
import StaleBanner from "../components/stale-banner";
import { Card, H, KV } from "../components/ui";
import { useSession } from "../lib/session";
import { VERDICT } from "../lib/verdict";
import { C, money } from "../lib/theme";
import { catLabel } from "../lib/pure";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export default function YearLens() {
  const { client } = useSession();
  const router = useRouter();
  const now = new Date();
  // ?y= deep link (notifications, the month lens's year label)
  const p = useLocalSearchParams<{ y?: string }>();
  const [y, setY] = useState(Number(p.y) || now.getFullYear());
  const query = useQuery({
    queryKey: ["lens", "year", y],
    queryFn: () => client!.lensYear(y),
    enabled: !!client,
    // ‹ › keeps the last year on screen (dimmed) while the next loads
    placeholderData: (prev) => prev,
  });
  const today = useQuery({ queryKey: ["today"],
    queryFn: () => client!.todayFull(), enabled: !!client });
  const goal = (today.data?.savings_goals ?? [])
    .find((g) => g.name === "Savings");
  const d = query.data;
  const atNow = y === now.getFullYear();
  const floor = d?.first_year ?? 1990;
  // the year money map covers elapsed BUDGETED months on one dollar
  // scale — "net" months had no plan, so they don't scale the map
  const elapsed = (d?.cells ?? [])
    .filter((c) => c.status === "final" || c.status === "projected").length;

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => query.refetch()} />}>
      <View style={s.nav}>
        <Pressable style={[s.navBtn, y <= floor && { opacity: 0.3 }]}
                   disabled={y <= floor} onPress={() => setY(y - 1)}>
          <Text style={s.navText}>‹</Text>
        </Pressable>
        <Text style={s.navLabel}>{y}</Text>
        <Pressable style={[s.navBtn, atNow && { opacity: 0.3 }]}
                   disabled={atNow} onPress={() => setY(y + 1)}>
          <Text style={s.navText}>›</Text>
        </Pressable>
      </View>
      <LensSwitch active="year" />
      <StaleBanner query={query} />
      {query.isPending && <Text style={s.center}>Loading…</Text>}
      {query.isError && !d && (
        <Text style={[s.center, { color: C.bad }]}>
          Couldn't reach the server — pull down to retry.
        </Text>
      )}
      {/* last year's cells stand in dimmed while this one loads — and
          not tappable, or a tap would open the wrong year's month */}
      {d && (
        <View style={query.isPlaceholderData ? { opacity: 0.5 } : null}
              pointerEvents={query.isPlaceholderData ? "none" : "auto"}>
          <Card>
            <View style={s.grid}>
              {/* each cell is the month's verdict + variance, and a door
                  into that month's lens — the web's year→month path */}
              {d.cells.map((c) => {
                // "net" = no budget snapshot for the month — show its
                // cash flow, muted, never a verdict (matches the web)
                const isNet = c.status === "net";
                const v = c.verdict ? VERDICT[c.verdict] : undefined;
                const color = v?.color ?? (c.verdict ? C.good : C.hover);
                const proj = c.status === "projected";
                return (
                  <Pressable key={c.m}
                    style={({ pressed }) => [s.cell, { borderColor: color },
                      pressed && { backgroundColor: C.hover }]}
                    disabled={c.status === "future"}
                    onPress={() => router.push({ pathname: "/month",
                      params: { y: String(y), m: String(c.m) } } as never)}>
                    <Text style={{ color: C.mut, fontSize: 11 }}>
                      {MONTHS[c.m - 1]}{proj ? " · proj." : ""}
                    </Text>
                    {/* the shared verdict labels — this cell and Today
                        must render one wording for the same server
                        value */}
                    <Text style={{ color: isNet ? C.mut : color,
                                   fontSize: 10, fontWeight: "700" }}>
                      {c.status === "future" ? "—"
                        : isNet ? "NET"
                        : v?.label ?? (c.verdict ? "On plan" : "—")}
                    </Text>
                    {isNet ? (
                      <Text style={{ color: c.net != null
                                       ? (c.net >= 0 ? C.good : C.bad)
                                       : C.mut,
                                     fontSize: 10,
                                     fontVariant: ["tabular-nums"] }}>
                        {c.net != null
                          ? `${c.net >= 0 ? "+" : "−"}${
                              money(Math.abs(c.net), false)}`
                          : money(c.spend ?? 0, false)}
                      </Text>
                    ) : c.variance !== null && c.status !== "future" && (
                      <Text style={{ color: c.variance > 0
                                       ? C.bad : C.good,
                                     fontSize: 10,
                                     fontVariant: ["tabular-nums"] }}>
                        {c.variance > 0 ? "+" : "−"}
                        {money(Math.abs(c.variance), false)}
                      </Text>
                    )}
                  </Pressable>
                );
              })}
            </View>
            {d.cells.some((c) => c.status === "net") && (
              <Text style={{ color: C.mut, fontSize: 11, marginTop: 8 }}>
                Months without a budget snapshot show net (income −
                spending) — a cash-flow fact, not a budget verdict.
                Budgets are snapshotted as each month closes, so verdict
                months are judged against the budget that month actually
                ran under.
              </Text>
            )}
          </Card>
          {d.buckets_annual && elapsed > 0 && (
            <Card>
              <H>Yearly budget</H>
              <MoneyMap rows={planRows(
                { food: d.buckets_annual.food,
                  other: d.buckets_annual.other },
                { fixedActual: d.buckets_annual.fixed.actual,
                  fixedBudget: d.buckets_annual.fixed.month_budget,
                  // standing monthly plan × the elapsed months every
                  // other row here covers; saved_ytd is the ledger's own
                  // figure, rate_90d × elapsed the fallback
                  savings: goal && elapsed > 0
                    ? { plan: goal.monthly_plan * elapsed,
                        saved: d.saved_ytd
                          ?? (goal.rate_90d === null ? null
                              : goal.rate_90d * elapsed) }
                    : null,
                  excess: d.plan_surplus != null && elapsed > 0
                    ? d.plan_surplus * elapsed : null })} />
            </Card>
          )}
          <Card>
            <H>{y} totals</H>
            {/* the web's four KPIs: income and savings rate render "·"
                when unknown rather than vanishing */}
            <KV k="Income" v={d.totals.income !== null
                  ? money(d.totals.income, false) : "·"}
                tone={d.totals.income !== null ? "good" : "mut"} />
            <KV k="Spend" v={money(d.totals.spend, false)} />
            <KV k="Saved"
                v={d.totals.saved !== null
                  ? `${d.totals.saved >= 0 ? "+" : ""}${
                      money(d.totals.saved, false)}` : "·"}
                tone={d.totals.saved === null ? "mut"
                  : d.totals.saved >= 0 ? "good" : "bad"} />
            <KV k="Savings rate"
                v={d.totals.rate !== null ? `${d.totals.rate}%` : "·"}
                tone="mut" />
            {d.totals.income === null && (
              <Text style={{ color: C.mut, fontSize: 12 }}>
                No bank income data for {y} — income and savings are
                unknown, not $0.
              </Text>
            )}
          </Card>
          {d.categories.length > 0 ? (
            <Card>
              <H>Spending by category — {y} vs {y - 1}</H>
              {d.categories.map(([name, cur, prev, key]) => (
                <KV key={name} k={catLabel(name)}
                    // the stored key, not the display name the row
                    // prints — the endpoint matches the key exactly
                    onPress={key ? () => router.push({
                      pathname: "/(tabs)/transactions",
                      params: { cat: key, date_from: `${y}-01-01`,
                                date_to: `${y}-12-31`, counted: "1" } } as never)
                      : undefined}
                    v={`${money(cur, false)}  ·  ${y - 1} ${
                        money(prev, false)}  ·  ${
                        cur - prev >= 0 ? "+" : "−"}${
                        money(Math.abs(cur - prev), false)}`}
                    tone="mut" />
              ))}
              {d.prev_totals.spend > 0 && (
                <KV k="Total spend"
                    v={`${money(d.totals.spend, false)}  ·  ${y - 1} ${
                        money(d.prev_totals.spend, false)}  ·  ${
                        d.totals.spend - d.prev_totals.spend >= 0
                          ? "+" : "−"}${money(Math.abs(
                            d.totals.spend - d.prev_totals.spend),
                            false)}`}
                    tone={d.totals.spend - d.prev_totals.spend > 0
                      ? "bad" : "good"} />)}
            </Card>
          ) : (
            <Text style={{ color: C.mut, fontSize: 12,
                           textAlign: "center", marginTop: 8 }}>
              No spending.
            </Text>
          )}
        </View>
      )}
    </ScrollView>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  center: { color: C.mut, textAlign: "center", padding: 20 },
  nav: { alignItems: "center", flexDirection: "row",
         justifyContent: "space-between", paddingHorizontal: 12,
         paddingTop: 8 },
  navBtn: { paddingHorizontal: 18, paddingVertical: 4 },
  navText: { color: C.accent, fontSize: 24 },
  navLabel: { color: C.text, fontSize: 16, fontWeight: "600" },
  grid: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  cell: { flexBasis: "22%", flexGrow: 1, borderWidth: 1, borderRadius: 8,
          padding: 8, alignItems: "center", gap: 2 },
});
