// The bank links this app is waiting on, under a connect button: one
// row each, with "cancel — I closed the bank window", and the recent
// outcomes. The web's PendingLinkRows (components/ConnectHub.tsx).
import { useQuery } from "@tanstack/react-query";
import { Pressable, Text, View } from "react-native";

import { cancelPendingLink, usePendingLinks } from "../lib/plaid";
import { bankLinkLine, pendingLinkLine } from "../lib/pure";
import { useSession } from "../lib/session";
import { C } from "../lib/theme";

/** Under every add-a-bank button: the server's line — how many more banks
 *  the household may connect, and whether several can be connected in one
 *  sign-in. The web's BankLinkLine (components/ConnectHub.tsx). */
export function BankLinkLine({ style }: { style?: object }) {
  const { client } = useSession();
  const me = useQuery({ queryKey: ["me"], queryFn: () => client!.me(),
                        enabled: !!client });
  const line = bankLinkLine(me.data);
  if (!line) return null;
  return <Text style={[{ color: C.mut, fontSize: 12 }, style]}>{line}</Text>;
}

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
            {(!p.cancelled && p.progress) || pendingLinkLine(p.kind, p.cancelled)}</Text>
          {!p.cancelled && (
            <Pressable accessibilityRole="button" onPress={() => cancelPendingLink(p.token)}
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
