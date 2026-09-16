// Accounts — the money's home: hero totals, connections (Plaid link +
// fix in the in-app browser, SimpleFIN paste, per-institution sync,
// disconnect), the per-account editor with the full remove ladder,
// linked sources, hidden vs archived, holdings, and the business pane.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ext } from "../../ext";
import { useRouter, useScrollToTop } from "expo-router";
import { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Image, Modal, Pressable, ScrollView, StyleSheet, Switch,
         Text, TextInput, View } from "react-native";
import PullRefresh from "../../components/pull-refresh";

import StaleBanner from "../../components/stale-banner";
import { Card, H, HelpLink, KV, Pill } from "../../components/ui";
import { Account, AccountRemoveMode, countsInTotals, linkCardVisible }
  from "../../lib/api";
import { patchList, patchQuery, removeFromList } from "../../lib/cache";
import { plaidHostedLink } from "../../lib/plaid";
import { ago } from "../../lib/dates";
import { accountRemoveMoved, hueOf, institutionAllowance, lastSyncPhrase,
         monogram } from "../../lib/pure";
import { useLogosOn } from "../../components/merchant-avatar";
import { useSession } from "../../lib/session";
import { useMe, useOwner, useViewer } from "../../lib/viewer";
import { C, money } from "../../lib/theme";
import { LogoSpinner } from "../../components/logo-spinner";

// the web add-form's kinds, verbatim
const KINDS: [string, string][] = [
  ["depository/checking", "checking"],
  ["depository/savings", "savings"],
  ["credit/credit card", "credit card"],
  ["investment/brokerage", "investment"],
  ["loan/", "loan"],
];

// the web's connection dot, verbatim semantics: green = synced/ok,
// amber = "not synced recently" (restored/stale — normal for import-fed
// accounts), red = a real connection error
function statusDot(a: Account): string {
  if (a.script) return a.script.ok ? C.good : C.bad;
  if (!a.status || a.status === "ok") return C.good;
  return a.status === "restored" || a.status === "stale" ? C.warn : C.bad;
}

function AccountRow({ a, onPress, primary, excluded, badge }:
    { a: Account; onPress?: () => void;
      primary?: boolean; excluded?: boolean; badge?: string }) {
  const bal = a.balance_current;
  const dot = statusDot(a);
  // linked-source state rides the row (the web's pills): the healthy
  // primary is serving, others are backups, a sick primary is down
  const linkFlag = a.link
    ? (a.link.primary
        ? (a.link.healthy ? "serving" : "source down") : "backup")
    : "";
  const flags = [
    primary ? "primary" : "",
    excluded ? "excl" : "",           // the web's pill names
    a.user_removed_at ? "removed" : "",
    linkFlag,
    // the web's red-dot tooltip: a collector whose script token was
    // revoked after its last good push (a password change or reset
    // revokes them all) is being refused, not broken — name it
    a.script && !a.script.ok && a.script.token_revoked_at
      ? "token revoked" : "",
  ].filter(Boolean).join(" · ");
  return (
    <Pressable onPress={onPress}
               style={({ pressed }) => [s.row,
                 pressed && onPress ? { backgroundColor: C.hover } : null]}>
      <View style={{ flex: 1 }}>
        <Text style={s.name} numberOfLines={1}>
          <Text style={{ color: dot }}>● </Text>
          {a.name}{a.mask ? <Text style={s.mut}> ··{a.mask}</Text> : null}
        </Text>
        <Text style={s.mut}>
          {a.kind}{a.owner ? ` · ${a.owner}` : ""} · {a.txns} txns
          {badge ? ` · ${badge}` : ""}
          {flags ? <Text style={{ color: C.warn }}>  {flags}</Text> : null}
        </Text>
      </View>
      <View style={{ alignItems: "flex-end" }}>
        <Text style={s.bal}>
          {bal === null ? "—" : money(bal, false)}
        </Text>
        {/* a card's next due date, right where the card is (web parity) */}
        {a.card_due && (bal ?? 0) > 0.5 ? (
          <Text style={s.mut}>
            due {a.card_due.slice(5, 7)}/{a.card_due.slice(8, 10)}
            {a.card_statement != null
              ? ` · ${money(a.card_statement, false)}` : ""}
          </Text>
        ) : null}
      </View>
    </Pressable>
  );
}

/** the bank's mark beside its name — Plaid's institution logo when the
 *  item has one, else a monogram in the brand colour (web InstMark);
 *  hidden when merchant logos are switched off */
function InstMark({ a, size = 20 }: { a?: Account; size?: number }) {
  const on = useLogosOn();
  const { client } = useSession();
  if (!on || !a) return null;
  const name = a.institution_name || "?";
  if (a.inst_logo_url && client) {
    // the logo route needs the session; the phone holds a bearer token,
    // not a cookie, so the image request carries it itself
    return <Image source={{ uri: client.baseUrl + a.inst_logo_url,
                            headers: client.authHeader }}
                  style={{ width: size, height: size, borderRadius: 4,
                           backgroundColor: "#fff" }} resizeMode="contain" />;
  }
  return (
    <View style={{ width: size, height: size, borderRadius: 4,
                   backgroundColor: a.inst_color || `hsl(${hueOf(name)}, 40%, 30%)`,
                   alignItems: "center", justifyContent: "center" }}>
      <Text style={{ color: "#fff", fontSize: Math.round(size * 0.45),
                     fontWeight: "700" }}>{monogram(name)}</Text>
    </View>
  );
}

