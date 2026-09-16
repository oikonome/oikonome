// The web header's lens picker — Today | Month | Year — so the lens
// triad is reachable from any lens without a round trip through More.
// On Home it rides the navigation header, in line with the logo;
// `inHeader` shrinks it to fit beside the brand row.
import { useRouter } from "expo-router";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { C } from "../lib/theme";

export default function LensSwitch({ active, inHeader }: {
  active: "today" | "month" | "year"; inHeader?: boolean;
}) {
  const router = useRouter();
  const go = (lens: "today" | "month" | "year") => {
    if (lens === active) return;
    if (lens === "today") router.navigate("/(tabs)" as never);
    else router.push(`/${lens}` as never);
  };
  return (
    <View style={[s.wrap, inHeader && s.wrapHeader]}>
      {(["today", "month", "year"] as const).map((l) => (
        <Pressable key={l}
                   style={[s.seg, inHeader && s.segHeader,
                           active === l && s.segOn]}
                   onPress={() => go(l)}>
          <Text style={{ color: active === l ? C.text : C.mut,
                         fontSize: inHeader ? 12 : 13,
                         fontWeight: "600" }}>
            {l === "today" ? "Today" : l === "month" ? "Month" : "Year"}
          </Text>
        </Pressable>
      ))}
    </View>
  );
}

const s = StyleSheet.create({
  wrap: { flexDirection: "row", alignSelf: "center", gap: 4,
          backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
          borderRadius: 10, padding: 3, marginTop: 8 },
  wrapHeader: { alignSelf: "auto", marginTop: 0, marginRight: 12 },
  seg: { paddingHorizontal: 14, paddingVertical: 5, borderRadius: 7 },
  segHeader: { paddingHorizontal: 9, paddingVertical: 3 },
  segOn: { backgroundColor: C.hover },
});
