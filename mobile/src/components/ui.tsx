// Shared chrome: the SPA's card / section / meter / pill anatomy in
// native form, so every screen reads as the same product.
import { useRouter } from "expo-router";
import { StyleSheet, Text, View, ViewStyle, Pressable, type TextProps }
  from "react-native";

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

/** What a screen shows while its first payload is on the way: quiet
 *  blocks in the shape of a screen, so the data fills a layout in rather
 *  than replacing a word and shoving everything down (the web's
 *  PageSkeleton). */
export function Skeleton() {
  const bar = (w: `${number}%`, h: number, mt = 0) => (
    <View style={{ width: w, height: h, marginTop: mt, borderRadius: 6,
                   backgroundColor: C.hover }} />
  );
  return (
    <View accessible accessibilityRole="progressbar"
          accessibilityLabel="Loading">
      <View style={s.card}>
        {bar("45%", 28)}
        {bar("96%", 10, 16)}
        {bar("82%", 10, 10)}
        {bar("64%", 10, 10)}
      </View>
      <View style={s.card}>
        {bar("40%", 16)}
        {bar("100%", 84, 14)}
      </View>
    </View>
  );
}

export function Card({ children, style }:
    { children: React.ReactNode; style?: ViewStyle }) {
  return <View style={[s.card, style]}>{children}</View>;
}

export function H({ children }: { children: React.ReactNode }) {
  // a heading to a screen reader's heading navigation, not plain text
  return <Text style={s.h} accessibilityRole="header">{children}</Text>;
}

/** The one line for what happened: an error, a result, a warning that
 *  appeared because of something the person did (specs/accessibility.md).
 *  A screen reader hears it when it appears: an error is an alert, anything
 *  else a polite live region. It looks like the Text it replaces; the web
 *  twin is components/Note.tsx. */
export function Note({ bad, children, ...rest }: TextProps & { bad?: boolean }) {
  return (
    <Text {...rest} accessibilityRole={bad ? "alert" : undefined}
          accessibilityLiveRegion={bad ? "assertive" : "polite"}>
      {children}
    </Text>
  );
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
    // a progress bar to a screen reader, at the width it draws; the
    // figures it stands for are printed beside it
    <View style={[s.meterWrap,
                  pace !== undefined && { paddingVertical: 3 }]}
          accessible accessibilityRole="progressbar"
          accessibilityValue={{ min: 0, max: 100,
                                now: Math.round(Math.min(1, Math.max(0, frac)) * 100) }}>
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
        ? <Pressable accessibilityRole="button" onPress={onPress} style={s.kvPress}>
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

// A control the server refuses this login, shown untouchable in place
// with the refusal's own sentence above it — the native twin of the web's
// DemoLock. Shown rather than hidden, because discoverable beats missing;
// switched off, because a button that can only answer 403 reads as broken.
// `note` null renders the children untouched.
export function SharedLock({ note, children }: {
  note: string | null; children: React.ReactNode;
}) {
  if (!note) return <>{children}</>;
  return (
    <View>
      <Text style={{ color: C.warn, fontSize: 12, textAlign: "center",
                     paddingHorizontal: 8, paddingTop: 8 }}>
        {note}
      </Text>
      <View pointerEvents="none" style={{ opacity: 0.55 }}>
        {children}
      </View>
    </View>
  );
}
