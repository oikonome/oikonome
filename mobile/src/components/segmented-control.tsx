// The one "pick one of these" control — the twin of the web's
// components/SegmentedControl.tsx and RangePicker, same names, same look:
// a sunken track whose chosen segment is raised and outlined (never the
// primary fill), one row that scrolls sideways instead of wrapping. Every
// screen uses this one control; do not add a local pill, chip or
// segmented variant.
import { useEffect, useRef } from "react";
import { Pressable, ScrollView, StyleSheet, Text,
         type StyleProp, type ViewStyle } from "react-native";

import { RANGE_LABEL, TREND_RANGES, type TrendRange } from "../lib/pure";
import { C } from "../lib/theme";

export type SegOption<T extends string> = {
  value: T; label: string; disabled?: boolean;
};

export default function SegmentedControl<T extends string>({
  options, value, onChange, label, size, style,
}: {
  options: readonly SegOption<T>[];
  /** null = nothing chosen (a filter whose picked value matches no option) */
  value: T | null;
  onChange: (v: T) => void;
  label?: string;
  size?: "sm";
  style?: StyleProp<ViewStyle>;
}) {
  const sm = size === "sm";
  // the chosen option stays in view: on a narrow screen the track scrolls,
  // and a choice at its far end (All, 1y) would otherwise start hidden
  const scroller = useRef<ScrollView>(null);
  const spots = useRef<Record<string, { x: number; w: number }>>({});
  const width = useRef(0);
  const reveal = () => {
    const at = value !== null ? spots.current[value] : undefined;
    if (!at || !width.current) return;
    scroller.current?.scrollTo({
      x: Math.max(0, at.x + at.w - width.current + 8), animated: false });
  };
  useEffect(reveal, [value]);
  return (
    <ScrollView horizontal showsHorizontalScrollIndicator={false}
                ref={scroller}
                onLayout={(e) => { width.current = e.nativeEvent.layout.width;
                                   reveal(); }}
                accessibilityLabel={label}
                style={[s.track, style]} contentContainerStyle={s.row}>
      {options.map((o) => {
        const on = o.value === value;
        return (
          <Pressable key={o.value} disabled={o.disabled}
                     onLayout={(e) => {
                       spots.current[o.value] = {
                         x: e.nativeEvent.layout.x,
                         w: e.nativeEvent.layout.width };
                       if (on) reveal();
                     }}
                     accessibilityRole="button"
                     accessibilityState={{ selected: on,
                                           disabled: !!o.disabled }}
                     // the pill draws 27–31pt tall; the slop reaches 44
                     hitSlop={{ top: sm ? 9 : 7, bottom: sm ? 9 : 7 }}
                     onPress={() => { if (!on) onChange(o.value); }}
                     style={[s.seg, sm && s.segSm, on && s.segOn,
                             o.disabled && { opacity: 0.38 }]}>
            <Text style={[s.txt, sm && s.txtSm, on && s.txtOn]}>
              {o.label}</Text>
          </Pressable>
        );
      })}
    </ScrollView>
  );
}

/** The site-wide timeframe toggle (This month · 1m · … · All) — the twin
 *  of the web's RangePicker. `dead` marks a window longer than the data,
 *  which would equal All. */
export function RangePicker<R extends TrendRange>({ value, onChange, dead,
                                                    style }: {
  value: R; onChange: (r: R) => void;
  dead?: (r: TrendRange) => boolean;
  style?: StyleProp<ViewStyle>;
}) {
  return (
    <SegmentedControl label="Timeframe" size="sm" style={style}
      value={value} onChange={(r) => onChange(r as R)}
      options={TREND_RANGES.map((r) => ({
        value: r, label: RANGE_LABEL[r],
        disabled: r !== "all" && !!dead?.(r),
      }))} />
  );
}

const s = StyleSheet.create({
  // flexGrow 0 + alignSelf: the track hugs its options and only scrolls
  // when they are wider than the screen; flexShrink lets it give way
  // inside a row (a header beside its title). A caller in a row passes
  // alignSelf "center" to centre it on that row.
  track: { flexGrow: 0, flexShrink: 1, alignSelf: "flex-start",
           maxWidth: "100%",
           backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
           borderRadius: 999 },
  row: { flexDirection: "row", gap: 2, padding: 2 },
  seg: { paddingHorizontal: 13, paddingVertical: 6, borderRadius: 999,
         borderWidth: 1, borderColor: "transparent" },
  segSm: { paddingHorizontal: 10, paddingVertical: 5 },
  segOn: { backgroundColor: C.card, borderColor: C.accent + "99" },
  txt: { color: C.mut, fontSize: 13 },
  txtSm: { fontSize: 12 },
  txtOn: { color: C.text, fontWeight: "600" },
});
