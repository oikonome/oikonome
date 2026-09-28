// The Android home-screen widget, as the JSX tree react-native-android-
// widget rasterises. Same anatomy as the Today hero's simple face: the
// eyebrow, the one number, the verdict sentence in its colour, the day
// bar, a sub-line; the wide cell adds the bucket chips beside it. Colours
// are the app's own palettes (theme.ts), light and dark, and the
// launcher picks which. Every string comes from glance-pure — the widget
// composes nothing.
import React from "react";
import { FlexWidget, TextWidget } from "react-native-android-widget";

import { type GlanceFace, wideEnoughForChips } from "../lib/glance-pure";
import { PALETTES } from "../lib/theme";

type Hex = `#${string}`;
type Palette = { card: string; text: string; mut: string; accent: string;
                 good: string; bad: string; line?: string };

const hex = (s: string) => s as Hex;
const BAR_TRACK = { dark: "#2a3441", light: "#e3e8ee" } as const;

function Bar({ fill, color, track }:
             { fill: number; color: string; track: string }) {
  // a fixed-height track; the fill is a nested row sized by flex
  const pct = Math.max(0, Math.min(1, fill));
  return (
    <FlexWidget style={{ width: "match_parent", height: 6, borderRadius: 3,
                         backgroundColor: hex(track), flexDirection: "row",
                         overflow: "hidden", marginTop: 8 }}>
      <FlexWidget style={{ flex: Math.max(pct, 0.0001), height: 6,
                           borderRadius: 3, backgroundColor: hex(color) }} />
      <FlexWidget style={{ flex: Math.max(1 - pct, 0.0001), height: 6 }} />
    </FlexWidget>
  );
}

function Face({ face, p, scheme, wide }:
              { face: GlanceFace; p: Palette; scheme: "dark" | "light";
                wide: boolean }) {
  const base = { height: "match_parent" as const, width: "match_parent" as const,
                 backgroundColor: hex(p.card), borderRadius: 24,
                 padding: 14 };
  if (face.kind === "signin" || face.kind === "noplan") {
    const title = face.kind === "signin" ? "Sign in" : "Set a plan";
    const sub = face.kind === "signin" ? "to see what's left today"
                                       : "the widget needs a monthly budget";
    return (
      <FlexWidget clickAction="OPEN_APP" style={{ ...base,
                  flexDirection: "column", justifyContent: "center" }}>
        <TextWidget text="OIKONOME" style={{ fontSize: 11, color: hex(p.mut),
                    fontWeight: "600", letterSpacing: 0.06 }} />
        <TextWidget text={title} style={{ fontSize: 22, fontWeight: "800",
                    color: hex(p.text), marginTop: 6 }} />
        <TextWidget text={sub} maxLines={2} style={{ fontSize: 12,
                    color: hex(p.mut), marginTop: 4 }} />
      </FlexWidget>
    );
  }
  const tone = face.stale ? p.mut : face.tone === "bad" ? p.bad : p.good;
  const left = (
    <FlexWidget style={{ flex: 1, flexDirection: "column",
                         height: "match_parent" }}>
      <TextWidget text="LEFT TODAY" style={{ fontSize: 11, color: hex(p.mut),
                  fontWeight: "600", letterSpacing: 0.06 }} />
      <TextWidget text={face.number} maxLines={1}
                  style={{ fontSize: wide ? 40 : 36, fontWeight: "800",
                           adjustsFontSizeToFit: true,
                           color: hex(face.stale ? p.mut : p.text),
                           marginTop: 4 }} />
      <TextWidget text={face.stale ? (face.asOf ? `as of ${face.asOf}`
                                                : face.sub)
                                   : face.verdict}
                  maxLines={1}
                  style={{ fontSize: 14, fontWeight: "800", color: hex(tone) }} />
      <Bar fill={face.fill} color={face.stale ? p.mut
                                   : face.over ? p.bad : p.good}
           track={BAR_TRACK[scheme]} />
      <TextWidget text={face.stale && face.asOf ? "tap to refresh" : face.sub}
                  maxLines={1}
                  style={{ fontSize: 12, color: hex(p.mut), marginTop: 6 }} />
    </FlexWidget>
  );
  if (!wide || face.chips.length === 0) {
    return (
      <FlexWidget clickAction="OPEN_APP" style={{ ...base,
                  flexDirection: "column" }}>
        {left}
      </FlexWidget>
    );
  }
  return (
    <FlexWidget clickAction="OPEN_APP" style={{ ...base,
                flexDirection: "row", flexGap: 14 }}>
      {left}
      <FlexWidget style={{ width: 130, flexDirection: "column",
                           justifyContent: "center", flexGap: 4 }}>
        {face.chips.slice(0, 4).map((c, i) => (
          <TextWidget key={i} text={c.text} maxLines={1} truncate="END"
                      style={{ fontSize: 12,
                               color: hex(c.neg ? p.bad : p.text) }} />
        ))}
      </FlexWidget>
    </FlexWidget>
  );
}

/** Both colour schemes; the launcher shows the one that matches. */
export function glanceWidget(face: GlanceFace, widthDp: number) {
  const wide = wideEnoughForChips(widthDp);
  return {
    light: <Face face={face} p={PALETTES.light} scheme="light" wide={wide} />,
    dark: <Face face={face} p={PALETTES.dark} scheme="dark" wide={wide} />,
  };
}
