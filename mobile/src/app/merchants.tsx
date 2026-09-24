// Merchants — the one place a person can correct who a payee is.
// Mirrors the web's Merchants page: one list with search, sort and the
// same filters; a row is a logo, a name and one line of facts; tapping
// a row opens the merchant's own screen (facts, places, source names,
// Rename / Merge into). The proposer's offers are the "Needs a look"
// queue above the list, one line each, the evidence and both sides behind "details".
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "expo-router";
import { memo, useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from "react-native";
import PullRefresh from "../components/pull-refresh";

import StaleBanner from "../components/stale-banner";
import { Card } from "../components/ui";
import { errText, MerchantCatalog, MergeProposal, MergeSideFacts } from "../lib/api";
import { removeFromList } from "../lib/cache";
import { useSession } from "../lib/session";
import { useDemo, useViewer } from "../lib/viewer";
import { C, mmddyy, money } from "../lib/theme";
import { catLabel, looksUnnamed, MERCHANT_FILTERS, MERCHANT_ORDERS,
         serverOrder, sortMerchants } from "../lib/pure";
import type { MerchantFilter, MerchantOrder } from "../lib/pure";
import MerchantAvatar from "../components/merchant-avatar";

// the server caps the catalog at its 200 most frequent merchants;
// the long tail is reachable only through search
const CATALOG_LIMIT = 200;
type CatalogRow = MerchantCatalog["merchants"][number];

// the server caps the `variants` array it returns, so the array's length
// is NOT the number of source names — a merchant with 3,000 spellings
// would claim the cap. `variant_count` is the true count; fall back to
// the array only for an older server that doesn't send it.
const variantCount = (m: { variants: string[]; variant_count?: number }) =>
  m.variant_count ?? m.variants.length;

export default function Merchants() {
  const { client } = useSession();
  const qc = useQueryClient();
  const router = useRouter();
  const viewer = useViewer();
  const demo = useDemo();
  const [qText, setQText] = useState("");
  const [q, setQ] = useState("");
  useEffect(() => {
    const id = setTimeout(() => setQ(qText.trim()), 300);
    return () => clearTimeout(id);
  }, [qText]);
  const [order, setOrder] = useState<MerchantOrder>("count");
  const [flt, setFlt] = useState<MerchantFilter>("all");
  const [recentOpen, setRecentOpen] = useState(false);

  const key = ["merchants", q, serverOrder(order)];
  const query = useQuery({
    queryKey: key,
    queryFn: () => client!.merchantCatalog(q, serverOrder(order)),
    enabled: !!client,
    // keep the last page on screen while refetching — a debounce fire
    // shouldn't flash "Loading…"
    placeholderData: (prev) => prev,
  });
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["merchants"] });
    qc.invalidateQueries({ queryKey: ["merchant-detail"] });
    // every surface that prints a payee is stale now — including a
    // mounted bill-history screen, which native-stack keeps alive
    qc.invalidateQueries({ queryKey: ["transactions"] });
    qc.invalidateQueries({ queryKey: ["today"] });
    qc.invalidateQueries({ queryKey: ["payee-history"] });
    qc.invalidateQueries({ queryKey: ["bills"] });
    // the "merges" alert retires on the server the moment the queue
    // empties; the Alerts screen must not keep showing it lit
    qc.invalidateQueries({ queryKey: ["alerts"] });
  };
  const undo = useMutation({
    mutationFn: (id: number) => client!.merchantUndo(id),
    onSuccess: (_r, id) => {
      // the journal entry leaves at once; the refetch restores the row
      removeFromList<MerchantCatalog, MerchantCatalog["recent"][number]>(
        qc, key, "recent", (r) => r.id === id);
      invalidate();
    },
    onError: (e) => Alert.alert("Couldn't undo", errText(e)),
  });
  const decide = useMutation({
    mutationFn: ({ pid, action }: { pid: string; action: "approve" | "reject" }) =>
      client!.merchantMerge(pid, action),
    onSuccess: (_r, v) => {
      removeFromList<MerchantCatalog, MergeProposal>(qc, key, "merges", (x) => x.id === v.pid);
      if (v.action === "approve") invalidate();
      else {
        // rejecting the last offer empties the queue too, and with it
        // the alert that pointed here
        qc.invalidateQueries({ queryKey: ["alerts"] });
        qc.invalidateQueries({ queryKey: ["today"] });
      }
    },
    onError: (e) => Alert.alert("Couldn't decide", errText(e)),
  });
  // "merge all" walks the queue one decision at a time: each is its own
  // journal entry, so a wrong one is undone alone
  const decideAll = useMutation({
    mutationFn: async (pids: string[]) => {
      let n = 0;
      for (const pid of pids) { await client!.merchantMerge(pid, "approve"); n += 1; }
      return n;
    },
    onSuccess: (n) => { invalidate(); Alert.alert("Merged", `${n} pair${n === 1 ? "" : "s"}.`); },
    onError: (e) => { invalidate(); Alert.alert("Stopped", errText(e)); },
  });
  const openRow = useCallback((display: string) =>
    router.push({ pathname: "/merchant", params: { display } } as never), [router]);

  const rows = useMemo(
    () => sortMerchants(query.data?.merchants ?? [], order, flt, new Date()),
    [query.data, order, flt]);
  const recent = query.data?.recent ?? [];
  const merges = query.data?.merges ?? [];
  const canEdit = !viewer && !demo;

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ padding: 12, gap: 10 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => query.refetch()} />}>
      <StaleBanner query={query} />
      <Text style={s.mut}>
        Every name your ledger shows. Open one to see its source strings,
        where it has been seen, and to rename or merge it.
      </Text>
      <TextInput style={s.search} value={qText} onChangeText={setQText}
                 placeholder="search merchants…" placeholderTextColor={C.mut}
                 autoCapitalize="none" autoCorrect={false} />
      {/* the web's sort select, in the app's segmented idiom */}
      <ScrollView horizontal showsHorizontalScrollIndicator={false}
                  contentContainerStyle={s.sortRow}>
        {MERCHANT_ORDERS.map(([v, label]) => (
          <Pressable key={v} style={[s.seg, order === v && s.segOn]}
                     onPress={() => setOrder(v)}>
            <Text style={{ color: order === v ? C.text : C.mut, fontSize: 13,
                           fontWeight: "600" }}>{label}</Text>
          </Pressable>
        ))}
      </ScrollView>
      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
        {MERCHANT_FILTERS.map(([v, label]) => (
          <Pressable key={v} style={[s.chip, flt === v && s.chipOn]} onPress={() => setFlt(v)}>
            <Text style={{ color: flt === v ? C.accent : C.mut, fontSize: 12 }}>{label}</Text>
          </Pressable>
        ))}
      </View>
      {!viewer && demo && (
        <Text style={{ color: C.warn, fontSize: 12, textAlign: "center" }}>
          Renames are locked on the demo — the login is shared, so a change
          would follow the next visitor. Everything else is free to explore.
        </Text>
      )}

      {merges.length > 0 && (
        <NeedsALook merges={merges} canEdit={canEdit}
                    busy={decide.isPending || decideAll.isPending}
                    onDecide={(pid, action) => decide.mutate({ pid, action })}
                    onMergeAll={() => decideAll.mutate(merges.map((p) => p.id))}
                    onOpen={openRow} />
      )}

      {recent.length > 0 && (
        <Card>
          <Pressable onPress={() => setRecentOpen((v) => !v)}
                     style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
            <Text style={s.small}>{recentOpen ? "▾" : "▸"}</Text>
            <Text style={s.name}>Recent changes</Text>
            <Text style={s.small}>
              {recent.length} change{recent.length === 1 ? "" : "s"} · undo here</Text>
          </Pressable>
          {recentOpen && recent.map((r) => (
            <View key={r.id} style={s.recent}>
              <View style={{ flex: 1 }}>
                <Text style={s.body} numberOfLines={2}>
                  <Text style={s.mut}>{r.raw_merchant}</Text>
                  {"  →  "}{r.to_canonical}
                </Text>
                <Text style={s.small}>
                  {mmddyy(r.at)}
                  {r.from_canonical ? ` · was ${r.from_canonical}` : ""}
                </Text>
              </View>
              {canEdit && (
                <Text style={[s.action, undo.isPending && undo.variables === r.id
                                && { opacity: 0.5 }]}
                      onPress={() => !undo.isPending && undo.mutate(r.id)}>
                  Undo</Text>
              )}
            </View>
          ))}
        </Card>
      )}

      {query.isPending && <Text style={s.mut}>Loading…</Text>}
      {/* a failed fetch is not an empty catalog — say so, and offer the
          retry, instead of claiming nothing matched */}
      {query.isError && (
        <Text style={[s.mut, { color: C.bad }]} onPress={() => query.refetch()}>
          Couldn't load merchants: {errText(query.error)} — tap to retry.
        </Text>
      )}
      {rows.length === 0 && !query.isPending && !query.isError && (
        <Text style={s.mut}>No merchants match that.</Text>
      )}

      {rows.length > 0 && (
        <Card style={{ paddingVertical: 2 }}>
          {rows.map((m) => <MerchantRow key={m.display} m={m} onOpen={openRow} />)}
        </Card>
      )}

      {rows.length >= CATALOG_LIMIT && !q && flt === "all" && (
        <Text style={s.mut}>
          Showing the {CATALOG_LIMIT} most frequent — search to find any merchant.
        </Text>
      )}
    </ScrollView>
  );
}

