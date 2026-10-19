// The guided first-run setup, native — the web's Welcome wizard.
//
// A household signs up on whichever device is in hand, so both clients
// have to open the same door. Without a native wizard the phone meets a
// new account with empty pages and no route through them.
//
// Same steps, same order, same words as the web (Appearance → Connect →
// Bills & budgets → Budgets → Email & push → Done, on every instance),
// and — the part that matters — the same marks in tenant config under
// `wizard_steps`. Start on the
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
import { CONFIRM_TO_CONNECT, VerifyEmailCard }
  from "../components/verify-email-card";

import StaleBanner from "../components/stale-banner";
import { Card, Pill, SharedLock } from "../components/ui";
import { AppearancePane } from "../components/wizard-appearance";
import { BillsQuestion, ManualBills, ProposalsInline, type SetupMode }
  from "../components/wizard-bills";
import { ManualBudgets } from "../components/wizard-budgets";
import { EmailPane, recipientCount } from "../components/wizard-email";
import { accountsSummary, billsContinue, billsSummary, budgetsSummary,
         checklistSummary, connectDoneHint, deliverySummary, historyRowNote,
         windowNarrower,
         finishBlurb, type HistoryMonths, historyMonthsOf,
         THEME_LABEL, type ThemeChoice, WIZARD_STEPS, type WizardStep,
         wizardThemeChoice } from "../components/wizard-logic";
import { HistoryChoice, useLatestWins } from "../components/wizard-history";
import { PrimaryCheckingPick } from "../components/wizard-primary-checking";
import { TAP, ws } from "../components/wizard-ui";
import type { Onboarding, SettingsView } from "../lib/api";
import { patchQuery, removeFromList, saveSettings } from "../lib/cache";
import { bankAwaitsConfirm, deliveryOf, institutionAllowance,
         FIRST_SYNC_RUNNING } from "../lib/pure";
import { ext } from "../ext";
import { useSession } from "../lib/session";
import { C, setThemeMode, themeRestartPending } from "../lib/theme";
import { useSharedLock } from "../lib/viewer";
import { markLeftWizard } from "../lib/storage";
import { type PlaidLinkOutcome, startPlaidLink,
         useRelinkingItems } from "../lib/plaid";
import { PendingLinkRows } from "../components/pending-links";
import { usePlaidLinkResume } from "./connect-hub";
import { ago } from "../lib/dates";
import { Planner } from "./budget";
import { errText } from "../lib/api";

type StepKey = WizardStep;

