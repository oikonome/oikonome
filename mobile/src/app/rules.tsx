// Rules — the learned auto-categorization surface: who taught each rule
// (you, the model, the seed set), what it claims, and the writes —
// enable/disable, change category, delete, rename a custom category.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Alert, Modal, Pressable, ScrollView, StyleSheet, Text, TextInput,
         View } from "react-native";
import PullRefresh from "../components/pull-refresh";

import StaleBanner from "../components/stale-banner";
import { Card, HelpLink } from "../components/ui";
import { errText, RuleRow, RulesPage } from "../lib/api";
import { patchList, removeFromList } from "../lib/cache";
import { useSession } from "../lib/session";
import { useViewer } from "../lib/viewer";
import { C } from "../lib/theme";
import { catLabel, filterCategories } from "../lib/pure";

const SOURCES = ["user", "llm", "model", "seed"] as const;

export default function Rules() {
  const { client } = useSession();
  const qc = useQueryClient();
  const viewer = useViewer();
  const [source, setSource] = useState<(typeof SOURCES)[number]>("user");
  const [qText, setQText] = useState("");
  const [q, setQFinal] = useState("");
  const [page, setPage] = useState(1);
  useEffect(() => {
    const id = setTimeout(() => { setQFinal(qText.trim()); setPage(1); },
                          300);
    return () => clearTimeout(id);
  }, [qText]);
  const key = ["rules", source, q, page];
  const query = useQuery({
    queryKey: key,
    queryFn: () => client!.rules(source, q, page),
    enabled: !!client,
    // keep the last page on screen while the next tab/search/page loads
    // — otherwise the card unmounts to "Loading…" on every keystroke
    placeholderData: (prev) => prev,
  });
  // a rule leaves the page it is on: one fewer in this source's count
  const dropRule = (merchant: string) => {
    removeFromList<RulesPage, RuleRow>(qc, key, "rules",
                                       (r) => r.merchant === merchant);
    qc.setQueryData<RulesPage>(key, (o) => o && {
      ...o, total: Math.max(0, o.total - 1),
      counts: { ...o.counts,
                [source]: Math.max(0, (o.counts[source] ?? 1) - 1) },
    });
  };
  const disable = useMutation({
    mutationFn: ({ merchant, disabled }:
        { merchant: string; disabled: boolean }) =>
      client!.ruleDisable(merchant, disabled),
    onSuccess: (_r, v) => {
      // disabling a learned rule makes it yours: on any other tab the
      // row leaves for "Your rules"; otherwise "· off" flips now and the
      // refetch only confirms
      if (v.disabled && source !== "user") dropRule(v.merchant);
      else
        patchList<RulesPage, RuleRow>(qc, key, "rules",
          (r) => r.merchant === v.merchant, { disabled: v.disabled });
      qc.invalidateQueries({ queryKey: ["rules"] });
      // re-enabling re-applies the rule to its rows, so the ledger moved
      if (!v.disabled) qc.invalidateQueries({ queryKey: ["transactions"] });
    },
  });
  // change a rule's category — same picker idiom as the txn detail
  const [changing, setChanging] = useState<string | null>(null);
  const [catFilter, setCatFilter] = useState("");
  const cats = useQuery({ queryKey: ["categories"],
    queryFn: () => client!.categories(),
    enabled: !!client && !!changing });
  const setRule = useMutation({
    mutationFn: ({ merchant, category }:
        { merchant: string; category: string }) =>
      client!.ruleSet(merchant, category),
    // the picker closes on the tap; the result lands behind it
    onMutate: () => setChanging(null),
    onSuccess: (r, v) => {
      // correcting a learned rule makes it yours: on the user tab the
      // row changes category, on any other tab it leaves for "Your rules"
      if (source === "user")
        patchList<RulesPage, RuleRow>(qc, key, "rules",
          (x) => x.merchant === v.merchant,
          { category_primary: v.category });
      else dropRule(v.merchant);
      qc.invalidateQueries({ queryKey: ["rules"] });
      // past rows moved with the rule, so the ledger and the verdict did
      qc.invalidateQueries({ queryKey: ["transactions"] });
      qc.invalidateQueries({ queryKey: ["today"] });
      qc.invalidateQueries({ queryKey: ["categories"] });
      Alert.alert("Rule updated",
        `${r.count} past transaction${r.count === 1 ? "" : "s"} moved.`);
    },
    onError: (e) => Alert.alert("Couldn't update", errText(e)),
  });
  // deleting a rule leaves every existing transaction alone (the prompt
  // says so), so only the rules list itself changes
  const delRule = useMutation({
    mutationFn: (merchant: string) => client!.ruleDelete(merchant),
    onSuccess: (_r, merchant) => {
      dropRule(merchant);
      qc.invalidateQueries({ queryKey: ["rules"] });
    },
    onError: (e) => Alert.alert("Couldn't delete", errText(e)),
  });
  const del = (merchant: string) =>
    Alert.alert("Delete rule",
      `Stop auto-categorizing "${merchant}"? Existing transactions `
      + "keep their category.",
      [{ text: "Delete", style: "destructive",
         onPress: () => delRule.mutate(merchant) },
       { text: "Cancel", style: "cancel" }]);
  const d = query.data;
  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => query.refetch()} />}>
      <View style={s.tabs}>
        {SOURCES.filter((x) => x === source || x === "user"
            || (d?.counts[x] ?? 1) > 0).map((x) => (
          <Pressable key={x} style={[s.tab, source === x && s.tabOn]}
                     onPress={() => setSource(x)}>
            <Text style={{ color: source === x ? C.text : C.mut,
                           fontWeight: "600", fontSize: 13 }}>
              {x === "user" ? "Your rules"
                : x === "llm" ? "Model-learned"
                : x === "model" ? "Learned" : "Built-in"}
              {d ? ` · ${d.counts[x] ?? 0}` : ""}
            </Text>
          </Pressable>
        ))}
      </View>
      {/* the web's per-group note — the promotion rule lives here */}
      <Text style={[s.mut, { marginHorizontal: 12 }]}>
        {source === "user" ? "corrections you made"
          : source === "llm"
            ? "inferred by the local model; correcting one makes it yours"
          : source === "model"
            ? "the classifier trained on your own corrections; "
              + "correcting one makes it yours"
            : "shipped starting points"}
      </Text>
      <TextInput style={s.search}
                 placeholder="search merchants or categories"
                 placeholderTextColor={C.mut} autoCapitalize="none"
                 value={qText} onChangeText={setQText} />
      <StaleBanner query={query} />
      {query.isPending && <Text style={s.center}>Loading…</Text>}
      {query.isError && !d && (
        <Text style={[s.center, { color: C.bad }]}>
          Couldn't reach the server — pull down to retry.
        </Text>
      )}
      {d && (
        <Card style={query.isPlaceholderData ? { opacity: 0.6 } : undefined}>
          {disable.isError && (
            <Text style={{ color: C.bad, fontSize: 12 }}>
              {String(disable.error)}
            </Text>
          )}
          {d.rules.map((r) => {
            // per-row pending: the tapped row dims and ignores a second
            // tap (which would flip it straight back); the rest stay live
            const busy = (disable.isPending
                            && disable.variables?.merchant === r.merchant)
              || (delRule.isPending && delRule.variables === r.merchant);
            return (
            <View key={r.merchant} style={[s.ruleRow, busy && { opacity: 0.5 }]}>
              <View style={{ flex: 1 }}>
                <Text style={{ color: r.disabled ? C.mut : C.text,
                               fontSize: 14, fontWeight: "600" }}
                      numberOfLines={1}>
                  {r.merchant}
                </Text>
                <Text style={{ color: C.mut, fontSize: 12 }}>
                  {catLabel(r.category_primary)}
                  {r.disabled ? " · off"
                    : r.count ? ` · ${r.count} txns` : ""}
                </Text>
              </View>
              {!viewer && (<>
                <Text style={{ color: C.accent, fontSize: 12, padding: 6 }}
                      onPress={() => { setCatFilter("");
                                       setChanging(r.merchant); }}>
                  Change
                </Text>
                <Text style={{ color: C.accent, fontSize: 12, padding: 6 }}
                      onPress={() => !busy && disable.mutate({
                        merchant: r.merchant, disabled: !r.disabled })}>
                  {r.disabled ? "Enable" : "Disable"}
                </Text>
                <Text style={{ color: C.bad, fontSize: 12, padding: 6 }}
                      onPress={() => !busy && del(r.merchant)}>
                  ✕
                </Text>
              </>)}
            </View>
            );
          })}
          {d.rules.length === 0 && (
            <Text style={s.center}>
              No rules yet — they appear as Oikonome learns your
              merchants: built-in rules match on your first import, the
              model classifies the rest when an AI is configured
              (Settings), and every category correction you make becomes
              a rule of your own.
            </Text>
          )}
          {d.total > d.per_page && (
            <Text style={[s.mut, { textAlign: "center", marginTop: 6 }]}>
              showing {d.rules.length} of {d.total} · page {page} of{" "}
              {Math.max(1, Math.ceil(d.total / d.per_page))}
            </Text>
          )}
          <View style={{ flexDirection: "row", gap: 16,
                         justifyContent: "center", marginTop: 8 }}>
            {page > 1 && (
              <Text style={{ color: C.accent, fontSize: 13 }}
                    onPress={() => setPage((p) => p - 1)}>‹ Prev</Text>
            )}
            {d.total > page * d.per_page && (
              <Text style={{ color: C.accent, fontSize: 13 }}
                    onPress={() => setPage((p) => p + 1)}>Next ›</Text>
            )}
          </View>
        </Card>
      )}
      {d && (
        <Text style={[s.mut, { paddingBottom: 8 }]}>
          {/* all four provenances — the trained classifier counts too */}
          {(d.counts.user + d.counts.llm + d.counts.seed
            + (d.counts.model ?? 0)).toLocaleString()}
          {" rules sorting your transactions · "}{d.counts.user} yours ·{" "}
          {d.counts.llm} model ·{" "}
          {(d.counts.model ?? 0) > 0 ? `${d.counts.model} learned · ` : ""}
          {d.counts.seed} built-in.{"\n"}
          Rules are learned, never hand-written. Correcting or disabling
          one makes it yours.
        </Text>
      )}
      {!viewer && <RenameCategoryCard />}

      <Modal visible={!!changing} animationType="slide" transparent
             onRequestClose={() => setChanging(null)}>
        <View style={s.sheetWrap}>
          <View style={s.sheet}>
            <Text style={s.sheetTitle}>Category for {changing}</Text>
            <TextInput style={s.search2} placeholder="filter…"
                       placeholderTextColor={C.mut} autoCapitalize="none"
                       value={catFilter} onChangeText={setCatFilter} />
            <ScrollView style={{ maxHeight: 400 }}>
              {/* the web's three groups, in its order — the taxonomy is
                  part of the answer, not decoration */}
              {(() => {
                // matched on the DISPLAY label each row renders, so
                // typing what you can see finds it
                const flt = (l: string[]) => filterCategories(l, catFilter);
                const spend = flt(cats.data?.plaid_spend ?? []);
                const flow = flt(cats.data?.plaid_flow ?? []);
                const std = new Set([...(cats.data?.plaid_spend ?? []),
                                     ...(cats.data?.plaid_flow ?? [])]);
                const custom = flt((cats.data?.categories ?? [])
                  .filter((c) => c && !std.has(c)));
                const row = (c: string) => (
                  <Pressable key={c} style={s.catRow}
                             disabled={setRule.isPending}
                             onPress={() => setRule.mutate(
                               { merchant: changing!, category: c })}>
                    <Text style={{ color: C.text, fontSize: 15 }}>
                      {catLabel(c)}
                    </Text>
                  </Pressable>
                );
                const head = (t: string) => (
                  <Text key={t} style={{ color: C.mut, fontSize: 11,
                                         fontWeight: "700", marginTop: 8,
                                         textTransform: "uppercase" }}>
                    {t}
                  </Text>
                );
                return (
                  <>
                    {spend.length > 0 && head("Spending")}
                    {spend.map(row)}
                    {flow.length > 0 && head("Transfers & income")}
                    {flow.map(row)}
                    {custom.length > 0 && head("Your own categories")}
                    {custom.map(row)}
                  </>
                );
              })()}
            </ScrollView>
            <Pressable style={[s.closeBtn]}
                       onPress={() => setChanging(null)}>
              <Text style={{ color: C.mut, fontWeight: "600" }}>Close</Text>
            </Pressable>
          </View>
        </View>
      </Modal>
      <HelpLink topic="rules" />
    </ScrollView>
  );
}

