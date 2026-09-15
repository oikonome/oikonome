// The money map — the web MoneyMap.tsx, native. Every row is a gauge of
// ITS OWN budget (full track = the row's budget); overspend renders PAST
// a solid budget divider, never absorbed into a longer bar; today's pace
// is the web's post tick — a 2px ink line through the bar, overshooting
// it 3px top and bottom. The web's striped overflow needs a CSS
// gradient, so here it is a solid red segment past the divider — the
// inline "over by $X" text and the legend carry the same meaning.
import { useRouter } from "expo-router";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { C, money as money$ } from "../lib/theme";

// the web map prints whole dollars (its money() default) — cents live on
// transaction/recurring surfaces only
const money = (v: number) => money$(v, false);

export interface PlanBucket {
  actual: number; expected: number; month_budget: number;
  children?: { name: string; actual: number; expected: number;
               month_budget: number }[];
  display_actual?: number; display_expected?: number;
  remaining_budget?: number;
}
// the web's PlanRow.to: where the row's transactions are
/** The transactions screen with the filters a row's `to` names
 *  ("bucket=…&y=…&m=…"). Mobile copies the web's deep link, as params. */
export function ledgerRoute(to: string) {
  const params: Record<string, string> = {};
  for (const kv of to.split("&")) {
    if (!kv) continue;
    // a bucket is named by the household, so its name can contain "=" —
    // split on the FIRST one only, or "Rent = home" loses its tail
    const eq = kv.indexOf("=");
    const k = eq < 0 ? kv : kv.slice(0, eq);
    const raw = eq < 0 ? "" : kv.slice(eq + 1);
    let v = raw;
    // a bare "%" in a bucket name makes decodeURIComponent throw, and a
    // thrown link is a screen that never opens; the raw text is then the
    // best answer available
    try { v = decodeURIComponent(raw); } catch { v = raw; }
    params[k] = v;
  }
  return { pathname: "/(tabs)/transactions", params };
}

export interface PlanRow {
  to?: string;
  label: string; actual: number; expected: number; budget: number;
  pending?: number; caption?: string; judged: boolean;
  // header override for the actual side — "—" when nothing is measurable
  actualLabel?: string;
}

/** Plan rows in the wizard's order: categories (Food first, then the
 *  carve-outs), Unallocated, Bills, Savings — webapp planRows(), same
 *  wire semantics. */
export function planRows(
  buckets: Record<string, PlanBucket>,
  opts: { fixedActual?: number; fixedBudget?: number;
          fixedPending?: number;
          savings?: { plan: number; saved: number | null } | null;
          excess?: number | null;
          // the month the rows cover; the ledger link opens that month
          period?: { y: number; m: number } | null } = {},
): PlanRow[] {
  const rows: PlanRow[] = [];
  // A plan row's rows are the rows the MONTH VERDICT counts in that
  // bucket, which no category filter can express: the Food number also
  // counts store rows overridden to food, a carve-out is a plan label
  // rather than a stored category, and "Everything else" is everything
  // the other rows did not claim. So the link names the bucket and the
  // server answers with exactly the rows behind the number. A bucket is
  // only defined for a month, so a map with no period (the yearly and
  // budget maps) has no link at all rather than a link to the wrong set.
  const ledger = (bucket: string) =>
    opts.period
      ? `bucket=${encodeURIComponent(bucket)}&y=${opts.period.y}&m=${
          opts.period.m}`
      : undefined;
  const food = buckets.food;
  const other = buckets.other;
  if (food) {
    const kids = food.children ?? [];
    rows.push({
      label: "Food", judged: true, to: ledger("food"),
      actual: kids.length ? (food.display_actual ?? food.actual) : food.actual,
      expected: kids.length ? (food.display_expected ?? food.expected)
                            : food.expected,
      budget: kids.length ? (food.remaining_budget ?? food.month_budget)
                          : food.month_budget,
    });
    for (const c of kids)
      rows.push({ label: c.name, actual: c.actual, expected: c.expected,
                  budget: c.month_budget, judged: true, to: ledger(c.name) });
  }
  if (other) {
    for (const c of other.children ?? [])
      rows.push({ label: c.name, actual: c.actual, expected: c.expected,
                  budget: c.month_budget, judged: true, to: ledger(c.name) });
    const kids = other.children ?? [];
    rows.push({
      label: "Everything else", judged: true, to: ledger("other"),
      caption: kids.length ? "unallocated — spending outside the categories"
                           : undefined,
      actual: kids.length ? (other.display_actual ?? other.actual)
                          : other.actual,
      expected: kids.length ? (other.display_expected ?? other.expected)
                            : other.expected,
      budget: kids.length ? (other.remaining_budget ?? other.month_budget)
                          : other.month_budget,
    });
  }
  // bills + savings are IN the plan but never in the verdict
  if (opts.fixedBudget !== undefined || opts.fixedActual !== undefined) {
    const posted = opts.fixedActual ?? 0;
    rows.push({
      label: "Bills", judged: false, actual: posted,
      expected: buckets.fixed?.expected ?? 0,
      budget: opts.fixedBudget ?? buckets.fixed?.month_budget ?? 0,
      pending: opts.fixedPending,
    });
  }
  if (opts.savings
      && (opts.savings.plan > 0 || (opts.savings.saved ?? 0) > 0)) {
    // plan-only goal (no destination account) — no transfers to measure,
    // so the bar carries the plan alone
    const planOnly = opts.savings.saved === null;
    rows.push({
      label: "Savings / Investment", judged: false,
      actual: opts.savings.saved ?? 0, expected: opts.savings.plan,
      budget: opts.savings.plan,
      actualLabel: planOnly ? "—" : undefined,
      caption: planOnly ? undefined : "transferred vs plan",
    });
  }
  // the plan's residual — income the plan deliberately leaves unassigned
  if (opts.excess != null && opts.excess > 0.005) {
    rows.push({
      label: "Excess cash", judged: false,
      actual: 0, actualLabel: "—",
      expected: opts.excess, budget: opts.excess,
    });
  }
  return rows;
}

