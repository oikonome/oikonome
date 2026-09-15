// Items — every line item parsed off receipts, searchable, with the
// grouped totals ("how much has this household spent on coffee pods").
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Modal, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from "react-native";
import PullRefresh from "../components/pull-refresh";

import StaleBanner from "../components/stale-banner";
import { Card, H, KV } from "../components/ui";
import { patchList } from "../lib/cache";
import { useSession } from "../lib/session";
import { C, mmddyy, money } from "../lib/theme";
import { useViewer } from "../lib/viewer";

interface TagTarget { receipt_id: string; line: number;
                      description: string; tag: string }

export default function Items() {
  const { client } = useSession();
  const qc = useQueryClient();
  const viewer = useViewer();
  const [tagging, setTagging] = useState<TagTarget | null>(null);
  const [tagText, setTagText] = useState("");
  const [qText, setQText] = useState("");
  const [q, setQFinal] = useState("");
  const [group, setGroup] = useState<"item" | "store" | "month">("item");
  const tag = useMutation({
    // the target and text travel as the mutation's variables: the sheet
    // closes on the tap (onMutate clears `tagging`), and mutationFn runs
    // after that, so it must not read the state it was started from
    mutationFn: ({ target, text }: { target: TagTarget; text: string }) =>
      client!.receiptTag(target.receipt_id, target.line, text),
    onMutate: () => setTagging(null),
    onSuccess: (_r, { target, text }) => {
      // the "· tag" suffix appears now, not after the refetch
      patchList<{ rows: TagTarget[] }, TagTarget>(
        qc, ["receipt-items", q, group], "rows",
        (r) => r.receipt_id === target.receipt_id
            && r.line === target.line,
        { tag: text });
      qc.invalidateQueries({ queryKey: ["receipt-items"] });
    },
  });
  useEffect(() => {
    const id = setTimeout(() => setQFinal(qText.trim()), 300);
    return () => clearTimeout(id);
  }, [qText]);
  // capped initial render — the server can answer with 300 rows and
  // hundreds of groups, and mounting them all at once freezes the page;
  // expanding is the reader's explicit choice. A new question resets
  // both caps.
  const GROUP_CAP = 30;
  const ROW_CAP = 50;
  const [allGroups, setAllGroups] = useState(false);
  const [rowLimit, setRowLimit] = useState(ROW_CAP);
  useEffect(() => {
    setAllGroups(false);
    setRowLimit(ROW_CAP);
  }, [q, group]);
  const query = useQuery({
    queryKey: ["receipt-items", q, group],
    // the wire value is "merchant" — an unknown group silently falls
    // back to by-item on the server, so "store" would show items
    queryFn: () => client!.receiptItems(q,
      group === "store" ? "merchant" : group),
    enabled: !!client,
    // keep the last answer on screen while the next search/grouping
    // loads — otherwise both cards unmount to "Loading…" on every change
    placeholderData: (prev) => prev,
  });
  const d = query.data;
  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => query.refetch()} />}>
      <TextInput style={s.search} placeholder="search items…"
                 placeholderTextColor={C.mut} autoCapitalize="none"
                 value={qText} onChangeText={setQText} />
      <StaleBanner query={query} />
      {query.isPending && <Text style={s.center}>Loading…</Text>}
      {query.isError && !d && (
        <Text style={[s.center, { color: C.bad }]}>
          Couldn't reach the server — pull down to retry.
        </Text>
      )}
      {d && d.groups.length > 0 && (
        <Card>
          <View style={{ flexDirection: "row", alignItems: "center",
                         justifyContent: "space-between" }}>
            <H>By {group}</H>
            <View style={{ flexDirection: "row", gap: 6 }}>
              {(["item", "store", "month"] as const).map((g) => (
                <Pressable key={g}
                           style={[s.gchip, group === g && s.gchipOn]}
                           onPress={() => setGroup(g)}>
                  <Text style={{ color: group === g ? C.text : C.mut,
                                 fontSize: 12 }}>{g}</Text>
                </Pressable>
              ))}
            </View>
          </View>
          {(allGroups ? d.groups : d.groups.slice(0, GROUP_CAP))
            .map((g) => (
            <KV key={g.key}
                k={`${g.key}${g.last_date
                    ? ` · last bought ${mmddyy(g.last_date)}` : ""}`}
                v={`${money(g.total, false)} total · ${g.n}×`}
                tone="mut" />
          ))}
          {!allGroups && d.groups.length > GROUP_CAP && (
            <Text style={{ color: C.accent, fontSize: 13, marginTop: 6 }}
                  onPress={() => setAllGroups(true)}>
              show all {d.groups.length} groups
            </Text>
          )}
        </Card>
      )}
      {d && (
        <Card>
          <H>Line items</H>
          {d.rows.slice(0, rowLimit).map((r) => (
            <Pressable key={`${r.receipt_id}-${r.line}`} style={s.row}
                       onPress={viewer ? undefined
                                       : () => { setTagging(r);
                                                 setTagText(r.tag ?? ""); }}>
              <View style={{ flex: 1 }}>
                <Text style={{ color: C.text, fontSize: 13 }}
                      numberOfLines={1}>
                  {r.description}
                </Text>
                <Text style={{ color: C.mut, fontSize: 11 }}>
                  {mmddyy(r.date)} · {r.payee}
                  {r.qty != null && r.qty !== 1 ? ` · ×${r.qty}` : ""}
                  {r.tag ? ` · ${r.tag}` : ""}
                </Text>
              </View>
              <Text style={{ color: C.text, fontSize: 13,
                             fontVariant: ["tabular-nums"] }}>
                {money(r.amount)}
              </Text>
            </Pressable>
          ))}
          {d.rows.length === 0 && (
            <Text style={s.center}>
              No parsed line items yet — every line item from parsed
              receipts lands here. Attach receipts from any transaction
              (📎); items also match the Transactions search.
            </Text>
          )}
          {d.rows.length > rowLimit && (
            <Text style={{ color: C.accent, fontSize: 13, marginTop: 6 }}
                  onPress={() => setRowLimit((n) => n + 100)}>
              show more ({rowLimit} of {d.rows.length})
            </Text>
          )}
          {d.truncated && d.rows.length <= rowLimit && (
            <Text style={[s.center, { paddingTop: 4 }]}>
              showing the first 300 — narrow the search.
            </Text>
          )}
        </Card>
      )}
      <Modal visible={!!tagging} animationType="slide" transparent
             onRequestClose={() => setTagging(null)}>
        <View style={s.sheetWrap}>
          <View style={s.sheet}>
            <Text style={s.sheetTitle} numberOfLines={1}>
              Tag: {tagging?.description}
            </Text>
            <Text style={{ color: C.mut, fontSize: 12 }}>
              Tags group items across receipts ("kids", "supplements"…)
              and feed the tag report.
            </Text>
            <TextInput style={s.search} placeholder="tag (blank clears)"
                       placeholderTextColor={C.mut} autoCapitalize="none"
                       value={tagText} onChangeText={setTagText} />
            {tag.isError ? (
              <Text style={{ color: C.bad, fontSize: 12 }}>
                {String(tag.error)}
              </Text>
            ) : null}
            <View style={{ flexDirection: "row", gap: 8,
                           justifyContent: "center" }}>
              <Pressable style={[s.btn, tag.isPending && { opacity: 0.5 }]}
                         disabled={tag.isPending}
                         onPress={() => tag.mutate({ target: tagging!,
                                                     text: tagText.trim() })}>
                <Text style={s.btnText}>Save</Text>
              </Pressable>
              <Pressable style={[s.btn, { backgroundColor: C.hover }]}
                         onPress={() => setTagging(null)}>
                <Text style={[s.btnText, { color: C.mut }]}>Cancel</Text>
              </Pressable>
            </View>
          </View>
        </View>
      </Modal>
    </ScrollView>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  center: { color: C.mut, textAlign: "center", padding: 20 },
  gchip: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
           borderRadius: 999, paddingHorizontal: 10, paddingVertical: 4 },
  gchipOn: { borderColor: C.accent, backgroundColor: C.hover },
  search: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
            borderRadius: 10, color: C.text, fontSize: 14,
            marginHorizontal: 12, marginTop: 10,
            paddingHorizontal: 12, paddingVertical: 8 },
  row: { flexDirection: "row", alignItems: "center", gap: 8,
         borderTopColor: C.border, borderTopWidth: StyleSheet.hairlineWidth,
         paddingVertical: 6 },
  sheetWrap: { flex: 1, justifyContent: "flex-end",
               backgroundColor: "rgba(0,0,0,0.55)" },
  sheet: { backgroundColor: C.card, borderTopLeftRadius: 16,
           borderTopRightRadius: 16, padding: 16, gap: 10 },
  sheetTitle: { color: C.text, fontSize: 15, fontWeight: "700" },
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 18, paddingVertical: 9 },
  btnText: { color: C.text, fontSize: 14, fontWeight: "600" },
});
