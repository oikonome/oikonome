// Month lens — one month's verdict, the money map, bills and biggest
// rows, with ‹ › month navigation and a jump-anywhere picker.
import { useQuery } from "@tanstack/react-query";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useState } from "react";
import { Modal, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import PullRefresh from "../components/pull-refresh";

import MoneyMap, { Track, planRows }
  from "../components/money-map";
import LensSwitch from "../components/lens-switch";
import StaleBanner from "../components/stale-banner";
import { Card, H, KV, Meter, Pill } from "../components/ui";
import { useSession } from "../lib/session";
import { C, mmddyy, money } from "../lib/theme";
import { VERDICT } from "../lib/verdict";
import { catLabel } from "../lib/pure";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export default function MonthLens() {
  const { client } = useSession();
  const now = new Date();
  // the year lens deep-links a specific month; default is the live one
  const p = useLocalSearchParams<{ y?: string; m?: string }>();
  const router = useRouter();
  const [ym, setYm] = useState({
    y: Number(p.y) || now.getFullYear(),
    m: Number(p.m) || now.getMonth() + 1,
  });
  const query = useQuery({
    queryKey: ["lens", "month", ym.y, ym.m],
    queryFn: () => client!.lensMonth(ym.y, ym.m),
    enabled: !!client,
    // ‹ › keeps the last month on screen (dimmed) while the next loads —
    // the whole body unmounting to "Loading…" on every tap read as lag
    placeholderData: (prev) => prev,
  });
  // the savings goal for the money map's row — the lens payload carries
  // no standing goal, so it comes from the Today payload (web pattern)
  const today = useQuery({ queryKey: ["today"],
    queryFn: () => client!.todayFull(), enabled: !!client });
  const goal = (today.data?.savings_goals ?? [])
    .find((g) => g.name === "Savings");
  const d = query.data;
  // jump-anywhere picker — Jan 2019 must not be 80 taps away. Years
  // span the ledger (first_year, the web's rule), not a guessed decade.
  const [picking, setPicking] = useState(false);
  const floorYear = d?.first_year ?? now.getFullYear() - 10;
  const years: number[] = [];
  for (let yy = now.getFullYear(); yy >= floorYear; yy--)
    years.push(yy);
  const [pickYear, setPickYear] = useState(now.getFullYear());
  const atNow = ym.y === now.getFullYear() && ym.m === now.getMonth() + 1;
  const shift = (dir: -1 | 1) => setYm(({ y, m }) => {
    const next = m + dir;
    return next < 1 ? { y: y - 1, m: 12 }
      : next > 12 ? { y: y + 1, m: 1 } : { y, m: next };
  });

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => query.refetch()} />}>
      <View style={s.nav}>
        <Pressable style={s.navBtn} onPress={() => shift(-1)}>
          <Text style={s.navText}>‹</Text>
        </Pressable>
        <Pressable onPress={() => { setPickYear(ym.y);
                                    setPicking(true); }}>
          <Text style={s.navLabel}>
            {MONTHS[ym.m - 1]} {ym.y} ▾
          </Text>
        </Pressable>
        <Pressable style={[s.navBtn, atNow && { opacity: 0.3 }]}
                   disabled={atNow} onPress={() => shift(1)}>
          <Text style={s.navText}>›</Text>
        </Pressable>
      </View>
      <Modal visible={picking} animationType="slide" transparent
             onRequestClose={() => setPicking(false)}>
        <View style={s.sheetWrap}>
          <View style={s.sheet}>
            <Text style={{ color: C.text, fontSize: 16,
                           fontWeight: "700" }}>Jump to</Text>
            <ScrollView horizontal showsHorizontalScrollIndicator={false}>
              <View style={{ flexDirection: "row", gap: 6 }}>
                {years.map((yy) => (
                  <Pressable key={yy}
                             style={[s.chip, pickYear === yy && s.chipOn]}
                             onPress={() => setPickYear(yy)}>
                    <Text style={{ color: pickYear === yy
                                     ? C.text : C.mut, fontSize: 13 }}>
                      {yy}
                    </Text>
                  </Pressable>
                ))}
              </View>
            </ScrollView>
            <View style={{ flexDirection: "row", flexWrap: "wrap",
                           gap: 6 }}>
              {MONTHS.map((mn, i) => {
                // future months are unreachable, like the web's
                // disabled options
                const future = pickYear === now.getFullYear()
                  && i + 1 > now.getMonth() + 1;
                return (
                <Pressable key={mn}
                           disabled={future}
                           style={[s.chip, { width: "22%",
                             alignItems: "center" },
                             future && { opacity: 0.3 },
                             ym.y === pickYear && ym.m === i + 1
                               && s.chipOn]}
                           onPress={() => { setYm({ y: pickYear,
                                                    m: i + 1 });
                                            setPicking(false); }}>
                  <Text style={{ color: C.text, fontSize: 13 }}>{mn}</Text>
                </Pressable>
                );
              })}
            </View>
          </View>
        </View>
      </Modal>
      <LensSwitch active="month" />
      <StaleBanner query={query} />
      {query.isPending && <Text style={s.center}>Loading…</Text>}
      {query.isError && !d && (
        <Text style={[s.center, { color: C.bad }]}>
          Couldn't reach the server — pull down to retry.
        </Text>
      )}
      {d && (() => {
        const left = d.variable_budget - d.variable_actual;
        const dim = query.isPlaceholderData ? { opacity: 0.5 } : null;
        // the web's exact threshold (remaining < 0) — a 30¢ overage
        // must read "over" on both clients, not "over" on one
        const over = left < 0;
        const dayN = Number(d.as_of?.slice(8, 10)) || 0;
        const inProg = d.status === "in_progress";
        const frac = d.variable_budget > 0
          ? Math.min(1, d.variable_actual / d.variable_budget)
          : d.variable_actual > 0.005 ? 1 : 0;
        const pace = inProg && d.days_in_month
          ? dayN / d.days_in_month : null;
        const tol = d.tolerance ?? 0;
        const withinTol = !over && Math.abs(d.variance) <= tol;
        const net = d.income.actual !== null
          ? d.income.actual - d.spend_total : null;
        return (
        <View style={dim}
              pointerEvents={query.isPlaceholderData ? "none" : "auto"}>
          <Card>
            <View style={{ flexDirection: "row", alignItems: "center",
                           gap: 8 }}>
              <Text style={[s.verdict,
                            { color: VERDICT[d.verdict]?.color
                                ?? C.text }]}>
                {VERDICT[d.verdict]?.label ?? d.verdict}
              </Text>
              <Pill text={d.status === "final" ? "final"
                : d.status === "future" ? "planned"
                : d.days_in_month ? `day ${dayN} of ${d.days_in_month}`
                : "so far"} />
            </View>
            {/* the headline the page exists for */}
            <Text style={s.hero}>
              <Text style={{ color: over ? C.bad : C.text }}>
                {money(Math.abs(left), false)}
              </Text>
              <Text style={s.heroSub}>
                {/* the web's exact branch: "left unspent of" is the
                    FINAL month's phrasing; future months read like a plan */}
                {over ? ` over ${money(d.variable_budget, false)} to spend`
                  : d.status === "final"
                    ? ` left unspent of ${
                        money(d.variable_budget, false)} to spend`
                    : ` left of ${money(d.variable_budget, false)} to spend`}
              </Text>
            </Text>
            {/* the web hero's Track: caret at EXPECTED spend
                (actual − variance), never the calendar day */}
            <Track paceLabel row={{ label: "", judged: true,
                          actual: d.variable_actual,
                          expected: d.variable_actual - d.variance,
                          budget: d.variable_budget }} />
            <Text style={{ fontSize: 13, marginTop: 4 }}>
              {d.variance > tol ? (
                <Text style={{ color: C.bad, fontWeight: "700" }}>
                  {money(d.variance, false)} over budget
                </Text>
              ) : d.variance < -tol ? (
                <Text style={{ color: C.good, fontWeight: "700" }}>
                  {money(-d.variance, false)} under budget
                </Text>
              ) : (
                <Text style={{ color: C.warn, fontWeight: "700" }}>
                  within tolerance ({money(tol, false)})
                </Text>
              )}
              <Text style={s.mut}>
                {d.status !== "final" ? " so far" : ""}
              </Text>
            </Text>
            {/* a closed month with no frozen snapshot is judged against
                TODAY's budget — say so (matches the web) */}
            {d.status === "final" && d.budget_source === "live" && (
              <Text style={{ color: C.mut, fontSize: 12 }}>
                No budget snapshot exists for this month — the verdict
                compares it against the current budget, applied
                retroactively.
              </Text>
            )}
            {d.variable_scale && (
              <Text style={{ color: C.warn, fontSize: 12 }}>
                Variable budgets auto-scaled from{" "}
                {money(d.variable_scale.static_total, false)} to fit this
                month&apos;s bills.
              </Text>
            )}
            {d.projection && (() => {
              const p0 = d.projection;
              const ptol = p0.tolerance ?? tol;
              return (
                <Text style={[s.mut, { marginTop: 4 }]}>
                  At the current rate, projected{" "}
                  <Text style={{ color: C.text, fontWeight: "700" }}>
                    {money(p0.pace_total, false)}
                  </Text>
                  {" by month end — "}
                  {p0.variance > ptol ? (
                    <Text style={{ color: C.bad, fontWeight: "700" }}>
                      {money(p0.variance, false)} over
                    </Text>
                  ) : p0.variance < -ptol ? (
                    <Text style={{ color: C.good, fontWeight: "700" }}>
                      {money(-p0.variance, false)} under
                    </Text>
                  ) : (
                    <Text style={{ color: C.warn, fontWeight: "700" }}>
                      on plan
                    </Text>
                  )}.
                </Text>
              );
            })()}
            <Text style={[s.mut, { marginTop: 6 }]}>
              {/* the web flow line's words: "budgeted", "net $N" */}
              {d.income.actual !== null
                ? `${money(d.income.actual, false)} in`
                : "income unknown"}
              {d.income.budgeted !== null
                ? ` (budgeted ${money(d.income.budgeted, false)})` : ""}
              {" → "}{money(d.spend_total, false)} spent
              {net !== null ? (
                ` → net ${net >= 0 ? "" : "−"}${money(Math.abs(net), false)}`
              ) : ""}
            </Text>
          </Card>
          <Card>
            <H>Monthly budget</H>
            <MoneyMap rows={planRows(d.buckets, { period: { y: ym.y, m: ym.m },
              fixedActual: d.bills.posted,
              fixedBudget: d.bills.planned,
              fixedPending: d.bills.awaiting,
              // the month's REAL posted contribution when the payload
              // carries it; rate_90d only as a trailing-average fallback
              savings: goal
                ? { plan: goal.monthly_plan,
                    saved: d.saved_month ?? goal.rate_90d } : null,
              excess: d.plan_surplus,
            })} />
          </Card>
          {/* the web's empty rule: only when paid, overdue AND envelopes
              are all empty — an envelope-only month still has a story */}
          {((d.bills.paid ?? []).length > 0 || d.bills.overdue.length > 0
            || (d.bills.envelopes ?? []).length > 0
            || d.bills.posted > 0 || d.bills.planned > 0
            || d.bills.awaiting > 0) ? (
          <Card>
            <H>Bills</H>
            <KV k={`Posted (${(d.bills.paid ?? []).length})`}
                v={money(d.bills.posted, false)} />
            <KV k="Planned" v={money(d.bills.planned, false)} tone="mut" />
            {d.bills.awaiting > 0 && (
              <KV k="Due, not posted" v={money(d.bills.awaiting, false)}
                  tone="warn" />)}
            {d.bills.overdue.length > 0 && (
              <Text style={s.mut}>Scheduled but not posted:</Text>
            )}
            {d.bills.overdue.slice(0, 5).map(([payee, amt, due]) => (
              <KV key={payee} k={`${payee} · due ${mmddyy(due)}`}
                  v={money(amt, false)} tone="bad" />
            ))}
            {(d.bills.envelopes ?? []).map((e) => (
              <KV key={e.payee}
                  k={`${e.payee} (envelope)`}
                  v={`${money(e.used, false)} of ${money(e.monthly, false)}${
                      e.overflow > 0.005
                        ? ` · over +${money(e.overflow, false)}` : ""}`}
                  tone={e.overflow > 0.005 ? "bad" : "mut"} />
            ))}
            {(d.bills.paid ?? []).map((p, i) => (
              <KV key={`${p.payee}-${i}`}
                  k={`${mmddyy(p.date)} · ${p.payee} · paid ✓`}
                  v={money(p.amount, false)} tone="mut" />
            ))}
          </Card>
          ) : (
            <Text style={s.mut2}>No tracked bill activity this month.</Text>
          )}
          {d.by_category.length > 0 && (
            <Card>
              <H>{d.status === "final"
                ? "Spending by category" : "Month to date by category"}</H>
              {d.by_category.map(([name, v, key]) => (
                <KV key={name}
                    k={name === "Amazon" && (d.amazon_subs ?? []).length
                      ? `${name} (${d.amazon_subs!.length})`
                      : catLabel(name)}
                    v={money(v, false)} tone="mut"
                    // the row carries the STORED key beside its display
                    // name; the display form ("FOOD AND DRINK") matches
                    // nothing server-side. No key means no single
                    // category stands behind the number — the clustered
                    // Amazon parent — so that row is plain text.
                    onPress={key ? () => router.push({
                      pathname: "/(tabs)/transactions",
                      params: { cat: key, y: String(ym.y),
                                m: String(ym.m), counted: "1" } } as never)
                      : undefined} />
              ))}
            </Card>
          )}
          {d.biggest.length > 0 && (
            <Card>
              <H>Biggest transactions</H>
              {d.biggest.map((t, i) => (
                <View key={`${t.payee}-${i}`} style={s.bigRow}>
                  <View style={{ flex: 1 }}>
                    <Text style={s.bigPayee} numberOfLines={1}>
                      {t.payee}
                    </Text>
                    <Text style={[s.mut, { fontSize: 11 }]}>
                      {mmddyy(t.date)} · {catLabel(t.category)}
                      {t.account ? ` · ${t.account}` : ""}
                    </Text>
                  </View>
                  <Text style={s.bigAmt}>{money(-t.amount, false)}</Text>
                </View>
              ))}
            </Card>
          )}
        </View>
        );
      })()}
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
  verdict: { fontSize: 22, fontWeight: "800" },
  hero: { marginTop: 8, fontSize: 26, fontWeight: "800",
          fontVariant: ["tabular-nums"] },
  heroSub: { color: C.mut, fontSize: 14, fontWeight: "400" },
  mut: { color: C.mut, fontSize: 12 },
  mut2: { color: C.mut, fontSize: 12, textAlign: "center", marginTop: 10 },
  bigRow: { flexDirection: "row", alignItems: "center", gap: 8,
            borderTopColor: C.border,
            borderTopWidth: StyleSheet.hairlineWidth, paddingVertical: 6 },
  bigPayee: { color: C.text, fontSize: 13, fontWeight: "600" },
  bigAmt: { color: C.text, fontSize: 13, fontVariant: ["tabular-nums"] },
  sheetWrap: { flex: 1, justifyContent: "flex-end",
               backgroundColor: "rgba(0,0,0,0.55)" },
  sheet: { backgroundColor: C.card, borderTopLeftRadius: 16,
           borderTopRightRadius: 16, padding: 16, gap: 12 },
  chip: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
          borderRadius: 999, paddingHorizontal: 12, paddingVertical: 7 },
  chipOn: { borderColor: C.accent, backgroundColor: C.hover },
});
