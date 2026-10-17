// The setup wizard's shared pieces: the option card (Appearance, the
// bills question), the pill row (history, delivery) and the one style
// sheet the panes draw with. Every target is at least 44pt tall and the
// cards stack in one column, so each step works with one thumb and never
// scrolls sideways.
import type { ReactNode } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";
import { C } from "../lib/theme";

export const TAP = 44;

/** One choice of a stacked set: title, an optional preview, a caption,
 *  and the web's corner tag (SELECTED / RECOMMENDED). */
export function OptionCard({ title, caption, tag, selected, onPress,
                             disabled, children }: {
  title: string; caption: string; tag?: string | null; selected: boolean;
  onPress: () => void; disabled?: boolean; children?: ReactNode;
}) {
  return (
    <Pressable onPress={onPress} disabled={disabled}
               accessibilityRole="radio"
               accessibilityState={{ selected, disabled }}
               style={[ws.opt, selected && ws.optOn,
                       disabled && { opacity: 0.5 }]}>
      <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
        <Text style={[ws.optTitle, { flex: 1 }]}>{title}</Text>
        {tag ? <Text style={ws.tag}>{tag}</Text> : null}
      </View>
      {children}
      <Text style={ws.small}>{caption}</Text>
    </Pressable>
  );
}

/** A wrapping row of pills, one selected. */
export function PillRow<T>({ choices, value, onPick, disabled, label }: {
  choices: [T, string][]; value: T; onPick: (v: T) => void;
  disabled?: (v: T) => boolean; label?: string;
}) {
  return (
    <View style={ws.pills} accessibilityRole="radiogroup"
          accessibilityLabel={label}>
      {choices.map(([v, text]) => {
        const on = v === value;
        const off = disabled?.(v) ?? false;
        return (
          <Pressable key={text} onPress={() => onPick(v)} disabled={off}
                     accessibilityRole="radio"
                     accessibilityState={{ selected: on, disabled: off }}
                     style={[ws.pill, on && ws.pillOn,
                             off && { opacity: 0.5 }]}>
            <Text style={[ws.pillText, on && ws.pillTextOn]}>{text}</Text>
          </Pressable>
        );
      })}
    </View>
  );
}

export const ws = StyleSheet.create({
  h2: { color: C.text, fontSize: 15, fontWeight: "700", marginTop: 4 },
  mut: { color: C.mut, fontSize: 12, lineHeight: 17, marginTop: 4 },
  small: { color: C.mut, fontSize: 12, lineHeight: 17, marginTop: 4 },
  body: { marginTop: 12, paddingTop: 12, gap: 10,
          borderTopWidth: StyleSheet.hairlineWidth,
          borderTopColor: C.border },
  section: { marginTop: 14, paddingTop: 12, gap: 6,
             borderTopWidth: StyleSheet.hairlineWidth,
             borderTopColor: C.border, borderStyle: "dashed" },
  opt: { borderWidth: 1, borderColor: C.border, borderRadius: C.r,
         padding: 12, backgroundColor: C.bg, minHeight: TAP },
  optOn: { borderColor: C.accent, borderWidth: 2, padding: 11 },
  optTitle: { color: C.text, fontSize: 14, fontWeight: "700" },
  tag: { color: C.accent, fontSize: 11, fontWeight: "700",
         letterSpacing: 0.5 },
  pills: { flexDirection: "row", flexWrap: "wrap", gap: 8, marginTop: 4 },
  pill: { borderWidth: 1, borderColor: C.border, borderRadius: 999,
          paddingHorizontal: 14, minHeight: TAP, justifyContent: "center",
          backgroundColor: C.bg },
  pillOn: { borderColor: C.accent },
  pillText: { color: C.text, fontSize: 13 },
  pillTextOn: { color: C.accent, fontWeight: "700" },
  row: { flexDirection: "row", alignItems: "center", gap: 10,
         paddingVertical: 8, borderTopWidth: StyleSheet.hairlineWidth,
         borderTopColor: C.border },
  input: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
           borderRadius: 8, color: C.text, fontSize: 14, minHeight: TAP,
           paddingHorizontal: 10, paddingVertical: 8 },
  btn: { backgroundColor: C.accent, borderRadius: 8, minHeight: TAP,
         paddingHorizontal: 14, justifyContent: "center",
         alignItems: "center" },
  quiet: { backgroundColor: C.hover, borderWidth: 1,
           borderColor: C.border },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
  chip: { fontSize: 11, paddingHorizontal: 8, paddingVertical: 2,
          borderRadius: 999, borderWidth: 1, overflow: "hidden" },
});
