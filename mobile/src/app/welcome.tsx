// The guided first-run setup, native — the web's Welcome wizard.
//
// A household signs up on whichever device is in hand, so both clients
// have to open the same door. Without a native wizard the phone meets a
// new account with empty pages and no route through them.
//
// Same steps, same order, same words as the web (Connect → Bills →
// Budgets → Email on self-host → Finish), and — the part that matters —
// the same marks in tenant config under `wizard_steps`. Start on the
// phone, finish in a browser, or the reverse: each client resumes at the
// first step the other left unfinished.
//
// One deliberate difference from the web, which embeds Connect/Bills/
// Budgets INSIDE the wizard pane. Those are full screens here, so each
// step explains itself and hands the user to the real screen; coming
// back lands on the same step with its work visible in the counts. A
// phone has no room for a page inside a page, and the native screens are
// already the best version of each job.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "expo-router";
import { useEffect, useRef, useState } from "react";
import { Alert, Animated, Easing, Pressable, ScrollView, StyleSheet, Text,
         View } from "react-native";
import PullRefresh from "../components/pull-refresh";

import StaleBanner from "../components/stale-banner";
import { Card, Pill } from "../components/ui";
import type { BillsData, Proposal } from "../lib/api";
import { patchQuery, removeFromList, saveSettings } from "../lib/cache";
import { type Delivery, deliveryOf, flagsOf, institutionAllowance } from "../lib/pure";
import { registerNativePush } from "../lib/push";
import { ext } from "../ext";
import { useSession } from "../lib/session";
import { C, mmddyy, money } from "../lib/theme";
import { useViewer } from "../lib/viewer";
import { markLeftWizard } from "../lib/storage";
import { plaidHostedLink } from "../lib/plaid";
import { ago } from "../lib/dates";
import { Planner } from "./budget";
import { errText } from "../lib/api";

type StepKey = "connect" | "bills" | "budgets" | "email" | "finish";

// self-hosted gets the optional SMTP step; hosted deploys send through
// the operator's mail account. MUST mirror the web's stepKeys().
const stepKeys = (hosted: boolean): StepKey[] => hosted
  ? ["connect", "bills", "budgets", "finish"]
  : ["connect", "bills", "budgets", "email", "finish"];