// one list row — memoized so the 200-row list only re-renders the rows
// whose props changed, not all of them on every keystroke in the search
const MerchantRow = memo(function MerchantRow({ m, onOpen }: {
  m: CatalogRow; onOpen: (display: string) => void;
}) {
  return (
    <Pressable style={s.row} onPress={() => onOpen(m.display)}>
      <MerchantAvatar name={m.display} logo={m.logo} size={28} />
      <View style={{ flex: 1 }}>
        <Text style={s.name} numberOfLines={1}>
          {m.display}
          {m.kind && m.kind !== "merchant" ? (
            <Text style={s.small}>  {catLabel(m.kind)}</Text>) : null}
          {m.parent ? <Text style={s.small}>  outlet of {m.parent}</Text> : null}
        </Text>
        <Text style={s.small} numberOfLines={1}>
          {m.rows} row{m.rows === 1 ? "" : "s"} · {money(m.total, false)} ·{" "}
          {mmddyy(m.first)} – {mmddyy(m.last)}
          {variantCount(m) > 1 ? ` · ${variantCount(m)} source names` : ""}
          {(m.business_rows ?? 0) > 0 ? ` · ${m.business_rows} business` : ""}
        </Text>
      </View>
      <Text style={s.small}>›</Text>
    </Pressable>
  );
});

