// Shared chrome: the SPA's card / section / meter / pill anatomy in
// native form, so every screen reads as the same product.
import { useRouter } from "expo-router";
import { StyleSheet, Text, View, ViewStyle, Pressable } from "react-native";

import { C } from "../lib/theme";

// the web header's contextual ? — every screen can land on ITS guide
// instead of the catalog (help.tsx honors ?topic=)
export function HelpLink({ topic }: { topic: string }) {
  const router = useRouter();
  return (
    <Text style={{ color: C.mut, fontSize: 12, textAlign: "center",
                   paddingVertical: 8 }}
          onPress={() => router.push(
            `/help?topic=${encodeURIComponent(topic)}` as never)}>
      ? Help with this page
    </Text>
  );
}

export function Card({ children, style }:
    { children: React.ReactNode; style?: ViewStyle }) {
  return <View style={[s.card, style]}>{children}</View>;
}

export function H({ children }: { children: React.ReactNode }) {
  return <Text style={s.h}>{children}</Text>;
}

/** month-scope progress bar with an optional "today" post tick (0..1) */
export function Meter({ frac, pace, tone, big }:
    { frac: number; pace?: number; tone?: "good" | "warn" | "bad";
      big?: boolean }) {
  // a gauge fill is a verdict, so the default is green like the web's
  // bars (green on budget, red over); blue is reserved for bars that
  // judge nothing — ranked spending bars, sync progress
  const fill = tone === "bad" ? C.bad : tone === "warn" ? C.warn : C.good;
  return (
    // the padding gives the tick its 3px overshoot without negative
    // offsets (Android clips children that overflow)
    <View style={[s.meterWrap,
                  pace !== undefined && { paddingVertical: 3 }]}>
      {/* big = the web money map's 14px bar — the Today hero rows use it
          so the whole page speaks one bar idiom */}
      <View style={[s.meterTrack,
                    big && { height: 14, borderRadius: 7 }]}>
        <View style={[s.meterFill,
                      big && { height: 14, borderRadius: 7 },
                      { width: `${Math.min(100, Math.max(0, frac * 100))}%`,
                        backgroundColor: fill }]} />
      </View>
      {pace !== undefined && (
        <View style={[s.tick,
                      { left: `${Math.min(99, Math.max(0, pace * 100))}%` }]} />
      )}
    </View>
  );
}

export function KV({ k, v, tone, onPress }:
    { k: string; v: string; tone?: "good" | "warn" | "bad" | "mut";
      /** the label opens something (a category's rows): plain at rest,
       *  accent while pressed — the web's catlink */
      onPress?: () => void }) {
  const color = tone === "good" ? C.good : tone === "warn" ? C.warn
    : tone === "bad" ? C.bad : tone === "mut" ? C.mut : C.text;
  return (
    <View style={s.kv}>
      {onPress
        ? <Pressable onPress={onPress} style={s.kvPress}>
            {({ pressed }) => (
              <Text style={[s.kvK, pressed && { color: C.accent }]}>{k}</Text>)}
          </Pressable>
        : <Text style={s.kvK}>{k}</Text>}
      <Text style={[s.kvV, { color }]}>{v}</Text>
    </View>
  );
}

export function Pill({ text, tone }:
    { text: string; tone?: "good" | "warn" | "bad" }) {
  const color = tone === "good" ? C.good : tone === "warn" ? C.warn
    : tone === "bad" ? C.bad : C.mut;
  return (
    <View style={[s.pill, { borderColor: color }]}>
      <Text style={{ color, fontSize: 11, fontWeight: "600" }}>{text}</Text>
    </View>
  );
}

const s = StyleSheet.create({
  card: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
          borderRadius: C.r, padding: 14, marginHorizontal: 12,
          marginTop: 10 },
  h: { color: C.text, fontSize: 16, fontWeight: "700", marginBottom: 8 },
  meterWrap: { marginTop: 6, marginBottom: 2 },
  meterTrack: { height: 6, borderRadius: 3, backgroundColor: C.hover,
                overflow: "hidden" },
  meterFill: { height: 6, borderRadius: 3 },
  tick: { position: "absolute", top: 0, bottom: 0, width: 2,
          borderRadius: 1, backgroundColor: C.text, marginLeft: -1 },
  kv: { flexDirection: "row", justifyContent: "space-between",
        paddingVertical: 4 },
  kvK: { color: C.mut, fontSize: 14, flexShrink: 1 },
  kvPress: { marginLeft: -4, paddingHorizontal: 4, borderRadius: 6 },
  kvV: { fontSize: 14, fontWeight: "600" },
  pill: { borderWidth: 1, borderRadius: 999, paddingHorizontal: 8,
          paddingVertical: 2, alignSelf: "flex-start" },
});
