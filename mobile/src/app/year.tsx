// Year lens — twelve verdict cells, the year totals against last year,
// and the category ranking. Mirrors the web YearLens page's core.
import { useQuery } from "@tanstack/react-query";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useState } from "react";
import { Modal, Pressable, ScrollView, StyleSheet, Text, View }
  from "react-native";
import PullRefresh from "../components/pull-refresh";

import MoneyMap, { sentPlanRows, sentPlanTotal } from "../components/money-map";
import LensSwitch from "../components/lens-switch";
import { FailedLine, StaleLine } from "../components/loaded";
import { Card, H, KV, Skeleton } from "../components/ui";
import { useSession } from "../lib/session";
import { VERDICT } from "../lib/verdict";
import type { Change, YearLensData } from "../lib/api";
import { C, money } from "../lib/theme";
import { catLabel, changeLook, householdDay, netSign, routeInt,
  signedMoney, signedPct } from "../lib/pure";
import { useMe } from "../lib/viewer";
import { useSheetAnimation } from "../lib/a11y";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// The prior year's column label: "2025" when both years are whole, and
// "2025 (to Jun 30)" while the lens year is in progress — the server sets
// it against that same span of the year before
function priorYearLabel(d: YearLensData): string {
  const [, m, day] = (d.prev_through ?? "").split("-").map(Number);
  return m ? `${d.y - 1} (to ${MONTHS[m - 1]} ${day})` : `${d.y - 1}`;
}


