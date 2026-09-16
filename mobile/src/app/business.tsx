// Business — the web section's six tabs, native: Overview tiles,
// Transactions (Schedule-C review + the flagged-on-personal-cards
// ledger), Books (P&L, balance sheet, owner equity), Tax (set-aside,
// estimated taxes, 1099 contractors, compliance calendar), Receipts
// (mileage log + itemized expenses), and Settings (entity lifecycle,
// account assignment, tax reserve, members). CSV/zip/ics exports ride
// the bearer-authenticated download door into the OS share sheet.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { QueryClient, QueryKey } from "@tanstack/react-query";
import { useRouter } from "expo-router";
import { memo, useCallback, useEffect, useState } from "react";
import { Alert, Linking, Pressable, ScrollView, StyleSheet, Switch, Text,
         TextInput, View } from "react-native";
import PullRefresh from "../components/pull-refresh";

import StaleBanner from "../components/stale-banner";
import { Card, H, HelpLink, KV, Pill } from "../components/ui";
import { errText, Account, BizData, BizTxn, countsInTotals, Entity,
         inOperatingProfit, Obligation, setHandedOffTxn, Txn,
         Vendor } from "../lib/api";
import { patchList, patchQueries, patchQuery, removeFromList, bizKeys, personalKeys, invalidateAll } from "../lib/cache";
import { ymd } from "../lib/dates";
import { isValidYmd, catLabel } from "../lib/pure";
import { DownloadButton } from "../components/download-button";
import { useSession } from "../lib/session";
import { useOwner, useViewer } from "../lib/viewer";
import { C, mmddyy, money } from "../lib/theme";

const m$ = (n: number | null | undefined) =>
  n == null ? "—" : money(n, false);
// account kind is "type/subtype" — never compare with ===
const kindIs = (a: { kind: string }, ...types: string[]) =>
  types.some((t) => a.kind === t || a.kind.startsWith(t + "/"));
// P&L exclusions, mirrored client-side: transfers and card payments are
// money movement, not profit — without this an owner draw reads as a
// loss on a profitable month
const inPnl = (x: { category: string | null; amount: number;
                    cat_detailed: string | null;
                    cat_original?: string | null }) =>
  !(x.category || "").includes("TRANSFER")
  && (x.cat_detailed || "") !== "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"
  // money in whose ORIGINAL category was a transfer is a capital move,
  // not revenue, however its display category was later renamed — the
  // server's books._pnl_core applies the same was_transfer rule
  && !(x.amount < 0 && (x.cat_original || "").toUpperCase().includes("TRANSFER")
       && (x.category || "") !== "INCOME");
// the free-text date fields' shared complaint — the web uses a native
// picker, so only this client can produce "2026-02-30"
const DATE_ERR = "Enter a real date as YYYY-MM-DD.";

const TABS: [string, string][] = [
  ["overview", "Overview"], ["transactions", "Review"],
  ["books", "Books"], ["tax", "Tax"], ["log", "Receipts"],
  ["settings", "Settings"]];
const BUCKETS: [string, string][] = [
  ["operating", "Operating"],
  ["organizational", "Organizational (§248)"],
  ["startup_195", "Start-up (§195)"]];
const EQUITY_KINDS: [string, string][] = [
  ["contribution", "Capital contribution"], ["draw", "Owner's draw"],
  ["distribution", "Distribution"], ["reimbursement", "Reimbursement"]];

// every cached query that reads one entity's money — what a write that
// moves transactions into or out of the entity (assigning an account,
// importing the legacy flags, deleting the entity) can have changed.
// the summary row is the entity every tab renders from; lifecycle and
// settings writes return the updated Entity, so it lands in place
const putEntity = (qc: QueryClient, e: Entity) =>
  patchList<{ entities: Entity[] }, Entity>(
    qc, ["biz-summary"], "entities", (x) => x.id === e.id, e);

export default function Business() {
  const { client } = useSession();
  const viewer = useViewer();
  const qc = useQueryClient();
  const router = useRouter();
  const [tab, setTab] = useState("overview");
  const summary = useQuery({
    queryKey: ["biz-summary"],
    queryFn: () => client!.businessSummary(),
    enabled: !!client,
  });
  const entities = summary.data?.entities ?? [];
  const [picked, setPicked] = useState<string | null>(null);
  // an archived business is out of active use but never out of reach: it
  // keeps its books readable and restorable, so it lives in a collapsed
  // Archived list (the web's pattern), not in the working switcher
  const actives = entities.filter((e) => e.status === "active");
  const archivedList = entities.filter((e) => e.status !== "active");
  const entity = entities.find((e) => e.id === picked)
    ?? actives[0] ?? entities[0];
  const entityId = entity?.id;
  // archived entity or viewer role → everything read-only
  const ro = !entity || entity.status !== "active" || viewer;
  // the web's STRUCTURES labels, one vocabulary in both clients
  const structureLabel: Record<string, string> = {
    sole_prop: "Sole proprietorship", llc: "LLC",
    single_member_llc: "Single-member LLC",
    multi_member_llc: "Multi-member LLC",
    s_corp: "S-corp", partnership: "partnership" };

  type Summary = { entities: Entity[]; flagged_unassigned: number;
                   combined: boolean };
  // the Switch is bound to the cached summary, so it flips here and is
  // put back only if the server refuses; the combined view changes the
  // PERSONAL aggregates (business money joins or leaves the budget), not
  // the business books, so that is the side to refresh
  const combine = useMutation({
    mutationFn: (on: boolean) => client!.businessCombine(on),
    onMutate: async (on) => {
      await qc.cancelQueries({ queryKey: ["biz-summary"] });
      const prev = qc.getQueryData<Summary>(["biz-summary"]);
      patchQuery<Summary>(qc, ["biz-summary"], { combined: on });
      return { prev };
    },
    onSuccess: (r) =>
      patchQuery<Summary>(qc, ["biz-summary"], { combined: r.combined }),
    onError: (e, _on, ctx) => {
      if (ctx?.prev) qc.setQueryData(["biz-summary"], ctx.prev);
      Alert.alert("Couldn't change", errText(e));
    },
    onSettled: () => invalidateAll(qc, personalKeys),
  });
  const importFlags = useMutation({
    mutationFn: () => client!.entityImportFlags(entityId!),
    onSuccess: (r) => {
      Alert.alert("Imported",
        `${r.moved} transaction${r.moved === 1 ? "" : "s"} moved into `
        + `${entity!.name}.`);
      // the "N carry the old flag" line reads the summary — clear it now
      patchQuery<Summary>(qc, ["biz-summary"], { flagged_unassigned: 0 });
      // flagged rows sat in the personal budget until now (the personal
      // filter excludes entity rows, not the flag), so the verdict,
      // reports and lenses move with them
      invalidateAll(qc, [...bizKeys(entityId!), ["biz-summary"],
                         ["business"], ...personalKeys]);
    },
    onError: (e) => Alert.alert("Import failed", errText(e)),
  });
  const refreshSummary = () =>
    qc.invalidateQueries({ queryKey: ["biz-summary"] });
  // pull-to-refresh: the summary plus what the open tab shows — not the
  // whole cache, which refetched every other screen's queries too
  const refreshAll = () => {
    const keys: QueryKey[] = [["biz-summary"]];
    if (entityId) {
      keys.push(...bizKeys(entityId), ["compliance", entityId],
                ["equity", entityId], ["mileage", entityId],
                ["entity", entityId]);
    }
    if (tab === "transactions") keys.push(["business"]);
    if (tab === "log") keys.push(["receipt-report"]);
    if (tab === "overview" || tab === "tax" || tab === "settings")
      keys.push(["accounts"]);
    // returned so the pull spinner stays up until every key has refetched
    return invalidateAll(qc, keys);
  };

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={refreshAll} />}>
      <StaleBanner query={summary} />
      {summary.isPending && <Text style={s.center}>Loading…</Text>}
      {summary.isError && !summary.data && (
        <Card>
          <Text style={{ color: C.bad, fontSize: 13 }}>
            Couldn&apos;t load: {String(summary.error)}
          </Text>
        </Card>
      )}
      {summary.data && entities.length === 0 && !viewer && (
        <Card>
          <Text style={s.mut}>
            No businesses yet — set one up and its money stays out of
            your personal budget, cash flow and net worth.
          </Text>
          {/* guided setup owns the empty state, the same rule as the
              web: a flat form asks cold questions the wizard explains */}
          <Pressable style={[s.btn, { alignSelf: "flex-start",
                                      marginTop: 8 }]}
                     onPress={() => router.push(
                       "/business-wizard" as never)}>
            <Text style={s.btnText}>Set up a business</Text>
          </Pressable>
        </Card>
      )}
      {entity && (
        <>
          <ContinueSetupPrompt entity={entity} viewer={viewer} />
          <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6,
                         paddingHorizontal: 12, paddingTop: 10 }}>
            {TABS.map(([k, l]) => (
              <Pressable key={k} style={[s.chip, tab === k && s.chipOn]}
                         onPress={() => setTab(k)}>
                <Text style={{ color: tab === k ? C.text : C.mut,
                               fontSize: 12 }}>{l}</Text>
              </Pressable>
            ))}
          </View>
          <Card>
            <View style={{ flexDirection: "row", alignItems: "center",
                           gap: 6, flexWrap: "wrap" }}>
              <H>{entity.name}</H>
              {entity.status !== "active" && (
                <Pill text="archived · read-only" tone="warn" />
              )}
            </View>
            <Text style={s.mut}>
              {structureLabel[entity.structure] ?? entity.structure}
              {entity.state ? ` · ${entity.state}` : ""}
              {entity.ein_last4 ? ` · EIN •••••${entity.ein_last4}` : ""}
              {entity.business_start_date
                ? ` · operating since ${entity.business_start_date}` : ""}
            </Text>
            {actives.length > 1 && (
              <View style={{ flexDirection: "row", flexWrap: "wrap",
                             gap: 6, marginTop: 6 }}>
                {actives.map((e) => (
                  <Pressable key={e.id}
                             style={[s.chip, entityId === e.id && s.chipOn]}
                             onPress={() => setPicked(e.id)}>
                    <Text style={{ color: entityId === e.id
                                     ? C.text : C.mut, fontSize: 12 }}>
                      {e.name}
                    </Text>
                  </Pressable>
                ))}
              </View>
            )}
            <View style={[s.rowLine, { marginTop: 6 }]}>
              <Text style={{ color: C.mut, fontSize: 12, flex: 1 }}>
                Combined view — show business + personal together
              </Text>
              <Switch value={!!summary.data?.combined}
                      disabled={combine.isPending}
                      onValueChange={(v) => combine.mutate(v)}
                      trackColor={{ true: C.accent, false: C.hover }}
                      thumbColor={C.text} />
            </View>
            {(summary.data?.flagged_unassigned ?? 0) > 0 && !ro && (
              <Text style={{ color: C.warn, fontSize: 12, marginTop: 4 }}>
                {summary.data!.flagged_unassigned} transaction
                {summary.data!.flagged_unassigned === 1 ? "" : "s"} carry
                the old business flag.{" "}
                <Text style={{ color: C.accent }}
                      onPress={() => !importFlags.isPending
                        && importFlags.mutate()}>
                  Import them into this entity
                </Text>
              </Text>
            )}
          </Card>

          {tab === "overview" && <Overview entity={entity} />}
          {tab === "transactions" && (
            <>
              <ReviewQueue entityId={entity.id} ro={ro} />
              <FlaggedLedger />
            </>
          )}
          {tab === "books" && <Books entity={entity} ro={ro} />}
          {tab === "tax" && <Tax entity={entity} ro={ro}
                                 onChanged={refreshSummary} />}
          {tab === "log" && <ReceiptsTab entity={entity} ro={ro} />}
          {tab === "settings" && (
            <SettingsTab entity={entity} ro={ro}
                         onChanged={refreshSummary} onSelect={setPicked} />
          )}
        </>
      )}
      {archivedList.length > 0 && (
        <ArchivedBusinesses archived={archivedList} viewer={viewer}
                            onView={(id) => setPicked(id)} />
      )}
      <HelpLink topic="business" />
    </ScrollView>
  );
}

