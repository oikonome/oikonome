import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type ReactNode, useEffect, useState } from "react";
import { api, errText } from "../api/client";
import { saveSettings } from "../api/cache";
import { isPlanSavingsGoal, parseMoneyDraft } from "../moneymath";

// The budgets step on the hand-entry path. The household said it would set
// its own numbers, so a first visit starts blank and the history-based
// suggestion sits beside it as a one-tap chip. Bills are the ones typed in
// the step before, evened out to a month, and savings is what is left — the
// same money model as the full planner, in four boxes.
//
// A budget that is already stored (budgets_set) is shown, never hidden
// behind blank boxes: a blank box would otherwise save as $0 and wipe a
// figure the person could not see. For the same reason a box left blank
// over a stored figure keeps that figure rather than sending zero.

const whole = (n: number) => "$" + Math.round(n).toLocaleString();

export default function ManualBudgets({ onSaved }: { onSaved?: () => void }) {
  const qc = useQueryClient();
  const s = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const ob = useQuery({ queryKey: ["onboarding"], queryFn: api.onboarding });
  const sug = useQuery({ queryKey: ["budgets-suggest"],
                         queryFn: api.budgetsSuggest });
  const [income, setIncome] = useState("");
  const [food, setFood] = useState("");
  const [other, setOther] = useState("");
  const [init, setInit] = useState(false);
  const [msg, setMsg] = useState<{ text: string; bad?: boolean } | null>(null);
  // a plan this household already has — saved here, on the Budget page, or
  // before the wizard was restarted — comes back as it is stored; only a
  // household with no budget starts blank
  useEffect(() => {
    if (init || !s.data || !ob.data) return;
    if (ob.data.wizard_steps?.budgets === "done" || ob.data.budgets_set) {
      const str = (v: number | null | undefined) => (v && v > 0 ? String(v) : "");
      setIncome(str(s.data.budgeted_income_monthly));
      setFood(str(s.data.food_monthly));
      setOther(str(s.data.other_monthly));
    }
    setInit(true);
  }, [init, s.data, ob.data]);

  const num = (v: string) => parseMoneyDraft(v) ?? 0;
  // what a box left blank (or at zero) resolves to: the stored figure when
  // there is a budget to protect, else nothing. Without a stored budget the
  // settings view can carry the app's provisional seed, which is not the
  // household's and must not be counted as kept.
  const keptOf = (stored: number | null | undefined, has: boolean) =>
    has && stored && stored > 0 ? stored : 0;
  const hasBudget = !!ob.data?.budgets_set;
  const keepIncome = keptOf(s.data?.budgeted_income_monthly, true);
  const keepFood = keptOf(s.data?.food_monthly, hasBudget);
  const keepOther = keptOf(s.data?.other_monthly, hasBudget);
  const eff = (v: string, kept: number) => (num(v) > 0 ? num(v) : kept);
  const bills = sug.data?.avg_bills_monthly ?? 0;
  const savings = Math.round(eff(income, keepIncome) - bills
    - eff(food, keepFood) - eff(other, keepOther));
  const n = sug.data?.months?.length ?? 0;
  const basis = n ? ` from ${n} month${n === 1 ? "" : "s"}` : "";
  const sugg = sug.data?.suggestions;

  const save = useMutation({
    mutationFn: async (): Promise<boolean> => {
      // the goals list is replaced wholesale on save: carry every other
      // goal through from the server's current copy, and keep the stored
      // Savings goal's own spelling and target. Whether a budget is stored
      // is read fresh too — it decides whether a blank box keeps a figure.
      const [cur, o] = await Promise.all([api.settings(), api.onboarding()]);
      const has = !!o.budgets_set;
      const others = (cur.savings_goals ?? [])
        .filter((g) => !isPlanSavingsGoal(g.name));
      const goal = (cur.savings_goals ?? [])
        .find((g) => isPlanSavingsGoal(g.name));
      const body: Record<string, unknown> = {};
      // over a stored budget a blank or zero box is left out of the body,
      // so the server keeps that figure; with nothing stored there is
      // nothing to protect and the empty one is written as none
      const put = (key: string, v: string, stored: number | null | undefined,
                   protect: boolean) => {
        if (num(v) > 0) { body[key] = num(v); return num(v); }
        if (!protect) body[key] = 0;
        return keptOf(stored, protect);
      };
      // income at zero means "not set" to the server either way, so a
      // blank one is always left out and a stored figure stands
      const inc = num(income) > 0 ? num(income)
        : keptOf(cur.budgeted_income_monthly, true);
      if (num(income) > 0) body.budgeted_income_monthly = num(income);
      const f = put("food_monthly", food, cur.food_monthly, has);
      const ot = put("other_monthly", other, cur.other_monthly, has);
      body.savings_goals = [...others,
        { ...(goal ?? { name: "Savings", target: 0, tokens: [],
                        start_balance: 0 }),
          monthly_plan: Math.max(0, Math.round(inc - bills - f - ot)) }];
      await saveSettings(qc, body);
      // "Saved" is only true when the step's own gate now passes; read it
      // back rather than assume, and hand the wizard the fresh answer
      const after = await api.onboarding();
      qc.setQueryData(["onboarding"], after);
      return !!after.budgets_set;
    },
    onSuccess: (ok) => {
      setMsg(ok ? { text: "Saved — Continue below." }
        : { text: "Saved, but no food or everything-else budget is stored "
            + "yet — enter one above.", bad: true });
      qc.invalidateQueries({ queryKey: ["onboarding"] });
      qc.invalidateQueries({ queryKey: ["today"] });
      onSaved?.();
    },
    onError: (e) => setMsg({ text: errText(e), bad: true }),
  });
  const submit = () => {
    // a budget is food or everything-else above zero — the verdict's own
    // test of "the household has one", and the phone's rule for this step
    if (num(food) <= 0 && num(other) <= 0) {
      setMsg({ text: "Enter a food or everything-else budget, or tap a "
        + "suggestion.", bad: true });
      return;
    }
    save.mutate();
  };

  if (!init) return <span className="mut">loading…</span>;
  const field = (label: ReactNode, val: string, set0: (v: string) => void,
                 suggestion?: number, kept = 0) => {
    // an edit makes the last save's line stale
    const set = (v: string) => { set0(v); setMsg(null); };
    return (
    <div className="wiz-field">
      <span>{label}
        {!!suggestion && suggestion > 0 && (
          <>{" "}<button type="button" className="wiz-chip"
              title="use the suggestion"
              onClick={() => set(String(Math.round(suggestion)))}>
            suggested {whole(suggestion)}{basis}</button></>)}
      </span>
      <input inputMode="decimal"
             placeholder={kept > 0 ? `${whole(kept)} (kept)` : "$"}
             value={val} onChange={(e) => set(e.target.value)} />
    </div>
    );
  };
  return (
    <div>
      {field("Income (approved paychecks)", income, setIncome,
             sugg?.income_monthly, keepIncome)}
      <div className="wiz-field">
        <span>Bills (approved)</span>
        <input value={whole(bills)} disabled aria-label="Bills per month" />
      </div>
      {field(<b>Food &amp; drink</b>, food, setFood, sugg?.food_monthly,
             keepFood)}
      {field(<b>Everything else</b>, other, setOther,
             sugg?.other_monthly, keepOther)}
      <div className="wiz-field">
        <span>Savings (what's left)</span>
        <input value={whole(savings)} disabled aria-label="Savings per month" />
      </div>
      {savings < 0 && (
        <p className="neg" style={{ fontSize: 13, margin: ".3rem 0 0" }}>
          The plan is {whole(-savings)} over income.</p>)}
      <div style={{ marginTop: ".6rem", display: "flex", gap: ".6rem",
                    alignItems: "center", flexWrap: "wrap" }}>
        <button className="pri" disabled={save.isPending}
                onClick={submit}>
          {save.isPending ? "saving…" : "Save budgets"}</button>
        {msg && <span className={msg.bad ? "neg" : "mut"}>{msg.text}</span>}
      </div>
    </div>
  );
}