export default function Accounts() {
  const { client } = useSession();
  // re-tapping the active tab scrolls back to the top
  const topRef = useRef<ScrollView>(null);
  useScrollToTop(topRef);
  const qc = useQueryClient();
  const viewer = useViewer();
  // Editing an account — name, kind, forecast anchor, exclusion — is
  // household bookkeeping and a member does it. The CONNECTION is the
  // owner's: linking a bank, repairing one, hiding or removing an
  // account. Those spend credentials or money and the server refuses
  // them, so the buttons are not drawn.
  const owner = useOwner();
  const router = useRouter();
  const [showArchived, setShowArchived] = useState(false);
  const [showHidden, setShowHidden] = useState(false);
  const [notice, setNoticeRaw] =
    useState<{ text: string; bad?: boolean } | null>(null);
  const setNotice = (text: string | null, bad = false) =>
    setNoticeRaw(text === null ? null : { text, bad });
  const query = useQuery({
    queryKey: ["accounts"],
    queryFn: () => client!.accounts(),
    enabled: !!client,
  });
  // Hosted runs ONE aggregator under the platform's credentials, so the
  // SimpleFIN door below is one /api refuses on hosted, and naming a
  // supplier the household cannot choose only invites the question.
  // Self-host keeps both: there it is the owner's call. The web Accounts
  // card applies the same gate.
  const me = useMe();
  const hosted = !!me.data?.hosted;
  // the web header's sync chip, brought to where the connections live
  const status = useQuery({
    queryKey: ["sync-status"],
    queryFn: () => client!.syncStatus(),
    enabled: !!client,
    refetchInterval: (q) =>
      q.state.data?.state === "running" ? 2500 : false,
  });
  const syncing = status.data?.state === "running";
  // how many of the plan's institutions are spent — both figures from the
  // server, never a constant here: the cap has a per-tenant override an
  // operator can raise
  const instCap = me.data?.features?.institution_cap;
  const instUsed = me.data?.features?.institutions_used;
  const { known: instCapKnown, full: instFull } =
    institutionAllowance(instCap, instUsed);
  // Plaid: add / fix / reconnect all funnel through the hosted link
  const [plaidBusy, setPlaidBusy] = useState(false);
  const conns = useQuery({
    queryKey: ["connections"],
    queryFn: () => client!.connections(),
    enabled: !!client,
    // Plaid Link finishes in the in-app browser — the new bank must
    // appear without a manual refresh — and a running sync moves the
    // progress line. There is no idle poll: nothing server-side moves
    // this query while nothing is busy, and the running→done edge below
    // already refreshes on completion. An idle timer would run all day on
    // every tab, each tick re-serializing the whole persisted cache
    // (accounts incl. base64 logos, every transactions page) into MMKV.
    refetchInterval: plaidBusy ? 3000 : syncing ? 5000 : false,
  });
  const holdings = useQuery({
    queryKey: ["holdings"],
    queryFn: () => client!.holdings(),
    enabled: !!client,
  });
  const links = useQuery({
    queryKey: ["account-links"],
    queryFn: () => client!.accountLinks(),
    enabled: !!client && !viewer,
  });
  const plaid = async (itemId = "") => {
    setPlaidBusy(true);
    setNotice("Finish signing in at your bank…");
    let changed = true;
    let added = false;
    try {
      const s0 = await plaidHostedLink(client!, itemId);
      if (s0.kind === "exited" || s0.kind === "cancelled") {
        // backing out is normal, never red — and nothing changed
        setNotice("No change — the sign-in window was closed.");
        changed = false;
      } else if (s0.kind === "add_failed" || s0.kind === "update_failed")
        setNotice(`Link failed: ${s0.error ?? "unknown"}`, true);
      else if (s0.kind === "update") setNotice("Reconnected.");
      else { setNotice(`Linked ${s0.institution || "bank"} ✓`); added = true; }
    } catch (e) { setNotice(String(e), true); }
    setPlaidBusy(false);
    if (!changed) return;
    qc.invalidateQueries({ queryKey: ["connections"] });
    // the institutions-used figure rides /api/me
    qc.invalidateQueries({ queryKey: ["me"] });
    qc.invalidateQueries({ queryKey: ["accounts"] });
    // a new bank changes the wizard's state and its backfill wait
    if (added) {
      qc.invalidateQueries({ queryKey: ["onboarding"] });
      qc.invalidateQueries({ queryKey: ["onboarding-backfill"] });
    }
  };
  const [sfOpen, setSfOpen] = useState(false);
  const simplefin = useMutation({
    mutationFn: (token: string) => client!.simplefinConnect(token),
    onSuccess: (r) => {
      setNotice(`Bank connected — ${r.accounts} accounts, ${
        r.transactions} transactions pulled.`);
      setSfOpen(false);
      qc.invalidateQueries({ queryKey: ["connections"] });
      // the institutions-used figure rides /api/me
      qc.invalidateQueries({ queryKey: ["me"] });
      qc.invalidateQueries({ queryKey: ["accounts"] });
    },
    onError: (e) => setNotice(String(e), true),
  });
  // in-flight per INSTITUTION — one mutation object's isPending is
  // global, which spun every row's ↻ and dropped a tap on bank B while
  // bank A was still syncing
  const [syncingItems, setSyncingItems] = useState<Set<string>>(new Set());
  const syncItem = useMutation({
    mutationFn: (itemId: string) => client!.accountSyncItem(itemId),
    onMutate: (itemId) => setSyncingItems((prev) =>
      new Set(prev).add(itemId)),
    onSettled: (_d, _e, itemId) => setSyncingItems((prev) => {
      const next = new Set(prev);
      next.delete(itemId);
      return next;
    }),
    onSuccess: (r) => {
      setNotice(r.message);
      qc.invalidateQueries({ queryKey: ["accounts"] });
      qc.invalidateQueries({ queryKey: ["connections"] });
      // the institutions-used figure rides /api/me
      qc.invalidateQueries({ queryKey: ["me"] });
    },
  });
  // `id` names the row the tap belongs to (a suggestion key or a group
  // id) so only that row shows pending; `drop` is a suggestion key that
  // leaves the list the moment the server agrees
  type LinksData = NonNullable<typeof links.data>;
  const linkAct = useMutation({
    mutationFn: ({ fn }: { id: string; fn: () => Promise<unknown>;
                           drop?: string; dismiss?: boolean;
                           card?: boolean }) => fn(),
    onSuccess: (_r, v) => {
      if (v.drop)
        removeFromList<LinksData, LinksData["suggestions"][number]>(
          qc, ["account-links"], "suggestions", (x) => x.key === v.drop);
      // putting the card away is a flag on the household, not a change
      // to any account
      if (v.card !== undefined) {
        const card = v.card;
        qc.setQueryData<LinksData>(["account-links"],
          (old) => old && { ...old, card_dismissed: card });
        return;
      }
      // "not the same" only hides a suggestion: no account changed
      if (v.dismiss) return;
      for (const k of ["account-links", "accounts", "today"])
        qc.invalidateQueries({ queryKey: [k] });
    },
    onError: (e) => {
      // overlapping suggestions go stale after one is linked — surface
      // the message AND refetch so dead pairs drop out
      setNotice(String(e).replace(/^\d+:\s*/, ""), true);
      qc.invalidateQueries({ queryKey: ["account-links"] });
    },
  });
  const [adding, setAdding] = useState(false);
  // the web's per-account editor, as a sheet: rename, owner, kind,
  // primary checking, exclude-from-budget, hide — and a balance field
  // ONLY for manual accounts (connected ones get theirs from the feed)
  // viewers see the primary/excluded pills read-only too
  const settings = useQuery({
    queryKey: ["settings-accounts"],
    queryFn: () => client!.settingsAccounts(),
    enabled: !!client,
  });
  const primary = settings.data?.checking_account_id ?? null;
  const excludedSet = new Set(settings.data?.excluded_accounts ?? []);
  const [editing, setEditing] = useState<Account | null>(null);
  const openEditor = (a: Account) => setEditing(a);
  // the editor's five writes are independent: fire them together, skip
  // the ones whose value did not change, and patch the cached account
  // from the form so the sheet closes onto the NEW name/kind/owner
  // instead of the old ones until two refetches land
  const save = useMutation({
    mutationFn: async (f: EditorForm) => {
      const a = editing!;
      const writes: Promise<unknown>[] = [];
      // an empty name — or retyping the bank's own — CLEARS the rename:
      // the server serves the bank's name again and follows its changes
      const trimmed = f.name.trim();
      const revert = !trimmed || trimmed === a.bank_name;
      if ((revert ? a.bank_name || a.name : trimmed) !== a.name)
        writes.push(client!.accountRename(a.id, revert ? "" : trimmed));
      if (f.kind !== a.kind || f.primary !== (a.id === primary))
        writes.push(client!.accountClassify(a.id, f.kind, f.primary));
      if (f.excl !== excludedSet.has(a.id))
        writes.push(client!.accountExclude(a.id, f.excl));
      if (f.owner.trim() !== (a.owner ?? ""))
        writes.push(client!.accountOwner(a.id, f.owner.trim()));
      if (isManual(a) && f.bal.trim() !== ""
          && Number(f.bal.replace(/[$,]/g, "")) !== a.balance_current)
        writes.push(client!.accountBalance(a.id, f.bal));
      await Promise.all(writes);
    },
    onSuccess: (_r, f) => {
      const a = editing!;
      setEditing(null);
      const balChanged = isManual(a) && f.bal.trim() !== ""
        && Number(f.bal.replace(/[$,]/g, "")) !== a.balance_current;
      patchList<{ accounts: Account[] }, Account>(qc, ["accounts"],
        "accounts", (x) => x.id === a.id, {
          name: !f.name.trim() || f.name.trim() === a.bank_name
            ? a.bank_name || a.name : f.name.trim(),
          kind: f.kind,
          owner: f.owner.trim() || null,
          ...(balChanged
              ? { balance_current: Number(f.bal.replace(/[$,]/g, "")) } : {}),
        });
      const wasPrimary = a.id === primary;
      const wasExcl = excludedSet.has(a.id);
      // ["settings-accounts"] and ["settings"] both read GET
      // /api/settings (api.ts:1803) — patch both so a primary/exclusion
      // change is visible to every settings reader, not just this screen
      const newSettingsAccts = {
        checking_account_id: f.primary ? a.id
          : wasPrimary ? null : primary,
        excluded_accounts: f.excl
          ? [...new Set([...excludedSet, a.id])]
          : [...excludedSet].filter((id) => id !== a.id),
      };
      patchQuery(qc, ["settings-accounts"], newSettingsAccts);
      patchQuery(qc, ["settings"], newSettingsAccts);
      qc.invalidateQueries({ queryKey: ["accounts"] });
      qc.invalidateQueries({ queryKey: ["settings-accounts"] });
      qc.invalidateQueries({ queryKey: ["settings"] });
      // only the forecast anchor, the budget exclusion, a balance and
      // the kind (income detection and the card-payment forecast key on
      // it) move the verdict; a rename or a new owner cannot
      if (f.primary !== wasPrimary || f.excl !== wasExcl || balChanged
          || f.kind !== a.kind)
        qc.invalidateQueries({ queryKey: ["today"] });
    },
  });
  const setHidden = useMutation({
    mutationFn: (hidden: boolean) =>
      client!.accountSetHidden(editing!.id, hidden),
    onSuccess: (r, hidden) => {
      // the row changes pile in the same frame the sheet closes; the
      // aggregates behind it (totals, ledger, verdict) really change
      patchList<{ accounts: Account[] }, Account>(qc, ["accounts"],
        "accounts", (x) => x.id === editing!.id,
        { user_removed_at: hidden ? new Date().toISOString() : null });
      setEditing(null);
      setNotice(r.note || "Done.");
      qc.invalidateQueries({ queryKey: ["accounts"] });
      qc.invalidateQueries({ queryKey: ["today"] });
      qc.invalidateQueries({ queryKey: ["transactions"] });
    },
  });
  const confirmHide = () => {
    const a = editing!;
    if (a.user_removed_at) { setHidden.mutate(false); return; }
    Alert.alert(`Hide ${a.name}?`,
      "It stops pulling new data and will not appear in or count toward "
      + `anything. The ${a.institution_name || "bank"} connection and the `
      + "existing history stay.",
      [{ text: "Cancel", style: "cancel" },
       { text: "Hide", style: "destructive",
         onPress: () => setHidden.mutate(true) }]);
  };
  // the remove ladder — bank connections are per institution, so
  // disconnect releases the whole login, never a single sub-account
  const [removing, setRemoving] = useState<Account | null>(null);
  const [rmMode, setRmMode] = useState<AccountRemoveMode>("disconnect");
  const remove = useMutation({
    mutationFn: () => client!.accountRemove(removing!.id, rmMode),
    onSuccess: (r) => {
      // the server names every account the mode touched: purged rows
      // leave the list now, a hidden one moves pile now; a plain
      // disconnect keeps its rows (only their status changes)
      const hit = new Set(r.affected_accounts ?? [removing!.id]);
      if (rmMode === "purge" || rmMode === "disconnect_purge")
        removeFromList<{ accounts: Account[] }, Account>(qc, ["accounts"],
          "accounts", (x) => hit.has(x.id));
      else if (rmMode === "hide")
        patchList<{ accounts: Account[] }, Account>(qc, ["accounts"],
          "accounts", (x) => hit.has(x.id),
          { user_removed_at: new Date().toISOString() });
      setRemoving(null); setEditing(null);
      setNotice((r.note || "Removed.")
        + (r.plaid_released ? " Plaid connection released." : ""));
      // One list, so the mode decides the keys in one place and the web
      // decides them the same way (pure.ts `accountRemoveMoved`). The
      // allowance on /api/me is exactly the thing a disconnect frees.
      for (const k of accountRemoveMoved(rmMode))
        qc.invalidateQueries({ queryKey: [k] });
    },
    onError: (e) => setNotice(String(e).replace(/^\d+:\s*/, ""), true),
  });
  const isPullAgg = (a: Account) =>
    ["plaid", "mx", "simplefin", "simplefin-org"]
      .includes(a.aggregator ?? "");
  const confirmRemove = () => {
    const a = removing!;
    const inst = a.institution_name || "this institution";
    const text = rmMode === "disconnect_purge"
      ? `Disconnect ${inst} and DELETE all its local data? This cannot `
        + "be undone."
      : rmMode === "purge"
        ? `Permanently delete ${a.name} and its transactions?`
        : rmMode === "disconnect"
          ? `Disconnect ${inst}? Syncing stops; history is kept.`
          : `Hide ${a.name} from active accounts?`;
    Alert.alert("Remove account", text,
      [{ text: "Confirm remove", style: "destructive",
         onPress: () => remove.mutate() },
       { text: "Cancel", style: "cancel" }]);
  };
  const add = useMutation({
    mutationFn: (f: { name: string; kind: string; bal: string }) =>
      client!.accountAdd(f.name.trim(), f.kind, f.bal),
    onSuccess: () => {
      setAdding(false);
      qc.invalidateQueries({ queryKey: ["accounts"] });
    },
  });
  // refresh ONLY on the running→done edge — "done" persists, and
  // invalidating during render loops the page's own query forever
  const prevState = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (prevState.current === "running" && status.data?.state === "done") {
      qc.invalidateQueries({ queryKey: ["accounts"] });
      qc.invalidateQueries({ queryKey: ["today"] });
      qc.invalidateQueries({ queryKey: ["transactions"] });
    }
    prevState.current = status.data?.state;
  }, [status.data?.state, qc]);
  const accounts = query.data?.accounts ?? [];
  // HIDDEN ≠ ARCHIVED (web doctrine): archived = the connection is
  // gone (no balance, closed lineage); hidden = the owner switched one
  // account off while the connection stays live. Both quiet, opposite
  // copy. Live = a balance-carrying, not-hidden feed.
  const isLive = (a: Account) =>
    a.balance_current !== null && !a.user_removed_at;
  // rebuilt on every poll response / notice / modal open otherwise —
  // harmless at 20 accounts, quadratic (groupBy's spread-per-push) at
  // one institution's worth of them
  const { hiddenList, archived, live, personal, business } = useMemo(() => {
    const liveList = accounts.filter(isLive);
    return {
      hiddenList: accounts.filter((a) => !!a.user_removed_at),
      archived: accounts.filter(
        (a) => a.balance_current === null && !a.user_removed_at),
      live: liveList,
      personal: liveList.filter((a) => !a.entity_id),
      business: liveList.filter((a) => !!a.entity_id),
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accounts]);
  const groupBy = (list: Account[]) => {
    const g = new Map<string, Account[]>();
    for (const a of list) {
      const key = a.institution_name || "Other";
      g.set(key, [...(g.get(key) ?? []), a]);
    }
    return [...g.entries()].sort((x, y) =>
      x[0].toLowerCase().localeCompare(y[0].toLowerCase()));
  };
  // the web's category tier: Bank / Credit Cards / Investments / Loans,
  // each with its own total, institutions nested inside
  const catOf = (kind: string): string => {
    const t = (kind || "").split("/")[0];
    return t === "credit" ? "Credit Cards"
      : t === "investment" ? "Investments"
      : t === "loan" ? "Loans" : "Bank";
  };
  const CATEGORY_ORDER = ["Bank", "Credit Cards", "Investments", "Loans"];
  const { groups, bizGroups, categories } = useMemo(() => {
    const groups0 = groupBy(personal);
    const bizGroups0 = groupBy(business);
    const categories0 = CATEGORY_ORDER.map((cat) => {
      const insts = groups0
        .map(([inst, list]) =>
          [inst, list.filter((a) => catOf(a.kind) === cat)] as const)
        .filter(([, list]) => list.length > 0);
      const total = insts.reduce((t, [, list]) =>
        t + list.filter(countsInTotals)
          .reduce((u, a) => u + (a.balance_current ?? 0), 0), 0);
      return { cat, insts, total };
    }).filter((c) => c.insts.length > 0);
    return { groups: groups0, bizGroups: bizGroups0, categories: categories0 };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [personal, business]);
  const connById = useMemo(() => new Map((conns.data?.connections ?? [])
    .map((c) => [c.id, c])), [conns.data]);
  // provenance: what feeds each institution's rows (the web's badge)
  const badgeLabel = (a: Account): string => {
    if (a.script) return "Script";
    const conn = a.item_id ? connById.get(a.item_id) : null;
    if (!conn) return "Import";
    return conn.aggregator === "plaid" ? "Plaid"
      : conn.aggregator === "mx" ? "MX"
      : conn.aggregator.startsWith("simplefin") ? "SimpleFIN"
      : conn.aggregator;
  };
  // hero math — personal live accounts only, and a doubly-linked
  // account counts ONCE: its backup sources are shadows of the same
  // money, excluded from the sum and from the account count the way
  // the server excludes them from every aggregate. The backup rows
  // still list below, badged.
  const counted = personal.filter(countsInTotals);
  const liveTotal = counted.reduce(
    (t, a) => t + (a.balance_current ?? 0), 0);
  const instCount = new Set(counted.map(
    (a) => a.institution_name || "Other")).size;
  // mirrors the web: a self-clearing bank outage is not "needs attention"
  const needsAttention = useMemo(() => (conns.data?.connections ?? [])
    .filter((c) => {
      const k = c.status_kind
        ?? (c.status && c.status !== "ok" ? "attention" : "ok");
      return k !== "ok" && k !== "retrying";
    }).length, [conns.data]);
  // one broken Plaid connection per item gets its fix affordance
  // only the ones a PERSON can act on: a bank that is down retries by
  // itself, and offering re-auth for it is a button that appears to work
  const brokenPlaid = (conns.data?.connections ?? []).filter(
    (c) => c.aggregator === "plaid"
      && ((c.status_kind ?? (c.status && c.status !== "ok" ? "attention"
                             : "ok")) === "reauth"
          || (c.status_kind ?? "ok") === "attention"));
  const acctName = (id: string) =>
    accounts.find((a) => a.id === id)?.name ?? id;
  // per-row pending for the linked-sources card: only the tapped
  // suggestion/group dims, and a second tap on it is ignored
  const linkBusy = (id: string) =>
    linkAct.isPending && linkAct.variables?.id === id;

  return (
    <ScrollView ref={topRef} style={s.wrap}
      contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => query.refetch()} />}>
      <StaleBanner query={query} />
      {/* There is no global "Sync now". A sync reads the aggregator's
          copy; it cannot make a bank send anything, and
          on a webhook-fed instance transactions land within seconds
          anyway — so such a button would nearly always be a no-op, which
          teaches a person the control is broken. Each connection keeps its
          own ↻ below, where the result can be explained against that
          bank's clock. A running sweep still shows itself here. */}
      {/* one row, two small doors: the bank sign-in (Plaid) and a manual
          account, with no introductory paragraph — the button says where
          it goes. */}
      {!viewer && (
        <View style={s.syncRow}>
          {owner && (
            // closes at the plan's cap, like the web's ConnectCard: the
            // server refuses past it anyway, but on the hosted link path
            // that refusal can land after the bank was authorized
            <Pressable style={[s.syncBtn, (plaidBusy || instFull)
                                          && { opacity: 0.5 }]}
                       disabled={plaidBusy || instFull}
                       onPress={() => plaid()}>
              <Text style={s.syncText}>
                {plaidBusy ? "Connecting…" : "＋ Connect account ↗"}
              </Text>
            </Pressable>
          )}
          <Pressable style={s.syncBtn} onPress={() => setAdding(true)}>
            <Text style={s.syncText}>＋ Add account</Text>
          </Pressable>
          {owner && !hosted && (
            <Text style={[s.syncText, { color: C.mut }]}
                  onPress={() => setSfOpen((x) => !x)}>SimpleFIN…</Text>
          )}
          {syncing && (
            <View style={{ flexDirection: "row", alignItems: "center",
                           gap: 6 }}>
              <LogoSpinner size={14} />
              <Text style={s.syncText}>Syncing…</Text>
            </View>
          )}
        </View>
      )}
      {/* both figures from the server — the cap has a per-tenant
          override an operator can raise */}
      {owner && instCapKnown && instFull && (
        <Text style={[s.mut, { marginHorizontal: 12, marginTop: 4 }]}>
          {ext.allowanceNote(instUsed, instCap, instFull)}
        </Text>
      )}
      {owner && sfOpen && !hosted && (
        <View style={{ marginHorizontal: 12, marginTop: 6 }}>
          <SimplefinPaste pending={simplefin.isPending}
                          onConnect={(t) => simplefin.mutate(t)} />
        </View>
      )}
      {/* the web sync page's progress: which phase, how far, and when
          it last finished */}
      {syncing && conns.data?.sync_progress ? (() => {
        const pgs = conns.data!.sync_progress!;
        const frac = pgs.total > 0 ? pgs.done / pgs.total : 0;
        return (
          <View style={{ marginHorizontal: 12, marginTop: 6, gap: 3 }}>
            <View style={{ backgroundColor: C.border, borderRadius: 4,
                           height: 6, overflow: "hidden" }}>
              <View style={{ width: `${Math.min(100, frac * 100)}%`,
                             backgroundColor: C.accent, height: 6 }} />
            </View>
            <Text style={{ color: C.mut, fontSize: 11 }}>
              {pgs.phase ? `${pgs.phase} · ` : ""}
              {pgs.done} of {pgs.total}
            </Text>
          </View>
        );
      })() : !syncing && lastSyncPhrase(conns.data?.last_sync) ? (
        <Text style={{ color: C.mut, fontSize: 11, marginHorizontal: 12,
                       marginTop: 4 }}>
          {lastSyncPhrase(conns.data?.last_sync)}
        </Text>
      ) : null}
      {query.isPending && <Text style={s.center}>Loading…</Text>}
      {notice ? (
        <Text style={{ color: notice.bad ? C.bad : C.good, fontSize: 12,
                       marginHorizontal: 12, marginTop: 4 }}
              onPress={() => setNotice(null)}>
          {notice.text}
        </Text>
      ) : null}
      {live.length > 0 && (
        <Card>
          <Text style={{ color: C.text, fontSize: 24, fontWeight: "700",
                         fontVariant: ["tabular-nums"] }}>
            {money(liveTotal, false)}
          </Text>
          <Text style={s.mut}>
            across {counted.length} account
            {counted.length === 1 ? "" : "s"} · {instCount} institution
            {instCount === 1 ? "" : "s"}
            {lastSyncPhrase(conns.data?.last_sync)
              ? ` · ${lastSyncPhrase(conns.data?.last_sync)}` : ""}
          </Text>
          {needsAttention > 0 && (
            <Text style={{ color: C.warn, fontSize: 12 }}>
              ⚠ {needsAttention} connection
              {needsAttention === 1 ? "" : "s"} need
              {needsAttention === 1 ? "s" : ""} attention
            </Text>
          )}
        </Card>
      )}

      {owner && brokenPlaid.length > 0 && (
        <View style={{ marginHorizontal: 12 }}>
          {brokenPlaid.map((c) => (
            <View key={c.id} style={{ flexDirection: "row",
                     alignItems: "center", gap: 8, marginTop: 8 }}>
              <Text style={{ color: C.bad, fontSize: 12, flex: 1 }}>
                {c.institution_name || c.id}:{" "}
                {(c.status ?? "").toUpperCase().includes("LOGIN_REQUIRED")
                  ? "sign-in expired — re-authenticate at the bank"
                  : c.status === "reaped"
                    ? "connection removed after 30 days of failure"
                    : "sync failing"}
              </Text>
              <Pressable style={[s.btn, plaidBusy && { opacity: 0.5 }]}
                         disabled={plaidBusy}
                         onPress={() =>
                           // a reaped Item's token is gone — reconnect
                           // must be a FRESH link, not update mode
                           plaid(c.status === "reaped" ? "" : c.id)}>
                <Text style={s.btnText}>
                  {c.status === "reaped" ? "reconnect ↗" : "fix ↗"}
                </Text>
              </Pressable>
            </View>
          ))}
        </View>
      )}

      {/* status the owner may put away — reorder/unlink live in the
          account editor, and the card returns on its own when a pair
          needs deciding or a source goes down */}
      {!viewer && links.data && linkCardVisible(links.data) && (
        <Card>
          <View style={{ flexDirection: "row", alignItems: "baseline" }}>
            <View style={{ flex: 1 }}><H>Linked sources</H></View>
            {!links.data.card_dismissed && links.data.groups.length > 0 && (
              <Text style={[{ color: C.mut, fontSize: 12, padding: 4 },
                            linkBusy("card") && { opacity: 0.5 }]}
                    onPress={() => !linkBusy("card")
                      && linkAct.mutate({ id: "card", card: true, fn: () =>
                      client!.accountLinkCard(true) })}>
                dismiss
              </Text>
            )}
          </View>
          <Text style={s.mut}>
            Two sources feeding the same real-world account get linked:
            the best live source serves the data, the others stay in
            sync as backups — never double-counted, and they take over
            automatically (with an email) if the primary breaks. Reorder
            or unlink from the account's editor.
          </Text>
          {(links.data?.suggestions ?? []).map((sg) => (
            <View key={sg.key} style={s.linkRow}>
              <Text style={{ color: C.text, fontSize: 12, flex: 1 }}>
                These look like the same account:{" "}
                <Text style={{ fontWeight: "700" }}>
                  {sg.a.institution_name ? `${sg.a.institution_name} — `
                                         : ""}{sg.a.name}
                  {sg.a.mask ? ` (${sg.a.mask})` : ""}
                </Text>
                {" and "}
                <Text style={{ fontWeight: "700" }}>
                  {sg.b.institution_name ? `${sg.b.institution_name} — `
                                         : ""}{sg.b.name}
                  {sg.b.mask ? ` (${sg.b.mask})` : ""}
                </Text>
              </Text>
              <Pressable style={[s.btn,
                           linkBusy(sg.key) && { opacity: 0.5 }]}
                         disabled={linkBusy(sg.key)}
                         onPress={() => linkAct.mutate({ id: sg.key,
                           drop: sg.key, fn: () =>
                           client!.accountLinkCreate([sg.a.id, sg.b.id]) })}>
                <Text style={s.btnText}>Link them</Text>
              </Pressable>
              <Pressable style={[s.btn, s.btnQuiet,
                           linkBusy(sg.key) && { opacity: 0.5 }]}
                         disabled={linkBusy(sg.key)}
                         onPress={() => linkAct.mutate({ id: sg.key,
                           drop: sg.key, dismiss: true, fn: () =>
                           client!.accountLinkDismiss(sg.key) })}>
                <Text style={[s.btnText, { color: C.mut }]}>
                  Not the same
                </Text>
              </Pressable>
            </View>
          ))}
          {/* put away, only the groups that need attention stay */}
          {links.data.groups.filter((g) => !links.data!.card_dismissed
              || g.members.some((m0) => !m0.healthy)).map((g) => (
            <View key={g.group_id} style={s.linkRow}>
              <Text style={{ color: C.text, fontSize: 12, flex: 1 }}>
                {g.members.map((m0, i) => (
                  <Text key={m0.account_id}>
                    {i > 0 ? <Text style={s.mut}> → </Text> : null}
                    {acctName(m0.account_id)}
                    <Text style={s.mut}>
                      {" "}({m0.primary ? "serving" : "backup"})
                      {!m0.healthy ? " ⚠" : ""}
                    </Text>
                  </Text>
                ))}
              </Text>
            </View>
          ))}
        </Card>
      )}

      {categories.map(({ cat, insts, total }) => (
        <View key={cat}>
          {/* the web's category tier — a header row with its own total */}
          <View style={{ flexDirection: "row", alignItems: "baseline",
                         marginHorizontal: 12, marginTop: 10 }}>
            <Text style={{ color: C.mut, fontSize: 12, fontWeight: "700",
                           textTransform: "uppercase", flex: 1 }}>
              {cat}
            </Text>
            <Text style={{ color: C.text, fontSize: 14, fontWeight: "700",
                           fontVariant: ["tabular-nums"] }}>
              {money(total, false)}
            </Text>
          </View>
          {insts.map(([inst, list]) => {
            const itemId = list[0]?.item_id ?? null;
            const conn = itemId ? connById.get(itemId) : null;
            // real statuses look like "error:ITEM_LOGIN_REQUIRED" —
            // match the class, not an exact string
            const st0 = conn?.status ?? "ok";
            const kind = conn?.status_kind
              ?? (st0 !== "ok" ? "attention" : "ok");
            const reaped = kind === "gone" || st0 === "reaped";
            // 'broken' means ACTIONABLE — a bank outage is amber and
            // wordless, because update mode cannot fix an institution
            // that is down and a fix button for it teaches people to
            // ignore the next real one.
            const broken = !!conn && !reaped
              && (kind === "reauth" || kind === "attention");
            const retrying = !!conn && kind === "retrying"
              && st0 !== "restored" && st0 !== "stale";
            const newAccounts = st0 === "new_accounts";
            const loginExpired =
              st0.toUpperCase().includes("LOGIN_REQUIRED");
            return (
            <Card key={`${cat}-${inst}`}>
              <View style={{ flexDirection: "row", alignItems: "center",
                             gap: 8, flexWrap: "wrap" }}>
                <InstMark a={list[0]} />
                <H>{inst}</H>
                {broken && (
                  <Pill text={newAccounts ? "new accounts to share"
                                : loginExpired ? "sign-in expired"
                                : "needs attention"}
                        tone={newAccounts ? "warn" : "bad"} />
                )}
                {retrying && (
                  <Pill text="bank having trouble — retrying" tone="warn" />
                )}
                {/* the BANK's own login rail is degraded fleet-wide
                    (Plaid institution health, cached hourly) — context
                    for a reconnect that dies on the bank's error page.
                    It shows on a BROKEN row too, which is the one whose
                    "fix ↗" is about to hit that page: it follows the red
                    pill and does not replace it, so the ask stays the
                    same and only the odds change. A 'retrying' row
                    already carries the outage sentence. DEGRADED only
                    earns the pill on a broken row — it is Plaid's
                    chronic fleet-wide new-login metric (weeks at a time
                    on major banks), a standing false alarm on a healthy,
                    syncing row; DOWN warrants it everywhere. */}
                {!retrying && !reaped
                  && !!conn?.institution_health
                  && conn.institution_health !== "HEALTHY"
                  && (conn.institution_health === "DOWN" || broken) && (
                  <Pill text={broken
                          ? "bank-side trouble — the fix may not go "
                            + "through yet"
                          : "bank-side trouble — may clear on its own"}
                        tone="warn" />
                )}
                {/* a reaped Item is GONE upstream — say so where the
                    accounts live, not only in the connect card */}
                {reaped && (
                  <Pill text="connection removed after 30 days of failure"
                        tone="warn" />
                )}
                {/* a billed product this connection cannot answer for —
                    the reason a card has no autopay line on the cash
                    forecast (mirrors the web pill): 'consent' is fixable
                    by an update-mode re-link. not_supported stays SILENT
                    (the web too): it is permanent and unactionable, so a
                    standing pill for it is only noise. */}
                {!reaped && conn?.product_issues &&
                  Object.entries(conn.product_issues)
                    .filter(([, iss]) => iss.cause === "consent")
                    .map(([prod]) => {
                    const what = prod === "liabilities"
                      ? "card due dates" : prod === "investments"
                      ? "investment holdings" : prod;
                    const consent = true;
                    return (
                      <View key={prod} style={{ flexDirection: "row",
                                    alignItems: "center", gap: 6 }}>
                        <Pill text={`${what} not shared`} tone="warn" />
                        {/* the pill said "reconnect to grant" but on a
                            healthy item neither the reaped nor broken
                            button renders — mirror the web's grant ↗:
                            an update-mode re-link lets the bank share it */}
                        {consent && owner && !broken && (
                          <Pressable
                            style={[s.btn, { paddingVertical: 5 },
                                    plaidBusy && { opacity: 0.5 }]}
                            disabled={plaidBusy}
                            onPress={() => plaid(itemId!)}>
                            <Text style={s.btnText}>grant ↗</Text>
                          </Pressable>
                        )}
                      </View>
                    );
                  })}
                {/* WHEN THE BANK LAST SENT DATA — a different clock from
                    the sync chip, which only says when we last read
                    Plaid's copy. Without it a sync that correctly finds
                    nothing looks broken. */}
                {!broken && !reaped && conn?.bank_updated_at && (
                  <Text style={{ color: C.mut, fontSize: 11 }}>
                    bank sent data {ago(conn.bank_updated_at)}</Text>
                )}
                <View style={{ flex: 1 }} />
                {owner && reaped && (
                  <Pressable style={[s.btn, { paddingVertical: 5 },
                               plaidBusy && { opacity: 0.5 }]}
                             disabled={plaidBusy}
                             onPress={() => plaid()}>
                    <Text style={s.btnText}>reconnect ↗</Text>
                  </Pressable>
                )}
                {owner && broken && conn!.aggregator === "plaid" && (
                  <Pressable style={[s.btn, { paddingVertical: 5 },
                               plaidBusy && { opacity: 0.5 }]}
                             disabled={plaidBusy}
                             onPress={() => plaid(itemId!)}>
                    <Text style={s.btnText}>fix ↗</Text>
                  </Pressable>
                )}
                {!viewer && conn && !reaped && (
                  <Text style={{ color: C.accent, fontSize: 14,
                                 padding: 4 }}
                        onPress={() => !syncingItems.has(itemId!)
                          && syncItem.mutate(itemId!)}>
                    {syncingItems.has(itemId!) ? "…" : "↻"}
                  </Text>
                )}
              </View>
              {list.map((a) => (
                <AccountRow key={a.id} a={a} badge={badgeLabel(a)}
                  primary={a.id === primary}
                  excluded={excludedSet.has(a.id)}
                  onPress={viewer
                    ? () => router.push({ pathname: "/transactions",
                        params: { acct: a.id } } as never)
                    : () => openEditor(a)} />))}
            </Card>
            );
          })}
        </View>
      ))}

      {bizGroups.length > 0 && (
        <Card>
          <H>Business accounts</H>
          <Text style={s.mut}>
            Excluded from personal totals; managed on{" "}
            <Text style={{ color: C.accent }}
                  onPress={() => router.push("/business" as never)}>
              Business ›
            </Text>
          </Text>
          {bizGroups.map(([inst, list]) => (
            <View key={inst}>
              <View style={{ flexDirection: "row", alignItems: "center", gap: 6,
                             marginTop: 6 }}>
                <InstMark a={list[0]} size={16} />
                <Text style={[s.mut, { fontWeight: "600" }]}>{inst}</Text>
              </View>
              {list.map((a) => (
                <AccountRow key={a.id} a={a}
                  onPress={viewer ? undefined : () => openEditor(a)} />))}
            </View>
          ))}
        </Card>
      )}

      {(holdings.data?.holdings ?? []).length > 0 && (
        <Card>
          <H>Top holdings</H>
          {holdings.data!.holdings.slice(0, 20).map((h, i) => (
            <View key={`${h.acct}-${h.symbol}-${i}`}>
              <KV k={`${h.symbol}${h.fund
                      ? ` · ${h.fund.slice(0, 30)}` : ""}`}
                  v={money(h.value, false)} tone="mut" />
              {/* the web table's Account / Qty / Price columns */}
              <Text style={[s.mut, { marginTop: -2 }]}>
                {h.acct}
                {h.qty != null ? ` · ${h.qty.toFixed(2)}` : ""}
                {h.price != null ? ` @ ${money(h.price)}` : ""}
              </Text>
            </View>
          ))}
          {holdings.data!.holdings.length > 20 && (
            <Text style={s.mut}>
              showing the top 20 of {holdings.data!.holdings.length}
            </Text>
          )}
        </Card>
      )}

      {/* the out-of-play piles close the page — live money first,
          hidden/archived last */}
      {hiddenList.length > 0 && (
        <Card>
          <Pressable onPress={() => setShowHidden((x) => !x)}>
            <Text style={s.archHead}>
              {showHidden ? "▾" : "▸"} Hidden ({hiddenList.length}) —
              excluded from every total; the connection is still live and the history is kept
            </Text>
          </Pressable>
          {showHidden && hiddenList.map((a) => (
            <AccountRow key={a.id} a={a}
              onPress={viewer ? undefined : () => openEditor(a)} />
          ))}
        </Card>
      )}
      {archived.length > 0 && (
        <Card>
          <Pressable onPress={() => setShowArchived((x) => !x)}>
            <Text style={s.archHead}>
              {showArchived ? "▾" : "▸"} Archived ({archived.length}) —
              closed / historical, not syncing
            </Text>
          </Pressable>
          {showArchived && archived.map((a) => (
            <AccountRow key={a.id} a={a}
              onPress={viewer ? undefined : () => openEditor(a)} />
          ))}
        </Card>
      )}

      {!query.isPending && accounts.length === 0 && (
        <Text style={s.center}>
          No accounts yet — connect your bank or add a manual account,
          then import files into it.
        </Text>
      )}

      <Modal visible={!!editing} animationType="slide" transparent
             onRequestClose={() => setEditing(null)}>
        <View style={s.sheetWrap}>
          <View style={s.sheet}>
            <Text style={s.sheetTitle}>
              {editing?.name}
              {editing?.mask ? ` ··${editing.mask}` : ""}
            </Text>
            {/* the drafts live in the child (keyed per account) so a
                keystroke re-renders the form, not the whole tab */}
            {editing && (
              <EditorFields key={editing.id} a={editing}
                            primary={editing.id === primary}
                            excluded={excludedSet.has(editing.id)}
                            manual={isManual(editing)}
                            saving={save.isPending}
                            error={save.isError ? String(save.error) : null}
                            onSave={(f) => save.mutate(f)} />
            )}
            {/* the account's place in a multi-source link — who it is
                linked with, whether it serves or backs up, and the two
                decisions. Here rather than on the status card so the
                card can be put away without losing them. */}
            {editing && (() => {
              const g = (links.data?.groups ?? []).find((x) =>
                x.members.some((m0) => m0.account_id === editing.id));
              if (!g) return null;
              const me = g.members.find((m0) => m0.account_id === editing.id)!;
              const others = g.members
                .filter((m0) => m0.account_id !== editing.id)
                .map((m0) => `${acctName(m0.account_id)} (${
                  m0.primary ? "serving" : "backup"}${m0.healthy ? "" : " ⚠"})`);
              const busy = linkBusy(g.group_id);
              return (
                <View style={{ gap: 6 }}>
                  <Text style={{ color: C.mut, fontSize: 12 }}>
                    {me.primary ? "Serving" : "Backup"}
                    {!me.healthy ? " ⚠ source down" : ""}
                    {" · linked with "}{others.join(", ")}
                  </Text>
                  <View style={{ flexDirection: "row", gap: 8,
                                 justifyContent: "center" }}>
                    {!me.primary && (
                      <Pressable style={[s.btn, s.btnQuiet,
                                   (busy || !me.healthy) && { opacity: 0.5 }]}
                                 disabled={busy || !me.healthy}
                                 onPress={() => linkAct.mutate({
                                   id: g.group_id, fn: () =>
                                   client!.accountLinkOrder(g.group_id,
                                     [editing.id, ...g.members
                                       .filter((x) => x.account_id !== editing.id)
                                       .map((x) => x.account_id)]) })}>
                        <Text style={s.btnText}>Make primary</Text>
                      </Pressable>
                    )}
                    <Pressable style={[s.btn, s.btnQuiet, busy && { opacity: 0.5 }]}
                               disabled={busy}
                               onPress={() => Alert.alert(
                                 `Unlink ${editing.name}?`,
                                 "Each source will count separately, so a "
                                 + "shared balance shows twice until you hide "
                                 + "or remove one.",
                                 [{ text: "Cancel", style: "cancel" },
                                  { text: "Unlink", style: "destructive",
                                    onPress: () => linkAct.mutate({
                                      id: g.group_id, fn: () =>
                                      client!.accountLinkDelete(g.group_id) }) }])}>
                      <Text style={[s.btnText, { color: C.warn }]}>Unlink</Text>
                    </Pressable>
                  </View>
                </View>
              );
            })()}
            {setHidden.isError ? (
              <Text style={{ color: C.bad, fontSize: 12 }}>
                {String(setHidden.error)}
              </Text>
            ) : null}
            <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8,
                           justifyContent: "center" }}>
              {owner && (
              <Pressable style={[s.btn, s.btnQuiet,
                                 setHidden.isPending && { opacity: 0.5 }]}
                         disabled={setHidden.isPending}
                         onPress={confirmHide}>
                <Text style={[s.btnText, { color: C.warn }]}>
                  {editing?.user_removed_at ? "Unhide" : "Hide"}
                </Text>
              </Pressable>
              )}
              {owner && (
              <Pressable style={[s.btn, s.btnQuiet]}
                         onPress={() => {
                           const a = editing!;
                           const liveConn = !!a.item_id && isPullAgg(a)
                             && (a.status || "ok") !== "archived";
                           setRmMode(liveConn ? "disconnect" : "purge");
                           setRemoving(a);
                         }}>
                <Text style={[s.btnText, { color: C.bad }]}>Remove…</Text>
              </Pressable>
              )}
              <Pressable style={[s.btn, s.btnQuiet]}
                         onPress={() => setEditing(null)}>
                <Text style={[s.btnText, { color: C.mut }]}>Cancel</Text>
              </Pressable>
            </View>
            <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 14,
                           justifyContent: "center", marginTop: 4 }}>
              <Text style={{ color: C.accent, fontSize: 12 }}
                    onPress={() => {
                      const id = editing!.id;
                      setEditing(null);
                      router.push({ pathname: "/transactions",
                                    params: { acct: id } } as never);
                    }}>
                view transactions ›
              </Text>
              {editing?.aggregator === "plaid" && editing.item_id
                && (editing.status || "ok") !== "archived"
                && (editing.status || "ok") !== "reaped" && (
                <Text style={{ color: C.accent, fontSize: 12 }}
                      onPress={async () => {
                        // Link's account-selection checklist — the ONLY
                        // way to stop Plaid billing a sub-account
                        const itemId = editing!.item_id!;
                        setEditing(null);
                        setPlaidBusy(true);
                        try {
                          const s0 = await plaidHostedLink(
                            client!, itemId, true);
                          setNotice(s0.kind === "update_failed"
                            ? `that didn't complete — the connection `
                              + `still reports an error${
                                s0.error ? ` (${s0.error})` : ""}`
                            : s0.kind === "exited"
                              ? "no change — the window was closed"
                              : "shared accounts updated");
                        } catch (e) { setNotice(String(e)); }
                        setPlaidBusy(false);
                        qc.invalidateQueries({ queryKey: ["accounts"] });
                        qc.invalidateQueries({ queryKey: ["connections"] });
                        // the institutions-used figure rides /api/me
                        qc.invalidateQueries({ queryKey: ["me"] });
                      }}>
                  edit connected accounts ↗
                </Text>
              )}
            </View>
          </View>
        </View>
      </Modal>

      <Modal visible={!!removing} animationType="slide" transparent
             onRequestClose={() => setRemoving(null)}>
        <View style={s.sheetWrap}>
          <View style={s.sheet}>
            <Text style={s.sheetTitle}>
              Remove {removing?.name}
              {removing?.mask ? ` ··${removing.mask}` : ""}?
            </Text>
            {removing && (() => {
              const a = removing;
              const liveConn = !!a.item_id && isPullAgg(a)
                && (a.status || "ok") !== "archived";
              const isPlaid = a.aggregator === "plaid";
              const inst = a.institution_name || "this institution";
              const opts: [AccountRemoveMode, string, string][] = liveConn
                ? [["disconnect", "Disconnect only — keep history",
                    isPlaid
                      ? `Stops syncing ${inst} and releases the Plaid `
                        + "Item (stops billing). Transaction history "
                        + "stays. Affects every account from this bank."
                      : `Stops syncing ${inst}. History stays. Affects `
                        + "every account from this bank."],
                   ["disconnect_purge", "Disconnect and delete data",
                    isPlaid
                      ? `Releases Plaid, stops syncing ${inst}, and `
                        + "permanently deletes all local accounts and "
                        + "transactions for this institution."
                      : `Stops syncing ${inst} and permanently deletes `
                        + "all local accounts and transactions for this "
                        + "institution."],
                   ["hide", "Hide this account only",
                    "Removes this account from active lists but keeps "
                    + "the bank connection (and other accounts) "
                    + "syncing. History kept. Does not free a Plaid "
                    + "slot."]]
                : [["purge", "Delete account and its transactions",
                    "Permanently deletes this account and its "
                    + "transaction history. Cannot be undone."],
                   ...(!a.user_removed_at
                     ? [["hide", "Hide this account",
                         "Move it to archived without deleting "
                         + "history."] as
                         [AccountRemoveMode, string, string]]
                     : [])];
              return (
                <>
                  <Text style={s.mut}>
                    {liveConn
                      ? "Bank connections are per institution — "
                        + "disconnect releases the whole login, not a "
                        + "single sub-account."
                      : "This account is not actively syncing."}
                  </Text>
                  {opts.map(([mode, title, sub]) => (
                    <Pressable key={mode} style={{ flexDirection: "row",
                                 gap: 8, paddingVertical: 6 }}
                               onPress={() => setRmMode(mode)}>
                      <Text style={{ color: rmMode === mode
                                       ? C.accent : C.mut, fontSize: 16 }}>
                        {rmMode === mode ? "◉" : "○"}
                      </Text>
                      <View style={{ flex: 1 }}>
                        <Text style={{ color: C.text, fontSize: 14,
                                       fontWeight: "600" }}>
                          {title}
                        </Text>
                        <Text style={s.mut}>{sub}</Text>
                      </View>
                    </Pressable>
                  ))}
                </>
              );
            })()}
            <View style={{ flexDirection: "row", gap: 8,
                           justifyContent: "center" }}>
              <Pressable style={[s.btn, { backgroundColor: C.bad },
                           remove.isPending && { opacity: 0.5 }]}
                         disabled={remove.isPending}
                         onPress={confirmRemove}>
                <Text style={s.btnText}>
                  {remove.isPending ? "removing…" : "Confirm remove"}
                </Text>
              </Pressable>
              <Pressable style={[s.btn, s.btnQuiet]}
                         onPress={() => setRemoving(null)}>
                <Text style={[s.btnText, { color: C.mut }]}>Cancel</Text>
              </Pressable>
            </View>
          </View>
        </View>
      </Modal>

      <Modal visible={adding} animationType="slide" transparent
             onRequestClose={() => setAdding(false)}>
        <View style={s.sheetWrap}>
          <View style={s.sheet}>
            <Text style={s.sheetTitle}>Add a manual account</Text>
            {adding && (
              <AddFields pending={add.isPending}
                         error={add.isError ? String(add.error) : null}
                         onAdd={(f) => add.mutate(f)}
                         onCancel={() => setAdding(false)} />
            )}
          </View>
        </View>
      </Modal>
      <HelpLink topic="accounts" />
    </ScrollView>
  );
}