// an archived business stays reachable — a collapsed list (the web's
// Archived section) with view / restore / delete-forever; every record
// is kept throughout
function ArchivedBusinesses({ archived, viewer, onView }: {
  archived: Entity[]; viewer: boolean; onView: (id: string) => void;
}) {
  const { client } = useSession();
  const qc = useQueryClient();
  const owner = useOwner();
  const [open, setOpen] = useState(false);
  const [nuking, setNuking] = useState<string | null>(null);
  const restore = useMutation({
    mutationFn: (id: string) => client!.entityRestore(id),
    onSuccess: (r, id) => {
      // the reply is the restored Entity — it moves from this list into
      // the switcher at once, not after the summary comes back
      putEntity(qc, r);
      onView(id);
      qc.invalidateQueries({ queryKey: ["biz-summary"] });
    },
    onError: (e) => Alert.alert("Couldn't restore", errText(e)),
  });
  return (
    <Card>
      <Pressable onPress={() => setOpen((x) => !x)}>
        <Text style={{ color: C.mut, fontSize: 13 }}>
          {open ? "▾" : "▸"} Archived — {archived.length} business
          {archived.length > 1 ? "es" : ""} (records kept)
        </Text>
      </Pressable>
      {open && archived.map((e) => (
        <View key={e.id} style={{ marginTop: 8 }}>
          <Text style={{ color: C.text, fontSize: 13 }}>
            {e.name}{" "}
            <Text style={{ color: C.mut, fontSize: 12 }}>
              {e.archived_at
                ? `archived ${mmddyy(e.archived_at)}` : ""}
            </Text>
          </Text>
          <View style={{ flexDirection: "row", gap: 14, marginTop: 2 }}>
            <Text style={{ color: C.accent, fontSize: 12 }}
                  onPress={() => onView(e.id)}>view</Text>
            {!viewer && (
              <>
                <Text style={{ color: C.accent, fontSize: 12,
                               opacity: restore.isPending
                                 && restore.variables === e.id ? 0.5 : 1 }}
                      onPress={() => !restore.isPending
                        && restore.mutate(e.id)}>restore</Text>
                {/* delete forever is owner-only — it irreversibly destroys
                    the entity's tax records; the server gates it with
                    _owner_only, so a member never sees the control */}
                {owner && (
                  <Text style={{ color: C.bad, fontSize: 12 }}
                        onPress={() => setNuking(
                          nuking === e.id ? null : e.id)}>
                    delete forever…</Text>
                )}
              </>
            )}
          </View>
          {nuking === e.id && owner && (
            <DeleteForeverConfirm entity={e}
              onDone={() => {
                setNuking(null);
                qc.invalidateQueries({ queryKey: ["biz-summary"] });
              }}
              onCancel={() => setNuking(null)} />
          )}
        </View>
      ))}
    </Card>
  );
}

// permanent destruction — the made-this-by-mistake case only; archiving
// is the normal way to remove a business. The typed exact name mirrors
// the web's confirm, and the server enforces the same match, so this
// gate can't be skipped. The copy states exactly what dies (per-table
// counts from the server) and what merely returns to personal.
function DeleteForeverConfirm({ entity, onDone, onCancel }: {
  entity: Entity; onDone: () => void; onCancel: () => void;
}) {
  const { client } = useSession();
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const impact = useQuery({ queryKey: ["entity-impact", entity.id],
    queryFn: () => client!.entityDeleteImpact(entity.id),
    enabled: !!client });
  const nuke = useMutation({
    mutationFn: () => client!.entityDeleteForever(entity.id, name),
    onSuccess: (r) => {
      removeFromList<{ entities: Entity[] }, Entity>(
        qc, ["biz-summary"], "entities", (x) => x.id === entity.id);
      // the accounts come back to personal and the Business door keys
      // on me.has_business; detached transactions move personal money
      invalidateAll(qc, [["accounts"], ["me"],
        ...(r.detached?.accounts || r.detached?.transactions
          ? personalKeys : [])]);
      onDone();
    },
    onError: (e) => Alert.alert("Couldn't delete", errText(e)),
  });
  const d = impact.data;
  const cnt = (x: number, w: string) => `${x} ${w}${x === 1 ? "" : "s"}`;
  const destroyed = d ? [
    cnt(d.destroyed.equity_movements, "equity movement"),
    cnt(d.destroyed.mileage_trips, "mileage trip"),
    cnt(d.destroyed.compliance_obligations, "compliance obligation"),
    cnt(d.destroyed.vendors_1099, "1099 vendor"),
    cnt(d.destroyed.members, "member"),
  ].join(", ") : "…";
  return (
    <View style={{ marginTop: 6, padding: 10, borderRadius: 8,
                   borderWidth: 1,
                   borderColor: "rgba(224,105,93,.45)" }}>
      <Text style={{ color: C.bad, fontSize: 13, fontWeight: "600" }}>
        Delete {entity.name} forever
      </Text>
      <Text style={[s.mut, { marginTop: 4 }]}>
        This permanently destroys the entity and its records —{" "}
        {destroyed}.
        {d ? ` ${cnt(d.detached.accounts, "account")} and `
          + `${cnt(d.detached.transactions, "transaction")} return to `
          + "personal — no transaction is deleted." : ""} There is no
        undo. If you might ever need these books again, archive instead:
        archiving keeps everything.
      </Text>
      <Text style={[s.mut, { marginTop: 6 }]}>
        Type {entity.name} to confirm
      </Text>
      <TextInput style={[s.input, { marginTop: 4 }]}
                 placeholder={entity.name} placeholderTextColor={C.mut}
                 autoCapitalize="none" autoCorrect={false}
                 value={name} onChangeText={setName} />
      <View style={{ flexDirection: "row", gap: 8, marginTop: 8 }}>
        <Pressable style={[s.btn, { backgroundColor: C.bad,
                     opacity: name === entity.name ? 1 : 0.5 }]}
                   disabled={nuke.isPending || name !== entity.name}
                   onPress={() => nuke.mutate()}>
          <Text style={s.btnText}>
            {nuke.isPending ? "deleting…" : "Delete forever"}
          </Text>
        </Pressable>
        <Pressable style={[s.btn, s.btnQuiet]} onPress={onCancel}>
          <Text style={[s.btnText, { color: C.mut }]}>Cancel</Text>
        </Pressable>
      </View>
    </View>
  );
}

// ---- overview: four tiles ----
// guided setup is FINISHED when the entity has at least one assigned
// account (the web's rule) — until then the prompt offers the resume
// point, and it never shows once the wizard recorded completion
function ContinueSetupPrompt({ entity, viewer }: {
  entity: Entity; viewer: boolean;
}) {
  const { client } = useSession();
  const router = useRouter();
  const accounts = useQuery({ queryKey: ["accounts"],
    queryFn: () => client!.accounts(), enabled: !!client && !viewer });
  const settings = useQuery({ queryKey: ["settings"],
    queryFn: () => client!.settingsFull(), enabled: !!client && !viewer });
  if (viewer) return null;
  const marks = ((settings.data as
    { biz_wizard_steps?: Record<string, string> } | undefined)
    ?.biz_wizard_steps) || {};
  const assigned = (accounts.data?.accounts ?? [])
    .some((a) => a.entity_id === entity.id);
  if (assigned || marks.finish === "done"
      || !accounts.data || !settings.data) return null;
  return (
    <Text style={{ color: C.accent, fontSize: 13, marginHorizontal: 12,
                   marginTop: 10 }}
          onPress={() => router.push({ pathname: "/business-wizard",
            params: { resume: entity.id, name: entity.name,
                      started: entity.business_start_date ?? "" },
          } as never)}>
      Continue guided setup — assign accounts and scan for startup
      costs ›
    </Text>
  );
}

function Overview({ entity }: { entity: Entity }) {
  const { client } = useSession();
  const year = new Date().getFullYear();
  // no placeholder across an entity switch: the only thing that changes
  // these keys IS the entity, and the previous one's profit, tax and
  // deadline under the new name read as its figures — "—" is honest
  const pnl = useQuery({ queryKey: ["pnl", entity.id, year],
    queryFn: () => client!.entityPnl(entity.id, year), enabled: !!client });
  const txns = useQuery({ queryKey: ["biztxns", entity.id, year],
    queryFn: () => client!.bizTxns(entity.id, year), enabled: !!client });
  const accounts = useQuery({ queryKey: ["accounts"],
    queryFn: () => client!.accounts(), enabled: !!client });
  const compliance = useQuery({ queryKey: ["compliance", entity.id],
    queryFn: () => client!.entityCompliance(entity.id),
    enabled: !!client });
  const est = useQuery({ queryKey: ["esttax", entity.id, year],
    queryFn: () => client!.estTax(entity.id, year), enabled: !!client });
  const sugg = useQuery({ queryKey: ["biz-suggestions", entity.id],
    queryFn: () => client!.bizSuggestions(entity.id), enabled: !!client });
  // shadow exclusion up front — a doubly-linked account must not count
  // twice in the cash SUM or in the account COUNT, as on the web
  const mine = (accounts.data?.accounts ?? [])
    .filter((a) => a.entity_id === entity.id && countsInTotals(a));
  const cash = mine.filter((a) => kindIs(a, "depository"))
    .reduce((t, a) => t + (a.balance_current ?? 0), 0);
  const thisMonth = ymd(new Date()).slice(0, 7);
  // the month figure sits directly under "Profit this year", so it has
  // to be the same arithmetic: capitalized §248/§195 costs are outside
  // net_operating, and counting them here makes a filing fee read as a
  // loss the line above never shows
  const monthProfit = (txns.data?.transactions ?? [])
    .filter((x) => (x.date ?? "").startsWith(thisMonth) && inPnl(x)
      && inOperatingProfit(x, entity.business_start_date))
    .reduce((t, x) => t - x.amount, 0);
  const today = ymd(new Date());
  const next = (compliance.data?.obligations ?? [])
    .filter((o) => o.due >= today)
    .sort((a, b) => a.due.localeCompare(b.due))[0];
  return (
    <Card>
      <KV k="Profit this year" v={m$(pnl.data?.net_operating)} />
      <Text style={s.mut}>{m$(monthProfit)} this month</Text>
      <KV k="Cash in the business" v={m$(cash)} />
      <Text style={s.mut}>
        {mine.length} account{mine.length === 1 ? "" : "s"}
      </Text>
      <KV k="Next deadline"
          v={next ? mmddyy(next.due) : "none"} />
      <Text style={s.mut}>
        {m$(est.data?.quarterly)} est. quarterly
      </Text>
      {(() => {
        const n = sugg.isPending ? null
          : ((sugg.data as { unclassified?: number } | undefined)
              ?.unclassified ?? sugg.data?.suggestions.length ?? 0);
        return (
          <>
            <KV k="Needs attention" v={n === null ? "…" : String(n)}
                tone={n ? "warn" : "mut"} />
            <Text style={s.mut}>
              {n === 0 ? "nothing outstanding"
                : "unclassified transactions"}
            </Text>
          </>
        );
      })()}
    </Card>
  );
}

