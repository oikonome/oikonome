import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type CSSProperties, useEffect, useState } from "react";
import { Link } from "react-router";
import { api } from "../api/client";
import { saveSettings } from "../api/cache";

// The balance-your-budget planner — the wizard's budgets step AND the
// standing /balance page share it.
//
// Money model: income and bills are fixed inputs; every
// budget category (Food included) is a row; UNALLOCATED is what history
// says you spend outside the named categories and auto-shrinks as
// categories absorb it; SAVINGS is what's left of income after all of
// it. Adding/raising a category therefore drains unallocated first,
// then savings — money is conserved, nothing double-counts.
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
  interface CRow { name: string; monthly: string; food?: boolean;
                   category?: string; cats?: string[]; merch?: string[] }
  const [init, setInit] = useState(false);
  const [income, setIncome] = useState("");
  const [rows, setRows] = useState<CRow[]>([]);
  // base for the unallocated auto value: history's everything-else
  const [otherBase, setOtherBase] = useState(0);
  const [unallocEdited, setUnallocEdited] = useState<string | null>(null);
  const [savings, setSavings] = useState("");
  const [savingsTouched, setSavingsTouched] = useState(false);
  const numv = (v: string) => Number((v || "0").replace(/[$,\s]/g, "")) || 0;
  // unsaved edits signal — the Budget page skips remounting
  // this card after a sibling's save while it holds a draft
  const touch = () => onDirtyChange?.(true);

  // one-time init: LIVE suggestion wins until the user has saved (the
  // first-run auto-seed can predate categorization); standalone treats a
  // saved config as authoritative
  useEffect(() => {
    if (init || !s.data || !ob.data) return;
    const userSaved = standalone
      ? Boolean(s.data.food_monthly || s.data.other_monthly
                || s.data.budgeted_income_monthly)
      : ob.data.wizard_steps?.budgets === "done";
    // A saved plan is seeded from settings the moment they land: the
    // suggestion query is a median over the whole history and was holding
    // the card at "Loading the plan…" for users whose numbers it does not
    // even decide. Only a first run waits for it.
    if (!userSaved && sug.isPending) return;
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
    const existing = (s.data.custom_buckets ?? [])
      .filter((b) => b.parent === "other")
      .map((b) => ({ name: b.name, monthly: String(b.monthly || 0),
                     cats: b.categories ?? [], merch: b.merchants ?? [] }));
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
      .find((g) => g.name === "Savings");
    // the ROW's presence means the user has spoken — an explicit
    // $0 is a remembered choice, not an invitation to re-prefill the
    // leftover. Only a truly absent goal lets the auto value fill first.
    if (userSaved && savedGoal != null) {
      setSavings(String(savedGoal.monthly_plan ?? 0));
      setSavingsTouched(true);
    }
    setInit(true);
  }, [init, s.data, ob.data, sug.data, sug.isPending, standalone]);

  const bills = sug.data?.avg_bills_monthly ?? 0;
  const billsCount = sug.data?.bills_count ?? 0;
  const foodVal = numv(rows.find((r) => r.food)?.monthly ?? "");
  const catsTotal = rows.filter((r) => !r.food)
    .reduce((t, r) => t + numv(r.monthly), 0);
  // A savings amount the person TYPES comes out of "Everything else": the
  // catch-all shrinks by that much so the plan still balances instead of
  // the same dollars sitting in both boxes. An explicit Everything-else
  // edit still wins over the auto value.
  const savingsTyped = savingsTouched ? numv(savings) : 0;
  const unallocAuto = Math.max(0, Math.round(otherBase - catsTotal
                                              - savingsTyped));
  const unalloc = unallocEdited !== null ? numv(unallocEdited) : unallocAuto;
  const inc = numv(income);
  const savingsAuto = Math.max(0, Math.round(
    inc - bills - foodVal - catsTotal - unalloc));
  const savingsShown = savingsTouched ? savings : String(savingsAuto || "");
  const outTotal = bills + foodVal + catsTotal + unalloc;
  const leftover = Math.round(inc - outTotal - numv(savingsShown));

  const setRow = (i: number, patch: Partial<CRow>) => {
    touch();
    setRows(rows.map((x, j) => (j === i ? { ...x, ...patch } : x)));
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
    mutationFn: async () => {
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
        budgeted_income_monthly: income,
      };
      const catRows = rows
        .filter((b) => !b.food && b.name.trim() && numv(b.monthly) > 0)
        .map((b) => ({ name: b.name.trim(), parent: "other",
                       monthly: numv(b.monthly),
                       categories: b.cats ?? (b.category ? [b.category] : []),
                       merchants: b.merch ?? [] }));
      const foodBuckets = (cur.custom_buckets ?? [])
        .filter((b) => b.parent === "food");
      if (catRows.length || foodBuckets.length
          || (cur.custom_buckets ?? []).length) {
        body.custom_buckets = [...foodBuckets, ...catRows];
      }
      // ALWAYS persist the Savings row, $0 included — deleting it on zero
      // makes the planner forget an explicit "no savings plan" and
      // re-prefill the leftover on every return visit
      const plan = numv(savingsShown);
      const others = (cur.savings_goals ?? [])
        .filter((g) => g.name !== "Savings");
      const curGoal = (cur.savings_goals ?? [])
        .find((g) => g.name === "Savings");
      body.savings_goals = [...others,
        { ...(curGoal ?? { name: "Savings", target: 0,
                           tokens: [], start_balance: 0 }),
          monthly_plan: plan }];
      // the save returns the full settings view; seeding the cache with it
      // means every card bound to ["settings"] flips as the response lands
      return saveSettings(qc, body);
    },
    onSuccess: () => { onDirtyChange?.(false); onSaved?.(); },
  });

  if (s.isPending || !init)
    return <span className="mut">loading…</span>;
  const panel: CSSProperties = {
    flex: "1 1 18rem", padding: "1rem", borderRadius: "var(--rs)",
    background: "var(--hover)", border: "1px solid var(--line)",
  };
  const seg = (v: number) => inc > 0 ? Math.max(0, (100 * v) / inc) : 0;
  const money = (n: number) => "$" + Math.round(n).toLocaleString();
  // every money-out split wears the Money-out amber (clearly separated);
  // save wears the Savings blue — the bars echo the panel headings
  const OUT = "var(--amber, #e6b159)";
  const SAVE = "var(--blue, #4f9bd6)";
  const parts = [
    { label: "Bills", v: bills },
    ...rows.filter((r) => numv(r.monthly) > 0)
      .map((r, i) => ({ label: r.name || `category ${i + 1}`,
                        v: numv(r.monthly) })),
    { label: "Unallocated", v: unalloc },
  ].filter((x) => x.v > 0);
  const pct = (v: number) => inc > 0 ? Math.round((100 * v) / inc) : 0;
  const zeroRows = rows.filter((r) => r.name.trim() && numv(r.monthly) <= 0)
    .map((r) => r.name.trim());
  // no food suggestion + no food history = categorization probably hasn't
  // caught up yet
  const suggestPending = !(sug.data?.suggestions.food_monthly ?? 0);
  return (
    <div>
      <p className="mut" style={{ marginTop: 0 }}>
        <b>Give every dollar a job</b>: start from income, cover the
        bills, put a monthly number on each budget category, and what's
        left becomes savings. Everything is pre-filled from your real
        history — adjust any number and the picture updates.
      </p>
      {/* the balance graphic: in / out / save, live as fields change */}
      {inc > 0 && (
        <div style={{ margin: "0 0 1rem", padding: ".8rem 1rem",
                      borderRadius: "var(--rs)",
                      border: "1px solid var(--line)" }}>
          <div className="sub" style={{ textTransform: "uppercase",
                letterSpacing: ".03em", marginBottom: ".45rem" }}>
            Where each dollar goes</div>
          <div style={{ display: "flex", alignItems: "center",
                        gap: ".6rem" }}>
            <span className="mut" style={{ fontSize: 12, width: "8.6rem",
                  whiteSpace: "nowrap" }}>in {money(inc)} · 100%</span>
            <div style={{ flex: 1, height: 16, borderRadius: 5,
                          background: "#3f9c5c" }} />
          </div>
          <div style={{ display: "flex", alignItems: "center",
                        gap: ".6rem", marginTop: ".35rem" }}>
            <span className="mut" style={{ fontSize: 12, width: "8.6rem",
                  whiteSpace: "nowrap" }}>
              out {money(outTotal)} · {pct(outTotal)}%</span>
            <div style={{ flex: 1, display: "flex", height: 16, gap: 2 }}>
              {parts.map((x, i) => (
                <div key={x.label}
                  title={`${x.label} ${money(x.v)} · ${pct(x.v)}%`}
                  style={{ width: `${seg(x.v)}%`, background: OUT,
                           transition: "width .3s ease",
                           borderRadius: i === 0 ? "5px 0 0 5px"
                             : i === parts.length - 1 ? "0 5px 5px 0" : 0,
                           display: "flex", alignItems: "center",
                           justifyContent: "center", overflow: "hidden",
                           color: "#1c2430", fontSize: 10,
                           whiteSpace: "nowrap" }}>
                  {seg(x.v) >= 6 ? `${pct(x.v)}%` : ""}</div>
              ))}
            </div>
          </div>
          <div style={{ display: "flex", alignItems: "center",
                        gap: ".6rem", marginTop: ".35rem" }}>
            <span className="mut" style={{ fontSize: 12, width: "8.6rem",
                  whiteSpace: "nowrap" }}>
              save {money(numv(savingsShown))} · {pct(numv(savingsShown))}%</span>
            <div style={{ flex: 1, display: "flex", height: 16 }}>
              <div title={`savings / investment ${money(numv(savingsShown))} · ${pct(numv(savingsShown))}%`}
                style={{ width: `${seg(numv(savingsShown))}%`,
                         borderRadius: 5, background: SAVE,
                         transition: "width .3s ease" }} />
              {leftover > 0 && (
                <div title={`excess cash ${money(leftover)} — stays in checking`}
                  style={{ width: `${seg(leftover)}%`, borderRadius: 5,
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
          {leftover > 0 && (
            <div className="mut" style={{ marginTop: ".5rem", fontSize: 13 }}>
              Excess cash <b style={{ color: "var(--ink)" }}>{money(leftover)}</b>
              {" "}· {pct(leftover)}% — stays in checking.
            </div>
          )}
          {leftover < 0 && (
            <div style={{ marginTop: ".5rem", fontSize: 13 }}>
              <b style={{ color: "var(--red)" }}>
                Plan exceeds income by {money(-leftover)}</b>
            </div>
          )}
        </div>
      )}
      <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap" }}>
        <div style={panel}>
          <h3 style={{ marginTop: 0, color: "var(--green, #4a4)" }}>
            ⬇ Money in</h3>
          <p><label>Expected income / month{" "}
            <span className="sub">(take-home; powers the cash forecast)</span>
            <br />
            <input inputMode="decimal" value={income}
              onChange={(e) => { touch(); setIncome(e.target.value); }}
              style={{ width: "12rem", padding: ".5rem",
                       background: "var(--hover)",
                       border: "1px solid var(--line)",
                       borderRadius: "var(--rs)", color: "var(--ink)" }} />
          </label></p>
          <p className="mut" style={{ fontSize: 13, marginBottom: 0 }}>
            Inferred from the paychecks detected in your ledger.</p>
        </div>
        <div style={{ ...panel, flex: "2 1 24rem" }}>
          <h3 style={{ marginTop: 0, color: "var(--amber, #e6b159)" }}>
            ⬆ Money out</h3>
          <div style={{ display: "flex", justifyContent: "space-between",
                        alignItems: "baseline", padding: ".25rem 0" }}>
            <span><b>Bills</b>{" "}
              <span className="sub">({billsCount} tracked, evened out —{" "}
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
                  padding: ".15rem 0" }}>
              <span className="bp-name" style={{ display: "flex", gap: ".35rem",
                    alignItems: "center" }}>
                <input value={r.name} placeholder="category name" size={16}
                  onChange={(e) => setRow(i, { name: e.target.value })} />
                <button type="button" title="remove"
                  style={{ fontSize: 12, padding: "0 .4rem" }}
                  onClick={() => { touch();
                                   setRows(rows.filter((_, j) => j !== i)); }}>
                  ✕</button>
              </span>
              <span style={{ whiteSpace: "nowrap" }}>$ <input
                inputMode="decimal" value={r.monthly} size={7}
                style={{ textAlign: "right" }}
                onChange={(e) => setRow(i, { monthly: e.target.value })} />
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
                  + {c.name} ~{"$"}{c.monthly}/mo</button>
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
                        marginTop: ".35rem",
                        borderTop: "1px solid var(--line)" }}>
            <span>Everything else <span className="sub">(unallocated —
              auto-shrinks as categories claim it; the rest flows to
              savings)</span></span>
            <span style={{ whiteSpace: "nowrap" }}>$ <input
              inputMode="decimal"
              value={unallocEdited ?? String(unallocAuto || "")} size={7}
              style={{ textAlign: "right" }}
              onChange={(e) => { touch();
                                 setUnallocEdited(e.target.value); }} /></span>
          </div>
        </div>
        <div style={panel}>
          <h3 style={{ marginTop: 0, color: "var(--blue, #4f9bd6)" }}>
            Savings / Investment</h3>
          <p><label>Save / month{" "}
            <span className="sub">(what's left of the money in)</span>
            <br />
            <input inputMode="decimal" value={savingsShown}
              onChange={(e) => { touch(); setSavings(e.target.value);
                                 setSavingsTouched(true); }}
              style={{ width: "12rem", padding: ".5rem",
                       background: "var(--hover)",
                       border: "1px solid var(--line)",
                       borderRadius: "var(--rs)", color: "var(--ink)" }} />
            {/* a saved value is never overwritten — the leftover
                arrives as an approve-each chip, like the budget suggestions */}
            {savingsTouched && savingsAuto > 0
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
            {savingsTouched ? "Your number." : "What's left, automatically."}
            {" "}Saved as your “Savings” goal — put it toward savings or
            investments; add a target under Savings goals on the Budget
            page to track it.</p>
          {leftover > 0 && (
            <p className="mut" style={{ fontSize: 13, marginBottom: 0,
                  marginTop: ".5rem" }}>
              Excess cash <b style={{ color: "var(--ink)" }}>
                {money(leftover)}</b>/mo — unplanned; stays in checking.</p>
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
        {save.isError && <span className="mut">{String(save.error)}</span>}
        {save.isSuccess && !standalone && (
          <span className="mut">Saved — Continue below.</span>)}
        {save.isSuccess && standalone && (
          <span className="mut">Saved.</span>)}
        <button className="pri" disabled={save.isPending}
          onClick={() => save.mutate()}>
          {save.isPending ? "saving…" : "Save budgets"}
        </button>
      </div>
    </div>
  );
}
