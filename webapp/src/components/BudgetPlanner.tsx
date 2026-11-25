import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type CSSProperties, useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { api, errText, money } from "../api/client";
import { saveSettings, SETTINGS_SAVES } from "../api/cache";
import { planSaved } from "../checklist";
import { historyBasisNote, inputPhase, isPlanSavingsGoal, mergePlannerBuckets,
         parseMoneyDraft, PLANNER_HISTORY_FAILED, plannerBucketsSaved,
         plannerAtRest, plannerPreview, plannerShares, plannerUnallocAuto, plannerWaits,
         SAVE_WAITS_FAILED, SAVE_WAITS_LOADING, UNKNOWN_FIGURE,
         type PlannerRow }
  from "../moneymath";

// The balance-your-budget planner — the wizard's budgets step AND the
// standing /balance page share it.
//
// Money model: income and bills are fixed inputs; every
// budget category (Food included) is a row; UNALLOCATED is what history
// says you spend outside the named categories and auto-shrinks as
// categories absorb it; SAVINGS is what's left of income after all of
// it. Adding/raising a category therefore drains unallocated first,
// then savings — money is conserved, nothing double-counts.
const capital = (w: string) => w.charAt(0).toUpperCase() + w.slice(1);

export default function BudgetPlanner({ onSaved, standalone = false,
                                         onDirtyChange }: {
  onSaved?: () => void; standalone?: boolean;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const qc = useQueryClient();
  const s = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const ob = useQuery({ queryKey: ["onboarding"], queryFn: api.onboarding });
  const sug = useQuery({ queryKey: ["budgets-suggest"],
                         queryFn: api.budgetsSuggest });
  // the month the header shows: its income is the plan's income, and the
  // server says where it comes from — the planner never works that out
  const now = useQuery({ queryKey: ["today", "now"],
                         queryFn: () => api.todayFull() });
  const billsLabel = capital(now.data?.plan_bills_label ?? "bills");
  const tracked = now.data?.income_source === "paychecks"
    || now.data?.income_source === "scenario";
  type CRow = PlannerRow;
  const [init, setInit] = useState(false);
  const [income, setIncome] = useState("");
  const [rows, setRows] = useState<CRow[]>([]);
  // the rows on screen now, for a save that lands after more typing
  const rowsNow = useRef(rows);
  rowsNow.current = rows;
  // the "other" buckets the planner was last seeded with (at open, then
  // each saved view) — the save-time merge tells "removed here" from
  // "added elsewhere since" by this list
  const [baseNames, setBaseNames] = useState<string[]>([]);
  // base for the unallocated auto value: history's everything-else
  const [otherBase, setOtherBase] = useState(0);
  const [unallocEdited, setUnallocEdited] = useState<string | null>(null);
  const [savings, setSavings] = useState("");
  const [savingsTouched, setSavingsTouched] = useState(false);
  // the Savings plan as saved when the planner opened: "Everything else"
  // is already net of it, so only a change from it moves the pool
  const [savingsSeed, setSavingsSeed] = useState(0);
  // the numbers came from the suggestion, not a saved plan: only then
  // does the history line describe what the person is looking at
  const [fromHistory, setFromHistory] = useState(false);
  // a typed amount read by the server's separator rules; not a number is 0
  const numv = (v: string) => parseMoneyDraft(v) ?? 0;
  // a plan opened from what is saved shows history's figure beside each
  // row, as sent by the server (suggestions); a plan seeded from history
  // IS that figure, and a server without the field shows none
  const hint = (v: number | undefined) =>
    fromHistory || typeof v !== "number" ? null : (
      <small className="mut bp-hint" title="your typical month, from history"
             style={{ marginLeft: ".4rem", whiteSpace: "nowrap" }}>
        history ~{money(v)}</small>);
  const rowHint = (r: CRow) => hint(r.food
    ? sug.data?.suggestions.food_monthly
    : r.orig ? sug.data?.suggestions.buckets[r.orig] : undefined);
  // unsaved edits signal — the Budget page skips remounting
  // this card after a sibling's save while it holds a draft
  // …and this card's own: while anything is unsaved a bar with Save and
  // Discard stays on screen, because the Save button sits at the foot of a
  // long form and a changed number is easy to walk away from
  const [dirty, setDirty] = useState(false);
  const touch = () => { setDirty(true); onDirtyChange?.(true); };
  const discard = () => {
    setUnallocEdited(null);
    setSavingsTouched(false);
    setDirty(false);
    onDirtyChange?.(false);
    setInit(false);            // re-seed every field from what is saved
  };

  // one-time init: standalone treats a saved config as authoritative; the
  // wizard opens on the household's plan whenever it saved one — the
  // server's budgets_saved_by_user (planSaved), which the first-run seed
  // stored once a month has closed never sets — so a re-run never offers
  // a different plan than the one Continue keeps, and a first run is
  // never opened on the machine's guess. Only a household without one is
  // seeded from history (specs/wizard-budgets-refine.md, "Running setup
  // again")
  const userSavedOf = (sd: NonNullable<typeof s.data>,
                       od: NonNullable<typeof ob.data>) => standalone
    ? Boolean(sd.food_monthly || sd.other_monthly
              || sd.budgeted_income_monthly)
    : planSaved(od);
  useEffect(() => {
    if (init || !s.data || !ob.data || now.isPending) return;
    const userSaved = userSavedOf(s.data, ob.data);
    // A saved plan is seeded from settings the moment they land: the
    // suggestion query is a median over the whole history and was holding
    // the card at "Loading the plan…" for users whose numbers it does not
    // even decide. Only a first run waits for it — for its answer: a
    // failed suggestion is not a history of $0
    if (!userSaved && !sug.data) return;
    const pick = (saved: number | null | undefined, suggested?: number) =>
      userSaved && saved && saved > 0 ? saved
        : suggested ? Math.round(suggested)
        : saved && saved > 0 ? saved : 0;
    // Number fields: keep "0" when history truly has none — `x || ""`
    // turned food $0 into a blank input that looked broken.
    const numStr = (n: number) => String(n || 0);
    setIncome(numStr(pick(s.data.budgeted_income_monthly,
                          sug.data?.suggestions.income_monthly)));
    const foodVal = pick(s.data.food_monthly,
                         sug.data?.suggestions.food_monthly);
    const { base, rows: existing } =
      plannerBucketsSaved(s.data.custom_buckets ?? []);
    setBaseNames(base);
    // Wizard first pass: pre-add the history carve-outs (gas, medical, …)
    // so step 3 lands with real category estimates, not only Food + a
    // giant Unallocated. Standalone / already-saved configs keep rows as-is.
    const seeded = (!userSaved && existing.length === 0)
      ? (sug.data?.bucket_candidates ?? []).map((c) => ({
          name: c.name,
          monthly: String(Math.round(c.monthly) || 0),
          category: c.category,
          cats: [c.category],
        }))
      : [];
    setRows([{ name: "Food", monthly: numStr(foodVal), food: true },
             ...existing, ...seeded]);
    const seededTotal = seeded.reduce(
      (t, b) => t + (Number(b.monthly) || 0), 0);
    const existingTotal = existing.reduce(
      (t, b) => t + (Number(b.monthly) || 0), 0);
    // other_monthly is the full non-food variable pool; carve-outs live
    // inside it and shrink Unallocated — never add them on top
    setOtherBase(Math.max(
      pick(s.data.other_monthly, sug.data?.suggestions.other_monthly),
      Math.round(existingTotal + seededTotal)));
    const savedGoal = (s.data.savings_goals ?? [])
      .find((g) => isPlanSavingsGoal(g.name));
    // the ROW's presence means the user has spoken — an explicit
    // $0 is a remembered choice, not an invitation to re-prefill the
    // leftover. Only a truly absent goal lets the auto value fill first.
    if (userSaved && savedGoal != null) {
      setSavings(String(savedGoal.monthly_plan ?? 0));
      setSavingsSeed(numv(String(savedGoal.monthly_plan ?? 0)));
      setSavingsTouched(true);
    }
    setFromHistory(!userSaved);
    setInit(true);
  }, [init, s.data, ob.data, sug.data, standalone,
      now.isPending]);

  // Every figure below is worked out from the header's bills; until that
  // read has answered, none is shown and nothing is saved. A first run
  // also waits for the suggestion its rows are filled in from.
  const firstRun = !init && !!s.data && !!ob.data
    && !userSavedOf(s.data, ob.data);
  const asksTyped = fromHistory && !tracked && !!sug.data
    && sug.data.suggestions.income_monthly == null;
  const [typedAsked, setTypedAsked] = useState("");
  useEffect(() => {
    if (!asksTyped) return;
    const t = setTimeout(() => setTypedAsked(income.trim()), 400);
    return () => clearTimeout(t);
  }, [asksTyped, income]);
  const sugTyped = useQuery({
    queryKey: ["budgets-suggest", "typed", typedAsked],
    queryFn: () => api.budgetsSuggestTyped(typedAsked),
    enabled: asksTyped && typedAsked !== "" });
  const waits = plannerWaits(inputPhase(now),
                             firstRun ? inputPhase(sug) : "ready");
  // the header's bills (month_status avg_bills): pending proposals count
  // once approved, never before
  const bills = now.data ? now.data.avg_bills : null;
  const billsCount = now.data ? now.data.avg_bills_count : null;
  const foodVal = numv(rows.find((r) => r.food)?.monthly ?? "");
  const catsTotal = rows.filter((r) => !r.food)
    .reduce((t, r) => t + numv(r.monthly), 0);
  // A savings amount the person TYPES comes out of "Everything else": the
  // catch-all shrinks by that much so the plan still balances instead of
  // the same dollars sitting in both boxes. An explicit Everything-else
  // edit still wins over the auto value.
  const unallocAuto = plannerUnallocAuto({ otherBase, catsTotal,
    savingsTouched, savingsTyped: numv(savings), savingsSeed });
  const unalloc = unallocEdited !== null ? numv(unallocEdited) : unallocAuto;
  // a tracked income is the server's figure; only a typed one is the box's
  const inc = tracked ? (now.data?.income ?? 0) : numv(income);
  // The excess is the server's (plan_surplus: every goal's plan comes off
  // it); the planner only moves it by what is typed and not yet saved.
  // The goal this card owns counts when the server counts it, or when it
  // does not exist yet (it is saved as a monthly plan).
  const planGoals = now.data?.plan_goals ?? [];
  const ownedPlan = planGoals.find((g) => isPlanSavingsGoal(g.name));
  const ownedStored = (s.data?.savings_goals ?? [])
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
  // otherwise. Nothing is worked out here; none sent is no suggestion.
  // A first run whose history names no income (the suggestion sent no
  // income_monthly) asks the same door again with the take-home typed in
  // the box, once typing pauses; the server measures the savings on it.
  // Only the reply for what the box holds now is printed.
  const typedNow = income.trim();
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
  const savingsShown = savingsTouched ? savings : String(savingsAuto || "");
  const outTotal = (bills ?? 0) + foodVal + catsTotal + unalloc;
  const shown = preview(numv(savingsShown));
  // null: no income to plan on yet, so no excess figure to show
  const leftover = shown.excess === null ? null : Math.round(shown.excess);
  const over = leftover ?? 0;

  const setRow = (i: number, patch: Partial<CRow>) => {
    touch();
    setRows(rows.map((x, j) => (j === i ? { ...x, ...patch, touched: true }
                                        : x)));
  };
  const addRow = (row: CRow) => { touch(); setRows([...rows, row]); };
  const candidates = [
    ...(rows.some((r) => r.food) ? [] : [{
      name: "Food", category: "__food__",
      monthly: Math.round(sug.data?.suggestions.food_monthly ?? 0) }]),
    ...(sug.data?.bucket_candidates ?? [])
      .filter((c) => !rows.some((b) => b.category === c.category
        || (b.cats ?? []).includes(c.category)
        || b.name.toLowerCase() === c.name.toLowerCase())),
  ];

  const save = useMutation({
    // every Budget-page list save reads the server fresh, merges and
    // writes the whole list back: one shared queue, so no two of them
    // interleave and erase each other's rows
    scope: SETTINGS_SAVES,
    // the rows being saved ride along, so success can tell whether the
    // person kept typing while the save was in flight
    mutationFn: async (sent: CRow[]) => {
      // a plan worked out from a missing input is never saved
      if (holds) throw new Error(holds);
      // /api/settings replaces each list key wholesale, and the
      // planner only OWNS the other-parent rows and the "Savings" goal —
      // the food-parent buckets and the other goals it must carry through
      // are someone else's (Fine-tune card, Savings-goals card, another
      // tab). Merge them from the server's CURRENT state, not from the
      // react-query cache, so a save can't resurrect a stale copy of a
      // sibling's list.
      const cur = await api.settings();
      const body: Record<string, unknown> = {
        food_monthly: foodVal,
        other_monthly: unalloc + catsTotal,
      };
      // the typed figure is only the plan's income while nothing is
      // tracked; a tracked one is changed where it comes from, so the
      // stored stand-in is left as it was
      if (!tracked) body.budgeted_income_monthly = income;
      // the rows were seeded at mount and the Bucket-rules card (or
      // another device) may have saved since: merge, never rebuild — see
      // mergePlannerBuckets for who wins what
      const merged = mergePlannerBuckets(sent, cur.custom_buckets ?? [],
                                         baseNames);
      if (merged.length || (cur.custom_buckets ?? []).length) {
        body.custom_buckets = merged;
      }
      // ALWAYS persist the Savings row, $0 included — deleting it on zero
      // makes the planner forget an explicit "no savings plan" and
      // re-prefill the leftover on every return visit
      const plan = numv(savingsShown);
      const others = (cur.savings_goals ?? [])
        .filter((g) => !isPlanSavingsGoal(g.name));
      const curGoal = (cur.savings_goals ?? [])
        .find((g) => isPlanSavingsGoal(g.name));
      // the stored goal keeps its own spelling: renaming it here would
      // be an edit the person never made
      body.savings_goals = [...others,
        { ...(curGoal ?? { name: "Savings", target: 0,
                           tokens: [], start_balance: 0 }),
          monthly_plan: plan }];
      // the save returns the full settings view; seeding the cache with it
      // means every card bound to ["settings"] flips as the response lands
      return saveSettings(qc, body, cur);
    },
    onSuccess: (v, sent) => {
      // every edit makes a new rows array: the same array means nothing
      // was typed since the save began, and only then do the saved
      // buckets replace the rows — edits typed in flight stay, unsaved
      const edited = rowsNow.current !== sent;
      // the saved view is the new merge base; mid-edit, only the names
      // the sent rows held (see plannerBucketsSaved)
      const next = plannerBucketsSaved(v.custom_buckets ?? [],
                                       edited ? sent : undefined);
      setBaseNames(next.base);
      if (!edited) setRows([...sent.filter((r) => r.food), ...next.rows]);
      if (!edited) { setDirty(false); onDirtyChange?.(false); }
      onSaved?.();
    },
  });

  // a read the plan is seeded from failed: say so, never "loading…"
  // forever, and never a plan filled in from nothing
  const seedFailed = init ? null
    : (!s.data && s.isError) || (!ob.data && ob.isError) ? SAVE_WAITS_FAILED
    : firstRun && !sug.data && sug.isError ? PLANNER_HISTORY_FAILED : null;
  if (seedFailed)
    return (
      <p className="mut">{seedFailed}{" "}
        <button type="button" onClick={() => {
          void s.refetch(); void ob.refetch(); void sug.refetch(); }}>
          Try again</button></p>
    );
  if (s.isPending || !init)
    return <span className="mut">loading…</span>;
  const panel: CSSProperties = {
    flex: "1 1 18rem", padding: "1rem", borderRadius: "var(--rs)",
    background: "var(--hover)", border: "1px solid var(--line)",
  };
  const seg = (v: number) => inc > 0 ? Math.max(0, (100 * v) / inc) : 0;
  // every money-out split wears the Money-out amber (clearly separated);
  // save wears the Savings blue — the bars echo the panel headings
  const OUT = "var(--amber, #e6b159)";
  const SAVE = "var(--blue, #4f9bd6)";
  const parts = [
    // the plan's bills leg in the server's words ("Bills & envelopes"
    // with a pool); older-server path: stands in for `plan_bills_label`
    { label: billsLabel, v: bills ?? 0 },
    ...rows.filter((r) => numv(r.monthly) > 0)
      .map((r, i) => ({ label: r.name || `category ${i + 1}`,
                        v: numv(r.monthly) })),
    { label: "Unallocated", v: unalloc },
  ].filter((x) => x.v > 0);
  // the shares of the income, by the server's largest-remainder rule
  // (plannerShares): out, save and the excess add up to 100 when they are
  // the whole income; each money-out segment is a share of the income too
  const livePct = plannerShares(
    [outTotal, shown.saved, Math.max(over, 0)], inc).map((p) => p ?? 0);
  // at rest — the figures are the saved plan's — the planner prints the
  // plan line's own whole dollars and shares, the ones the Budget header
  // prints; while something is typed away from it, the live figures
  const rest = plannerAtRest(now.data?.plan_line,
    { income: inc, out: outTotal, saved: shown.saved, excess: over });
  const [outPct, savePct, overPct] = rest
    ? [rest.outPct, rest.savePct, rest.overPct] : livePct;
  const incShown = rest ? rest.income : inc;
  const outShown = rest ? rest.out : outTotal;
  const saveShown = rest ? rest.saved : shown.saved;
  const overShown = rest ? rest.excess : over;
  const segPct = plannerShares(parts.map((x) => x.v), inc).map((p) => p ?? 0);
  const zeroRows = rows.filter((r) => r.name.trim() && numv(r.monthly) <= 0)
    .map((r) => r.name.trim());
  // no food suggestion + no food history = categorization probably hasn't
  // caught up yet
  // (not when the ledger is a day old: there is simply no history yet)
  const suggestPending = !sug.data?.not_enough_history
    && !(sug.data?.suggestions.food_monthly ?? 0);
  return (
    <div>
      <p className="mut" style={{ marginTop: 0 }}>
        <b>Give every dollar a job</b> — the numbers start from your own
        history.{standalone && <>{" "}
          <Link to="/help?topic=budget">How this works</Link></>}
      </p>
      {fromHistory && historyBasisNote(sug.data) && (
        <p className="mut" style={{ marginTop: "-.4rem", fontSize: 13 }}>
          {historyBasisNote(sug.data)}</p>
      )}
      {/* the balance graphic: in / out / save, live as fields change */}
      {inc > 0 && !waits && (
        <div style={{ margin: "0 0 1rem", padding: ".8rem 1rem",
                      borderRadius: "var(--rs)",
                      border: "1px solid var(--line)" }}>
          <div className="sub" style={{ textTransform: "uppercase",
                letterSpacing: ".03em", marginBottom: ".45rem" }}>
            Where each dollar goes</div>
          <div style={{ display: "flex", alignItems: "center",
                        gap: ".6rem" }}>
            <span className="mut" style={{ fontSize: 12, width: "8.6rem",
                  whiteSpace: "nowrap" }}>in {money(incShown)} · 100%</span>
            <div style={{ flex: 1, height: 16, borderRadius: 5,
                          background: "#3f9c5c" }} />
          </div>
          <div style={{ display: "flex", alignItems: "center",
                        gap: ".6rem", marginTop: ".35rem" }}>
            <span className="mut" style={{ fontSize: 12, width: "8.6rem",
                  whiteSpace: "nowrap" }}>
              out {money(outShown)} · {outPct}%</span>
            <div style={{ flex: 1, display: "flex", height: 16, gap: 2 }}>
              {parts.map((x, i) => (
                <div key={x.label}
                  title={`${x.label} ${money(x.v)} · ${segPct[i]}%`}
                  style={{ width: `${seg(x.v)}%`, background: OUT,
                           transition: "width .3s ease",
                           borderRadius: i === 0 ? "5px 0 0 5px"
                             : i === parts.length - 1 ? "0 5px 5px 0" : 0,
                           display: "flex", alignItems: "center",
                           justifyContent: "center", overflow: "hidden",
                           color: "var(--card)", fontSize: 10,
                           whiteSpace: "nowrap" }}>
                  {/* a label only where the segment has room for it at a
                      phone's bar width; the hover has every figure */}
                  {seg(x.v) >= 13 ? `${segPct[i]}%` : ""}</div>
              ))}
            </div>
          </div>
          <div style={{ display: "flex", alignItems: "center",
                        gap: ".6rem", marginTop: ".35rem" }}>
            <span className="mut" style={{ fontSize: 12, width: "8.6rem",
                  whiteSpace: "nowrap" }}>
              save {money(saveShown)} · {savePct}%</span>
            <div style={{ flex: 1, display: "flex", height: 16 }}>
              <div title={`savings / investment ${money(shown.saved)} · ${savePct}%`}
                style={{ width: `${seg(shown.saved)}%`,
                         borderRadius: 5, background: SAVE,
                         transition: "width .3s ease" }} />
              {over > 0 && (
                <div title={`excess cash ${money(over)} — stays in checking`}
                  style={{ width: `${seg(over)}%`, borderRadius: 5,
                           marginLeft: 2, transition: "width .3s ease",
                           background: "repeating-linear-gradient(45deg, transparent, transparent 4px, var(--line) 4px, var(--line) 8px)" }} />
              )}
            </div>
          </div>
          {/* No legend row: it would list every segment with its dollar
              amount and percentage — the same figures the bars already
              carry as inline labels and tooltips, and the same figures the
              panels below hold as editable inputs. Three statements of one
              number on one card. Only the two facts the bars CANNOT show
              are stated here. */}
          {over > 0 && (
            <div className="mut" style={{ marginTop: ".5rem", fontSize: 13 }}>
              Excess cash <b style={{ color: "var(--ink)" }}>{money(overShown)}</b>
              {" "}· {overPct}% — stays in checking.
            </div>
          )}
          {over < 0 && (
            <div style={{ marginTop: ".5rem", fontSize: 13 }}>
              <b style={{ color: "var(--red)" }}>
                Plan exceeds income by {money(-over)}</b>
            </div>
          )}
        </div>
      )}
      <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap" }}>
        <div style={panel}>
          <h3 style={{ marginTop: 0, color: "var(--green, #4a4)" }}>
            ⬇ Money in</h3>
          {tracked ? (<>
            <p>Money in this month{" "}
              <span className="sub">(take-home)</span><br />
              <b style={{ fontSize: "1.25rem" }}>{money(incShown)}</b></p>
            <p className="mut" style={{ fontSize: 13, marginBottom: 0 }}>
              {/* every deposit that landed, not paychecks alone: paychecks
                  are named only beside the portion the server sends */}
              {now.data?.income_source === "paychecks"
                ? <>What landed this month
                    {(now.data?.income_expected ?? 0) >= 0.5
                      ? <>, plus {money(now.data!.income_expected!)} of
                          paychecks still due</> : null}. {standalone
                      ? <Link to="/bills">Change them on Bills</Link>
                      : "Go Back to change them"}.</>
                : <>From the active income scenario. Change it under Income
                    scenarios.</>}</p>
          </>) : (<>
            <p><label>Expected income / month{" "}
              <span className="sub">(take-home)</span>
              <br />
              <input inputMode="decimal" value={income}
                onChange={(e) => { touch(); setIncome(e.target.value); }}
                style={{ width: "12rem", padding: ".5rem",
                         background: "var(--hover)",
                         border: "1px solid var(--line)",
                         borderRadius: "var(--rs)", color: "var(--ink)" }} />
            </label></p>
            <p className="mut" style={{ fontSize: 13, marginBottom: 0 }}>
              The plan counts this figure until a paycheck is tracked on
              Bills; then it counts this month&apos;s checks.</p>
          </>)}
        </div>
        <div style={{ ...panel, flex: "2 1 24rem" }}>
          <h3 style={{ marginTop: 0, color: "var(--amber, #e6b159)" }}>
            ⬆ Money out</h3>
          <div style={{ display: "flex", justifyContent: "space-between",
                        alignItems: "baseline", padding: ".25rem 0" }}>
            <span><b>{billsLabel}</b>{" "}
              <span className="sub">({billsCount !== null
                  ? `${billsCount} tracked, ` : ""}evened out —{" "}
                {standalone
                  ? <Link to="/bills">adjust</Link>
                  : "go Back to adjust"})</span></span>
            <b style={{ whiteSpace: "nowrap" }}>{money(bills)}</b>
          </div>
          <div className="sub" style={{ margin: ".45rem 0 .15rem",
                textTransform: "uppercase", letterSpacing: ".03em" }}>
            + Budget categories</div>
          {rows.map((r, i) => (
            <div key={i} className="bp-row" style={{ display: "flex",
                  justifyContent: "space-between", alignItems: "center",
                  flexWrap: "wrap", gap: ".2rem .5rem",
                  padding: ".15rem 0" }}>
              {/* the name keeps room to be read: at phone width the amount
                  and its history wrap under it rather than squeeze it */}
              <span className="bp-name" style={{ display: "flex", gap: ".35rem",
                    alignItems: "center", flex: "1 1 10rem", minWidth: 0 }}>
                {/* Food's name is the plan's own: no save carries it */}
                <input aria-label="Category name" readOnly={!!r.food}
                       value={r.name} placeholder="category name" size={16}
                  maxLength={40} style={{ flex: "1 1 auto", minWidth: "6rem" }}
                  onChange={(e) => setRow(i, { name: e.target.value })} />
                <button type="button" title="remove" aria-label="Remove this category"
                  style={{ fontSize: 12, padding: "0 .4rem" }}
                  onClick={() => { touch();
                                   setRows(rows.filter((_, j) => j !== i)); }}>
                  ✕</button>
              </span>
              <span style={{ whiteSpace: "nowrap" }}>$ <input aria-label={`${r.name || "Category"} monthly amount`}
                inputMode="decimal" value={r.monthly} size={7}
                style={{ textAlign: "right" }}
                onChange={(e) => setRow(i, { monthly: e.target.value })} />
                {rowHint(r)}
              </span>
            </div>
          ))}
          {/* suggestions: one per line, each a real number from history */}
          <div style={{ margin: ".3rem 0 .1rem" }}>
            {candidates.map((c) => (
              <div key={c.category} style={{ margin: ".25rem 0" }}>
                <button type="button" style={{ fontSize: 13 }}
                  title="add as a budget category — amount from your 3-month median"
                  onClick={() => addRow(c.category === "__food__"
                    ? { name: "Food", monthly: String(c.monthly),
                        food: true }
                    : { name: c.name, monthly: String(c.monthly),
                        category: c.category })}>
                  + {c.name} ~{money(c.monthly)}/mo</button>
              </div>
            ))}
            <div style={{ margin: ".25rem 0" }}>
              <button type="button" style={{ fontSize: 13 }}
                onClick={() => addRow({ name: "", monthly: "" })}>
                + add category</button>
            </div>
          </div>
          <div style={{ display: "flex", justifyContent: "space-between",
                        alignItems: "center", padding: ".3rem 0 0",
                        flexWrap: "wrap", gap: ".2rem .5rem",
                        marginTop: ".35rem",
                        borderTop: "1px solid var(--line)" }}>
            <span style={{ flex: "1 1 10rem" }}>Everything else <span className="sub">(unallocated —
              auto-shrinks as categories claim it)</span></span>
            <span style={{ whiteSpace: "nowrap" }}>$ <input aria-label="Everything else, monthly amount"
              inputMode="decimal"
              value={unallocEdited ?? String(unallocAuto || "")} size={7}
              style={{ textAlign: "right" }}
              onChange={(e) => { touch();
                                 setUnallocEdited(e.target.value); }} />
              {hint(sug.data?.suggestions.unallocated_monthly)}</span>
          </div>
        </div>
        <div style={panel}>
          <h3 style={{ marginTop: 0, color: "var(--blue, #4f9bd6)" }}>
            Savings / Investment</h3>
          <p><label>Save / month{" "}
            <span className="sub">(what's left of the money in)</span>
            <br />
            <input inputMode="decimal" value={savingsShown}
              placeholder={savingsAuto === null ? UNKNOWN_FIGURE : undefined}
              onChange={(e) => { touch(); setSavings(e.target.value);
                                 setSavingsTouched(true); }}
              style={{ width: "12rem", padding: ".5rem",
                       background: "var(--hover)",
                       border: "1px solid var(--line)",
                       borderRadius: "var(--rs)", color: "var(--ink)" }} />
            {/* a typed or saved value is never overwritten — the server's
                suggestion arrives as an approve-each chip, like the budget
                suggestions */}
            {savingsTouched && savingsAuto !== null && savingsAuto > 0
              && savingsAuto !== numv(savingsShown) && (
              <span className="mut" style={{ fontSize: 12,
                    whiteSpace: "nowrap" }}>
                {" "}suggested {money(savingsAuto)}{" "}
                <button type="button"
                  style={{ fontSize: 12, padding: "0 .4rem" }}
                  onClick={() => setSavings(String(savingsAuto))}>use</button>
              </span>
            )}
          </label></p>
          <p className="mut" style={{ fontSize: 13, marginBottom: 0 }}>
            {savingsTouched ? "Your number."
              : savingsAuto !== null
                ? "Suggested: what an ordinary month leaves."
                : "No suggestion yet."}
            {" "}Saved as your “Savings” goal.</p>
          {/* every other goal's plan comes off the excess too; they are
              edited under Savings goals, so they are listed, not typed */}
          {otherGoals.map((g) => (
            <p key={g.name} className="mut" style={{ fontSize: 13,
                  margin: ".35rem 0 0", display: "flex",
                  justifyContent: "space-between", gap: ".6rem" }}>
              <span>{g.name}</span>
              <b style={{ color: "var(--ink)", whiteSpace: "nowrap" }}>
                {money(g.monthly_plan)}/mo</b></p>
          ))}
          {leftover !== null && !waits && (
            <p className="mut" style={{ fontSize: 13, marginBottom: 0,
                  marginTop: ".5rem" }}>
              Excess cash <b className={over < 0 ? "neg" : undefined}
                style={over < 0 ? undefined : { color: "var(--ink)" }}>
                {money(over)}</b>/mo — {over < 0
                  ? "the plan spends more than comes in"
                  : "unplanned; stays in checking"}.</p>
          )}
        </div>
      </div>
      {/* a category with a $0 plan is almost always the categorize pass
          not having finished when the step loaded — a bucket saved at $0
          while real spending exists in it. Say so before the save, don't
          fail silently. */}
      {zeroRows.length > 0 && (
        <p className="mut" style={{ margin: ".7rem 0 0", fontSize: 13,
              color: "var(--amber)" }}>
          ⚠ {zeroRows.join(", ")} {zeroRows.length === 1 ? "has" : "have"} no
          budget — saving now leaves {zeroRows.length === 1 ? "it" : "them"}
          {" "}unbudgeted (spending there won't count toward the verdict).
          {suggestPending
            ? " Smart categorization may still be running — reload in a minute for a suggestion."
            : ""}
        </p>
      )}
      {/* Wizard: flush right above the step’s Continue button. Standalone
          /balance keeps the natural left stack. */}
      <div style={{ marginTop: ".8rem",
                    display: "flex", flexWrap: "wrap", gap: ".6rem",
                    alignItems: "center",
                    justifyContent: standalone ? "flex-start" : "flex-end" }}>
        {holds && <span className="mut">{holds}</span>}
        {save.isError && <span className="mut">{errText(save.error)}</span>}
        {save.isSuccess && !standalone && (
          <span className="mut">Saved — Continue below.</span>)}
        {save.isSuccess && standalone && (
          <span className="mut">Saved.</span>)}
        <button className="pri" disabled={save.isPending || !!holds}
          onClick={() => save.mutate(rows)}>
          {save.isPending ? "saving…" : "Save budgets"}
        </button>
      {dirty && (
        <div className="savebar" role="status">
          <span style={{ flex: 1 }}>Unsaved changes</span>
          <button disabled={save.isPending} onClick={discard}>Discard</button>
          <button className="pri" disabled={save.isPending || !!holds}
                  onClick={() => save.mutate(rows)}>
            {save.isPending ? "saving…" : "Save budgets"}</button>
        </div>
      )}
      </div>
    </div>
  );
}
