// Budget — the web Budget page, native: the plan's flow line, this
// month tracked, the planner (income → bills → categories → unallocated
// → savings), bucket rules, income scenarios, savings goals, and the
// bill-tweaks summary line. Every save merges against a FRESH
// /api/settings read — never the cache — so two cards can't clobber
// each other's keys.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "expo-router";
import { memo, useCallback, useEffect, useRef, useState } from "react";
import { Alert, Pressable, ScrollView, StyleSheet, Switch, Text, TextInput,
         View } from "react-native";
import PullRefresh from "../components/pull-refresh";

import MoneyMap, { sentPlanRows, sentPlanTotal } from "../components/money-map";
import { FailedLine, StaleLine } from "../components/loaded";
import { OfflineNote, offlineGrey, useOffline } from "../components/offline";
import { Card, H, HelpLink, Note } from "../components/ui";
import { errText, Account, BudgetSettings, CustomBucket, TodayFull } from "../lib/api";
import { saveSettings, SETTINGS_SAVES } from "../lib/cache";
import { goalHasWords, goalNeedsAccount, isPlanOnlySavingsGoal,
         isPlanSavingsGoal,
         bucketRulesSaved, mergeBucketRules, plannerBucketsSaved,
         savedMergeBase,
         mergeIncomeScenarios, mergePlannerBuckets,
         mergeSavingsGoals, numv, savedAmount, type BucketRuleDraft, type GoalDraft,
         type ScenarioDraft,
         type PlannerRow, catLabel, historyBasisNote, inputPhase,
         PLANNER_HISTORY_FAILED, plannerAtRest, plannerShares, plannerWaits,
         SAVE_WAITS_LOADING, UNKNOWN_FIGURE }
  from "../lib/pure";
import { useRefetchWhenShown } from "../lib/focus";
import { plannerPreview, plannerUnallocAuto }
  from "../lib/planner";
import { useSession } from "../lib/session";
import { useViewer } from "../lib/viewer";
import { C, money } from "../lib/theme";
import { disclosure } from "../lib/a11y";
import { fieldA11y } from "../components/field";

const m$ = (n: number) => money(n, false);
// '2026-11-03' → '11/03': the web Budget page's dates (its mmdd)
const mmdd = (iso: string | null | undefined) =>
  iso && iso.length >= 10 ? `${iso.slice(5, 7)}/${iso.slice(8, 10)}` : "";

// stable per-row ids for the draft lists below (Planner categories,
// income scenarios, savings goals, bucket rules). Keying on array index
// breaks mid-edit deletes: React reuses the deleted row's TextInputs for
// its successor, so keyboard focus and the native selection stay put
// on screen while the value under them shifts up. A counter, not
// Date.now() — two rows added in the same render tick need distinct ids.
let rowIdSeq = 0;
const newRowId = () => `row${++rowIdSeq}`;

const TODAY_KEY = ["today"];

// A server older than the plan's legs: the line from the figures it sends
// (plan_line when it has an income, the raw figures otherwise).
function olderPlanLegs(d: TodayFull) {
  const pl = d.plan_line;
  const saved = pl ? pl.saved !== null : (d.savings_plan ?? 0) > 0.005;
  return {
    income: d.income === null ? null : pl ? pl.income : d.income,
    bills: pl ? pl.bills : d.avg_bills,
    to_spend: pl ? pl.to_spend : d.plan_variable ?? d.variable_budget,
    saved: !saved ? null : pl ? pl.saved : d.savings_plan ?? null,
    excess: d.plan_surplus === null ? null : pl ? pl.excess : d.plan_surplus,
  };
}

export default function Budget() {
  const { client } = useSession();
  const viewer = useViewer();
  const qc = useQueryClient();
  const router = useRouter();
  const today = useQuery({ queryKey: ["today"],
    queryFn: () => client!.todayFull(), enabled: !!client });
  // a write here or on a screen pushed over this one defers its
  // ["today"] refetch to whoever shows it — this screen (and the Savings
  // goals card below, same query) when it is on screen
  useRefetchWhenShown(today, TODAY_KEY);
  const settings = useQuery({ queryKey: ["settings"],
    queryFn: () => client!.settingsFull(), enabled: !!client });
  const ob = useQuery({ queryKey: ["onboarding"],
    queryFn: () => client!.onboarding(), enabled: !!client,
    staleTime: 5 * 60_000 });
  const d = today.data;
  const s0 = settings.data;
  // the summary line's legs are the server's (budget.plan_legs), a plan
  // with no income included
  const legs = !d ? null : d.plan_legs !== undefined ? d.plan_legs
    // older-server path: stands in for `plan_legs`
    : olderPlanLegs(d);
  // after a save the SIBLING cards remount so their mount-time seeds
  // pick up fresh settings — but a sibling holding unsaved edits is
  // skipped: the remount would silently discard the draft (web rule)
  const [gens, setGens] = useState({ p: 0, i: 0, g: 0, b: 0 });
  const dirty = useRef({ p: false, i: false, g: false, b: false });
  const markDirty = (k: "p" | "i" | "g" | "b") => (v: boolean) => {
    dirty.current[k] = v;
    if (k === "p") setPlanDirty(v);
  };
  // the planner's unsaved edits, as state: while there are any, a bar with
  // Save and Discard floats over the screen (the web planner's bar) — its
  // own button sits at the foot of a long card
  const [planDirty, setPlanDirty] = useState(false);
  const planSave = useRef<{ save: () => void; saving: boolean } | null>(null);
  const discardPlan = () => {
    dirty.current.p = false;
    setPlanDirty(false);
    // a remount re-seeds every field from what is saved
    setGens((g0) => ({ ...g0, p: g0.p + 1 }));
  };
  // every save here goes through saveSettings, which has already put the
  // server's reply into ["settings"] by the time this runs — so the
  // siblings remount onto fresh settings without a refetch. What a plan
  // change CAN move: the verdict, the Month lens, the prefill — and the
  // bill calendar, whose forecast plots the savings goals and caps
  // paychecks by the income scenario.
  const refresh = (saved: "p" | "i" | "g" | "b") => () => {
    // the Today tab is frozen and catches up when shown; this screen is
    // shown, so its own Today observer (the prefill, the goals) refetches
    // now through useRefetchWhenShown
    qc.invalidateQueries({ queryKey: ["today"], refetchType: "none" });
    qc.invalidateQueries({ queryKey: ["lens"] });
    qc.invalidateQueries({ queryKey: ["budgets-suggest"] });
    qc.invalidateQueries({ queryKey: ["calendar"], refetchType: "none" });
    // the saved card clears its own unsaved flag, and only when nothing
    // was typed while the save was in flight (the web's rule) — clearing
    // it here let the next sibling save remount over those edits
    setGens((g0) => {
      const n = { ...g0 };
      for (const k of ["p", "i", "g", "b"] as const)
        if (k !== saved && !dirty.current[k]) n[k] = g0[k] + 1;
      return n;
    });
  };

  const tweakBits: string[] = [];
  if (s0) {
    const nd = (s0.disabled_bills ?? []).length;
    const nc = Object.keys(s0.occurrence_caps ?? {}).length;
    const nh = Object.keys(s0.dismissed_hints ?? {}).length;
    if (nd) tweakBits.push(`${nd} bill${nd === 1 ? "" : "s"} disabled`);
    if (nc) tweakBits.push(`${nc} occurrence cap${nc === 1 ? "" : "s"}`);
    if (nh) tweakBits.push(`${nh} hint${nh === 1 ? "" : "s"} dismissed`);
  }

  return (
    <View style={{ flex: 1, backgroundColor: C.bg }}>
    <ScrollView style={st.wrap}
                contentContainerStyle={{ paddingBottom: planDirty ? 96 : 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => Promise.all([
                                        settings.refetch(), today.refetch()])} />}>
      <StaleLine query={settings} />
      <StaleLine query={today} />
      {(today.isPending || settings.isPending) && (
        <Text style={st.center}>Loading…</Text>
      )}
      {/* each read that failed says so in its sections' place: the plan
          and this month are one, the cards that edit settings the other */}
      {d === undefined && today.isError && (
        <FailedLine q={today} what="this month's plan" />)}
      {s0 === undefined && settings.isError && (
        <FailedLine q={settings} what="the budget settings" />)}
      {d && legs && (
        <Card>
          <Text style={{ color: C.text, fontSize: 15, lineHeight: 22 }}>
            {/* the plan's legs in the whole dollars every surface prints
                (budget.plan_legs), an income-less plan's too: the same
                figures and words as the web */}
            {legs.income !== null && (
              <><B>{m$(legs.income)}</B><M> in → </M></>
            )}
            {/* an older server's unknown bills print as unknown, never $0 */}
            {/* "bills & envelopes" with a pool; older-server path:
                stands in for `plan_bills_label` — "bills" */}
            <B>{money(legs.bills, false)}</B>
            <M> {d?.plan_bills_label ?? "bills"} → </M>
            <B>{m$(legs.to_spend)}</B><M> to spend</M>
            {legs.saved !== null && (
              <><M> → </M><B>{m$(legs.saved)}</B><M> saved</M></>
            )}
            {legs.excess !== null && (
              <>
                <M> → </M>
                {/* the server's words and tone after the figure
                    (todayview.plan_excess_words) */}
                <Text style={{ color: d.plan_excess_words !== undefined
                                 ? (d.plan_excess_words?.tone === "neg" ? C.bad
                                   : d.plan_excess_words?.tone === "mut" ? C.mut
                                   : C.good)
                                 // older-server path: stands in for `plan_excess_words`
                                 : legs.excess >= 0 ? C.good : C.bad,
                               fontWeight: "700" }}>
                  {m$(legs.excess)}
                </Text>
                <M> {d.plan_excess_words !== undefined
                  ? (d.plan_excess_words?.text ?? "excess")
                  // older-server path: stands in for `plan_excess_words`
                  : "excess"}</M>
              </>
            )}
          </Text>
          {/* the web's sub-line, in its order: the variable spending, the
              income (what landed plus what is still due), manage bills.
              "variable spending $0 of $0" is arithmetic, not news, and it
              reads as progress against a plan nobody has made, so it is
              hidden when budgets are unset, as on the Today hero.
              Undefined onboarding data (still loading) keeps the line, so
              it never flashes away. */}
          <Text style={st.mut}>
            {!(ob.data && !ob.data.budgets_set) && (<>
              day {d.day_of_month} of {d.days_in_month} · variable spending{" "}
              {m$(d.variable_actual)} of {money(d.variable_budget_paced ?? d.variable_budget, false)}
              {/* the month's spendable share; when it is not the plan's
                  figure the line says why (the web's words and dates) */}
              {d.variable_scale
                // older-server path: stands in for `variable_budget_paced`
                ? ` (scaled to ${money(d.variable_budget_paced ?? d.variable_budget, false)} this month)`
                : d.paced_from ? ` (paced from ${mmdd(d.paced_from)})` : ""}
              {"   "}
            </>)}
            {d.income_expected != null && (<>
              income <Text style={{ color: C.text }}>{m$(d.income_received ?? 0)}</Text> landed
              {d.income_expected > 0 ? (
                <> · <Text style={{ color: C.text }}>{m$(d.income_expected)}</Text> still due</>
              ) : null}
              {"   "}
            </>)}
            <Text style={{ color: C.accent }}
                  onPress={() => router.push("/bills" as never)}>
              manage bills ›
            </Text>
          </Text>
          {d.variable_scale && (
            <Text style={{ color: C.warn, fontSize: 12, marginTop: 6 }}>
              Variable budgets auto-scaled from{" "}
              {m$(d.variable_scale.static_total)} to fit this
              month&apos;s bills.
            </Text>
          )}
        </Card>
      )}
      {d && d.buckets?.fixed && (
        <Card>
          <H>
            This month{" "}
            <Text style={[st.mut, { fontWeight: "400" }]}>
              — the plan below, tracked
            </Text>
          </H>
          <MoneyMap
            // the server's rows and the bar over them (each figure the
            // sum of its rows) — the web Budget page's
            total={sentPlanTotal(d.plan_total)}
            rows={sentPlanRows(d.plan_rows ?? [])} />
        </Card>
      )}
      {s0 && (
        <Planner key={`p${gens.p}`} s0={s0} viewer={viewer}
                 saveRef={planSave}
                 onSaved={refresh("p")} onDirty={markDirty("p")} />
      )}
      {!viewer && s0 && (
        <>
          <BucketRules key={`b${gens.b}`} s0={s0}
                       onSaved={refresh("b")}
                       onDirty={markDirty("b")} />
          {/* the plan's two levers, stacked and minimized below the
              bucket rules, mirroring the web — hidden, never unmounted,
              so an open draft survives a toggle */}
          <CollapsiblePane title="Income scenarios"
            sub="take-home per paycheck; the active one drives the cash forecast">
            <IncomeScenarios key={`i${gens.i}`} s0={s0}
                             onSaved={refresh("i")}
                             onDirty={markDirty("i")} />
          </CollapsiblePane>
          <CollapsiblePane title="Savings goals"
            sub="trip fund, emergency fund; progress comes from real transfers">
            <SavingsGoals key={`g${gens.g}`} s0={s0}
                          onSaved={refresh("g")}
                          onDirty={markDirty("g")} />
          </CollapsiblePane>
          {tweakBits.length > 0 && (
            <Text style={[st.mut, { textAlign: "center", marginTop: 4 }]}>
              Bill tweaks: {tweakBits.join(" · ")} — all set on{" "}
              <Text style={{ color: C.accent }}
                    onPress={() => router.push("/bills" as never)}>
                Bills ›
              </Text>
            </Text>
          )}
        </>
      )}
      {/* the frozen record the lenses judge closed months by — viewers
          read it too; only the backfill action is owner chrome */}
      <BudgetHistory viewer={viewer} />
      <HelpLink topic="budget" />
    </ScrollView>
    {planDirty && !viewer && (
      <View style={{ position: "absolute", left: 12, right: 12, bottom: 12,
                     flexDirection: "row", alignItems: "center", gap: 8,
                     backgroundColor: C.hover, borderColor: C.accent,
                     borderWidth: 1, borderRadius: 12, padding: 8 }}>
        <Text style={{ color: C.text, fontSize: 14, flex: 1 }}>
          Unsaved changes</Text>
        <Pressable accessibilityRole="button" onPress={discardPlan}
                   style={{ minHeight: 44, justifyContent: "center",
                            paddingHorizontal: 14, borderRadius: 8,
                            borderWidth: 1, borderColor: C.border,
                            backgroundColor: C.card }}>
          <Text style={{ color: C.text, fontSize: 14 }}>Discard</Text>
        </Pressable>
        <Pressable accessibilityRole="button"
                   onPress={() => planSave.current?.save()}
                   style={{ minHeight: 44, justifyContent: "center",
                            paddingHorizontal: 14, borderRadius: 8,
                            backgroundColor: C.btn }}>
          <Text style={{ color: C.text, fontSize: 14, fontWeight: "600" }}>
            Save budgets</Text>
        </Pressable>
      </View>
    )}
    </View>
  );
}