/** Right-side status text so over/under is never color-alone. */
function rowDelta(row: PlanRow): { text: string; color: string } | null {
  if (row.budget <= 0.005 || row.actualLabel === "—") return null;
  const pend = row.pending ?? 0;
  const left = row.budget - row.actual - pend;
  if (row.actual > row.budget + 0.5)
    return { text: `over by ${money(row.actual - row.budget)}`,
             color: C.bad };
  if (row.judged && row.actual > row.expected + 0.5)
    return { text: `${money(row.actual - row.expected)} ahead of pace`,
             color: C.bad };
  if (row.judged)
    return { text: `${money(left)} left`, color: C.good };
  if (row.label === "Bills" && (left > 0.5 || pend > 0.5))
    return { text: (pend > 0.5 ? `${money(pend)} awaiting · ` : "")
               + `${money(Math.max(left, 0))} still due`,
             color: C.mut };
  return null;
}

/** The bar: segments are % of the row's own budget (or of the actual when
 *  over). Overspend sits PAST a 2px budget divider; the post tick marks
 *  today's pace on judged rows. */
export function Track({ row, paceLabel = false }:
    { row: PlanRow; paceLabel?: boolean }) {
  const unbudgeted = row.budget <= 0.005 && row.actual > 0.005;
  const extent = Math.max(row.budget, row.actual + (row.pending ?? 0), 1);
  const w = (v: number) => `${(100 * v) / extent}%` as const;
  const isover = row.judged ? row.actual > row.expected + 0.5
                            : row.actual > row.budget + 0.5;
  const spent = Math.min(row.actual, row.budget);
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
            today&apos;s pace
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
  return (
    <View style={{ marginVertical: 5,
                   marginLeft: indent ? 18 : 0 }}>
      <View style={s.head}>
        {row.to !== undefined
          ? <Pressable onPress={() => router.push(ledgerRoute(row.to!) as never)}
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
      ) : row.caption ? (
        <Text style={s.caption}>{row.caption}</Text>
      ) : null}
    </View>
  );
}

/** The plan's rows, each a gauge of its own budget. Judged (variable)
 *  rows sum into a "Variable spending" header and sit indented under it;
 *  Bills / Savings stay flat. Plan-only rows fold into one muted footer
 *  sentence instead of empty bars. */
export default function MoneyMap({ rows, footnote, total: totalProp }: {
  rows: PlanRow[];
  footnote?: React.ReactNode;
  /** server-computed header row (the Budget page passes the real
   *  variable budget/actual instead of a row-sum) */
  total?: PlanRow | null;
}) {
  const router = useRouter();
  const onBudget = () => router.push("/budget" as never);
  const measurable = rows.filter((r) => r.actualLabel !== "—");
  const folded = rows.filter((r) => r.actualLabel === "—");
  const judged = measurable.filter((r) => r.judged);
  const unjudged = measurable.filter((r) => !r.judged);
  const total: PlanRow | null = totalProp !== undefined ? totalProp
    : judged.length > 1 ? {
        label: "Variable spending", judged: true,
        actual: judged.reduce((s0, r) => s0 + r.actual, 0),
        expected: judged.reduce((s0, r) => s0 + r.expected, 0),
        budget: judged.reduce((s0, r) => s0 + r.budget, 0),
      } : null;
  const shown = [...(total ? [total] : []), ...judged, ...unjudged];
  const hasOverflow = shown.some((r) => r.actual > r.budget + 0.5);
  const hasPending = shown.some((r) => (r.pending ?? 0) > 0.5);
  const foldedBits = folded.map((r) =>
    r.label === "Excess cash"
      ? `${money(r.budget)} excess stays in checking`
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
