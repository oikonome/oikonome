// The money map — the web MoneyMap.tsx, native. Every row is a gauge of
// ITS OWN budget (full track = the row's budget); overspend renders PAST
// a solid budget divider, never absorbed into a longer bar; today's pace
// is the web's post tick — a 2px ink line through the bar, overshooting
// it 3px top and bottom. The web's striped overflow needs a CSS
// gradient, so here it is a solid red segment past the divider — the
// inline "over by $X" text and the legend carry the same meaning.
import { useRouter } from "expo-router";
import { Pressable, StyleSheet, Text, View } from "react-native";

import type { PlanTotal, RowStatus, ServerPlanRow } from "../lib/api";
import { BillsAddsUp } from "./bills-table";
import { ledgerParams, terseCaption } from "../lib/pure";
import { C, money as money$ } from "../lib/theme";

// the web map prints whole dollars (its money() default) — cents live on
// transaction/recurring surfaces only
const money = (v: number) => money$(v, false);

// the web's PlanRow.to: where the row's transactions are
/** The transactions screen with the filters a row's `to` names — the
 *  server's path ("/transactions?bucket=…&y=…&m=…") or a bare query
 *  ("bucket=…&y=…", the tiles' links). Mobile copies the web's deep
 *  link, as params (ledgerParams). */
export function ledgerRoute(to: string) {
  return { pathname: "/(tabs)/transactions", params: ledgerParams(to) };
}

export interface PlanRow {
  to?: string;
  label: string; actual: number; expected: number; budget: number;
  pending?: number; caption?: string; judged: boolean;
  // header override for the actual side — "—" when nothing is measurable
  actualLabel?: string;
  // what the row says beside its figures (todayview.row_status); null or
  // absent (an older server) says nothing
  status?: RowStatus | null;
  // the Bills row's parts and the table they open (bills_breakdown);
  // absent from an older server, which sends no table to open
  breakdown?: ServerPlanRow["breakdown"];
}

/** The plan rows the server built (Today's payload and the Month and Year
 *  lens replies, `plan_rows`), in the shape the map draws — nothing is
 *  computed here. */
export function sentPlanRows(rows: readonly ServerPlanRow[]): PlanRow[] {
  return rows.map((r) => ({
    label: r.label, judged: r.judged, actual: r.actual,
    expected: r.expected, budget: r.budget,
    pending: r.pending || undefined, to: r.to ?? undefined,
    actualLabel: r.actual_label ?? undefined,
    caption: r.caption ?? undefined,
    status: r.status,
    breakdown: r.breakdown,
  }));
}

/** The "Variable spending" row the server sums over the judged rows
 *  (todayview.plan_total), with its status — the web's sentPlanTotal. */
export function sentPlanTotal(t: PlanTotal | null | undefined)
  : PlanRow | null {
  return t ? { label: t.label, judged: true, actual: t.actual,
               expected: t.expected, budget: t.budget, status: t.status }
           : null;
}

/** Right-side status text so over/under is never color-alone: the row's
 *  status (todayview.row_status) in the words and tone the server wrote,
 *  as on the web. A server from before the words were sent says nothing
 *  here rather than this screen composing them. */
const TONE_COLOR = { neg: C.bad, pos: C.good, mut: C.mut } as const;
function rowDelta(row: PlanRow): { text: string; color: string } | null {
  const st = row.status;
  if (!st || !st.text) return null;
  return { text: st.text, color: TONE_COLOR[st.tone] ?? C.mut };
}

/** The bar: segments are % of the row's own budget (or of the actual when
 *  over). Overspend sits PAST a 2px budget divider; the post tick marks
 *  today's pace on judged rows. */
