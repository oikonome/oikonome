import { Pressable, Text, View } from "react-native";

import { C } from "../lib/theme";

// the account-state lockouts the web shell renders when /api/me 403s —
// the account is frozen, data intact; only sign-out works
export function Lockout({ title, body, action, onOut }: {
  title: string; body: string;
  // the one way out that isn't signing out — an add-on's lockout screen
  // may offer one, which is the whole point of the screen existing
  // rather than being a dead end
  action?: { label: string; onPress: () => void };
  onOut: () => void;
}) {
  return (
    <View style={{ flex: 1, backgroundColor: C.bg,
                   justifyContent: "center", padding: 24, gap: 12 }}>
      <Text style={{ color: C.text, fontSize: 18, fontWeight: "700" }}>
        {title}
      </Text>
      <Text style={{ color: C.mut, fontSize: 14, lineHeight: 20 }}>
        {body}
      </Text>
      {action && (
        <Pressable onPress={action.onPress}
                   style={{ backgroundColor: C.accent, borderRadius: 8,
                            paddingVertical: 11, alignItems: "center" }}>
          <Text style={{ color: "#04121d", fontWeight: "700" }}>
            {action.label}
          </Text>
        </Pressable>
      )}
      <Pressable onPress={onOut}
                 style={{ backgroundColor: C.hover, borderRadius: 8,
                          paddingVertical: 10, alignItems: "center" }}>
        <Text style={{ color: C.mut, fontWeight: "600" }}>Sign out</Text>
      </Pressable>
    </View>
  );
}
