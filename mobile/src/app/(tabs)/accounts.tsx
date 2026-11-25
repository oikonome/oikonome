// Accounts — the money's home: hero totals, connections (Plaid link +
// fix in the in-app browser, SimpleFIN paste, per-institution sync,
// disconnect), the per-account editor with the full remove ladder,
// linked sources, hidden vs archived, holdings, and the business pane.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ext } from "../../ext";
import { useRouter, useScrollToTop } from "expo-router";
import { useEffect, useMemo, useRef, useState } from "react";
import { Image as ExpoImage } from "expo-image";
import { Alert, Image, KeyboardAvoidingView, Modal, Platform, Pressable,
         ScrollView, StyleSheet, Switch, Text, TextInput, View }
  from "react-native";
import PullRefresh from "../../components/pull-refresh";

import { FailedLine, StaleLine } from "../../components/loaded";
import { Card, H, HelpLink, KV, Pill, SharedLock, Skeleton, Note } from "../../components/ui";
import { Account, AccountRemoveMode, type Connection, errText,
         linkCardVisible, releasedHoldDays,
         removedConnectionNote } from "../../lib/api";
import { patchList, patchQuery, removeFromList } from "../../lib/cache";
import { plaidHostedLink, type PlaidLinkOutcome, startPlaidLink,
         useFirstSyncLanding, useRelinkingItems } from "../../lib/plaid";
import { PendingLinkRows } from "../../components/pending-links";
import { ago } from "../../lib/dates";
import { CONFIRM_TO_CONNECT, VerifyEmailCard }
  from "../../components/verify-email-card";
import { firstSyncPending, groupByLogin, holdingSymbol, addedLine, accountRemoveMoved, accountRemoveNote, bankAwaitsConfirm, hueOf, keptRowsQuestion, institutionAllowance, lastSyncPhrase,
         listPhase, monogram, parseMoneyDraft, purgeKeptRows,
         classifyChange, accountSwitches, kindWords }
  from "../../lib/pure";
import { useLogosOn } from "../../components/merchant-avatar";
import { historyMonthsOf, historyRowNote, windowNarrower }
  from "../../components/wizard-logic";
import { useSession } from "../../lib/session";
import { useMe, useOwner, useSharedLock, useViewer } from "../../lib/viewer";
import { C, mmddyy, money } from "../../lib/theme";
import { disclosure, useSheetAnimation } from "../../lib/a11y";
import { LogoSpinner } from "../../components/logo-spinner";
import { fieldA11y } from "../../components/field";

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

/** A stamp's day on this device's calendar, as the web's toLocaleString
 *  reads it: a push at 11pm here is not dated tomorrow from its UTC. */
function seenDay(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const p = (n: number) => String(n).padStart(2, "0");
  return mmddyy(`${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`);
}

