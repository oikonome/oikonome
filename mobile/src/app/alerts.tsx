// Alerts — what the app has flagged: one line per subject, live first.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "expo-router";
import { useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import PullRefresh from "../components/pull-refresh";

import { FailedLine, StaleLine } from "../components/loaded";
import { Card, HelpLink, Pill, Skeleton } from "../components/ui";
import { alertCta, alertRoute } from "../lib/alert-routes";
import type { Alert, AlertRow, TodayFull } from "../lib/api";
import { patchList, removeFromList } from "../lib/cache";
import { alertSections, withLiveLinks, historyToggleLabel, listPhase,
         pendingRecoveryLine, EMPTY_LINES } from "../lib/pure";
import { useSession } from "../lib/session";
import { useViewer } from "../lib/viewer";
import { C, mmddyy } from "../lib/theme";
import SegmentedControl from "../components/segmented-control";

// which chip lists a line is the server's (`group`), as are its name
// (`kind_label`) and where it is acted on (`link`) — the web's page reads
// the same fields
const FILTERS = ["all", "connection", "bills", "merchants", "cash",
                 "dismissed"] as const;

// older-server path: stands in for `group` (the chips' kinds before it)
const OLD_GROUP: Record<string, string> = {
  "stale-pull": "connection", "connection-removed": "connection",
  setup: "connection", proposals: "bills", drift: "bills",
  funding: "cash", transfer: "cash", bonus: "cash", anomaly: "cash",
  reimb: "cash" };

export default function Alerts() {
  const { client } = useSession();
  const router = useRouter();
  const viewer = useViewer();
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>("all");
  // what needs attention is the page; the log opens on request
  const [showHistory, setShowHistory] = useState(false);
  const qc = useQueryClient();
  // Dismiss / Restore flip the row in the same frame as the tap — the
  // outcome is certain, and waiting for the history to refetch leaves the
  // label and "· dismissed" a round trip behind. Rolled back if
  // the server refuses. Today's banner list is patched for a dismiss;
  // a restore has to refetch Today to get the banner back.
  const act = useMutation({
    // dismiss and restore answer different counts; only success matters
    mutationFn: ({ kind, message, on }:
        { kind: string; message: string; on: boolean }): Promise<unknown> =>
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
      if (on) qc.invalidateQueries({ queryKey: ["today"], refetchType: "none" });
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
  // A security push opens this screen too (the relay's alert event carries
  // no text), and the log says nothing about a pending recovery — so the
  // one notice that can still be undone links to the card that cancels it.
  const me = useQuery({ queryKey: ["me"], queryFn: () => client!.me(),
                        enabled: !!client });
  const recovery = pendingRecoveryLine(me.data?.factor_reset_pending,
                                       mmddyy);
  // the live alerts Today already fetched carry each one's own target
  const live = qc.getQueryData<TodayFull>(["today"])?.alerts;
  const all = withLiveLinks(query.data?.alerts ?? [], live);
  const { active, dismissed, resolved, history } = alertSections(all);
  const alerts = filter === "all" ? history
    : filter === "dismissed" ? dismissed
    : history.filter((a) => (a.group !== undefined ? a.group : OLD_GROUP[a.kind]) === filter);

  const row = (a: AlertRow, i: number) => (
    <View key={`${a.kind}-${i}`} style={s.row}>
      <View style={s.top}>
        <Pill text={a.kind_label ?? a.kind}
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
      {/* the server's words for the line; `message` stays the key
          dismiss and restore send back */}
      <Text style={s.msg}>{a.text !== undefined
        ? a.text
        // older-server path: stands in for `text`
        : a.message}</Text>
      <View style={{ flexDirection: "row", gap: 16 }}>
        {/* every line opens where its subject is acted on; a resolved
            one has nothing left to fix, so it only opens */}
        {alertRoute(a) && (
          <Text style={{ color: C.accent, fontSize: 12 }}
                onPress={() => router.push(alertRoute(a) as never)}>
            {a.active ? alertCta(a.kind) : "Open"} ›
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
  );

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => query.refetch()} />}>
      <StaleLine query={query} />
      {recovery && (
        <Pressable onPress={() => router.push("/security" as never)}
                   accessibilityRole="link">
          <Card>
            <Text style={[s.msg, { color: C.warn }]}>{recovery} ›</Text>
          </Card>
        </Pressable>
      )}
      {query.isPending && <Skeleton />}
      {!query.isPending && all.length > 0 && (
        <Text style={[s.center, { paddingBottom: 4 }]}>
          {active.length > 0
            ? `${active.length} need${active.length === 1 ? "s" : ""} attention`
            : "All clear"}
          {dismissed.length > 0 ? ` · ${dismissed.length} dismissed` : ""}
          {resolved.length > 0 ? ` · ${resolved.length} resolved` : ""}
        </Text>
      )}
      {listPhase({ isError: query.isError, hasData: !!query.data },
                 all.length) === "failed" && (
        <FailedLine q={query} what="alerts" />
      )}
      {listPhase({ isError: query.isError, hasData: !!query.data },
                 all.length) === "empty" && (
        <Text style={s.center}>
          No alerts logged yet — they appear here as the daily checks
          find things worth telling you about.
        </Text>
      )}
      {/* what needs a person now, and nothing else beside it */}
      {active.length > 0 && (
        <Card>
          <Text style={s.section}>Needs attention</Text>
          {active.map(row)}
        </Card>
      )}
      {all.length > 0 && (
        <View style={s.historyHead}>
          <Text style={s.section}>History</Text>
          <Pressable onPress={() => setShowHistory((v) => !v)}
                     accessibilityRole="button">
            <Text style={{ color: C.accent, fontSize: 12 }}>
              {historyToggleLabel(showHistory, history.length)}
            </Text>
          </Pressable>
        </View>
      )}
      {showHistory && all.length > 0 && (
        <SegmentedControl label="Show" size="sm" value={filter}
          onChange={setFilter}
          style={{ marginHorizontal: 12, marginBottom: 8 }}
          options={FILTERS.map((f) => (
            { value: f, label: f[0].toUpperCase() + f.slice(1) }))} />
      )}
      {showHistory && alerts.length > 0 && (
        <Card>
          {alerts.map(row)}
        </Card>
      )}
      {showHistory && all.length > 0 && alerts.length === 0 && (
        <Text style={s.center}>{EMPTY_LINES.filtered}</Text>
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
  section: { color: C.mut, fontSize: 12, fontWeight: "600",
             textTransform: "uppercase", letterSpacing: 0.5,
             paddingBottom: 4 },
  historyHead: { flexDirection: "row", justifyContent: "space-between",
                 alignItems: "center", paddingHorizontal: 16,
                 paddingTop: 12, paddingBottom: 6 },
});
