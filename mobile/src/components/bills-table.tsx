// "How this adds up" — the Bills row's table, folded under the row the way
// a chart folds its numbers (show-numbers.tsx): a button that opens it in
// place. The web twin is webapp/src/components/BillsTable.tsx. The rows
// are the server's (bills_breakdown.table): each a label and its signed
// figure, the last the plan they add up to, and below it any bill late
// from an earlier month, which is still due and in none of the plan. The
// Envelopes row opens its pools the same way (envelope_breakdown.table,
// "Each pool"): each pool's line and the plan's set-aside, a list, not a
// sum. Nothing here adds or words a figure.
import { useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { billsTableLines, type BillsTableLine } from "../lib/pure";
import type { ServerPlanRow } from "../lib/api";
import { C } from "../lib/theme";

/** The button and the table, for a row whose reply carries one; nothing
 *  otherwise (billsTableLines). */
export function BillsAddsUp({ breakdown }:
    { breakdown: ServerPlanRow["breakdown"] }) {
  const [open, setOpen] = useState(false);
  const table = billsTableLines(breakdown);
  if (!table) return null;
  return (
    <View style={{ marginTop: 4 }}>
      <Pressable accessibilityRole="button" accessibilityState={{ expanded: open }}
                 onPress={() => setOpen((o) => !o)} hitSlop={12}
                 style={{ alignSelf: "flex-start" }}>
        <Text style={s.toggle}>
          {open ? table.toggle[1] : table.toggle[0]}</Text>
      </Pressable>
      {open && (
        <View style={{ marginTop: 6 }}>
          {table.rows.map((r, i) => <Line key={i} r={r} />)}
          {table.heading && (
            <>
              <Text style={s.th}>{table.heading}</Text>
              {table.outside.map((r, i) => <Line key={i} r={r} />)}
            </>
          )}
        </View>
      )}
    </View>
  );
}

function Line({ r }: { r: BillsTableLine }) {
  const total = r.total;
  return (
    <View style={s.tr}>
      {/* a label wraps rather than being cut short at large text */}
      <Text style={[s.td, total && s.total]}>{r.label}</Text>
      <Text style={[s.td, s.num, total && s.total]}>{r.figure}</Text>
    </View>
  );
}

const s = StyleSheet.create({
  toggle: { color: C.mut, fontSize: 12 },
  tr: { flexDirection: "row", borderBottomWidth: 1, borderBottomColor: C.border,
        paddingVertical: 4, gap: 6 },
  th: { color: C.mut, fontSize: 11, fontWeight: "600", marginTop: 10,
        marginBottom: 2 },
  td: { flex: 1, color: C.text, fontSize: 12 },
  num: { flex: 0, textAlign: "right", fontVariant: ["tabular-nums"] },
  total: { fontWeight: "600" },
});