export default function Welcome() {
  const { client } = useSession();
  const router = useRouter();
  const qc = useQueryClient();
  // poll like the web: counts move while the user is off in Connect or
  // Bills, and the step's own primary button unlocks off them
  const ob = useQuery({ queryKey: ["onboarding"],
    queryFn: () => client!.onboarding(), enabled: !!client,
    refetchInterval: 5000 });
  const me = useQuery({ queryKey: ["me"], queryFn: () => client!.me(),
                        enabled: !!client });
  // The left-the-wizard flag is keyed by TENANT, because that is how
  // Today reads it (index.tsx), so wait for the id rather than fall back
  // to a key Today never looks at.
  const tenantId = async () =>
    me.data?.tenant_id ?? (await client!.me()).tenant_id;
  // The walk starts at "connect a bank" and ends at the instance's mail
  // settings, both of which are the owner's, so a member is turned away
  // here exactly as a viewer is. Their editing lives on the money
  // screens, not in setup.
  // Derived from me.DATA, never from a loading query: useOwner() reads
  // false while ["me"] is in flight, so a bare `!useOwner()` would flip
  // viewer true->false mid-mount. With the viewer early-return sitting
  // above six hooks, that flip would change the hook count between
  // renders, which React does not allow.
  const viewer = !!me.data
    && (me.data as { role?: string }).role !== "owner";
  const settings = useQuery({ queryKey: ["settings"],
    queryFn: () => client!.settingsFull(), enabled: !!client });

  const d = ob.data;
  const hosted = !!(me.data as { hosted?: boolean } | undefined)?.hosted;
  const keys = stepKeys(hosted);
  // how many of the plan's institutions are spent — both figures from the
  // server, never a constant here: the cap has a per-tenant override an
  // operator can raise
  const instCap = me.data?.features?.institution_cap;
  const instUsed = me.data?.features?.institutions_used;
  const { known: instCapKnown, full: instFull } =
    institutionAllowance(instCap, instUsed);
  // step 2's doorman: poll the backfill while any connection is still
  // pulling its history; an endpoint error fails open (never strand)
  const backfill = useQuery({ queryKey: ["onboarding-backfill"],
    queryFn: () => client!.onboardingBackfill(), enabled: !!client,
    refetchInterval: (q) => q.state.data?.all_ready ? false : 4000 });
  // hold the finished bars on screen for a moment before the step's body
  // replaces them (the web's rule: the fill reaching the end IS the
  // "done" signal, and nobody saw it when the view swapped on the same
  // poll)
  const [reveal, setReveal] = useState(false);
  const finished = !!backfill.data && backfill.data.all_ready
    && backfill.data.items.length > 0;
  useEffect(() => {
    if (!finished) return;
    const t = setTimeout(() => setReveal(true), 1800);
    return () => clearTimeout(t);
  }, [finished]);
  const backfillReady = backfill.isError
    || !!(backfill.data && backfill.data.all_ready && (reveal || !finished));
  // the poll is read-only; kicking a quiet backfill is an owner-only
  // POST — once when the wizard opens and then every 30 s while the
  // wait lasts (the web's rule: the link-time pull is too recent for the
  // first nudge to start another, and without a webhook nothing else
  // would; the server declines when a pull is recent or running, so the
  // repeat is free while history is flowing). A viewer's 403 is ignored.
  const backfillWaiting = !!backfill.data && !backfill.data.all_ready;
  useEffect(() => {
    if (!client) return;
    client.onboardingBackfillNudge().catch(() => {});
    if (!backfillWaiting) return;
    const t = setInterval(() => {
      client.onboardingBackfillNudge().catch(() => {});
    }, 30_000);
    return () => clearInterval(t);
  }, [client, backfillWaiting]);
  // the step marks ride on /api/onboarding (the web's source), NOT on
  // /api/settings — the settings view does not carry wizard_steps, so
  // reading them there gives {} every time and the wizard resumes at
  // step 1 on every open, however far the person had got
  const marks = d?.wizard_steps || {};

  const [i, setI] = useState<number | null>(null);
  const [touched, setTouched] = useState(false);
  // remount the inline planner after a save so it re-seeds from what
  // was saved (the Budget page's own rule for its planner)
  const [plannerGen, setPlannerGen] = useState(0);
  // resume at the first step with no done/skipped mark — the web's rule,
  // and why a wizard started in a browser continues correctly here
  // …and not from a stale settings snapshot: a remount right after a
  // step is marked done (its save is what invalidates settings) must not
  // read the pre-save marks and resume at the step just finished
  useEffect(() => {
    if (touched || i !== null || !d || ob.isFetching || me.isPending)
      return;
    const first = keys.findIndex((k) => !marks[k]);
    setI(first === -1 ? keys.length - 1 : first);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [d, i, touched, ob.isFetching, me.isPending]);

  // the reply seeds ["settings"]; the step marks live on /api/onboarding
  const mark = useMutation({
    mutationFn: (m: { key: StepKey; state: "done" | "skipped" }) =>
      saveSettings(qc, client!, { wizard_steps: { [m.key]: m.state } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["onboarding"] }),
  });
  // "Go to Today" goes to Today NOW: the local left-the-wizard flag is
  // what gates Today's redirect back in, so nothing waits on the round
  // trip — the save lands behind the navigation
  const finish = useMutation({
    mutationFn: () => saveSettings(qc, client!,
      { wizard_done: true, wizard_steps: { finish: "done" } }),
    onMutate: async () => {
      // per tenant, like the web (and Today's read) — see index.tsx
      await markLeftWizard(await tenantId());
      router.replace("/" as never);
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["onboarding"] }),
    onError: (e) => Alert.alert("Couldn't finish setup", errText(e)),
  });

  const idx = i ?? 0;
  const advance = (state: "done" | "skipped") => {
    setTouched(true);
    mark.mutate({ key: keys[idx], state });
    setI(Math.min(idx + 1, keys.length - 1));
  };

  const connected = (d?.connections ?? 0) > 0 || (d?.transactions ?? 0) > 0;
  // Hosted: Plaid is included and already wired, so step 1 links a bank
  // right here — the connect hub (Plaid vs MX vs SimpleFIN vs scripts vs
  // files) is a self-host menu and a hosted user should never see it.
  // Same as the web's hosted ConnectHub: one button, the linked banks
  // listed under it.
  const conns = useQuery({ queryKey: ["connections"],
    queryFn: () => client!.connections(),
    enabled: !!client && hosted && !viewer,
    // Plaid Link finishes in the in-app browser — the new bank must
    // appear without a manual refresh (the accounts tab's rule); and a
    // persisted list from before a reset must not outlive the mount.
    // The poll only runs on the connect step — the list is shown nowhere
    // else, and the wizard stays mounted under every screen it opens
    refetchOnMount: "always",
    refetchInterval: keys[idx] === "connect" ? 5000 : false });
  const [linkMsg, setLinkMsg] = useState<string | null>(null);
  const link = useMutation({
    mutationFn: () => plaidHostedLink(client!, ""),
    onSuccess: (r) => {
      setLinkMsg(r.kind === "add" ? null
        : r.kind === "exited" || r.kind === "cancelled"
          ? "No bank was linked — the sign-in window was closed."
          : String(r.error ?? "link failed"));
      if (r.kind === "add") {
        qc.invalidateQueries({ queryKey: ["connections"] });
        qc.invalidateQueries({ queryKey: ["onboarding"] });
        // a new bank spends one of the plan's institutions, and the
        // allowance this step renders (used of cap) rides /api/me
        qc.invalidateQueries({ queryKey: ["me"] });
        // the step-2 wait read the backfill before this bank existed
        // (no connections → all_ready, poll off); a new one changes that
        qc.invalidateQueries({ queryKey: ["onboarding-backfill"] });
      }
    },
    onError: (e) => setLinkMsg(String(e)),
  });
  const disconnect = useMutation({
    mutationFn: (id: string) => client!.connectionDisconnect(id),
    onSuccess: (_r, id) => {
      // the row goes now; the accounts it fed and Today's balances move
      removeFromList<{ connections: { id: string }[] }, { id: string }>(
        qc, ["connections"], "connections", (c) => c.id === id);
      qc.invalidateQueries({ queryKey: ["connections"] });
      qc.invalidateQueries({ queryKey: ["onboarding"] });
      qc.invalidateQueries({ queryKey: ["accounts"] });
      qc.invalidateQueries({ queryKey: ["today"] });
      // …and the freed slot: the allowance above this list is /api/me's,
      // so without this the step still reads as full and keeps its
      // Connect button disabled after the disconnect that freed one
      qc.invalidateQueries({ queryKey: ["me"] });
    },
    onError: (e) => setLinkMsg(String(e)),
  });
  // Step 2 runs the finder itself once the history is in — the web's
  // autoDetect. Without it the step says "the finder already scanned"
  // over a Bills tab that has never scanned, and the person has to find
  // the button. Once per visit, and only after the bills
  // data has loaded so the refetch that follows is not folded into a
  // still-in-flight first load.
  const detectRan = useRef(false);
  const detect = useMutation({
    mutationFn: () => client!.billsDetect(),
    onSuccess: async () => {
      // cancel BEFORE invalidating: a first fetch of ["bills"] still in
      // flight (the tab's prefetch, started as the step opened) is not
      // cancelled by invalidate — react-query keeps a first fetch and
      // folds the refetch into it — so the step would show the pre-scan
      // snapshot, "no proposals" over the pending ones
      await qc.cancelQueries({ queryKey: ["bills"] });
      await qc.invalidateQueries({ queryKey: ["bills"] });
      qc.invalidateQueries({ queryKey: ["onboarding"] });
    },
  });
  const steps: {
    key: StepKey; title: string; blurb: string;
    go?: { label: string; to: string } | null;
    primary: { label: string; disabled?: boolean; hint?: string } | null;
    skippable: boolean;
  }[] = [
    { key: "connect", title: "Connect your accounts",
      // no "as many as you like" on hosted: an institution cap applies and
      // the add door enforces it. The live figure is rendered beside the
      // button below, from the server (an operator can raise a household's
      // cap, so no number belongs in this copy).
      blurb: hosted
        ? "Pick your bank and sign in — that's it. Transactions, balances "
          + "and cards flow in automatically. Add the ones you use, then "
          + "say so below."
        : "Link every bank you use. When you've added everything you "
          + "plan to, say so below. (File imports alone work completely "
          + "too.)",
      go: hosted ? null
        : { label: connected ? "Connect another account" : "Connect an account",
            to: "/connect-hub" },
      primary: { label: "Done connecting →", disabled: !connected,
                 hint: connected ? undefined
                   : "connect at least one source, or skip" },
      skippable: true },
    { key: "bills", title: "Confirm bills & income",
      blurb: "The finder already scanned your ledger for recurring bills "
        + "and paychecks. Approve what's right, dismiss what isn't — "
        + "approved (and still-pending) income seeds the next step's "
        + "budget. Continue any time and revisit later.",
      // the door only opens once the history is in — see BackfillGate,
      // the same wait the web's step 2 makes; the proposals themselves
      // are inline (ProposalsInline), as on the web
      go: null,
      primary: backfillReady ? { label: "Continue →" } : null,
      skippable: backfillReady },
    { key: "budgets", title: "Set your budgets",
      blurb: "Your monthly plan, pre-filled from your real data: income "
        + "and bills from the last step, spending budgets from your "
        + "history, and what's left as savings. The daily verdict only "
        + "judges the variable spending.",
      // the planner is inline (the web's step 3), not a detour to the
      // Budget page — same Planner the page uses
      go: null,
      primary: { label: "Continue →", disabled: !d?.budgets_set,
                 hint: d?.budgets_set ? undefined : "save budgets first" },
      skippable: true },
    ...(hosted ? [] : [{
      key: "email" as StepKey, title: "Daily email delivery",
      blurb: "Optional: give Oikonome an outbound email (SMTP) account so "
        + "the daily verdict and alerts can reach your inbox. Settings → "
        + "Email has the provider guides.",
      go: { label: "Open email settings", to: "/settings" },
      primary: { label: "Continue →" } as
        { label: string; disabled?: boolean; hint?: string } | null,
      skippable: true,
    }]),
    { key: "finish", title: "Done",
      blurb: "That's everything. The Today page carries your daily "
        + "verdict from here on.",
      primary: null, skippable: false },
  ];
  const step = steps[idx];
  const last = idx === steps.length - 1;
  // arriving at the bills step re-reads the backfill: the query may have
  // settled (all_ready, poll off) before the bank was linked in the hub
  useEffect(() => {
    if (step.key === "bills") backfill.refetch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step.key]);
  useEffect(() => {
    if (step.key === "bills" && backfillReady && !detectRan.current
        && client && !viewer) {
      detectRan.current = true;
      detect.mutate();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step.key, backfillReady, client]);

  // the wizard is nothing but writes, so a viewer gets the same note the
  // web gives rather than a form that 403s on every button. This return
  // sits BELOW every hook on purpose — see the viewer note above.
  if (viewer)
    return (
      <ScrollView style={s.wrap}>
        <Card>
          <Text style={s.h1}>Setup</Text>
          <Text style={s.mut}>View-only access — the instance owner runs
            the guided setup.</Text>
        </Card>
      </ScrollView>
    );

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 28 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => ob.refetch()} />}>
      <StaleBanner query={ob} />
      <Card>
        <Text style={s.mut}>Step {idx + 1} of {steps.length}</Text>
        {/* passive progress dots — position, not navigation, exactly as
            the web's row of dots reports it */}
        <View style={{ flexDirection: "row", gap: 6, marginVertical: 8 }}>
          {steps.map((x, n) => (
            <View key={x.key}
                  style={{ width: 10, height: 10, borderRadius: 5,
                           backgroundColor: n === idx ? C.warn
                             : marks[x.key] ? C.good : C.border }} />
          ))}
        </View>
        <Text style={s.h1}>{step.title}</Text>
        <Text style={s.mut}>{step.blurb}</Text>

        {step.key === "bills" && !backfillReady && backfill.data && (
          <BackfillGate items={backfill.data.items} />
        )}

        {step.key === "connect" && hosted && (
          <View style={{ marginTop: 10, gap: 8 }}>
            {instCapKnown && (
              <Text style={s.mut}>
                {ext.allowanceNote(instUsed, instCap, instFull)}
              </Text>
            )}
            <Pressable style={[s.btn, (link.isPending || instFull)
                                      && { opacity: 0.5 }]}
                       disabled={link.isPending || instFull}
                       onPress={() => link.mutate()}>
              <Text style={s.btnText}>
                {link.isPending ? "Finish signing in at your bank…"
                                : (conns.data?.connections ?? []).length
                                  ? "+ Connect another account ↗"
                                  : "+ Connect an account ↗"}
              </Text>
            </Pressable>
            <Text style={s.mut}>Opens your bank's sign-in (Plaid). When
              you finish, the connection appears below so you can link
              another or continue.</Text>
            {linkMsg && <Text style={[s.mut, { color: C.warn }]}>{linkMsg}</Text>}
            {(conns.data?.connections ?? []).length > 0 && (
              <LinkedBanks conns={conns.data!.connections}
                           onDisconnect={(id) => disconnect.mutate(id)}
                           busyId={disconnect.isPending
                             ? disconnect.variables ?? null : null} />
            )}
          </View>
        )}

        {step.key === "bills" && backfillReady && detect.isPending && (
          <Text style={[s.mut, { marginTop: 8 }]}>
            Scanning your history for paychecks and recurring bills…</Text>
        )}
        {step.key === "bills" && backfillReady && !detect.isPending && (
          <ProposalsInline />
        )}

        {step.key === "budgets" && settings.data && (
          <Planner key={`wiz-p${plannerGen}`} s0={settings.data}
                   viewer={viewer}
                   onSaved={async () => {
                     // Continue unlocks now; the refetch only confirms it
                     patchQuery<{ budgets_set: boolean }>(qc, ["onboarding"],
                       { budgets_set: true });
                     await qc.invalidateQueries({ queryKey: ["settings"] });
                     qc.invalidateQueries({ queryKey: ["onboarding"] });
                     setPlannerGen((g) => g + 1);
                   }} />
        )}

        {step.go && (
          <Pressable style={s.btn}
                     onPress={() => router.push(step.go!.to as never)}>
            <Text style={s.btnText}>{step.go.label}</Text>
          </Pressable>
        )}

        {last && <FinishStep d={d} />}

        {!!step.primary?.hint && (
          <Text style={[s.mut, { color: C.warn }]}>{step.primary.hint}</Text>
        )}

        {/* Skip · Back · Continue, the web's order */}
        <View style={{ flexDirection: "row", alignItems: "center",
                       gap: 12, marginTop: 16, flexWrap: "wrap" }}>
          {step.skippable && (
            <Pressable onPress={() => advance("skipped")}>
              <Text style={s.link}>Skip this step</Text>
            </Pressable>
          )}
          <View style={{ flex: 1 }} />
          {idx > 0 && (
            <Pressable onPress={() => { setTouched(true); setI(idx - 1); }}>
              <Text style={s.link}>← Back</Text>
            </Pressable>
          )}
          {step.primary && (
            <Pressable
              style={[s.btn, { marginTop: 0 },
                      step.primary.disabled && { opacity: 0.5 }]}
              disabled={step.primary.disabled}
              onPress={() => advance("done")}>
              <Text style={s.btnText}>{step.primary.label}</Text>
            </Pressable>
          )}
          {last && (
            <Pressable style={[s.btn, { marginTop: 0 },
                               finish.isPending && { opacity: 0.5 }]}
                       disabled={finish.isPending}
                       onPress={() => finish.mutate()}>
              <Text style={s.btnText}>Go to Today →</Text>
            </Pressable>
          )}
        </View>

        <View style={{ flexDirection: "row", justifyContent: "flex-end",
                       gap: 14, marginTop: 12 }}>
          <Pressable onPress={async () => {
                       // records the decision so Today stops redirecting
                       // back into the wizard — the web keeps the same flag
                       await markLeftWizard(await tenantId());
                       router.replace("/" as never);
                     }}>
            <Text style={s.link}>save &amp; finish later</Text>
          </Pressable>
          <Pressable disabled={finish.isPending}
                     onPress={() => finish.mutate()}>
            <Text style={s.link}>don't show this again</Text>
          </Pressable>
        </View>
      </Card>
    </ScrollView>
  );
}

