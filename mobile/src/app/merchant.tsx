// One merchant, opened from the Merchants list — the web's side panel as
// a screen: facts, category (+ the rule behind it), every source name,
// where it has been seen, and the two actions. Rename takes a new name;
// Merge into picks an EXISTING merchant from a search, so a typo cannot
// fork a merchant. Both go through the journalled rename (undo on the
// Merchants list).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useEffect, useState } from "react";
import { Alert, Linking, Platform, Pressable, ScrollView, StyleSheet, Text,
         TextInput, View } from "react-native";

import { Card } from "../components/ui";
import MerchantAvatar from "../components/merchant-avatar";
import { errText, MerchantDetail } from "../lib/api";
import { useSession } from "../lib/session";
import { useDemo, useViewer } from "../lib/viewer";
import { C, mmddyy, money } from "../lib/theme";
import { catLabel } from "../lib/pure";

export default function Merchant() {
  const { display } = useLocalSearchParams<{ display: string }>();
  const name = String(display ?? "");
  const { client } = useSession();
  const qc = useQueryClient();
  const router = useRouter();
  const viewer = useViewer();
  const demo = useDemo();
  const canEdit = !viewer && !demo;
  const [mode, setMode] = useState<"view" | "rename" | "merge">("view");
  const [draft, setDraft] = useState(name);
  const [pick, setPick] = useState("");
  const [pickQ, setPickQ] = useState("");
  useEffect(() => {
    const id = setTimeout(() => setPickQ(pick.trim()), 250);
    return () => clearTimeout(id);
  }, [pick]);

  const d = useQuery({ queryKey: ["merchant-detail", name],
                       queryFn: () => client!.merchantDetail(name),
                       enabled: !!client && !!name });
  // the merge picker searches the catalog itself, so the target is always
  // a merchant that exists — the long tail included
  const cands = useQuery({ queryKey: ["merchants", pickQ, "count"],
                           queryFn: () => client!.merchantCatalog(pickQ, "count"),
                           enabled: !!client && mode === "merge" && pickQ.length > 0,
                           placeholderData: (prev) => prev });
  const options = (cands.data?.merchants ?? []).filter((m) => m.display !== name).slice(0, 8);

  const rename = useMutation({
    mutationFn: (to: string) => client!.merchantRename(name, to),
    onSuccess: (r, to) => {
      qc.invalidateQueries({ queryKey: ["merchants"] });
      qc.invalidateQueries({ queryKey: ["merchant-detail"] });
      // every surface that prints a payee is stale now
      qc.invalidateQueries({ queryKey: ["transactions"] });
      qc.invalidateQueries({ queryKey: ["today"] });
      qc.invalidateQueries({ queryKey: ["payee-history"] });
      qc.invalidateQueries({ queryKey: ["bills"] });
      Alert.alert(mode === "merge" ? "Merged" : "Renamed",
        `${name} → ${to} · ${r.rows} transaction${r.rows === 1 ? "" : "s"}. Undo on the Merchants list.`);
      // the screen is keyed by the old name; the list shows the result
      router.back();
    },
    onError: (e) => Alert.alert("Couldn't rename", errText(e)),
  });

  const m: MerchantDetail | undefined = d.data ?? undefined;
  const place = (l: MerchantDetail["locations"][number]) =>
    [l.address, [l.city, l.region].filter(Boolean).join(", "), l.postal]
      .filter(Boolean).join(" · ");
  const openMap = (l: MerchantDetail["locations"][number]) => {
    const label = encodeURIComponent(name);
    const q = encodeURIComponent(
      [name, l.address, l.city, l.region, l.postal].filter(Boolean).join(", "));
    let url: string;
    if (l.lat != null && l.lon != null) {
      url = Platform.OS === "ios"
        ? `https://maps.apple.com/?ll=${l.lat},${l.lon}&q=${label}`
        : `geo:${l.lat},${l.lon}?q=${l.lat},${l.lon}(${label})`;
    } else {
      url = Platform.OS === "ios" ? `https://maps.apple.com/?q=${q}` : `geo:0,0?q=${q}`;
    }
    Linking.openURL(url).catch(() =>
      Linking.openURL(`https://www.openstreetmap.org/search?query=${q}`).catch(() => {}));
  };
  const site = m?.website
    ? (/^https?:/.test(m.website) ? m.website : "https://" + m.website) : null;
  const busy = rename.isPending;

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ padding: 12, gap: 10 }}>
      <Card>
        <View style={{ flexDirection: "row", alignItems: "center", gap: 10 }}>
          <MerchantAvatar name={name} logo={m?.logo} size={40} />
          <View style={{ flex: 1 }}>
            <Text style={s.title}>{name}</Text>
            <Text style={s.small}>
              {m?.parent ? `outlet of ${m.parent} · ` : ""}
              {m?.kind && m.kind !== "merchant" ? `${catLabel(m.kind)} · ` : ""}
              {m ? `${m.rows} row${m.rows === 1 ? "" : "s"} · ${money(m.total, false)} · `
                 + `${mmddyy(m.first)} – ${mmddyy(m.last)}` : ""}
            </Text>
          </View>
        </View>

        {canEdit && mode === "view" && (
          <View style={{ flexDirection: "row", gap: 16, marginTop: 10 }}>
            <Text style={s.action} onPress={() => { setDraft(name); setMode("rename"); }}>
              Rename</Text>
            <Text style={s.action} onPress={() => { setPick(""); setMode("merge"); }}>
              Merge into…</Text>
          </View>
        )}
        {canEdit && mode === "rename" && (
          <View style={{ marginTop: 10, gap: 8 }}>
            <TextInput style={s.input} value={draft} onChangeText={setDraft}
                       autoFocus autoCapitalize="words" placeholderTextColor={C.mut} />
            <View style={{ flexDirection: "row", gap: 14, alignItems: "center" }}>
              <Pressable style={[s.doneBtn, (busy || !draft.trim() || draft.trim() === name)
                                   && { opacity: 0.5 }]}
                         disabled={busy || !draft.trim() || draft.trim() === name}
                         onPress={() => rename.mutate(draft.trim())}>
                <Text style={{ color: "#fff", fontWeight: "700" }}>Save</Text>
              </Pressable>
              <Text style={s.action} onPress={() => setMode("view")}>Cancel</Text>
            </View>
          </View>
        )}
        {canEdit && mode === "merge" && (
          <View style={{ marginTop: 10, gap: 8 }}>
            <TextInput style={s.input} value={pick} onChangeText={setPick} autoFocus
                       placeholder="merge into which merchant?" placeholderTextColor={C.mut}
                       autoCapitalize="none" autoCorrect={false} />
            <Text style={s.small}>
              Every row now shown as {name} moves under the merchant you pick. Undoable.
            </Text>
            <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
              {options.map((o) => (
                <Pressable key={o.display} style={s.chip} disabled={busy}
                           onPress={() => rename.mutate(o.display)}>
                  <Text style={{ color: C.accent, fontSize: 12 }}>
                    {o.display}<Text style={{ color: C.mut }}> · {o.rows}</Text></Text>
                </Pressable>
              ))}
              {pickQ.length > 0 && !cands.isPending && options.length === 0 && (
                <Text style={s.small}>No merchant matches that.</Text>
              )}
            </View>
            <Text style={s.action} onPress={() => setMode("view")}>Cancel</Text>
          </View>
        )}
      </Card>

      {d.isPending && <Text style={s.small}>Loading…</Text>}
      {d.isError && (
        <Text style={[s.small, { color: C.bad }]} onPress={() => d.refetch()}>
          Couldn't load: {errText(d.error)} — tap to retry.</Text>
      )}
      {m && (
        <Card>
          <Text style={s.h}>About</Text>
          {(site || m.phone || m.mcc) && (
            <Text style={s.small}>
              {site && <Text style={s.link} onPress={() => Linking.openURL(site)}>
                {m.website}</Text>}
              {m.phone && <Text> · <Text style={s.link}
                onPress={() => Linking.openURL("tel:" + m.phone)}>{m.phone}</Text></Text>}
              {m.mcc && <Text> · MCC {m.mcc}</Text>}
            </Text>
          )}
          <Text style={s.small}>
            Category: <Text style={{ color: C.text }}>
              {m.category ? catLabel(m.category) : "—"}</Text>
            {m.rule ? ` · ${m.rule.source === "user" ? "your rule" : m.rule.source + " rule"}`
                       + ` → ${catLabel(m.rule.category_primary)}` : ""}
          </Text>
          <Text style={s.small}>Source names: {m.variants.join(" · ")}</Text>
          <Text style={[s.h, { marginTop: 10 }]}>Where</Text>
          {m.locations.length === 0 ? (
            <Text style={s.small}>
              No location came through for this merchant — online, or the bank
              didn't say.</Text>
          ) : m.locations.map((l, i) => (
            <View key={i} style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
              <Text style={[s.small, { flex: 1, color: C.text }]}>
                {place(l) || "unnamed place"}
                <Text style={s.small}>  {l.n} visit{l.n === 1 ? "" : "s"} · last {mmddyy(l.last)}</Text>
              </Text>
              <Text style={s.action} onPress={() => openMap(l)}>Map</Text>
            </View>
          ))}
        </Card>
      )}
    </ScrollView>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  title: { color: C.text, fontSize: 17, fontWeight: "700" },
  h: { color: C.mut, fontSize: 11, textTransform: "uppercase", letterSpacing: 0.5 },
  small: { color: C.mut, fontSize: 12, marginTop: 2 },
  link: { color: C.accent, fontSize: 12 },
  action: { color: C.accent, fontSize: 13, fontWeight: "600",
            paddingVertical: 6, paddingHorizontal: 4 },
  input: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
           borderRadius: 10, color: C.text, fontSize: 15,
           paddingHorizontal: 12, paddingVertical: 9 },
  chip: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
          borderRadius: 999, paddingHorizontal: 10, paddingVertical: 5 },
  doneBtn: { backgroundColor: C.accent, borderRadius: 8,
             paddingHorizontal: 20, paddingVertical: 9 },
});
