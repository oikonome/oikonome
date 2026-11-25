// Month lens — one month's verdict, the money map, bills and biggest
// rows, with ‹ › month navigation and a jump-anywhere picker.
import { useQuery } from "@tanstack/react-query";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useState } from "react";
import { Modal, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import PullRefresh from "../components/pull-refresh";

import MoneyMap, { Track, sentPlanRows, sentPlanTotal }
  from "../components/money-map";
import LensSwitch from "../components/lens-switch";
import { FailedLine, NoAccountsLine, StaleLine, useNoAccounts }
  from "../components/loaded";
import { Card, H, KV, Pill, Skeleton } from "../components/ui";
import { useSession } from "../lib/session";
import { C, money } from "../lib/theme";
import { VERDICT } from "../lib/verdict";
import { catLabel, EMPTY_LINES, householdDay, routeYm, signedMoney }
  from "../lib/pure";
import { useMe } from "../lib/viewer";
import { useSheetAnimation } from "../lib/a11y";

// '2026-11-03' → '11/03': the web lens's dates (its mmdd)
const mmdd = (iso: string | null | undefined) =>
  iso && iso.length >= 10 ? `${iso.slice(5, 7)}/${iso.slice(8, 10)}` : "";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];


/** The plan's income, named by where it comes from (budget.income_source)
 *  — the web's planIncomeLabel. Only a typed figure is "budgeted". */
function planIncomeLabel(source?: string | null): string {
  return source === "paychecks" ? "expected from paychecks"
    : source === "scenario" ? "income scenario"
    : source === "typed" ? "budgeted" : "planned";
}


// the colour of a line whose tone the server chose (lenses.hero_words)
// (read at render: the palette follows the theme)
const toneColor = (t?: string | null) =>
  t === "neg" ? C.bad : t === "pos" ? C.good : t === "warn" ? C.warn : C.text;