/** The proposer's queue: pairs the ledger thinks are one business, one
 *  line each. The reasons and each side's facts sit behind "details". */
function NeedsALook({ merges, canEdit, busy, onDecide, onMergeAll, onOpen }: {
  merges: MergeProposal[]; canEdit: boolean; busy: boolean;
  onDecide: (pid: string, action: "approve" | "reject") => void;
  onMergeAll: () => void;
  onOpen: (display: string) => void;
}) {
  const [why, setWhy] = useState<string | null>(null);
  const n = merges.length;
  return (
    <Card>
      <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
        <Text style={[s.name, { flex: 1 }]}>
          Needs a look
          <Text style={s.small}>  {n} pair{n === 1 ? "" : "s"} look{n === 1 ? "s" : ""} like one merchant</Text>
        </Text>
        {canEdit && n > 1 && (
          <Text style={[s.action, busy && { opacity: 0.5 }]}
                onPress={() => !busy && onMergeAll()}>Merge all {n}</Text>
        )}
      </View>
      <Text style={s.small}>
        Merge joins them (undo under Recent changes); Not the same keeps
        them apart for good. Nothing is merged unless you say so.
      </Text>
      {merges.map((p) => (
        <View key={p.id} style={{ paddingVertical: 8, borderTopWidth: StyleSheet.hairlineWidth,
                                  borderTopColor: C.border, marginTop: 8 }}>
          <Text style={s.body}>
            <Text style={{ fontWeight: "600" }}>{p.from}</Text>
            <Text style={s.mut}> · {p.from_facts?.rows ?? p.from_rows ?? "?"}</Text>
            <Text style={s.mut}>  →  </Text>
            <Text style={{ fontWeight: "600" }}>{p.into}</Text>
            <Text style={s.mut}> · {p.into_facts?.rows ?? p.into_rows ?? "?"}</Text>
          </Text>
          <Text style={s.small}>
            {p.signals[0]}{"  "}
            <Text style={s.link} onPress={() => setWhy(why === p.id ? null : p.id)}>
              {why === p.id ? "less" : "details"}</Text>
          </Text>
          {why === p.id && (
            <View style={{ marginTop: 4 }}>
              {p.signals.slice(1).map((sg) => <Text key={sg} style={s.small}>• {sg}</Text>)}
              {p.supports.map((sp) => <Text key={sp} style={s.small}>• {sp}</Text>)}
              {/* the side being folded in is usually the newcomer, but a
                  long-lived spelling with fewer rows can lose too */}
              <OfferSide tag={(p.from_facts?.first ?? "") > (p.into_facts?.first ?? "")
                                ? "New detection" : "Other spelling"}
                         tone={C.warn} name={p.from}
                         facts={p.from_facts} samples={p.from_samples}
                         onOpen={() => onOpen(p.from)} />
              <Text style={s.op}>+</Text>
              <OfferSide tag="History" tone={C.accent} name={p.into_name ?? p.into}
                         facts={p.into_facts} samples={p.into_samples}
                         onOpen={() => onOpen(p.into_name ?? p.into)} />
              <Text style={s.op}>=</Text>
              <OfferMerged p={p} />
            </View>
          )}
          {canEdit && (
            <View style={{ flexDirection: "row", gap: 16, marginTop: 4 }}>
              <Text style={[s.action, busy && { opacity: 0.5 }]}
                    onPress={() => !busy && onDecide(p.id, "approve")}>Merge</Text>
              <Text style={[s.action, { color: C.mut }, busy && { opacity: 0.5 }]}
                    onPress={() => !busy && onDecide(p.id, "reject")}>Not the same</Text>
            </View>
          )}
        </View>
      ))}
    </Card>
  );
}