// ---- Schedule C review queue ----
function ReviewQueue({ entityId, ro }: {
  entityId: string; ro: boolean;
}) {
  const { client } = useSession();
  const qc = useQueryClient();
  const sugg = useQuery({ queryKey: ["biz-suggestions", entityId],
    queryFn: () => client!.bizSuggestions(entityId), enabled: !!client });
  const [pickFor, setPickFor] = useState<string | null>(null);
  type Sugg = NonNullable<typeof sugg.data> & { unclassified?: number };
  const classify = useMutation({
    mutationFn: ({ txnId, bucket, line }:
        { txnId: string; bucket: string; line?: string }) =>
      client!.bizClassify(entityId, txnId,
        { bucket, sched_c_line: line }),
    onSuccess: (_r, v) => {
      // the accepted row leaves the queue in this frame; the refetch
      // refills the 25-row window behind it
      qc.setQueryData<Sugg>(["biz-suggestions", entityId], (o) => o && {
        ...o,
        suggestions: o.suggestions.filter((x) => x.txn_id !== v.txnId),
        unclassified: Math.max(
          0, (o.unclassified ?? o.suggestions.length) - 1),
      });
      for (const k of [["biz-suggestions", entityId],
                       ["pnl", entityId], ["biztxns", entityId]])
        qc.invalidateQueries({ queryKey: k });
    },
    onError: (e) => Alert.alert("Couldn't classify", errText(e)),
  });
  // only the row in flight is held, not all 25 Accept buttons
  const busyRow = (id: string) =>
    classify.isPending && classify.variables?.txnId === id;
  const d = sugg.data;
  if (!d) return null;
  const total = (d as { unclassified?: number }).unclassified
    ?? d.suggestions.length;
  return (
    <Card>
      <H>
        Schedule C review{" "}
        {total > 0 && (
          <Text style={[s.mut, { fontWeight: "400" }]}>
            — {total} to go
          </Text>
        )}
      </H>
      {d.suggestions.length === 0 ? (
        <Text style={s.mut}>
          Nothing to review — every business transaction has a line.
        </Text>
      ) : (
        <Text style={s.mut}>
          Nothing is filed automatically. Accept a suggestion or pick a
          different line; a wrong line is a wrong number on your return.
        </Text>
      )}
      {d.suggestions.slice(0, 25).map((x) => (
        <View key={x.txn_id} style={s.row}>
          <View style={{ flex: 1 }}>
            <View style={{ flexDirection: "row", alignItems: "center",
                           gap: 6 }}>
              <Text style={{ color: C.text, fontSize: 13,
                             fontWeight: "600", flexShrink: 1 }}
                    numberOfLines={1}>{x.payee}</Text>
              {(x as { source?: string | null }).source === "merchant"
                ? <Pill text="learned" tone="good" />
                : (x as { source?: string | null }).source === "category"
                  ? <Pill text="guess" tone="warn" /> : null}
            </View>
            <Text style={s.mut}>
              {(x as { date?: string }).date
                ? `${mmddyy((x as { date: string }).date)} · ` : ""}
              {money(x.amount)}
              {x.suggested_line ? ` → ${x.suggested_line}` : ""}
            </Text>
            {pickFor === x.txn_id && (
              <View style={{ flexDirection: "row", flexWrap: "wrap",
                             gap: 4, marginTop: 4 }}>
                {d.lines.map((ln) => (
                  <Pressable key={ln}
                             style={[s.chip,
                               x.suggested_line === ln && s.chipOn]}
                             disabled={busyRow(x.txn_id)}
                             onPress={() => { setPickFor(null);
                               classify.mutate({ txnId: x.txn_id,
                                 bucket: x.suggested_bucket, line: ln });
                             }}>
                    <Text style={{ color: C.mut, fontSize: 11 }}>
                      {ln}
                    </Text>
                  </Pressable>
                ))}
              </View>
            )}
          </View>
          {!ro && (<>
            <Pressable style={[s.sBtn,
                         (busyRow(x.txn_id) || !x.suggested_line)
                           && { opacity: 0.5 }]}
                       disabled={busyRow(x.txn_id) || !x.suggested_line}
                       onPress={() => classify.mutate({
                         txnId: x.txn_id, bucket: x.suggested_bucket,
                         line: x.suggested_line ?? undefined })}>
              <Text style={{ color: C.text, fontSize: 12,
                             fontWeight: "600" }}>Accept</Text>
            </Pressable>
            <Text style={{ color: C.accent, fontSize: 12, padding: 4 }}
                  onPress={() => setPickFor(
                    pickFor === x.txn_id ? null : x.txn_id)}>
              line ▾
            </Text>
          </>)}
        </View>
      ))}
      {d.suggestions.length > 25 && (
        <Text style={s.mut}>
          Showing 25 of {total} — the list refills as you go.
        </Text>
      )}
    </Card>
  );
}

// ---- the flagged-on-personal-cards ledger ----
function FlaggedLedger() {
  const { client } = useSession();
  const viewer = useViewer();
  const qc = useQueryClient();
  const router = useRouter();
  const [year, setYear] = useState<number | undefined>(undefined);
  // render window over the fetched rows (the web renders all of them; a
  // phone list grows on demand) — items.tsx's show-more pattern
  const [rowLimit, setRowLimit] = useState(40);
  // …and the FETCH is capped too: the server ships 500 rows by default,
  // 1000 at most, while `count` spans the whole scope. Growing only the
  // render window would stop dead at 500 and send a tenant with more to
  // the CSV, so the fetch widens once, as the web's does.
  const [limit, setLimit] = useState(500);
  const q = useQuery({ queryKey: ["business", year ?? "all", limit],
    queryFn: () => client!.business(year, limit), enabled: !!client,
    // a year chip swaps the key; keep the card up instead of collapsing
    placeholderData: (prev) => prev });
  type Row = BizData["rows"][number];
  const unflag = useMutation({
    mutationFn: (t: Row) => client!.bizFlag(t.id, false),
    onSuccess: (_r, t) => {
      // the row leaves every cached year/limit view at once — the
      // total and count with it — instead of after the refetch
      patchQueries<BizData>(qc, ["business"], (o) => ({
        ...o,
        rows: o.rows.filter((r) => r.id !== t.id),
        count: Math.max(0, o.count - 1),
        total: o.total - t.amount,
      }));
      qc.invalidateQueries({ queryKey: ["business"] });
      qc.invalidateQueries({ queryKey: ["transactions"] });
    },
    onError: (e) => Alert.alert("Unflag failed", errText(e)),
  });
  const { mutate: unflagMutate } = unflag;
  const unflagRow = useCallback((t: Row) => unflagMutate(t),
                                [unflagMutate]);
  const openRow = useCallback((t: Row) => {
    setHandedOffTxn(t as Txn);
    router.push({ pathname: "/txn", params: { id: t.id } } as never);
  }, [router]);
  const d = q.data;
  return (
    <Card>
      <H>Flagged on personal cards</H>
      <Text style={s.mut}>
        the biz tag from Transactions — separate from the entity&apos;s
        own books; import them to merge
      </Text>
      {d && d.years.length > 0 && (
        <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6,
                       marginTop: 4 }}>
          <Pressable style={[s.chip, !year && s.chipOn]}
                     onPress={() => setYear(undefined)}>
            <Text style={{ color: !year ? C.text : C.mut,
                           fontSize: 11 }}>all years</Text>
          </Pressable>
          {d.years.map((y) => (
            <Pressable key={y} style={[s.chip, year === y && s.chipOn]}
                       onPress={() => setYear(y)}>
              <Text style={{ color: year === y ? C.text : C.mut,
                             fontSize: 11 }}>{y}</Text>
            </Pressable>
          ))}
        </View>
      )}
      {d && (
        <Text style={{ color: C.text, fontSize: 13, marginTop: 4 }}>
          <Text style={{ fontWeight: "700" }}>{m$(d.total)}</Text>
          {" across "}{d.count} transaction{d.count === 1 ? "" : "s"}
          {year ? ` in ${year}` : ""}
        </Text>
      )}
      {d && d.rows.length === 0 && (
        <Text style={s.mut}>
          Nothing flagged{year ? ` in ${year}` : ""} — hit the biz
          button on any transaction.
        </Text>
      )}
      {/* while another year's rows stand in they are dimmed and not
          tappable — a tap would open or unflag a row of the wrong year */}
      <View pointerEvents={q.isPlaceholderData ? "none" : "auto"}
            style={q.isPlaceholderData ? { opacity: 0.5 } : null}>
      {(d?.rows ?? []).slice(0, rowLimit).map((t) => (
        <FlaggedRow key={t.id} t={t} viewer={viewer}
                    pending={unflag.isPending
                             && unflag.variables?.id === t.id}
                    onOpen={openRow} onUnflag={unflagRow} />
      ))}
      </View>
      {d && (d.rows.length > rowLimit
             || (d.count > d.rows.length && limit < 1000)) && (
        <Text style={{ color: C.accent, fontSize: 13, marginTop: 6 }}
              onPress={() => {
                setRowLimit((n) => n + 100);
                // window caught up with what was fetched, and the scope
                // holds more — widen the fetch as well
                if (rowLimit + 100 >= d.rows.length
                    && d.count > d.rows.length && limit < 1000)
                  setLimit(1000);
              }}>
          show more ({Math.min(rowLimit, d.rows.length)} of {d.count})
        </Text>
      )}
      {/* the fetch is at the server's ceiling while `count` spans the
          whole scope — say so once the fetched rows are exhausted */}
      {d && limit >= 1000 && d.rows.length <= rowLimit
       && d.count > d.rows.length && (
        <Text style={s.mut}>
          Showing the newest {d.rows.length} of {d.count} — the CSV
          lists all.
        </Text>
      )}
      {d && d.count > 0 && (
        <DownloadButton
          path={`/api/business/export.csv${year ? `?year=${year}` : ""}`}
          filename="business.csv" label="Download CSV" />
      )}
    </Card>
  );
}

