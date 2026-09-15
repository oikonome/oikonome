// A tap-to-pick calendar for the ledger's date filters — the web's
// <input type="date"> without a native module: month ‹ ›, the Bills
// grid's Monday-first weeks, one tap per date. Dates travel as Y-M-D and
// read as MM/DD/YY, like every other date the app shows.
import { useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import Sheet, { sheetStyles as b } from "./sheet";
import { monthGrid, ymd } from "../lib/dates";
import { C, mmddyy } from "../lib/theme";

const MONTHS = ["January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November",
                "December"];

/** the field itself: shows the picked date or the placeholder, opens
 *  the picker; ✕ clears without opening it */
export function DateField({ value, placeholder, onPress, onClear }: {
  value: string; placeholder: string; onPress: () => void;
  onClear: () => void }) {
  return (
    <Pressable style={s.field} onPress={onPress}
               accessibilityRole="button"
               accessibilityLabel={`${placeholder} date`}>
      <Text style={{ color: value ? C.text : C.mut, fontSize: 15,
                     flex: 1, fontVariant: ["tabular-nums"] }}>
        {value ? mmddyy(value) : placeholder}
      </Text>
      {value ? (
        <Text style={{ color: C.mut, fontSize: 15, paddingLeft: 8 }}
              onPress={onClear} accessibilityLabel="clear date">✕</Text>
      ) : (
        <Text style={{ color: C.mut, fontSize: 12 }}>▾</Text>
      )}
    </Pressable>
  );
}

export default function DatePicker({ visible, title, value, min, max,
                                     onPick, onClose }: {
  visible: boolean; title: string; value: string;
  // inclusive bounds — a "to" date cannot precede the "from" date
  min?: string; max?: string;
  onPick: (ymd: string) => void; onClose: () => void }) {
  const todayIso = ymd(new Date());
  const seed = value || todayIso;
  const [y, setY] = useState(Number(seed.slice(0, 4)));
  const [m, setM] = useState(Number(seed.slice(5, 7)));
  const shift = (dir: -1 | 1) => {
    const next = m + dir;
    if (next < 1) { setY(y - 1); setM(12); }
    else if (next > 12) { setY(y + 1); setM(1); }
    else setM(next);
  };
  const allowed = (d: string) => (!min || d >= min) && (!max || d <= max);
  return (
    <Sheet visible={visible} onClose={onClose} title={title}
           footer={
             <View style={b.footer}>
               <Pressable style={[b.btn, b.btnQuiet]}
                          disabled={!allowed(todayIso)}
                          onPress={() => { onPick(todayIso); onClose(); }}>
                 <Text style={b.btnQuietText}>Today</Text>
               </Pressable>
               {value ? (
                 <Pressable style={[b.btn, b.btnQuiet]}
                            onPress={() => { onPick(""); onClose(); }}>
                   <Text style={b.btnQuietText}>Clear</Text>
                 </Pressable>
               ) : null}
               <Pressable style={[b.btn, b.btnQuiet]} onPress={onClose}>
                 <Text style={b.btnQuietText}>Close</Text>
               </Pressable>
             </View>}>
      <View style={s.nav}>
        <Pressable style={s.navBtn} onPress={() => shift(-1)}
                   accessibilityLabel="previous month">
          <Text style={s.navText}>‹</Text>
        </Pressable>
        <Text style={s.navLabel}>{MONTHS[m - 1]} {y}</Text>
        <Pressable style={s.navBtn} onPress={() => shift(1)}
                   accessibilityLabel="next month">
          <Text style={s.navText}>›</Text>
        </Pressable>
      </View>
      <View style={{ flexDirection: "row" }}>
        {["M", "T", "W", "T", "F", "S", "S"].map((w, i) => (
          <Text key={i} style={s.gridHead}>{w}</Text>
        ))}
      </View>
      {monthGrid(y, m).map((week, wi) => (
        <View key={wi} style={{ flexDirection: "row" }}>
          {week.map((c) => {
            const isToday = c.date === todayIso;
            const on = c.date === value;
            const ok = allowed(c.date);
            return (
              <Pressable key={c.date}
                         style={[s.cell, isToday && s.today, on && s.on]}
                         disabled={!ok}
                         onPress={() => { onPick(c.date); onClose(); }}
                         accessibilityLabel={mmddyy(c.date)}>
                <Text style={{ color: on ? C.text : isToday ? C.accent
                                 : c.inMonth ? C.text : C.mut,
                               fontSize: 14,
                               fontWeight: on || isToday ? "800" : "400",
                               opacity: !ok ? 0.25 : c.inMonth ? 1 : 0.5 }}>
                  {Number(c.date.slice(8, 10))}
                </Text>
              </Pressable>
            );
          })}
        </View>
      ))}
    </Sheet>
  );
}

const s = StyleSheet.create({
  field: { flexDirection: "row", alignItems: "center", flex: 1,
           backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
           borderRadius: 10, paddingHorizontal: 12, paddingVertical: 9 },
  nav: { alignItems: "center", flexDirection: "row",
         justifyContent: "space-between", marginBottom: 4 },
  navBtn: { paddingHorizontal: 18, paddingVertical: 2 },
  navText: { color: C.accent, fontSize: 24 },
  navLabel: { color: C.text, fontSize: 16, fontWeight: "600" },
  gridHead: { flex: 1, color: C.mut, fontSize: 10, textAlign: "center" },
  cell: { flex: 1, minHeight: 40, alignItems: "center",
          justifyContent: "center", borderRadius: 6, margin: 1,
          backgroundColor: C.bg },
  today: { borderColor: C.accent, borderWidth: 1 },
  on: { backgroundColor: C.accent },
});