const HIST_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Budget history — read-only list of each month's frozen budget plus the
// one deliberate action: backfill, the owner's explicit opt-in to judging
// pre-snapshot months against the current budget (rows stay labeled
// "backfilled" forever). Mirrors the web BudgetHistoryCard.
function BudgetHistory({ viewer }: { viewer: boolean }) {
  const { client } = useSession();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["budget-snapshots"],
    queryFn: () => client!.budgetSnapshots(), enabled: !!client });
  const [busy, setBusy] = useState(false);
  const [all, setAll] = useState(false);
  const d = q.data;
  if (!d || (!d.months.length && !d.missing)) return null;
  const rows = all ? d.months : d.months.slice(0, 24);   // web's SHOW
  const backfill = () => Alert.alert(
    // the web card's confirm, verbatim intent (and has/have agreement)
    `Apply your current budget to ${d.missing} past month${
      d.missing > 1 ? "s" : ""} that ${d.missing > 1 ? "have" : "has"} `
    + "no frozen budget?",
    "Those months will be judged against today's numbers — a verdict "
    + "you're choosing retroactively, and the history will label it "
    + "\"backfilled\". This doesn't touch months already frozen.",
    [{ text: "Cancel", style: "cancel" },
     { text: "Apply", onPress: () => { void (async () => {
        setBusy(true);
        try {
          const r = await client!.budgetBackfill();
          Alert.alert("Budget history",
            `Froze your current budget for ${r.backfilled} past month${
              r.backfilled === 1 ? "" : "s"}.`);
          // unawaited, like the two lens invalidations below — the
          // success Alert already fired, so "freezing…" has no reason
          // to keep the label past the server's answer, only past the
          // refetch that confirms it
          qc.invalidateQueries({ queryKey: ["budget-snapshots"] });
          qc.invalidateQueries({ queryKey: ["lens", "year"] });
          qc.invalidateQueries({ queryKey: ["lens"] });
        } catch (e) {
          Alert.alert("Backfill failed", errText(e));
        } finally {
          setBusy(false);
        }
      })(); } }]);
  return (
    <Card>
      <H>Budget history</H>
      <M>Each month&apos;s budget and bill schedule, frozen as the month
        closed (this month&apos;s so far).</M>
      {rows.map((r) => (
        <View key={`${r.y}-${r.m}`} style={{ marginTop: 6 }}>
          <View style={{ flexDirection: "row", alignItems: "baseline",
                         gap: 8 }}>
            <Text style={{ color: C.text, fontSize: 13, flex: 1 }}>
              {HIST_MONTHS[r.m - 1]} {r.y}
              {r.status_text ? (
                <Text style={{ color: C.mut, fontSize: 11 }}>
                  {"  "}· {r.status_text}</Text>) : null}
              {r.source === "backfill" && (
                <Text style={{ color: C.mut, fontSize: 11 }}>
                  {"  "}· backfilled {r.captured_at.slice(0, 10)}
                </Text>
              )}
            </Text>
            <Text style={{ color: C.text, fontSize: 13, fontWeight: "700",
                           fontVariant: ["tabular-nums"] }}>
              {m$(r.variable_budget)}
            </Text>
          </View>
          <Text style={{ color: C.mut, fontSize: 11,
                         fontVariant: ["tabular-nums"] }}>
            Food {m$(r.food_monthly)} · Everything else{" "}
            {m$(r.other_monthly)}
            {/* the schedule frozen with the budget — absent on snapshots
                that predate bill freezing (matches the web's "—") */}
            {r.bills_monthly !== undefined &&
              ` · Bills ${m$(Math.abs(r.bills_monthly))} evened out`}
          </Text>
        </View>
      ))}
      {d.months.length > 24 && !all && (
        <Pressable accessibilityRole="button" onPress={() => setAll(true)}>
          <Text style={{ color: C.accent, fontSize: 13, marginTop: 8 }}>
            show all {d.months.length} months
          </Text>
        </Pressable>
      )}
      {d.missing > 0 && (
        <Text style={{ color: C.mut, fontSize: 12, marginTop: 8 }}>
          {d.missing} earlier month{d.missing > 1 ? "s have" : " has"} no
          frozen budget — the Year page shows net income − spending for
          {d.missing > 1 ? " them" : " it"}.
          {!viewer && d.budget_set && (
            <Text style={{ color: C.accent }}
                  onPress={busy ? undefined : backfill}>
              {"  "}{busy ? "freezing…" : "apply my current budget to them"}
            </Text>
          )}
        </Text>
      )}
    </Card>
  );
}