/** The web's linked-bank rows: one row per bank — status dot, name,
 *  accounts · last pull, Disconnect. */
function LinkedBanks({ conns, onDisconnect, busyId }: {
  conns: { id: string; institution_name: string | null; status: string | null;
           last_ok: string | null; accounts?: number;
           bank_updated_at?: string | null }[];
  // the one row whose disconnect is in flight — never the whole list
  onDisconnect: (id: string) => void; busyId: string | null }) {
  const total = conns.reduce((t, c) => t + (c.accounts ?? 0), 0);
  return (
    <View style={{ marginTop: 6 }}>
      <View style={{ flexDirection: "row", alignItems: "center", gap: 8,
                     marginBottom: 6 }}>
        <Text style={{ color: C.text, fontSize: 14, fontWeight: "700" }}>
          Connected</Text>
        <Pill text={`${conns.length} ${conns.length === 1 ? "bank" : "banks"}`
                    + (total ? ` · ${total} accounts` : "")} tone="good" />
      </View>
      <View style={{ borderWidth: StyleSheet.hairlineWidth,
                     borderColor: C.border, borderRadius: 8,
                     overflow: "hidden" }}>
        {conns.map((c, i) => {
          const bad = (c.status || "").startsWith("error")
            || c.status === "reaped";
          return (
            <View key={c.id}
                  style={{ flexDirection: "row", alignItems: "center", gap: 10,
                           paddingVertical: 8, paddingHorizontal: 10,
                           borderTopWidth: i ? StyleSheet.hairlineWidth : 0,
                           borderTopColor: C.border }}>
              <View style={{ width: 9, height: 9, borderRadius: 5,
                             backgroundColor: bad ? C.bad : C.good }} />
              <View style={{ flex: 1 }}>
                <Text style={{ color: C.text, fontSize: 14, fontWeight: "600" }}
                      numberOfLines={1}>{c.institution_name || c.id}</Text>
                <Text style={{ color: C.mut, fontSize: 12 }}>
                  {typeof c.accounts === "number"
                    ? `${c.accounts} ${c.accounts === 1 ? "account" : "accounts"}`
                    : ""}
                  {bad ? " · needs attention"
                    : c.last_ok ? ` · synced ${ago(c.last_ok)}`
                    : " · first sync pending"}
                  {/* the bank's clock, not ours: sync reads Plaid's copy,
                      and the bank refreshes that copy on its own
                      schedule */}
                  {!bad && c.bank_updated_at
                    ? ` · bank sent data ${ago(c.bank_updated_at)}` : ""}
                </Text>
              </View>
              <Pressable disabled={busyId === c.id}
                         onPress={() => Alert.alert(
                           `Disconnect ${c.institution_name || c.id}?`,
                           "It stops syncing — history is kept.",
                           [{ text: "Cancel", style: "cancel" },
                            { text: "Disconnect", style: "destructive",
                              onPress: () => onDisconnect(c.id) }])}
                         style={{ borderWidth: StyleSheet.hairlineWidth,
                                  borderColor: C.border, borderRadius: 8,
                                  paddingHorizontal: 10, paddingVertical: 4 }}>
                <Text style={{ color: C.mut, fontSize: 12 }}>Disconnect</Text>
              </Pressable>
            </View>
          );
        })}
      </View>
    </View>
  );
}

