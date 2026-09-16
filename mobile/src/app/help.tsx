// Help — the shipped docs and FAQ: searchable, grouped like the web's
// sidebar, with the markdown actually rendered (headings, lists, code,
// tables) instead of printed raw.
import { useQuery } from "@tanstack/react-query";
import { useLocalSearchParams } from "expo-router";
import { memo, useMemo, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, TextInput, View } from "react-native";
import PullRefresh from "../components/pull-refresh";

import StaleBanner from "../components/stale-banner";
import { Card } from "../components/ui";
import { useSession } from "../lib/session";
import { C } from "../lib/theme";

// the web sidebar's grouping order
// the server emits group IDS — map to labels, order by id
const GROUP_ORDER = ["guides", "setup", "reference"];
const GROUP_LABEL: Record<string, string> = {
  guides: "Guides", setup: "Setup & data", reference: "Reference" };

// strip inline markdown down to readable text: links keep their label,
// emphasis marks drop (RN Text can't nest arbitrary spans cheaply and
// the docs read fine without them)
const inline = (t: string) => t
  .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
  .replace(/\*\*([^*]+)\*\*/g, "$1")
  .replace(/\*([^*]+)\*/g, "$1")
  .replace(/`([^`]+)`/g, "$1");

// a tiny line-based markdown renderer — headings, bullets, code fences,
// tables (monospace), blockquotes, paragraphs. Memoized: an open topic
// must not re-parse on every keystroke in the search box.
const Markdown = memo(function Markdown({ body }: { body: string }) {
  const out: React.ReactNode[] = [];
  const lines = body.split("\n");
  let inCode = false;
  let code: string[] = [];
  lines.forEach((raw, i) => {
    const line = raw.replace(/\s+$/, "");
    if (line.trim().startsWith("```")) {
      if (inCode) {
        out.push(
          <View key={`c${i}`} style={m.codeBox}>
            <Text style={m.code}>{code.join("\n")}</Text>
          </View>);
        code = [];
      }
      inCode = !inCode;
      return;
    }
    if (inCode) { code.push(raw); return; }
    if (!line.trim()) return;
    if (/^#{1,3} /.test(line)) {
      const depth = line.match(/^#+/)![0].length;
      out.push(
        <Text key={i} style={[m.h, depth >= 3 && { fontSize: 13 }]}>
          {inline(line.replace(/^#+ /, ""))}
        </Text>);
    } else if (/^\s*[-*] /.test(line)) {
      out.push(
        <Text key={i} style={m.li}>
          {"•  "}{inline(line.replace(/^\s*[-*] /, ""))}
        </Text>);
    } else if (/^\s*\d+\. /.test(line)) {
      out.push(<Text key={i} style={m.li}>{inline(line.trim())}</Text>);
    } else if (line.trim().startsWith("|")) {
      if (/^\s*\|[\s\-|:]+\|\s*$/.test(line)) return; // separator row
      out.push(
        <Text key={i} style={m.trow}>
          {inline(line.trim().replace(/^\||\|$/g, "")
            .split("|").map((c) => c.trim()).join("   ·   "))}
        </Text>);
    } else if (line.trim().startsWith(">")) {
      out.push(
        <Text key={i} style={m.quote}>
          {inline(line.replace(/^\s*>\s?/, ""))}
        </Text>);
    } else {
      out.push(<Text key={i} style={m.p}>{inline(line)}</Text>);
    }
  });
  return <View style={{ gap: 6, marginTop: 8 }}>{out}</View>;
});

export default function Help() {
  const { client } = useSession();
  // ?topic= deep link — the web's contextual ? lands on the right
  // page's guide; mobile screens link here the same way
  const p = useLocalSearchParams<{ topic?: string }>();
  const [open, setOpen] = useState<string | null>(p.topic ?? null);
  const [q, setQ] = useState("");
  const query = useQuery({
    queryKey: ["help"],
    queryFn: () => client!.help(),
    enabled: !!client,
  });
  const needle = q.trim().toLowerCase();
  // lowercase every topic once per fetch, not once per keystroke
  const index = useMemo(() => (query.data?.topics ?? []).map((t) => ({
    t, hay: `${t.title}\n${t.body}`.toLowerCase() })), [query.data]);
  const { topics, groups, order } = useMemo(() => {
    const topics = index.filter((x) => !needle || x.hay.includes(needle))
      .map((x) => x.t);
    const groups = new Map<string, typeof topics>();
    for (const t of topics) {
      const g = (t as { group?: string }).group || "reference";
      groups.set(g, [...(groups.get(g) ?? []), t]);
    }
    const order = [...GROUP_ORDER.filter((g) => groups.has(g)),
                   ...[...groups.keys()].filter(
                     (g) => !GROUP_ORDER.includes(g))];
    return { topics, groups, order };
  }, [index, needle]);
  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => query.refetch()} />}>
      <TextInput style={s.search} placeholder="search the docs…"
                 placeholderTextColor={C.mut} autoCapitalize="none"
                 value={q} onChangeText={setQ} />
      <StaleBanner query={query} />
      {query.isPending && <Text style={s.center}>Loading…</Text>}
      {!query.isPending && topics.length === 0 && (
        <Text style={s.center}>No topics match “{q.trim()}”.</Text>
      )}
      {!query.isPending && topics.length > 0 && (
        <Text style={[s.center, { fontSize: 11, paddingVertical: 4 }]}>
          The same pages live in the repo&apos;s docs/ folder.
        </Text>
      )}
      {order.map((g) => (
        <View key={g}>
          <Text style={s.group}>{GROUP_LABEL[g] ?? g}</Text>
          {groups.get(g)!.map((t) => (
            <Card key={t.id}>
              <Pressable onPress={() => setOpen(open === t.id ? null : t.id)}>
                <Text style={s.title}>
                  {open === t.id ? "▾" : "▸"} {t.title}
                </Text>
              </Pressable>
              {open === t.id && <Markdown body={t.body} />}
            </Card>
          ))}
        </View>
      ))}
    </ScrollView>
  );
}

const m = StyleSheet.create({
  h: { color: C.text, fontSize: 14, fontWeight: "700", marginTop: 6 },
  p: { color: C.mut, fontSize: 13, lineHeight: 20 },
  li: { color: C.mut, fontSize: 13, lineHeight: 20, paddingLeft: 8 },
  quote: { color: C.mut, fontSize: 13, lineHeight: 20, paddingLeft: 10,
           borderLeftColor: C.border, borderLeftWidth: 2,
           fontStyle: "italic" },
  trow: { color: C.mut, fontSize: 12, lineHeight: 18,
          fontVariant: ["tabular-nums"] },
  codeBox: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
             borderRadius: 8, padding: 10 },
  code: { color: C.text, fontSize: 12, lineHeight: 17,
          fontFamily: "monospace" },
});

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  center: { color: C.mut, textAlign: "center", padding: 20 },
  title: { color: C.text, fontSize: 15, fontWeight: "600" },
  group: { color: C.mut, fontSize: 12, textTransform: "uppercase",
           letterSpacing: 1, marginHorizontal: 12, marginTop: 14 },
  search: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
            borderRadius: 10, color: C.text, fontSize: 14,
            marginHorizontal: 12, marginTop: 10,
            paddingHorizontal: 12, paddingVertical: 8 },
});