/** One side of an offer: what the ledger holds under that name today, so
 *  the two can be told apart before they are joined. The name opens the
 *  merchant's own screen for everything else. */
function OfferSide({ tag, tone, name, facts, samples, onOpen }: {
  tag: string; tone: string; name: string;
  facts?: MergeSideFacts | null; samples: string[]; onOpen: () => void;
}) {
  return (
    <View style={[s.box, { borderColor: tone }]}>
      <Text style={[s.tag, { color: tone }]}>{tag}</Text>
      <Text style={[s.link, { fontWeight: "600" }]} onPress={onOpen}>{name}</Text>
      {facts ? (
        <>
          <Text style={s.small}>
            {facts.rows} row{facts.rows === 1 ? "" : "s"} · {money(facts.total, false)} ·{" "}
            {facts.first === facts.last
              ? mmddyy(facts.first) : `${mmddyy(facts.first)} – ${mmddyy(facts.last)}`}
          </Text>
          <Text style={s.small}>
            {[facts.rows > 1 && facts.typical != null && `usually ${money(facts.typical, false)}`,
              facts.category && catLabel(facts.category), facts.city,
              facts.accounts.join(", ")].filter(Boolean).join(" · ")}
          </Text>
          {facts.recent.map((r, k) => (
            <View key={k} style={{ flexDirection: "row", gap: 6, marginTop: 3 }}>
              <Text style={s.small}>{mmddyy(r.date)}</Text>
              <Text style={[s.small, { flex: 1, fontFamily: "monospace" }]}>{r.line}</Text>
              <Text style={s.small}>{money(r.amount, true)}</Text>
            </View>
          ))}
        </>
      ) : (
        <Text style={[s.small, { fontFamily: "monospace" }]} numberOfLines={3}>
          {samples.join("  ·  ")}
        </Text>
      )}
    </View>
  );
}