const isManual = (a: Account) =>
  a.institution_name === "Manual" || a.aggregator === "manual";

// Draft TextInput state lives in these three small components rather than
// in the tab, so a keystroke does not re-render the whole Accounts screen
// (groups, categories, hero math recomputed) for one character.

interface EditorForm { name: string; owner: string; kind: string;
                       primary: boolean; excl: boolean; bal: string }

// the web's per-account editor fields: rename, owner, kind, primary
// checking, exclude-from-budget — and a balance field ONLY for manual
// accounts (connected ones get theirs from the feed)
function EditorFields({ a, primary, excluded, manual, saving, error,
                        onSave }: {
  a: Account; primary: boolean; excluded: boolean; manual: boolean;
  saving: boolean; error: string | null; onSave: (f: EditorForm) => void;
}) {
  const [eName, setEName] = useState(a.name);
  const [eOwner, setEOwner] = useState(a.owner ?? "");
  const [eKind, setEKind] = useState(a.kind);
  const [ePrimary, setEPrimary] = useState(primary);
  const [eExcl, setEExcl] = useState(excluded);
  const [editBal, setEditBal] = useState(
    a.balance_current !== null ? String(a.balance_current) : "");
  return (
    <>
      <TextInput style={s.input}
                 placeholder={a.bank_name || "display name"}
                 placeholderTextColor={C.mut} value={eName}
                 onChangeText={setEName} />
      {!!a.bank_name && a.name !== a.bank_name && (
        <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
          <Text style={{ color: C.mut, fontSize: 12, flexShrink: 1 }}>
            bank name “{a.bank_name}”
          </Text>
          <Pressable onPress={() => setEName("")}
                     hitSlop={8}>
            <Text style={{ color: C.accent, fontSize: 12 }}>Revert</Text>
          </Pressable>
        </View>
      )}
      <TextInput style={s.input}
                 placeholder="owner (yours / mine / ours)"
                 placeholderTextColor={C.mut} value={eOwner}
                 onChangeText={setEOwner} />
      <View style={s.kindRow}>
        {(KINDS.some(([v]) => v === eKind)
          ? KINDS : [...KINDS, [eKind, a.kind]] as
            [string, string][]).map(([v, l]) => (
          <Pressable key={v}
                     style={[s.kind, eKind === v && s.kindOn]}
                     onPress={() => setEKind(v)}>
            <Text style={{ color: eKind === v ? C.text : C.mut,
                           fontSize: 12 }}>{l}</Text>
          </Pressable>
        ))}
      </View>
      <View style={s.toggleRow}>
        <View style={{ flex: 1 }}>
          <Text style={s.toggleLabel}>Primary checking</Text>
          <Text style={s.toggleSub}>
            anchor the cash forecast on this account
          </Text>
        </View>
        <Switch value={ePrimary} onValueChange={setEPrimary}
                trackColor={{ false: C.hover, true: C.accent }}
                thumbColor={C.text} />
      </View>
      <View style={s.toggleRow}>
        <View style={{ flex: 1 }}>
          <Text style={s.toggleLabel}>Exclude from budget</Text>
          <Text style={s.toggleSub}>
            its spending never counts toward the verdict
          </Text>
        </View>
        <Switch value={eExcl} onValueChange={setEExcl}
                trackColor={{ false: C.hover, true: C.accent }}
                thumbColor={C.text} />
      </View>
      {manual && (
        <TextInput style={s.input} placeholder="balance ($)"
                   placeholderTextColor={C.mut} value={editBal}
                   onChangeText={setEditBal}
                   keyboardType="decimal-pad" />
      )}
      {error ? (
        <Text style={{ color: C.bad, fontSize: 12 }}>{error}</Text>
      ) : null}
      <Pressable style={[s.btn, { alignSelf: "center" },
                         saving && { opacity: 0.5 }]}
                 disabled={saving}
                 onPress={() => onSave({ name: eName, owner: eOwner,
                   kind: eKind, primary: ePrimary, excl: eExcl,
                   bal: editBal })}>
        <Text style={s.btnText}>Save</Text>
      </Pressable>
    </>
  );
}

