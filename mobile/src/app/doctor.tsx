// Doctor — the instance's own health checks, grouped by section, with
// the overall verdict, a re-run button, and the host-side collectors
// table (expected-hours + email-when-stale, self-host only).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Pressable, ScrollView, StyleSheet, Switch, Text, TextInput, View } from "react-native";
import PullRefresh from "../components/pull-refresh";

import StaleBanner from "../components/stale-banner";
import { Card, H, HelpLink } from "../components/ui";
import { useSession } from "../lib/session";
import { C } from "../lib/theme";
import { DownloadButton } from "./settings";
import { useOwner } from "../lib/viewer";

export default function Doctor() {
  const { client } = useSession();
  const qc = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: () => client!.me(),
    enabled: !!client, staleTime: 5 * 60_000 });
  // the collector watch settings write through a script-token door,
  // which is the owner's — everyone else reads the checks
  const viewer = !useOwner();
  const query = useQuery({
    queryKey: ["doctor"],
    queryFn: () => client!.doctor(),
    // the web's cadence: failing instances re-check every 10s, healthy
    // ones every 30s — health that only updates on pull-down is stale.
    // A hosted phone renders "managed by the host" and never needs it.
    enabled: !!client && me.data?.hosted === false,
    refetchInterval: (q) =>
      q.state.data && !q.state.data.ok ? 10_000 : 30_000,
  });
  // the web's relative age for collector pushes
  const age = (hours: number): string =>
    hours < 1 ? `${Math.round(hours * 60)}m ago`
    : hours < 48 ? `${hours.toFixed(1)}h ago`
    : `${Math.round(hours / 24)}d ago`;
  // collectors run on the USER'S host — hosted instances have none
  const scripts = useQuery({
    queryKey: ["doctor-scripts"],
    queryFn: () => client!.doctorScripts(),
    enabled: !!client && me.data?.hosted === false,
  });
  const [hoursDraft, setHoursDraft] =
    useState<Record<string, string>>({});
  type Scripts = NonNullable<typeof scripts.data>;
  const upd = useMutation({
    mutationFn: ({ source, body }: { source: string;
        body: { expected_hours?: number; alerts?: boolean } }) =>
      client!.doctorScriptUpdate(source, body),
    // the Switch is bound to the cached value, so it flips here — on the
    // tap — and a refused write puts it back
    onMutate: async ({ source, body }) => {
      await qc.cancelQueries({ queryKey: ["doctor-scripts"] });
      const before = qc.getQueryData<Scripts>(["doctor-scripts"]);
      qc.setQueryData<Scripts>(["doctor-scripts"], (o) => o && {
        ...o, scripts: o.scripts.map((sc) => sc.source !== source ? sc : {
          ...sc,
          ...(body.alerts !== undefined ? { alerts: body.alerts ? 1 : 0 } : {}),
          ...(body.expected_hours !== undefined
              ? { expected_hours: body.expected_hours } : {}),
        }),
      });
      return { before };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.before) qc.setQueryData(["doctor-scripts"], ctx.before);
    },
    onSettled: () =>
      qc.invalidateQueries({ queryKey: ["doctor-scripts"] }),
  });
  if (me.data?.hosted) {
    return (
      <ScrollView style={s.wrap}>
        <Text style={s.center}>
          System health is managed by the host on this instance.
        </Text>
      </ScrollView>
    );
  }
  const checks = query.data?.checks ?? [];
  const sections = [...new Set(checks.map((c) => c.section))];
  const bad = checks.filter((c) => !c.ok);
  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => query.refetch()} />}>
      <StaleBanner query={query} />
      {query.isPending && <Text style={s.center}>Loading…</Text>}
      {query.data && (
        <Card>
          <View style={{ flexDirection: "row", alignItems: "center",
                         gap: 8 }}>
            <Text style={{ color: query.data.ok ? C.good : C.warn,
                           fontSize: 16, fontWeight: "700", flex: 1 }}>
              {query.data.ok ? "All checks pass."
                : `${bad.length} check${bad.length === 1 ? "" : "s"} `
                  + "need attention."}
            </Text>
            <Pressable style={[s.btn,
                         query.isRefetching && { opacity: 0.5 }]}
                       disabled={query.isRefetching}
                       onPress={() => query.refetch()}>
              <Text style={s.btnText}>
                {query.isRefetching ? "running…" : "Re-run"}
              </Text>
            </Pressable>
          </View>
          <Text style={s.mut}>
            auto-refreshes — checked{" "}
            {new Date(query.dataUpdatedAt).toLocaleTimeString()}
            {query.isFetching ? " ⟳" : ""}
          </Text>
          {/* the bundle is what bug reports ask for (the web's link) */}
          <DownloadButton path="/api/doctor/bundle"
                          filename="oikonome-doctor-bundle.zip"
                          label="Download diagnostic bundle" />
        </Card>
      )}
      {sections.map((sec) => (
        <Card key={sec}>
          <H>{sec}</H>
          {checks.filter((c) => c.section === sec).map((c) => (
            <View key={c.name} style={s.row}>
              {/* dot + WORD — never color alone */}
              <Text style={{ color: c.ok ? C.good
                               : c.severity === "warn" ? C.warn : C.bad,
                             fontSize: 12, width: 40 }}>
                ● {c.ok ? "ok" : c.severity === "warn" ? "warn" : "fail"}
              </Text>
              <View style={{ flex: 1 }}>
                <Text style={{ color: C.text, fontSize: 14,
                               fontWeight: "600" }}>{c.name}</Text>
                <Text style={{ color: C.mut, fontSize: 12 }}>
                  {c.detail}
                </Text>
              </View>
            </View>
          ))}
        </Card>
      ))}
      {(scripts.data?.scripts ?? []).length > 0 && (
        <Card>
          <H>Collectors</H>
          <Text style={s.mut}>
            Host-side scripts pushing data in. Expected hours sets when
            a silent one counts as stale; the switch emails you when it
            goes quiet.
          </Text>
          {scripts.data!.scripts.map((sc) => (
            <View key={sc.source} style={[s.row, { alignItems: "center" }]}>
              <View style={{ flex: 1 }}>
                <Text style={{ color: C.text, fontSize: 13,
                               fontWeight: "600" }}>
                  {/* the web's status pill: fresh / stale / off */}
                  <Text style={{ color: sc.status === "fresh" ? C.good
                                   : sc.status === "stale" ? C.warn
                                   : sc.ok ? C.good : C.bad }}>
                    ● {sc.status ?? (sc.ok ? "fresh" : "stale")}{"  "}
                  </Text>
                  {sc.label || sc.source}
                  {sc.label && sc.label !== sc.source ? (
                    <Text style={{ color: C.mut, fontWeight: "400" }}>
                      {" "}({sc.source})
                    </Text>
                  ) : null}
                </Text>
                <Text style={{ color: C.mut, fontSize: 11 }}>
                  {sc.age_hours != null ? age(sc.age_hours)
                    : sc.last_push ? "pushed" : "never pushed"}
                  {sc.last_rows != null ? ` · ${sc.last_rows} rows` : ""}
                </Text>
                <Text style={{ color: C.mut, fontSize: 10 }}>
                  every __ h (0 = don&apos;t warn) · switch = email me
                  when stale
                </Text>
              </View>
              <TextInput style={s.hours} keyboardType="number-pad"
                         placeholder="hrs" placeholderTextColor={C.mut}
                         value={hoursDraft[sc.source]
                           ?? (sc.expected_hours != null
                               ? String(sc.expected_hours) : "")}
                         onChangeText={(v) => setHoursDraft((h) =>
                           ({ ...h, [sc.source]: v }))}
                         editable={!viewer}
                         onEndEditing={() => {
                           const v = hoursDraft[sc.source];
                           // blank = 0 = "don't warn" — a watch must
                           // be clearable
                           if (v !== undefined)
                             upd.mutate({ source: sc.source,
                               body: { expected_hours:
                                 parseInt(v || "0", 10) || 0 } });
                         }} />
              <Switch value={!!sc.alerts} disabled={viewer}
                      onValueChange={(v) => upd.mutate({
                        source: sc.source, body: { alerts: v } })}
                      trackColor={{ true: C.accent, false: C.hover }}
                      thumbColor={C.text} />
            </View>
          ))}
        </Card>
      )}
      <HelpLink topic="troubleshooting" />
    </ScrollView>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  center: { color: C.mut, textAlign: "center", padding: 20 },
  mut: { color: C.mut, fontSize: 12, lineHeight: 17 },
  row: { flexDirection: "row", gap: 10, alignItems: "flex-start",
         borderTopColor: C.border, borderTopWidth: StyleSheet.hairlineWidth,
         paddingVertical: 7 },
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 14, paddingVertical: 7 },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
  hours: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
           borderRadius: 8, color: C.text, fontSize: 13, width: 52,
           paddingHorizontal: 8, paddingVertical: 5,
           textAlign: "center" },
});