/** Step 2's body, the web's Proposed-changes table: every pending
 *  proposal with approve / reject per row and Approve all above them,
 *  right in the step — no detour through the Bills tab. Once nothing is
 *  pending it says what is tracked. */
function ProposalsInline() {
  const { client } = useSession();
  const qc = useQueryClient();
  const viewer = useViewer();
  const q = useQuery({ queryKey: ["bills"],
    queryFn: () => client!.bills(), enabled: !!client,
    // mounted right after the scan: never trust a cached list here
    refetchOnMount: "always" });
  const [notice, setNotice] = useState<string | null>(null);
  // a decided proposal leaves the list NOW (the server has committed
  // it). Approving writes a bill — the counts, calendar and Today move;
  // rejecting writes none, so only the list, the counts and the step-3
  // budgets prefill (which reads PENDING income proposals) can change.
  const refetch = (wroteBill: boolean) => {
    qc.invalidateQueries({ queryKey: ["bills"] });
    qc.invalidateQueries({ queryKey: ["onboarding"] });
    qc.invalidateQueries({ queryKey: ["budgets-suggest"] });
    if (!wroteBill) return;
    qc.invalidateQueries({ queryKey: ["calendar"] });
    qc.invalidateQueries({ queryKey: ["today"] });
  };
  const act = useMutation({
    mutationFn: ({ pid, action }:
        { pid: string; action: "approve" | "reject" }) =>
      client!.billsProposal(pid, action),
    onSuccess: (r, v) => {
      setNotice(`${v.action === "approve" ? "Approved" : "Rejected"}: ${r.payee}`);
      removeFromList<BillsData, Proposal>(qc, ["bills"], "proposals",
        (p) => p.id === v.pid);
      refetch(v.action === "approve");
    },
    onError: (e) => setNotice(String(e)),
  });
  const approveAll = useMutation({
    mutationFn: () => client!.billsApproveAll(),
    onSuccess: (r) => {
      setNotice(r.failed.length
        ? `Approved ${r.approved}; ${r.failed.length} stayed pending.`
        : `Approved all ${r.approved} proposal${r.approved === 1 ? "" : "s"}.`);
      // only what stayed pending is still a proposal
      qc.setQueryData<BillsData>(["bills"], (old) => old && {
        ...old,
        proposals: old.proposals.filter(
          (p) => r.failed.some((f) => f.pid === p.id)),
      });
      refetch(true);
    },
    onError: (e) => setNotice(String(e)),
  });
  if (!q.data) return <Text style={s.mut}>Loading proposals…</Text>;
  const props = q.data.proposals;
  const tracked = q.data.bills.length;
  return (
    <View style={{ marginTop: 10 }}>
      {notice ? <Text style={[s.mut, { color: C.good }]}>{notice}</Text> : null}
      {props.length === 0 ? (
        <Text style={s.mut}>
          {tracked > 0
            ? `${tracked} recurring bill${tracked === 1 ? "" : "s"} & `
              + "paychecks tracked — nothing left to review."
            : "No proposals — the finder found no recurring pattern yet. "
              + "Continue; it runs again as history grows."}
        </Text>
      ) : (
        <>
          <View style={{ flexDirection: "row", alignItems: "center",
                         justifyContent: "space-between", gap: 8 }}>
            <Text style={s.h2}>Proposed changes</Text>
            {!viewer && props.length > 1 && (
              <Pressable style={p.btn}
                         disabled={approveAll.isPending || act.isPending}
                         onPress={() => approveAll.mutate()}>
                <Text style={p.btnText}>
                  {approveAll.isPending ? "Approving…"
                    : `Approve all ${props.length}`}
                </Text>
              </Pressable>
            )}
          </View>
          {props.map((x) => {
            // this row's own decision in flight — never the whole list
            const busy = act.isPending && act.variables?.pid === x.id;
            return (
            <View key={x.id} style={p.row}>
              <View style={{ flex: 1 }}>
                <View style={{ flexDirection: "row", alignItems: "center",
                               gap: 6, flexWrap: "wrap" }}>
                  <Text style={{ color: C.text, fontSize: 14,
                                 fontWeight: "600", flexShrink: 1 }}
                        numberOfLines={1}>{x.payee}</Text>
                  {x.kind ? (
                    <Pill text={x.kind}
                          tone={x.kind === "remove" ? "bad"
                            : x.kind === "add" ? "good" : "warn"} />
                  ) : null}
                  {x.income ? <Pill text="income" tone="good" /> : null}
                  {x.bill_type === "envelope" ? <Pill text="envelope" /> : null}
                </View>
                <Text style={s.mut}>
                  {money(x.amount)} · {x.cadence}
                  {x.next_due ? ` · next ${mmddyy(x.next_due)}` : ""}
                  {x.summary ? ` · ${x.summary}` : ""}
                </Text>
              </View>
              {!viewer && (<>
                <Pressable style={[p.btn, busy && { opacity: 0.5 }]}
                           disabled={busy}
                           onPress={() => act.mutate({ pid: x.id,
                                                       action: "approve" })}>
                  <Text style={p.btnText}>Approve</Text>
                </Pressable>
                <Pressable style={[p.btn, p.quiet, busy && { opacity: 0.5 }]}
                           disabled={busy}
                           onPress={() => act.mutate({ pid: x.id,
                                                       action: "reject" })}>
                  <Text style={[p.btnText, { color: C.mut }]}>Reject</Text>
                </Pressable>
              </>)}
            </View>
            );
          })}
        </>
      )}
    </View>
  );
}