// the bills step's panes: the question first, then the path it chose
type BillsPane = "question" | "auto" | "manual";

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
  // the setup answer is the owner's alone — the server refuses anyone
  // else's with a 403 — so it is only ever saved once /api/me says owner
  const owner = (me.data as { role?: string } | undefined)?.role === "owner";
  const settings = useQuery({ queryKey: ["settings"],
    queryFn: () => client!.settingsFull(), enabled: !!client });

  const d = ob.data;
  const hosted = !!(me.data as { hosted?: boolean } | undefined)?.hosted;
  // linking or disconnecting a bank starts or ends a connection that
  // outlives the shared demonstration login, so the server refuses it
  // both; the connect step says so in place, as the hub does
  const credentialLock = useSharedLock("credential");
  const keys = WIZARD_STEPS;
  // how many of the plan's institutions are spent — both figures from the
  // server, never a constant here: the cap has a per-tenant override an
  // operator can raise
  const instCap = me.data?.features?.institution_cap;
  const instUsed = me.data?.features?.institutions_used;
  const { known: instCapKnown, full: instFull } =
    institutionAllowance(instCap, instUsed);
  // a free trial whose owner has not confirmed has an allowance of zero
  // (the server says so on /api/me): the connect
  // step closes the bank door with the reason above it, before the tap
  // the server refuses (the web wizard's hosted ConnectHub)
  const confirmFirst = bankAwaitsConfirm(me.data);
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
  // wait lasts (the web's rule: a new bank's first pull is queued on the
  // worker, not made at link time, and a queued first sync counts as
  // syncing, so the nudge only asks the worker and never starts a second
  // pull beside it; without a webhook nothing else would pull again, and
  // the server declines while a pull is recent, running or queued, so the
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
  const sv = settings.data as SettingsView | undefined;

  // ---- Appearance: the household's pinned theme, else Auto ----
  const [themeOv, setThemeOv] = useState<ThemeChoice | null>(null);
  const theme = themeOv ?? wizardThemeChoice(sv?.theme);
  const [restartPending, setRestartPending] = useState(false);
  // the owner's theme follows the ACCOUNT (the Settings card's rule);
  // the shared demonstration login keeps a per-device choice, as there
  const persistTheme = (m: ThemeChoice) => {
    setThemeMode(m);
    setRestartPending(themeRestartPending());
    if (me.data?.demo || !client) return;
    // the launch-time reconcile reads the cached settings: they must
    // carry the new theme from the tap, or the next launch writes the
    // old one back
    patchQuery<SettingsView>(qc, ["settings"], { theme: m });
    saveSettings(qc, client, { theme: m }).catch(() =>
      qc.invalidateQueries({ queryKey: ["settings"] }));
  };
  const pickTheme = (m: ThemeChoice) => { setThemeOv(m); persistTheme(m); };

  // ---- Connect: how much history comes in (HistoryChoice saves it on
  // the tap); the bank link waits while that save is in flight ----
  const [historyBusy, setHistoryBusy] = useState(false);
  const history: HistoryMonths = historyMonthsOf(
    sv && "history_months" in sv ? sv.history_months : d?.history_months);

  // ---- Bills & budgets: find them for me, or I'll enter them myself ----
  const [billsPane, setBillsPane] = useState<BillsPane>("question");
  const [modeOv, setModeOv] = useState<SetupMode | null>(null);
  const [modeErr, setModeErr] = useState<string | null>(null);
  const storedMode = sv?.setup_mode ?? d?.setup_mode;
  const setupMode: SetupMode = modeOv
    ?? (storedMode === "manual" ? "manual" : "auto");
  // manual also stops the finder scanning on its own; choosing the
  // finder turns it back on — the answer is the person's either way.
  // One save in flight at a time, the last tap winning: two sent together
  // can commit out of order, leaving the finder on or off against what
  // the screen says.
  const modeSaver = useLatestWins<SetupMode>(
    (m) => saveSettings(qc, client!, { setup_mode: m,
                                       bill_detection_auto: m === "auto" }),
    (m, failed, e) => {
      if (failed) { setModeOv(null); setModeErr(errText(e)); return; }
      setModeOv(m);
      qc.invalidateQueries({ queryKey: ["onboarding"] });
    });
  const saveMode = (m: SetupMode) => { setModeErr(null); modeSaver.save(m); };
  const pickMode = (m: SetupMode) => { setModeOv(m); saveMode(m); };
  // remount the inline planner after a save so it re-seeds from what
  // was saved (the Budget page's own rule for its planner)
  const [plannerGen, setPlannerGen] = useState(0);
  // a save made in this wizard makes the re-seeded planner show what was
  // saved, not the live suggestion again
  const [plannerSaved, setPlannerSaved] = useState(false);
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
  const onLinked = (r: PlaidLinkOutcome) => {
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
  };
  // a link the app stopped watching (Android dropped it during the
  // bank's OAuth hop) reports here when the wizard comes back
  usePlaidLinkResume(onLinked);
  // busy only while the bank's browser opens: the session is then a
  // pending row under the button (PendingLinkRows) and the button is
  // free for the next bank; the answer arrives through onLinked
  const link = useMutation({
    mutationFn: () => startPlaidLink(client!, qc, { kind: "add" },
                                     (r) => { if (r.kind !== "cancelled") onLinked(r); }),
    onMutate: () => setLinkMsg(null),
    onError: (e) => setLinkMsg(errText(e)),
  });
  // the same bank linked afresh with the wider window: the old link stays
  // live until the new one of the same bank is saved (a sign-in at
  // another bank just adds it); what is in is kept
  const relink = useMutation({
    mutationFn: (itemId: string) => startPlaidLink(
      client!, qc, { kind: "relink", relinkItemId: itemId },
      (r) => {
        if (r.kind !== "cancelled") onLinked(r);
        qc.invalidateQueries({ queryKey: ["accounts"] });
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["connections"] }),
    onError: (e) => setLinkMsg(errText(e)),
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
      qc.invalidateQueries({ queryKey: ["today"], refetchType: "none" });
      // …and the freed slot: the allowance above this list is /api/me's,
      // so without this the step still reads as full and keeps its
      // Connect button disabled after the disconnect that freed one
      qc.invalidateQueries({ queryKey: ["me"] });
    },
    onError: (e) => setLinkMsg(errText(e)),
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
      await qc.invalidateQueries({ queryKey: ["bills"], refetchType: "none" });
      qc.invalidateQueries({ queryKey: ["onboarding"] });
    },
  });
  // either planner's save: Continue unlocks now (the refetch only
  // confirms it), and the full planner re-seeds from what was saved
  // `set` is the server's own read-back (the manual step passes it; the
  // planner always stores a budget): a save that left nothing stored must
  // not mark the step done for the moment before the refetch corrects it
  const onBudgetsSaved = async (set: boolean = true) => {
    setPlannerSaved(set);
    patchQuery<{ budgets_set: boolean }>(qc, ["onboarding"],
      { budgets_set: set });
    await qc.invalidateQueries({ queryKey: ["settings"] });
    qc.invalidateQueries({ queryKey: ["onboarding"] });
    setPlannerGen((g) => g + 1);
  };
  const billsOut = billsContinue(d?.pending_proposals ?? 0);
  const askChecking = connectDoneHint(d?.checking_candidates?.length ?? 0,
                                      !!d?.primary_checking_id);
  type Primary = { label: string; disabled?: boolean; hint?: string;
                   // a pane change inside the step instead of advancing
                   onPress?: () => void } | null;
  const billsTitle = !backfillReady ? "Bills & budgets"
    : billsPane === "question"
      ? "Bills & budgets — how do you want to set them up?"
    : billsPane === "auto" ? "Confirm bills & income"
    : "Enter bills & income";
  const billsBlurb = !backfillReady ? ""
    : billsPane === "question"
      ? "Your history is in. Oikonome can read it and propose your "
        + "recurring bills, paychecks and a starting budget — or you can "
        + "enter them yourself."
    : billsPane === "auto"
      ? "The finder scanned your ledger for recurring bills and "
        + "paychecks. Approve what's right, dismiss what isn't — approved "
        + "(and still-pending) income seeds the next step's budget. "
        + "Continue any time and revisit later."
    : "Add the bills and paychecks you know. Each one matches its "
      + "charges in your ledger by merchant from here on, so renames and "
      + "price drift keep working. You can switch to automatic detection "
      + "any time from Bills.";
  const daily = (sv?.email_schedule as { daily?: { on?: boolean;
    push?: boolean; hour?: number } } | null | undefined)?.daily;
  const steps: {
    key: StepKey;
    // the progress line's name for the step; the heading may be longer
    label: string; title: string; blurb: string;
    go?: { label: string; to: string } | null;
    primary: Primary;
    // Back inside the step (a pane back to the question) before leaving it
    back?: (() => void) | null;
    skippable: boolean;
  }[] = [
    { key: "appearance", label: "Appearance", title: "Appearance",
      blurb: "Oikonome follows your phone or computer by default — light "
        + "by day, dark at night, whatever the system says. Pin one if you "
        + "prefer. You can change this any time in Settings.",
      primary: { label: "Continue →" }, skippable: false },
    { key: "connect", label: "Connect your accounts",
      title: "Connect your accounts",
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
      primary: { label: "Done connecting →",
                 disabled: !connected || !!askChecking,
                 hint: !connected ? "connect at least one source, or skip"
                   : askChecking },
      skippable: true },
    { key: "bills", label: "Bills & budgets", title: billsTitle,
      blurb: billsBlurb,
      // the door only opens once the history is in — see BackfillGate,
      // the same wait the web's step makes; then the question, then the
      // pane it chose, all inline as on the web
      go: null,
      primary: !backfillReady ? null
        : billsPane === "question"
          ? { label: "Continue →", onPress: () => {
                // continuing on the pre-selected answer is an answer too
                if (owner && storedMode !== setupMode) saveMode(setupMode);
                setBillsPane(setupMode);
              } }
          // the auto pane leaves done, or skipped with proposals still
          // pending: the Today checklist counts approved bills, and a
          // Continue past pending offers must not say the step is finished
          : billsPane === "auto"
            ? { label: billsOut.label, hint: billsOut.hint,
                onPress: () => advance(billsOut.state) }
            : { label: "Continue →" },
      back: billsPane !== "question" && backfillReady
        ? () => setBillsPane("question") : null,
      skippable: backfillReady },
    { key: "budgets", label: "Set your budgets", title: "Set your budgets",
      blurb: setupMode === "manual"
        ? "Your monthly plan: income and bills from the last step, a "
          + "number for each kind of spending, and what's left as savings. "
          + "The figures start blank — tap a suggestion from your history to "
          + "use it. The daily verdict only judges the variable spending."
        : "Your monthly plan, pre-filled from your real data: income "
          + "and bills from the last step, spending budgets from your "
          + "history, and what's left as savings. The daily verdict only "
          + "judges the variable spending.",
      // the planner is inline (the web's step), not a detour to the
      // Budget page — same Planner the page uses
      go: null,
      primary: { label: "Continue →", disabled: !d?.budgets_set,
                 hint: d?.budgets_set ? undefined : "save budgets first" },
      skippable: true },
    { key: "email", label: "Email & push", title: "Email & push",
      blurb: "One message a day with your verdict, and alerts when "
        + "something needs you. Choose how it reaches you, when, and who "
        + "else in the household gets it.",
      primary: { label: "Continue →" }, skippable: true },
    { key: "finish", label: "Done", title: "Done",
      // the first-email promise only when email is what was chosen
      blurb: finishBlurb(deliveryOf(daily?.on ?? true, !!daily?.push),
                         daily?.hour ?? 7),
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
  // the finder runs only on the path that asked for it: "I'll enter them
  // myself" means nothing is detected behind the person's back
  useEffect(() => {
    if (step.key === "bills" && billsPane === "auto" && backfillReady
        && !detectRan.current && client && !viewer) {
      detectRan.current = true;
      detect.mutate();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step.key, billsPane, backfillReady, client]);

  // leaving Appearance by Continue saves what is selected, the
  // pre-selected Auto included: a household that never chose has no
  // theme stored, and Auto is the answer it just accepted
  const onPrimary = () => {
    if (step.primary?.onPress) { step.primary.onPress(); return; }
    // (only once the stored theme has been read: a guess before then
    // could write Auto over a theme the household pinned)
    if (step.key === "appearance" && sv && sv.theme !== theme)
      persistTheme(theme);
    advance("done");
  };
  const onBack = () => {
    if (step.back) { step.back(); return; }
    setTouched(true); setI(idx - 1);
  };

  // the wizard is nothing but writes, so a viewer gets the same note the
  // web gives rather than a form that 403s on every button. This return
  // sits BELOW every hook on purpose — see the viewer note above.
  if (viewer)
    return (
      <ScrollView style={s.wrap}>
        <Card>
          <Text style={s.h1}>Setup</Text>
          <Text style={s.mut}>The account owner runs the guided setup — it
            connects banks and configures this instance.</Text>
        </Card>
      </ScrollView>
    );

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 28 }}
      keyboardShouldPersistTaps="handled"
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => ob.refetch()} />}>
      <StaleBanner query={ob} />
      <Card>
        <Text style={s.mut}>Step {idx + 1} of {steps.length} ·{" "}
          <Text style={{ color: C.text, fontWeight: "700" }}>{step.label}</Text>
        </Text>
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
        {!!step.blurb && <Text style={s.mut}>{step.blurb}</Text>}

        {step.key === "appearance" && (
          <AppearancePane value={theme} onPick={pickTheme}
                          restartPending={restartPending} />
        )}

        {step.key === "bills" && !backfillReady && backfill.data && (
          <BackfillGate items={backfill.data.items} />
        )}
        {step.key === "connect" && hosted && (
          <View style={{ marginTop: 10, gap: 8 }}>
            {confirmFirst && (
              <VerifyEmailCard me={me.data} note={CONFIRM_TO_CONNECT} />)}
            {/* "0 of 0" while the address waits says nothing true */}
            {instCapKnown && !confirmFirst && (
              <Text style={s.mut}>
                {ext.allowanceNote(instUsed, instCap, instFull)}
              </Text>
            )}
            <SharedLock note={credentialLock}>
            {/* the link's token is minted from the stored history
                window, so it waits for a window save still in flight */}
            <Pressable style={[s.btn, (link.isPending || instFull
                                       || confirmFirst || historyBusy)
                                      && { opacity: 0.5 }]}
                       disabled={link.isPending || instFull || confirmFirst
                                 || historyBusy || !!credentialLock}
                       accessibilityHint={confirmFirst ? CONFIRM_TO_CONNECT
                                                       : undefined}
                       onPress={() => link.mutate()}>
              <Text style={s.btnText}>
                {link.isPending ? "Opening your bank…"
                                : (conns.data?.connections ?? []).length
                                  ? "+ Connect another account ↗"
                                  : "+ Connect an account ↗"}
              </Text>
            </Pressable>
            </SharedLock>
            <PendingLinkRows />
            <Text style={s.mut}>Opens your bank's sign-in (Plaid). When
              you finish, the connection appears below so you can link
              another or continue.</Text>
            {linkMsg && <Text style={[s.mut, { color: C.warn }]}>{linkMsg}</Text>}
            {(conns.data?.connections ?? []).length > 0 && (
              <LinkedBanks conns={conns.data!.connections}
                           locked={!!credentialLock}
                           household={history}
                           onRelink={(id) => Alert.alert(
                             "Pull more history?",
                             "You sign in at the bank again; what is "
                             + "already in is kept.",
                             [{ text: "Cancel", style: "cancel" },
                              { text: "Relink", onPress: () => relink.mutate(id) }])}
                           relinkBusy={relink.isPending || link.isPending
                                       || historyBusy}
                           onDisconnect={(id) => disconnect.mutate(id)}
                           busyId={disconnect.isPending
                             ? disconnect.variables ?? null : null} />
            )}
          </View>
        )}

        {step.key === "bills" && backfillReady && billsPane === "question" && (
          <BillsQuestion value={setupMode} onPick={pickMode} />
        )}
        {step.key === "bills" && modeErr && (
          <Text style={[s.mut, { color: C.warn }]}>{modeErr}</Text>
        )}
        {step.key === "bills" && backfillReady && billsPane === "auto"
          && detect.isPending && (
          <Text style={[s.mut, { marginTop: 8 }]}>
            Scanning your history for paychecks and recurring bills…</Text>
        )}
        {step.key === "bills" && backfillReady && billsPane === "auto"
          && !detect.isPending && <ProposalsInline />}
        {step.key === "bills" && backfillReady && billsPane === "manual"
          && <ManualBills />}

        {step.key === "budgets" && setupMode === "manual" && (
          <ManualBudgets viewer={viewer} onSaved={onBudgetsSaved} />
        )}
        {step.key === "budgets" && setupMode === "auto" && settings.data
          && ob.data && (
          <Planner key={`wiz-p${plannerGen}`} s0={settings.data}
                   viewer={viewer}
                   wizard={{ saved: plannerSaved
                     || ob.data.wizard_steps?.budgets === "done" }}
                   onSaved={onBudgetsSaved} />
        )}

        {step.go && (
          <Pressable style={s.btn}
                     onPress={() => router.push(step.go!.to as never)}>
            <Text style={s.btnText}>{step.go.label}</Text>
          </Pressable>
        )}

        {step.key === "connect" && (
          <HistoryChoice fallback={d?.history_months}
                         onBusy={setHistoryBusy}
                         onSaved={() => qc.invalidateQueries(
                           { queryKey: ["onboarding"] })} />
        )}
        {/* the forecast's anchor: one checking account is pinned on the
            way out; two or more are asked here, before the step is left */}
        {step.key === "connect" && (d?.checking_candidates?.length ?? 0) > 0 && (
          <PrimaryCheckingPick candidates={d?.checking_candidates ?? []}
                               pinned={d?.primary_checking_id ?? null} />
        )}

        {step.key === "email" && <EmailPane hosted={hosted} />}

        {last && <FinishSummary d={d} sv={sv} theme={theme}
                                ownerEmail={me.data?.email}
                                history={history} />}

        {!!step.primary?.hint && (
          <Text style={[s.mut, { color: C.warn }]}>{step.primary.hint}</Text>
        )}

        {/* Skip · Back · Continue, the web's order */}
        <View style={{ flexDirection: "row", alignItems: "center",
                       gap: 12, marginTop: 16, flexWrap: "wrap" }}>
          {step.skippable && (
            <Pressable style={s.tap} onPress={() => advance("skipped")}>
              <Text style={s.link}>Skip this step</Text>
            </Pressable>
          )}
          <View style={{ flex: 1 }} />
          {(idx > 0 || !!step.back) && (
            <Pressable style={s.tap} onPress={onBack}>
              <Text style={s.link}>← Back</Text>
            </Pressable>
          )}
          {step.primary && (
            <Pressable
              style={[s.btn, { marginTop: 0 },
                      step.primary.disabled && { opacity: 0.5 }]}
              disabled={step.primary.disabled}
              onPress={onPrimary}>
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
                       gap: 14, marginTop: 4, flexWrap: "wrap" }}>
          <Pressable style={s.tap} onPress={async () => {
                       // records the decision so Today stops redirecting
                       // back into the wizard — the web keeps the same flag
                       await markLeftWizard(await tenantId());
                       router.replace("/" as never);
                     }}>
            <Text style={s.link}>save &amp; finish later</Text>
          </Pressable>
          <Pressable style={s.tap} disabled={finish.isPending}
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
function LinkedBanks({ conns, onDisconnect, busyId, locked, household,
                       onRelink, relinkBusy }: {
  conns: { id: string; institution_name: string | null; status: string | null;
           last_ok: string | null; accounts?: number;
           bank_updated_at?: string | null;
           history_months?: HistoryMonths }[];
  // the one row whose disconnect is in flight — never the whole list
  onDisconnect: (id: string) => void; busyId: string | null;
  // refused to this login; the reason is stated once above the list
  locked: boolean;
  // the household's window: a bank linked narrower gets "pull more
  // history", a fresh link of the same bank (the web's HubMoreHistory)
  household?: HistoryMonths; onRelink?: (id: string) => void;
  relinkBusy?: boolean }) {
  const total = conns.reduce((t, c) => t + (c.accounts ?? 0), 0);
  // a bank whose fresh link is still pending stays listed and live; its
  // door waits for that link to land (the server refuses a second one)
  const relinking = useRelinkingItems();
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
                    : ` · ${FIRST_SYNC_RUNNING}`}
                  {/* the bank's clock, not ours: sync reads Plaid's copy,
                      and the bank refreshes that copy on its own
                      schedule */}
                  {!bad && c.bank_updated_at
                    ? ` · bank sent data ${ago(c.bank_updated_at)}` : ""}
                  {/* the window this bank was linked with, when it is
                      less than everything */}
                  {c.history_months != null
                    ? ` · ${historyRowNote(c.history_months)}` : ""}
                </Text>
              </View>
              {!bad && onRelink && windowNarrower(c.history_months, household)
                && relinking.has(c.id) && (
                <Text style={{ color: C.mut, fontSize: 12 }}>relinking…</Text>
              )}
              {!bad && onRelink && windowNarrower(c.history_months, household)
                && !relinking.has(c.id) && (
                <Pressable disabled={relinkBusy || locked}
                           onPress={() => onRelink(c.id)}
                           style={{ borderWidth: StyleSheet.hairlineWidth,
                                    borderColor: C.border, borderRadius: 8,
                                    paddingHorizontal: 10, paddingVertical: 4,
                                    opacity: relinkBusy || locked ? 0.55 : 1 }}>
                  <Text style={{ color: C.accent, fontSize: 12 }}>
                    pull more history ↗</Text>
                </Pressable>
              )}
              <Pressable disabled={busyId === c.id || locked}
                         onPress={() => Alert.alert(
                           `Disconnect ${c.institution_name || c.id}?`,
                           "It stops syncing — history is kept.",
                           [{ text: "Cancel", style: "cancel" },
                            { text: "Disconnect", style: "destructive",
                              onPress: () => onDisconnect(c.id) }])}
                         style={{ borderWidth: StyleSheet.hairlineWidth,
                                  borderColor: C.border, borderRadius: 8,
                                  paddingHorizontal: 10, paddingVertical: 4,
                                  opacity: locked ? 0.55 : 1 }}>
                <Text style={{ color: C.mut, fontSize: 12 }}>Disconnect</Text>
              </Pressable>
            </View>
          );
        })}
      </View>
    </View>
  );
}

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
                // nothing has landed yet: the first sync is queued
                : it.transactions === 0 ? FIRST_SYNC_RUNNING
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

