// the Budget page — the plan and its performance in one place.
// Top: the same planner the setup wizard uses (edit the plan). Below:
// this month's actuals in the plan's own language (shared PlanBars, so
// Today/Month/Week can never drift from it).
// every budget control lives here, not split with Settings —
// fine-tuning, income scenarios and the bill tweaks that feed the plan —
// so there is one place to answer "what is my budget".
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type ReactNode } from "react";
import { Link } from "react-router";
import BudgetFineTune from "../components/BudgetFineTune";
import BudgetHistoryCard from "../components/BudgetHistoryCard";
import BudgetPlanner from "../components/BudgetPlanner";
import IncomeScenarioCard from "../components/IncomeScenarioCard";
import PlanBars, { planRows } from "../components/PlanBars";
import BillTweaksCard from "../components/BillTweaksCard";
import SavingsGoalsCard from "../components/SavingsGoalsCard";
import ViewerLock from "../components/ViewerLock";
import { api, money } from "../api/client";
import { canEdit } from "../role";

const toast = (m: string) =>
  window.dispatchEvent(new CustomEvent("oiko-toast", { detail: m }));

// a minimized pane: one summary line closed, the real card open. The
// children stay MOUNTED either way (display, not unmount) so a draft in
// progress survives a stray toggle.
function Collapsible({ title, sub, children }: {
  title: string; sub: string; children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <div className="card" style={{ display: open ? "none" : undefined }}>
        <h2 style={{ margin: 0, fontSize: "1rem" }}>
          <button className="linklike"
                  style={{ background: "none", border: "none", padding: 0,
                           cursor: "pointer", font: "inherit",
                           color: "inherit" }}
                  aria-expanded={false}
                  onClick={() => setOpen(true)}>
            ▸ {title}
          </button>
          <span className="sub" style={{ fontWeight: 400 }}> — {sub}</span>
        </h2>
      </div>
      <div style={{ display: open ? undefined : "none" }}>
        <p className="sub" style={{ margin: ".5rem .2rem 0" }}>
          <button className="linklike"
                  style={{ background: "none", border: "none", padding: 0,
                           cursor: "pointer", font: "inherit",
                           color: "inherit" }}
                  aria-expanded
                  onClick={() => setOpen(false)}>
            ▾ {title} — minimize
          </button>
        </p>
        {children}
      </div>
    </>
  );
}