export default function YearLens() {
  const { client } = useSession();
  const router = useRouter();
  // the household's day, not the device's: near midnight across zones
  // the device would open the wrong month or year
  const me = useMe();
  const now = householdDay(new Date(), me.data?.timezone);
  // ?y= deep link (notifications, the month lens's year label)
  const p = useLocalSearchParams<{ y?: string }>();
  const [y, setY] = useState(
    () => routeInt(p.y, 1900, 2100) ?? now.getFullYear());
  const query = useQuery({
    queryKey: ["lens", "year", y],
    queryFn: () => client!.lensYear(y),
    enabled: !!client,
    // ‹ › keeps the last year on screen (dimmed) while the next loads
    placeholderData: (prev) => prev,
  });
  const d = query.data;
  const atNow = y >= now.getFullYear();
  const floor = d?.first_year ?? 1990;
  // the web's year dropdown: any year of the ledger, one tap away
  const sheetAnim = useSheetAnimation();
  const [picking, setPicking] = useState(false);
  const years: number[] = [];
  for (let yy = now.getFullYear(); yy >= Math.min(floor, now.getFullYear());
       yy--) years.push(yy);

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => query.refetch()} />}>
      <View style={s.nav}>
        <Pressable style={[s.navBtn, y <= floor && { opacity: 0.3 }]}
                   disabled={y <= floor}
                   accessibilityRole="button" accessibilityLabel="Previous year"
                   onPress={() => setY(y - 1)}>
          <Text style={s.navText}>‹</Text>
        </Pressable>
        <Pressable onPress={() => setPicking(true)}
                   accessibilityRole="button"
                   accessibilityLabel={`${y}, choose a year`}>
          <Text style={s.navLabel}>{y} ▾</Text>
        </Pressable>
        <Pressable style={[s.navBtn, atNow && { opacity: 0.3 }]}
                   disabled={atNow}
                   accessibilityRole="button" accessibilityLabel="Next year"
                   onPress={() => setY(y + 1)}>
          <Text style={s.navText}>›</Text>
        </Pressable>
      </View>
      <Modal visible={picking} animationType={sheetAnim} transparent
             onRequestClose={() => setPicking(false)}>
        <View style={s.sheetWrap}>
          <View style={s.sheet}>
            <Text style={{ color: C.text, fontSize: 16,
                           fontWeight: "700" }}>Jump to</Text>
            <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
              {years.map((yy) => (
                <Pressable accessibilityRole="button" key={yy}
                           style={[s.chip, yy === y && s.chipOn]}
                           onPress={() => { setY(yy); setPicking(false); }}>
                  <Text style={{ color: yy === y ? C.text : C.mut,
                                 fontSize: 13 }}>{yy}</Text>
                </Pressable>
              ))}
            </View>
          </View>
        </View>
      </Modal>
      <LensSwitch active="year" />
      <StaleLine query={query} />
      {query.isPending && <Skeleton />}
      {query.isError && !d && (
        <FailedLine q={query} what="the year" />
      )}
      {/* last year's cells stand in dimmed while this one loads — and
          not tappable, or a tap would open the wrong year's month */}
      {d && (
        <View style={query.isPlaceholderData ? { opacity: 0.5 } : null}
              pointerEvents={query.isPlaceholderData ? "none" : "auto"}>
          <Card>
            <View style={s.grid}>
              {/* each cell is the month's verdict + margin, and a door
                  into that month's lens — the web's year→month path */}
              {d.cells.map((c) => {
                // "net" = no budget snapshot for the month — show its
                // cash flow, muted, never a verdict (matches the web)
                const isNet = c.status === "net";
                // a verdict this build does not know reads as on plan
                // (the web's fallback to ON BUDGET)
                const v = c.verdict
                  ? VERDICT[c.verdict] ?? VERDICT["ON BUDGET"] : undefined;
                const color = v?.color ?? C.hover;
                const proj = c.status === "projected";
                // the server's words for the cell (lenses.cell_words; web:
                // YearLens): pill, figure, and the note that greys a month
                // whose income the bank feed does not measure
                const tone = c.tone === "pos" ? C.good
                  : c.tone === "neg" ? C.bad : C.mut;
                return (
                  <Pressable accessibilityRole="button" key={c.m}
                    style={({ pressed }) => [s.cell, { borderColor: color },
                      c.note ? { opacity: 0.6 } : null,
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
                      {/* the server's label as sent (the web's pill) */}
                      {isNet ? (c.label ?? "net") : v?.label ?? "—"}
                    </Text>
                    {isNet ? (
                      // older-server path: stands in for `label`/`figure`
                      // (the cell's net, signed, as before)
                      <Text style={{ color: c.label ? tone
                                       : c.net != null
                                         ? (c.net >= 0 ? C.good : C.bad)
                                         : C.mut,
                                     fontSize: 10,
                                     fontVariant: ["tabular-nums"] }}>
                        {/* the server's figure and tone when it sends them */}
                        {c.label ? (c.figure ?? "—")
                          : c.net != null
                            ? `${c.net >= 0 ? "+" : "−"}${
                                money(Math.abs(c.net), false)}`
                            : "—"}
                      </Text>
                    ) : v && c.figure != null && (
                      // the margin in the server's words ("over by $300"):
                      // on this page only a net carries a sign
                      <Text style={{ color: tone, fontSize: 10,
                                     fontVariant: ["tabular-nums"] }}>
                        {c.figure}
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
          {/* the server's rows over the elapsed budgeted months
              (lenses.year_summary → plan_rows): null when nothing was
              budgeted, absent from an older server — no card either way */}
          {d.plan_rows && (
            <Card>
              <H>Yearly budget</H>
              {/* the server's words for which months the card covers */}
              {d.plan_caption ? (
                <Text style={{ color: C.mut, fontSize: 12, marginBottom: 6 }}>
                  {d.plan_caption}</Text>) : null}
              <MoneyMap rows={sentPlanRows(d.plan_rows)}
                        total={sentPlanTotal(d.plan_total)}
                        footnote={d.excess_text ? (
                          // the server's sentence: what an ordinary month
                          // leaves, absent under a year over budget
                          <Text style={{ color: C.mut, fontSize: 12,
                                         marginTop: 8 }}>
                            {d.excess_text}</Text>) : null} />
            </Card>
          )}
          <Card>
            <H>{y} totals</H>
            {/* the web's four KPIs: income and savings rate render "·"
                when unknown rather than vanishing */}
            {/* neutral, as the web's income figure */}
            {/* the server's whole dollars (lenses.totals_dollars): left
                over is the printed income less the printed spend */}
            {/* the left over's sign as printed: $0 is even, untoned,
                with no rate (the web's rule, netSign) */}
            {(() => { const shown = totalsShown(d.totals);
              const sign = shown.saved !== null ? netSign(shown.saved) : null;
              return (<>
            <KV k="Income" v={shown.income !== null
                  ? money(shown.income, false) : "·"}
                tone={d.totals.income !== null ? undefined : "mut"} />
            <KV k="Spend" v={money(shown.spend, false)} />
            <KV k="Left over"
                v={shown.saved !== null
                  ? signedMoney(shown.saved, (n) => money(n, false), true)
                  : "·"}
                tone={sign === null ? "mut"
                  : sign === 1 ? "good" : sign === -1 ? "bad" : undefined} />
            <KV k="Savings rate"
                v={d.totals.rate !== null && sign !== 0
                  ? signedPct(d.totals.rate) : "·"}
                tone="mut" />
            </>); })()}
            {/* the server's words when a month's income is incomplete:
                income, left over and the rate then count the other months
                (the web's Year lens) */}
            {!!d.totals.incomplete_text && (
              <Text style={{ color: C.mut, fontSize: 11 }}>
                {d.totals.incomplete_text}
              </Text>
            )}
            {d.dating === "bill_month" && (
              <Text style={{ color: C.mut, fontSize: 11 }}>
                the sum of the months above; a bill counts in the month it
                belongs to — Cash Flow dates spending by the day it was paid
              </Text>
            )}
            {/* the server's words when no month of the year has bank
                data; nothing of the client's own */}
            {!!d.totals.unknown_text && (
              <Text style={{ color: C.mut, fontSize: 12 }}>
                {d.totals.unknown_text}
              </Text>
            )}
            {/* older-server path: stands in for `unknown_text` (its null
                income meant no month had bank data) */}
            {d.totals.unknown_text === undefined && d.totals.income === null && (
              <Text style={{ color: C.mut, fontSize: 12 }}>
                No month of {y} so far has bank data — income and savings
                are unknown, not $0.
              </Text>
            )}
          </Card>
          {d.categories.length > 0 ? (
            <Card>
              <H>Spending by category — {y} vs {priorYearLabel(d)}</H>
              {d.categories.map(([name, cur, prev, key], i) => (
                <KV key={name} k={catLabel(name)}
                    // the stored key, not the display name the row
                    // prints — the endpoint matches the key exactly
                    onPress={key ? () => router.push({
                      pathname: "/(tabs)/transactions",
                      params: { cat: key, date_from: `${y}-01-01`,
                                date_to: `${y}-12-31`, counted: "1" } } as never)
                      : undefined}
                    v={`${money(cur, false)}  ·  ${priorYearLabel(d)} ${
                        money(prev, false)}${
                        yearChangeText(d.changes?.categories[i])}`}
                    // the change's colour, as the web's Δ column
                    tone={yearChangeTone(d.changes?.categories[i])} />
              ))}
              {d.prev_totals.spend > 0 && (
                <KV k="Total spend"
                    v={`${money(d.totals.spend, false)}  ·  ${priorYearLabel(d)} ${
                        money(d.prev_totals.spend, false)}${
                        yearChangeText(d.changes?.spend)}`}
                    tone={yearChangeTone(d.changes?.spend)} />)}
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
  // a fingertip in each direction: the arrows are the screen's main control
  navBtn: { paddingHorizontal: 18, paddingVertical: 4, minHeight: 44,
            minWidth: 44, alignItems: "center", justifyContent: "center" },
  navText: { color: C.accent, fontSize: 24 },
  navLabel: { color: C.text, fontSize: 16, fontWeight: "600" },
  grid: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  sheetWrap: { flex: 1, justifyContent: "flex-end",
               backgroundColor: "rgba(0,0,0,0.55)" },
  sheet: { backgroundColor: C.card, borderTopLeftRadius: 16,
           borderTopRightRadius: 16, padding: 16, gap: 12 },
  chip: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
          borderRadius: 999, paddingHorizontal: 12, paddingVertical: 7 },
  chipOn: { borderColor: C.accent, backgroundColor: C.hover },
  cell: { flexBasis: "22%", flexGrow: 1, borderWidth: 1, borderRadius: 8,
          padding: 8, alignItems: "center", gap: 2 },
});

/** The year's totals in the whole dollars the server prints them in. */
function totalsShown(t: { income: number | null; spend: number;
                          saved: number | null;
                          dollars?: { income: number | null; spend: number;
                                      saved: number | null } }) {
  return t.dollars !== undefined
    ? t.dollars
    // older-server path: stands in for `dollars`
    : { income: t.income, spend: t.spend, saved: t.saved };
}

/** "  ·  +$120" — the server's change from last year (reporting.change,
 *  the printed figure minus the printed figure), "unchanged" at $0 as on
 *  the web; nothing from an older server, which sends none. */
function yearChangeText(ch: Change | null | undefined): string {
  const c = changeLook(ch, true);
  if (!c) return "";
  return c.unchanged ? "  ·  unchanged"
    : `  ·  ${c.sign}${money(c.dollars, false)}`;
}

function yearChangeTone(ch: Change | null | undefined)
  : "good" | "bad" | "mut" {
  const c = changeLook(ch, true);
  return !c || c.unchanged ? "mut" : c.good ? "good" : "bad";
}