export function Track({ row, paceLabel = null }:
    // the tick's label as the server words it (null: none)
    { row: PlanRow; paceLabel?: string | null }) {
  const unbudgeted = row.budget <= 0.005 && row.actual > 0.005;
  const extent = Math.max(row.budget, row.actual + (row.pending ?? 0), 1);
  const w = (v: number) => `${(100 * v) / extent}%` as const;
  const isover = row.judged ? row.actual > row.expected + 0.5
                            : row.actual > row.budget + 0.5;
  // a month that took money back out of savings reads a negative actual:
  // nothing is filled, and the rest of the bar is still only the budget
  const spent = Math.max(0, Math.min(row.actual, row.budget));
  const pend = Math.min(row.pending ?? 0, Math.max(row.budget - spent, 0));
  const over = Math.max(row.actual - row.budget, 0);
  const remaining = Math.max(row.budget - spent - pend, 0);
  const pace = row.judged && row.expected > 0 && row.expected < extent - 0.5
    ? (100 * row.expected) / extent : null;
  return (
    <View>
      {/* vertical padding gives the tick its 3px overshoot without
          negative offsets (Android clips children that overflow) */}
      <View style={{ paddingVertical: 3 }}>
        <View style={s.track}>
        {unbudgeted ? (
          <View style={{ width: "100%", backgroundColor: C.warn,
                         opacity: 0.45 }} />
        ) : (
          <>
            {spent > 0.005 && (
              <View style={{ width: w(spent),
                             backgroundColor: isover ? C.bad : C.good }} />
            )}
            {pend > 0.005 && (
              <View style={{ width: w(pend), backgroundColor: C.warn }} />
            )}
            {remaining > 0.005 && (
              // "light = still coming out of checking" — the remainder is
              // visible money, not empty track
              <View style={{ width: w(remaining), backgroundColor: C.good,
                             opacity: 0.22 }} />
            )}
            {over > 0.005 && (
              <>
                {/* the budget divider — overspend is PAST it */}
                <View style={{ width: 2, backgroundColor: C.text }} />
                <View style={{ width: w(over), backgroundColor: C.bad }} />
              </>
            )}
          </>
        )}
        </View>
        {pace !== null && (
          <View style={[s.tick, { left: `${Math.min(99, pace)}%` }]} />
        )}
      </View>
      {pace !== null && paceLabel && (
        <View style={{ position: "relative", height: 14 }}>
          <Text style={{ position: "absolute",
                         left: `${Math.min(85, pace)}%`,
                         color: C.mut, fontSize: 10 }}>
            {paceLabel}
          </Text>
        </View>
      )}
    </View>
  );
}

function MapBar({ row, indent = false, onBudget }:
    { row: PlanRow; indent?: boolean; onBudget?: () => void }) {
  const router = useRouter();
  const unbudgeted = row.budget <= 0.005 && row.actual > 0.005;
  const delta = rowDelta(row);
  const caption = terseCaption(row.label, row.caption, row.breakdown);
  return (
    <View style={{ marginVertical: 5,
                   marginLeft: indent ? 18 : 0 }}>
      <View style={s.head}>
        {row.to !== undefined
          ? <Pressable accessibilityRole="button"
                       onPress={() => router.push(ledgerRoute(row.to!) as never)}
                       style={({ pressed }) => [s.labelPress, pressed && s.labelPressed]}>
              {({ pressed }) => (
                <Text style={[s.label, pressed && { color: C.accent }]}
                      numberOfLines={1}>{row.label}</Text>)}
            </Pressable>
          : <Text style={s.label} numberOfLines={1}>{row.label}</Text>}
        <Text style={s.nums}>
          {row.actualLabel ?? money(row.actual)} /{" "}
          {unbudgeted ? "—" : money(row.budget)}
          {delta ? (
            <Text style={{ color: delta.color, fontWeight: "600" }}>
              {"  "}{delta.text}
            </Text>
          ) : null}
        </Text>
      </View>
      <Track row={row} />
      {unbudgeted ? (
        <Text style={[s.caption, { color: C.warn }]}
              onPress={onBudget}>
          no budget set —{" "}
          <Text style={{ color: C.accent }}>set one</Text> so this
          counts toward the verdict
        </Text>
      ) : caption ? (
        <Text style={s.caption}>{caption}</Text>
      ) : null}
      {/* the Bills row opens how its figures add up, as on the web, only
          when the server sends the table */}
      <BillsAddsUp breakdown={row.breakdown} />
    </View>
  );
}

