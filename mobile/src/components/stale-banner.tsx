// Offline read layer, made honest: cached data stays on screen, and this
// banner says how old it is whenever the latest refetch failed.
import type { UseQueryResult } from "@tanstack/react-query";
import { StyleSheet, Text } from "react-native";

import { C } from "../lib/theme";

export default function StaleBanner({ query }:
    { query: UseQueryResult<unknown> }) {
  if (!query.isError || !query.data) return null;
  const age = Date.now() - query.dataUpdatedAt;
  const mins = Math.round(age / 60_000);
  const label = mins < 60 ? `${mins} min`
    : mins < 60 * 48 ? `${Math.round(mins / 60)} h`
    : `${Math.round(mins / 60 / 24)} d`;
  return (
    <Text style={s.banner}>
      Offline — showing data from {label} ago
    </Text>
  );
}

const s = StyleSheet.create({
  banner: { backgroundColor: "#3a2d12", borderRadius: 8, color: C.warn,
            fontSize: 13, overflow: "hidden", paddingHorizontal: 12,
            paddingVertical: 8, textAlign: "center" },
});
