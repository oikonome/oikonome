// The web header's lens picker — Today | Month | Year — so the lens
// triad is reachable from any lens without a round trip through More.
// On Home it rides the navigation header, in line with the logo;
// `inHeader` shrinks it to fit beside the brand row. The same
// SegmentedControl as every other pick-one on the app (and the web's).
import { useRouter } from "expo-router";
import SegmentedControl from "./segmented-control";

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
    <SegmentedControl label="Lens" size={inHeader ? "sm" : undefined}
      value={active} onChange={go}
      style={inHeader ? { marginRight: 12, alignSelf: "center" }
                      : { alignSelf: "center", marginTop: 8 }}
      options={(["today", "month", "year"] as const).map((l) => ({
        value: l,
        label: l === "today" ? "Today" : l === "month" ? "Month" : "Year",
      }))} />
  );
}