export default function MonthLens() {
  const sheetAnim = useSheetAnimation();
  const { client } = useSession();
  // the household's day, not the device's: near midnight across zones
  // the device would open the wrong month or year
  const me = useMe();
  const now = householdDay(new Date(), me.data?.timezone);
  // the year lens deep-links a specific month; default is the live one
  const p = useLocalSearchParams<{ y?: string; m?: string }>();
  const router = useRouter();
  // a deep link can carry any month at all; out of range opens this one
  const [ym, setYm] = useState(() => routeYm(p.y, p.m, now));
  const query = useQuery({
    queryKey: ["lens", "month", ym.y, ym.m],
    queryFn: () => client!.lensMonth(ym.y, ym.m),
    enabled: !!client,
    // ‹ › keeps the last month on screen (dimmed) while the next loads —
    // the whole body unmounting to "Loading…" on every tap read as lag
    placeholderData: (prev) => prev,
  });
  const d = query.data;
  const noAccounts = useNoAccounts();
  // jump-anywhere picker — Jan 2019 must not be 80 taps away. Years
  // span the ledger (first_year, the web's rule), not a guessed decade.
  const [picking, setPicking] = useState(false);
  const floorYear = d?.first_year ?? now.getFullYear() - 10;
  const years: number[] = [];
  for (let yy = now.getFullYear(); yy >= floorYear; yy--)
    years.push(yy);
  const [pickYear, setPickYear] = useState(now.getFullYear());
  // the web's rule: no step past the household's month, from it or from
  // any later month a link opened on
  const atNow = ym.y > now.getFullYear()
    || (ym.y === now.getFullYear() && ym.m >= now.getMonth() + 1);
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
        <Pressable style={s.navBtn}
                   accessibilityRole="button" accessibilityLabel="Previous month"
                   onPress={() => shift(-1)}>
          <Text style={s.navText}>‹</Text>
        </Pressable>
        <Pressable onPress={() => { setPickYear(ym.y);
                                    setPicking(true); }}
                   accessibilityRole="button"
                   accessibilityLabel={`${MONTHS[ym.m - 1]} ${ym.y}, choose a month`}>
          <Text style={s.navLabel}>
            {MONTHS[ym.m - 1]} {ym.y} ▾
          </Text>
        </Pressable>
        <Pressable style={[s.navBtn, atNow && { opacity: 0.3 }]}
                   disabled={atNow}
                   accessibilityRole="button" accessibilityLabel="Next month"
                   onPress={() => shift(1)}>
          <Text style={s.navText}>›</Text>
        </Pressable>
      </View>
      <Modal visible={picking} animationType={sheetAnim} transparent
             onRequestClose={() => setPicking(false)}>
        <View style={s.sheetWrap}>
          <View style={s.sheet}>
            <Text style={{ color: C.text, fontSize: 16,
                           fontWeight: "700" }}>Jump to</Text>
            <ScrollView horizontal showsHorizontalScrollIndicator={false}>
              <View style={{ flexDirection: "row", gap: 6 }}>
                {years.map((yy) => (
                  <Pressable accessibilityRole="button" key={yy}
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
                <Pressable accessibilityRole="button" key={mn}
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
      <StaleLine query={query} />
      {query.isPending && <Skeleton />}
      {query.isError && !d && (
        <FailedLine q={query} what="the month" />
      )}
      {d && noAccounts && <NoAccountsLine />}
      {d && (() => {
        // an unjudged month (verdict null, budget_set false) still carries
        // a numeric variable_budget; the ?? only covers an older server's
        // null (web: MonthLens)
        const budget = d.variable_budget ?? 0;
        // the spendable left and the month's net are the server's
        // (lenses.month_summary → remaining, net), as on the web; an older
        // server sends neither, and the hero shows the spend alone
        const left = d.remaining ?? 0;
        const dim = query.isPlaceholderData ? { opacity: 0.5 } : null;
        // the web's exact threshold (remaining < 0) — a 30¢ overage
        // must read "over" on both clients, not "over" on one
        const over = left < 0;
        const dayN = Number(d.as_of?.slice(8, 10)) || 0;
        const net = d.net ?? null;
        // no budget the household saved stands behind the month: no
        // verdict (web: MonthLens unjudged)
        const unjudged = d.verdict === null;
        // a closed month from before the household had a budget, or one
        // with no data: the server says which and writes the words; no
        // budget figures, map or bills to draw (web: MonthLens past)
        const past = d.standing === "before_budget"
          || d.standing === "no_data";
        // a month with no rows at all: its line, and nothing else
        const noData = d.standing === "no_data";
        // the hero's left-of figure is the server's; without it (an older
        // server) the hero prints the spend alone
        const spendOnly = unjudged || d.remaining == null;
        return (
        <View style={dim}
              pointerEvents={query.isPlaceholderData ? "none" : "auto"}>
          <Card>
            <View style={{ flexDirection: "row", alignItems: "center",
                           gap: 8 }}>
              <Text style={[s.verdict,
                            { color: d.verdict === null ? C.mut
                                : VERDICT[d.verdict]?.color ?? C.text }]}>
                {d.verdict === null ? (d.standing_label ?? "no budget set")
                  : VERDICT[d.verdict]?.label ?? d.verdict}
              </Text>
              <Pill text={d.status === "final" ? "final"
                : d.status === "future" ? "planned"
                : d.days_in_month ? `day ${dayN} of ${d.days_in_month}`
                : "so far"} />
            </View>
            {/* the headline the page exists for */}
            {past ? null : spendOnly ? (
              <Text style={s.hero}>
                <Text style={{ color: C.text }}>
                  {money(d.variable_actual, false)}
                </Text>
                <Text style={s.heroSub}> variable spend</Text>
              </Text>
            ) : (<>
            <Text style={s.hero}>
              <Text style={{ color: over ? C.bad : C.text }}>
                {money(Math.abs(left), false)}
              </Text>
              <Text style={s.heroSub}>
                {/* the server's word ("over", "left of", "left unspent
                    of" — lenses.hero_words), as the web prints it */}
                {/* older-server path: stands in for `remaining_word` */}
                {d.remaining_word !== undefined
                  ? (d.remaining_word
                    ? ` ${d.remaining_word} ${money(budget, false)} to spend` : "")
                  : ` ${over ? "over" : d.status === "final"
                      ? "left unspent of" : "left of"} ${
                      money(budget, false)} to spend`}
              </Text>
            </Text>
            {/* the web hero's Track: caret where the server says the pace
                stands (pace_expected); the tick's label is the server's */}
            {/* older-server path: stands in for `pace_expected` (the
                spend less its variance, as the old hero placed it) */}
            {(d.pace_expected !== undefined ? d.pace_expected != null : true) && (
            <Track paceLabel={d.pace_label} row={{ label: "", judged: true,
                          actual: d.variable_actual,
                          expected: d.pace_expected
                            ?? d.variable_actual - d.variance,
                          budget }} />)}
            </>)}
            {unjudged ? (
              <Text style={[s.mut, { fontSize: 13, marginTop: 4 }]}>
                {d.standing_text ?? `No budget saved yet, so this month gets no verdict — set one on the Budget page.`}
              </Text>
            ) : (
            <Text style={{ fontSize: 13, marginTop: 4 }}>
              {/* the verdict line in the server's words and tone */}
              {d.variance_text !== undefined ? (d.variance_text && (
                <Text style={{ color: toneColor(d.variance_text.tone),
                               fontWeight: "700" }}>
                  {d.variance_text.text}
                </Text>
              )) : (() => {
                // older-server path: stands in for `variance_text`
                const tol = d.tolerance ?? 0;
                const soFar = d.status === "in_progress" ? " so far" : "";
                return d.variance > tol
                  ? <Text style={{ color: C.bad, fontWeight: "700" }}>
                      {money(d.variance, false)} over budget{soFar}</Text>
                  : d.variance < -tol
                    ? <Text style={{ color: C.good, fontWeight: "700" }}>
                        {money(-d.variance, false)} under budget{soFar}</Text>
                    : <Text style={{ color: C.warn, fontWeight: "700" }}>
                        within tolerance ({money(tol, false)}){soFar}</Text>;
              })()}
            </Text>
            )}
            {/* a closed month with no frozen snapshot is judged against
                TODAY's budget — say so (matches the web) */}
            {!unjudged && d.status === "final"
              && d.budget_source === "live" && (
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
              return (
                <Text style={[s.mut, { marginTop: 4 }]}>
                  At the current rate, projected{" "}
                  <Text style={{ color: C.text, fontWeight: "700" }}>
                    {money(p0.pace_total, false)}
                  </Text>
                  {" by month end"}
                  {/* the projection's last words are the server's */}
                  {p0.tail ? <>{" — "}<Text style={{
                      color: toneColor(p0.tail_tone),
                      fontWeight: "700" }}>{p0.tail}</Text></>
                    : p0.tail !== undefined ? null : (() => {
                    // older-server path: stands in for `projection.tail`
                    const ptol = p0.tolerance ?? d.tolerance ?? 0;
                    return <>{" — "}{p0.variance > ptol
                      ? <Text style={{ color: C.bad, fontWeight: "700" }}>
                          {money(p0.variance, false)} over</Text>
                      : p0.variance < -ptol
                        ? <Text style={{ color: C.good, fontWeight: "700" }}>
                            {money(-p0.variance, false)} under</Text>
                        : <Text style={{ color: C.warn, fontWeight: "700" }}>
                            on plan</Text>}</>;
                  })()}.
                </Text>
              );
            })()}
            {!noData && <Text style={[s.mut, { marginTop: 6 }]}>
              {/* the web flow line: income the bank feed does not measure
                  is the server's words alone (income_text) — no plan
                  figure beside it and no net; else the income, the plan
                  income named by its source (planIncomeLabel), and the
                  net in its sign's colour ("·" when the server has none) */}
              {d.income_text ? d.income_text
                // older-server path: stands in for `income_text`
                : d.income_text === undefined && d.income.actual === null
                  ? "income unknown" : <>
                <Text style={{ color: C.text, fontWeight: "700" }}>
                  {money(d.income.actual, false)}</Text> in
                {d.income.plan_text !== undefined
                  ? (d.income.plan_text ? ` (${d.income.plan_text})` : "")
                  // older-server path: stands in for `plan_text`
                  : d.income.budgeted !== null
                    ? ` (${planIncomeLabel(d.income.source)} ${
                        money(d.income.budgeted, false)})` : ""}</>}
              {" → "}
              <Text style={{ color: C.text, fontWeight: "700" }}>
                {money(d.spend_total, false)}</Text> spent
              {!d.income_text && <>{" → net "}
                <Text style={{ fontWeight: "700", color: net === null ? C.mut
                                 : net >= 0 ? C.good : C.bad }}>
                  {net !== null ? signedMoney(net, (n) => money(n, false), true)
                    : money(null, false)}</Text></>}
            </Text>}
            {d.dating === "bill_month" && !past && (
              <Text style={[s.mut, { fontSize: 11, marginTop: 2 }]}>
                a bill counts in the month it belongs to — Cash Flow dates
                spending by the day it was paid
              </Text>
            )}
          </Card>
          {!past && <Card>
            <H>Monthly budget</H>
            {/* the server's rows (lenses.month_summary → plan_rows),
                the web's; an older server sends none and draws none */}
            <MoneyMap rows={sentPlanRows(d.plan_rows ?? [])}
                      total={sentPlanTotal(d.plan_total)} />
          </Card>}
          {/* the web's empty rule: only when paid, overdue AND envelopes
              are all empty — an envelope-only month still has a story */}
          {past ? null : ((d.bills.paid ?? []).length > 0 || d.bills.overdue.length > 0
            || (d.bills.envelopes ?? []).length > 0
            || d.bills.posted > 0 || d.bills.planned > 0
            || d.bills.awaiting > 0
            || (d.bills.notes ?? []).length > 0) ? (
          <Card>
            <H>Bills — paid vs planned</H>
            {/* the charges the figure holds, as the server counts them.
                older-server path: stands in for `posted_counts` */}
            {/* the due-dated bills (`bills_*`, the web's card); the pools
                are the set-aside line below. older-server path: stands in
                for `bills_posted` / `bills_planned` / `bills_awaiting` —
                the card's combined figures */}
            <KV k={`Posted (${d.bills.bills_posted_counts
                    ?? d.bills.posted_counts
                    ?? (d.bills.paid ?? []).length})`}
                v={money(d.bills.bills_posted ?? d.bills.posted, false)} />
            <KV k="Planned" v={money(d.bills.bills_planned ?? d.bills.planned,
                                     false)} tone="mut" />
            {(d.bills.bills_awaiting ?? d.bills.awaiting) > 0 && (
              <KV k="Awaiting" v={money(d.bills.bills_awaiting
                                        ?? d.bills.awaiting, false)}
                  tone="warn" />)}
            {/* the plan's share of the pools, said apart from the bills
                plan, and the plan the two make together (web: MonthLens).
                older-server path: stands in for `set_aside` — nothing, and
                "Planned" above is the whole plan */}
            {d.bills.set_aside != null
              && (d.bills.envelopes_set_aside ?? 0) > 0 && (
              <KV k="Envelopes set aside"
                  v={`${money(d.bills.envelopes_set_aside ?? 0, false)} · `
                     + `${money(d.bills.set_aside, false)} planned in all`}
                  tone="mut" />)}
            {/* a closed month's own lines, as the server wrote them
                (web: MonthLens) */}
            {/* keyed by place: a weekly bill unpaid on two dates sends
                the same line twice */}
            {(d.bills.notes ?? []).map((n, i) => (
              <Text key={i} style={s.mut}>{n}</Text>
            ))}
            {d.bills.overdue.length > 0 && (
              <Text style={s.mut}>Scheduled but not posted:</Text>
            )}
            {d.bills.overdue.slice(0, 5).map(([payee, amt, due, gone]) => (
              // a charge the bank feed withdrew is a warning, not a miss
              <KV key={payee}
                  k={gone ? `${payee} · withdrawn by the bank feed ${mmdd(gone)}`
                          : `${payee} · due ${mmdd(due)}`}
                  v={money(amt, false)} tone={gone ? "warn" : "bad"} />
            ))}
            {(d.bills.envelopes ?? []).map((e) => (
              <KV key={e.payee}
                  k={`${e.payee} (envelope)`}
                  v={e.pair_text != null
                    // the pool's line as the server prints it (the web's
                    // words): its period's spend, the pool, and the spill
                    // counted in spending over the same period
                    ? e.pair_text
                    // older-server path: stands in for `pair_text`
                    : `${e.pair_spent != null
                      // the pool's pair: everything its period charged of
                      // the pool (the web's words)
                      ? `${money(e.pair_spent, false)} of ${money(e.pair_of ?? 0, false)}${
                          e.shown_period ? ` ${e.shown_period}` : ""}`
                      // older-server path: stands in for `pair_spent`
                      : e.shown_used !== undefined
                      ? `${money(e.shown_used, false)} of ${money(e.shown_of ?? 0, false)}${
                          e.shown_period ? ` ${e.shown_period}` : ""}`
                      // older-server path: stands in for `shown_used` / `shown_of`
                      : (e.period_months ?? 1) > 1
                        ? `${money(e.period_used ?? e.used, false)} of ${money(e.pool ?? e.monthly, false)}`
                        : `${money(e.used, false)} of ${money(e.monthly, false)}`}${
                      (e.pair_past ?? e.overflow) > 0.005
                        ? ` · ${money(e.pair_past ?? e.overflow, false)} past the pool, counted in spending`
                        : ""}`}
                  // red by the line's own spill, the figure it prints
                  tone={(e.pair_past ?? e.overflow) > 0.005 ? "bad" : "mut"} />
            ))}
            {(d.bills.paid ?? []).map((p, i) => (
              <KV key={`${p.payee}-${i}`}
                  k={`${mmdd(p.date)} · ${p.payee} · paid ✓`}
                  v={money(p.amount, false)} tone="mut" />
            ))}
          </Card>
          ) : (
            <Text style={s.mut2}>No tracked bill activity this month.</Text>
          )}
          {/* the web's cards: an empty one says so rather than vanish */}
          {!noData && d.by_category.length === 0 && (
            <Card>
              <H>{d.status === "in_progress"
                ? "Month to date by category" : "Spending by category"}</H>
              <Text style={s.mut}>{EMPTY_LINES.noSpending}</Text>
            </Card>
          )}
          {d.by_category.length > 0 && (
            <Card>
              <H>{d.status === "in_progress"
                ? "Month to date by category" : "Spending by category"}</H>
              {d.by_category.map(([name, v, key]) => (
                <KV key={name}
                    // the web's Amazon row: the itemised bucket with its
                    // count, never a filter to open
                    k={name === "Amazon"
                      ? `Amazon (${(d.amazon_subs ?? []).length})`
                      : catLabel(name)}
                    v={money(v, false)} tone="mut"
                    // the row carries the STORED key beside its display
                    // name; the display form ("FOOD AND DRINK") matches
                    // nothing server-side. No key means no single
                    // category stands behind the number — the clustered
                    // Amazon parent — so that row is plain text.
                    onPress={key && name !== "Amazon" ? () => router.push({
                      pathname: "/(tabs)/transactions",
                      params: { cat: key, y: String(ym.y),
                                m: String(ym.m), counted: "1" } } as never)
                      : undefined} />
              ))}
            </Card>
          )}
          {/* a month with no rows has its line above and no card here
              (the web's rule) */}
          {!noData && d.biggest.length === 0 && (
            <Card>
              <H>Biggest transactions</H>
              <Text style={s.mut}>{EMPTY_LINES.noSpendingPosted}</Text>
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
                      {mmdd(t.date)} · {catLabel(t.category)}
                      {t.account ? ` · ${t.account}` : ""}
                    </Text>
                  </View>
                  <Text style={s.bigAmt}>{signedMoney(-t.amount, (n) => money(n, false))}</Text>
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
  // a fingertip in each direction: the arrows are the screen's main control
  navBtn: { paddingHorizontal: 18, paddingVertical: 4, minHeight: 44,
            minWidth: 44, alignItems: "center", justifyContent: "center" },
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