const p = StyleSheet.create({
  row: { flexDirection: "row", alignItems: "center", gap: 8,
         borderTopColor: C.border, borderTopWidth: StyleSheet.hairlineWidth,
         paddingVertical: 8, marginTop: 4 },
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 12, paddingVertical: 7 },
  quiet: { backgroundColor: C.hover },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
});

/** The web's import bar: a determinate fill (how much of the two-year
 *  window has landed) with a sweep running over it while the bank is
 *  still sending, so an unmoving fill still reads as working, not hung.
 *  Full and still only once the bank says the window is complete. */
function BackfillBar({ frac, live, tall, muted }: {
  frac: number; live: boolean; tall?: boolean; muted?: boolean }) {
  const pct = Math.max(0, Math.min(1, frac));
  const sweep = useRef(new Animated.Value(0)).current;
  useEffect(() => {
    if (!live) return;
    const loop = Animated.loop(Animated.timing(sweep, {
      toValue: 1, duration: 1600, easing: Easing.inOut(Easing.ease),
      useNativeDriver: true }));
    loop.start();
    return () => loop.stop();
  }, [live, sweep]);
  const [w, setW] = useState(0);
  const tx = sweep.interpolate({ inputRange: [0, 1],
                                 outputRange: [-w, w] });
  return (
    <View style={{ height: tall ? 10 : 6, borderRadius: tall ? 5 : 3,
                   backgroundColor: C.hover, overflow: "hidden",
                   marginTop: tall ? 8 : 4, marginBottom: tall ? 4 : 0 }}
          accessibilityRole="progressbar"
          accessibilityValue={{ min: 0, max: 100, now: Math.round(pct * 100) }}>
      <View onLayout={(e) => setW(e.nativeEvent.layout.width)}
            style={{ height: "100%", width: `${pct * 100}%`,
                     borderRadius: tall ? 5 : 3, overflow: "hidden",
                     backgroundColor: muted ? C.warn : C.accent,
                     opacity: muted ? 0.6 : 1 }}>
        {live && (
          <Animated.View style={{ position: "absolute", top: 0, bottom: 0,
                                  left: 0, right: 0,
                                  backgroundColor: "rgba(255,255,255,0.35)",
                                  transform: [{ translateX: tx }] }} />
        )}
      </View>
    </View>
  );
}