// one ledger row, memoized: the list is a ScrollView map (a FlatList
// cannot virtualize inside the page's own ScrollView), so a "show more"
// or an unflag in flight must not re-render the hundreds already shown
const FlaggedRow = memo(function FlaggedRow({ t, viewer, pending, onOpen,
                                              onUnflag }: {
  t: BizData["rows"][number]; viewer: boolean; pending: boolean;
  onOpen: (t: BizData["rows"][number]) => void;
  onUnflag: (t: BizData["rows"][number]) => void;
}) {
  return (
    <View style={s.row}>
      <Pressable style={{ flex: 1 }} onPress={() => onOpen(t)}>
        <Text style={{ color: C.text, fontSize: 13,
                       fontWeight: "600" }} numberOfLines={1}>
          {t.payee}
          {t.pending ? <Text style={s.mut}>  pend</Text> : null}
        </Text>
        <Text style={s.mut} numberOfLines={1}>
          {mmddyy(t.date)} · {catLabel(t.category)}
          {t.note ? `  ✎ ${t.note}` : ""}
        </Text>
      </Pressable>
      {/* Schedule-C framing: expenses positive, refunds negative */}
      <Text style={{ color: t.amount < 0 ? C.good : C.text,
                     fontSize: 13, fontVariant: ["tabular-nums"] }}>
        {money(t.amount)}
      </Text>
      {!viewer && (
        <Text style={{ color: C.mut, fontSize: 12, padding: 4,
                       opacity: pending ? 0.5 : 1 }}
              onPress={() => !pending && onUnflag(t)}>
          unflag
        </Text>
      )}
    </View>
  );
});

// ---- books: P&L + balance sheet + equity ----
function Books({ entity, ro }: { entity: Entity; ro: boolean }) {
  const { client } = useSession();
  const qc = useQueryClient();
  const now = new Date().getFullYear();
  const [year, setYear] = useState<number | undefined>(now);
  // a year chip swaps the key; the P&L line stays up meanwhile — but
  // only the SAME entity's: another entity's figures under this name
  // would read as its own
  const pnl = useQuery({ queryKey: ["pnl", entity.id, year ?? "all"],
    queryFn: () => client!.entityPnl(entity.id, year),
    enabled: !!client,
    placeholderData: (prev, q) =>
      q?.queryKey[1] === entity.id ? prev : undefined });
  const txns = useQuery({ queryKey: ["biztxns", entity.id, year ?? "all"],
    queryFn: () => client!.bizTxns(entity.id, year),
    enabled: !!client,
    placeholderData: (prev, q) =>
      q?.queryKey[1] === entity.id ? prev : undefined });
  const bs = useQuery({ queryKey: ["balancesheet", entity.id],
    queryFn: () => client!.balanceSheet(entity.id), enabled: !!client });
  const equity = useQuery({ queryKey: ["equity", entity.id],
    queryFn: () => client!.entityEquity(entity.id), enabled: !!client });
  const [bucketFor, setBucketFor] = useState<string | null>(null);
  type TxnList = { transactions: BizTxn[] };
  // the bucket label is a pure field on the row: flip it in every cached
  // year view up front, put it back if the server refuses, and refetch
  // only the figures that derive from it (P&L, estimated tax)
  const classify = useMutation({
    mutationFn: ({ txnId, bucket }: { txnId: string; bucket: string }) =>
      client!.bizClassify(entity.id, txnId, { bucket }),
    onMutate: async ({ txnId, bucket }) => {
      await qc.cancelQueries({ queryKey: ["biztxns", entity.id] });
      const prev = qc.getQueriesData<TxnList>(
        { queryKey: ["biztxns", entity.id] });
      patchQueries<TxnList>(qc, ["biztxns", entity.id], (o) => ({
        ...o,
        transactions: o.transactions.map((x) =>
          x.id === txnId ? { ...x, bucket } : x),
      }));
      return { prev };
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["pnl", entity.id] });
      qc.invalidateQueries({ queryKey: ["esttax", entity.id] });
    },
    onError: (e, _v, ctx) => {
      for (const [k, v] of ctx?.prev ?? []) qc.setQueryData(k, v);
      Alert.alert("Couldn't classify", errText(e));
    },
  });
  const [eqKind, setEqKind] = useState("contribution");
  const [eqAmt, setEqAmt] = useState("");
  const [eqDate, setEqDate] = useState(ymd(new Date()));
  const [eqForm, setEqForm] = useState("");
  const [eqErr, setEqErr] = useState<string | null>(null);
  const record = useMutation({
    mutationFn: () => client!.equityRecord(entity.id,
      { kind: eqKind, amount: Number(eqAmt.replace(/[$,]/g, "")),
        date: eqDate, form: eqForm || undefined }),
    onSuccess: () => {
      setEqAmt(""); setEqForm("");
      // the capital account is a line on the balance sheet as well
      qc.invalidateQueries({ queryKey: ["equity", entity.id] });
      qc.invalidateQueries({ queryKey: ["balancesheet", entity.id] });
    },
    onError: (e) => Alert.alert("Couldn't record", errText(e)),
  });
  type EquityView = NonNullable<typeof equity.data>;
  const eqDel = useMutation({
    mutationFn: (movementId: string) =>
      client!.equityDelete(entity.id, movementId),
    onSuccess: (_r, movementId) => {
      removeFromList<EquityView, EquityView["movements"][number]>(
        qc, ["equity", entity.id], "movements",
        (mv) => mv.id === movementId);
      qc.invalidateQueries({ queryKey: ["equity", entity.id] });
      qc.invalidateQueries({ queryKey: ["balancesheet", entity.id] });
    },
    onError: (e) => Alert.alert("Couldn't delete", errText(e)),
  });
  const p = pnl.data;
  const expenses = (txns.data?.transactions ?? [])
    .filter((x) => x.amount > 0);
  const cap = equity.data?.capital;
  return (
    <>
      <Card>
        <View style={{ flexDirection: "row", alignItems: "center",
                       gap: 6, flexWrap: "wrap" }}>
          <H>Books &amp; P&amp;L</H>
          <View style={{ flexDirection: "row", gap: 4,
                         marginLeft: "auto" }}>
            {[undefined, now, now - 1, now - 2].map((y) => (
              <Pressable key={y ?? "all"}
                         style={[s.chip, year === y && s.chipOn]}
                         onPress={() => setYear(y)}>
                <Text style={{ color: year === y ? C.text : C.mut,
                               fontSize: 11 }}>
                  {y ?? "all"}
                </Text>
              </Pressable>
            ))}
          </View>
        </View>
        {p && (
          <>
            <Text style={{ color: C.text, fontSize: 13, marginTop: 4 }}>
              Revenue <B>{m$(p.revenue)}</B> − Operating{" "}
              <B>{m$(p.operating_expenses)}</B> = Net{" "}
              <Text style={{ fontWeight: "700",
                             color: p.net_operating >= 0
                               ? C.good : C.bad }}>
                {m$(p.net_operating)}
              </Text>
            </Text>
            {p.organizational > 0 && (
              <Text style={s.mut}>
                Organizational (§248) {m$(p.organizational)} ·{" "}
                {m$(p.organizational_deduction.immediate)} deductible
                now.
              </Text>
            )}
            {p.startup_195 > 0 && (
              <Text style={s.mut}>
                Start-up (§195) {m$(p.startup_195)} ·{" "}
                {m$(p.startup_deduction.immediate)} now,{" "}
                {m$(p.startup_deduction.amortizable)} over 180 mo.
              </Text>
            )}
            <Text style={s.mut}>
              Estimates — confirm the tax treatment with your CPA.
            </Text>
            <DownloadButton
              path={`/api/business/entities/${entity.id}/pnl.csv${
                year ? `?year=${year}` : ""}`}
              filename="pnl.csv" label="Download P&L CSV" />
          </>
        )}
        {expenses.length > 0 && (
          <Text style={[s.mut, { marginTop: 6 }]}>
            Expenses only — classify each into a bucket. Revenue is
            summed into the figure above.
          </Text>
        )}
        {/* placeholder rows belong to another year: shown dimmed, not
            tappable, until the chosen year's land */}
        <View pointerEvents={txns.isPlaceholderData ? "none" : "auto"}
              style={txns.isPlaceholderData ? { opacity: 0.5 } : null}>
        {expenses.slice(0, 30).map((x) => (
          <View key={x.id} style={s.row}>
            <View style={{ flex: 1 }}>
              <Text style={{ color: C.text, fontSize: 13 }}
                    numberOfLines={1}>
                {x.payee}
                {x.note ? <Text style={s.mut}>  ✎ {x.note}</Text> : null}
              </Text>
              <Text style={s.mut}>
                {x.date ? mmddyy(x.date) : "·"} · {money(x.amount)}
              </Text>
              {bucketFor === x.id && !ro && (
                <View style={{ flexDirection: "row", flexWrap: "wrap",
                               gap: 4, marginTop: 4 }}>
                  {BUCKETS.map(([v, l]) => (
                    <Pressable key={v}
                               style={[s.chip,
                                 (x.bucket ?? "operating") === v
                                   && s.chipOn]}
                               disabled={classify.isPending
                                 && classify.variables?.txnId === x.id}
                               onPress={() => { setBucketFor(null);
                                 classify.mutate({ txnId: x.id,
                                                   bucket: v }); }}>
                      <Text style={{ color: C.mut, fontSize: 11 }}>
                        {l}
                      </Text>
                    </Pressable>
                  ))}
                </View>
              )}
            </View>
            <Text style={{ color: ro ? C.mut : C.accent, fontSize: 12 }}
                  onPress={ro ? undefined
                    : () => setBucketFor(
                        bucketFor === x.id ? null : x.id)}>
              {(BUCKETS.find(([v]) => v === (x.bucket ?? "operating"))
                ?.[1] ?? x.bucket)}{ro ? "" : " ▾"}
            </Text>
          </View>
        ))}
        </View>
        {expenses.length > 30 && (
          <Text style={s.mut}>
            showing 30 of {expenses.length} expense rows — the P&amp;L
            CSV carries them all.
          </Text>
        )}
      </Card>

      {bs.data && (
        <Card>
          <H>Balance sheet</H>
          {bs.data.assets.length === 0
            && bs.data.liabilities.length === 0 ? (
            <Text style={s.mut}>
              Assign a business bank or credit account and its balances
              show up here as assets and liabilities.
            </Text>
          ) : (
            <>
              <Text style={{ color: C.text, fontSize: 13 }}>
                Assets <B>{m$(bs.data.total_assets)}</B> − Liabilities{" "}
                <B>{m$(bs.data.total_liabilities)}</B> = Net{" "}
                <B>{m$(bs.data.net)}</B> · Capital account{" "}
                <B>{m$(bs.data.capital_account)}</B>
              </Text>
              <Text style={s.mut}>
                Snapshot from business account balances — not
                double-entry books.
              </Text>
            </>
          )}
        </Card>
      )}

      <Card>
        <H>Owner equity</H>
        {cap && (
          <Text style={{ color: C.text, fontSize: 13 }}>
            Capital account:{" "}
            <Text style={{ fontWeight: "700",
                           color: cap.capital_balance >= 0
                             ? C.good : C.bad }}>
              {m$(cap.capital_balance)}
            </Text>
            <Text style={s.mut}>
              {" "}(contributions {m$(cap.contributions)} + retained{" "}
              {m$(cap.net_income ?? 0)} − draws{" "}
              {m$(cap.draws + cap.distributions)})
              {cap.reimbursements > 0
                ? ` · reimbursements ${m$(cap.reimbursements)}` : ""}
            </Text>
          </Text>
        )}
        {(equity.data?.movements ?? []).map((mv) => (
          <View key={mv.id} style={s.row}>
            <Text style={[s.mut, { width: 60 }]}>
              {mv.date ? mmddyy(mv.date) : "·"}
            </Text>
            <Text style={{ color: C.text, fontSize: 13, flex: 1 }}>
              {EQUITY_KINDS.find(([v]) => v === mv.kind)?.[1] ?? mv.kind}
              {mv.form ? <Text style={s.mut}> · {mv.form}</Text> : null}
            </Text>
            <Text style={{ color: C.text, fontSize: 13,
                           fontVariant: ["tabular-nums"] }}>
              {money(mv.amount)}
            </Text>
            {!ro && (
              <Text style={{ color: C.mut, fontSize: 14, padding: 4,
                             opacity: eqDel.isPending
                               && eqDel.variables === mv.id ? 0.5 : 1 }}
                    onPress={() => !eqDel.isPending
                      && Alert.alert("Delete movement",
                      "Delete this equity movement? It changes the "
                      + "capital-account balance.",
                      [{ text: "Delete", style: "destructive",
                         onPress: () => eqDel.mutate(mv.id) },
                       { text: "Cancel", style: "cancel" }])}>
                ✕
              </Text>
            )}
          </View>
        ))}
        {!ro && (
          <View style={{ gap: 6, marginTop: 8 }}>
            <View style={{ flexDirection: "row", flexWrap: "wrap",
                           gap: 6 }}>
              {EQUITY_KINDS.map(([v, l]) => (
                <Pressable key={v} style={[s.chip, eqKind === v && s.chipOn]}
                           onPress={() => setEqKind(v)}>
                  <Text style={{ color: eqKind === v ? C.text : C.mut,
                                 fontSize: 11 }}>{l}</Text>
                </Pressable>
              ))}
            </View>
            <View style={{ flexDirection: "row", gap: 6 }}>
              <TextInput style={[s.input, { flex: 1 }]}
                         placeholder="amount" placeholderTextColor={C.mut}
                         keyboardType="decimal-pad" value={eqAmt}
                         onChangeText={setEqAmt} />
              <TextInput style={[s.input, { flex: 1 }]}
                         placeholder="YYYY-MM-DD"
                         placeholderTextColor={C.mut} value={eqDate}
                         autoCapitalize="none" onChangeText={setEqDate} />
            </View>
            <View style={{ flexDirection: "row", gap: 6 }}>
              <TextInput style={[s.input, { flex: 1 }]}
                         placeholder="form (Cash, ACH…)"
                         placeholderTextColor={C.mut} value={eqForm}
                         onChangeText={setEqForm} />
              <Pressable style={[s.btn,
                           (!eqAmt || record.isPending)
                             && { opacity: 0.5 }]}
                         disabled={!eqAmt || record.isPending}
                         onPress={() => {
                           if (!isValidYmd(eqDate)) {
                             setEqErr(DATE_ERR);
                             return;
                           }
                           setEqErr(null);
                           record.mutate();
                         }}>
                <Text style={s.btnText}>record</Text>
              </Pressable>
            </View>
            {eqErr && (
              <Text style={{ color: C.bad, fontSize: 12 }}>{eqErr}</Text>
            )}
          </View>
        )}
      </Card>
    </>
  );
}

