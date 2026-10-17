// "Set your budgets" for a household that chose to enter things itself.
// The automatic path pre-fills the full planner from history; this one
// starts a first visit blank, because "I'll enter them myself" was the
// answer, and offers the history's suggestion as a chip beside each field
// — one tap to take it, nothing taken unasked.
//
// A budget that is already stored (budgets_set) is shown, never hidden
// behind blank boxes: a blank box would otherwise save as $0 and wipe a
// figure the person could not see. For the same reason a box left blank
// over a stored figure keeps that figure rather than sending zero. The
// rules live in wizard-logic.ts so plain node can pin them.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Pressable, Text, TextInput, View } from "react-native";

import { errText } from "../lib/api";
import { saveSettings } from "../lib/cache";
import { isPlanSavingsGoal, parseMoneyDraft } from "../lib/pure";
import { useSession } from "../lib/session";
import { C } from "../lib/theme";
import { keptBudget, manualBudgetsBody, manualBudgetsPrefill,
         manualBudgetsRefusal } from "./wizard-logic";
import { TAP, ws } from "./wizard-ui";

const whole = (v: number) => `$${Math.round(v).toLocaleString("en-US")}`;

function Field({ label, value, onChange, suggestion, from, kept = 0 }: {
  label: string; value: string; onChange: (v: string) => void;
  suggestion?: number; from?: number; kept?: number;
}) {
  const sug = suggestion && suggestion > 0 ? Math.round(suggestion) : 0;
  return (
    <View style={{ gap: 4 }}>
      <Text style={{ color: C.text, fontSize: 14, fontWeight: "600" }}>
        {label}</Text>
      <View style={{ flexDirection: "row", alignItems: "center", gap: 8,
                     flexWrap: "wrap" }}>
        <TextInput style={[ws.input, { flexGrow: 1, minWidth: 120 }]}
                   value={value} onChangeText={onChange}
                   placeholder={kept > 0 ? `${whole(kept)} (kept)` : "$"}
                   placeholderTextColor={C.mut}
                   keyboardType="decimal-pad" accessibilityLabel={label} />
        {sug > 0 && (
          <Pressable onPress={() => onChange(String(sug))}
                     accessibilityHint="Use the suggestion from your history"
                     style={{ minHeight: TAP, justifyContent: "center",
                              paddingHorizontal: 12, borderRadius: 999,
                              borderWidth: 1, borderColor: C.border }}>
            <Text style={{ color: C.accent, fontSize: 12 }}>
              suggested {whole(sug)}{from ? ` from ${from} months` : ""}
            </Text>
          </Pressable>
        )}
      </View>
    </View>
  );
}