export default function Budget() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["today", "now"], queryFn: () => api.todayFull() });
  const s = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const ob = useQuery({ queryKey: ["onboarding"], queryFn: api.onboarding });
  const d = q.data;
  const goal = (d?.savings_goals ?? []).find((g) => g.name === "Savings");
  // the plan's controls are editor chrome (writes are 403'd
  // server-side too); a viewer still reads the plan and this month.
  const mayEdit = canEdit(me);

  // The planner, the fine-tune card and the goals card all write config
  // lists, and each seeds its form once on mount. So after one saves, the
  // others' state is stale. Remount the siblings once the settings refetch
  // has actually landed — keyed off a real save, never off a background
  // refetch. A sibling holding UNSAVED edits is skipped (the
  // remount would silently discard the draft); every card merges against
  // the server's live state at save time, so skipping stays safe.
  const [plannerKey, setPlannerKey] = useState(0);
  const [tuneKey, setTuneKey] = useState(0);
  const [goalsKey, setGoalsKey] = useState(0);
  const dirty = useRef({ planner: false, tune: false, goals: false });
  const mark = (k: keyof typeof dirty.current) =>
    (v: boolean) => { dirty.current[k] = v; };
  const remount = (...targets: (keyof typeof dirty.current)[]) => {
    if (targets.includes("planner") && !dirty.current.planner)
      setPlannerKey((k) => k + 1);
    if (targets.includes("tune") && !dirty.current.tune)
      setTuneKey((k) => k + 1);
    if (targets.includes("goals") && !dirty.current.goals)
      setGoalsKey((k) => k + 1);
  };
  // The settings refetch is awaited on purpose — it is what the sibling
  // remount keys off. The wizard's progress and the budgets prefill only
  // move when the PLAN is written; an income scenario or a goal changes
  // the verdict and the forecast calendar, nothing the wizard reads.
  const refresh = async (plan = true) => {
    await qc.invalidateQueries({ queryKey: ["settings"] });
    qc.invalidateQueries({ queryKey: ["today"] });
    qc.invalidateQueries({ queryKey: ["calendar"] });
    if (plan) {
      qc.invalidateQueries({ queryKey: ["onboarding"] });
      qc.invalidateQueries({ queryKey: ["budgets-suggest"] });
    }
  };

  return (
    <>
      <h1>Budget</h1>
      {/* HERO: the plan as ONE flow line — the same sentence Today
          and the daily email speak, so the plan reads identically wherever it
          appears. It leads, and the planner FORM sits beneath it, because
          editing is the rare act and checking "am I on plan" is the daily
          one. */}
      {d && (
        <div className="card" style={{ marginTop: 0 }}>
          <div style={{ display: "flex", gap: ".55rem", flexWrap: "wrap",
                        alignItems: "baseline" }}>
            {d.income !== null && (<>
              <span><b style={{ fontSize: "1.25rem" }}>{money(d.income)}</b>
                <span className="sub"> in</span></span>
              <span style={{ color: "var(--line)" }}>→</span>
            </>)}
            <span><b style={{ fontSize: "1.25rem" }}>
              {money(d.buckets.fixed.month_budget)}</b>
              <span className="sub"> bills</span></span>
            <span style={{ color: "var(--line)" }}>→</span>
            <span><b style={{ fontSize: "1.25rem" }}>{money(d.variable_budget)}</b>
              <span className="sub"> to spend</span></span>
            {(d.savings_plan ?? 0) > 0.005 && (<>
              <span style={{ color: "var(--line)" }}>→</span>
              <span><b style={{ fontSize: "1.25rem" }}>{money(d.savings_plan!)}</b>
                <span className="sub"> saved</span></span>
            </>)}
            {d.plan_surplus !== null && (<>
              <span style={{ color: "var(--line)" }}>→</span>
              <span><b style={{ fontSize: "1.25rem" }}
                       className={d.plan_surplus >= 0 ? "pos" : "neg"}>
                {money(d.plan_surplus)}</b>
                <span className="sub"> excess</span></span>
            </>)}
          </div>
          {/* "variable spending $0 of $0" is arithmetic, not news, and it
              reads as progress against a plan nobody has made — the same
              sentence the Today hero stopped showing when budgets are
              unset. Undefined onboarding data (still loading) keeps the
              line, so it never flashes away. */}
          <div className="sub" style={{ marginTop: ".2rem", display: "flex",
                                        gap: ".9rem", flexWrap: "wrap" }}>
            {!(ob.data && !ob.data.budgets_set) && (
              <span>day {d.day_of_month} of {d.days_in_month} · variable spending{" "}
                <b style={{ color: "var(--ink)" }}>{money(d.variable_actual)}</b>{" "}
                of {money(d.variable_budget)}</span>)}
            <Link to="/bills" style={{ marginLeft: "auto" }}>manage bills ›</Link>
          </div>
        </div>
      )}

      {/* THIS MONTH — the answer, before the editor. Rows are the plan's own
          categories, so tracking answers at the granularity the plan is
          written in rather than only the coarse variable/bills/savings trio. */}
      {d && (
        <div className="card">
          <h2 style={{ marginTop: 0 }}>This month
            <span className="sub" style={{ fontWeight: 400 }}>
              {" "}— the plan below, tracked</span></h2>
          <PlanBars
            total={{ label: "Variable spending", actual: d.variable_actual,
                     expected: d.buckets.food.expected
                               + d.buckets.other.expected,
                     budget: d.variable_budget }}
            rows={planRows(d.buckets, {
              // the Budget page shows the same month Today does, so its
              // rows open the same doors
              period: { y: Number(d.date.slice(0, 4)), m: Number(d.date.slice(5, 7)) },
              fixedActual: d.buckets.fixed.actual,
              fixedBudget: d.buckets.fixed.month_budget,
              fixedPending: d.fixed_unpaid_due,
              savings: goal ? { plan: goal.monthly_plan,
                                saved: goal.rate_90d } : null,
              excess: d.plan_surplus,
            })} />
          {/* the excess-cash caption that stood here is gone: the hero's
              last term already says it, once */}
        </div>
      )}

      <div className="card">
        <h2 style={{ marginTop: 0 }}>Your plan</h2>
        {/* a viewer still READS the plan (the form carries the numbers)
            but can't submit it — disabled-in-place, like DemoLock */}
        <ViewerLock on={me.data && !mayEdit}>
          <BudgetPlanner key={plannerKey} standalone
            onDirtyChange={mark("planner")}
            onSaved={async () => {
              toast("Plan saved.");
              await refresh();
              remount("tune", "goals");
            }} />
        </ViewerLock>
      </div>

      {mayEdit && s.data && (
        <>
          <BudgetFineTune key={tuneKey} data={s.data}
            onDirtyChange={mark("tune")}
            onSaved={async () => {
              toast("Bucket rules saved.");
              await refresh();
              remount("planner", "goals");
            }} />
          {/* the plan's two levers, stacked and minimized below the
              bucket rules — a side-by-side grid puts two different-height
              cards next to each other and they never align. Collapsed
              hides, never unmounts, so an open draft survives a stray
              toggle. */}
          <Collapsible title="Income scenarios"
                       sub="take-home per paycheck; the active one powers the plan">
            <IncomeScenarioCard data={s.data} onSaved={() => {
              toast("Income scenario saved.");
              void refresh(false);
            }} />
          </Collapsible>
          {/* goals live here rather than in Settings: the plan's money and
              the goals it funds belong on one page. The planner also writes
              savings_goals (its Savings row), so the cards remount each
              other after a landed save unless the target holds a draft. */}
          <Collapsible title="Savings goals"
                       sub="trip fund, emergency fund; progress comes from real transfers">
            <SavingsGoalsCard key={goalsKey}
              data={s.data}
              onDirtyChange={mark("goals")}
              onSaved={async () => {
                toast("Savings goals saved.");
                await refresh(false);
                remount("planner", "tune");
              }} />
          </Collapsible>
          {/* one line naming what the Bills page has adjusted */}
          <BillTweaksCard data={s.data} />
        </>
      )}
      {/* the frozen record the lenses judge closed months by — viewers
          read it too; only the backfill action is editor chrome */}
      <BudgetHistoryCard mayEdit={mayEdit} onToast={toast} />
    </>
  );
}
