// The bank links this app is waiting on, under a connect button: one
// row each, with "cancel — I closed the bank window", and the recent
// outcomes. The web's PendingLinkRows (components/ConnectHub.tsx).
import { Pressable, Text, View } from "react-native";

import { cancelPendingLink, usePendingLinks } from "../lib/plaid";
import { pendingLinkLine } from "../lib/pure";
import { C } from "../lib/theme";

export function PendingLinkRows() {
  const { pending, outcomes } = usePendingLinks();
  if (!pending.length && !outcomes.length) return null;
  return (
    <View style={{ marginTop: 6, gap: 4 }}>
      {pending.map((p) => (
        <View key={p.token}
              style={{ flexDirection: "row", alignItems: "center", gap: 10,
                       flexWrap: "wrap" }}>
          <Text style={{ color: C.good, fontSize: 12 }}>
            {pendingLinkLine(p.kind, p.cancelled)}</Text>
          {!p.cancelled && (
            <Pressable onPress={() => cancelPendingLink(p.token)}
                       hitSlop={8}>
              <Text style={{ color: C.mut, fontSize: 12 }}>
                cancel — I closed the bank window</Text>
            </Pressable>
          )}
        </View>
      ))}
      {outcomes.map((o) => (
        <Text key={o.id} style={{ color: o.ok ? C.good : C.warn, fontSize: 12 }}>
          {o.text}</Text>
      ))}
    </View>
  );
}
