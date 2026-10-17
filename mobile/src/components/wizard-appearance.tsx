// The wizard's first step: Auto / Light / Dark as three stacked cards,
// each with a small preview of the palette it picks. The choice itself
// (local mode + the household's saved theme) is made by the wizard, which
// also saves the pre-selected Auto when the person simply continues.
import { Text, View } from "react-native";
import { C, PALETTES } from "../lib/theme";
import { OptionCard, ws } from "./wizard-ui";
import type { ThemeChoice } from "./wizard-logic";

const OPTIONS: { v: ThemeChoice; title: string; caption: string }[] = [
  { v: "auto", title: "Auto",
    caption: "Matches your device's light/dark setting. Default." },
  { v: "light", title: "Light",
    caption: "Always light, on every device you sign in on." },
  { v: "dark", title: "Dark",
    caption: "Always dark, on every device you sign in on." },
];

function Swatch({ v }: { v: ThemeChoice }) {
  const box = { height: 44, borderRadius: 8, marginTop: 8, marginBottom: 2,
                overflow: "hidden" as const, flexDirection: "row" as const,
                borderWidth: 1 };
  if (v === "auto")
    return (
      <View style={[box, { borderColor: PALETTES.dark.border }]}>
        <View style={{ flex: 1, backgroundColor: PALETTES.dark.bg,
                       justifyContent: "center", paddingLeft: 10 }}>
          <Text style={{ color: PALETTES.dark.mut, fontSize: 11 }}>
            follows the system</Text>
        </View>
        <View style={{ flex: 1, backgroundColor: PALETTES.light.bg }} />
      </View>
    );
  const p = PALETTES[v];
  return (
    <View style={[box, { backgroundColor: p.bg, borderColor: p.border,
                         alignItems: "center", paddingHorizontal: 10 }]}>
      <Text style={{ color: p.text, fontSize: 11 }}>Today · $1,240 left</Text>
    </View>
  );
}

export function AppearancePane({ value, onPick, restartPending }: {
  value: ThemeChoice; onPick: (v: ThemeChoice) => void;
  restartPending: boolean;
}) {
  return (
    <View style={ws.body} accessibilityRole="radiogroup">
      {OPTIONS.map((o) => (
        <OptionCard key={o.v} title={o.title} caption={o.caption}
                    selected={value === o.v}
                    tag={value === o.v ? "SELECTED" : null}
                    onPress={() => onPick(o.v)}>
          <Swatch v={o.v} />
        </OptionCard>
      ))}
      {/* the palette is fixed when the app starts, so a change that
          differs from what is on screen says how to see it */}
      {restartPending && (
        <Text style={[ws.mut, { color: C.warn }]}>
          Close and reopen the app to apply.</Text>
      )}
    </View>
  );
}