function AccountRow({ a, onPress, primary, pinnedUnused, excluded, badge,
                      shown, dim }:
    { a: Account; onPress?: () => void;
      // a row of a login whose accounts are all linked backups
      dim?: boolean;
      // the balance in the whole dollars its section adds up in (the
      // server's `shown.by_account`, or `backup_shown` for a linked
      // backup that agrees with its serving account); absent for any
      // other account the section does not count, or from an older server
      shown?: number;
      // the server's words for why the pin is passed over ("" when it
      // sent none); undefined when this row is not a passed-over pin
      primary?: boolean; pinnedUnused?: string; excluded?: boolean;
      badge?: string }) {
  const bal = a.balance_current;
  const dot = statusDot(a);
  // linked-source state rides the row (the web's pills): the primary is
  // serving, others are backups, and any sick source — a backup included
  // — is flagged down
  const linkFlag = a.link
    ? [a.link.primary ? "serving" : "backup",
       a.link.healthy ? "" : "source down"].filter(Boolean).join(" · ")
    : "";
  const flags = [
    primary ? "primary" : "",
    pinnedUnused !== undefined
      ? `pinned — not used${pinnedUnused ? `: ${pinnedUnused}` : ""}` : "",
    excluded ? "excl" : "",           // the web's pill names
    a.user_removed_at ? "removed" : "",
    linkFlag,
    // the web's red-dot tooltip: a collector whose script token was
    // revoked after its last good push (a password change or reset
    // revokes them all) is being refused, not broken — name it
    // dated as the web's tooltip dates it
    a.script && !a.script.ok && a.script.token_revoked_at
      ? `token revoked ${seenDay(a.script.token_revoked_at)}` : "",
  ].filter(Boolean).join(" · ");
  return (
    <Pressable accessibilityRole="button" onPress={onPress}
               style={({ pressed }) => [s.row, dim && { opacity: 0.6 },
                 pressed && onPress ? { backgroundColor: C.hover } : null]}>
      <View style={{ flex: 1 }}>
        <Text style={s.name} numberOfLines={1}>
          <Text style={{ color: dot }}>● </Text>
          {a.name}{a.mask ? <Text style={s.mut}> ··{a.mask}</Text> : null}
        </Text>
        <Text style={s.mut}>
          {/* the type in the web's words ("Checking", "403(b)"), not
              the feed's "depository/checking" pair */}
          {kindWords(a.kind)}{a.owner ? ` · ${a.owner}` : ""} · {a.txns} txns
          {badge ? ` · ${badge}` : ""}
          {flags ? <Text style={{ color: C.warn }}>  {flags}</Text> : null}
        </Text>
        {/* a collector's last good run, the web dot's tooltip: a quiet
            one says since when */}
        {a.script?.last_push ? (
          <Text style={[s.mut, !a.script.ok && { color: C.bad }]}>
            {a.script.ok ? "last run" : "no successful run since"}{" "}
            {seenDay(a.script.last_push)}
          </Text>) : null}
      </View>
      <View style={{ alignItems: "flex-end" }}>
        <Text style={s.bal}>
          {shown !== undefined ? money(shown, false)
            : bal === null ? "—" : money(bal, false)}
        </Text>
        {/* a card's next due date, right where the card is (web parity) */}
        {/* the server's words: "due 11/25", or once that date has gone
            by "paid 11/14" / "past due since 11/15". The server decides
            whether there is a line at all; a server that sends none gets
            none, never a "due" worded here from the bare date */}
        {a.card_due && a.card_due_text ? (
          <Text style={s.mut}>
            {a.card_due_text}
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
    return <ExpoImage source={{ uri: client.baseUrl + a.inst_logo_url,
                                headers: client.authHeader }}
                      cachePolicy="memory-disk"
                      style={{ width: size, height: size, borderRadius: 4,
                               backgroundColor: "#fff" }} contentFit="contain" />;
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
  const sheetAnim = useSheetAnimation();
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
  // every connect door plants a credential that outlives the shared
  // demonstration login, so the server refuses it; the buttons say so
  const credentialLock = useSharedLock("credential");
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
  // a free trial whose owner has not confirmed has an allowance of zero
  // (the server says so on /api/me): the bank door
  // closes with the confirm card above it, before the tap the server
  // refuses; the manual account and its imports stay open (the web
  // Accounts ConnectCard)
  const confirmFirst = bankAwaitsConfirm(me.data);
  // Plaid: add / fix / reconnect all funnel through the hosted link
  const [plaidBusy, setPlaidBusy] = useState(false);
  // banks whose fresh link is still pending: the old row stays listed
  // and live, so its "more history" door waits for that link to land
  const relinking = useRelinkingItems();
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
    // ...and a just-linked bank's first sync, queued on the worker, polls
    // until it lands (then its accounts are refreshed, below)
    refetchInterval: (q) => plaidBusy ? 3000 : syncing ? 5000
      : (q.state.data?.connections ?? []).some(firstSyncPending) ? 5000
      : false,
  });
  useFirstSyncLanding(conns.data?.connections);
  // both move only after a sync, and the running→done edge invalidates
  const holdings = useQuery({
    queryKey: ["holdings"],
    queryFn: () => client!.holdings(),
    enabled: !!client, staleTime: 5 * 60_000,
  });
  const links = useQuery({
    queryKey: ["account-links"],
    queryFn: () => client!.accountLinks(),
    enabled: !!client && !viewer, staleTime: 5 * 60_000,
  });
  // `manage` opens Link's account checklist, which update mode needs
  // before it offers accounts opened at the bank since the link
  // relinkItemId: the same bank linked afresh with the household's
  // wider history window (the old link stays live until the new one of
  // the same bank is saved; a sign-in at another bank just adds it)
  // what a session's answer changes on this screen; the answer of an add
  // or a relink lands whenever it does (the session is a pending row
  // under the button until then), a fix is waited for in place
  const afterLink = (s0: PlaidLinkOutcome) => {
    let changed = true;
    let added = false;
    if (s0.kind === "exited" || s0.kind === "cancelled") {
      // backing out is normal, never red — and nothing changed
      setNotice("No change — the sign-in window was closed.");
      changed = false;
    } else if (s0.kind === "add_failed" && s0.summary)
      setNotice(s0.summary, true);
    else if (s0.kind === "add_failed" || s0.kind === "update_failed")
      setNotice(`Link failed: ${s0.error ?? "unknown"}`, true);
    else if (s0.kind === "update") setNotice("Reconnected.");
    else { setNotice(addedLine(s0)); added = true; }
    return { changed, added };
  };
  const plaid = async (itemId = "", manage = false, relinkItemId = "") => {
    setPlaidBusy(true);
    let changed = true;
    let added = false;
    try {
      if (!itemId) {
        // busy only while the bank's browser opens; the row below carries
        // the wait, and the button is free for the next bank
        setNotice(null);
        await startPlaidLink(client!, qc,
          { kind: relinkItemId ? "relink" : "add", relinkItemId },
          (r) => {
            if (r.kind === "cancelled") return;
            const out = afterLink(r);
            if (!out.changed) return;
            for (const k of ["connections", "me", "accounts"])
              qc.invalidateQueries({ queryKey: [k] });
            if (out.added) {
              qc.invalidateQueries({ queryKey: ["onboarding"] });
              qc.invalidateQueries({ queryKey: ["onboarding-backfill"] });
            }
          });
        setPlaidBusy(false);
        // the old row stays until the new link lands; this only refetches
        if (relinkItemId) qc.invalidateQueries({ queryKey: ["connections"] });
        return;
      }
      setNotice("Finish signing in at your bank…");
      const s0 = await plaidHostedLink(client!, itemId, manage);
      ({ changed, added } = afterLink(s0));
    } catch (e) { setNotice(errText(e), true); }
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
    onError: (e) => setNotice(errText(e), true),
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
    // a refused or failed sync (rate limit, bank down, offline) must say
    // so — otherwise the spinner just stops, which reads as "synced"
    onError: (e) => setNotice(errText(e), true),
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
      // a linked shadow's positions drop out of top holdings
      for (const k of ["account-links", "accounts", "today", "holdings"])
        qc.invalidateQueries({ queryKey: [k] });
    },
    onError: (e) => {
      // overlapping suggestions go stale after one is linked — surface
      // the message AND refetch so dead pairs drop out
      setNotice(errText(e), true);
      qc.invalidateQueries({ queryKey: ["account-links"] });
    },
  });
  const [adding, setAdding] = useState(false);
  // the web's per-account editor, as a sheet: rename, owner, kind,
  // primary checking, exclude-from-budget, hide — and a balance field
  // ONLY for manual accounts (connected ones get theirs from the feed)
  // viewers see the primary/excluded pills read-only too
  // the same GET /api/settings the Settings screen holds, projected to
  // the two fields the editor reads — one query, not a second copy
  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: () => client!.settingsFull(),
    select: (v) => ({
      checking_account_id: (v.checking_account_id as string | null) ?? null,
      excluded_accounts: (v.excluded_accounts as string[] | null) ?? [],
      history_months: historyMonthsOf(v.history_months) }),
    enabled: !!client,
  });
  // the pin as saved (the editor's switch) and the account the cash
  // forecast actually starts from — the web's pair: a pin on an excluded
  // account or a linked pair's backup is kept but anchors nothing, so the
  // pill follows the server and the dead pin says why
  const primary = settings.data?.checking_account_id ?? null;
  const anchor = query.data?.primary_checking_id ?? null;
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
      // the name as the server stored it (a typed card number is cut to
      // its last four), which is what the cache then holds
      const renamed = (revert ? a.bank_name || a.name : trimmed) !== a.name
        ? client!.accountRename(a.id, revert ? "" : trimmed) : null;
      if (renamed) writes.push(renamed);
      // the exclusion lands BEFORE the pin is sent: the pin door refuses
      // an excluded account, so including and pinning in one save must
      // not race (the web's order)
      const excl = f.excl !== null
        ? client!.accountExclude(a.id, f.excl) : Promise.resolve();
      writes.push(excl);
      const cls = classifyChange(a.kind, f.kind, f.primary);
      if (cls) writes.push(excl.then(() =>
        client!.accountClassify(a.id, cls.kind, cls.primary)));
      if (f.owner.trim() !== (a.owner ?? ""))
        writes.push(client!.accountOwner(a.id, f.owner.trim()));
      if (isManual(a) && f.bal.trim() !== ""
          && (parseMoneyDraft(f.bal) ?? NaN) !== a.balance_current)
        writes.push(client!.accountBalance(a.id, f.bal));
      await Promise.all(writes);
      return renamed ? (await renamed).name ?? null : null;
    },
    onSuccess: (stored, f) => {
      const a = editing!;
      setEditing(null);
      const balChanged = isManual(a) && f.bal.trim() !== ""
        && (parseMoneyDraft(f.bal) ?? NaN) !== a.balance_current;
      patchList<{ accounts: Account[] }, Account>(qc, ["accounts"],
        "accounts", (x) => x.id === a.id, {
          name: stored ?? (!f.name.trim() || f.name.trim() === a.bank_name
            ? a.bank_name || a.name : f.name.trim()),
          kind: f.kind,
          owner: f.owner.trim() || null,
          ...(balChanged
              ? { balance_current: (parseMoneyDraft(f.bal) ?? NaN) } : {}),
        });
      const wasPrimary = a.id === primary;
      const wasExcl = excludedSet.has(a.id);
      // patched into ["settings"], the one cache of GET /api/settings,
      // so a primary/exclusion change is visible to every settings reader
      const newSettingsAccts = {
        checking_account_id: f.primary === null ? primary
          : f.primary ? a.id : wasPrimary ? null : primary,
        excluded_accounts: f.excl === null ? [...excludedSet]
          : f.excl ? [...new Set([...excludedSet, a.id])]
          : [...excludedSet].filter((id) => id !== a.id),
      };
      patchQuery(qc, ["settings"], newSettingsAccts);
      qc.invalidateQueries({ queryKey: ["accounts"] });
      qc.invalidateQueries({ queryKey: ["settings"] });
      // only the forecast anchor, the budget exclusion, a balance and
      // the kind (income detection and the card-payment forecast key on
      // it) move the verdict; a rename or a new owner cannot
      if ((f.primary !== null && f.primary !== wasPrimary)
          || (f.excl !== null && f.excl !== wasExcl) || balChanged
          || f.kind !== a.kind)
        qc.invalidateQueries({ queryKey: ["today"], refetchType: "none" });
    },
    // the writes are separate doors: a refused pin can follow an exclusion
    // that already landed, so the cache is re-read, not left stale
    onError: () => {
      qc.invalidateQueries({ queryKey: ["accounts"] });
      qc.invalidateQueries({ queryKey: ["settings"] });
      qc.invalidateQueries({ queryKey: ["today"], refetchType: "none" });
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
      qc.invalidateQueries({ queryKey: ["today"], refetchType: "none" });
      qc.invalidateQueries({ queryKey: ["transactions"], refetchType: "none" });
      // top holdings leaves a hidden account's positions out
      qc.invalidateQueries({ queryKey: ["holdings"] });
    },
  });
  // A disconnected MX bank is still alive at MX, so it comes back on the
  // owner's word rather than through a new link; the server marks how far
  // back the next sync has to reach.
  const reconnect = useMutation({
    mutationFn: (a: Account) => client!.connectionReconnect(a.item_id!),
    onSuccess: (_r, a) => {
      patchList<{ accounts: Account[] }, Account>(qc, ["accounts"],
        "accounts", (x) => x.item_id === a.item_id, { status: "ok" });
      setEditing(null);
      setNotice(`${a.institution_name || "The bank"} is reconnected — the `
                + "next sync also brings in what arrived while it was "
                + "disconnected.");
      qc.invalidateQueries({ queryKey: ["accounts"] });
      qc.invalidateQueries({ queryKey: ["connections"] });
      qc.invalidateQueries({ queryKey: ["me"] });
    },
    onError: (e) => setNotice(errText(e), true),
  });
  // a link the bank feed removed that the household has since linked
  // again: history, not work — forgetting it is the disconnect door,
  // which on a removed link only stops listing it (the web's
  // ReplacedConnections)
  const forget = useMutation({
    mutationFn: (id: string) => client!.connectionDisconnect(id),
    onSuccess: (_r, id) => {
      removeFromList<{ connections: Connection[] }, Connection>(
        qc, ["connections"], "connections", (c) => c.id === id);
      setNotice("Forgotten — its history stays.");
      qc.invalidateQueries({ queryKey: ["connections"] });
    },
    onError: (e) => setNotice(errText(e), true),
  });
  const replaced = (conns.data?.connections ?? []).filter((c) => c.replaced);
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
  // iOS presents a modal from the root view controller, which cannot
  // present a second one while the editor is up (or still sliding away):
  // the remove sheet would never appear. So the editor closes first and
  // the remove sheet opens once it has gone.
  const removeAfterEdit = useRef<Account | null>(null);
  const remove = useMutation({
    mutationFn: (forceKept: boolean = false) =>
      client!.accountRemove(removing!.id, rmMode, forceKept),
    onSuccess: (r) => {
      // the server names every account the mode touched: purged rows
      // leave the list now, a hidden one moves pile now; a plain
      // disconnect keeps its rows (only their status changes)
      const hit = new Set(r.affected_accounts ?? [removing!.id]);
      // a purge that kept rows keeps their account too; the refetch
      // below says which
      if ((rmMode === "purge" || rmMode === "disconnect_purge")
          && !purgeKeptRows(r))
        removeFromList<{ accounts: Account[] }, Account>(qc, ["accounts"],
          "accounts", (x) => hit.has(x.id));
      else if (rmMode === "hide")
        patchList<{ accounts: Account[] }, Account>(qc, ["accounts"],
          "accounts", (x) => hit.has(x.id),
          { user_removed_at: new Date().toISOString() });
      setRemoving(null); setEditing(null);
      setNotice(accountRemoveNote(r));
      // One list, so the mode decides the keys in one place and the web
      // decides them the same way (pure.ts `accountRemoveMoved`). The
      // allowance on /api/me is exactly the thing a disconnect frees.
      for (const k of accountRemoveMoved(rmMode))
        qc.invalidateQueries({ queryKey: [k] });
    },
    onError: (e) => {
      // rows only a note naming no feed account keeps: the server asks
      // before deleting them, and the answer re-sends the same removal
      const q = keptRowsQuestion(e);
      if (q) {
        Alert.alert("Delete anyway?", q,
          [{ text: "Delete anyway", style: "destructive",
             onPress: () => remove.mutate(true) },
           { text: "Cancel", style: "cancel" }]);
        return;
      }
      setNotice(errText(e), true);
    },
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
         onPress: () => remove.mutate(false) },
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
      qc.invalidateQueries({ queryKey: ["today"], refetchType: "none" });
      qc.invalidateQueries({ queryKey: ["transactions"], refetchType: "none" });
    }
    prevState.current = status.data?.state;
  }, [status.data?.state, qc]);
  const accounts = query.data?.accounts ?? [];
  // a first fetch that failed is not "No accounts yet — connect your bank"
  const acctPhase = listPhase({ isError: query.isError,
                                hasData: !!query.data }, accounts.length);
  // HIDDEN ≠ ARCHIVED (web doctrine): archived = the connection is
  // gone (no balance, closed lineage); hidden = the owner switched one
  // account off while the connection stays live. Both quiet, opposite
  // copy. Live = a balance-carrying, not-hidden feed.
  const isLive = (a: Account) =>
    a.balance_current !== null && !a.user_removed_at;
  // rebuilt on every poll response / notice / modal open otherwise —
  // harmless at 20 accounts, quadratic (a spread-per-push grouping) at
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
  // one card per bank LOGIN, as the web: two logins at one bank are two
  // cards, each with the subtotal the server sends for it
  type Inst = { key: string; inst: string; list: Account[] };
  // the web's category tier: Bank / Credit Cards / Investments / Loans,
  // each with its own total, institutions nested inside
  const catOf = (kind: string): string => {
    const t = (kind || "").split("/")[0];
    return t === "credit" ? "Credit Cards"
      : t === "investment" ? "Investments"
      : t === "loan" ? "Loans" : "Bank";
  };
  const CATEGORY_ORDER = ["Bank", "Credit Cards", "Investments", "Loans"];
  // the headline and the section subtotals are the server's
  // (reporting.account_totals); an older server sends none and the
  // figures are left out rather than worked out here
  const totals = query.data?.totals;
  const { bizCategories, categories, shownBalance } = useMemo(() => {
    const groups0 = groupByLogin(personal);
    const bizGroups0 = groupByLogin(business);
    // the web's category tier, per pane: each section's total and each
    // login's subtotal in it are the server's whole dollars
    // (reporting.account_totals → shown.total / shown.by_item); an older
    // server sends no `shown`, and the subtotal is left out
    const tiers = (pane: "personal" | "business", gs: Inst[]) =>
      CATEGORY_ORDER.map((cat) => {
        const sec = totals?.sections[pane]?.[cat];
        const insts = gs
          .map((g) => ({ ...g, list: g.list.filter((a) => catOf(a.kind) === cat),
                         sub: sec?.shown?.by_item[g.key] }))
          .filter((g) => g.list.length > 0);
        // the whole dollars the screen prints, so the rows below add up
        // to it as shown; an older server sends none and prints none
        const total = sec?.shown?.total ?? null;
        // the server's words when the headline counts other than the
        // section's total (a card's debt comes off it, a loan is not in it)
        const counted = sec?.shown?.counted_text ?? null;
        return { cat, insts, total, counted };
      }).filter((c) => c.insts.length > 0);
    const categories0 = tiers("personal", groups0);
    const bizCategories0 = tiers("business", bizGroups0);
    // each counted account's balance in the whole dollars its section
    // adds up in (reporting.account_totals → shown.by_account)
    const shownBalance0 = new Map<string, number>();
    for (const pane of Object.values(totals?.sections ?? {}))
      for (const sec of Object.values(pane))
        for (const [id, d] of Object.entries(sec.shown?.by_account ?? {}))
          shownBalance0.set(id, d);
    // a linked backup whose balance is its serving account's prints that
    // account's dollars (reporting.account_totals → backup_shown)
    for (const [id, d] of Object.entries(totals?.backup_shown ?? {}))
      shownBalance0.set(id, d);
    return { bizCategories: bizCategories0,
             categories: categories0, shownBalance: shownBalance0 };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [personal, business, totals]);
  const connById = useMemo(() => new Map((conns.data?.connections ?? [])
    .map((c) => [c.id, c])), [conns.data]);
  // provenance: what feeds each institution's rows (the web's badge)
  // An account under no known connection is a manual one only when the
  // connections read answered; unread, nothing is said about it.
  const connsKnown = conns.data !== undefined;
  const badgeLabel = (a: Account): string | undefined => {
    if (a.script) return "Script";
    const conn = a.item_id ? connById.get(a.item_id) : null;
    if (!conn) return connsKnown ? "Import" : undefined;
    return conn.aggregator === "plaid" ? "Plaid"
      : conn.aggregator === "mx" ? "MX"
      : conn.aggregator.startsWith("simplefin") ? "SimpleFIN"
      : conn.aggregator;
  };
  // the hero is the Net Worth screen's Financial accounts figure, under
  // the same label, with its counts — the server's; loans are listed
  // under it as owed, not taken off it
  // the server's count, as on the web: a self-clearing bank outage and a
  // removed link the household has linked again are not "needs attention"
  const needsAttention = conns.data?.attention ?? 0;
  // one broken Plaid connection per item gets its fix affordance
  // only the ones a PERSON can act on: a bank that is down retries by
  // itself, and offering re-auth for it is a button that appears to work
  const brokenPlaid = (conns.data?.connections ?? []).filter(
    (c) => c.aggregator === "plaid"
      // older-server path: stands in for `status_kind`
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
      <StaleLine query={query} />
      {/* the reads the rows are labelled from: unread connections are not
          manual accounts, unread settings are not "nothing excluded" */}
      {!!query.data && !connsKnown && conns.isError && (
        <FailedLine q={conns} what="your bank connections" />
      )}
      {!!query.data && settings.data === undefined && settings.isError && (
        <FailedLine q={settings} what="which accounts are excluded or primary" />
      )}
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
      {!viewer && owner && confirmFirst && (
        <VerifyEmailCard me={me.data} note={CONFIRM_TO_CONNECT} />)}
      {!viewer && (
        <View style={s.syncRow}>
          {owner && (
            // closes at the plan's cap, like the web's ConnectCard: the
            // server refuses past it anyway, but on the hosted link path
            // that refusal can land after the bank was authorized
            <SharedLock note={credentialLock}>
              <Pressable accessibilityRole="button" style={[s.syncBtn, (plaidBusy || instFull
                                             || confirmFirst)
                                            && { opacity: 0.5 }]}
                         disabled={plaidBusy || instFull || confirmFirst}
                         accessibilityHint={confirmFirst ? CONFIRM_TO_CONNECT
                                                         : undefined}
                         onPress={() => plaid()}>
                <Text style={s.syncText}>
                  {plaidBusy ? "Connecting…" : "＋ Connect account ↗"}
                </Text>
              </Pressable>
            </SharedLock>
          )}
          <PendingLinkRows />
          <Pressable accessibilityRole="button" style={s.syncBtn} onPress={() => setAdding(true)}>
            <Text style={s.syncText}>＋ Add account</Text>
          </Pressable>
          {owner && !hosted && !credentialLock && (
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
      {owner && instCapKnown && instFull && !confirmFirst && (
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
      {query.isPending && <Skeleton />}
      {notice ? (
        <Note bad={notice.bad} style={{ color: notice.bad ? C.bad : C.good, fontSize: 12,
                       marginHorizontal: 12, marginTop: 4 }}
              onPress={() => setNotice(null)}>
          {notice.text}
        </Note>
      ) : null}
      {live.length > 0 && (
        <Card>
          {totals ? <>
            <Text style={s.mut}>{totals.label}</Text>
            <Text style={{ color: C.text, fontSize: 24, fontWeight: "700",
                           fontVariant: ["tabular-nums"] }}>
              {money(totals.financial_total, false)}
            </Text>
          </> : null}
          <Text style={s.mut}>
            {totals ? `across ${totals.account_count} account${
              totals.account_count === 1 ? "" : "s"} · ${
              totals.institution_count} institution${
              totals.institution_count === 1 ? "" : "s"}` : ""}
            {lastSyncPhrase(conns.data?.last_sync)
              ? `${totals ? " · " : ""}${lastSyncPhrase(conns.data?.last_sync)}`
              : ""}
          </Text>
          {totals && totals.loan_count > 0 && (
            <Text style={s.mut}>
              loans <Text style={{ color: C.bad }}>
                {money(totals.loans_owed, false)}</Text> owed — listed, not
              taken off the total
            </Text>
          )}
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
        // fix and reconnect open a Link session, refused the shared
        // demonstration login like every other credential door
        <SharedLock note={credentialLock}>
        <View style={{ marginHorizontal: 12 }}>
          {brokenPlaid.map((c) => (
            <View key={c.id} style={{ flexDirection: "row",
                     alignItems: "center", gap: 8, marginTop: 8 }}>
              <Text style={{ color: C.bad, fontSize: 12, flex: 1 }}>
                {c.institution_name || c.id}:{" "}
                {(c.status ?? "").toUpperCase().includes("LOGIN_REQUIRED")
                  ? "sign-in expired — re-authenticate at the bank"
                  : c.status === "reaped"
                    ? removedConnectionNote(c)
                    : c.status === "new_accounts"
                      ? "new accounts to share"
                      : "sync failing"}
              </Text>
              <Pressable accessibilityRole="button"
                         style={[s.btn, (plaidBusy || (c.status === "reaped"
                                                      && confirmFirst))
                                        && { opacity: 0.5 }]}
                         // a fresh link is a new connection, closed to a
                         // trial still waiting on confirmation (the card
                         // above says why); fixing a live one stays open
                         disabled={plaidBusy
                                   || (c.status === "reaped" && confirmFirst)}
                         accessibilityHint={c.status === "reaped"
                                            && confirmFirst
                           ? CONFIRM_TO_CONNECT : undefined}
                         onPress={() =>
                           // a reaped Item's token is gone — reconnect
                           // must be a FRESH link, not update mode
                           plaid(c.status === "reaped" ? "" : c.id,
                                 c.status === "new_accounts")}>
                <Text style={s.btnText}>
                  {c.status === "reaped" ? "reconnect ↗"
                    : c.status === "new_accounts" ? "share ↗" : "fix ↗"}
                </Text>
              </Pressable>
            </View>
          ))}
        </View>
        </SharedLock>
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
            Two sources for the same account are linked so nothing is
            counted twice.
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
              <Pressable accessibilityRole="button" style={[s.btn,
                           linkBusy(sg.key) && { opacity: 0.5 }]}
                         disabled={linkBusy(sg.key)}
                         onPress={() => linkAct.mutate({ id: sg.key,
                           drop: sg.key, fn: () =>
                           client!.accountLinkCreate([sg.a.id, sg.b.id]) })}>
                <Text style={s.btnText}>Link them</Text>
              </Pressable>
              <Pressable accessibilityRole="button" style={[s.btn, s.btnQuiet,
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

      {owner && replaced.length > 0 && (
        <Card>
          {replaced.map((c) => (
            <View key={c.id} style={{ flexDirection: "row", gap: 8,
                                      alignItems: "center", flexWrap: "wrap" }}>
              <Text style={{ color: C.text, fontSize: 13, fontWeight: "600" }}>
                {c.institution_name || c.id}</Text>
              <Text style={[s.mut, { flex: 1 }]}>{c.removed_text}</Text>
              <Pressable accessibilityRole="button"
                         disabled={forget.isPending || !!credentialLock}
                         onPress={() => forget.mutate(c.id)}>
                <Text style={{ color: C.accent, fontSize: 13,
                               opacity: credentialLock ? 0.55 : 1 }}>
                  forget</Text>
              </Pressable>
            </View>
          ))}
        </Card>
      )}

      {categories.map(({ cat, insts, total, counted }) => (
        <View key={cat}>
          {/* the web's category tier — a header row with its own total */}
          <View style={{ flexDirection: "row", alignItems: "baseline",
                         marginHorizontal: 12, marginTop: 10 }}>
            <Text style={{ color: C.mut, fontSize: 12, fontWeight: "700",
                           textTransform: "uppercase", flex: 1 }}>
              {cat}
              {counted ? (
                <Text style={{ textTransform: "none", fontWeight: "400" }}>
                  {` · ${counted}`}</Text>
              ) : null}
            </Text>
            {total !== null && (
              <Text style={{ color: C.text, fontSize: 14, fontWeight: "700",
                             fontVariant: ["tabular-nums"] }}>
                {money(total, false)}
              </Text>
            )}
          </View>
          {insts.map(({ key, inst, list, sub }) => {
            const itemId = list[0]?.item_id ?? null;
            const conn = itemId ? connById.get(itemId) : null;
            // real statuses look like "error:ITEM_LOGIN_REQUIRED" —
            // match the class, not an exact string
            const st0 = conn?.status ?? "ok";
            // older-server path: stands in for `status_kind`
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
            <Card key={`${cat}-${key}`}>
              <View style={{ flexDirection: "row", alignItems: "center",
                             gap: 8, flexWrap: "wrap" }}>
                <InstMark a={list[0]} />
                <H>{inst}</H>
                {/* the login's subtotal, as the web's institution row —
                    a login of one account shows its balance on its row
                    alone, as the web does */}
                {sub !== undefined && list.length > 1 && (
                  <Text style={{ color: C.text, fontSize: 14, fontWeight: "700",
                                 fontVariant: ["tabular-nums"],
                                 marginLeft: "auto" }}>
                    {money(sub, false)}
                  </Text>
                )}
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
                  <Pill text={removedConnectionNote(conn ?? {})}
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
                          <Pressable accessibilityRole="button"
                            style={[s.btn, { paddingVertical: 5 },
                                    (plaidBusy || !!credentialLock)
                                      && { opacity: 0.5 }]}
                            disabled={plaidBusy || !!credentialLock}
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
                {/* the window this bank was linked with, when it is less
                    than everything — fixed at Plaid, so a wider choice
                    made later only reaches it through a fresh link of
                    the same bank (what is in is kept) */}
                {!reaped && conn?.aggregator === "plaid"
                  && conn.history_months != null && (
                  <Text style={{ color: C.mut, fontSize: 11 }}>
                    {historyRowNote(conn.history_months)}</Text>
                )}
                <View style={{ flex: 1 }} />
                {owner && !broken && !reaped && conn?.aggregator === "plaid"
                  && windowNarrower(conn.history_months,
                                    settings.data?.history_months)
                  && relinking.has(itemId!) && (
                  <Text style={{ color: C.mut, fontSize: 11 }}>relinking…</Text>
                )}
                {owner && !broken && !reaped && conn?.aggregator === "plaid"
                  && windowNarrower(conn.history_months,
                                    settings.data?.history_months)
                  && !relinking.has(itemId!) && (
                  <Pressable accessibilityRole="button" style={[s.btn, { paddingVertical: 5 },
                               (plaidBusy || !!credentialLock)
                                 && { opacity: 0.5 }]}
                             disabled={plaidBusy || !!credentialLock}
                             onPress={() => Alert.alert(
                               "Pull more history?",
                               "You sign in at the bank again; what is "
                               + "already in is kept.",
                               [{ text: "Cancel", style: "cancel" },
                                { text: "Relink",
                                  onPress: () => plaid("", false, itemId!) }])}>
                    <Text style={s.btnText}>more history ↗</Text>
                  </Pressable>
                )}
                {owner && reaped && (
                  <Pressable accessibilityRole="button" style={[s.btn, { paddingVertical: 5 },
                               (plaidBusy || !!credentialLock)
                                 && { opacity: 0.5 }]}
                             disabled={plaidBusy || !!credentialLock}
                             onPress={() => plaid()}>
                    <Text style={s.btnText}>reconnect ↗</Text>
                  </Pressable>
                )}
                {owner && broken && conn!.aggregator === "plaid" && (
                  <Pressable accessibilityRole="button" style={[s.btn, { paddingVertical: 5 },
                               (plaidBusy || !!credentialLock)
                                 && { opacity: 0.5 }]}
                             disabled={plaidBusy || !!credentialLock}
                             onPress={() =>
                               // update mode offers newly opened accounts
                               // only with its account checklist on
                               plaid(itemId!, newAccounts)}>
                    <Text style={s.btnText}>
                      {newAccounts ? "share ↗" : "fix ↗"}
                    </Text>
                  </Pressable>
                )}
                {!viewer && conn && !reaped && (
                  <Text style={{ color: C.accent, fontSize: 14,
                                 padding: 4 }}
                        accessibilityRole="button" accessibilityLabel="Sync this connection"
                        onPress={() => !syncingItems.has(itemId!)
                          && syncItem.mutate(itemId!)}>
                    {syncingItems.has(itemId!) ? "…" : "↻"}
                  </Text>
                )}
              </View>
              {totals?.backups?.[key] ? (
                <Text style={[s.mut, { marginTop: 2 }]}>
                  {totals.backups[key]}</Text>) : null}
              {list.map((a) => (
                <AccountRow key={a.id} a={a} badge={badgeLabel(a)}
                  shown={shownBalance.get(a.id)}
                  dim={!!totals?.backups?.[key]}
                  primary={a.id === anchor}
                  pinnedUnused={a.id === primary && a.id !== anchor
                    ? (query.data?.primary_pin_unused ?? "") : undefined}
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

      {bizCategories.length > 0 && (
        <Card>
          <H>Business accounts</H>
          <Text style={s.mut}>
            {totals?.business_counted
              ? "In the Financial accounts total — you combine business and personal money"
              : "Not in the Financial accounts total"}; managed on{" "}
            <Text style={{ color: C.accent }}
                  onPress={() => router.push("/business" as never)}>
              Business ›
            </Text>
          </Text>
          {bizCategories.map(({ cat, insts, total, counted }) => (
            <View key={`biz-${cat}`}>
              {/* the web's category tier, as in the personal list */}
              <View style={{ flexDirection: "row", alignItems: "baseline",
                             marginTop: 10 }}>
                <Text style={{ color: C.mut, fontSize: 12, fontWeight: "700",
                               textTransform: "uppercase", flex: 1 }}>
                  {cat}
                  {counted ? (
                    <Text style={{ textTransform: "none", fontWeight: "400" }}>
                      {` · ${counted}`}</Text>
                  ) : null}
                </Text>
                {total !== null && (
                  <Text style={{ color: C.text, fontSize: 14, fontWeight: "700",
                                 fontVariant: ["tabular-nums"] }}>
                    {money(total, false)}
                  </Text>
                )}
              </View>
              {insts.map(({ key, inst, list, sub }) => (
                <View key={`biz-${cat}-${key}`}>
                  <View style={{ flexDirection: "row", alignItems: "center",
                                 gap: 6, marginTop: 6 }}>
                    <InstMark a={list[0]} size={16} />
                    <Text style={[s.mut, { fontWeight: "600", flex: 1 }]}>
                      {inst}</Text>
                    {sub !== undefined && list.length > 1 && (
                      <Text style={{ color: C.text, fontWeight: "600",
                                     fontVariant: ["tabular-nums"] }}>
                        {money(sub, false)}
                      </Text>
                    )}
                  </View>
                  {list.map((a) => (
                    <AccountRow key={a.id} a={a} shown={shownBalance.get(a.id)}
                      onPress={viewer ? undefined : () => openEditor(a)} />))}
                </View>
              ))}
            </View>
          ))}
        </Card>
      )}

      {((holdings.data?.holdings ?? []).length > 0
        || holdings.data?.hidden_text) && (
        <Card>
          <H>Top holdings{holdings.data?.as_of_text ? (
            <Text style={[s.mut, { fontWeight: "400" }]}>
              {" "}— {holdings.data.as_of_text}</Text>) : null}</H>
          {holdings.data?.hidden_text ? (
            <Text style={s.mut}>{holdings.data.hidden_text}</Text>) : null}
          {holdings.data!.holdings.slice(0, 20).map((h, i) => (
            <View key={`${h.acct}-${h.symbol}-${i}`}>
              <KV k={`${holdingSymbol(h.symbol)}${h.fund
                      ? ` · ${h.fund.slice(0, 30)}` : ""}`}
                  v={money(h.value, false)} tone="mut" />
              {/* the web table's Account / Qty / Price columns */}
              <Text style={[s.mut, { marginTop: -2 }]}>
                {h.acct}
                {/* the quantity and the pricing day as the server prints
                    them (the web's cells); an older server sends neither */}
                {h.qty_text ? ` · ${h.qty_text}` : ""}
                {h.price != null ? ` @ ${money(h.price)}` : ""}
                {h.priced_text ? ` · ${h.priced_text}` : ""}
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
          <Pressable onPress={() => setShowHidden((x) => !x)}
                     {...disclosure(showHidden, `Hidden (${hiddenList.length}) — `
                       + "excluded from every total; the connection is still "
                       + "live and the history is kept")}>
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
          <Pressable onPress={() => setShowArchived((x) => !x)}
                     {...disclosure(showArchived, `Archived (${archived.length}) — `
                       + "closed / historical, not syncing")}>
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

      {acctPhase === "failed" && (
        <FailedLine q={query} what="accounts" />
      )}
      {acctPhase === "empty" && (
        <Text style={s.center}>
          No accounts yet — connect your bank or add a manual account,
          then import files into it.
        </Text>
      )}

      <Modal visible={!!editing} animationType={sheetAnim} transparent
             onRequestClose={() => setEditing(null)}
             onDismiss={() => {
               const a = removeAfterEdit.current;
               removeAfterEdit.current = null;
               if (a) setRemoving(a);
             }}>
        {/* edge-to-edge Android does not resize for the keyboard: the
            avoiding view keeps the field being typed in above it */}
        <KeyboardAvoidingView style={s.sheetWrap} behavior="padding">
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
                            error={save.isError ? errText(save.error) : null}
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
                      <Pressable accessibilityRole="button" style={[s.btn, s.btnQuiet,
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
                    <Pressable accessibilityRole="button"
                               style={[s.btn, s.btnQuiet, busy && { opacity: 0.5 }]}
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
                {errText(setHidden.error)}
              </Text>
            ) : null}
            <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8,
                           justifyContent: "center" }}>
              {owner && (
              <Pressable accessibilityRole="button" style={[s.btn, s.btnQuiet,
                                 setHidden.isPending && { opacity: 0.5 }]}
                         disabled={setHidden.isPending}
                         onPress={confirmHide}>
                <Text style={[s.btnText, { color: C.warn }]}>
                  {editing?.user_removed_at ? "Unhide" : "Hide"}
                </Text>
              </Pressable>
              )}
              {owner && (
              <Pressable accessibilityRole="button" style={[s.btn, s.btnQuiet]}
                         onPress={() => {
                           const a = editing!;
                           const liveConn = !!a.item_id && isPullAgg(a)
                             && (a.status || "ok") !== "archived";
                           // both disconnect modes are refused the
                           // shared demonstration login, so its ladder
                           // starts on the one rung it may take
                           setRmMode(!liveConn ? "purge"
                             : credentialLock ? "hide" : "disconnect");
                           setEditing(null);
                           if (Platform.OS === "ios")
                             removeAfterEdit.current = a;
                           else setRemoving(a);
                         }}>
                <Text style={[s.btnText, { color: C.bad }]}>Remove…</Text>
              </Pressable>
              )}
              <Pressable accessibilityRole="button" style={[s.btn, s.btnQuiet]}
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
                <SharedLock note={credentialLock}>
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
                        } catch (e) { setNotice(errText(e)); }
                        setPlaidBusy(false);
                        qc.invalidateQueries({ queryKey: ["accounts"] });
                        qc.invalidateQueries({ queryKey: ["connections"] });
                        // the institutions-used figure rides /api/me
                        qc.invalidateQueries({ queryKey: ["me"] });
                      }}>
                  edit connected accounts ↗
                </Text>
                </SharedLock>
              )}
              {owner && (editing?.aggregator === "mx"
                         || editing?.aggregator === "simplefin-org")
                && editing.item_id && editing.status === "archived" && (
                // reconnecting restarts syncing (and its billing) for a
                // connection that outlives the shared demonstration login
                <SharedLock note={credentialLock}>
                <Text style={{ color: C.accent, fontSize: 12 }}
                      onPress={() => {
                        if (!reconnect.isPending) reconnect.mutate(editing!);
                      }}>
                  {reconnect.isPending ? "reconnecting…" : "reconnect"}
                </Text>
                </SharedLock>
              )}
            </View>
          </View>
        </KeyboardAvoidingView>
      </Modal>

      <Modal visible={!!removing} animationType={sheetAnim} transparent
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
                  {/* at the allowance, the likely reason to disconnect
                      is to make room — and a released bank keeps its
                      place for a while (the web panel says the same) */}
                  {liveConn && isPlaid && instFull && (
                    <Text style={[s.mut, { color: C.warn }]}>
                      Disconnecting does not free a slot for{" "}
                      {releasedHoldDays()} days.
                    </Text>
                  )}
                  {[opts.filter(([m]) => m.startsWith("disconnect")),
                    opts.filter(([m]) => !m.startsWith("disconnect"))]
                    .map((group, gi) => (
                  // disconnecting ends a bank connection (a Plaid release
                  // cannot be undone) that outlives the shared
                  // demonstration login, so the server refuses it both
                  // disconnect rungs; hiding one account stays open
                  <SharedLock key={gi}
                              note={gi === 0 && group.length
                                    ? credentialLock : null}>
                  {group.map(([mode, title, sub]) => (
                    <Pressable accessibilityRole="button" key={mode} style={{ flexDirection: "row",
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
                  </SharedLock>
                  ))}
                </>
              );
            })()}
            <View style={{ flexDirection: "row", gap: 8,
                           justifyContent: "center" }}>
              <Pressable accessibilityRole="button" style={[s.btn, { backgroundColor: C.danger },
                           remove.isPending && { opacity: 0.5 }]}
                         disabled={remove.isPending}
                         onPress={confirmRemove}>
                <Text style={[s.btnText, { color: C.onDanger }]}>
                  {remove.isPending ? "removing…" : "Confirm remove"}
                </Text>
              </Pressable>
              <Pressable accessibilityRole="button" style={[s.btn, s.btnQuiet]}
                         onPress={() => setRemoving(null)}>
                <Text style={[s.btnText, { color: C.mut }]}>Cancel</Text>
              </Pressable>
            </View>
          </View>
        </View>
      </Modal>

      <Modal visible={adding} animationType={sheetAnim} transparent
             onRequestClose={() => setAdding(false)}>
        <KeyboardAvoidingView style={s.sheetWrap} behavior="padding">
          <View style={s.sheet}>
            <Text style={s.sheetTitle}>Add a manual account</Text>
            {adding && (
              <AddFields pending={add.isPending}
                         error={add.isError ? errText(add.error) : null}
                         onAdd={(f) => add.mutate(f)}
                         onCancel={() => setAdding(false)} />
            )}
          </View>
        </KeyboardAvoidingView>
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

// primary / excl are null when the switch was left as the editor opened
// it, so a save never re-sends a household setting read from the cache
interface EditorForm { name: string; owner: string; kind: string;
                       primary: boolean | null; excl: boolean | null;
                       bal: string }

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
  // what the switches showed on open (the props can move under a refetch)
  const [seed] = useState({ primary, excluded });
  const offered = accountSwitches({
    can_pin: a.can_pin, can_exclude: a.can_exclude,
    pinned: seed.primary, excluded: seed.excluded });
  const [editBal, setEditBal] = useState(
    a.balance_current !== null ? String(a.balance_current) : "");
  return (
    <>
      <TextInput {...fieldA11y("Display name")} style={s.input}
                 placeholder={a.bank_name || "display name"}
                 placeholderTextColor={C.mut} value={eName}
                 onChangeText={setEName} />
      {!!a.bank_name && a.name !== a.bank_name && (
        <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
          <Text style={{ color: C.mut, fontSize: 12, flexShrink: 1 }}>
            bank name “{a.bank_name}”
          </Text>
          <Pressable accessibilityRole="button" onPress={() => setEName("")}
                     hitSlop={8}>
            <Text style={{ color: C.accent, fontSize: 12 }}>Revert</Text>
          </Pressable>
        </View>
      )}
      <TextInput {...fieldA11y("Owner (yours / mine / ours)")} style={s.input}
                 placeholder="owner (yours / mine / ours)"
                 placeholderTextColor={C.mut} value={eOwner}
                 onChangeText={setEOwner} />
      <View style={s.kindRow}>
        {(KINDS.some(([v]) => v === eKind)
          ? KINDS : [...KINDS, [eKind, kindWords(a.kind)]] as
            [string, string][]).map(([v, l]) => (
          <Pressable accessibilityRole="button" key={v}
                     style={[s.kind, eKind === v && s.kindOn]}
                     onPress={() => setEKind(v)}>
            <Text style={{ color: eKind === v ? C.text : C.mut,
                           fontSize: 12 }}>{l}</Text>
          </Pressable>
        ))}
      </View>
      {/* each switch only where the server says it applies
          (accountSwitches, as on the web): the forecast anchor on a bank
          account, the budget exclusion on any account whose rows can
          count; an older server says neither and both are offered */}
      {offered.primary && (
      <View style={s.toggleRow}>
        <View style={{ flex: 1 }}>
          <Text style={s.toggleLabel}>Primary checking</Text>
          <Text style={s.toggleSub}>
            anchor the cash forecast on this account
          </Text>
        </View>
        <Switch value={ePrimary}
                accessibilityLabel="Primary checking"
                onValueChange={setEPrimary}
                trackColor={{ false: C.hover, true: C.accent }}
                thumbColor={C.text} />
      </View>)}
      {offered.excluded && (
      <View style={s.toggleRow}>
        <View style={{ flex: 1 }}>
          <Text style={s.toggleLabel}>Exclude from budget</Text>
          <Text style={s.toggleSub}>
            its spending never counts toward the verdict
          </Text>
        </View>
        <Switch value={eExcl}
                accessibilityLabel="Exclude from budget"
                onValueChange={setEExcl}
                trackColor={{ false: C.hover, true: C.accent }}
                thumbColor={C.text} />
      </View>)}
      {/* not decimal-pad: it has no minus key on iOS (nor on Android
          without the signed flag), and a manual account can be overdrawn */}
      {manual && (
        <TextInput {...fieldA11y("Balance ($)")} style={s.input} placeholder="balance ($)"
                   placeholderTextColor={C.mut} value={editBal}
                   onChangeText={setEditBal}
                   keyboardType="numbers-and-punctuation" />
      )}
      {error ? (
        <Note bad style={{ color: C.bad, fontSize: 12 }}>{error}</Note>
      ) : null}
      <Pressable accessibilityRole="button" style={[s.btn, { alignSelf: "center" },
                         saving && { opacity: 0.5 }]}
                 disabled={saving}
                 onPress={() => onSave({ name: eName, owner: eOwner,
                   kind: eKind,
                   primary: ePrimary === seed.primary ? null : ePrimary,
                   excl: eExcl === seed.excluded ? null : eExcl,
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
      <TextInput {...fieldA11y("Account name")} style={s.input} placeholder="name (e.g. HSA)"
                 placeholderTextColor={C.mut} value={newName}
                 onChangeText={setNewName} />
      <View style={s.kindRow}>
        {KINDS.map(([v, l]) => (
          <Pressable accessibilityRole="button" key={v}
                     style={[s.kind, newKind === v && s.kindOn]}
                     onPress={() => setNewKind(v)}>
            <Text style={{ color: newKind === v ? C.text : C.mut,
                           fontSize: 12 }}>{l}</Text>
          </Pressable>
        ))}
      </View>
      <TextInput {...fieldA11y("Starting balance ($)")}
                 style={s.input} placeholder="starting balance ($)"
                 placeholderTextColor={C.mut} value={newBal}
                 onChangeText={setNewBal}
                 keyboardType="numbers-and-punctuation" />
      {error ? (
        <Note bad style={{ color: C.bad, fontSize: 12 }}>{error}</Note>
      ) : null}
      <View style={{ flexDirection: "row", gap: 8,
                     justifyContent: "center" }}>
        <Pressable accessibilityRole="button" style={[s.btn, (!newName.trim() || pending)
                            && { opacity: 0.5 }]}
                   disabled={!newName.trim() || pending}
                   onPress={() => onAdd({ name: newName, kind: newKind,
                                          bal: newBal })}>
          <Text style={s.btnText}>Add</Text>
        </Pressable>
        <Pressable accessibilityRole="button" style={[s.btn, s.btnQuiet]} onPress={onCancel}>
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
      <TextInput {...fieldA11y("SimpleFIN setup token")} style={[s.input, { flex: 1 }]}
                 placeholder="SimpleFIN setup token"
                 placeholderTextColor={C.mut} autoCapitalize="none"
                 value={sfToken} onChangeText={setSfToken} />
      <Pressable accessibilityRole="button" style={[s.btn,
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
  input: { backgroundColor: C.bg, borderColor: C.field, borderWidth: 1,
           borderRadius: 8, color: C.text, fontSize: 15,
           paddingHorizontal: 10, paddingVertical: 8 },
  kindRow: { flexDirection: "row", flexWrap: "wrap", gap: 6 },
  toggleRow: { flexDirection: "row", alignItems: "center", gap: 10 },
  toggleLabel: { color: C.text, fontSize: 14, fontWeight: "600" },
  toggleSub: { color: C.mut, fontSize: 11 },
  kind: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
          borderRadius: 999, paddingHorizontal: 10, paddingVertical: 5 },
  kindOn: { borderColor: C.accent, backgroundColor: C.hover },
  btn: { backgroundColor: C.btn, borderRadius: 8,
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