// rename a free-form custom category everywhere it appears — the
// web Rules page's second card
function RenameCategoryCard() {
  const { client } = useSession();
  const qc = useQueryClient();
  const [oldName, setOldName] = useState("");
  const [newName, setNewName] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  // only names YOU created rename — standard bank/Plaid labels stay
  // fixed, so the "from" side is a picker over customs, never free text
  const cats = useQuery({ queryKey: ["categories"],
    queryFn: () => client!.categories(), enabled: !!client });
  const std = new Set([...(cats.data?.plaid_spend ?? []),
                       ...(cats.data?.plaid_flow ?? [])]);
  const customs = (cats.data?.categories ?? [])
    .filter((c) => c && !std.has(c));
  const run = useMutation({
    mutationFn: () => client!.categoryRename(oldName.trim(),
                                             newName.trim()),
    onSuccess: (r) => {
      const n = r.overrides + (r.primaries ?? 0) + (r.rules ?? 0)
        + (r.buckets ?? 0);
      setMsg(n === 0
        ? `Nothing used “${r.old}” — name unchanged elsewhere.`
        : `Renamed “${r.old}” → “${r.new}” (${r.overrides} overrides, `
          + `${r.primaries ?? 0} rows, ${r.rules ?? 0} rules, `
          + `${r.buckets ?? 0} budget matches).`);
      setOldName(""); setNewName("");
      qc.invalidateQueries({ queryKey: ["categories"] });
      qc.invalidateQueries({ queryKey: ["rules"] });
      qc.invalidateQueries({ queryKey: ["transactions"] });
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
    onError: (e) => setMsg(errText(e)),
  });
  return (
    <Card>
      <Text style={{ color: C.text, fontSize: 15, fontWeight: "700" }}>
        Rename a category
      </Text>
      <Text style={{ color: C.mut, fontSize: 12, marginTop: 2 }}>
        Only custom names you created (✎ custom category…). Standard
        bank/Plaid labels stay fixed. Rename rewrites every transaction
        override, merchant rule, and budget carve-out that used the old
        name.
      </Text>
      {customs.length === 0 ? (
        <Text style={{ color: C.mut, fontSize: 12, marginTop: 6 }}>
          no custom categories yet
        </Text>
      ) : (
        <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6,
                       marginTop: 6 }}>
          {customs.map((c) => (
            <Pressable key={c}
                       style={[s.chip, oldName === c && s.chipOn]}
                       onPress={() => setOldName(c)}>
              <Text style={{ color: oldName === c ? C.text : C.mut,
                             fontSize: 12 }}>
                {catLabel(c)}
              </Text>
            </Pressable>
          ))}
        </View>
      )}
      <View style={{ flexDirection: "row", gap: 6, marginTop: 6 }}>
        <TextInput style={[s.search, { flex: 1, marginHorizontal: 0,
                                       marginTop: 0 }]}
                   placeholder="new name" placeholderTextColor={C.mut}
                   autoCapitalize="none"
                   value={newName} onChangeText={setNewName} />
        <Pressable style={{ backgroundColor: C.accent, borderRadius: 8,
                            paddingHorizontal: 12,
                            justifyContent: "center",
                            opacity: !oldName.trim() || !newName.trim()
                              || run.isPending ? 0.5 : 1 }}
                   disabled={!oldName.trim() || !newName.trim()
                             || run.isPending}
                   onPress={() => run.mutate()}>
          <Text style={{ color: C.text, fontSize: 13,
                         fontWeight: "600" }}>rename</Text>
        </Pressable>
      </View>
      {msg ? (
        <Text style={{ color: msg.startsWith("Renamed")
                         ? C.good : C.bad, fontSize: 12, marginTop: 4 }}
              onPress={() => setMsg(null)}>{msg}</Text>
      ) : null}
    </Card>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  center: { color: C.mut, textAlign: "center", padding: 20 },
  mut: { color: C.mut, fontSize: 12, textAlign: "center", paddingTop: 8 },
  tabs: { flexDirection: "row", gap: 8, paddingHorizontal: 12,
          paddingTop: 10 },
  tab: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
         borderRadius: 999, paddingHorizontal: 14, paddingVertical: 6 },
  tabOn: { borderColor: C.accent, backgroundColor: C.hover },
  chip: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
          borderRadius: 14, paddingHorizontal: 10, paddingVertical: 4 },
  chipOn: { borderColor: C.accent, backgroundColor: C.hover },
  search: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
            borderRadius: 10, color: C.text, fontSize: 14,
            marginHorizontal: 12, marginTop: 10,
            paddingHorizontal: 12, paddingVertical: 8 },
  ruleRow: { flexDirection: "row", alignItems: "center", gap: 8,
             borderTopColor: C.border,
             borderTopWidth: StyleSheet.hairlineWidth,
             paddingVertical: 8 },
  sheetWrap: { flex: 1, justifyContent: "flex-end",
               backgroundColor: "rgba(0,0,0,0.55)" },
  sheet: { backgroundColor: C.card, borderTopLeftRadius: 16,
           borderTopRightRadius: 16, padding: 16, gap: 10 },
  sheetTitle: { color: C.text, fontSize: 16, fontWeight: "700" },
  search2: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
             borderRadius: 8, color: C.text, fontSize: 14,
             paddingHorizontal: 10, paddingVertical: 7 },
  catRow: { paddingVertical: 9, borderTopColor: C.border,
            borderTopWidth: StyleSheet.hairlineWidth },
  closeBtn: { backgroundColor: C.hover, borderRadius: 8,
              paddingHorizontal: 20, paddingVertical: 9,
              alignSelf: "center" },
});