/** What approving makes: one merchant under the surviving name, holding
 *  both sides' rows. The sums are the two boxes above it, added. */
function OfferMerged({ p }: { p: MergeProposal }) {
  const f = p.from_facts, i = p.into_facts;
  const dates = [f?.first, f?.last, i?.first, i?.last].filter(Boolean).sort() as string[];
  return (
    <View style={[s.box, { borderColor: C.good }]}>
      <Text style={[s.tag, { color: C.good }]}>After merge</Text>
      <Text style={[s.body, { fontWeight: "600" }]}>{p.into}</Text>
      {f && i ? (
        <>
          <Text style={s.small}>
            {f.rows + i.rows} rows <Text style={s.delta}>+{f.rows}</Text> ·{" "}
            {money(f.total + i.total, false)} <Text style={s.delta}>+{money(f.total, false)}</Text>
          </Text>
          <Text style={s.small}>
            {mmddyy(dates[0])} – {mmddyy(dates[dates.length - 1])}
            {i.category ? ` · ${catLabel(i.category)}` : ""}
          </Text>
        </>
      ) : null}
      <Text style={[s.small, { marginTop: 4 }]}>
        “{p.from}” stops being its own merchant: its rows, and any later
        charge under that name, file here.
      </Text>
      <Text style={[s.small, { marginTop: 3 }]}>Undo any time under Recent changes.</Text>
    </View>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  search: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
            borderRadius: 10, color: C.text, fontSize: 15,
            paddingHorizontal: 12, paddingVertical: 9 },
  mut: { color: C.mut, fontSize: 13 },
  small: { color: C.mut, fontSize: 12, marginTop: 2 },
  link: { color: C.accent, fontSize: 12 },
  body: { color: C.text, fontSize: 14 },
  name: { color: C.text, fontSize: 15, fontWeight: "600" },
  action: { color: C.accent, fontSize: 13, fontWeight: "600",
            paddingVertical: 6, paddingHorizontal: 4 },
  row: { flexDirection: "row", alignItems: "center", gap: 10, paddingVertical: 8,
         borderTopColor: C.border, borderTopWidth: StyleSheet.hairlineWidth },
  recent: { flexDirection: "row", alignItems: "center", gap: 10,
            paddingVertical: 6, borderTopColor: C.border, borderTopWidth: 0.5 },
  sortRow: { flexDirection: "row", gap: 4, backgroundColor: C.card,
             borderColor: C.border, borderWidth: 1, borderRadius: 10, padding: 3 },
  seg: { paddingHorizontal: 12, paddingVertical: 5, borderRadius: 7 },
  segOn: { backgroundColor: C.hover },
  chip: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
          borderRadius: 999, paddingHorizontal: 10, paddingVertical: 5 },
  chipOn: { borderColor: C.accent },
  box: { borderWidth: 1, borderRadius: C.rs, backgroundColor: C.bg,
         paddingHorizontal: 10, paddingVertical: 8, marginTop: 6 },
  tag: { fontSize: 10.5, fontWeight: "700", letterSpacing: 0.6, textTransform: "uppercase" },
  op: { color: C.mut, fontSize: 18, fontWeight: "600", textAlign: "center", marginTop: 4 },
  delta: { color: C.good, fontWeight: "600" },
});