/** The Done step's recap, one line per step — what the household chose,
 *  read back from /api/onboarding and the settings, the web's rows. */
function FinishSummary({ d, sv, theme, ownerEmail, history }: {
  d?: Onboarding; sv?: SettingsView; theme: ThemeChoice;
  ownerEmail?: string; history: HistoryMonths;
}) {
  const { client } = useSession();
  const bills = useQuery({ queryKey: ["bills"],
    queryFn: () => client!.bills(), enabled: !!client });
  // tracked = not disabled, the web's count
  const list = (bills.data?.bills ?? []).filter((b) => !b.disabled);
  const income = list.filter((b) => b.income).length;
  const daily = (sv?.email_schedule as { daily?: { on?: boolean;
    push?: boolean; hour?: number } } | null | undefined)?.daily;
  const rows: [string, string][] = [
    ["Appearance", THEME_LABEL[theme]],
    ["Accounts", accountsSummary(d?.connections ?? 0, d?.accounts ?? 0,
                                 history)],
    ["Bills & income", billsSummary(list.length - income, income)],
    ["Budgets", budgetsSummary(!!d?.budgets_set, sv?.food_monthly,
                               sv?.other_monthly)],
    ["Email & push", deliverySummary(
      deliveryOf(daily?.on ?? true, !!daily?.push), daily?.hour ?? 7,
      recipientCount(sv, ownerEmail))],
    // what the Today screen's checklist will say — the same list it reads
    ["Today checklist", d ? checklistSummary(d) : "…"],
  ];
  return (
    <View style={ws.body}>
      {rows.map(([k, v], n) => (
        <View key={k} style={[ws.row, { justifyContent: "space-between",
                                        borderTopWidth: n ? ws.row.borderTopWidth
                                                          : 0 }]}>
          <Text style={{ color: C.text, fontSize: 14 }}>{k}</Text>
          <Text style={[ws.small, { marginTop: 0, flexShrink: 1,
                                    textAlign: "right" }]}>{v}</Text>
        </View>
      ))}
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
  btn: { backgroundColor: C.accent, borderRadius: 8, minHeight: TAP,
         paddingHorizontal: 14, paddingVertical: 8, marginTop: 8,
         alignSelf: "flex-start", justifyContent: "center" },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
  // a text link's touch area — one thumb, 44pt
  tap: { minHeight: TAP, justifyContent: "center" },
});