/** Step-2 doorman, the web's BackfillGate. Income/bill detection is only
 *  as good as the history it scans, and the bank sends its two-year
 *  window minutes AFTER the link. So the step waits, visibly, and shows
 *  the one honest progress signal there is — how far back each
 *  connection has reached. No "scan anyway" escape: the budget the next
 *  step pre-fills is derived from this history, and skipping ahead
 *  produces a wrong budget, not a faster one. Broken
 *  connections are shown as needs-attention and don't hold the step. */
function BackfillGate({ items }: { items: {
  id: string; institution: string | null; transactions: number;
  earliest: string | null; progress: number; active: boolean;
  ready: boolean; stalled: boolean }[] }) {
  const done = items.filter((it) => it.ready).length;
  const monthYear = (iso: string) =>
    new Date(iso + "T00:00:00").toLocaleDateString(undefined,
      { month: "short", year: "numeric" });
  // one bar for the whole import — the mean of the connections' fills
  const overall = items.length
    ? items.reduce((a, it) => a + (it.stalled ? 1 : it.progress), 0)
      / items.length : 0;
  return (
    <View style={{ marginTop: 10 }}>
      <Text style={[s.h2, { color: C.text }]}>
        Importing your full transaction history — {done}/{items.length}{" "}
        {items.length === 1 ? "connection" : "connections"} complete
      </Text>
      <BackfillBar frac={overall}
                   live={items.some((it) => !it.ready && !it.stalled)} tall />
      <Text style={s.mut}>
        Your bank sends up to two years of transactions after it connects,
        and this step waits for all of it. That history is how Oikonome
        finds your paychecks and recurring bills automatically, and how the
        next step pre-fills your budget from what you really spend.
        Scanning a few weeks instead would find almost nothing and set the
        wrong numbers. Usually a few minutes; the scan starts by itself
        when it finishes.
      </Text>
      {items.map((it) => (
        <View key={it.id} style={{ marginTop: 8 }}>
          <Text style={{ color: C.text, fontSize: 13 }}>
            {it.institution || "bank"}</Text>
          <Text style={s.mut}>
            {it.transactions.toLocaleString()} transaction
            {it.transactions === 1 ? "" : "s"}{it.ready ? "" : " so far"}
            {it.earliest ? ` · back to ${monthYear(it.earliest)}` : ""}
            {"  "}
            <Text style={{ color: it.ready ? C.good
                             : it.stalled ? C.warn : C.accent }}>
              {it.ready ? "✓ complete"
                : it.stalled ? "needs attention (Accounts) — won't hold this step"
                : it.active ? "pulling from your bank…"
                : "waiting for the bank to send more…"}
            </Text>
          </Text>
          <BackfillBar frac={it.stalled ? 1 : it.progress}
                       live={!it.ready && !it.stalled} muted={it.stalled} />
        </View>
      ))}
      <Text style={s.mut}>
        You can leave this screen — the import keeps going and the wizard
        resumes here.</Text>
    </View>
  );
}