function AddFields({ pending, error, onAdd, onCancel }: {
  pending: boolean; error: string | null;
  onAdd: (f: { name: string; kind: string; bal: string }) => void;
  onCancel: () => void;
}) {
  const [newName, setNewName] = useState("");
  const [newKind, setNewKind] = useState(KINDS[0][0]);
  const [newBal, setNewBal] = useState("");
  return (
    <>
      <TextInput style={s.input} placeholder="name (e.g. HSA)"
                 placeholderTextColor={C.mut} value={newName}
                 onChangeText={setNewName} />
      <View style={s.kindRow}>
        {KINDS.map(([v, l]) => (
          <Pressable key={v}
                     style={[s.kind, newKind === v && s.kindOn]}
                     onPress={() => setNewKind(v)}>
            <Text style={{ color: newKind === v ? C.text : C.mut,
                           fontSize: 12 }}>{l}</Text>
          </Pressable>
        ))}
      </View>
      <TextInput style={s.input} placeholder="starting balance ($)"
                 placeholderTextColor={C.mut} value={newBal}
                 onChangeText={setNewBal} keyboardType="decimal-pad" />
      {error ? (
        <Text style={{ color: C.bad, fontSize: 12 }}>{error}</Text>
      ) : null}
      <View style={{ flexDirection: "row", gap: 8,
                     justifyContent: "center" }}>
        <Pressable style={[s.btn, (!newName.trim() || pending)
                            && { opacity: 0.5 }]}
                   disabled={!newName.trim() || pending}
                   onPress={() => onAdd({ name: newName, kind: newKind,
                                          bal: newBal })}>
          <Text style={s.btnText}>Add</Text>
        </Pressable>
        <Pressable style={[s.btn, s.btnQuiet]} onPress={onCancel}>
          <Text style={[s.btnText, { color: C.mut }]}>Cancel</Text>
        </Pressable>
      </View>
    </>
  );
}

