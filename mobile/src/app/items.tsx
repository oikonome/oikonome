// Receipts — the ones snapped before their transaction arrived (waiting
// to be matched), then every line item parsed off receipts, searchable,
// with the grouped totals ("how much has this household spent on coffee
// pods"). The web's Receipts page, top to bottom.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Modal, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from "react-native";
import PullRefresh from "../components/pull-refresh";

import { EmptyState, FailedLine, StaleLine } from "../components/loaded";
import { EMPTY_LINES } from "../lib/pure";
import { Card, H, KV, Skeleton } from "../components/ui";
import WaitingReceiptsCard from "../components/waiting-receipts";
import { errText, UNKNOWN_STORE } from "../lib/api";
import { patchList } from "../lib/cache";
import { useRefetchWhenShown } from "../lib/focus";
import { useSession } from "../lib/session";
import { C, mmddyy, money } from "../lib/theme";
import { useViewer } from "../lib/viewer";
import { WAITING_KEY, useWaitingReceipts } from "../lib/waiting";
import SegmentedControl from "../components/segmented-control";
import { fieldA11y } from "../components/field";
import { useSheetAnimation } from "../lib/a11y";

interface TagTarget { receipt_id: string; line: number;
                      description: string; tag: string }

export default function Items() {
  const sheetAnim = useSheetAnimation();
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
  // a sync can match a waiting receipt while this screen sits behind
  // another — catch up whenever it is shown
  const waiting = useWaitingReceipts();
  useRefetchWhenShown(waiting, WAITING_KEY);
  const d = query.data;
  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => Promise.all([
                                        waiting.refetch(), query.refetch()])} />}>
      <WaitingReceiptsCard />
      <TextInput {...fieldA11y("Search items")} style={s.search} placeholder="search items…"
                 placeholderTextColor={C.mut} autoCapitalize="none"
                 value={qText} onChangeText={setQText} />
      <StaleLine query={query} />
      {query.isPending && <Skeleton />}
      {query.isError && !d && (
        <FailedLine q={query} what="line items" />
      )}
      {d && d.groups.length > 0 && (
        <Card>
          <View style={{ flexDirection: "row", alignItems: "center",
                         justifyContent: "space-between" }}>
            <H>By {group}</H>
            <SegmentedControl label="Group items" size="sm" value={group}
              onChange={setGroup} style={{ alignSelf: "center" }}
              options={(["item", "store", "month"] as const).map((g) => (
                { value: g, label: `By ${g}` }))} />
          </View>
          {(allGroups ? d.groups : d.groups.slice(0, GROUP_CAP))
            .map((g) => (
            <KV key={g.key ?? "\u0000unknown"}
                k={`${g.key ?? UNKNOWN_STORE}${g.last_date
                    ? ` · last bought ${mmddyy(g.last_date)}` : ""}`}
                // the web's two columns: how many were bought, and on
                // how many receipts (receipts.search_items)
                // an older server sends neither: its line, from the count
                v={g.bought == null || g.receipts == null
                   ? `${money(g.total, false)} total · ${g.n}×`
                   : `${money(g.total, false)} total · bought ${g.bought}`
                     + ` · receipts ${g.receipts}`}
                tone="mut" />
          ))}
          {/* a total that may hold a purchase twice says so, in the
              server's words (the web's line under the table) */}
          {d.groups.filter((g) => g.note).map((g) => (
            <Text key={`note-${g.key ?? "\u0000unknown"}`}
                  style={{ color: C.mut, fontSize: 12, marginTop: 4 }}>
              {g.key ?? UNKNOWN_STORE}: {g.note}
            </Text>
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
            <Pressable accessibilityRole="button" key={`${r.receipt_id}-${r.line}`}
                       style={[s.row, r.counts === false && { opacity: 0.55 }]}
                       onPress={viewer ? undefined
                                       : () => { setTagging(r);
                                                 setTagText(r.tag ?? ""); }}>
              <View style={{ flex: 1 }}>
                <Text style={{ color: C.text, fontSize: 13 }}
                      numberOfLines={1}>
                  {r.description}
                </Text>
                <Text style={{ color: C.mut, fontSize: 11 }}>
                  {mmddyy(r.date)} · {r.payee ?? UNKNOWN_STORE}
                  {r.txn_id == null ? " · waiting" : ""}
                  {r.qty != null && r.qty !== 1 ? ` · ×${r.qty}` : ""}
                  {r.tag ? ` · ${r.tag}` : ""}
                  {/* listed, never added up, or possibly counted
                      twice: the server's words (specs/receipts.md) */}
                  {r.note ? ` · ${r.note}` : ""}
                </Text>
              </View>
              <Text style={{ color: C.text, fontSize: 13,
                             fontVariant: ["tabular-nums"] }}>
                {money(r.amount)}
              </Text>
            </Pressable>
          ))}
          {/* a search miss is not an empty catalog (the web's words) */}
          {d.rows.length === 0 && (q
            ? <EmptyState title={EMPTY_LINES.filtered} />
            : <EmptyState title={EMPTY_LINES.lineItems} to="/(tabs)/transactions"
                          action={EMPTY_LINES.lineItemsAction} />)}
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
      <Modal visible={!!tagging} animationType={sheetAnim} transparent
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
            <TextInput {...fieldA11y("Tag (blank clears)")}
                       style={s.search} placeholder="tag (blank clears)"
                       placeholderTextColor={C.mut} autoCapitalize="none"
                       value={tagText} onChangeText={setTagText} />
            {tag.isError ? (
              <Text style={{ color: C.bad, fontSize: 12 }}>
                {errText(tag.error)}
              </Text>
            ) : null}
            <View style={{ flexDirection: "row", gap: 8,
                           justifyContent: "center" }}>
              <Pressable accessibilityRole="button"
                         style={[s.btn, tag.isPending && { opacity: 0.5 }]}
                         disabled={tag.isPending}
                         onPress={() => tag.mutate({ target: tagging!,
                                                     text: tagText.trim() })}>
                <Text style={s.btnText}>Save</Text>
              </Pressable>
              <Pressable accessibilityRole="button" style={[s.btn, { backgroundColor: C.hover }]}
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
  search: { backgroundColor: C.card, borderColor: C.field, borderWidth: 1,
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
  btn: { backgroundColor: C.btn, borderRadius: 8,
         paddingHorizontal: 18, paddingVertical: 9 },
  btnText: { color: C.text, fontSize: 14, fontWeight: "600" },
});