function B({ children }: { children: React.ReactNode }) {
  return (
    <Text style={{ color: C.text, fontWeight: "700" }}>{children}</Text>
  );
}

// ---- tax: set-aside + estimated + 1099 + compliance ----
function Tax({ entity, ro, onChanged }: {
  entity: Entity; ro: boolean; onChanged: () => void;
}) {
  const { client } = useSession();
  const qc = useQueryClient();
  const year = new Date().getFullYear();
  const est = useQuery({ queryKey: ["esttax", entity.id, year],
    queryFn: () => client!.estTax(entity.id, year), enabled: !!client });
  const accounts = useQuery({ queryKey: ["accounts"],
    queryFn: () => client!.accounts(), enabled: !!client });
  const vendors = useQuery({ queryKey: ["vendors", entity.id],
    queryFn: () => client!.vendorList(entity.id), enabled: !!client });
  const compliance = useQuery({ queryKey: ["compliance", entity.id],
    queryFn: () => client!.entityCompliance(entity.id),
    enabled: !!client });
  const [rate, setRate] =
    useState(entity.income_tax_rate != null
      ? String(entity.income_tax_rate) : "");
  const [sqft, setSqft] =
    useState(entity.home_office_sqft != null
      ? String(entity.home_office_sqft) : "");
  const saveTax = useMutation({
    mutationFn: () => client!.entityUpdate(entity.id,
      { income_tax_rate: rate.trim() ? Number(rate) : null,
        home_office_sqft: sqft.trim() ? Number(sqft) : null }),
    onSuccess: (r) => {
      putEntity(qc, r);
      qc.invalidateQueries({ queryKey: ["esttax", entity.id] });
      onChanged();
    },
    onError: (e) => Alert.alert("Couldn't save", errText(e)),
  });
  type VendorList = { vendors: Vendor[] };
  // the Switch is bound to the cached list: flip it up front, put it
  // back if the server refuses; needs_1099 is derived, so still refetch
  const mark = useMutation({
    mutationFn: ({ merchant, reportable }:
        { merchant: string; reportable: boolean }) =>
      client!.vendorMark(entity.id, { merchant, reportable }),
    onMutate: async ({ merchant, reportable }) => {
      await qc.cancelQueries({ queryKey: ["vendors", entity.id] });
      const prev = qc.getQueryData<VendorList>(["vendors", entity.id]);
      patchList<VendorList, Vendor>(qc, ["vendors", entity.id], "vendors",
        (v) => v.merchant === merchant, { reportable });
      return { prev };
    },
    onError: (e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(["vendors", entity.id], ctx.prev);
      Alert.alert("Couldn't change", errText(e));
    },
    onSettled: () =>
      qc.invalidateQueries({ queryKey: ["vendors", entity.id] }),
  });
  const [oTitle, setOTitle] = useState("");
  const [oDue, setODue] = useState("");
  const [oRec, setORec] = useState("yearly");
  const [oErr, setOErr] = useState<string | null>(null);
  type ObligationList = { obligations: Obligation[] };
  const addObl = useMutation({
    mutationFn: () => client!.complianceAdd(entity.id,
      { title: oTitle.trim(), due_date: oDue, recurrence: oRec }),
    onSuccess: (r, _v) => {
      // the row shows up now; the refetch brings the server's fee/url
      const row: Obligation = { id: r.id, title: oTitle.trim(), due: oDue,
        fee: null, url: null, recurrence: oRec };
      qc.setQueryData<ObligationList>(["compliance", entity.id],
        (o) => o && { ...o, obligations: [...o.obligations, row] });
      setOTitle(""); setODue("");
      qc.invalidateQueries({ queryKey: ["compliance", entity.id] });
    },
    onError: (e) => Alert.alert("Couldn't add", errText(e)),
  });
  const delObl = useMutation({
    mutationFn: (oblId: string) => client!.complianceDelete(entity.id, oblId),
    onSuccess: (_r, oblId) => {
      removeFromList<ObligationList, Obligation>(qc,
        ["compliance", entity.id], "obligations", (o) => o.id === oblId);
      qc.invalidateQueries({ queryKey: ["compliance", entity.id] });
    },
    onError: (e) => Alert.alert("Couldn't remove", errText(e)),
  });
  const e0 = est.data;
  // set-aside: measure the nominated reserve account when it exists —
  // "is it ring-fenced" beats "is the money there somewhere"
  const mine = (accounts.data?.accounts ?? []).filter(
    (a) => a.entity_id === entity.id && kindIs(a, "depository")
      && countsInTotals(a));
  const reserve = mine.find(
    (a) => a.id === entity.tax_reserve_account_id);
  const ringFenced = !!reserve;
  const stale = !!entity.tax_reserve_account_id && !reserve;
  const cash = ringFenced ? (reserve!.balance_current ?? 0)
    : mine.reduce((t, a) => t + (a.balance_current ?? 0), 0);
  const gap = e0 ? cash - e0.total_estimated : 0;
  return (
    <>
      {e0 && (
        <Card>
          <H>Set aside for tax</H>
          <KV k={`Estimated for ${e0.year}`}
              v={m$(e0.total_estimated)} />
          {e0.net_profit > 0 && (
            <Text style={s.mut}>
              {Math.round((100 * e0.total_estimated)
                / e0.net_profit)}% of profit
            </Text>
          )}
          <KV k={ringFenced ? "In the tax account" : "Business cash"}
              v={m$(cash)} />
          {ringFenced && (
            <Text style={s.mut}>
              {reserve!.name}
              {reserve!.mask ? ` ····${reserve!.mask}` : ""}
            </Text>
          )}
          <KV k={gap >= 0 ? "Covered by" : "Short by"}
              v={m$(Math.abs(gap))}
              tone={gap >= 0 ? "good" : "warn"} />
          {stale && (
            <Text style={{ color: C.warn, fontSize: 12 }}>
              The account nominated as this business&apos;s tax reserve
              is no longer assigned to it, so this is showing all
              business cash instead. Pick one again under Settings →
              Tax reserve.
            </Text>
          )}
          <Text style={s.mut}>
            {ringFenced
              ? "“Saved” is the balance of the account you nominated "
                + "as this business's tax reserve, so this answers "
                + "“is it ring-fenced”."
              : "“Saved” here is all business cash, so this answers "
                + "“is the money there”, not “is it ring-fenced”. "
                + "Nominate a tax account under Settings."}
            {" The percentage is computed from your actual profit and "
             + "SE tax, not a flat rule of thumb. Not tax advice."}
          </Text>
        </Card>
      )}
      <Card>
        <H>Estimated taxes</H>
        {e0 && (
          <Text style={{ color: C.text, fontSize: 13, lineHeight: 19 }}>
            On <B>{m$(e0.net_profit)}</B> net
            {e0.mileage_deduction > 0
              ? <> − <B>{m$(e0.mileage_deduction)}</B> mileage</> : null}
            {e0.home_office_deduction > 0
              ? <> − <B>{m$(e0.home_office_deduction)}</B> home
                  office</> : null}
            {" = "}<B>{m$(e0.taxable_profit)}</B> taxable · SE tax{" "}
            <B>{m$(e0.se_tax)}</B>
            {e0.income_tax != null
              ? <> + income tax <B>{m$(e0.income_tax)}</B></> : null}
            {" → "}<B>{m$(e0.total_estimated)}</B>/yr,{" "}
            <B>{m$(e0.quarterly)}</B> per quarter.
          </Text>
        )}
        <Text style={s.mut}>
          Estimate — set aside quarterly; confirm with your CPA.
          {e0?.income_tax == null
            ? " Set an income-tax rate below to include income tax." : ""}
        </Text>
        {/* the year-end package moved to its own card below — this
            card is the estimate, that one is the deliverable */}
        {!ro && (
          <View style={{ flexDirection: "row", gap: 6, marginTop: 6 }}>
            <TextInput style={[s.input, { flex: 1 }]}
                       placeholder="income-tax rate %"
                       placeholderTextColor={C.mut}
                       keyboardType="decimal-pad" value={rate}
                       onChangeText={setRate} />
            <TextInput style={[s.input, { flex: 1 }]}
                       placeholder="home-office sq ft"
                       placeholderTextColor={C.mut}
                       keyboardType="number-pad" value={sqft}
                       onChangeText={setSqft} />
            <Pressable style={[s.btn, saveTax.isPending
                         && { opacity: 0.5 }]}
                       disabled={saveTax.isPending}
                       onPress={() => saveTax.mutate()}>
              <Text style={s.btnText}>save</Text>
            </Pressable>
          </View>
        )}
      </Card>
      <Card>
        <H>Year-end package</H>
        <Text style={s.mut}>
          P&amp;L, ledger with Schedule C lines, balance sheet, 1099
          vendor totals and the mileage log — one zip for your
          accountant.
        </Text>
        <DownloadButton
          path={`/api/business/entities/${entity.id}/package.zip?year=${
            year}`}
          filename={`year-end-${year}.zip`}
          label={`Download ${year} package`} />
      </Card>
      <Card>
        <H>1099 contractors</H>
        <Text style={s.mut}>
          Mark who&apos;s a contractor — those paid $600+ this year need
          a 1099-NEC.
          {(vendors.data?.vendors ?? []).length === 0
            ? " No business payees yet — assign an account or "
              + "transactions to this entity first." : ""}
        </Text>
        {(vendors.data?.vendors ?? []).slice(0, 12).map((v) => (
          <View key={v.merchant} style={s.row}>
            <Text style={{ color: C.text, fontSize: 13, flex: 1 }}
                  numberOfLines={1}>{v.merchant}</Text>
            <Text style={{ color: C.mut, fontSize: 12 }}>
              {m$(v.paid)}
            </Text>
            {v.needs_1099 && <Pill text="1099" tone="warn" />}
            <Switch value={v.reportable}
                    disabled={ro || (mark.isPending
                      && mark.variables?.merchant === v.merchant)}
                    onValueChange={(on) => mark.mutate(
                      { merchant: v.merchant, reportable: on })}
                    trackColor={{ true: C.accent, false: C.hover }}
                    thumbColor={C.text} />
          </View>
        ))}
        {(vendors.data?.vendors.length ?? 0) > 12 && (
          <Text style={s.mut}>
            showing the top 12 of {vendors.data!.vendors.length} payees
            by amount — the books CSV lists every vendor.
          </Text>
        )}
      </Card>
      <Card>
        <H>Compliance calendar</H>
        {(compliance.data?.obligations ?? []).length === 0 ? (
          <Text style={s.mut}>
            No obligations tracked — add a filing deadline below.
          </Text>
        ) : (
          <DownloadButton
            path={`/api/business/entities/${entity.id}/compliance.ics`}
            filename="compliance.ics" label="Add to calendar (.ics)" />
        )}
        {(compliance.data?.obligations ?? []).map((o, i) => (
          <View key={o.id ?? i} style={s.row}>
            <Text style={[s.mut, { width: 60 }]}>{mmddyy(o.due)}</Text>
            <Text style={{ color: C.text, fontSize: 13, flex: 1 }}>
              {/* the URL renders only under the same https? guard the
                  web applies — the server's write guard's client half */}
              {o.url && /^https?:\/\//.test(o.url) ? (
                <Text style={{ color: C.accent }}
                      onPress={() => Linking.openURL(o.url!)}>
                  {o.title} ↗
                </Text>
              ) : o.title}
              {o.fee === 0 ? <Text style={s.mut}> · free</Text>
                : o.fee ? <Text style={s.mut}> · {money(o.fee)}</Text>
                : null}
            </Text>
            {!ro && o.id && (
              <Text style={{ color: C.mut, fontSize: 14, padding: 4,
                             opacity: delObl.isPending
                               && delObl.variables === o.id ? 0.5 : 1 }}
                    onPress={() => !delObl.isPending
                      && Alert.alert("Remove obligation",
                      `Remove "${o.title}" from the compliance `
                      + "calendar?",
                      [{ text: "Remove", style: "destructive",
                         onPress: () => delObl.mutate(o.id!) },
                       { text: "Cancel", style: "cancel" }])}>
                ✕
              </Text>
            )}
          </View>
        ))}
        {!ro && (
          <View style={{ gap: 6, marginTop: 6 }}>
            <TextInput style={s.input}
                       placeholder="obligation (e.g. franchise tax)"
                       placeholderTextColor={C.mut} value={oTitle}
                       onChangeText={setOTitle} />
            <View style={{ flexDirection: "row", gap: 6 }}>
              <TextInput style={[s.input, { flex: 1 }]}
                         placeholder="due (YYYY-MM-DD)"
                         placeholderTextColor={C.mut} value={oDue}
                         autoCapitalize="none" onChangeText={setODue} />
              {(["yearly", "once"] as const).map((r) => (
                <Pressable key={r} style={[s.chip, oRec === r && s.chipOn,
                             { alignSelf: "center" }]}
                           onPress={() => setORec(r)}>
                  <Text style={{ color: oRec === r ? C.text : C.mut,
                                 fontSize: 11 }}>
                    {r === "yearly" ? "every year" : "one-time"}
                  </Text>
                </Pressable>
              ))}
              <Pressable style={[s.btn,
                           (!oTitle.trim() || !oDue || addObl.isPending)
                             && { opacity: 0.5 }]}
                         disabled={!oTitle.trim() || !oDue
                                   || addObl.isPending}
                         onPress={() => {
                           if (!isValidYmd(oDue)) {
                             setOErr(DATE_ERR);
                             return;
                           }
                           setOErr(null);
                           addObl.mutate();
                         }}>
                <Text style={s.btnText}>add</Text>
              </Pressable>
            </View>
            {oErr && (
              <Text style={{ color: C.bad, fontSize: 12 }}>{oErr}</Text>
            )}
          </View>
        )}
      </Card>
    </>
  );
}