// a minimized pane: one summary row closed, the real card open. Nothing
// mounts before the FIRST open — that's what stops the pane's own
// queries (SavingsGoals' ["accounts"]) from firing on every Budget open
// while the card sits collapsed. Once opened, the children stay mounted
// (display, not unmount) so a draft in progress survives a stray
// re-toggle. Mirrors the web's Collapsible, minus the always-mounted part.
function CollapsiblePane({ title, sub, children }: {
  title: string; sub: string; children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const everOpened = useRef(false);
  if (open) everOpened.current = true;
  return (
    <>
      {!open && (
        <Card>
          <Pressable onPress={() => setOpen(true)}
                     {...disclosure(false, `${title} — ${sub}`)}>
            <Text style={{ color: C.text, fontSize: 15,
                           fontWeight: "700" }}>
              ▸ {title}
              <Text style={{ color: C.mut, fontSize: 12,
                             fontWeight: "400" }}>  — {sub}</Text>
            </Text>
          </Pressable>
        </Card>
      )}
      {everOpened.current && (
        <View style={open ? undefined : { display: "none" }}>
          {open && (
            <Text style={{ color: C.mut, fontSize: 12,
                           marginHorizontal: 12, marginTop: 8 }}
                  onPress={() => setOpen(false)}>
              ▾ {title} — minimize
            </Text>
          )}
          {children}
        </View>
      )}
    </>
  );
}

function B({ children }: { children: React.ReactNode }) {
  return <Text style={{ fontWeight: "700" }}>{children}</Text>;
}
function M({ children }: { children: React.ReactNode }) {
  return <Text style={{ color: C.mut, fontSize: 13 }}>{children}</Text>;
}

// ---- the planner: give every dollar a job ----
type Row = PlannerRow & { id: string };

export function Planner({ s0, viewer, onSaved, onDirty, wizard, saveRef }: {
  /** the screen's handle on this card's save, for a Save button it draws
   *  outside the card */
  saveRef?: { current: { save: () => void; saving: boolean } | null };
  s0: BudgetSettings; viewer: boolean; onSaved: () => void;
  onDirty?: (v: boolean) => void;
  /** Set by the welcome wizard (the web's non-standalone BudgetPlanner):
   *  /api/settings always carries the provisional first-run seed once any
   *  spend exists, and the stored row keeps it once a month has closed,
   *  so in the wizard the plan counts as saved by the server's
   *  budgets_saved_by_user (wizard-logic planSaved), never by the
   *  settings view's figures. */
  wizard?: { saved: boolean };
}) {
  const { client } = useSession();
  const router = useRouter();
  // offline, the saves here are greyed with "You're offline." beside them
  const offline = useOffline();
  const qc = useQueryClient();
  const sug = useQuery({ queryKey: ["budgets-suggest"],
    queryFn: () => client!.budgetsSuggest(), enabled: !!client });
  // the month the header shows: its income is the plan's income, and the
  // server says where it comes from — the planner never works that out
  const now = useQuery({ queryKey: ["today"],
    queryFn: () => client!.todayFull(), enabled: !!client });
  // the plan's bills leg in the server's words ("Bills & envelopes" with
  // a pool); older-server path: stands in for `plan_bills_label`
  const billsWord = now.data?.plan_bills_label ?? "bills";
  const billsLabel = billsWord.charAt(0).toUpperCase() + billsWord.slice(1);
  const tracked = now.data?.income_source === "paychecks"
    || now.data?.income_source === "scenario";
  const [income, setIncome] = useState<string | null>(null);
  const [rows, setRows] = useState<Row[] | null>(null);
  // the rows on screen now, for a save that lands after more typing
  const rowsNow = useRef(rows);
  rowsNow.current = rows;
  // the "other" buckets the planner was last seeded with (at open, then
  // each saved view) — the save-time merge tells "removed here" from
  // "added elsewhere since" by this list
  const [baseNames, setBaseNames] = useState<string[]>([]);
  const [otherBase, setOtherBase] = useState(0);
  const [unallocEdited, setUnallocEdited] = useState<string | null>(null);
  const [savings, setSavings] = useState("");
  const [savingsTouched, setSavingsTouched] = useState(false);
  // the Savings plan the planner opened with, already outside other_monthly
  const [savingsSeed, setSavingsSeed] = useState(0);
  // the numbers came from the suggestion, not a saved plan: only then
  // does the history line describe what the person is looking at
  const [fromHistory, setFromHistory] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  // a plan opened from what is saved shows history's figure beside each
  // row, as the server sends it (the web's hint); a plan seeded from
  // history IS that figure, and a server without the field shows none
  const hint = (v: number | undefined) =>
    fromHistory || typeof v !== "number" ? null : (
      <Text style={{ color: C.mut, fontSize: 11 }}
            accessibilityLabel={`history about ${m$(v)}`}>
        history ~{m$(v)}</Text>);
  const rowHint = (r: Row) => hint(r.food
    ? sug.data?.suggestions.food_monthly
    : r.orig ? sug.data?.suggestions.buckets[r.orig] : undefined);
  // A first run whose history names no income (the suggestion sent no
  // income_monthly) asks the same door again with the take-home typed in
  // the box, once typing pauses; the server measures the savings on it
  // (the web's rule). An older server ignores the figure and sends no
  // suggestion, so none is shown.
  const asksTyped = fromHistory && !tracked && !!sug.data
    && sug.data.suggestions.income_monthly == null;
  const typedNow = (income ?? "").trim();
  const [typedAsked, setTypedAsked] = useState("");
  useEffect(() => {
    if (!asksTyped) return;
    const t = setTimeout(() => setTypedAsked(typedNow), 400);
    return () => clearTimeout(t);
  }, [asksTyped, typedNow]);
  const sugTyped = useQuery({
    queryKey: ["budgets-suggest", "typed", typedAsked],
    queryFn: () => client!.budgetsSuggestTyped(typedAsked),
    enabled: !!client && asksTyped && typedAsked !== "" });
  // one-time seed. An established user's plan comes entirely from s0 —
  // it doesn't need ["budgets-suggest"] (a median-over-history
  // computation) to show the plan card, so only a first-time user (whose
  // seed leans on bucket_candidates) waits for it.
  useEffect(() => {
    const userSaved = wizard
      ? wizard.saved
      : !!(s0.food_monthly || s0.other_monthly
           || s0.budgeted_income_monthly);
    // a first run waits for the suggestion's answer: a failed one is not
    // a history of $0
    if (rows !== null || (!sug.data && !userSaved)) return;
    const suggestions = sug.data?.suggestions;
    const pick = (saved: number | null, suggested?: number) =>
      userSaved && (saved ?? 0) > 0 ? saved!
        : suggested ? Math.round(suggested) : (saved ?? 0) > 0 ? saved! : 0;
    setIncome(String(pick(s0.budgeted_income_monthly,
                          suggestions?.income_monthly) || 0));
    const foodVal = pick(s0.food_monthly, suggestions?.food_monthly);
    const opened = plannerBucketsSaved(s0.custom_buckets ?? []);
    const existing: Row[] = opened.rows.map((r) => ({ ...r, id: newRowId() }));
    setBaseNames(opened.base);
    const seeded: Row[] = (!userSaved && existing.length === 0)
      ? (sug.data?.bucket_candidates ?? []).map((c) => ({
          id: newRowId(), name: c.name, monthly: String(Math.round(c.monthly)),
          category: c.category, cats: [c.category] }))
      : [];
    setRows([{ id: newRowId(), name: "Food", monthly: String(foodVal || 0),
               food: true },
             ...existing, ...seeded]);
    const claimed = [...existing, ...seeded]
      .reduce((t, r) => t + numv(r.monthly), 0);
    setOtherBase(Math.max(pick(s0.other_monthly,
                               suggestions?.other_monthly),
                          Math.round(claimed)));
    const savedGoal = (s0.savings_goals ?? [])
      .find((g) => isPlanSavingsGoal(g.name));
    if (userSaved && savedGoal != null) {
      setSavings(String(savedGoal.monthly_plan ?? 0));
      setSavingsSeed(numv(String(savedGoal.monthly_plan ?? 0)));
      setSavingsTouched(true);
    }
    setFromHistory(!userSaved);
  }, [rows, sug.data, s0, wizard?.saved]);

  const save = useMutation({
    // every Budget-page list save reads the server fresh, merges and
    // writes the whole list back: one shared queue, so no two of them
    // interleave and erase each other's rows
    scope: SETTINGS_SAVES,
    // the rows being saved ride along, so success can tell whether the
    // person kept typing while the save was in flight
    mutationFn: async (sent: Row[] | null) => {
      // a plan worked out from a missing input is never saved
      if (holds) throw new Error(holds);
      // merge against a FRESH read — the planner owns only the
      // other-parent buckets and the "Savings" goal
      const cur = await client!.settingsFull();
      const body: Record<string, unknown> = {
        food_monthly: foodVal,
        other_monthly: unalloc + catsTotal,
      };
      // the typed figure is only the plan's income while nothing is
      // tracked; a tracked one is changed where it comes from, so the
      // stored stand-in is left as it was
      if (!tracked) body.budgeted_income_monthly = income ?? "0";
      // the rows were seeded at mount and the Bucket-rules card (or
      // another device) may have saved since: merge, never rebuild — see
      // mergePlannerBuckets for who wins what
      const merged = mergePlannerBuckets(sent ?? [],
                                         cur.custom_buckets ?? [], baseNames);
      if (merged.length || (cur.custom_buckets ?? []).length)
        body.custom_buckets = merged;
      // ALWAYS persist the Savings goal — a $0 plan is a remembered
      // choice, not an absence
      const others = (cur.savings_goals ?? [])
        .filter((g) => !isPlanSavingsGoal(g.name));
      const curGoal = (cur.savings_goals ?? [])
        .find((g) => isPlanSavingsGoal(g.name));
      // the stored goal keeps its own spelling: renaming it here would
      // be an edit the person never made
      body.savings_goals = [...others,
        { ...(curGoal ?? { name: "Savings", target: 0, tokens: [],
                           start_balance: 0 }),
          monthly_plan: numv(savingsShown) }];
      return saveSettings(qc, client!, body, cur);
    },
    onSuccess: (v, sent) => {
      // every edit makes a new rows array: the same array means nothing
      // was typed since the save began, and only then do the saved
      // buckets replace the rows — edits typed in flight stay, unsaved
      const edited = rowsNow.current !== sent;
      // the saved view is the new merge base; mid-edit, only the names
      // the sent rows held (see plannerBucketsSaved)
      const next = plannerBucketsSaved(v.custom_buckets ?? [],
                                       edited ? (sent ?? []) : undefined);
      setBaseNames(next.base);
      if (!edited && sent)
        setRows([...sent.filter((r) => r.food),
                 ...next.rows.map((r) => ({ ...r, id: newRowId() }))]);
      setMsg("Plan saved."); if (!edited) onDirty?.(false);
      onSaved();
    },
    onError: (e) => setMsg(errText(e)),
  });

  // Every figure below is worked out from the header's bills; until that
  // read has answered, none is shown and nothing is saved (the web
  // planner's rule). A first run also waits for the suggestion its rows
  // are filled in from.
  const waits = plannerWaits(inputPhase(now),
                             rows === null ? inputPhase(sug) : "ready");
  if (rows === null && !sug.data && sug.isError)
    return (
      <Card>
        <Text style={st.mut}>{PLANNER_HISTORY_FAILED}</Text>
        <Text style={{ color: C.accent, marginTop: 6 }}
              onPress={() => sug.refetch()}>Try again</Text>
      </Card>
    );
  if (rows === null || income === null || now.isPending)
    return <Card><Text style={st.mut}>loading…</Text></Card>;
  // the header's bills (month_status avg_bills): pending proposals count
  // once approved, never before (the web planner's rule)
  const bills = now.data?.avg_bills ?? null;
  const billsCount = now.data?.avg_bills_count ?? null;
  const foodVal = numv(rows.find((r) => r.food)?.monthly ?? "0");
  const catsTotal = rows.filter((r) => !r.food)
    .reduce((t, r) => t + numv(r.monthly), 0);
  // a typed savings amount comes out of Everything else so the plan
  // balances (the web planner's rule) — only what changed this session
  const unallocAuto = plannerUnallocAuto({ otherBase, catsTotal,
    savingsTouched, savingsTyped: numv(savings), savingsSeed });
  const unalloc = unallocEdited !== null
    ? numv(unallocEdited) : unallocAuto;
  // a tracked income is the server's figure; only a typed one is the box's
  const inc = tracked ? (now.data?.income ?? 0) : numv(income);
  // The excess is the server's (plan_surplus: every goal's plan comes off
  // it); the planner only moves it by what is typed and not yet saved
  // (plannerPreview, the web planner's rule). The goal this card owns
  // counts when the server counts it, or when it does not exist yet.
  const planGoals = now.data?.plan_goals ?? [];
  const ownedPlan = planGoals.find((g) => isPlanSavingsGoal(g.name));
  const ownedStored = (s0.savings_goals ?? [])
    .some((g) => isPlanSavingsGoal(g.name));
  const otherGoals = planGoals.filter((g) => !isPlanSavingsGoal(g.name)
    && g.monthly_plan > 0.005);
  const savedIncome = !tracked && now.data?.income_source === "typed"
    ? (now.data?.income ?? 0) : 0;
  const preview = (typedGoal: number) => plannerPreview({
    planSurplus: now.data?.plan_surplus ?? null,
    savingsPlan: now.data?.savings_plan ?? 0,
    savedVariable: now.data?.plan_variable ?? 0,
    typedVariable: foodVal + catsTotal + unalloc,
    savedGoal: ownedPlan?.monthly_plan ?? 0, typedGoal,
    goalCounted: !!ownedPlan || !ownedStored,
    savedIncome, typedIncome: tracked ? savedIncome : numv(income ?? ""),
    avgBills: bills ?? 0, incomeTyped: !tracked,
  });
  // The suggested savings is the server's, measured on the income an
  // ordinary month brings (never this month's own deposits): history's
  // figure when the plan was seeded from history, the saved plan's
  // otherwise (the web's rule). Nothing is worked out here; none sent —
  // an older server included — is no suggestion.
  // only the reply for what the box holds now is printed
  const savingsReply = !asksTyped ? sug.data
    : typedNow !== "" && typedAsked === typedNow ? sugTyped.data : undefined;
  const savingsSuggested = fromHistory
    ? savingsReply?.suggestions.savings_monthly : now.data?.savings_suggested;
  const savingsAuto = waits ? null : (savingsSuggested ?? null);
  // an untouched box is saved as the suggestion, so Save waits for the
  // reply to what is typed (a failed one is no suggestion, as before)
  const savingsWaits = asksTyped && !savingsTouched && typedNow !== ""
    && savingsReply === undefined && !sugTyped.isError
    ? SAVE_WAITS_LOADING : null;
  const holds = waits ?? savingsWaits;
  const savingsShown = savingsTouched ? savings
    : String(savingsAuto || "");
  const outTotal = (bills ?? 0) + foodVal + catsTotal + unalloc;
  const shown = preview(numv(savingsShown));
  // null: no income to plan on yet, so no excess figure to show
  const leftover = shown.excess === null ? null : Math.round(shown.excess);
  const over = leftover ?? 0;
  // the shares of the income, by the server's largest-remainder rule
  // (plannerShares, the web planner's): out, save and the excess add up
  // to 100 when they are the whole income
  const livePct = plannerShares(
    [outTotal, shown.saved, Math.max(over, 0)], inc).map((p) => p ?? 0);
  // at rest — the figures are the saved plan's — the plan line's own
  // whole dollars and shares (the web planner's rule); while something is
  // typed away from it, the live figures
  const rest = plannerAtRest(now.data?.plan_line,
    { income: inc, out: outTotal, saved: shown.saved, excess: over });
  const [outPct, savePct, overPct] = rest
    ? [rest.outPct, rest.savePct, rest.overPct] : livePct;
  const incShown = rest ? rest.income : inc;
  const outShown = rest ? rest.out : outTotal;
  const saveShown = rest ? rest.saved : shown.saved;
  const overShown = rest ? rest.excess : over;
  const zeroed = rows.filter((r) => r.name.trim() && numv(r.monthly) <= 0)
    .map((r) => r.name.trim());
  const candidates = [
    // Food is always re-addable — deleting it must not be one-way
    ...(!rows.some((r) => r.food)
      ? [{ name: "Food", category: "__food__",
           monthly: Math.round(
             sug.data?.suggestions.food_monthly ?? 0) }]
      : []),
    ...(sug.data?.bucket_candidates ?? []).filter((c) =>
      !rows.some((r) => r.category === c.category
        || (r.cats ?? []).includes(c.category)
        || r.name.toLowerCase() === c.name.toLowerCase())),
  ];
  const patch = (i: number, p: Partial<Row>) => {
    onDirty?.(true);
    setRows((rs) => rs!.map((r, j) => (j === i ? { ...r, ...p, touched: true }
                                              : r)));
  };

  return (
    <Card>
      <H>Your plan</H>
      {viewer && (
        <Text style={st.mut}>
          View-only access — the instance owner manages this.
        </Text>
      )}
      <Text style={st.mut}>
        Give every dollar a job — the numbers start from your own history.
      </Text>
      {fromHistory && historyBasisNote(sug.data) && (
        <Text style={[st.mut, { fontSize: 13 }]}>
          {historyBasisNote(sug.data)}
        </Text>
      )}

      {inc > 0 && !waits && (
        <View style={{ gap: 4, marginTop: 6 }}>
          <Text style={st.capLbl}>Where each dollar goes</Text>
          <FlowBar label={`in ${m$(incShown)} · 100%`} color={C.good} frac={1} />
          {/* the web's bars: money out in one segment per part (bills,
              each category, everything else), save beside the excess */}
          <SegBar label={`out ${m$(outShown)} · ${outPct}%`} inc={inc}
                  segs={[bills ?? 0,
                         ...rows.map((r) => numv(r.monthly)), unalloc]
                    .filter((v) => v > 0).map((v) => ({ v, color: C.warn }))} />
          <SegBar label={`save ${m$(saveShown)} · ${savePct}%`} inc={inc}
                  segs={[{ v: shown.saved, color: C.accent },
                         ...(over > 0 ? [{ v: over, color: C.border,
                                           hatched: true }] : [])]} />
          {over > 0 && (
            <Text style={st.mut}>
              Excess cash {m$(overShown)} · {overPct}% — stays in
              checking.
            </Text>
          )}
          {over < 0 && (
            <Text style={{ color: C.bad, fontSize: 12 }}>
              Plan exceeds income by {m$(-over)}
            </Text>
          )}
        </View>
      )}

      <Text style={[st.secHead, { color: C.good }]}>Money in</Text>
      {tracked ? (<>
        <Text style={st.lbl}>Money in this month (take-home)</Text>
        <Text style={{ color: C.text, fontSize: 18, fontWeight: "700" }}>
          {m$(incShown)}</Text>
        <Text style={st.mut}>
          {/* every deposit that landed, not paychecks alone: paychecks
              are named only beside the portion the server sends — the
              web planner's words */}
          {now.data?.income_source === "paychecks"
            ? "What landed this month"
              + ((now.data?.income_expected ?? 0) >= 0.5
                ? `, plus ${m$(now.data!.income_expected!)} of paychecks still due`
                : "")
              + ". Change them on Bills."
            : "From the active income scenario. Change it under Income scenarios."}
        </Text>
      </>) : (<>
        <Text style={st.lbl}>Expected income / month (take-home)</Text>
        <Text style={st.mut}>
          The plan counts this figure until a paycheck is tracked on
          Bills; then it counts this month&apos;s checks.
        </Text>
        <TextInput {...fieldA11y("Expected income / month (take-home)")}
                   style={st.input} keyboardType="decimal-pad"
                   editable={!viewer}
                   value={income}
                   onChangeText={(v) => { onDirty?.(true); setIncome(v); }} />
      </>)}

      <Text style={[st.secHead, { color: C.warn }]}>Money out</Text>
      <View style={st.rowLine}>
        <Text style={{ color: C.text, fontSize: 14, fontWeight: "700",
                       flex: 1 }}>
          {billsLabel}{" "}
          <Text style={[st.mut, { fontWeight: "400" }]}>
            ({billsCount !== null ? `${billsCount} tracked, ` : ""}evened out —{" "}
            {/* the wizard has a Back step instead (the web's words) */}
            {wizard ? "go Back to adjust" : (
              <Text style={{ color: C.accent }} accessibilityRole="link"
                    onPress={() => router.push("/bills" as never)}>adjust</Text>)})
          </Text>
        </Text>
        <Text style={{ color: C.text, fontSize: 14 }}>
          {bills === null ? UNKNOWN_FIGURE : m$(bills)}</Text>
      </View>
      <Text style={st.capLbl}>+ Budget categories</Text>
      {rows.map((r, i) => (
        <View key={r.id} style={st.rowLine}>
          <TextInput {...fieldA11y("Budget category name")}
                     style={[st.input, { flex: 1, paddingVertical: 6 }]}
                     placeholder="category name" placeholderTextColor={C.mut}
                     maxLength={40}
                     editable={!viewer && !r.food}
                     value={r.name}
                     onChangeText={(v) => patch(i, { name: v })} />
          {/* Food's name stays as it is (no save carries it), as on the
              web; the row itself can go, and is offered back below */}
          {!viewer && (
            <Text style={{ color: C.mut, fontSize: 16, padding: 4 }}
                  accessibilityRole="button" accessibilityLabel="Remove this category"
                  onPress={() => { onDirty?.(true);
                    setRows((rs) => rs!.filter((_, j) => j !== i)); }}>
              ✕
            </Text>
          )}
          <TextInput {...fieldA11y(`${r.name || "Category"} per month`)}
                     style={[st.input, { width: 90, paddingVertical: 6,
                                         textAlign: "right" }]}
                     keyboardType="decimal-pad" editable={!viewer}
                     value={r.monthly}
                     onChangeText={(v) => patch(i, { monthly: v })} />
          {rowHint(r)}
        </View>
      ))}
      {!viewer && candidates.map((c) => (
        <Text key={c.category} style={{ color: C.accent, fontSize: 12 }}
              onPress={() => { onDirty?.(true); setRows((rs) => [...rs!,
                c.category === "__food__"
                  ? { id: newRowId(), name: "Food",
                      monthly: String(c.monthly), food: true }
                  : { id: newRowId(), name: c.name,
                      monthly: String(c.monthly),
                      category: c.category, cats: [c.category] }]); }}>
          + {c.name} ~{m$(c.monthly)}/mo
        </Text>
      ))}
      {!viewer && (
        <Text style={{ color: C.accent, fontSize: 12 }}
              onPress={() => { onDirty?.(true); setRows((rs) =>
                [...rs!, { id: newRowId(), name: "", monthly: "" }]); }}>
          + add category
        </Text>
      )}
      <View style={[st.rowLine, { borderTopColor: C.border,
                                  borderTopWidth: StyleSheet.hairlineWidth,
                                  paddingTop: 6 }]}>
        <Text style={{ color: C.text, fontSize: 13, flex: 1 }}>
          Everything else{" "}
          <Text style={st.mut}>(unallocated — auto-shrinks as categories
            claim it)</Text>
        </Text>
        <TextInput {...fieldA11y("Everything else (unallocated) per month")}
                   style={[st.input, { width: 90, paddingVertical: 6,
                                       textAlign: "right" }]}
                   keyboardType="decimal-pad" editable={!viewer}
                   value={unallocEdited ?? String(unallocAuto || "")}
                   onChangeText={(v) => { onDirty?.(true);
                                          setUnallocEdited(v); }} />
        {hint(sug.data?.suggestions.unallocated_monthly)}
      </View>

      <Text style={[st.secHead, { color: C.accent }]}>
        Savings / Investment
      </Text>
      <Text style={st.lbl}>
        Save / month (what&apos;s left of the money in)
      </Text>
      <TextInput {...fieldA11y("Save / month (what's left of the money in)")}
                 style={st.input} keyboardType="decimal-pad"
                 editable={!viewer}
                 placeholder={savingsAuto === null ? UNKNOWN_FIGURE : undefined}
                 placeholderTextColor={C.mut}
                 value={savingsShown}
                 onChangeText={(v) => { onDirty?.(true); setSavings(v);
                                        setSavingsTouched(true); }} />
      {savingsTouched && savingsAuto !== null && savingsAuto > 0
        && savingsAuto !== numv(savingsShown) && !viewer && (
        <Text style={st.mut}>
          suggested {m$(savingsAuto)}{"  "}
          <Text style={{ color: C.accent }}
                onPress={() => setSavings(String(savingsAuto))}>
            use
          </Text>
        </Text>
      )}
      <Text style={st.mut}>
        {savingsTouched ? "Your number."
          : savingsAuto !== null
            ? "Suggested: what an ordinary month leaves."
            : "No suggestion yet."}
        {" Saved as your “Savings” goal."}
      </Text>
      {/* every other goal's plan comes off the excess too; they are edited
          under Savings goals, so they are listed, not typed */}
      {otherGoals.map((g) => (
        <View key={g.name} style={st.rowLine}>
          <Text style={[st.mut, { flex: 1 }]}>{g.name}</Text>
          <Text style={{ color: C.text, fontSize: 13 }}>
            {m$(g.monthly_plan)}/mo</Text>
        </View>
      ))}
      {leftover !== null && !waits && (
        <Text style={[st.mut, over < 0 ? { color: C.bad } : null]}>
          Excess cash {m$(over)}/mo — {over < 0
            ? "the plan spends more than comes in"
            : "unplanned; stays in checking"}.
        </Text>
      )}

      {zeroed.length > 0 && (
        <Text style={{ color: C.warn, fontSize: 12, marginTop: 6 }}>
          ⚠ {zeroed.join(", ")} {zeroed.length === 1 ? "has" : "have"} no
          budget — saving now leaves{" "}
          {zeroed.length === 1 ? "it" : "them"} unbudgeted (spending
          there won&apos;t count toward the verdict).
          {/* the web's rule: no food suggestion and no food history is the
              categorize pass not having caught up yet */}
          {!sug.data?.not_enough_history
            && !(sug.data?.suggestions.food_monthly ?? 0)
            ? " Smart categorization may still be running — reload in a minute for a suggestion."
            : ""}
        </Text>
      )}
      {msg ? (
        <Note bad={!(msg === "Plan saved.")} style={{ color: msg === "Plan saved." ? C.good : C.bad,
                       fontSize: 12, marginTop: 6 }}
              onPress={() => setMsg(null)}>
          {msg}
        </Note>
      ) : null}
      {(() => {
        if (saveRef) saveRef.current = {
          save: () => {
            if (holds) { setMsg(holds); return; }
            if (!save.isPending) save.mutate(rows);
          },
          saving: save.isPending };
        return null;
      })()}
      {!viewer && (
        <Pressable accessibilityRole="button" style={[st.btn, { alignSelf: "flex-start",
                     marginTop: 10 },
                     (save.isPending || !!holds) && { opacity: 0.5 },
                     offlineGrey(offline)]}
                   disabled={save.isPending || !!holds || offline}
                   onPress={() => save.mutate(rows)}>
          <Text style={st.btnText}>
            {save.isPending ? "saving…" : "Save budgets"}
          </Text>
        </Pressable>
      )}
      {!viewer && <OfflineNote />}
      {!viewer && holds && <Text style={st.mut}>{holds}</Text>}
    </Card>
  );
}

/** A bar of parts, each its share of the income — the web planner's
 *  out and save bars; an excess segment is drawn hollow (the web's
 *  hatching). Geometry only: every figure printed is in the label. */
function SegBar({ label, inc, segs }: {
  label: string; inc: number;
  segs: { v: number; color: string; hatched?: boolean }[];
}) {
  return (
    <View style={{ gap: 2 }}>
      <Text style={st.mut}>{label}</Text>
      <View style={[st.flowTrack, { flexDirection: "row", gap: 2,
                                    backgroundColor: "transparent" }]}>
        {segs.map((x, i) => (
          <View key={i} style={{
            width: `${inc > 0 ? Math.min(100, Math.max(0, 100 * x.v / inc)) : 0}%`,
            height: 8, borderRadius: 3,
            backgroundColor: x.hatched ? "transparent" : x.color,
            borderWidth: x.hatched ? 1 : 0, borderColor: x.color }} />
        ))}
      </View>
    </View>
  );
}

function FlowBar({ label, color, frac }: {
  label: string; color: string; frac: number;
}) {
  return (
    <View style={{ gap: 2 }}>
      <Text style={st.mut}>{label}</Text>
      <View style={st.flowTrack}>
        <View style={{ width: `${Math.min(100, Math.max(0, frac * 100))}%`,
                       backgroundColor: color, height: 8 }} />
      </View>
    </View>
  );
}

// ---- income scenarios ----
type ScenarioRow = ScenarioDraft & { id: string };

function IncomeScenarios({ s0, onSaved, onDirty }: {
  s0: BudgetSettings; onSaved: () => void;
  onDirty?: (v: boolean) => void;
}) {
  const { client } = useSession();
  // offline, the saves here are greyed with "You're offline." beside them
  const offline = useOffline();
  const qc = useQueryClient();
  const [rows, setRows] = useState<ScenarioRow[]>(
    () => (s0.income_scenarios ?? []).map((r) => (
      { id: newRowId(), name: r.name, take_home: String(r.take_home),
        cadence: r.cadence })));
  const [active, setActive] = useState(s0.active_scenario ?? "");
  // the copy this card was seeded from: the merge base at save time
  const baseline = useRef(s0.income_scenarios ?? []);
  const baseActive = useRef(s0.active_scenario ?? "");
  // what is saved now: the one "Active now" speaks for
  const [saved, setSaved] = useState({
    list: s0.income_scenarios ?? [], active: s0.active_scenario ?? "" });
  const [msg, setMsg] = useState<string | null>(null);
  // every draft edit marks the card dirty so a sibling save's remount
  // loop skips it — otherwise the remount wipes the unsaved scenario
  const patch = (i: number, p: Partial<ScenarioRow>) => {
    onDirty?.(true);
    setRows((rs) => rs.map(
      (x, j) => (j === i ? { ...x, ...p, touched: true } : x)));
  };
  const save = useMutation({
    // every Budget-page list save reads the server fresh, merges and
    // writes the whole list back: one shared queue, so no two of them
    // interleave and erase each other's rows
    scope: SETTINGS_SAVES,
    mutationFn: async () => {
      // The server replaces the list wholesale, and these rows were
      // seeded at mount; merge against its CURRENT list so a scenario
      // added or edited on another device since is not erased.
      const cur = await client!.settingsFull();
      const body: Record<string, unknown> = {
        income_scenarios: mergeIncomeScenarios(
          rows, baseline.current, cur.income_scenarios ?? []),
      };
      // the active pick is sent only when it was changed here; omitted,
      // the server keeps whichever one is live now
      if (active !== baseActive.current) body.active_scenario = active;
      return saveSettings(qc, client!, body, cur);
    },
    onSuccess: (v) => {
      // the saved view is the new merge base, and the card shows what was
      // merged in from elsewhere rather than its own pre-save rows
      const scen = v.income_scenarios ?? [];
      baseline.current = scen;
      baseActive.current = v.active_scenario ?? "";
      setSaved({ list: scen, active: v.active_scenario ?? "" });
      setRows(scen.map((x) => ({ id: newRowId(), name: x.name,
        take_home: String(x.take_home), cadence: x.cadence })));
      setActive(v.active_scenario ?? "");
      // the rows above were just replaced by the saved view
      setMsg("Income scenario saved."); onDirty?.(false); onSaved();
    },
    onError: (e) => setMsg(errText(e)),
  });
  const CAD = ["weekly", "biweekly", "semimonthly", "monthly"];
  // "Active now" is the SAVED active scenario's month, as the server
  // converts it for the plan (each scenario's `monthly`); an unsaved edit
  // changes nothing until it is saved, and a server without the figure
  // gets no line rather than a second conversion worked out here (web:
  // IncomeScenarioCard.tsx)
  const live = saved.list.find((x) => x.name === saved.active);
  // older-server path: stands in for `monthly` (each scenario's; the
  // server's own CADENCE_MONTHLY conversion, on the saved active one)
  const MULT: Record<string, number> =
    { weekly: 4, biweekly: 2, semimonthly: 2, monthly: 1 };
  const liveMonthly = live
    ? live.monthly !== undefined ? live.monthly
      : (live.take_home ? live.take_home * (MULT[live.cadence] ?? 2) : null)
    : null;
  return (
    <Card>
      <H>
        Income scenarios{" "}
        <Text style={[st.mut, { fontWeight: "400" }]}>
          — take-home per paycheck
        </Text>
      </H>
      {rows.map((r, i) => (
        <View key={r.id} style={{ gap: 4, borderTopColor: C.border,
                               borderTopWidth: StyleSheet.hairlineWidth,
                               paddingVertical: 6 }}>
          <View style={st.rowLine}>
            <Text style={{ color: active === r.name ? C.accent : C.mut,
                           fontSize: 16 }}
                  accessibilityRole="radio" accessibilityLabel="Use this income"
                  accessibilityState={{ checked: active === r.name }}
                  onPress={() => { onDirty?.(true); setActive(r.name); }}>
              {active === r.name ? "◉" : "○"}
            </Text>
            <TextInput {...fieldA11y("Income scenario name")}
                       style={[st.input, { flex: 1, paddingVertical: 6 }]}
                       placeholder="full salary" placeholderTextColor={C.mut}
                       value={r.name}
                       onChangeText={(v) => {
                         if (active === r.name) setActive(v);
                         patch(i, { name: v });
                       }} />
            <TextInput {...fieldA11y(`${r.name || "Scenario"} take-home`)}
                       style={[st.input, { width: 90, paddingVertical: 6,
                                           textAlign: "right" }]}
                       keyboardType="decimal-pad" value={r.take_home}
                       onChangeText={(v) => patch(i, { take_home: v })} />
            <Text style={{ color: C.mut, fontSize: 16, padding: 4 }}
                  accessibilityRole="button" accessibilityLabel="Remove this income"
                  onPress={() => { onDirty?.(true);
                    setRows((rs) => rs.filter((_, j) => j !== i)); }}>
              ✕
            </Text>
          </View>
          <View style={{ flexDirection: "row", gap: 6, marginLeft: 26 }}>
            {CAD.map((c) => (
              <Pressable accessibilityRole="button" key={c}
                         style={[st.chip, r.cadence === c && st.chipOn]}
                         onPress={() => patch(i, { cadence: c })}>
                <Text style={{ color: r.cadence === c ? C.text : C.mut,
                               fontSize: 11 }}>{c}</Text>
              </Pressable>
            ))}
          </View>
        </View>
      ))}
      <Text style={{ color: C.accent, fontSize: 12 }}
            onPress={() => { onDirty?.(true);
              setRows((rs) =>
                [...rs, { id: newRowId(), name: "", take_home: "",
                          cadence: "biweekly", touched: true }]); }}>
        + Add scenario
      </Text>
      <Text style={st.mut}>
        {live && liveMonthly != null
          ? `Active now: ${m$(liveMonthly)}/mo (${live.name}). ` : ""}
        The active scenario caps that paycheck in the cash forecast and
        daily email.
      </Text>
      {msg ? (
        <Note bad={!(msg.includes("saved"))} style={{ color: msg.includes("saved") ? C.good : C.bad,
                       fontSize: 12 }}
              onPress={() => setMsg(null)}>{msg}</Note>
      ) : null}
      <Pressable accessibilityRole="button"
                 style={[st.btn, { alignSelf: "flex-start", marginTop: 8 },
                   save.isPending && { opacity: 0.5 }, offlineGrey(offline)]}
                 disabled={save.isPending || offline}
                 onPress={() => save.mutate()}>
        <Text style={st.btnText}>
          {save.isPending ? "saving…" : "Save income scenarios"}
        </Text>
      </Pressable>
      <OfflineNote />
    </Card>
  );
}

// ---- savings goals ----
// the draft-row shape lives in pure.ts beside the merge it feeds; `id` is
// local — a stable React key that mergeSavingsGoals never needs to see
type GoalRow = GoalDraft & { id: string };

// the account chips are identical for every goal and every keystroke —
// without memo each character typed in any goal field re-rendered the
// whole account list once PER GOAL. Props stay referentially stable
// (react-query's array + a useCallback handler), so memo skips them
// unless the accounts or this row's selection actually changed.
const GoalAccountPicker = memo(function GoalAccountPicker({
  accounts, index, selected, onPick }: {
  accounts: Account[] | undefined; index: number; selected: string;
  onPick: (i: number, id: string) => void;
}) {
  return (
    <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
      <Pressable accessibilityRole="button" style={[st.chip, !selected && st.chipOn]}
                 onPress={() => onPick(index, "")}>
        <Text style={{ color: !selected ? C.text : C.mut,
                       fontSize: 11 }}>no account (plan only)</Text>
      </Pressable>
      {/* open cash and investment accounts a transfer can reach (the
          server's goal_destination; an older server sends none, so all),
          with their last four — and the one already chosen, whatever it
          is, so a goal bound to a hidden account still shows it */}
      {(accounts ?? []).filter((a) => a.goal_destination !== false
                                      || a.id === selected).map((a) => (
        <Pressable accessibilityRole="button" key={a.id}
                   style={[st.chip, selected === a.id && st.chipOn]}
                   onPress={() => onPick(index, a.id)}>
          <Text style={{ color: selected === a.id ? C.text : C.mut,
                         fontSize: 11 }}>
            {a.name}{a.mask ? ` ···${a.mask}` : ""}
          </Text>
        </Pressable>
      ))}
    </View>
  );
});

function SavingsGoals({ s0, onSaved, onDirty }: {
  s0: BudgetSettings; onSaved: () => void;
  onDirty?: (v: boolean) => void;
}) {
  const { client } = useSession();
  // offline, the saves here are greyed with "You're offline." beside them
  const offline = useOffline();
  const qc = useQueryClient();
  const baseline = useRef((s0.savings_goals ?? []).map((g) => g.name));
  const accounts = useQuery({ queryKey: ["accounts"],
    queryFn: () => client!.accounts(), enabled: !!client });
  const today = useQuery({ queryKey: ["today"],
    queryFn: () => client!.todayFull(), enabled: !!client });
  const goalRow = (g: NonNullable<BudgetSettings["savings_goals"]>[number]): GoalRow => ({
    id: newRowId(), name: g.name, target: String(g.target ?? ""),
    target_date: g.target_date ?? "",
    monthly_plan: String(g.monthly_plan ?? ""),
    account_id: g.account_id ?? "",
    tokens: (g.tokens ?? []).join(", "),
    start_balance: String(g.start_balance || ""),
    mode: g.mode === "sweep" ? "sweep" : "monthly" });
  const [rows, setRows] = useState<GoalRow[]>(
    () => (s0.savings_goals ?? []).map(goalRow));
  const [msg, setMsg] = useState<string | null>(null);
  // the rows on screen now, for a save that lands after more typing
  const rowsNow = useRef(rows);
  rowsNow.current = rows;
  const patch = (i: number, p: Partial<GoalRow>) => {
    onDirty?.(true);
    setRows((rs) => rs.map((r, j) =>
      j === i ? { ...r, ...p, touched: true } : r));
  };
  // stable identity so the memoized per-goal account picker's props
  // don't change on every keystroke
  const pickAccount = useCallback((i: number, id: string) => {
    onDirty?.(true);
    setRows((rs) => rs.map((r, j) =>
      j === i ? { ...r, account_id: id, touched: true } : r));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // the plan's Savings line: "no account" is where it already is, so only
  // a real account binds it (and opens it as a goal)
  const pickPlanAccount = useCallback((i: number, id: string) => {
    if (id) pickAccount(i, id);
  }, [pickAccount]);
  const save = useMutation({
    // every Budget-page list save reads the server fresh, merges and
    // writes the whole list back: one shared queue, so no two of them
    // interleave and erase each other's rows
    scope: SETTINGS_SAVES,
    // the rows being saved ride along as the variables, so success can
    // tell whether the person kept typing while the save was in flight
    mutationFn: async (sent: GoalRow[]) => {
      // fresh-read merge: untouched rows defer to the server's copy;
      // rows added elsewhere are carried through (logic in pure.ts so
      // node can exercise it — it's the only lost-update guard here)
      const cur = await client!.settingsFull();
      return saveSettings(qc, client!, {
        savings_goals: mergeSavingsGoals(sent, cur.savings_goals ?? [],
                                         baseline.current) }, cur);
    },
    onSuccess: (v, sent) => {
      // the saved view is the new merge base: a goal added here and saved
      // is no longer "added elsewhere", so removing or renaming it in a
      // later save must not carry it back through
      const goals = v.savings_goals ?? [];
      // every edit makes a new rows array: the same array means nothing
      // was typed since the save began, and only then does the saved view
      // replace the rows — edits typed in flight stay, still unsaved
      const edited = rowsNow.current !== sent;
      // mid-edit, only the names the sent rows held (see savedMergeBase)
      baseline.current = savedMergeBase(goals.map((g) => g.name),
        edited ? sent : undefined, baseline.current);
      if (!edited) setRows(goals.map(goalRow));
      setMsg("Savings goals saved."); if (!edited) onDirty?.(false);
      onSaved();
    },
    onError: (e) => setMsg(errText(e)),
  });
  return (
    <Card>
      <H>
        Savings goals{" "}
        <Text style={[st.mut, { fontWeight: "400" }]}>
          — trip fund, emergency fund; progress comes from real transfers
        </Text>
      </H>
      <Text style={st.mut}>
        A goal tracks transfers into its account; its monthly plan comes
        out of excess cash.
      </Text>
      {rows.map((r, i) => {
        const p = (today.data?.savings_goals ?? [])
          .find((x) => x.name === r.name.trim());
        // the budget plan's own Savings line, with no account to measure
        // it yet: its amount is set on the planner, and the one thing to
        // choose here is the account it lands in — once chosen it is a
        // goal like any other (web: SavingsGoalsCard); unbound, it rides
        // along untouched on a save
        if (isPlanOnlySavingsGoal(r) && !r.touched)
          return (
            <View key={r.id} style={{ paddingVertical: 6, gap: 6 }}>
              <Text style={st.mut}>
                <Text style={{ color: C.text, fontWeight: "600" }}>Savings</Text>
                {" "}— the plan&apos;s savings line
                {Number(r.monthly_plan) > 0
                  ? ` (${m$(Number(r.monthly_plan))}/month)` : ""}, set on the
                planner above. Pick the account it lands in to measure it.
              </Text>
              {accounts.data === undefined && accounts.isError ? (
                <FailedLine q={accounts} what="the accounts" />
              ) : (
              <GoalAccountPicker accounts={accounts.data?.accounts}
                                 index={i} selected=""
                                 onPick={pickPlanAccount} />)}
            </View>);
        return (
        <View key={r.id} style={{ borderTopColor: C.border,
                               borderTopWidth: StyleSheet.hairlineWidth,
                               paddingVertical: 8, gap: 6 }}>
          <View style={st.rowLine}>
            <TextInput {...fieldA11y("Savings goal name")}
                       style={[st.input, { flex: 1, paddingVertical: 6 }]}
                       placeholder="Trip fund" placeholderTextColor={C.mut}
                       value={r.name}
                       onChangeText={(v) => patch(i, { name: v })} />
            <Text style={{ color: C.mut, fontSize: 16, padding: 4 }}
                  accessibilityRole="button" accessibilityLabel="Remove this goal"
                  onPress={() => {
                    // deleting is an edit — without the dirty mark a
                    // sibling save remounts this card and the deleted
                    // goal quietly comes back
                    onDirty?.(true);
                    setRows((rs) => rs.filter((_, j) => j !== i));
                  }}>
              ✕
            </Text>
          </View>
          <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
            <Field label="Target $" value={r.target}
                   onChange={(v) => patch(i, { target: v })} num />
            <Field label="Target date (opt.)" value={r.target_date}
                   onChange={(v) => patch(i, { target_date: v })}
                   placeholder="YYYY-MM-DD" />
            <Field label="Plan $/month" value={r.monthly_plan}
                   onChange={(v) => patch(i, { monthly_plan: v })} num />
            <Field label="Start balance $" value={r.start_balance}
                   onChange={(v) => patch(i, { start_balance: v })} num />
            {/* words only carve a goal out of an account; with no
                account they stay editable only so existing ones can be
                cleared (the web editor's rule) */}
            <Field label="Match words (opt.)" value={r.tokens}
                   onChange={(v) => patch(i, { tokens: v })}
                   placeholder="trip" wide
                   disabled={!r.account_id && !goalHasWords(r)} />
          </View>
          {/* contribution mode — the web editor's wording, verbatim */}
          <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
            {([["monthly", "every month (fixed plan)"],
               ["sweep", "only when the month worked out (sweep)"]] as
               const).map(([val, label]) => (
              <Pressable accessibilityRole="button" key={val}
                         style={[st.chip, r.mode === val && st.chipOn]}
                         onPress={() => patch(i, { mode: val })}>
                <Text style={{ color: r.mode === val ? C.text : C.mut,
                               fontSize: 11 }}>{label}</Text>
              </Pressable>
            ))}
          </View>
          {/* an unread account list is not "no account": the failed
              line in the picker's place */}
          {accounts.data === undefined && accounts.isError ? (
            <FailedLine q={accounts} what="the accounts" />
          ) : (
          <GoalAccountPicker accounts={accounts.data?.accounts}
                             index={i} selected={r.account_id}
                             onPick={pickAccount} />)}
          {goalNeedsAccount(r) && (
            <Text style={{ color: C.bad, fontSize: 12 }}>
              Match words need an account — pick the account the money
              lands in.
            </Text>
          )}
          {p && p.pct != null && (
            <Text style={st.mut}>
              {money(p.saved, false)} of {money(p.target, false)} ({p.pct}%)
              {/* A sweep goal contributes in one lump at month end, so
                  the last-3-months pace text reads "off pace" all
                  month on a goal that is fine. Say what this goal's
                  month actually did instead — the Today line's wording,
                  per goal, since the pooled sentence there cannot tell
                  two sweep goals apart. */}
              {p.mode === "sweep" && p.swept_this_month != null
                ? (p.sweep_satisfied
                    ? ` — swept ${m$(p.swept_this_month)} this month`
                    : ` — swept ${m$(p.swept_this_month)} of `
                      + `${m$(p.monthly_plan)} this month`)
                : p.on_pace === false ? " — off pace"
                : p.on_pace ? " — on pace"
                : p.eta && p.pct < 100 ? ` — ~${mmdd(p.eta)}` : ""}
            </Text>
          )}
        </View>
        );
      })}
      <Text style={{ color: C.accent, fontSize: 12 }}
            onPress={() => { onDirty?.(true); setRows((rs) => [...rs,
              { id: newRowId(), name: "", target: "", target_date: "",
                monthly_plan: "", account_id: "", tokens: "",
                start_balance: "", mode: "monthly", touched: true }]); }}>
        + Add goal
      </Text>
      {msg ? (
        <Note bad={!(msg.includes("saved"))} style={{ color: msg.includes("saved") ? C.good : C.bad,
                       fontSize: 12 }}
              onPress={() => setMsg(null)}>{msg}</Note>
      ) : null}
      <Pressable accessibilityRole="button"
                 style={[st.btn, { alignSelf: "flex-start", marginTop: 8 },
                   save.isPending && { opacity: 0.5 }, offlineGrey(offline)]}
                 disabled={save.isPending || offline}
                 onPress={() => save.mutate(rows)}>
        <Text style={st.btnText}>
          {save.isPending ? "saving…" : "Save savings goals"}
        </Text>
      </Pressable>
      <OfflineNote />
    </Card>
  );
}

function Field({ label, value, onChange, num, placeholder, wide,
                disabled }: {
  label: string; value: string; onChange: (v: string) => void;
  num?: boolean; placeholder?: string; wide?: boolean; disabled?: boolean;
}) {
  return (
    <View style={{ width: wide ? "100%" : "47%" }}>
      <Text style={{ color: C.mut, fontSize: 10 }}>{label}</Text>
      <TextInput {...fieldA11y(label)} style={[st.input, { paddingVertical: 5 },
                         disabled && { opacity: 0.5 }]}
                 keyboardType={num ? "decimal-pad" : "default"}
                 placeholder={placeholder} placeholderTextColor={C.mut}
                 autoCapitalize="none" editable={!disabled}
                 value={value} onChangeText={onChange} />
    </View>
  );
}

// ---- bucket rules (fine-tune) ----
// `id` is local — a stable React key that mergeBucketRules never needs
type BucketRow = BucketRuleDraft & { id: string };
const bucketRows = (saved: readonly CustomBucket[]): BucketRow[] =>
  bucketRulesSaved(saved).rows.map((r) => ({ ...r, id: newRowId() }));

function BucketRules({ s0, onSaved, onDirty }: {
  s0: BudgetSettings; onSaved: () => void;
  onDirty?: (v: boolean) => void;
}) {
  const { client } = useSession();
  // offline, the saves here are greyed with "You're offline." beside them
  const offline = useOffline();
  const qc = useQueryClient();
  // the server copy the rows were last seeded from — at mount, then each
  // saved view: the save-time merge base
  const baseline = useRef(bucketRulesSaved(s0.custom_buckets ?? []).base);
  const [editing, setEditingRaw] = useState<number | null>(null);
  // the category chips only render inside an open row editor — no need
  // to fetch the tenant's whole category list before the first one opens
  const [everEdited, setEverEdited] = useState(false);
  const setEditing = (i: number | null) => {
    if (i !== null) setEverEdited(true);
    setEditingRaw(i);
  };
  const cats = useQuery({ queryKey: ["categories"],
    queryFn: () => client!.categories(), enabled: !!client && everEdited });
  const [rows, setRows] = useState<BucketRow[]>(
    () => bucketRows(s0.custom_buckets ?? []));
  // the rows on screen now, for a save that lands after more typing
  const rowsNow = useRef(rows);
  rowsNow.current = rows;
  // absent means on, the way the budget engine reads it: an unchecked
  // switch here would be posted back as false by the next bucket save
  const [dynamic, setDynamic] =
    useState(s0.dynamic_variable_budget !== false);
  // the Switch flips before the save; this is what it falls back to when
  // the server says no
  const committedDyn = useRef(s0.dynamic_variable_budget !== false);
  const [msg, setMsg] = useState<string | null>(null);
  const save = useMutation({
    // every Budget-page list save reads the server fresh, merges and
    // writes the whole list back: one shared queue, so no two of them
    // interleave and erase each other's rows
    scope: SETTINGS_SAVES,
    // the rows being saved ride along with the switch's value, so
    // success can tell whether the person kept typing in flight
    mutationFn: async ({ dyn, sent }: { dyn: boolean; sent: BucketRow[] }) => {
      // fresh-read merge (pure.ts, shared with the web card): touched rows
      // win, untouched rows follow the server's copy, buckets added
      // elsewhere since the base are carried through
      const cur = await client!.settingsFull();
      const out = mergeBucketRules(sent, cur.custom_buckets ?? [],
                                   baseline.current, savedAmount);
      // partial body on purpose — food/other/income/savings belong to
      // the planner
      return saveSettings(qc, client!, { dynamic_variable_budget: dyn,
                                         custom_buckets: out }, cur);
    },
    onSuccess: (v, { dyn, sent }) => {
      committedDyn.current = dyn;
      // every edit makes a new rows array: the same array means nothing
      // was typed since the save began, and only then does the saved view
      // replace the rows — edits typed in flight stay, still unsaved
      const edited = rowsNow.current !== sent;
      // the saved view is the new merge base; mid-edit, only the names
      // the sent rows held (see bucketRulesSaved)
      const saved = v.custom_buckets ?? [];
      baseline.current = bucketRulesSaved(saved, edited ? sent : undefined,
                                          baseline.current).base;
      if (!edited) setRows(bucketRows(saved));
      setMsg("Bucket rules saved."); if (!edited) onDirty?.(false);
      onSaved();
    },
    onError: (e) => {
      setDynamic(committedDyn.current);
      setMsg(errText(e));
    },
  });
  const patch = (i: number, p: Partial<BucketRow>) => {
    onDirty?.(true);
    setRows((rs) => rs.map((r, j) =>
      j === i ? { ...r, ...p, touched: true } : r));
  };
  // median-of-history suggestions, approve-each — never auto-applied
  const [sugg, setSugg] = useState<Record<string, number> | null>(null);
  // how many months the medians are taken over (the web's footnote)
  const [suggMonths, setSuggMonths] = useState<number | null>(null);
  const suggest = useMutation({
    mutationFn: () => client!.budgetsSuggest(),
    onSuccess: (r) => { setSugg(r.suggestions.buckets ?? {});
                        setSuggMonths(r.months.length); },
    onError: (e) => setMsg(errText(e)),
  });
  return (
    <Card>
      <H>
        Bucket rules{" "}
        <Text style={[st.mut, { fontWeight: "400" }]}>
          — what counts toward each bucket; a merchant match wins
        </Text>
      </H>
      {rows.length === 0 && (
        <Text style={st.mut}>
          No buckets yet — a bucket carves named spend out of Food or
          Everything else, and shows as its own bar on Today.
        </Text>
      )}
      {rows.map((r, i) => editing === i ? (
        <View key={r.id} style={{ borderTopColor: C.border,
                               borderTopWidth: StyleSheet.hairlineWidth,
                               paddingVertical: 8, gap: 6 }}>
          <View style={st.rowLine}>
            <TextInput {...fieldA11y("Bucket name")}
                       style={[st.input, { flex: 1, paddingVertical: 6 }]}
                       placeholder="Coffee" placeholderTextColor={C.mut}
                       maxLength={40}
                       value={r.name}
                       onChangeText={(v) => patch(i, { name: v })} />
            <TextInput {...fieldA11y(`${r.name || "Bucket"} per month`)}
                       style={[st.input, { width: 80, paddingVertical: 6,
                                           textAlign: "right" }]}
                       keyboardType="decimal-pad" value={r.monthly}
                       onChangeText={(v) => patch(i, { monthly: v })} />
            <Text style={st.mut}>/mo</Text>
          </View>
          <View style={{ flexDirection: "row", gap: 6 }}>
            {(["other", "food"] as const).map((p) => (
              <Pressable accessibilityRole="button"
                         key={p} style={[st.chip, r.parent === p && st.chipOn]}
                         onPress={() => patch(i, { parent: p })}>
                <Text style={{ color: r.parent === p ? C.text : C.mut,
                               fontSize: 11 }}>
                  from {p === "food" ? "Food" : "Everything else"}
                </Text>
              </Pressable>
            ))}
          </View>
          <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
            {r.categories.map((c) => (
              <Pressable accessibilityRole="button" key={c} style={st.chip}
                         onPress={() => patch(i, { categories:
                           r.categories.filter((x) => x !== c) })}>
                <Text style={{ color: C.text, fontSize: 11 }}>
                  {catLabel(c)} ✕
                </Text>
              </Pressable>
            ))}
            {(cats.data?.categories ?? [])
              .filter((c) => !r.categories.includes(c))
              .map((c) => (
              <Pressable accessibilityRole="button" key={c} style={[st.chip, { opacity: 0.7 }]}
                         onPress={() => patch(i, { categories:
                           [...r.categories, c] })}>
                <Text style={{ color: C.mut, fontSize: 11 }}>
                  ＋ {catLabel(c)}
                </Text>
              </Pressable>
            ))}
          </View>
          {sugg && sugg[r.name.trim()] != null && (
            <Text style={st.mut}>
              suggested {m$(sugg[r.name.trim()])}{"  "}
              <Text style={{ color: C.accent }}
                    onPress={() => patch(i, { monthly:
                      String(sugg[r.name.trim()]) })}>
                use
              </Text>
            </Text>
          )}
          <View style={st.rowLine}>
            <TextInput {...fieldA11y(`${r.name || "Bucket"} merchants, comma-separated`)}
                       style={[st.input, { flex: 1, paddingVertical: 6 }]}
                       placeholder="＋ merchant, comma-separated"
                       placeholderTextColor={C.mut} autoCapitalize="none"
                       value={r.merchants}
                       onChangeText={(v) => patch(i, { merchants: v })} />
          </View>
          <View style={{ flexDirection: "row", gap: 8 }}>
            <Pressable accessibilityRole="button"
                       style={[st.btn, save.isPending && { opacity: 0.5 },
                               offlineGrey(offline)]}
                       disabled={save.isPending || offline}
                       onPress={() => { setEditing(null);
                                        save.mutate({ dyn: dynamic,
                                                      sent: rows }); }}>
              <Text style={st.btnText}>save</Text>
            </Pressable>
            <Pressable accessibilityRole="button" style={[st.btn, st.btnQuiet]}
                       onPress={() => setEditing(null)}>
              <Text style={[st.btnText, { color: C.mut }]}>cancel</Text>
            </Pressable>
            <Pressable accessibilityRole="button" style={[st.btn, st.btnQuiet]}
                       onPress={() => { onDirty?.(true); setRows((rs) =>
                         rs.filter((_, j) => j !== i));
                         setEditing(null); }}>
              <Text style={[st.btnText, { color: C.bad }]}>remove</Text>
            </Pressable>
          </View>
        </View>
      ) : (
        <View key={r.id} style={[st.rowLine, { borderTopColor: C.border,
                       borderTopWidth: StyleSheet.hairlineWidth,
                       paddingVertical: 8 }]}>
          <View style={{ flex: 1 }}>
            <Text style={{ color: C.text, fontSize: 13,
                           fontWeight: "700" }}>
              {r.name || "(unnamed)"}{" "}
              <Text style={[st.mut, { fontWeight: "400" }]}>
                {m$(numv(r.monthly))}/mo from{" "}
                {r.parent === "food" ? "Food" : "Everything else"}
              </Text>
            </Text>
            <Text style={st.mut} numberOfLines={1}>
              {[...r.categories.map((c) => catLabel(c)),
                ...r.merchants.split(",").map((x) => x.trim())
                  .filter(Boolean)].join(" · ") || "nothing matched yet"}
            </Text>
          </View>
          <Text style={{ color: C.accent, fontSize: 14, padding: 6 }}
                accessibilityRole="button" accessibilityLabel={`Edit ${r.name || "this budget"}`}
                onPress={() => setEditing(i)}>✎</Text>
        </View>
      ))}
      <View style={{ flexDirection: "row", gap: 16 }}>
        <Text style={{ color: C.accent, fontSize: 12 }}
              onPress={() => { onDirty?.(true);
                setRows((rs) => [...rs,
                { id: newRowId(), name: "", parent: "other", monthly: "",
                  categories: [], merchants: "", touched: true }]);
                setEditing(rows.length); }}>
          + add bucket
        </Text>
        <Text style={{ color: C.accent, fontSize: 12 }}
              onPress={() => !suggest.isPending && suggest.mutate()}>
          {suggest.isPending ? "computing…" : "Suggest from history"}
        </Text>
      </View>
      {sugg && (
        <Text style={st.mut}>
          Suggestions are the median of the last {suggMonths} months.
        </Text>
      )}
      <View style={[st.rowLine, { marginTop: 6 }]}>
        <Text style={{ color: C.text, fontSize: 13, flex: 1 }}>
          Dynamic budget{" "}
          <Text style={st.mut}>— adapts to the month so far</Text>
        </Text>
        <Switch value={dynamic} disabled={save.isPending || offline}
                accessibilityLabel="Dynamic budget"
                onValueChange={(v) => { setDynamic(v);
                                         save.mutate({ dyn: v, sent: rows }); }}
                trackColor={{ true: C.accent, false: C.hover }}
                thumbColor={C.text} />
      </View>
      <OfflineNote />
      {msg ? (
        <Note bad={!(msg.includes("saved"))} style={{ color: msg.includes("saved") ? C.good : C.bad,
                       fontSize: 12 }}
              onPress={() => setMsg(null)}>{msg}</Note>
      ) : null}
    </Card>
  );
}

const st = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  center: { color: C.mut, textAlign: "center", padding: 20 },
  mut: { color: C.mut, fontSize: 12, lineHeight: 17 },
  lbl: { color: C.mut, fontSize: 12, marginTop: 6, marginBottom: 2 },
  capLbl: { color: C.mut, fontSize: 11, textTransform: "uppercase",
            letterSpacing: 1, marginTop: 8 },
  secHead: { fontSize: 14, fontWeight: "700", marginTop: 12 },
  input: { backgroundColor: C.bg, borderColor: C.field, borderWidth: 1,
           borderRadius: 8, color: C.text, fontSize: 14,
           paddingHorizontal: 10, paddingVertical: 8 },
  rowLine: { flexDirection: "row", alignItems: "center", gap: 8,
             marginTop: 4 },
  btn: { backgroundColor: C.btn, borderRadius: 8,
         paddingHorizontal: 16, paddingVertical: 8 },
  btnQuiet: { backgroundColor: C.hover },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
  chip: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
          borderRadius: 999, paddingHorizontal: 9, paddingVertical: 4 },
  chipOn: { borderColor: C.accent, backgroundColor: C.hover },
  flowTrack: { backgroundColor: C.border, borderRadius: 4, height: 8,
               overflow: "hidden" },
});