/** The recap, and the daily email — on/off, hour, verdict-only vs the
 *  full report, and who receives it. Mirrors the web's FinishStep: with
 *  no schedule saved the daily email is ON by default. */
function FinishStep({ d }: {
  d?: { accounts: number; transactions: number; bills: number } }) {
  const { client } = useSession();
  const router = useRouter();
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"],
    queryFn: () => client!.settingsFull(), enabled: !!client });
  const me = useQuery({ queryKey: ["me"], queryFn: () => client!.me(),
                        enabled: !!client });
  // The left-the-wizard flag is keyed by TENANT, because that is how
  // Today reads it (index.tsx), so wait for the id rather than fall back
  // to a key Today never looks at.
  const tenantId = async () =>
    me.data?.tenant_id ?? (await client!.me()).tenant_id;
  const cfg = (settings.data || {}) as {
    email_schedule?: { daily?: { on?: boolean; hour?: number;
                                 summary?: boolean; push?: boolean } } | null;
    email_recipients?: string[] | null; smtp_host?: string | null;
    smtp_password_set?: boolean;
    food_monthly?: number | null; other_monthly?: number | null };
  const daily = cfg.email_schedule?.daily;
  // the web recap's budgets line: "$<food> food / $<other> everything-else"
  const dollars = (v?: number | null) =>
    v && v > 0 ? `$${Math.round(v).toLocaleString()}` : null;
  const budgetsLine = [dollars(cfg.food_monthly) && `${dollars(cfg.food_monthly)} food`,
                       dollars(cfg.other_monthly)
                         && `${dollars(cfg.other_monthly)} everything-else`]
    .filter(Boolean).join(" / ");
  // delivery = the email flag (`on`) and the push flag, read as one choice;
  // a local override holds the tap's answer while the write lands
  const [deliveryOv, setDeliveryOv] = useState<Delivery | null>(null);
  const delivery = deliveryOv ?? deliveryOf(daily?.on ?? true, !!daily?.push);
  const { on, push } = flagsOf(delivery);
  const [hour, setHour] = useState<number | null>(null);
  const [summary, setSummary] = useState<boolean | null>(null);
  const hourVal = hour ?? daily?.hour ?? 7;
  const summaryVal = summary ?? daily?.summary ?? false;
  const [pushNote, setPushNote] = useState<{ text: string; bad?: boolean } | null>(null);
  const notify = useQuery({ queryKey: ["notify"],
    queryFn: () => client!.notifyStatus(), enabled: !!client });
  // this phone's own channel is the relay, so it decides first; an instance
  // with browser push but no relay can still be told push works somewhere
  // a phone receives push only through the mobile-app relay — web push
  // (VAPID) reaches browsers, never this app, so it does not make the
  // choice available here; registering would "succeed" and deliver nothing
  const pushReady = !!notify.data?.native_push_available;
  // the reply seeds ["settings"], so the on/off line flips with it
  const save = useMutation({
    mutationFn: (next: { on: boolean; push: boolean; hour: number;
                         summary: boolean }) =>
      saveSettings(qc, client!, {
        email_schedule: {
          ...(cfg.email_schedule ?? {}),
          daily: { ...(daily ?? {}), on: next.on, push: next.push,
                   hour: next.hour, summary: next.summary },
        },
      }),
    onError: () => setDeliveryOv(null),
  });
  const commit = (patch: Partial<{ on: boolean; push: boolean; hour: number;
                                    summary: boolean }>) =>
    save.mutate({ on, push, hour: hourVal, summary: summaryVal, ...patch });
  // choosing push registers THIS phone too — the same door the launch
  // offers once — and says what the OS answered, so the choice is not a
  // promise the phone never agreed to
  const choose = async (next: Delivery) => {
    setDeliveryOv(next); setPushNote(null);
    commit(flagsOf(next));
    if ((next === "push" || next === "both") && client) {
      const r = await registerNativePush(client, { force: true });
      setPushNote(
        r === "ok" ? { text: "This phone will receive it." }
        : r === "denied" ? { bad: true, text: "Notifications are off for "
            + "this app in the phone's settings — allow them, then choose "
            + "Push again." }
        : r === "unsupported" ? { bad: true, text: "This build cannot "
            + "receive push; the installed app can." }
        : { bad: true, text: "Could not register this phone for push." });
      if (r === "ok") qc.invalidateQueries({ queryKey: ["notify"] });
    }
  };
  const hosted = !!(me.data as { hosted?: boolean } | undefined)?.hosted;
  const smtpSet = !!(cfg.smtp_host || cfg.smtp_password_set) || hosted;
  const hourLabel = (h: number) =>
    h === 0 ? "12am" : h < 12 ? `${h}am` : h === 12 ? "12pm" : `${h - 12}pm`;
  const n = (v: number | undefined) => (v ?? 0).toLocaleString();
  const plural = (v: number | undefined, one: string, many: string) =>
    (v ?? 0) === 1 ? one : many;
  return (
    <View style={{ marginTop: 10 }}>
      <Text style={s.h2}>You're set up ✓</Text>
      <Text style={s.mut}>
        {n(d?.accounts)} {plural(d?.accounts, "account", "accounts")} ·{" "}
        {n(d?.transactions)}{" "}
        {plural(d?.transactions, "transaction", "transactions")}</Text>
      <Text style={s.mut}>
        {n(d?.bills)} recurring {plural(d?.bills, "bill", "bills")} &amp;
        paychecks tracked</Text>
      {budgetsLine ? (
        <Text style={s.mut}>budgets: {budgetsLine}</Text>
      ) : null}
      <Text style={[s.h2, { marginTop: 12 }]}>Daily verdict</Text>
      <Text style={s.mut}>
        Each morning: on budget or not, and the number to hit. Where it
        goes:
        {!smtpSet && on ? " (Email needs SMTP in Settings → Email & Push before it can send.)" : ""}
      </Text>
      <View style={{ flexDirection: "row", gap: 8, flexWrap: "wrap",
                     marginTop: 6 }}>
        {([["email", "Email"], ["push", "Push"], ["both", "Both"],
           ["off", "Off"]] as [Delivery, string][]).map(([v, label]) => {
          const blocked = (v === "push" || v === "both") && !pushReady;
          const active = delivery === v;
          return (
            <Pressable key={v} disabled={save.isPending || blocked}
                       onPress={() => choose(v)}
                       style={[s.btn, active && { backgroundColor: C.accent,
                                                  borderColor: C.accent },
                               (blocked || save.isPending) && { opacity: 0.5 }]}>
              <Text style={[s.btnText, active && { color: "#fff" }]}>{label}</Text>
            </Pressable>
          );
        })}
      </View>
      {push && (
        <Text style={[s.mut, pushNote?.bad && { color: C.warn }]}>
          {pushNote ? pushNote.text
            : "Push goes to the phones and browsers you have enabled."}
          {(notify.data?.native_push_devices ?? 0) > 0
            ? ` ${notify.data!.native_push_devices} phone${
                notify.data!.native_push_devices === 1 ? "" : "s"} registered.`
            : ""}
        </Text>
      )}
      <View style={{ flexDirection: "row", alignItems: "center", gap: 10,
                     flexWrap: "wrap" }}>
        {(on || push) && (
          <View style={{ flexDirection: "row", alignItems: "center",
                         gap: 6, marginTop: 8 }}>
            <Pressable onPress={() => { const h = (hourVal + 23) % 24;
                                        setHour(h); commit({ hour: h }); }}>
              <Text style={s.link}>−</Text></Pressable>
            <Text style={{ color: C.text, fontSize: 13 }}>
              at {hourLabel(hourVal)}</Text>
            <Pressable onPress={() => { const h = (hourVal + 1) % 24;
                                        setHour(h); commit({ hour: h }); }}>
              <Text style={s.link}>+</Text></Pressable>
          </View>
        )}
      </View>
      {on && (
        <View style={{ marginTop: 8, gap: 6 }}>
          {([[false, "Full report", "the verdict plus the whole Today page: "
                + "spending buckets, recent transactions, upcoming bills"],
             [true, "Verdict only", "just the card: on budget or not, left "
                + "to spend, pinned bills"]] as const).map(([v, t, sub]) => (
            <Pressable key={t} onPress={() => { setSummary(v);
                                                commit({ summary: v }); }}
                       style={{ flexDirection: "row", gap: 8,
                                alignItems: "flex-start" }}>
              <Text style={{ color: summaryVal === v ? C.accent : C.mut,
                             fontSize: 14, lineHeight: 17 }}>
                {summaryVal === v ? "◉" : "○"}</Text>
              <Text style={[s.mut, { marginTop: 0, flex: 1 }]}>
                <Text style={{ color: C.text, fontWeight: "600" }}>{t}</Text>
                {" — " + sub}</Text>
            </Pressable>
          ))}
        </View>
      )}
      {/* no recipient line / household link here — the web's rule: the
          link led out of the wizard; household is added later */}
      {save.isError && <Text style={[s.mut, { color: C.warn }]}>
        {String(save.error)}</Text>}
    </View>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  h1: { color: C.text, fontSize: 17, fontWeight: "700", marginTop: 2 },
  h2: { color: C.text, fontSize: 15, fontWeight: "700", marginTop: 4 },
  mut: { color: C.mut, fontSize: 12, lineHeight: 17, marginTop: 4 },
  link: { color: C.accent, fontSize: 13 },
  input: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
           borderRadius: 8, color: C.text, fontSize: 14,
           paddingHorizontal: 10, paddingVertical: 8, marginTop: 6 },
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 14, paddingVertical: 8, marginTop: 8,
         alignSelf: "flex-start" },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
});