// ---- receipts tab: mileage + itemized expenses ----

// IRS mileage rates are quoted in cents (72.5¢), and a year can carry two of
// them (2026 changed on Jul 1), so the summary names each rate that applied
// rather than a rounded blend that matches no trip.
const cents = (r: number) =>
  `${(r * 100).toFixed(1).replace(/\.0$/, "")}¢`;
function mileageRateLabel(s: { rate: number;
                               rates?: { rate: number; miles: number }[] }) {
  const rs = s.rates ?? [];
  if (rs.length > 1)
    return `(${rs.map((x) => `${x.miles} mi at ${cents(x.rate)}`).join(", ")})`;
  return `× ${cents(s.rate)}/mi`;
}

function ReceiptsTab({ entity, ro }: { entity: Entity; ro: boolean }) {
  const { client } = useSession();
  const qc = useQueryClient();
  const mileage = useQuery({ queryKey: ["mileage", entity.id],
    queryFn: () => client!.mileageList(entity.id), enabled: !!client });
  const [mDate, setMDate] = useState(ymd(new Date()));
  const [mMiles, setMMiles] = useState("");
  const [mPurpose, setMPurpose] = useState("");
  const [mErr, setMErr] = useState<string | null>(null);
  const addTrip = useMutation({
    mutationFn: () => client!.mileageAdd(entity.id,
      { date: mDate, miles: Number(mMiles),
        purpose: mPurpose || undefined }),
    onSuccess: () => {
      setMMiles(""); setMPurpose("");
      qc.invalidateQueries({ queryKey: ["mileage", entity.id] });
    },
    onError: (e) => Alert.alert("Couldn't log", errText(e)),
  });
  type MileageView = NonNullable<typeof mileage.data>;
  const delTrip = useMutation({
    mutationFn: (tripId: string) => client!.mileageDelete(entity.id, tripId),
    onSuccess: (_r, tripId) => {
      removeFromList<MileageView, MileageView["trips"][number]>(qc,
        ["mileage", entity.id], "trips", (x) => x.id === tripId);
      // the deduction summary is recomputed server-side
      qc.invalidateQueries({ queryKey: ["mileage", entity.id] });
    },
    onError: (e) => Alert.alert("Couldn't delete", errText(e)),
  });
  const now = new Date();
  const [tagText, setTagText] = useState("business");
  // the report is keyed on the tag; a fetch per keystroke janks the
  // card and spams the server, so the key follows the input on a delay
  const [tag, setTag] = useState("business");
  useEffect(() => {
    const id = setTimeout(() => setTag(tagText.trim()), 300);
    return () => clearTimeout(id);
  }, [tagText]);
  const [rm, setRm] = useState({ y: now.getFullYear(),
                                 m: now.getMonth() + 1 });
  const report = useQuery({
    queryKey: ["receipt-report", tag, rm.y, rm.m],
    queryFn: () => client!.receiptReport(tag, rm.y, rm.m),
    enabled: !!client,
    // keep the last rows up while the next MONTH of the same tag loads;
    // a different tag is a different report, so it starts from empty
    placeholderData: (prev, q) => q?.queryKey[1] === tag ? prev : undefined,
  });
  const shiftRm = (dir: -1 | 1) => setRm(({ y, m }) => {
    const next = m + dir;
    return next < 1 ? { y: y - 1, m: 12 }
      : next > 12 ? { y: y + 1, m: 1 } : { y, m: next };
  });
  const sum = mileage.data?.summary;
  return (
    <>
      <Card>
        <H>Mileage log</H>
        {sum && (
          <Text style={{ color: C.text, fontSize: 13 }}>
            {sum.miles} mi {mileageRateLabel(sum)} ={" "}
            <B>{m$(sum.deduction)}</B> deduction
            {sum.year ? ` (${sum.year})` : ""}.
          </Text>
        )}
        {(mileage.data?.trips ?? []).slice(0, 6).map((t) => (
          <View key={t.id} style={s.row}>
            <Text style={[s.mut, { width: 60 }]}>
              {t.date ? mmddyy(t.date) : "·"}
            </Text>
            <Text style={{ color: C.text, fontSize: 13, flex: 1 }}>
              {t.miles} mi{t.purpose ? ` · ${t.purpose}` : ""}
            </Text>
            {!ro && (
              <Text style={{ color: C.mut, fontSize: 14, padding: 4,
                             opacity: delTrip.isPending
                               && delTrip.variables === t.id ? 0.5 : 1 }}
                    onPress={() => !delTrip.isPending
                      && Alert.alert("Delete trip",
                      "Delete this mileage trip?",
                      [{ text: "Delete", style: "destructive",
                         onPress: () => delTrip.mutate(t.id) },
                       { text: "Cancel", style: "cancel" }])}>
                ✕
              </Text>
            )}
          </View>
        ))}
        {(mileage.data?.trips.length ?? 0) > 6 && (
          <Text style={s.mut}>
            showing the 6 most recent of {mileage.data!.trips.length}
            {" trips (the deduction total covers all of this year's)."}
          </Text>
        )}
        {!ro && (
          <View style={{ gap: 6, marginTop: 6 }}>
            <View style={{ flexDirection: "row", gap: 6 }}>
              <TextInput style={[s.input, { flex: 1 }]}
                         placeholder="YYYY-MM-DD"
                         placeholderTextColor={C.mut} value={mDate}
                         autoCapitalize="none" onChangeText={setMDate} />
              <TextInput style={[s.input, { width: 80 }]}
                         placeholder="miles" placeholderTextColor={C.mut}
                         keyboardType="decimal-pad" value={mMiles}
                         onChangeText={setMMiles} />
            </View>
            <View style={{ flexDirection: "row", gap: 6 }}>
              <TextInput style={[s.input, { flex: 1 }]}
                         placeholder="purpose"
                         placeholderTextColor={C.mut} value={mPurpose}
                         onChangeText={setMPurpose} />
              <Pressable style={[s.btn,
                           (!mMiles || addTrip.isPending)
                             && { opacity: 0.5 }]}
                         disabled={!mMiles || addTrip.isPending}
                         onPress={() => {
                           if (!isValidYmd(mDate)) {
                             setMErr(DATE_ERR);
                             return;
                           }
                           setMErr(null);
                           addTrip.mutate();
                         }}>
                <Text style={s.btnText}>log trip</Text>
              </Pressable>
            </View>
            {mErr && (
              <Text style={{ color: C.bad, fontSize: 12 }}>{mErr}</Text>
            )}
          </View>
        )}
      </Card>
      <Card>
        <H>Itemized expenses (receipt line items)</H>
        <Text style={s.mut}>
          Line items tagged on receipts — the itemized Schedule-C
          companion to the transaction-level list.
        </Text>
        <View style={{ flexDirection: "row", gap: 6, marginTop: 6,
                       alignItems: "center" }}>
          <TextInput style={[s.input, { flex: 1 }]}
                     placeholder="tag" placeholderTextColor={C.mut}
                     autoCapitalize="none"
                     value={tagText} onChangeText={setTagText} />
          <Text style={{ color: C.accent, fontSize: 18, padding: 4 }}
                onPress={() => shiftRm(-1)}>‹</Text>
          <Text style={{ color: C.text, fontSize: 13 }}>
            {rm.m}/{rm.y}
          </Text>
          <Text style={{ color: C.accent, fontSize: 18, padding: 4 }}
                onPress={() => shiftRm(1)}>›</Text>
        </View>
        {report.data && report.data.count === 0 && (
          <Text style={s.mut}>
            No “{tag}” line items in {rm.m}/{rm.y}.
          </Text>
        )}
        {(report.data?.rows ?? []).slice(0, 25).map((r, i) => (
          <View key={`${r.receipt_id}-${i}`}
                style={[s.row, report.isPlaceholderData && { opacity: 0.5 }]}>
            <Text style={[s.mut, { width: 60 }]}>{mmddyy(r.date)}</Text>
            <Text style={{ color: C.text, fontSize: 13, flex: 1 }}
                  numberOfLines={1}>
              {r.payee} · {r.description}
            </Text>
            <Text style={{ color: C.text, fontSize: 13,
                           fontVariant: ["tabular-nums"] }}>
              ${r.amount.toFixed(2)}
            </Text>
          </View>
        ))}
        {report.data && report.data.count > 0 && (
          <>
            <Text style={{ color: C.text, fontSize: 13, marginTop: 4 }}>
              <B>Total: ${report.data.total.toFixed(2)}</B>
              <Text style={s.mut}> across {report.data.count} items</Text>
            </Text>
            <DownloadButton
              path={`/api/receipts/report?tag=${encodeURIComponent(tag)
                }&y=${rm.y}&m=${rm.m}&format=csv`}
              filename="itemized-expenses.csv" label="Download CSV" />
          </>
        )}
      </Card>
    </>
  );
}

