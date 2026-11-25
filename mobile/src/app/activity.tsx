// Activity — the household's log: who changed which category, bill,
// note, split, rule… and when. Everyone reads it (everyone sees
// everything); the server composes each sentence, so this screen only
// puts the person and the time in front of it. Filter chips by person
// and by kind; "show older" pages by the last row's timestamp.
// Mirrors the Activity card on webapp/src/pages/Settings.tsx (Users).
import { FlashList } from "@shopify/flash-list";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { memo, useCallback, useEffect, useMemo, useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";
import PullRefresh from "../components/pull-refresh";

import { FailedLine, StaleLine } from "../components/loaded";
import { HelpLink } from "../components/ui";
import { type ActivityPage } from "../lib/api";
import { EMPTY_LINES, pagesAfter } from "../lib/pure";
import { useSession } from "../lib/session";
import { C } from "../lib/theme";

const KIND_LABEL: Record<string, string> = {
  category: "categories", split: "splits", note: "notes", bill: "bills",
  rule: "rules", receipt: "receipts", reimbursement: "reimbursements",
  business: "business", merchant: "merchants", account: "accounts",
  settings: "settings",
};

// MM/DD/YY HH:MM on the phone's own clock — the log is read by the
// people in the house, whose clocks agree
export function when(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}/${p(d.getDate())}/${String(d.getFullYear()).slice(2)}`
    + ` ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// one row of the log; memoised, so "show older" appending a page does
// not repaint the pages already on screen
const ActivityRow = memo(function ActivityRow({ at, who, summary, first }:
    { at: string; who: string | null; summary: string; first: boolean }) {
  return (
    <View style={[s.row, first && s.rowFirst]}>
      <Text style={s.mut}>{when(at)}</Text>
      <Text style={s.msg}>
        {who ? (<>
          <Text style={{ fontWeight: "600" }}>{who}</Text>
          {" "}{summary}
        </>) : summary.charAt(0).toUpperCase() + summary.slice(1)}
      </Text>
    </View>
  );
});

export default function Activity() {
  const { client } = useSession();
  const [who, setWho] = useState("");
  const [kind, setKind] = useState("");
  const qc = useQueryClient();
  const [cursor, setCursor] = useState<string | null>(null);
  // the filter the held pages answer; they stay on screen (dimmed) while
  // another filter loads, and go only when its answer, or its failure,
  // arrives
  const filterKey = `${who}|${kind}`;
  const [held, setHeld] = useState<{ key: string;
    pages: { cursor: string | null; page: ActivityPage }[] }>(
    // nothing held answers any filter yet
    { key: "\u0000", pages: [] });
  const me = useQuery({ queryKey: ["me"], queryFn: () => client!.me(),
                        enabled: !!client });
  const params: Record<string, string> = {};
  if (who) params.who = who;
  if (kind) params.kind = kind;
  if (cursor) params.before = cursor;
  const query = useQuery({
    queryKey: ["activity", params],
    queryFn: () => client!.activity(params),
    enabled: !!client,
    placeholderData: (prev) => prev,
  });
  // dataUpdatedAt too: a refetch that answers the same log hands back the
  // same object, and the pages it answers must still be taken as current
  useEffect(() => {
    if (!query.data || query.isPlaceholderData) return;
    const page = query.data;
    setHeld((prev) => pagesAfter(prev, filterKey, cursor, page));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query.data, query.dataUpdatedAt, query.isPlaceholderData]);
  // a new filter starts from its first page; what is held stays up until
  // that page answers
  const reset = () => setCursor(null);
  const current = held.key === filterKey;
  // another filter's rows go once this one's read has failed
  const pages = current || !query.isError ? held.pages.map((x) => x.page) : [];
  const dimmed = !current || query.isPlaceholderData;
  const first = pages[0] ?? query.data;
  const rows = useMemo(() => pages.flatMap((p) => p.rows), [pages]);
  const last = pages[pages.length - 1];
  const mine = me.data?.email?.toLowerCase();
  const nameOf = useCallback((a: string) => a === mine ? "you"
    : a.startsWith("script:") ? `the ${a.slice(7)} script` : a, [mine]);
  const actors = first?.actors ?? [];
  const kinds = first?.kinds ?? [];
  // In a household of one every row would open with the same name, so the
  // name goes (as on the web); a script's rows keep theirs
  const members = useQuery({ queryKey: ["members"],
    queryFn: () => client!.members(), enabled: !!client });
  const solo = members.data?.users.length === 1;
  const renderRow = useCallback(({ item: r, index }:
      { item: ActivityPage["rows"][number]; index: number }) => (
    <ActivityRow at={r.at}
                 who={solo && !r.actor.startsWith("script:")
                   ? null : nameOf(r.actor)}
                 summary={r.summary} first={index === 0} />),
    [nameOf, solo]);

  const header = (
    <>
      <StaleLine query={query} />
      <View style={{ paddingHorizontal: 12, paddingTop: 10, gap: 4 }}>
        <Text style={s.mut}>
          Who changed what, and when — categories, splits, notes, bills,
          rules, receipts, merchant names, settings. What the app does on
          its own is not listed; the log keeps two years.
          {" "}<HelpLink topic="security" />
        </Text>
      </View>
      {query.isPending && rows.length === 0 && (
        <Text style={s.center}>Loading…</Text>)}
      {query.isError && rows.length === 0 && (
        <FailedLine q={query} what="the activity log" />)}
      {actors.length > 1 && (
        <View style={s.filterRow}>
          {["", ...actors].map((a) => (
            <Pressable accessibilityRole="button"
                       key={a || "*"} style={[s.fchip, who === a && s.fchipOn]}
                       onPress={() => { setWho(a); reset(); }}>
              <Text style={{ color: who === a ? C.text : C.mut, fontSize: 12 }}>
                {a ? nameOf(a) : "everyone"}</Text>
            </Pressable>
          ))}
        </View>
      )}
      {rows.length > 0 || kind ? (
        <View style={[s.filterRow, { marginTop: 6 }]}>
          {["", ...kinds].map((k) => (
            <Pressable accessibilityRole="button"
                       key={k || "*"} style={[s.fchip, kind === k && s.fchipOn]}
                       onPress={() => { setKind(k); reset(); }}>
              <Text style={{ color: kind === k ? C.text : C.mut, fontSize: 12 }}>
                {k ? (KIND_LABEL[k] ?? k) : "everything"}</Text>
            </Pressable>
          ))}
        </View>
      ) : null}
      {/* empty only from this filter's own answer; a filtered log that
          answered empty says so, not that the log is */}
      {current && !query.isPlaceholderData && !query.isError
        && rows.length === 0 && (
        <Text style={s.center}>
          {who || kind ? EMPTY_LINES.filtered
            : "Nothing yet — the first category you correct, note you "
              + "leave or bill you edit lands here, with your name on it."}
        </Text>
      )}
    </>
  );
  const footer = rows.length > 0 ? (
    <View style={s.rowLast}>
      {last?.more && last.next ? (
        <Pressable accessibilityRole="button" style={s.more} disabled={query.isFetching}
                   onPress={() => setCursor(last.next)}>
          <Text style={{ color: C.accent, fontSize: 13 }}>
            {query.isFetching ? "loading…" : "show older"}</Text>
        </Pressable>
      ) : null}
    </View>
  ) : null;

  // the rows recycle: a log that has been paged through a few times is
  // hundreds of lines, and only the visible ones are mounted
  return (
    <FlashList
      data={rows}
      keyExtractor={(r) => r.id}
      renderItem={renderRow}
      extraData={mine}
      style={dimmed && rows.length > 0 ? { ...s.wrap, opacity: 0.55 } : s.wrap}
      ListHeaderComponent={header}
      ListFooterComponent={footer}
      contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                   progressBackgroundColor={C.card}
                                   onRefresh={() => { reset();
                                     return qc.invalidateQueries(
                                       { queryKey: ["activity"] }); }} />}
    />
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  center: { color: C.mut, textAlign: "center", padding: 20 },
  // the rows draw the card the log used to sit in: sides on every row,
  // the first row its top, the footer its bottom
  row: { borderTopColor: C.border, borderTopWidth: StyleSheet.hairlineWidth,
         paddingVertical: 8, gap: 2, backgroundColor: C.card,
         borderLeftWidth: 1, borderRightWidth: 1, borderColor: C.border,
         paddingHorizontal: 14, marginHorizontal: 12 },
  rowFirst: { marginTop: 8, borderTopWidth: 1, borderTopLeftRadius: C.r,
              borderTopRightRadius: C.r, borderTopColor: C.border },
  rowLast: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
             borderTopWidth: 0, borderBottomLeftRadius: C.r,
             borderBottomRightRadius: C.r, marginHorizontal: 12,
             paddingHorizontal: 14, paddingBottom: 6 },
  msg: { color: C.text, fontSize: 13, lineHeight: 18 },
  mut: { color: C.mut, fontSize: 12 },
  more: { paddingTop: 10, alignItems: "center" },
  filterRow: { flexDirection: "row", flexWrap: "wrap", gap: 6,
               paddingHorizontal: 12, paddingTop: 8 },
  fchip: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
           borderRadius: 999, paddingHorizontal: 12, paddingVertical: 5 },
  fchipOn: { borderColor: C.accent, backgroundColor: C.hover },
});
