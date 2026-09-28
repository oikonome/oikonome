// A drop-down for the ledger's filters — the web's <select> without a
// native module: a field that names the current choice and opens a
// sheet of options, one tap per choice. A long list (categories,
// accounts) gets a filter box; grouped options carry the web's optgroup
// headers. The field reads like the date field beside it — the same
// box, the same ▾, the same ✕ to clear without opening it.
import { useState } from "react";
import { Pressable, StyleSheet, Text, TextInput, View } from "react-native";

import Sheet, { sheetStyles as b } from "./sheet";
import { C } from "../lib/theme";

export type SelectOption = {
  value: string; label: string;
  // the optgroup header the option sits under (options with the same
  // group must be adjacent)
  group?: string;
  // drawn faded — an archived account is still choosable, just quieter
  dim?: boolean;
};

export default function Select({ title, placeholder, value, options,
                                 onChange, filter }: {
  title: string;
  // what the field says when nothing is chosen ("all accounts") — also
  // the first option in the sheet, because "all" is a real choice
  placeholder: string;
  value: string; options: SelectOption[];
  onChange: (value: string) => void;
  // offer a filter box — for a list long enough to scroll
  filter?: boolean }) {
  const [open, setOpen] = useState(false);
  const [needle, setNeedle] = useState("");
  const chosen = options.find((o) => o.value === value);
  const n = needle.trim().toLowerCase();
  const shown = n ? options.filter((o) => o.label.toLowerCase().includes(n))
                  : options;
  const pick = (v: string) => { onChange(v); setOpen(false); setNeedle(""); };
  const close = () => { setOpen(false); setNeedle(""); };
  return (
    <>
      <Pressable style={s.field} onPress={() => setOpen(true)}
                 accessibilityRole="button" accessibilityLabel={title}>
        <Text style={{ color: value ? C.text : C.mut, fontSize: 15, flex: 1 }}
              numberOfLines={1}>
          {chosen?.label ?? (value || placeholder)}
        </Text>
        {value ? (
          <Text style={{ color: C.mut, fontSize: 15, paddingLeft: 8 }}
                onPress={() => onChange("")}
                accessibilityLabel={`clear ${title}`}>✕</Text>
        ) : (
          <Text style={{ color: C.mut, fontSize: 12 }}>▾</Text>
        )}
      </Pressable>
      <Sheet visible={open} onClose={close} title={title}
             footer={
               <View style={b.footer}>
                 <Pressable style={[b.btn, b.btnQuiet]} onPress={close}>
                   <Text style={b.btnQuietText}>Close</Text>
                 </Pressable>
               </View>}>
        {filter && (
          <TextInput style={s.input} placeholder="filter…"
                     placeholderTextColor={C.mut} autoCapitalize="none"
                     autoCorrect={false} value={needle}
                     onChangeText={setNeedle} />
        )}
        {!n && (
          <Row label={placeholder} on={!value} onPress={() => pick("")} />
        )}
        {shown.map((o, i) => (
          <View key={o.value}>
            {o.group && o.group !== shown[i - 1]?.group && (
              <Text style={s.group}>{o.group}</Text>
            )}
            <Row label={o.label} on={o.value === value} dim={o.dim}
                 onPress={() => pick(o.value)} />
          </View>
        ))}
        {n && shown.length === 0 && (
          <Text style={{ color: C.mut, fontSize: 13, paddingVertical: 10 }}>
            Nothing matches.
          </Text>
        )}
      </Sheet>
    </>
  );
}

function Row({ label, on, dim, onPress }: {
  label: string; on: boolean; dim?: boolean; onPress: () => void }) {
  return (
    <Pressable style={s.row} onPress={onPress} accessibilityRole="button"
               accessibilityState={{ selected: on }}>
      <Text style={{ color: on ? C.accent : C.text, fontSize: 15, flex: 1,
                     fontWeight: on ? "700" : "400",
                     opacity: dim ? 0.75 : 1 }}
            numberOfLines={1}>
        {label}
      </Text>
      {on && <Text style={{ color: C.accent, fontSize: 15 }}>✓</Text>}
    </Pressable>
  );
}

const s = StyleSheet.create({
  field: { flexDirection: "row", alignItems: "center",
           backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
           borderRadius: 10, paddingHorizontal: 12, paddingVertical: 9 },
  input: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
           borderRadius: 10, color: C.text, fontSize: 15,
           paddingHorizontal: 12, paddingVertical: 9, marginBottom: 4 },
  row: { flexDirection: "row", alignItems: "center", gap: 8,
         paddingVertical: 10, borderTopColor: C.border,
         borderTopWidth: StyleSheet.hairlineWidth },
  group: { color: C.mut, fontSize: 11, fontWeight: "700",
           letterSpacing: 0.6, textTransform: "uppercase",
           marginTop: 10, marginBottom: 2 },
});
