// The figures behind a chart, folded under a "Show numbers" button (the
// decided wording, specs/accessibility.md) — the picture is never the
// only way to read a number. The web twin is webapp/src/components/
// ChartText.tsx. The table is built only once it is opened.
import { useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import type { ChartTable } from "../lib/chart-summary";
import { C } from "../lib/theme";

export function ShowNumbers({ table }: { table: () => ChartTable }) {
  const [open, setOpen] = useState(false);
  const t = open ? table() : null;
  return (
    <View style={{ marginTop: 6 }}>
      <Pressable accessibilityRole="button" accessibilityState={{ expanded: open }}
                 onPress={() => setOpen((o) => !o)} hitSlop={12}
                 style={{ alignSelf: "flex-start" }}>
        <Text style={s.toggle}>{open ? "Hide numbers" : "Show numbers"}</Text>
      </Pressable>
      {t && <DataTable columns={t.columns} rows={t.rows} />}
    </View>
  );
}

export function DataTable({ columns, rows }: { columns: string[]; rows: string[][] }) {
  return (
    <View style={{ marginTop: 6 }}>
      <View style={s.tr}>
        {columns.map((c, i) => (
          <Text key={i} style={[s.th, i > 0 && s.num]} numberOfLines={1}>{c}</Text>))}
      </View>
      {rows.map((r, ri) => (
        <View key={ri} style={s.tr}>
          {r.map((c, ci) => (
            // a figure wraps rather than being cut short at large text
            <Text key={ci} style={[s.td, ci > 0 && s.num]}
                  numberOfLines={ci > 0 ? undefined : 2}>{c}</Text>))}
        </View>
      ))}
    </View>
  );
}

const s = StyleSheet.create({
  toggle: { color: C.mut, fontSize: 12 },
  tr: { flexDirection: "row", borderBottomWidth: 1, borderBottomColor: C.border,
        paddingVertical: 4, gap: 6 },
  th: { flex: 1, color: C.mut, fontSize: 11, fontWeight: "600" },
  td: { flex: 1, color: C.text, fontSize: 12 },
  num: { textAlign: "right", fontVariant: ["tabular-nums"] },
});