// ---- settings tab ----
function SettingsTab({ entity, ro, onChanged, onSelect }: {
  entity: Entity; ro: boolean; onChanged: () => void;
  onSelect: (id: string) => void;
}) {
  const { client } = useSession();
  const qc = useQueryClient();
  const viewer = useViewer();
  // deleting an entity forever is the ACCOUNT's decision — owner only,
  // as on the web (the server refuses a member; the button must too)
  const owner = useOwner();
  const [editing, setEditing] = useState(false);
  const [addingEntity, setAddingEntity] = useState(false);
  const [picking, setPicking] = useState(false);
  // delete-forever confirm open (typed-name gate lives in the confirm)
  const [nuke, setNuke] = useState(false);
  const accounts = useQuery({ queryKey: ["accounts"],
    queryFn: () => client!.accounts(), enabled: !!client });
  // the summary rows carry no members — only the entity detail does
  // (the web reads the same ["entity", id] for its Members list)
  const full = useQuery({ queryKey: ["entity", entity.id],
    queryFn: () => client!.entityGet(entity.id), enabled: !!client });
  type AccountList = { accounts: Account[] };
  // assigning moves every transaction in the account between the two
  // sides, so the entity's books AND the personal aggregates refresh;
  // the "✓ assigned" / mine list flips from the patched accounts first
  const assign = useMutation({
    mutationFn: ({ id, on }: { id: string; on: boolean }) =>
      client!.accountAssignEntity(id, on ? entity.id : null),
    onSuccess: (_r, v) => {
      patchList<AccountList, Account>(qc, ["accounts"], "accounts",
        (a) => a.id === v.id, { entity_id: v.on ? entity.id : null });
      invalidateAll(qc, [["accounts"], ["biz-summary"],
                         ...bizKeys(entity.id), ...personalKeys]);
    },
    onError: (e) => Alert.alert("Couldn't change", errText(e)),
  });
  const assignBusy = (id: string) =>
    assign.isPending && assign.variables?.id === id;
  const setReserve = useMutation({
    mutationFn: (id: string | null) =>
      client!.entityUpdate(entity.id, { tax_reserve_account_id: id }),
    onSuccess: (r) => { putEntity(qc, r); onChanged(); },
    onError: (e) => Alert.alert("Couldn't nominate", errText(e)),
  });
  // archive / restore return the updated Entity — the pill, the
  // read-only state and this button's label follow it at once
  const lifecycle = useMutation({
    mutationFn: (action: "archive" | "restore") => action === "archive"
      ? client!.entityArchive(entity.id) : client!.entityRestore(entity.id),
    onSuccess: (r) => { putEntity(qc, r); onChanged(); },
    onError: (e, action) => Alert.alert(
      action === "archive" ? "Couldn't archive" : "Couldn't restore",
      String(e)),
  });
  const [memberName, setMemberName] = useState("");
  const [memberPct, setMemberPct] = useState("");
  type Member = NonNullable<Entity["members"]>[number];
  const addMember = useMutation({
    mutationFn: () => client!.entityAddMember(entity.id,
      { member_name: memberName.trim(),
        ...(memberPct.trim()
          ? { ownership_pct: Number(memberPct) } : {}) }),
    onSuccess: (r) => {
      setMemberName(""); setMemberPct("");
      // the reply is the member row — it joins the list now
      const m0 = r as unknown as Member;
      if (m0 && typeof m0.id === "string")
        qc.setQueryData<Entity>(["entity", entity.id], (o) => o && {
          ...o, members: [...(o.members ?? []), m0] });
      qc.invalidateQueries({ queryKey: ["entity", entity.id] });
    },
    onError: (e) => Alert.alert("Couldn't add", errText(e)),
  });
  const mine = (accounts.data?.accounts ?? [])
    .filter((a) => a.entity_id === entity.id);
  const eligible = mine.filter((a) => kindIs(a, "depository"));
  const [pickQ, setPickQ] = useState("");
  const [showAll, setShowAll] = useState(false);
  const pickable = (accounts.data?.accounts ?? [])
    .filter((a) => showAll
      || (a.status !== "archived" && a.balance_current != null))
    .filter((a) => (a.name + " " + a.institution_name).toLowerCase()
      .includes(pickQ.trim().toLowerCase()))
    .sort((a, b) => (a.institution_name + a.name)
      .localeCompare(b.institution_name + b.name));
  const confirmAssign = (a: Account, on: boolean) => Alert.alert(
    on ? "Assign account" : "Unassign account",
    on
      ? `Assign ${a.name} to ${entity.name}? All its transactions `
        + "become business money and leave your personal budget."
      : `Unassign ${a.name}? Its transactions return to personal.`,
    [{ text: on ? "Assign" : "Unassign",
       onPress: () => assign.mutate({ id: a.id, on }) },
     { text: "Cancel", style: "cancel" }]);
  return (
    <>
      <Card>
        {!viewer && (
        <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
          {!ro && (
            <Pressable style={[s.btn, s.btnQuiet]}
                       onPress={() => setEditing((x) => !x)}>
              <Text style={[s.btnText, { color: C.accent }]}>
                {editing ? "Close" : "Edit details"}
              </Text>
            </Pressable>
          )}
          <Pressable style={[s.btn, s.btnQuiet,
                       lifecycle.isPending && { opacity: 0.5 }]}
                     disabled={lifecycle.isPending}
                     onPress={() => {
                       if (entity.status === "active") {
                         // reversible, but say what it means before it
                         // happens — the point is that nothing is lost
                         Alert.alert("Archive " + entity.name + "?",
                           "Every record is kept — books, equity, "
                           + "mileage, compliance stay readable. You "
                           + "can restore any time.",
                           [{ text: "Archive",
                              onPress: () => lifecycle.mutate("archive") },
                            { text: "Cancel", style: "cancel" }]);
                       } else {
                         lifecycle.mutate("restore");
                       }
                     }}>
            <Text style={[s.btnText, { color: C.warn }]}>
              {entity.status === "active" ? "Archive" : "Restore"}
            </Text>
          </Pressable>
          {owner && (
          <Pressable style={[s.btn, s.btnQuiet]}
                     onPress={() => setNuke((x) => !x)}>
            <Text style={[s.btnText, { color: C.bad }]}>
              Delete forever…
            </Text>
          </Pressable>
          )}
          <Pressable style={[s.btn, s.btnQuiet]}
                     onPress={() => setAddingEntity((x) => !x)}>
            <Text style={[s.btnText, { color: C.mut }]}>
              + Add business
            </Text>
          </Pressable>
        </View>
        )}
        {!viewer && entity.status === "active" && (
          <Text style={[s.mut, { marginTop: 4 }]}>
            Archive keeps every record — books, equity, mileage,
            compliance stay readable, and you can restore any time.
            Delete forever is only for a business created by mistake.
          </Text>
        )}
        {nuke && owner && (
          <DeleteForeverConfirm entity={entity}
            onDone={() => { setNuke(false); onChanged(); }}
            onCancel={() => setNuke(false)} />
        )}
        {editing && !ro && (
          <EntityForm entity={entity}
                      onDone={() => { setEditing(false); onChanged(); }} />
        )}
        {addingEntity && (
          <EntityForm onDone={(created) => { setAddingEntity(false);
                                             onChanged();
                                             if (created) onSelect(created.id);
                                           }} />
        )}
      </Card>

      <Card>
        <H>Accounts</H>
        <Text style={s.mut}>
          {mine.length > 0
            ? "Everything in these accounts is this business's money — "
              + "out of your personal budget."
            : "No accounts yet. Assign one and all its transactions "
              + "become this business's money."}
        </Text>
        {mine.map((a) => (
          <View key={a.id} style={s.row}>
            <Text style={{ color: C.text, fontSize: 13, flex: 1 }}
                  numberOfLines={1}>
              {a.name} <Text style={s.mut}>{a.kind}</Text>
            </Text>
            <Text style={{ color: C.mut, fontSize: 12 }}>
              {a.balance_current != null
                ? m$(a.balance_current) : "—"}
            </Text>
            {!ro && (
              <Text style={{ color: C.mut, fontSize: 12, padding: 4,
                             opacity: assignBusy(a.id) ? 0.5 : 1 }}
                    onPress={() => !assignBusy(a.id)
                      && confirmAssign(a, false)}>
                unassign
              </Text>
            )}
          </View>
        ))}
        {!ro && (
          <Pressable style={[s.btn, s.btnQuiet,
                       { alignSelf: "flex-start", marginTop: 6 }]}
                     onPress={() => setPicking((x) => !x)}>
            <Text style={[s.btnText, { color: C.accent }]}>
              {picking ? "Done" : "Assign an account…"}
            </Text>
          </Pressable>
        )}
        {picking && !ro && (
          <>
            <Text style={[s.mut, { marginTop: 6 }]}>
              Pick an account the business owns. All of its
              transactions — past and future — become business money
              and leave your personal budget.
            </Text>
            <TextInput style={[s.input, { marginTop: 6 }]}
                       placeholder="search accounts"
                       placeholderTextColor={C.mut} autoCapitalize="none"
                       value={pickQ} onChangeText={setPickQ} />
            <Text style={{ color: C.accent, fontSize: 12, marginTop: 4 }}
                  onPress={() => setShowAll((x) => !x)}>
              {showAll ? "hide" : "show"} closed/archived
            </Text>
            {pickable.map((a) => {
              const minea = a.entity_id === entity.id;
              const other = !!a.entity_id && !minea;
              return (
                <View key={a.id} style={s.row}>
                  <Text style={{ color: C.text, fontSize: 13, flex: 1 }}
                        numberOfLines={1}>
                    {a.institution_name} {a.name}
                    <Text style={s.mut}>
                      {" "}{a.kind}
                      {a.balance_current != null
                        ? ` · ${m$(a.balance_current)}` : ""}
                    </Text>
                  </Text>
                  {other ? (
                    <Text style={s.mut}>other business</Text>
                  ) : (
                    <Text style={{ color: minea ? C.good : C.accent,
                                   fontSize: 12, padding: 4,
                                   opacity: assignBusy(a.id) ? 0.5 : 1 }}
                          onPress={() => !assignBusy(a.id)
                            && confirmAssign(a, !minea)}>
                      {minea ? "✓ assigned" : "assign"}
                    </Text>
                  )}
                </View>
              );
            })}
          </>
        )}
        <Text style={s.mut}>
          For a one-off business cost on a personal card, use the
          business flag on any transaction.
        </Text>
      </Card>

      <Card>
        <H>Tax reserve</H>
        {eligible.length === 0 ? (
          <Text style={s.mut}>
            Assign a bank account to this business first — then you can
            nominate one of them as the account its tax money sits in.
          </Text>
        ) : (
          <>
            <Text style={s.mut}>
              Nominate the account you keep this business&apos;s tax
              money in. The set-aside card then measures THAT balance
              against what you owe, instead of all business cash.
            </Text>
            {/* the web's dangling-nomination state: a reserve that is
                no longer assigned here needs re-picking, and the picker
                must say so, not just the set-aside card */}
            {entity.tax_reserve_account_id
              && !eligible.some(
                (a) => a.id === entity.tax_reserve_account_id) && (
              <Text style={{ color: C.warn, fontSize: 12 }}>
                The nominated account is no longer assigned to this
                business — re-assign that account, or pick another
                below.
              </Text>
            )}
            <View style={{ flexDirection: "row", flexWrap: "wrap",
                           gap: 6, marginTop: 6 }}>
              <Pressable style={[s.chip,
                           !entity.tax_reserve_account_id && s.chipOn]}
                         disabled={ro || setReserve.isPending}
                         onPress={() => setReserve.mutate(null)}>
                <Text style={{ color: C.mut, fontSize: 11 }}>
                  none — measure all business cash
                </Text>
              </Pressable>
              {eligible.map((a) => (
                <Pressable key={a.id}
                           style={[s.chip,
                             entity.tax_reserve_account_id === a.id
                               && s.chipOn]}
                           disabled={ro || setReserve.isPending}
                           onPress={() => setReserve.mutate(a.id)}>
                  <Text style={{ color:
                    entity.tax_reserve_account_id === a.id
                      ? C.text : C.mut, fontSize: 11 }}>
                    {a.name}
                    {a.mask ? ` ····${a.mask}` : ""}
                    {a.balance_current != null
                      ? ` — ${m$(a.balance_current)}` : ""}
                  </Text>
                </Pressable>
              ))}
            </View>
          </>
        )}
      </Card>

      <Card>
        <H>Members</H>
        {(full.data?.members ?? []).map((m0) => (
          <Text key={m0.id} style={[s.mut, { paddingVertical: 2 }]}>
            {m0.member_name}
            {m0.ownership_pct != null ? ` — ${m0.ownership_pct}%` : ""}
            {m0.is_manager ? " · manager" : ""}
          </Text>
        ))}
        {!ro && (
          <View style={{ flexDirection: "row", gap: 6, marginTop: 6 }}>
            <TextInput style={[s.input, { flex: 1 }]}
                       placeholder="member name"
                       placeholderTextColor={C.mut} value={memberName}
                       onChangeText={setMemberName} />
            <TextInput style={[s.input, { width: 60 }]}
                       placeholder="%" placeholderTextColor={C.mut}
                       keyboardType="decimal-pad" value={memberPct}
                       onChangeText={setMemberPct} />
            <Pressable style={[s.btn,
                         (!memberName.trim() || addMember.isPending)
                           && { opacity: 0.5 }]}
                       disabled={!memberName.trim()
                                 || addMember.isPending}
                       onPress={() => addMember.mutate()}>
              <Text style={s.btnText}>add</Text>
            </Pressable>
          </View>
        )}
      </Card>
    </>
  );
}

