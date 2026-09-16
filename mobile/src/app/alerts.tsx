// Alerts — what the app has flagged, newest first.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "expo-router";
import { useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import PullRefresh from "../components/pull-refresh";

import StaleBanner from "../components/stale-banner";
import { Card, HelpLink, Pill } from "../components/ui";
import { KIND_ROUTE } from "../lib/alert-routes";
import type { Alert, AlertRow, TodayFull } from "../lib/api";
import { patchList, removeFromList } from "../lib/cache";
import { useSession } from "../lib/session";
import { useViewer } from "../lib/viewer";
import { C, mmddyy } from "../lib/theme";

const FILTERS = ["all", "connection", "bills", "cash", "dismissed"] as const;
const FILTER_KINDS: Record<string, string[]> = {
  connection: ["stale-pull", "connection-removed", "setup"],
  bills: ["proposals", "drift"],
  cash: ["funding", "transfer", "bonus", "anomaly", "reimb"],
};

export default function Alerts() {
  const { client } = useSession();
  const router = useRouter();
  const viewer = useViewer();
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>("all");
  const qc = useQueryClient();
  // Dismiss / Restore flip the row in the same frame as the tap — the
  // outcome is certain, and waiting for the history to refetch leaves the
  // label and "· dismissed" a round trip behind. Rolled back if
  // the server refuses. Today's banner list is patched for a dismiss;
  // a restore has to refetch Today to get the banner back.
  const act = useMutation({
    mutationFn: ({ kind, message, on }:
        { kind: string; message: string; on: boolean }) =>
      on ? client!.alertRestore(kind, message)
         : client!.alertDismiss(kind, message),
    onMutate: async ({ kind, message, on }) => {
      await qc.cancelQueries({ queryKey: ["alerts"] });
      const prev = qc.getQueryData<{ alerts: AlertRow[] }>(["alerts"]);
      patchList<{ alerts: AlertRow[] }, AlertRow>(qc, ["alerts"], "alerts",
        (a) => a.kind === kind && a.message === message,
        { dismissed: on ? 0 : 1 });
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(["alerts"], ctx.prev);
    },
    onSuccess: (_r, { kind, message, on }) => {
      if (on) qc.invalidateQueries({ queryKey: ["today"] });
      else removeFromList<TodayFull, Alert>(qc, ["today"], "alerts",
        (a) => a.kind === kind && a.message === message);
      qc.invalidateQueries({ queryKey: ["alerts"] });
    },
  });
  const query = useQuery({
    queryKey: ["alerts"],
    queryFn: () => client!.alertsHistory(),
    enabled: !!client,
  });
  const all = query.data?.alerts ?? [];
  const active = all.filter((a) => a.active && !a.dismissed);
  const dismissed = all.filter((a) => a.active && a.dismissed);
  const alerts = filter === "all" ? all
    : filter === "dismissed" ? dismissed
    : all.filter((a) => (FILTER_KINDS[filter] ?? []).includes(a.kind));

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => query.refetch()} />}>
      <StaleBanner query={query} />
      {query.isPending && <Text style={s.center}>Loading…</Text>}
      {!query.isPending && all.length > 0 && (
        <Text style={[s.center, { paddingBottom: 4 }]}>
          {active.length > 0
            ? `${active.length} need${active.length === 1 ? "s" : ""} attention`
            : "All clear"}
          {dismissed.length > 0 ? ` · ${dismissed.length} dismissed` : ""}
          {all.filter((a) => !a.active).length > 0
            ? ` · ${all.filter((a) => !a.active).length} resolved` : ""}
        </Text>
      )}
      {all.length > 0 && (
        <View style={s.filterRow}>
          {FILTERS.map((f) => (
            <Pressable key={f} style={[s.fchip, filter === f && s.fchipOn]}
                       onPress={() => setFilter(f)}>
              <Text style={{ color: filter === f ? C.text : C.mut,
                             fontSize: 12 }}>{f}</Text>
            </Pressable>
          ))}
        </View>
      )}
      {!query.isPending && all.length === 0 && (
        <Text style={s.center}>
          No alerts logged yet — they appear here as the daily checks
          find things worth telling you about.
        </Text>
      )}
      {alerts.length > 0 && (
        <Card>
          {alerts.map((a, i) => (
            <View key={`${a.kind}-${i}`} style={s.row}>
              <View style={s.top}>
                <Pill text={a.kind}
                      tone={a.severity === "warn" ? "warn"
                        : a.severity === "bad" || a.severity === "error"
                          ? "bad"
                        : a.severity === "good" ? "good" : undefined} />
                <Text style={s.mut}>
                  {/* an ACTIVE alert runs first_seen → today (the web's
                      literal word) — its end date hasn't happened yet */}
                  {a.active
                    ? `${mmddyy(a.first_seen ?? a.last_seen)} – today`
                    : `${a.first_seen && a.first_seen !== a.last_seen
                        ? `${mmddyy(a.first_seen)} – ` : ""}${
                        mmddyy(a.last_seen)} · resolved`}
                  {a.dismissed ? " · dismissed" : ""}
                </Text>
              </View>
              <Text style={s.msg}>{a.message}</Text>
              <View style={{ flexDirection: "row", gap: 16 }}>
                {KIND_ROUTE[a.kind] && !!a.active && (
                  <Text style={{ color: C.accent, fontSize: 12 }}
                        onPress={() =>
                          router.push(KIND_ROUTE[a.kind] as never)}>
                    fix this ›
                  </Text>
                )}
                {!viewer && (a.active || a.dismissed) ? (() => {
                  // this row's own write in flight — never the whole list
                  const busy = act.isPending
                    && act.variables?.kind === a.kind
                    && act.variables?.message === a.message;
                  return (
                  <Text style={{ color: C.accent, fontSize: 12,
                                 opacity: busy ? 0.5 : 1 }}
                        onPress={busy ? undefined
                          : () => act.mutate({ kind: a.kind,
                              message: a.message, on: !!a.dismissed })}>
                    {a.dismissed ? "Restore" : "Dismiss"}
                  </Text>
                  );
                })() : null}
              </View>
            </View>
          ))}
        </Card>
      )}
      <HelpLink topic="alerts" />
    </ScrollView>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  center: { color: C.mut, textAlign: "center", padding: 20 },
  row: { borderTopColor: C.border, borderTopWidth: StyleSheet.hairlineWidth,
         paddingVertical: 8, gap: 4 },
  top: { flexDirection: "row", justifyContent: "space-between",
         alignItems: "center" },
  msg: { color: C.text, fontSize: 13, lineHeight: 18 },
  mut: { color: C.mut, fontSize: 12 },
  filterRow: { flexDirection: "row", flexWrap: "wrap", gap: 6,
               paddingHorizontal: 12 },
  fchip: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
           borderRadius: 999, paddingHorizontal: 12, paddingVertical: 5 },
  fchipOn: { borderColor: C.accent, backgroundColor: C.hover },
});