function SimplefinPaste({ pending, onConnect }: {
  pending: boolean; onConnect: (token: string) => void;
}) {
  const [sfToken, setSfToken] = useState("");
  return (
    <View style={{ flexDirection: "row", gap: 8, marginTop: 8 }}>
      <TextInput style={[s.input, { flex: 1 }]}
                 placeholder="SimpleFIN setup token"
                 placeholderTextColor={C.mut} autoCapitalize="none"
                 value={sfToken} onChangeText={setSfToken} />
      <Pressable style={[s.btn,
                   (!sfToken.trim() || pending) && { opacity: 0.5 }]}
                 disabled={!sfToken.trim() || pending}
                 onPress={() => onConnect(sfToken.trim())}>
        <Text style={s.btnText}>
          {pending ? "claiming…" : "connect"}
        </Text>
      </Pressable>
    </View>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  center: { color: C.mut, textAlign: "center", padding: 20 },
  archHead: { color: C.mut, fontSize: 13, fontWeight: "600" },
  syncRow: { flexDirection: "row", alignItems: "center", gap: 10,
             marginHorizontal: 12, marginTop: 10 },
  syncBtn: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
             borderRadius: 999, paddingHorizontal: 14, paddingVertical: 7 },
  syncText: { color: C.accent, fontSize: 13, fontWeight: "600" },
  sheetWrap: { flex: 1, justifyContent: "flex-end",
               backgroundColor: "rgba(0,0,0,0.55)" },
  sheet: { backgroundColor: C.card, borderTopLeftRadius: 16,
           borderTopRightRadius: 16, padding: 16, gap: 10 },
  sheetTitle: { color: C.text, fontSize: 16, fontWeight: "700" },
  input: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
           borderRadius: 8, color: C.text, fontSize: 15,
           paddingHorizontal: 10, paddingVertical: 8 },
  kindRow: { flexDirection: "row", flexWrap: "wrap", gap: 6 },
  toggleRow: { flexDirection: "row", alignItems: "center", gap: 10 },
  toggleLabel: { color: C.text, fontSize: 14, fontWeight: "600" },
  toggleSub: { color: C.mut, fontSize: 11 },
  kind: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
          borderRadius: 999, paddingHorizontal: 10, paddingVertical: 5 },
  kindOn: { borderColor: C.accent, backgroundColor: C.hover },
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 18, paddingVertical: 9 },
  btnQuiet: { backgroundColor: C.hover },
  btnText: { color: C.text, fontSize: 14, fontWeight: "600" },
  row: { flexDirection: "row", alignItems: "center", gap: 10,
         borderTopColor: C.border, borderTopWidth: StyleSheet.hairlineWidth,
         paddingVertical: 8 },
  name: { color: C.text, fontSize: 14, fontWeight: "600" },
  linkRow: { flexDirection: "row", alignItems: "center", gap: 8,
             borderTopColor: C.border,
             borderTopWidth: StyleSheet.hairlineWidth,
             paddingVertical: 8 },
  bal: { color: C.text, fontSize: 15, fontWeight: "600",
         fontVariant: ["tabular-nums"] },
  mut: { color: C.mut, fontSize: 12, fontWeight: "400" },
});