/** The plan's rows, each a gauge of its own budget. Judged (variable)
 *  rows sum into a "Variable spending" header and sit indented under it;
 *  Bills / Savings stay flat. Plan-only rows fold into one muted footer
 *  sentence instead of empty bars. */
export default function MoneyMap({ rows, footnote, total: totalRow }: {
  rows: PlanRow[];
  footnote?: React.ReactNode;
  /** the "Variable spending" row over the judged rows — the server's
   *  (todayview.plan_total) on Today, Budget and both lenses; null (an
   *  older server) draws none */
  total: PlanRow | null;
}) {
  const router = useRouter();
  const onBudget = () => router.push("/budget" as never);
  const measurable = rows.filter((r) => r.actualLabel !== "—");
  const folded = rows.filter((r) => r.actualLabel === "—");
  const judged = measurable.filter((r) => r.judged);
  const unjudged = measurable.filter((r) => !r.judged);
  // the header row stands over the judged rows only when there are two or
  // more of them to stand over (as on the web)
  const total = judged.length > 1 ? totalRow : null;
  const shown = [...(total ? [total] : []), ...judged, ...unjudged];
  const hasOverflow = shown.some((r) => r.actual > r.budget + 0.5);
  const hasPending = shown.some((r) => (r.pending ?? 0) > 0.5);
  // the excess row's words are the server's (todayview.map_rows)
  const foldedBits = folded.map((r) =>
    r.label === "Excess cash"
      ? (r.caption !== undefined
          ? r.caption
          // older-server path: stands in for `caption`
          : `${money(r.budget)} excess stays in checking`)
      : `${r.label.toLowerCase()} plan ${money(r.budget)}/mo (plan only)`);
  return (
    <View>
      {total && <MapBar row={total} onBudget={onBudget} />}
      {judged.map((r) => (
        <MapBar key={r.label} row={r} indent={!!total}
                onBudget={onBudget} />
      ))}
      {unjudged.map((r) =>
        <MapBar key={r.label} row={r} onBudget={onBudget} />)}
      {(hasOverflow || hasPending) && (
        <Text style={s.legend}>
          {hasOverflow && "past the divider is over the budget"}
          {hasOverflow && hasPending && " · "}
          {hasPending && "amber is awaiting"}
        </Text>
      )}
      {foldedBits.length > 0 && (
        <Text style={[s.legend, { fontSize: 12 }]}>
          Also this month: {foldedBits.join(" · ")}
        </Text>
      )}
      {footnote}
    </View>
  );
}

const s = StyleSheet.create({
  // 14px like the hero rows' Meter big — one bar height on the whole
  // Today page; the map's bars read smaller on a phone
  track: { flexDirection: "row", backgroundColor: C.border, borderRadius: 7,
           height: 14, overflow: "hidden" },
  // the "today" post tick — spans the padded wrapper, so it overshoots
  // the track 3px top and bottom; marginLeft centers the 2px line
  tick: { position: "absolute", top: 0, bottom: 0, width: 2,
          borderRadius: 1, backgroundColor: C.text, marginLeft: -1 },
  head: { flexDirection: "row", justifyContent: "space-between", gap: 8 },
  label: { color: C.text, fontSize: 13, flexShrink: 1 },
  labelPress: { marginLeft: -4, paddingHorizontal: 4, borderRadius: 6 },
  labelPressed: { backgroundColor: C.hover },
  nums: { color: C.mut, fontSize: 12, fontVariant: ["tabular-nums"] },
  caption: { color: C.mut, fontSize: 11, marginTop: 1 },
  legend: { color: C.mut, fontSize: 11, marginTop: 6 },
});