// create / edit an entity — name, structure, dates, EIN (write-only:
// only ein_last4 comes back; blank keeps the stored one)
function EntityForm({ entity, onDone }: {
  entity?: Entity; onDone: (created?: Entity) => void;
}) {
  const { client } = useSession();
  const qc = useQueryClient();
  const [name, setName] = useState(entity?.name ?? "");
  const [structure, setStructure] =
    useState(entity?.structure ?? "single_member_llc");
  const [state, setState] = useState(entity?.state ?? "");
  const [ein, setEin] = useState("");
  const [started, setStarted] =
    useState(entity?.business_start_date ?? "");
  const [formed, setFormed] =
    useState(entity?.formation_date ?? "");
  const [err, setErr] = useState<string | null>(null);
  // the web's STRUCTURES, verbatim — order and labels
  const STRUCTURES: [string, string][] = [
    ["single_member_llc", "Single-member LLC"],
    ["multi_member_llc", "Multi-member LLC"],
    ["sole_prop", "Sole proprietorship"],
    ["s_corp", "S-corp"]];
  const save = useMutation({
    mutationFn: () => entity
      ? client!.entityUpdate(entity.id, {
          name: name.trim(), structure,
          state: state.trim() || null,
          formation_date: formed.trim() || null,
          business_start_date: started.trim() || null,
          ...(ein.trim() ? { ein: ein.trim() } : {}) })
      : client!.entityCreate({ name: name.trim(), structure,
          ...(state.trim() ? { state: state.trim() } : {}),
          ...(formed.trim() ? { formation_date: formed.trim() } : {}),
          ...(started.trim()
            ? { business_start_date: started.trim() } : {}),
          ...(ein.trim() ? { ein: ein.trim() } : {}) }),
    onSuccess: (r) => {
      // the reply is the Entity: an edit lands on the summary row it
      // renders from, a new business joins the switcher before the
      // summary comes back
      if (entity) putEntity(qc, r);
      else qc.setQueryData<{ entities: Entity[] }>(["biz-summary"],
        (o) => o && { ...o, entities: [...o.entities, r] });
      // the Business door elsewhere keys on me.has_business
      qc.invalidateQueries({ queryKey: ["me"] });
      onDone(entity ? undefined : r);
    },
    onError: (e) => setErr(errText(e)),
  });
  return (
    <View style={{ marginTop: 8, gap: 6 }}>
      {!entity && (
        <Text style={{ color: C.text, fontSize: 14, fontWeight: "700" }}>
          Set up a business
        </Text>
      )}
      <TextInput style={s.input} placeholder="business name"
                 placeholderTextColor={C.mut}
                 value={name} onChangeText={setName} />
      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
        {STRUCTURES.map(([v, l]) => (
          <Pressable key={v} style={[s.chip, structure === v && s.chipOn]}
                     onPress={() => setStructure(v)}>
            <Text style={{ color: structure === v ? C.text : C.mut,
                           fontSize: 12 }}>{l}</Text>
          </Pressable>
        ))}
      </View>
      <View style={{ flexDirection: "row", gap: 6 }}>
        <TextInput style={[s.input, { width: 70 }]}
                   placeholder="state" placeholderTextColor={C.mut}
                   maxLength={2} autoCapitalize="characters"
                   value={state} onChangeText={setState} />
        <TextInput style={[s.input, { flex: 1 }]}
                   placeholder="business start date (YYYY-MM-DD)"
                   placeholderTextColor={C.mut} autoCapitalize="none"
                   value={started} onChangeText={setStarted} />
      </View>
      <TextInput style={s.input}
                 placeholder="formation date (YYYY-MM-DD, optional)"
                 placeholderTextColor={C.mut} autoCapitalize="none"
                 value={formed} onChangeText={setFormed} />
      <TextInput style={s.input}
                 placeholder={entity?.ein_last4
                   ? `EIN •••••${entity.ein_last4} (unchanged)`
                   : "EIN (optional, stored encrypted)"}
                 placeholderTextColor={C.mut} keyboardType="number-pad"
                 value={ein} onChangeText={setEin} />
      <Text style={s.mut}>
        The EIN is encrypted at rest — only its last 4 digits are shown
        back.
      </Text>
      {err ? (
        <Text style={{ color: C.bad, fontSize: 12 }}
              onPress={() => setErr(null)}>{err}</Text>
      ) : null}
      <Pressable style={[s.btn, { alignSelf: "flex-start" },
                   (!name.trim() || save.isPending) && { opacity: 0.5 }]}
                 disabled={!name.trim() || save.isPending}
                 onPress={() => {
                   // both dates are optional, but a typed one must be a
                   // date that exists before it reaches the books
                   if (started.trim() && !isValidYmd(started.trim())) {
                     setErr(`Business start date: ${DATE_ERR}`);
                     return;
                   }
                   if (formed.trim() && !isValidYmd(formed.trim())) {
                     setErr(`Formation date: ${DATE_ERR}`);
                     return;
                   }
                   setErr(null);
                   save.mutate();
                 }}>
        <Text style={s.btnText}>
          {save.isPending ? "saving…" : entity ? "Save" : "Create"}
        </Text>
      </Pressable>
    </View>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  center: { color: C.mut, textAlign: "center", padding: 20 },
  mut: { color: C.mut, fontSize: 12, lineHeight: 17 },
  row: { flexDirection: "row", alignItems: "center", gap: 8,
         borderTopColor: C.border,
         borderTopWidth: StyleSheet.hairlineWidth,
         paddingVertical: 7 },
  rowLine: { flexDirection: "row", alignItems: "center", gap: 8 },
  chip: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
          borderRadius: 999, paddingHorizontal: 10, paddingVertical: 5 },
  chipOn: { borderColor: C.accent, backgroundColor: C.hover },
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 14, paddingVertical: 8 },
  btnQuiet: { backgroundColor: C.hover },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
  sBtn: { backgroundColor: C.accent, borderRadius: 8,
          paddingHorizontal: 12, paddingVertical: 6 },
  input: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
           borderRadius: 8, color: C.text, fontSize: 14,
           paddingHorizontal: 10, paddingVertical: 8 },
});
