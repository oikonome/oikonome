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

import MoneyMap, { planRows } from "../components/money-map";
import StaleBanner from "../components/stale-banner";
import { Card, H, HelpLink } from "../components/ui";
import { errText, Account, BudgetSettings, CustomBucket } from "../lib/api";
import { saveSettings } from "../lib/cache";
import { mergeSavingsGoals, numv, type GoalDraft, catLabel } from "../lib/pure";
import { useSession } from "../lib/session";
import { useViewer } from "../lib/viewer";
import { C, money } from "../lib/theme";

const m$ = (n: number) => money(n, false);

// stable per-row ids for the draft lists below (Planner categories,
// income scenarios, savings goals, bucket rules). Keying on array index
// breaks mid-edit deletes: React reuses the deleted row's TextInputs for
// its successor, so keyboard focus and the native selection stay put
// on screen while the value under them shifts up. A counter, not
// Date.now() — two rows added in the same render tick need distinct ids.
let rowIdSeq = 0;
const newRowId = () => `row${++rowIdSeq}`;

export default function Budget() {
  const { client } = useSession();
  const viewer = useViewer();
  const qc = useQueryClient();
  const router = useRouter();
  const today = useQuery({ queryKey: ["today"],
    queryFn: () => client!.todayFull(), enabled: !!client });
  const settings = useQuery({ queryKey: ["settings"],
    queryFn: () => client!.settingsFull(), enabled: !!client });
  const ob = useQuery({ queryKey: ["onboarding"],
    queryFn: () => client!.onboarding(), enabled: !!client,
    staleTime: 5 * 60_000 });
  const d = today.data;
  const s0 = settings.data;
  // after a save the SIBLING cards remount so their mount-time seeds
  // pick up fresh settings — but a sibling holding unsaved edits is
  // skipped: the remount would silently discard the draft (web rule)
  const [gens, setGens] = useState({ p: 0, i: 0, g: 0, b: 0 });
  const dirty = useRef({ p: false, i: false, g: false, b: false });
  const markDirty = (k: "p" | "i" | "g" | "b") => (v: boolean) => {
    dirty.current[k] = v;
  };
  // every save here goes through saveSettings, which has already put the
  // server's reply into ["settings"] by the time this runs — so the
  // siblings remount onto fresh settings without a refetch. What a plan
  // change CAN move: the verdict, the Month lens, the prefill — and the
  // bill calendar, whose forecast plots the savings goals and caps
  // paychecks by the income scenario.
  const refresh = (saved: "p" | "i" | "g" | "b") => () => {
    qc.invalidateQueries({ queryKey: ["today"] });
    qc.invalidateQueries({ queryKey: ["lens", "month"] });
    qc.invalidateQueries({ queryKey: ["budgets-suggest"] });
    qc.invalidateQueries({ queryKey: ["calendar"] });
    dirty.current[saved] = false;
    setGens((g0) => {
      const n = { ...g0 };
      for (const k of ["p", "i", "g", "b"] as const)
        if (k !== saved && !dirty.current[k]) n[k] = g0[k] + 1;
      return n;
    });
  };
  const goal = (d?.savings_goals ?? []).find((g) => g.name === "Savings");

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
    <ScrollView style={st.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => Promise.all([
                                        settings.refetch(), today.refetch()])} />}>
      <StaleBanner query={settings} />
      {(today.isPending || settings.isPending) && (
        <Text style={st.center}>Loading…</Text>
      )}
      {d && (
        <Card>
          <Text style={{ color: C.text, fontSize: 15, lineHeight: 22 }}>
            {d.income !== null && (
              <><B>{m$(d.income)}</B><M> in → </M></>
            )}
            <B>{m$(d.buckets.fixed?.month_budget ?? 0)}</B><M> bills → </M>
            <B>{m$(d.variable_budget)}</B><M> to spend</M>
            {(d.savings_plan ?? 0) > 0.005 && (
              <><M> → </M><B>{m$(d.savings_plan!)}</B><M> saved</M></>
            )}
            {d.plan_surplus !== null && (
              <>
                <M> → </M>
                <Text style={{ color: d.plan_surplus >= 0 ? C.good : C.bad,
                               fontWeight: "700" }}>
                  {m$(d.plan_surplus)}
                </Text>
                <M> excess</M>
              </>
            )}
          </Text>
          {/* "variable spending $0 of $0" is arithmetic, not news, and it
              reads as progress against a plan nobody has made, so it is
              hidden when budgets are unset, as on the Today hero.
              Undefined onboarding data (still loading) keeps the
              line, so it never flashes away. */}
          <Text style={st.mut}>
            {!(ob.data && !ob.data.budgets_set) && (<>
              day {d.day_of_month} of {d.days_in_month} · variable spending{" "}
              {m$(d.variable_actual)} of {m$(d.variable_budget)}
              {"   "}
            </>)}
            <Text style={{ color: C.accent }}
                  onPress={() => router.push("/bills" as never)}>
              manage bills ›
            </Text>
          </Text>
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
            // the header row is the SERVER's variable budget/actual —
            // never a row-sum (web Budget passes an explicit total)
            total={{ label: "Variable spending", judged: true,
                     actual: d.variable_actual,
                     expected: (d.buckets.food?.expected ?? 0)
                       + (d.buckets.other?.expected ?? 0),
                     budget: d.variable_budget }}
            rows={planRows(d.buckets, {
              // the Budget page shows the same month Today does, so its
              // rows open the same doors
              period: { y: Number(String(d.date).slice(0, 4)),
                        m: Number(String(d.date).slice(5, 7)) },
              fixedActual: d.buckets.fixed.actual,
              fixedBudget: d.buckets.fixed.month_budget,
              fixedPending: d.fixed_unpaid_due,
              savings: goal
                ? { plan: goal.monthly_plan, saved: goal.rate_90d }
                : null,
              excess: d.plan_surplus,
            })} />
        </Card>
      )}
      {s0 && (
        <Planner key={`p${gens.p}`} s0={s0} viewer={viewer}
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
            sub="take-home per paycheck; the active one powers the plan">
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
          qc.invalidateQueries({ queryKey: ["lens", "month"] });
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
        closed.</M>
      {rows.map((r) => (
        <View key={`${r.y}-${r.m}`} style={{ marginTop: 6 }}>
          <View style={{ flexDirection: "row", alignItems: "baseline",
                         gap: 8 }}>
            <Text style={{ color: C.text, fontSize: 13, flex: 1 }}>
              {HIST_MONTHS[r.m - 1]} {r.y}
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
              ` · Fixed bills ${m$(Math.abs(r.bills_monthly))}`}
          </Text>
        </View>
      ))}
      {d.months.length > 24 && !all && (
        <Pressable onPress={() => setAll(true)}>
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
          <Pressable onPress={() => setOpen(true)}>
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
type Row = { id: string; name: string; monthly: string; food?: boolean;
             category?: string; cats?: string[]; merch?: string[] };

export function Planner({ s0, viewer, onSaved, onDirty }: {
  s0: BudgetSettings; viewer: boolean; onSaved: () => void;
  onDirty?: (v: boolean) => void;
}) {
  const { client } = useSession();
  const qc = useQueryClient();
  const sug = useQuery({ queryKey: ["budgets-suggest"],
    queryFn: () => client!.budgetsSuggest(), enabled: !!client });
  const [income, setIncome] = useState<string | null>(null);
  const [rows, setRows] = useState<Row[] | null>(null);
  const [otherBase, setOtherBase] = useState(0);
  const [unallocEdited, setUnallocEdited] = useState<string | null>(null);
  const [savings, setSavings] = useState("");
  const [savingsTouched, setSavingsTouched] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  // one-time seed. An established user's plan comes entirely from s0 —
  // it doesn't need ["budgets-suggest"] (a median-over-history
  // computation) to show the plan card, so only a first-time user (whose
  // seed leans on bucket_candidates) waits for it.
  useEffect(() => {
    const userSaved = !!(s0.food_monthly || s0.other_monthly
      || s0.budgeted_income_monthly);
    if (rows !== null || (sug.isPending && !userSaved)) return;
    const suggestions = sug.data?.suggestions;
    const pick = (saved: number | null, suggested?: number) =>
      userSaved && (saved ?? 0) > 0 ? saved!
        : suggested ? Math.round(suggested) : (saved ?? 0) > 0 ? saved! : 0;
    setIncome(String(pick(s0.budgeted_income_monthly,
                          suggestions?.income_monthly) || 0));
    const foodVal = pick(s0.food_monthly, suggestions?.food_monthly);
    const existing: Row[] = (s0.custom_buckets ?? [])
      .filter((b) => b.parent === "other")
      .map((b) => ({ id: newRowId(), name: b.name,
                     monthly: String(b.monthly),
                     cats: b.categories, merch: b.merchants }));
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
      .find((g) => g.name === "Savings");
    if (userSaved && savedGoal != null) {
      setSavings(String(savedGoal.monthly_plan ?? 0));
      setSavingsTouched(true);
    }
  }, [rows, sug.isPending, sug.data, s0]);

  const save = useMutation({
    mutationFn: async () => {
      // merge against a FRESH read — the planner owns only the
      // other-parent buckets and the "Savings" goal
      const cur = await client!.settingsFull();
      const body: Record<string, unknown> = {
        food_monthly: foodVal,
        other_monthly: unalloc + catsTotal,
        budgeted_income_monthly: income ?? "0",
      };
      const catRows = (rows ?? [])
        .filter((b) => !b.food && b.name.trim() && numv(b.monthly) > 0)
        .map((b) => ({ name: b.name.trim(), parent: "other",
                       monthly: numv(b.monthly),
                       categories: b.cats
                         ?? (b.category ? [b.category] : []),
                       merchants: b.merch ?? [] }));
      const foodBuckets = (cur.custom_buckets ?? [])
        .filter((b) => b.parent === "food");
      if (catRows.length || foodBuckets.length
          || (cur.custom_buckets ?? []).length)
        body.custom_buckets = [...foodBuckets, ...catRows];
      // ALWAYS persist the Savings goal — a $0 plan is a remembered
      // choice, not an absence
      const others = (cur.savings_goals ?? [])
        .filter((g) => g.name !== "Savings");
      const curGoal = (cur.savings_goals ?? [])
        .find((g) => g.name === "Savings");
      body.savings_goals = [...others,
        { ...(curGoal ?? { name: "Savings", target: 0, tokens: [],
                           start_balance: 0 }),
          monthly_plan: numv(savingsShown) }];
      return saveSettings(qc, client!, body);
    },
    onSuccess: () => { setMsg("Plan saved."); onSaved(); },
    onError: (e) => setMsg(errText(e)),
  });

  if (rows === null || income === null)
    return <Card><Text style={st.mut}>Loading the plan…</Text></Card>;
  const bills = sug.data?.avg_bills_monthly ?? 0;
  const billsCount = sug.data?.bills_count ?? 0;
  const foodVal = numv(rows.find((r) => r.food)?.monthly ?? "0");
  const catsTotal = rows.filter((r) => !r.food)
    .reduce((t, r) => t + numv(r.monthly), 0);
  // a typed savings amount comes out of Everything else so the plan
  // balances (the web planner's rule)
  const savingsTyped = savingsTouched ? numv(savings) : 0;
  const unallocAuto = Math.max(0, Math.round(otherBase - catsTotal
                                              - savingsTyped));
  const unalloc = unallocEdited !== null
    ? numv(unallocEdited) : unallocAuto;
  const inc = numv(income);
  const savingsAuto = Math.max(0,
    Math.round(inc - bills - foodVal - catsTotal - unalloc));
  const savingsShown = savingsTouched ? savings
    : String(savingsAuto || "");
  const outTotal = bills + foodVal + catsTotal + unalloc;
  const leftover = Math.round(inc - outTotal - numv(savingsShown));
  const pct = (v: number) => (inc > 0 ? Math.round((100 * v) / inc) : 0);
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
    setRows((rs) => rs!.map((r, j) => (j === i ? { ...r, ...p } : r)));
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
        Give every dollar a job: start from income, cover the bills, put
        a monthly number on each budget category, and what&apos;s left
        becomes savings. Everything is pre-filled from your real history.
      </Text>

      {inc > 0 && (
        <View style={{ gap: 4, marginTop: 6 }}>
          <Text style={st.capLbl}>Where each dollar goes</Text>
          <FlowBar label={`in ${m$(inc)} · 100%`} color={C.good} frac={1} />
          <FlowBar label={`out ${m$(outTotal)} · ${pct(outTotal)}%`}
                   color={C.warn} frac={inc > 0 ? outTotal / inc : 0} />
          <FlowBar label={`save ${m$(numv(savingsShown))} · ${
                     pct(numv(savingsShown))}%`}
                   color={C.accent}
                   frac={inc > 0 ? numv(savingsShown) / inc : 0} />
          {leftover > 0 && (
            <Text style={st.mut}>
              Excess cash {m$(leftover)} · {pct(leftover)}% — stays in
              checking.
            </Text>
          )}
          {leftover < 0 && (
            <Text style={{ color: C.bad, fontSize: 12 }}>
              Plan exceeds income by {m$(-leftover)}
            </Text>
          )}
        </View>
      )}

      <Text style={[st.secHead, { color: C.good }]}>Money in</Text>
      <Text style={st.lbl}>
        Expected income / month (take-home; powers the cash forecast)
      </Text>
      <TextInput style={st.input} keyboardType="decimal-pad"
                 editable={!viewer}
                 value={income} onChangeText={setIncome} />
      <Text style={st.mut}>
        Inferred from the paychecks detected in your ledger.
      </Text>

      <Text style={[st.secHead, { color: C.warn }]}>Money out</Text>
      <View style={st.rowLine}>
        <Text style={{ color: C.text, fontSize: 14, fontWeight: "700",
                       flex: 1 }}>
          Bills{" "}
          <Text style={[st.mut, { fontWeight: "400" }]}>
            ({billsCount} tracked, evened out)
          </Text>
        </Text>
        <Text style={{ color: C.text, fontSize: 14 }}>{m$(bills)}</Text>
      </View>
      <Text style={st.capLbl}>+ Budget categories</Text>
      {rows.map((r, i) => (
        <View key={r.id} style={st.rowLine}>
          <TextInput style={[st.input, { flex: 1, paddingVertical: 6 }]}
                     placeholder="category name" placeholderTextColor={C.mut}
                     editable={!viewer && !r.food}
                     value={r.name}
                     onChangeText={(v) => patch(i, { name: v })} />
          {!viewer && !r.food && (
            <Text style={{ color: C.mut, fontSize: 16, padding: 4 }}
                  onPress={() =>
                    setRows((rs) => rs!.filter((_, j) => j !== i))}>
              ✕
            </Text>
          )}
          <TextInput style={[st.input, { width: 90, paddingVertical: 6,
                                         textAlign: "right" }]}
                     keyboardType="decimal-pad" editable={!viewer}
                     value={r.monthly}
                     onChangeText={(v) => patch(i, { monthly: v })} />
        </View>
      ))}
      {!viewer && candidates.map((c) => (
        <Text key={c.category} style={{ color: C.accent, fontSize: 12 }}
              onPress={() => setRows((rs) => [...rs!,
                c.category === "__food__"
                  ? { id: newRowId(), name: "Food",
                      monthly: String(c.monthly), food: true }
                  : { id: newRowId(), name: c.name,
                      monthly: String(c.monthly),
                      category: c.category, cats: [c.category] }])}>
          + {c.name} ~${c.monthly}/mo
        </Text>
      ))}
      {!viewer && (
        <Text style={{ color: C.accent, fontSize: 12 }}
              onPress={() => setRows((rs) =>
                [...rs!, { id: newRowId(), name: "", monthly: "" }])}>
          + add category
        </Text>
      )}
      <View style={[st.rowLine, { borderTopColor: C.border,
                                  borderTopWidth: StyleSheet.hairlineWidth,
                                  paddingTop: 6 }]}>
        <Text style={{ color: C.text, fontSize: 13, flex: 1 }}>
          Everything else{" "}
          <Text style={st.mut}>(unallocated — auto-shrinks)</Text>
        </Text>
        <TextInput style={[st.input, { width: 90, paddingVertical: 6,
                                       textAlign: "right" }]}
                   keyboardType="decimal-pad" editable={!viewer}
                   value={unallocEdited ?? String(unallocAuto || "")}
                   onChangeText={setUnallocEdited} />
      </View>

      <Text style={[st.secHead, { color: C.accent }]}>
        Savings / Investment
      </Text>
      <Text style={st.lbl}>
        Save / month (what&apos;s left of the money in)
      </Text>
      <TextInput style={st.input} keyboardType="decimal-pad"
                 editable={!viewer}
                 value={savingsShown}
                 onChangeText={(v) => { setSavings(v);
                                        setSavingsTouched(true); }} />
      {savingsTouched && savingsAuto > 0
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
        {savingsTouched ? "Your number." : "What's left, automatically."}
        {" Saved as your “Savings” goal."}
      </Text>

      {zeroed.length > 0 && (
        <Text style={{ color: C.warn, fontSize: 12, marginTop: 6 }}>
          ⚠ {zeroed.join(", ")} {zeroed.length === 1 ? "has" : "have"} no
          budget — saving now leaves{" "}
          {zeroed.length === 1 ? "it" : "them"} unbudgeted (spending
          there won&apos;t count toward the verdict).
        </Text>
      )}
      {msg ? (
        <Text style={{ color: msg === "Plan saved." ? C.good : C.bad,
                       fontSize: 12, marginTop: 6 }}
              onPress={() => setMsg(null)}>
          {msg}
        </Text>
      ) : null}
      {!viewer && (
        <Pressable style={[st.btn, { alignSelf: "flex-start",
                     marginTop: 10 }, save.isPending && { opacity: 0.5 }]}
                   disabled={save.isPending}
                   onPress={() => save.mutate()}>
          <Text style={st.btnText}>
            {save.isPending ? "saving…" : "Save budgets"}
          </Text>
        </Pressable>
      )}
    </Card>
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
type ScenarioRow = { id: string; name: string; take_home: string;
                     cadence: string };

function IncomeScenarios({ s0, onSaved, onDirty }: {
  s0: BudgetSettings; onSaved: () => void;
  onDirty?: (v: boolean) => void;
}) {
  const { client } = useSession();
  const qc = useQueryClient();
  const [rows, setRows] = useState<ScenarioRow[]>(
    () => (s0.income_scenarios ?? []).map((r) => (
      { id: newRowId(), name: r.name, take_home: String(r.take_home),
        cadence: r.cadence })));
  const [active, setActive] = useState(s0.active_scenario ?? "");
  const [msg, setMsg] = useState<string | null>(null);
  // every draft edit marks the card dirty so a sibling save's remount
  // loop skips it — otherwise the remount wipes the unsaved scenario
  const patch = (i: number, p: Partial<ScenarioRow>) => {
    onDirty?.(true);
    setRows((rs) => rs.map((x, j) => (j === i ? { ...x, ...p } : x)));
  };
  const save = useMutation({
    mutationFn: () => saveSettings(qc, client!, {
      income_scenarios: rows.filter((r) => r.name.trim())
        .map((r) => ({ name: r.name.trim(),
                       take_home: numv(r.take_home),
                       cadence: r.cadence })),
      active_scenario: active,
    }),
    onSuccess: () => { setMsg("Income scenario saved."); onSaved(); },
    onError: (e) => setMsg(errText(e)),
  });
  const CAD = ["weekly", "biweekly", "semimonthly", "monthly"];
  const MULT: Record<string, number> =
    { weekly: 4, biweekly: 2, semimonthly: 2, monthly: 1 };
  const activeRow = rows.find((r) => r.name === active) ?? rows[0];
  const monthly = activeRow
    ? numv(activeRow.take_home) * (MULT[activeRow.cadence] ?? 2) : 0;
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
                  onPress={() => { onDirty?.(true); setActive(r.name); }}>
              {active === r.name ? "◉" : "○"}
            </Text>
            <TextInput style={[st.input, { flex: 1, paddingVertical: 6 }]}
                       placeholder="full salary" placeholderTextColor={C.mut}
                       value={r.name}
                       onChangeText={(v) => {
                         if (active === r.name) setActive(v);
                         patch(i, { name: v });
                       }} />
            <TextInput style={[st.input, { width: 90, paddingVertical: 6,
                                           textAlign: "right" }]}
                       keyboardType="decimal-pad" value={r.take_home}
                       onChangeText={(v) => patch(i, { take_home: v })} />
            <Text style={{ color: C.mut, fontSize: 16, padding: 4 }}
                  onPress={() => { onDirty?.(true);
                    setRows((rs) => rs.filter((_, j) => j !== i)); }}>
              ✕
            </Text>
          </View>
          <View style={{ flexDirection: "row", gap: 6, marginLeft: 26 }}>
            {CAD.map((c) => (
              <Pressable key={c}
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
                          cadence: "biweekly" }]); }}>
        + Add scenario
      </Text>
      <Text style={st.mut}>
        {activeRow && monthly > 0
          ? `Active now: ${m$(monthly)}/mo (${activeRow.name}). ` : ""}
        Applies to the plan, cash forecast, and daily email. No
        scenarios → the plan&apos;s own income figure is used.
      </Text>
      {msg ? (
        <Text style={{ color: msg.includes("saved") ? C.good : C.bad,
                       fontSize: 12 }}
              onPress={() => setMsg(null)}>{msg}</Text>
      ) : null}
      <Pressable style={[st.btn, { alignSelf: "flex-start", marginTop: 8 },
                   save.isPending && { opacity: 0.5 }]}
                 disabled={save.isPending}
                 onPress={() => save.mutate()}>
        <Text style={st.btnText}>
          {save.isPending ? "saving…" : "Save income scenarios"}
        </Text>
      </Pressable>
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
      <Pressable style={[st.chip, !selected && st.chipOn]}
                 onPress={() => onPick(index, "")}>
        <Text style={{ color: !selected ? C.text : C.mut,
                       fontSize: 11 }}>any account</Text>
      </Pressable>
      {/* every account — a goal bound to a hidden/archived one
          must still show its selection */}
      {(accounts ?? []).map((a) => (
        <Pressable key={a.id}
                   style={[st.chip, selected === a.id && st.chipOn]}
                   onPress={() => onPick(index, a.id)}>
          <Text style={{ color: selected === a.id ? C.text : C.mut,
                         fontSize: 11 }}>
            {a.name}
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
  const qc = useQueryClient();
  const baseline = useRef((s0.savings_goals ?? []).map((g) => g.name));
  const accounts = useQuery({ queryKey: ["accounts"],
    queryFn: () => client!.accounts(), enabled: !!client });
  const today = useQuery({ queryKey: ["today"],
    queryFn: () => client!.todayFull(), enabled: !!client });
  const [rows, setRows] = useState<GoalRow[]>(
    () => (s0.savings_goals ?? []).map((g) => ({
      id: newRowId(), name: g.name, target: String(g.target || ""),
      target_date: g.target_date ?? "",
      monthly_plan: String(g.monthly_plan ?? ""),
      account_id: g.account_id ?? "",
      tokens: (g.tokens ?? []).join(", "),
      start_balance: String(g.start_balance || ""),
      mode: g.mode === "sweep" ? "sweep" : "monthly" })));
  const [msg, setMsg] = useState<string | null>(null);
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
  const save = useMutation({
    mutationFn: async () => {
      // fresh-read merge: untouched rows defer to the server's copy;
      // rows added elsewhere are carried through (logic in pure.ts so
      // node can exercise it — it's the only lost-update guard here)
      const cur = await client!.settingsFull();
      return saveSettings(qc, client!, {
        savings_goals: mergeSavingsGoals(rows, cur.savings_goals ?? [],
                                         baseline.current) });
    },
    onSuccess: () => { setMsg("Savings goals saved."); onSaved(); },
    onError: (e) => setMsg(errText(e)),
  });
  return (
    <Card>
      <H>
        Savings goals{" "}
        <Text style={[st.mut, { fontWeight: "400" }]}>
          — progress comes from real transfers
        </Text>
      </H>
      <Text style={st.mut}>
        A goal watches transfers on its account (add match words to
        carve several goals out of one account). The monthly plan
        lowers the excess cash line and rides the cash forecast — it
        never makes the daily verdict go over. A sweep goal contributes
        at month end instead, and only what the month&apos;s surplus
        covers.
      </Text>
      {rows.map((r, i) => {
        const p = (today.data?.savings_goals ?? [])
          .find((x) => x.name === r.name.trim());
        return (
        <View key={r.id} style={{ borderTopColor: C.border,
                               borderTopWidth: StyleSheet.hairlineWidth,
                               paddingVertical: 8, gap: 6 }}>
          <View style={st.rowLine}>
            <TextInput style={[st.input, { flex: 1, paddingVertical: 6 }]}
                       placeholder="Trip fund" placeholderTextColor={C.mut}
                       value={r.name}
                       onChangeText={(v) => patch(i, { name: v })} />
            <Text style={{ color: C.mut, fontSize: 16, padding: 4 }}
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
            <Field label="Match words (opt.)" value={r.tokens}
                   onChange={(v) => patch(i, { tokens: v })}
                   placeholder="trip" wide />
          </View>
          {/* contribution mode — the web editor's wording, verbatim */}
          <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
            {([["monthly", "every month (fixed plan)"],
               ["sweep", "only when the month worked out (sweep)"]] as
               const).map(([val, label]) => (
              <Pressable key={val}
                         style={[st.chip, r.mode === val && st.chipOn]}
                         onPress={() => patch(i, { mode: val })}>
                <Text style={{ color: r.mode === val ? C.text : C.mut,
                               fontSize: 11 }}>{label}</Text>
              </Pressable>
            ))}
          </View>
          <GoalAccountPicker accounts={accounts.data?.accounts}
                             index={i} selected={r.account_id}
                             onPick={pickAccount} />
          {p && p.pct != null && (
            <Text style={st.mut}>
              {m$(p.saved ?? 0)} of {m$(p.target)} ({p.pct}%)
              {/* A sweep goal contributes in one lump at month end, so
                  the trailing-90-day pace text reads "off pace" all
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
                : p.eta && p.pct < 100 ? ` — ~${p.eta.slice(5, 10)}` : ""}
            </Text>
          )}
        </View>
        );
      })}
      <Text style={{ color: C.accent, fontSize: 12 }}
            onPress={() => setRows((rs) => [...rs,
              { id: newRowId(), name: "", target: "", target_date: "",
                monthly_plan: "", account_id: "", tokens: "",
                start_balance: "", mode: "monthly", touched: true }])}>
        + Add goal
      </Text>
      {msg ? (
        <Text style={{ color: msg.includes("saved") ? C.good : C.bad,
                       fontSize: 12 }}
              onPress={() => setMsg(null)}>{msg}</Text>
      ) : null}
      <Pressable style={[st.btn, { alignSelf: "flex-start", marginTop: 8 },
                   save.isPending && { opacity: 0.5 }]}
                 disabled={save.isPending}
                 onPress={() => save.mutate()}>
        <Text style={st.btnText}>
          {save.isPending ? "saving…" : "Save savings goals"}
        </Text>
      </Pressable>
    </Card>
  );
}

function Field({ label, value, onChange, num, placeholder, wide }: {
  label: string; value: string; onChange: (v: string) => void;
  num?: boolean; placeholder?: string; wide?: boolean;
}) {
  return (
    <View style={{ width: wide ? "100%" : "47%" }}>
      <Text style={{ color: C.mut, fontSize: 10 }}>{label}</Text>
      <TextInput style={[st.input, { paddingVertical: 5 }]}
                 keyboardType={num ? "decimal-pad" : "default"}
                 placeholder={placeholder} placeholderTextColor={C.mut}
                 autoCapitalize="none"
                 value={value} onChangeText={onChange} />
    </View>
  );
}

// ---- bucket rules (fine-tune) ----
type BucketRow = { id: string; name: string; parent: "food" | "other";
                   monthly: string; categories: string[];
                   merchants: string; touched?: boolean };

function BucketRules({ s0, onSaved, onDirty }: {
  s0: BudgetSettings; onSaved: () => void;
  onDirty?: (v: boolean) => void;
}) {
  const { client } = useSession();
  const qc = useQueryClient();
  const baseline = useRef((s0.custom_buckets ?? []).map((b) => b.name));
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
    () => (s0.custom_buckets ?? []).map((b) => ({
      id: newRowId(), name: b.name, parent: b.parent,
      monthly: String(b.monthly || ""),
      categories: b.categories ?? [],
      merchants: (b.merchants ?? []).join(", ") })));
  const [dynamic, setDynamic] =
    useState(!!s0.dynamic_variable_budget);
  // the Switch flips before the save; this is what it falls back to when
  // the server says no
  const committedDyn = useRef(!!s0.dynamic_variable_budget);
  const [msg, setMsg] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: async (dyn: boolean) => {
      const cur = await client!.settingsFull();
      const curBy = new Map((cur.custom_buckets ?? [])
        .map((b) => [b.name, b]));
      const baseNames = new Set(baseline.current);
      const out: CustomBucket[] = [];
      for (const b of rows) {
        const name = b.name.trim();
        if (!name) continue;
        if (!b.touched) {
          const live = curBy.get(name);
          if (live) { out.push(live); continue; }
          if (baseNames.has(name)) continue;
        }
        out.push({ name, parent: b.parent, monthly: numv(b.monthly),
                   categories: b.categories,
                   merchants: b.merchants.split(",").map((x) => x.trim())
                     .filter(Boolean) });
      }
      for (const b of cur.custom_buckets ?? [])
        if (!baseNames.has(b.name) && !out.some((o) => o.name === b.name))
          out.push(b);
      // partial body on purpose — food/other/income/savings belong to
      // the planner
      return saveSettings(qc, client!, { dynamic_variable_budget: dyn,
                                         custom_buckets: out });
    },
    onSuccess: (_v, dyn) => {
      committedDyn.current = dyn;
      setMsg("Bucket rules saved."); onSaved();
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
  const suggest = useMutation({
    mutationFn: () => client!.budgetsSuggest(),
    onSuccess: (r) => setSugg(r.suggestions.buckets ?? {}),
    onError: (e) => setMsg(errText(e)),
  });
  return (
    <Card>
      <H>
        Bucket rules{" "}
        <Text style={[st.mut, { fontWeight: "400" }]}>
          — what counts toward each bucket
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
            <TextInput style={[st.input, { flex: 1, paddingVertical: 6 }]}
                       placeholder="Coffee" placeholderTextColor={C.mut}
                       value={r.name}
                       onChangeText={(v) => patch(i, { name: v })} />
            <TextInput style={[st.input, { width: 80, paddingVertical: 6,
                                           textAlign: "right" }]}
                       keyboardType="decimal-pad" value={r.monthly}
                       onChangeText={(v) => patch(i, { monthly: v })} />
            <Text style={st.mut}>/mo</Text>
          </View>
          <View style={{ flexDirection: "row", gap: 6 }}>
            {(["other", "food"] as const).map((p) => (
              <Pressable key={p} style={[st.chip, r.parent === p && st.chipOn]}
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
              <Pressable key={c} style={st.chip}
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
              <Pressable key={c} style={[st.chip, { opacity: 0.7 }]}
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
                      String(Math.round(sugg[r.name.trim()])) })}>
                use
              </Text>
            </Text>
          )}
          <View style={st.rowLine}>
            <TextInput style={[st.input, { flex: 1, paddingVertical: 6 }]}
                       placeholder="＋ merchant, comma-separated"
                       placeholderTextColor={C.mut} autoCapitalize="none"
                       value={r.merchants}
                       onChangeText={(v) => patch(i, { merchants: v })} />
          </View>
          <View style={{ flexDirection: "row", gap: 8 }}>
            <Pressable style={[st.btn, save.isPending && { opacity: 0.5 }]}
                       disabled={save.isPending}
                       onPress={() => { setEditing(null);
                                        save.mutate(dynamic); }}>
              <Text style={st.btnText}>save</Text>
            </Pressable>
            <Pressable style={[st.btn, st.btnQuiet]}
                       onPress={() => setEditing(null)}>
              <Text style={[st.btnText, { color: C.mut }]}>cancel</Text>
            </Pressable>
            <Pressable style={[st.btn, st.btnQuiet]}
                       onPress={() => { setRows((rs) =>
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
          Suggestions are the median of recent months, this one so far
          included (outlier months excluded) — tap “use” beside a bucket
          to take one.
        </Text>
      )}
      <View style={[st.rowLine, { marginTop: 6 }]}>
        <Text style={{ color: C.text, fontSize: 13, flex: 1 }}>
          Dynamic budget{" "}
          <Text style={st.mut}>— adapts to the month so far</Text>
        </Text>
        <Switch value={dynamic} disabled={save.isPending}
                onValueChange={(v) => { setDynamic(v); save.mutate(v); }}
                trackColor={{ true: C.accent, false: C.hover }}
                thumbColor={C.text} />
      </View>
      {msg ? (
        <Text style={{ color: msg.includes("saved") ? C.good : C.bad,
                       fontSize: 12 }}
              onPress={() => setMsg(null)}>{msg}</Text>
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
  input: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
           borderRadius: 8, color: C.text, fontSize: 14,
           paddingHorizontal: 10, paddingVertical: 8 },
  rowLine: { flexDirection: "row", alignItems: "center", gap: 8,
             marginTop: 4 },
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 16, paddingVertical: 8 },
  btnQuiet: { backgroundColor: C.hover },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
  chip: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
          borderRadius: 999, paddingHorizontal: 9, paddingVertical: 4 },
  chipOn: { borderColor: C.accent, backgroundColor: C.hover },
  flowTrack: { backgroundColor: C.border, borderRadius: 4, height: 8,
               overflow: "hidden" },
});