export function ManualBudgets({ viewer, onSaved }: {
  viewer: boolean; onSaved: (set?: boolean) => void;
}) {
  const { client } = useSession();
  const qc = useQueryClient();
  const st = useQuery({ queryKey: ["settings"],
    queryFn: () => client!.settingsFull(), enabled: !!client });
  const ob = useQuery({ queryKey: ["onboarding"],
    queryFn: () => client!.onboarding(), enabled: !!client });
  const sug = useQuery({ queryKey: ["budgets-suggest"],
    queryFn: () => client!.budgetsSuggest(), enabled: !!client });
  const [income, setIncome] = useState("");
  const [food, setFood] = useState("");
  const [other, setOther] = useState("");
  const [init, setInit] = useState(false);
  const [msg, setMsg] = useState<{ text: string; bad?: boolean } | null>(null);
  useEffect(() => {
    if (init || !st.data || !ob.data) return;
    const p = manualBudgetsPrefill(ob.data, st.data);
    setIncome(p.income); setFood(p.food); setOther(p.other);
    setInit(true);
  }, [init, st.data, ob.data]);

  const num = (v: string) => parseMoneyDraft(v) ?? 0;
  const hasBudget = !!ob.data?.budgets_set;
  const keepIncome = keptBudget(st.data?.budgeted_income_monthly, true);
  const keepFood = keptBudget(st.data?.food_monthly, hasBudget);
  const keepOther = keptBudget(st.data?.other_monthly, hasBudget);
  const eff = (v: string, kept: number) => (num(v) > 0 ? num(v) : kept);
  const s0 = sug.data?.suggestions;
  const months = sug.data?.months?.length ?? 0;
  const bills = sug.data?.avg_bills_monthly ?? 0;
  const savings = Math.round(eff(income, keepIncome) - bills
    - eff(food, keepFood) - eff(other, keepOther));
  const save = useMutation({
    mutationFn: async (): Promise<boolean> => {
      // the goals list is replaced wholesale on save: carry every other
      // goal through from the server's current copy, and keep the stored
      // Savings goal's own spelling and target. Whether a budget is stored
      // is read fresh too — it decides whether a blank box keeps a figure.
      const [cur, o] = await Promise.all([client!.settingsFull(),
                                          client!.onboarding()]);
      const others = (cur.savings_goals ?? [])
        .filter((g) => !isPlanSavingsGoal(g.name));
      const curGoal = (cur.savings_goals ?? [])
        .find((g) => isPlanSavingsGoal(g.name));
      const { body, monthlyPlan } = manualBudgetsBody(
        { income, food, other }, cur, !!o.budgets_set, bills);
      await saveSettings(qc, client!, {
        ...body,
        savings_goals: [...others,
          { ...(curGoal ?? { name: "Savings", target: 0, tokens: [],
                             start_balance: 0 }),
            monthly_plan: monthlyPlan }],
      });
      // "Saved" is only true when the step's own gate now passes; read it
      // back rather than assume, and hand the wizard the fresh answer
      const after = await client!.onboarding();
      qc.setQueryData(["onboarding"], after);
      return !!after.budgets_set;
    },
    onSuccess: (ok) => {
      setMsg(ok ? { text: "Saved — Continue below." }
        : { text: "Saved, but no food or everything-else budget is stored "
            + "yet — enter one above.", bad: true });
      qc.invalidateQueries({ queryKey: ["today"] });
      // the wizard marks the step from the read-back, never from the tap
      onSaved(ok);
    },
    onError: (e) => setMsg({ text: errText(e), bad: true }),
  });
  if (!init) return <Text style={ws.mut}>loading…</Text>;
  // an edit makes the last save's line stale
  const edit = (set: (v: string) => void) => (v: string) => {
    set(v); setMsg(null);
  };
  return (
    <View style={ws.body}>
      <Field label="Income (approved paychecks)" value={income}
             onChange={edit(setIncome)} suggestion={s0?.income_monthly}
             kept={keepIncome} />
      <View style={[ws.row, { justifyContent: "space-between" }]}>
        <Text style={{ color: C.text, fontSize: 14 }}>Bills (approved)</Text>
        <Text style={{ color: C.text, fontSize: 14 }}>{whole(bills)}</Text>
      </View>
      <Field label="Food & drink" value={food} onChange={edit(setFood)}
             suggestion={s0?.food_monthly} from={months || undefined}
             kept={keepFood} />
      <Field label="Everything else" value={other} onChange={edit(setOther)}
             suggestion={s0?.other_monthly} kept={keepOther} />
      <View style={[ws.row, { justifyContent: "space-between" }]}>
        <Text style={{ color: C.text, fontSize: 14 }}>
          Savings (what's left)</Text>
        <Text style={{ color: C.mut, fontSize: 14 }}>{whole(savings)}</Text>
      </View>
      {savings < 0 && (
        <Text style={[ws.mut, { color: C.warn }]}>
          The plan is {whole(-savings)} over income.</Text>
      )}
      {!viewer && (
        <Pressable style={[ws.btn, { alignSelf: "flex-start" },
                           save.isPending && { opacity: 0.5 }]}
                   disabled={save.isPending}
                   onPress={() => {
                     const refusal = manualBudgetsRefusal(food, other);
                     if (refusal) { setMsg({ text: refusal, bad: true }); return; }
                     save.mutate();
                   }}>
          <Text style={ws.btnText}>
            {save.isPending ? "Saving…" : "Save budgets"}</Text>
        </Pressable>
      )}
      {msg ? <Text style={[ws.mut, { color: msg.bad ? C.warn : C.good }]}>
        {msg.text}</Text> : null}
    </View>
  );
}
